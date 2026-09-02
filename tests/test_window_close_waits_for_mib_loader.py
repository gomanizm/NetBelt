"""ウィンドウを閉じるとき、MIB 読み込みスレッドの終了を待つことを検証する。

SNMPPanel は生成時にバックグラウンドの MIBLoaderThread（QThread）を
起動する。MainWindow.closeEvent は Trap 受信などのスレッドは止めるが、
このスレッドは待っていなかった。ウィンドウがスレッドより先に破棄されると

  - 実行中の QThread が破棄されて Qt が abort する
  - 終わったスレッドの finished_signal が解放済みのパネルへ届く

のどちらかで、プロセスごと落ちる。起動直後にアプリを閉じた利用者が
踏む経路で、テストスイートでは MainWindow を作っては閉じる回数が増える
ほど当たりやすく、無関係な後続テストの processEvents() で access
violation として現れていた（間欠 segfault の原因の 1 つ）。

MIB の読み込みは数 ms で終わることが多く、素のままでは当たりにくい。
テストでは読み込みを遅くして、待たない実装が必ず落ちるようにする。
"""
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class WindowCloseWaitsForMibLoaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_loader_thread_has_finished_when_close_returns(self):
        """close() から戻った時点で、MIB 読み込みスレッドが動いていないこと。"""
        from ui import snmp_panel as mod
        real = mod.get_resolver

        def slow_resolver(*args, **kwargs):
            time.sleep(0.5)
            return real(*args, **kwargs)

        with mock.patch.object(mod, "get_resolver", slow_resolver):
            from ui.main_window import MainWindow
            window = MainWindow()
            thread = window.snmp_panel.mib_thread
            self.assertTrue(thread.isRunning(), "前提: 読み込み中である")

            window.close()

            self.assertFalse(thread.isRunning(),
                             "閉じたのに MIB 読み込みスレッドが動いている")
        # 後片付け（待たない実装でもプロセスを道連れにしないため）
        thread.wait(5000)


class WindowCloseDoesNotCrashTest(unittest.TestCase):
    """別プロセスで、閉じてすぐ破棄しても落ちないことを確かめる。

    落ちるとテストプロセスごと消えるので、同じプロセスでは検証できない。
    """

    SCRIPT = textwrap.dedent('''
        import gc, os, sys, time
        sys.path.insert(0, %(src)r)
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from unittest import mock
        from PyQt6.QtWidgets import QApplication
        app = QApplication([])
        from ui import snmp_panel as mod
        real = mod.get_resolver
        def slow(*a, **k):
            time.sleep(0.4)
            return real(*a, **k)
        with mock.patch.object(mod, "get_resolver", slow), \\
             mock.patch("ui.main_window.MainWindow._check_for_updates_on_startup"):
            from ui.main_window import MainWindow
            for _ in range(3):
                w = MainWindow()
                w.close()
                del w
                gc.collect()
                end = time.monotonic() + 0.8
                while time.monotonic() < end:
                    app.processEvents()
                    time.sleep(0.01)
        print("SURVIVED")
    ''')

    def test_closing_right_after_opening_does_not_crash(self):
        work = tempfile.mkdtemp(prefix="netbelt-close-")
        script = os.path.join(work, "run.py")
        with io.open(script, "w", encoding="utf-8") as f:
            f.write(self.SCRIPT % {"src": os.path.abspath("src")})
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"

        proc = subprocess.run([sys.executable, script], env=env,
                              capture_output=True, timeout=180)

        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0,
                         "閉じた直後に落ちている (exit=%s)\n%s"
                         % (proc.returncode, err[-600:]))
        self.assertIn("SURVIVED", out)
        self.assertNotIn("Destroyed while thread is still running", err,
                         "実行中の QThread を破棄している")


if __name__ == "__main__":
    unittest.main()
