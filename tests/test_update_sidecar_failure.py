"""検証記録（.sha256 / .version）を書けなかったダウンロードを、成功にしないこと。

検査役の実測: os.replace で ZIP を最終名にしたあとに控えを書いていたため、
控えの書き込みが失敗しても download_update は ZIP のパスを返していた。
画面は「ダウンロード完了！」まで進むのに、適用は is_verified_update が
False のため必ず拒否される。

    [VersionManager] チェックサムの控えを書けませんでした: [Errno 28] No space left on device
    download_update の戻り値 = ...\\NetBelt-1.3.2.zip
    is_verified_update() = False
    verify_before_apply(zip, '1.3.2') = 'ダウンロードした更新ファイルが、表示していた内容と
    一致しません。もう一度ダウンロードしてください。'

さらに、同じ版を取り直したときは直前まで適用できていた検証済み ZIP を
上書きしてから控えの更新に失敗するため、使えていた更新まで壊れた
（実測の CASE 2）。.version だけ失敗した場合は print すら出なかった
（CASE 3）。
"""
import hashlib
import io
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, "src")

URL = "https://api.github.com/repos/example/NetBelt/releases/assets/1"
VERSION = "1.3.2"
BODY_A = b"PK" + b"payload-A" * 100
BODY_B = b"PK" + b"payload-B" * 100


class _Resp:
    def __init__(self, content=b"", text=""):
        self.content = content
        self.text = text
        self.status_code = 200
        self.headers = {"content-length": str(len(content))}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=8192):
        yield self.content

    def close(self):
        pass


def _fake_get(body):
    def get(url, **kwargs):
        if str(url).endswith(".sha256"):
            return _Resp(text=hashlib.sha256(body).hexdigest())
        return _Resp(content=body)
    return get


class _FailingOpen:
    """指定した拡張子への書き込みだけを OSError にする open。"""

    def __init__(self, suffixes):
        self.suffixes = suffixes
        self.real = io.open

    def __call__(self, file, mode="r", *args, **kwargs):
        if "w" in str(mode) and str(file).endswith(self.suffixes):
            raise OSError(28, "No space left on device")
        return self.real(file, mode, *args, **kwargs)


class SidecarWriteFailureTest(unittest.TestCase):
    def setUp(self):
        from core.version_manager import VersionManager
        self.tmp = tempfile.mkdtemp(prefix="netbelt-sidecar-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = unittest.mock.patch.object(
            VersionManager, "UPDATE_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mgr = VersionManager()

    def _download(self, body, failing_suffixes=None):
        get = _fake_get(body)
        with unittest.mock.patch("core.version_manager.requests.get", get):
            if failing_suffixes:
                with unittest.mock.patch(
                        "builtins.open", _FailingOpen(failing_suffixes)):
                    return self.mgr.download_update(
                        URL, sha256_url=URL + ".sha256", version=VERSION)
            return self.mgr.download_update(
                URL, sha256_url=URL + ".sha256", version=VERSION)

    def test_a_failed_sha256_sidecar_fails_the_download(self):
        """.sha256 を書けなければ失敗として戻ること（画面に完了を出さない）。"""
        result = self._download(BODY_A, failing_suffixes=(".sha256",))

        self.assertIsNone(
            result,
            "控えを書けなかったのに成功として戻った（画面は「ダウンロード完了！」"
            "まで進み、適用は必ず拒否される）")

    def test_a_failed_version_sidecar_fails_the_download(self):
        """.version を書けなければ失敗として戻ること。"""
        result = self._download(BODY_A, failing_suffixes=(".version",))

        self.assertIsNone(result, "控え(.version)を書けなかったのに成功として戻った")

    def test_no_unusable_zip_is_left_behind(self):
        """適用できない ZIP を未適用の更新として残さないこと。"""
        self._download(BODY_A, failing_suffixes=(".sha256",))

        self.assertEqual(self.mgr.get_pending_update_files(), [],
                         "検証できない ZIP が未適用の更新として残った: %s"
                         % os.listdir(self.tmp))

    def test_an_already_verified_zip_survives_a_failed_redownload(self):
        """検証済みの ZIP を、失敗する取り直しの巻き添えで壊さないこと。"""
        first = self._download(BODY_A)
        self.assertIsNotNone(first, "前提: 1回目は成功する")
        self.assertTrue(self.mgr.is_verified_update(first),
                        "前提: 1回目は検証済みになる")

        second = self._download(BODY_B, failing_suffixes=(".sha256",))

        self.assertIsNone(second)
        self.assertEqual(io.open(first, "rb").read(), BODY_A,
                         "適用できていた ZIP の中身を書き換えてしまった")
        self.assertTrue(
            self.mgr.is_verified_update(first),
            "適用できていた ZIP が、失敗した取り直しの巻き添えで壊れた")
        self.assertIsNone(self.mgr.verify_before_apply(first, VERSION))


if __name__ == "__main__":
    unittest.main()
