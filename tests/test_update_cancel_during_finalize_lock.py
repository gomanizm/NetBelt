"""確定ロックの待機中に中止しても、更新が公開されてしまう件の回帰。

実測（検査役 cx5j-check-release の p3_cancel_during_lock_wait.py、16101ef）:
同じ最終名の確定ロックを外から握っておき、download_update がその待ちへ
入ってから中止を立ててロックを放すと、

    中止を立てた時点でまだ待っていた: True
    download_update の戻り: ...\\NetBelt-1.3.2.zip
    更新フォルダ: ['NetBelt-1.3.2.zip', '...sha256', '...version']
    適用前検査 (None なら適用できる): None
    未適用の更新として拾われるか: ['NetBelt-1.3.2.zip']

最後の中止確認は `with _finalize_lock(zip_path)` の手前にあり、ロックを
取ってから最終名へ置き換えるまでの間には中止判定が無かった。そのため
中止したはずの更新が検証済みとして公開され、次回起動時に「未適用の更新」
として提示される。中止時に update_dialog 側は signal を外してスレッドを
切り離すので、公開された ZIP は誰にも使われないまま残る。
チェックサム取得中の中止（tests/test_update_cancel_during_checksum.py）と
同じ症状。

直し方: ロックを取った直後、退避・置き換えを始める前にもう一度
cancel_check を見て、立っていれば .part の3つを捨てて None を返す。

窓は短い（ロックが握られているのは os.replace 数回ぶん）が、判定を
1つ足すだけなので直しておく。
"""
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock

sys.path.insert(0, "src")

URL = "https://api.github.com/repos/example/NetBelt/releases/assets/1"
VERSION = "1.3.2"
BODY = b"PK" + b"payload-for-1.3.2" * 50


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


def _fake_get(url, **kwargs):
    if str(url).endswith(".sha256"):
        return _Resp(text=hashlib.sha256(BODY).hexdigest())
    return _Resp(content=BODY)


class CancelDuringFinalizeLockTest(unittest.TestCase):
    def setUp(self):
        from core.version_manager import VersionManager, _finalize_lock
        self.tmp = tempfile.mkdtemp(prefix="netbelt-cancel-lock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = unittest.mock.patch.object(
            VersionManager, "UPDATE_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mgr = VersionManager()
        self.zip_path = os.path.join(self.tmp, "NetBelt-%s.zip" % VERSION)
        self.cancelled = False
        # 同じ版の確定が別で走っている状態を作るための本物のロック
        self.busy = _finalize_lock(self.zip_path)
        self.waiting = threading.Event()

    def _start_download(self):
        """別スレッドで受信させ、(スレッド, 戻り値の入れ物) を返す。"""
        outer = self

        class _Gate:
            """ロック待ちへ入ったことを知らせてから、本物のロックを取る。"""

            def __enter__(self):
                outer.waiting.set()
                outer.busy.acquire()
                return self

            def __exit__(self, exc_type, exc, tb):
                outer.busy.release()
                return False

        result = {}

        def run():
            with unittest.mock.patch("core.version_manager.requests.get",
                                     _fake_get), \
                    unittest.mock.patch("core.version_manager._finalize_lock",
                                        lambda path: _Gate()):
                result["path"] = self.mgr.download_update(
                    URL, sha256_url=URL + ".sha256", version=VERSION,
                    cancel_check=lambda: self.cancelled)

        thread = threading.Thread(target=run, name="download")
        thread.start()
        return thread, result

    def _download_cancelled_while_waiting(self):
        """ロック待ちへ入ってから中止を立て、待ちを明ける。"""
        self.busy.acquire()
        try:
            thread, result = self._start_download()
            self.assertTrue(self.waiting.wait(60), "確定ロックの待ちへ入らない")
            # ここでロックを待って止まっている。利用者が中止を押す。
            self.cancelled = True
            self.assertTrue(thread.is_alive(), "待たずに通り抜けた")
        finally:
            self.busy.release()
        thread.join(60)
        self.assertFalse(thread.is_alive(), "受信スレッドが終わらない")
        return result.get("path")

    def test_cancelling_while_waiting_for_the_finalize_lock_aborts(self):
        """ロック待ちの間に中止したら、成功として戻らないこと。"""
        path = self._download_cancelled_while_waiting()

        self.assertIsNone(path, "ロック待ちの間に中止したのに成功として戻った")

    def test_nothing_is_published_after_that_cancel(self):
        """中止後に検証済み ZIP と検証記録を残さないこと。"""
        self._download_cancelled_while_waiting()

        self.assertEqual(os.listdir(self.tmp), [],
                         "中止したのに更新ファイルが残った")
        self.assertEqual(self.mgr.get_pending_update_files(), [],
                         "中止した更新が「未適用の更新」として提示される")

    def test_a_download_that_only_waits_still_succeeds(self):
        """中止しなければ、待たされても通常どおり公開されること。"""
        self.busy.acquire()
        try:
            thread, result = self._start_download()
            self.assertTrue(self.waiting.wait(60), "確定ロックの待ちへ入らない")
        finally:
            self.busy.release()
        thread.join(60)

        self.assertEqual(result.get("path"), self.zip_path,
                         "待たされただけの受信まで失敗させている")
        self.assertTrue(self.mgr.is_verified_update(self.zip_path))
        self.assertEqual(self.mgr.pending_version(self.zip_path), VERSION)


if __name__ == "__main__":
    unittest.main()
