"""ZIP の受信後、チェックサム取得中のキャンセルでも中止されること。

検査役の実測: cancel_check を見ているのはチャンクループの中だけで、
ループを抜けたあとのチェックサム取得（別の requests.get、timeout=30）と
os.replace の前には中止判定が無い。DownloadThread.cancel() が呼ぶ
VersionManager.abort() は受信済みの本体側の応答を閉じるだけなので、
この窓は短くならない。

    === ケースB: チェックサム取得中にキャンセル（指摘の経路） ===
    >>> ZIP 受信完了・チェックサム取得中に cancel() 相当を実行
    [VersionManager] チェックサム照合 OK
    download_update の戻り値: '...\\NetBelt-99.9.9.zip'
    更新ディレクトリの中身:
      NetBelt-99.9.9.zip  (2097156 bytes)
      NetBelt-99.9.9.zip.sha256  (64 bytes)
      NetBelt-99.9.9.zip.version  (6 bytes)
    get_pending_update_files(): ['...\\NetBelt-99.9.9.zip']

つまり中止したはずの更新が検証済みとして公開され、次回起動時に
「未適用の更新」として提示される条件が揃っていた。
"""
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, "src")

URL = "https://api.github.com/repos/example/NetBelt/releases/assets/1"
VERSION = "99.9.9"
BODY = b"PK" + b"payload-for-99.9.9" * 100


class _Resp:
    def __init__(self, content=b"", text=""):
        self.content = content
        self.text = text
        self.status_code = 200
        self.headers = {"content-length": str(len(content))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        step = max(1, len(self.content) // 4)
        for i in range(0, len(self.content), step):
            yield self.content[i:i + step]

    def close(self):
        pass


class CancelDuringChecksumTest(unittest.TestCase):
    def setUp(self):
        from core.version_manager import VersionManager
        self.tmp = tempfile.mkdtemp(prefix="netbelt-cancel-sha-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = unittest.mock.patch.object(
            VersionManager, "UPDATE_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mgr = VersionManager()
        self.cancelled = False

    def _get(self, cancel_on_checksum):
        """チェックサムを取りに来たところで cancel() 相当を起こす応答。"""
        def get(url, **kwargs):
            if str(url).endswith(".sha256"):
                if cancel_on_checksum:
                    # DownloadThread.cancel() と同じ操作
                    self.cancelled = True
                    self.mgr.abort()
                return _Resp(text=hashlib.sha256(BODY).hexdigest())
            return _Resp(content=BODY)
        return get

    def _download(self, cancel_on_checksum):
        with unittest.mock.patch("core.version_manager.requests.get",
                                 self._get(cancel_on_checksum)):
            return self.mgr.download_update(
                URL, sha256_url=URL + ".sha256", version=VERSION,
                cancel_check=lambda: self.cancelled)

    def test_cancelling_while_fetching_the_checksum_aborts(self):
        result = self._download(cancel_on_checksum=True)

        self.assertIsNone(
            result, "チェックサム取得中に中止したのに成功として戻った")

    def test_nothing_is_published_after_that_cancel(self):
        """中止後に検証済み ZIP と検証記録を残さないこと。"""
        self._download(cancel_on_checksum=True)

        self.assertEqual(os.listdir(self.tmp), [],
                         "中止したのに更新ファイルが残った")
        self.assertEqual(self.mgr.get_pending_update_files(), [],
                         "中止した更新が「未適用の更新」として提示される")

    def test_an_uncancelled_download_still_succeeds(self):
        """中止しない通常の経路を壊していないこと。"""
        result = self._download(cancel_on_checksum=False)

        self.assertIsNotNone(result, "通常のダウンロードまで失敗させている")
        self.assertTrue(self.mgr.is_verified_update(result))
        self.assertEqual(self.mgr.pending_version(result), VERSION)


if __name__ == "__main__":
    unittest.main()
