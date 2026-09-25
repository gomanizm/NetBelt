"""確定の目印まわりの積み残し（持ち主の印・置き土産の掃除・理由の伝達）。

利用者の決定（2026-09-20 / release-04）: 同じ更新の確定はプロセス間で排他する。
更新フォルダに O_EXCL の目印を作って確定手順を囲み、取れなかった側は数秒待って
『別の NetBelt が同じ更新を保存中です』で失敗させる（嘘の成功を返さない）。
異常終了で残った目印は、古くなっていれば引き取る。

その実装（tests/test_update_finalize_cross_process.py）に、8 周目の検査役が
実測で 3 つの穴を見つけた。

1. 目印に持ち主の印が無い（実測: 検査役 cx5m-check-release の
   p23_finalize_marker.py part1。FINALIZE_STALE_SEC=0.2 / FINALIZE_WAIT_SEC=2 で
   3 者ぶん呼ぶ）: P1 が持ったまま古さの境を越えると P2 が引き取って owned=True に
   なる。そのあと P1 の後始末が、名前だけを見て P2 の目印を消してしまうため、
   待たされるはずの P3 が 0.00 秒で通った（同時に持つ数 = 2）。updater.bat 側が
   holder.txt で避けている型の穴がそのまま残っていた。

2. 異常終了で残る目印を誰も掃除しない（同 part2）: <版>.zip.finalizing と
   <版>.zip.finalizing.<pid>.stale を 48 時間前の日付で置いて
   cleanup_old_updates(24) を呼んでも、消した数 0 で両方そのまま。
   get_pending_update_files は .zip しか見ず、書きかけの掃除は
   '.part' / '.part.sha256' / '.part.version' しか見ていなかった。実際に kill
   された場合、0 バイトのファイルが %TEMP%\\NetBeltUpdates に残り続ける。

3. 決定に書かれた文言が利用者へ届かない: 『別の NetBelt が同じ更新を保存中です』は
   print だけで、download_update は None を返していた。画面に出るのは
   src/ui/dialogs/update_dialog.py の『ダウンロードに失敗しました（チェックサムが
   一致しない場合も含みます）』で、原因と違うことを名指ししている。凍結ビルドでは
   stdout はログファイル行きなので、画面には何も出ない。

直し方: (1) 目印の中へ持ち主の印を書き、後始末は中身が自分のものだったときだけ
外す（引き取りと同じく、一意名へ改名してから消す）。(2) cleanup_old_updates の
書きかけの掃除に '.finalizing' と '.finalizing.*.stale' を足す（保持期間を
過ぎたものだけ）。(3) VersionManager に最後の失敗の理由を持たせ、
DownloadThread がそれを伝える。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

sys.path.insert(0, "src")

URL = "https://api.github.com/repos/example/NetBelt/releases/assets/1"
VERSION = "1.3.2"
BODY = b"PK" + b"A" * 1000
BUSY_MESSAGE = "別の NetBelt が同じ更新を保存中です"
GENERIC_MESSAGE = "チェックサムが一致しない場合も含みます"


class _Response:
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
    import hashlib
    if str(url).endswith(".sha256"):
        return _Response(text=hashlib.sha256(BODY).hexdigest())
    return _Response(content=BODY)


class FinalizeMarkerTest(unittest.TestCase):

    def setUp(self):
        from core.version_manager import VersionManager
        self.VersionManager = VersionManager
        self.tmp = tempfile.mkdtemp(prefix="netbelt-finmarker-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = unittest.mock.patch.object(
            VersionManager, "UPDATE_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _names(self):
        return sorted(os.listdir(self.tmp))

    def test_a_finished_owner_leaves_the_marker_of_the_one_that_took_over(self):
        """引き取りが起きた後、先に終わった側が他人の目印を外さないこと。"""
        from core.version_manager import _cross_process_finalize

        zip_path = os.path.join(self.tmp, "NetBelt-%s.zip" % VERSION)
        marker = zip_path + ".finalizing"

        with unittest.mock.patch("core.version_manager.FINALIZE_STALE_SEC", 0.2), \
             unittest.mock.patch("core.version_manager.FINALIZE_WAIT_SEC", 2.0):
            first = _cross_process_finalize(zip_path)
            self.assertTrue(first.__enter__(), "1 本目が目印を取れない")
            try:
                # 確定が長引いて、古さの境を越えた状態を作る。
                time.sleep(0.35)
                second = _cross_process_finalize(zip_path)
                self.assertTrue(second.__enter__(),
                                "前提が崩れている（引き取りが起きない）")
            except BaseException:
                first.__exit__(None, None, None)
                raise

            try:
                # 1 本目が確定を終える。目印はもう 2 本目のもの。
                first.__exit__(None, None, None)
                self.assertTrue(
                    os.path.exists(marker),
                    "引き取った側の目印を消した: %s" % (self._names(),))

                # 3 本目は待たされたうえで失敗すること（同時に 2 本入らない）。
                with unittest.mock.patch(
                        "core.version_manager.FINALIZE_STALE_SEC", 60.0), \
                     unittest.mock.patch(
                        "core.version_manager.FINALIZE_WAIT_SEC", 0.3):
                    third = _cross_process_finalize(zip_path)
                    owned = third.__enter__()
                    third.__exit__(None, None, None)
                self.assertFalse(owned, "同時に 2 本が確定へ入った")
            finally:
                second.__exit__(None, None, None)

            # 持ち主が外すぶんには、ちゃんと消えること。
            self.assertFalse(os.path.exists(marker),
                             "持ち主の後始末で消えない: %s" % (self._names(),))

    def test_cleanup_takes_away_finalize_markers_left_by_a_kill(self):
        """異常終了で残った確定の目印も、保持期間を過ぎたら掃除すること。"""
        old = time.time() - 48 * 3600
        stale_names = ["NetBelt-%s.zip.finalizing" % VERSION,
                       "NetBelt-%s.zip.finalizing.4321.stale" % VERSION]
        for name in stale_names:
            path = os.path.join(self.tmp, name)
            open(path, "wb").close()
            os.utime(path, (old, old))
        # 今まさに確定している最中のものは、消さないこと。
        fresh = os.path.join(self.tmp, "NetBelt-9.9.9.zip.finalizing")
        open(fresh, "wb").close()

        deleted = self.VersionManager().cleanup_old_updates(24)

        self.assertEqual(deleted, 2, self._names())
        self.assertEqual(self._names(), [os.path.basename(fresh)],
                         "置き去りの目印が残った")

    def test_the_dialog_is_told_that_another_netbelt_is_saving(self):
        """取れなかったときの理由が、画面へ出る文言として届くこと。"""
        from ui.dialogs.update_dialog import DownloadThread

        name = self.VersionManager._download_filename(URL, VERSION)
        marker = os.path.join(self.tmp, name + ".finalizing")
        open(marker, "wb").close()

        thread = DownloadThread(URL, None, None,
                                sha256_url=URL + ".sha256", version=VERSION)
        messages = []
        thread.download_failed.connect(messages.append)
        with unittest.mock.patch("core.version_manager.requests.get", _fake_get), \
             unittest.mock.patch("core.version_manager.FINALIZE_WAIT_SEC", 0.3):
            thread.run()

        self.assertEqual(len(messages), 1,
                         "失敗を 1 回だけ伝えること: %s" % (messages,))
        self.assertIn(BUSY_MESSAGE, messages[0])
        self.assertNotIn(GENERIC_MESSAGE, messages[0],
                         "原因と違うことを名指ししている")


if __name__ == "__main__":
    unittest.main()
