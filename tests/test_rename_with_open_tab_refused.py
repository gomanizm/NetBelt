"""タブを開いている機器の改名を断ることを検証する。

改名すると config と接続先リストは新しい名前になるが、タブ・接続・マクロの
実行状態は古い名前のまま残る。キープアライブとマクロの操作は接続先リストの
「ツール」から名前で引くので、改名したとたんにそのセッションを操作できなく
なり、実行中のマクロも止められなかった（検査役が確認。以前は端末の右クリック
から止められた）。

タブを開いている間の改名は断り、タブを閉じてから変えるよう伝える（利用者判断
2026-09-18）。名前以外の変更と、タブの無い機器の改名はこれまでどおり通す。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


def _device(name="rtrA", **extra):
    data = {"name": name, "host": "192.0.2.10", "port": 22, "protocol": "ssh",
            "username": "cisco", "password": "", "ssh_key": "", "macros": []}
    data.update(extra)
    return data


class RenameWithOpenTabRefusedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-rename-open-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            window = MainWindow()
        type(self)._windows.append(window)
        return window

    def _edit(self, window, old, new):
        """編集ダイアログで new を入力して OK したことにする。(警告, 保存) を返す。"""
        from PyQt6.QtWidgets import QDialog
        dialog = mock.Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = new
        dialog.get_selected_group.return_value = "Default"
        dialog.group_combo.findText.return_value = 0
        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn, \
                mock.patch.object(window, "_load_devices"), \
                mock.patch.object(window.config_manager, "update_device",
                                  return_value=True) as update_device:
            window._on_device_edit("Default", old)
        return warn, update_device

    def test_renaming_a_device_with_an_open_tab_is_refused(self):
        """タブを開いている機器は改名できず、タブを閉じるよう伝えること。"""
        window = self._window()
        old = _device("rtrA")
        window.terminal_widget.create_terminal_tab("rtrA")
        window.device_info["rtrA"] = old

        warn, update_device = self._edit(window, old, _device("rtrB"))

        self.assertFalse(update_device.called, "タブを開いたまま改名できてしまう")
        warn.assert_called_once()
        message = warn.call_args[0][2]
        self.assertIn("rtrA", message)
        self.assertIn("タブを閉じて", message, "どうすれば変えられるかを伝えていない")
        self.assertIn("rtrA", window.device_info, "断ったのに再接続用の写しを変えた")
        self.assertNotIn("rtrB", window.device_info)

    def test_renaming_a_device_with_a_live_connection_is_refused(self):
        """接続だけが残っている場合（タブを作る前など）も断ること。"""
        window = self._window()
        old = _device("rtrA")
        window.connections["rtrA"] = object()
        self.addCleanup(window.connections.pop, "rtrA", None)

        warn, update_device = self._edit(window, old, _device("rtrB"))

        self.assertFalse(update_device.called, "接続中のまま改名できてしまう")
        warn.assert_called_once()

    def test_other_changes_to_a_device_with_an_open_tab_are_saved(self):
        """名前も接続先も変えない編集は、タブを開いていても保存すること（回帰防止）。

        接続先（ホストなど）の変更は、タブを開いている間は断る（利用者判断
        2026-09-20、test_endpoint_change_with_open_tab_refused.py）。ここでは
        接続先以外のユーザー名を変える。
        """
        window = self._window()
        old = _device("rtrA")
        window.terminal_widget.create_terminal_tab("rtrA")

        warn, update_device = self._edit(window, old,
                                         _device("rtrA", username="admin"))

        self.assertTrue(update_device.called, "名前以外の変更まで断っている")
        warn.assert_not_called()

    def test_a_device_without_a_tab_can_still_be_renamed(self):
        """タブを開いていない機器は、これまでどおり改名できること。"""
        window = self._window()
        old = _device("rtrA")

        warn, update_device = self._edit(window, old, _device("rtrB"))

        self.assertTrue(update_device.called, "タブの無い機器まで改名を断っている")
        warn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
