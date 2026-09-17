"""更新ダイアログを閉じたときに、ダウンロードスレッドが片付くことを確認する。

実測（inspector, release #2）の3点:

* Esc は QDialog.reject() を呼ぶだけで closeEvent を通らない。
  `dialog visible: False result: 0 thread running: True _cancelled flag:
  False` — 確認も中止もされないまま、スレッドだけが走り続けた。
* キャンセルボタンは wait(10000) で GUI スレッドを止める。
  `cancel path took 10.0s`。
* どちらの経路でも、実行中の QThread をダイアログが持ったまま親が破棄され、
  `QThread: Destroyed while thread '' is still running` の致命エラーで
  `EXIT: -1073740791 (hex 0xC0000409)` になった。

相手が黙り込んだ状態を、`download_update` が cancel_check を見ずに
数秒眠る形で作る（実際には読み取りのタイムアウト 60 秒まで戻らない）。
"""
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, "src")

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")
STUCK_SECONDS = 4.0
# 「待たずに戻ること」の境目。止まった受信より十分短く取る
PROMPT = 2.0

CHILD = '''
import gc
import os
import sys
import time
import types

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, sys.argv[1])

from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget
import core.config_manager as config_manager
import core.version_manager as version_manager


def stuck(self, url, progress_callback=None, sha256_url=None, version=None,
          cancel_check=None):
    time.sleep(%(stuck)s)
    return None


version_manager.VersionManager.download_update = stuck
config_manager.ConfigManager = lambda *a, **k: types.SimpleNamespace(
    get_github_token=lambda: None)
QMessageBox.question = staticmethod(
    lambda *a, **k: QMessageBox.StandardButton.Yes)

from ui.dialogs.update_dialog import UpdateDialog

app = QApplication([])
parent = QWidget()
dialog = UpdateDialog(parent, {"version": "9.9.9",
                               "download_url": "https://example.com/a.zip"})
dialog.show()
dialog._on_download_clicked()
thread = dialog.download_thread
assert thread is not None and thread.isRunning(), "thread did not start"

dialog.reject()
print("still running after reject:", thread.isRunning(), flush=True)

dialog = None
parent = None
gc.collect()
app.processEvents()
print("SURVIVED", flush=True)
try:
    thread.wait(15000)
except RuntimeError:
    pass
sys.exit(0)
''' % {"stuck": STUCK_SECONDS}


def _stuck_download(self, url, progress_callback=None, sha256_url=None,
                    version=None, cancel_check=None):
    """相手が黙り込んだ受信。中止の要求を見ずに眠る。"""
    time.sleep(STUCK_SECONDS)
    return None


class DialogClosesWithoutBlockingTest(unittest.TestCase):
    """閉じる経路が、GUI を止めずにスレッドを手放すこと。"""

    def setUp(self):
        from PyQt6.QtWidgets import QMessageBox
        from core.version_manager import VersionManager

        for p in (unittest.mock.patch.object(
                      VersionManager, "download_update", _stuck_download),
                  unittest.mock.patch(
                      "core.config_manager.ConfigManager",
                      lambda *a, **k: unittest.mock.Mock(
                          get_github_token=lambda: None)),
                  unittest.mock.patch.object(
                      QMessageBox, "question",
                      lambda *a, **k: QMessageBox.StandardButton.Yes)):
            p.start()
            self.addCleanup(p.stop)

    def _downloading_dialog(self):
        from ui.dialogs.update_dialog import UpdateDialog

        dialog = UpdateDialog(None, {"version": "9.9.9",
                                     "download_url": "https://example.com/a.zip"})
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        dialog._on_download_clicked()
        thread = dialog.download_thread
        self.assertIsNotNone(thread, "ダウンロードが始まっていない")
        self.assertTrue(thread.isRunning(), "ダウンロードが始まっていない")
        self.addCleanup(thread.wait, 15000)
        return dialog, thread

    def _check_released(self, dialog, thread, elapsed):
        self.assertLess(elapsed, PROMPT,
                        "閉じるのに %.1f 秒 GUI を止めた" % elapsed)
        self.assertTrue(thread._cancelled, "中止が伝わっていない")
        self.assertIsNone(dialog.download_thread,
                          "ダイアログがスレッドを持ったまま閉じた")
        self.assertIsNone(thread.parent(),
                          "実行中のスレッドがダイアログの子のまま"
                          "（親の破棄で QThread ごと消えて落ちる）")
        self.assertFalse(dialog.isVisible(), "ダイアログが閉じていない")

    def test_escape_cancels_the_download(self):
        """Esc（= reject）も、閉じるボタンと同じ経路を通ること。"""
        dialog, thread = self._downloading_dialog()

        start = time.monotonic()
        dialog.reject()
        elapsed = time.monotonic() - start

        self._check_released(dialog, thread, elapsed)

    def test_closing_does_not_freeze_the_gui(self):
        """閉じる操作が、受信の終わりを待って固まらないこと。"""
        dialog, thread = self._downloading_dialog()

        start = time.monotonic()
        dialog.close()
        elapsed = time.monotonic() - start

        self._check_released(dialog, thread, elapsed)

    def test_saying_no_keeps_the_download(self):
        """「いいえ」と答えたら、閉じずに続けること。"""
        from PyQt6.QtWidgets import QMessageBox

        dialog, thread = self._downloading_dialog()
        with unittest.mock.patch.object(
                QMessageBox, "question",
                lambda *a, **k: QMessageBox.StandardButton.No):
            dialog.reject()

        self.assertIs(dialog.download_thread, thread,
                      "続けると答えたのにスレッドを手放した")
        self.assertFalse(thread._cancelled, "続けると答えたのに中止した")


class ParentDestructionTest(unittest.TestCase):
    """実行中のスレッドを抱えたまま親を壊しても、落ちないこと。

    壊れる版はプロセスごと落ちる（0xC0000409）ので、別プロセスで確かめる。
    """

    def test_destroying_the_parent_does_not_kill_the_process(self):
        script = os.path.join(tempfile.mkdtemp(prefix="netbelt-parent-"),
                              "child.py")
        io.open(script, "w", encoding="utf-8").write(CHILD)

        proc = subprocess.run([sys.executable, script, SRC],
                              capture_output=True, timeout=120)
        out = (proc.stdout + proc.stderr).decode("utf-8", "replace")

        self.assertEqual(proc.returncode, 0,
                         "親の破棄でプロセスが落ちた (rc=%d)\n%s"
                         % (proc.returncode, out))
        self.assertIn("SURVIVED", out, out)


class CancelStopsTheReadTest(unittest.TestCase):
    """中止の要求が、止まっている読み取りを実際に打ち切ること。"""

    def test_abort_closes_the_active_response(self):
        from core.version_manager import VersionManager

        closed = threading.Event()

        class _StalledResponse:
            status_code = 200
            headers = {"content-length": "1000"}

            def raise_for_status(self):
                pass

            def close(self):
                closed.set()

            def iter_content(self, chunk_size=8192):
                yield b"x" * 10
                # 相手が黙り込んだ状態。close() されるまで戻らない
                closed.wait(30)
                raise IOError("読み取りが打ち切られました")

        tmp = tempfile.mkdtemp(prefix="netbelt-abort-")
        mgr = VersionManager()
        mgr.UPDATE_DIR = tmp
        result = {}

        def run():
            with unittest.mock.patch("core.version_manager.requests.get",
                                     return_value=_StalledResponse()):
                result["path"] = mgr.download_update(
                    "https://example.com/a.zip",
                    sha256_url="https://example.com/a.zip.sha256")

        worker = threading.Thread(target=run)
        worker.start()
        for _ in range(100):
            if mgr._response is not None:
                break
            time.sleep(0.02)

        start = time.monotonic()
        mgr.abort()
        worker.join(15)
        elapsed = time.monotonic() - start

        self.assertFalse(worker.is_alive(), "中止しても受信が終わらない")
        self.assertLess(elapsed, 5.0, "中止から %.1f 秒かかった" % elapsed)
        self.assertTrue(closed.is_set(), "応答を閉じていない")
        self.assertIsNone(result.get("path"))
        self.assertEqual(os.listdir(tmp), [], "書きかけが残っている")


if __name__ == "__main__":
    unittest.main()
