"""ポートチェッカーを開いたままでも、メインを閉じれば終了できることを検証する。

_on_port_checker が作る PortCheckerGUI は親を持たないトップレベルで、
MainWindow.closeEvent は別ウィンドウにしたツール（_detached）は閉じるが
こちらは閉じていなかった。quitOnLastWindowClosed は既定 True なので、
見えているウィンドウが 1 つでも残ると app.exec() が戻らず、
NetBelt.exe がプロセスとして居座る。

計測: main() 経路でメインを閉じたあとも exec() が回り続け、可視の
トップレベルとして ('PortCheckerGUI', 'ポートチェッカー') だけが残った。
closeEvent は既にサーバ類・接続を止めているので、残るのは何も動かない
抜け殻の窓になる。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class PortCheckerShutdownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 破棄済みウィジェットへのシグナル配送で落ちないよう保持する
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-portchk-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        type(self)._windows.append(w)
        return w

    def _with_port_checker(self):
        w = self._window()
        w._on_port_checker()
        pc = w.port_checker_window
        self.assertTrue(pc.isVisible(), "前提: ポートチェッカーが出ている")
        return w, pc

    def test_the_port_checker_is_closed_when_the_app_exits(self):
        """メインを閉じたら、ポートチェッカーも閉じること。"""
        w, pc = self._with_port_checker()
        w.close()
        self.assertFalse(pc.isVisible(),
                         "ポートチェッカーが残り、アプリが終了できない")

    def test_no_visible_window_is_left_behind(self):
        """見えているトップレベルを 1 つも残さないこと。

        1 つでも残ると quitOnLastWindowClosed が働かず、
        イベントループが終わらない。
        """
        from PyQt6.QtWidgets import QApplication
        w, _ = self._with_port_checker()
        w.close()
        for _ in range(3):
            QApplication.instance().processEvents()

        leftover = [x for x in QApplication.instance().topLevelWidgets()
                    if x.isVisible()]
        self.assertEqual(leftover, [],
                         "見えたままのウィンドウが残っている: %s" % leftover)

    def test_closing_without_ever_opening_it_still_works(self):
        """一度も開いていなければ、これまでどおり何事もなく閉じること。"""
        w = self._window()
        w.close()
        self.assertFalse(w.isVisible())

    def test_a_port_checker_closed_by_hand_does_not_break_shutdown(self):
        """利用者が先に閉じていても、終了処理が落ちないこと。"""
        w, pc = self._with_port_checker()
        pc.close()
        w.close()
        self.assertFalse(w.isVisible())


if __name__ == "__main__":
    unittest.main()
