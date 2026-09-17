"""機器一覧の「切断」ボタンが実際に切断すること。

ボタンは有効・表示状態で並んでいるのに clicked がどこにも繋がっておらず、
接続中の機器を選んで押しても何も起きなかった（接続は残ったまま、
ステータスバーの表示も変わらない）。押しても無反応のボタンは、
切断できたと思わせたまま接続を生かし続ける。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class DisconnectButtonTest(unittest.TestCase):
    """「切断」ボタンの配線。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.windows = []       # ウィンドウを先に捨てると子ごと消えるので保持する

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-disconnect-btn-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        type(self).windows.append(w)
        return w

    def _select(self, w, name):
        """機器一覧で name の機器が選択されている状態にする。"""
        w.device_tree.get_selected_device = lambda: ("グループA", {"name": name})

    def test_pressing_it_disconnects_the_selected_device(self):
        w = self._window()
        conn = mock.Mock()
        w.connections["ルータA"] = conn
        self._select(w, "ルータA")

        w.device_tree.btn_disconnect.click()

        self.assertEqual(conn.disconnect.call_count, 1,
                         "切断ボタンが接続を切っていない")
        self.assertNotIn("ルータA", w.connections,
                         "切断後も接続が登録されたまま残っている")

    def test_pressing_it_reports_the_disconnect(self):
        w = self._window()
        w.connections["ルータA"] = mock.Mock()
        self._select(w, "ルータA")

        w.device_tree.btn_disconnect.click()

        self.assertIn("ルータA", w.status_bar.currentMessage())

    def test_pressing_it_cleans_up_the_sftp_session(self):
        """SFTP セッションもタブの × と同じように片付けること。"""
        w = self._window()
        w.connections["ルータA"] = mock.Mock()
        sftp = mock.Mock()
        w.sftp_managers["ルータA"] = sftp
        self._select(w, "ルータA")

        w.device_tree.btn_disconnect.click()

        sftp.disconnect.assert_called_once()
        self.assertNotIn("ルータA", w.sftp_managers)

    def test_pressing_it_with_nothing_selected_does_nothing(self):
        w = self._window()
        conn = mock.Mock()
        w.connections["ルータA"] = conn
        w.device_tree.get_selected_device = lambda: None

        w.device_tree.btn_disconnect.click()

        self.assertIn("ルータA", w.connections)
        conn.disconnect.assert_not_called()

    def test_pressing_it_for_a_device_that_is_not_connected_is_harmless(self):
        w = self._window()
        conn = mock.Mock()
        w.connections["ルータA"] = conn
        self._select(w, "ルータB")

        w.device_tree.btn_disconnect.click()

        self.assertIn("ルータA", w.connections)
        conn.disconnect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
