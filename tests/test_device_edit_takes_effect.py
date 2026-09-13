"""編集した接続情報が、開いているタブの再接続に届くことを検証する。

実機で踏んだ経路:

  正しい鍵で接続 → 切断 → 機器を編集して、存在しない鍵のパスを指定
  → ターミナルで Enter を押して再接続 → **繋がってしまう**

再接続は config ではなく device_info の写しを見ており、その写しが
編集で更新されていなかった。鍵に限らず、ユーザー名・パスワード・ホストを
変えても同じで、古い接続情報のまま繋がり続ける。パスワードを変えて
締め出したつもりの相手が、そのタブからは入れることになる。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class DeviceEditReachesReconnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 破棄済みウィジェットへのシグナル配送でプロセスごと落ちるため保持する
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _window(self):
        from ui.main_window import MainWindow
        with mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            window = MainWindow()
        type(self)._windows.append(window)
        return window

    def _old(self):
        return {"name": "Ubuntu0", "host": "192.0.2.10", "port": 22,
                "protocol": "ssh", "username": "cisco", "password": "",
                "ssh_key": r"C:\keys\netbelt_test", "macros": []}

    def _edit(self, window, old, new):
        """編集ダイアログで new を返させ、編集処理を通す。"""
        dialog = mock.Mock()
        dialog.exec.return_value = 1          # Accepted
        dialog.get_device_data.return_value = new
        dialog.get_selected_group.return_value = "Default"
        dialog.group_combo.findText.return_value = 0

        from PyQt6.QtWidgets import QDialog
        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
             mock.patch("ui.main_window.QDialog", QDialog), \
             mock.patch.object(window, "_load_devices"), \
             mock.patch.object(window.config_manager, "get_groups",
                               return_value=[{"name": "Default"}]), \
             mock.patch.object(window.config_manager, "update_device",
                               return_value=True):
            dialog.exec.return_value = QDialog.DialogCode.Accepted
            window._on_device_edit("Default", old)

    def test_a_changed_key_reaches_the_reconnect(self):
        """鍵を差し替えたら、再接続もその鍵を使うこと。"""
        window = self._window()
        old = self._old()
        window.device_info["Ubuntu0"] = old

        new = dict(old, ssh_key=r"C:\keys\netbelt_test2")
        self._edit(window, old, new)

        self.assertEqual(window.device_info["Ubuntu0"]["ssh_key"],
                         r"C:\keys\netbelt_test2",
                         "再接続が古い鍵を使い続ける")

    def test_a_changed_password_reaches_the_reconnect(self):
        """パスワードを変えたら、古いもので繋がらないこと。"""
        window = self._window()
        old = dict(self._old(), ssh_key="", password="old-secret")
        window.device_info["Ubuntu0"] = old

        new = dict(old, password="new-secret")
        self._edit(window, old, new)

        self.assertEqual(window.device_info["Ubuntu0"]["password"],
                         "new-secret",
                         "締め出したはずの古いパスワードで繋がり続ける")

    def test_renaming_does_not_leave_the_old_entry(self):
        """名前を変えたら、古い名前の写しを残さないこと。

        残すと、開いたままのタブがどこにも無い機器へ繋ぎにいく。
        """
        window = self._window()
        old = self._old()
        window.device_info["Ubuntu0"] = old

        new = dict(old, name="Ubuntu1")
        self._edit(window, old, new)

        self.assertNotIn("Ubuntu0", window.device_info,
                         "古い名前の接続情報が残っている")
        self.assertIn("Ubuntu1", window.device_info)

    def test_a_device_that_is_not_open_is_left_alone(self):
        """繋いでいない機器の編集で、他の写しを触らないこと。"""
        window = self._window()
        other = dict(self._old(), name="Cat8000v")
        window.device_info["Cat8000v"] = other

        old = self._old()
        self._edit(window, old, dict(old, ssh_key="changed"))

        self.assertEqual(window.device_info, {"Cat8000v": other})

    def test_deleting_a_device_forgets_how_to_reach_it(self):
        """削除した機器の接続情報を残さないこと。

        残っていると、開いたままのタブで Enter を押すだけで、
        消したはずの機器へ繋がる。
        """
        from PyQt6.QtWidgets import QMessageBox
        window = self._window()
        window.device_info["Ubuntu0"] = self._old()

        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
             mock.patch.object(window, "_load_devices"), \
             mock.patch.object(window.config_manager, "remove_device",
                               return_value=True):
            window._on_device_delete("Default", "Ubuntu0")

        self.assertNotIn("Ubuntu0", window.device_info,
                         "消した機器の接続情報が残っている")


if __name__ == "__main__":
    unittest.main()
