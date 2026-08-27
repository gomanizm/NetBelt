"""別ウィンドウにしたツールを残したまま終了できることを検証する。

_detach_tool が作る DetachedToolWindow は親を持たないトップレベルで、
MainWindow.closeEvent はこれを閉じていなかった。
QApplication の quitOnLastWindowClosed は既定 True なので、Qt は
「最後のウィンドウが閉じたとき」にしかイベントループを抜けない。
デタッチした窓が見えたまま残るとメインウィンドウを閉じても終了せず、
NetBelt.exe がプロセスとして居座る。

しかも closeEvent は先に Syslog 受信・TFTP/FTP/SFTP サーバ・SNMP の
スレッドを止めているので、残るのは何も動かない抜け殻の窓になる。
（パネル自身は生きているので、終了したはずのプロセス上で受信を
再開できてしまう。）

閉じるときは、窓が予約する「タブへ戻す」処理を先に止めておく必要が
ある。DetachedToolWindow.closeEvent は QTimer.singleShot(0, ...) で
reparent を次のイベントループへ回すので、終了処理の最中にそれが走ると
ウィンドウの破棄と競合する。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class DetachedToolShutdownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-detach-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            return MainWindow()

    def _detached(self, key="syslog"):
        w = self._window()
        w._detach_tool(key)
        win = w._detached[key]
        self.assertTrue(win.isVisible(), "前提: 別ウィンドウが出ている")
        return w, win

    def test_a_detached_window_is_closed_when_the_app_exits(self):
        """終了時に別ウィンドウも閉じること。"""
        w, win = self._detached()
        w.close()
        self.assertFalse(win.isVisible(),
                         "別ウィンドウが残り、アプリが終了できない")

    def test_no_visible_window_is_left_behind(self):
        """見えているトップレベルを1つも残さないこと。

        1つでも残ると quitOnLastWindowClosed が働かず、
        イベントループが終わらない。
        """
        from PyQt6.QtWidgets import QApplication
        w, _ = self._detached()
        w.close()
        for _ in range(3):
            QApplication.instance().processEvents()

        leftover = [x for x in QApplication.instance().topLevelWidgets()
                    if x.isVisible()]
        self.assertEqual(leftover, [],
                         "見えたままのウィンドウが残っている: %s" % leftover)

    def test_shutting_down_does_not_schedule_a_reattach(self):
        """終了中に「タブへ戻す」処理を予約しないこと。

        予約されると次のイベントループで reparent が走り、
        破棄処理と競合する。
        """
        from PyQt6.QtWidgets import QApplication
        w, _ = self._detached()

        with mock.patch.object(type(w), "_reattach_tool") as reattach:
            w.close()
            for _ in range(3):
                QApplication.instance().processEvents()

        reattach.assert_not_called()

    def test_two_detached_windows_are_both_closed(self):
        """複数を切り離していても、全部閉じること。"""
        w = self._window()
        w._detach_tool("syslog")
        w._detach_tool("snmp")
        windows = list(w._detached.values())

        w.close()

        for win in windows:
            self.assertFalse(win.isVisible(),
                             "閉じられていない別ウィンドウがある")

    def test_closing_a_detached_window_by_hand_still_puts_it_back(self):
        """利用者が窓を閉じたときは、これまでどおりタブへ戻すこと。"""
        from PyQt6.QtWidgets import QApplication
        w, win = self._detached()

        win.close()
        for _ in range(3):
            QApplication.instance().processEvents()

        self.assertNotIn("syslog", w._detached,
                         "タブへ戻す処理が働いていない")


if __name__ == "__main__":
    unittest.main()
