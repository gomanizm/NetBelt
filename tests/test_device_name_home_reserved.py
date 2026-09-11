"""機器名「ホーム」を登録させないことを検証する。

計測で確認した経路:

  機器名を「ホーム」にして登録し接続すると、接続自体は成立するが、
  TerminalWidget はホームタブをタブ名 "ホーム" で見分けているため、
  そのタブは閉じられず（tab_closed も飛ばず connections に残る）、
  ログ保存・ログ記録・マクロ設定が「ホームタブ」扱いで断られる。

GroupDialog が「コンソール接続」を予約語として断っているのと同じく、
登録の入口（ダイアログ）と config への追加・更新の両方で断る。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


def device(name):
    return {"name": name, "host": "192.0.2.10", "port": 22, "protocol": "ssh",
            "username": "admin", "password": "", "ssh_key": "", "macros": []}


class DeviceDialogRefusesHomeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        patcher = mock.patch("ui.dialogs.device_dialog.QMessageBox.warning")
        self.warning = patcher.start()
        self.addCleanup(patcher.stop)

    def _dialog(self, name):
        from ui.dialogs.device_dialog import DeviceDialog
        dialog = DeviceDialog(groups=["Default"])
        dialog.name_edit.setText(name)
        dialog.host_edit.setText("192.0.2.10")
        dialog.username_edit.setText("admin")
        return dialog

    def _accepted(self, dialog):
        with mock.patch.object(type(dialog), "accept") as accept:
            dialog._on_ok()
            return accept.called

    def test_home_is_refused(self):
        self.assertFalse(self._accepted(self._dialog("ホーム")),
                         "ホームタブと衝突する名前で登録できてしまう")
        self.assertTrue(self.warning.called, "断った理由を伝えていない")
        self.assertIn("ホーム", self.warning.call_args[0][2])

    def test_home_with_surrounding_spaces_is_refused(self):
        """名前は strip して保存されるので、空白で囲んでもすり抜けないこと。"""
        self.assertFalse(self._accepted(self._dialog("  ホーム ")))

    def test_an_ordinary_name_is_still_accepted(self):
        self.assertTrue(self._accepted(self._dialog("ホームルータ")),
                        "予約語でない名前まで断っている")


class ConfigRefusesHomeTest(unittest.TestCase):
    def _manager(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-home-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        if not cm.get_group("Default"):
            cm.add_group("Default")
        return cm

    def test_add_device_refuses_home(self):
        cm = self._manager()
        self.assertFalse(cm.add_device("Default", device("ホーム")))
        self.assertIsNone(cm.find_device_group("ホーム"),
                          "断ったのにメモリに残っている")

    def test_update_device_refuses_renaming_to_home(self):
        cm = self._manager()
        self.assertTrue(cm.add_device("Default", device("SW1")))
        self.assertFalse(cm.update_device("Default", "SW1", "Default",
                                          device("ホーム")))
        self.assertEqual(cm.find_device_group("SW1"), "Default",
                         "断ったのに元の機器が消えている")

    def test_add_device_still_accepts_an_ordinary_name(self):
        cm = self._manager()
        self.assertTrue(cm.add_device("Default", device("SW1")))


if __name__ == "__main__":
    unittest.main()
