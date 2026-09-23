"""機器の任意文字列項目の型が違っても、編集ダイアログが開けることを検証する。

何が起きていたか（基準 f4cad23 で実測）。_is_valid_device が見るのは
name / host だけで、読み込み時にそろえ直すのは password と macros /
auto_commands に限られる。そのため手編集された config.json の
username / ssh_key / protocol は型を崩したまま素通りし、その機器の
「編集」を開くと QLineEdit.setText / QComboBox.findText が TypeError に
なっていた:

  username: 123   -> 読み込み通過 / TypeError: setText(...) 'int'
  ssh_key: []     -> 読み込み通過 / TypeError: setText(...) 'list'
  protocol: 7     -> 読み込み通過 / TypeError: findText(...) 'int'

呼び出し側（MainWindow の機器編集・機器の複製）は DeviceDialog の生成を
try で囲んでいないので、例外は main.py の excepthook へ抜ける。アプリは
落ちないが「予期しないエラーが発生しました」のダイアログが出るだけで、
その機器は GUI からもう直せない（パスワードの入れ直しもできない）。

どう直したか。読み込み時のそろえ直し（_quarantine_invalid_devices）で、
username / ssh_key / protocol の型も password と同じ流儀でそろえる。
数値は文字列にして知らせ、それ以外の型は既定値（protocol は "ssh"、
他は空文字）へ戻して知らせる。null は「無し」と同じ意味なので黙って
そろえる。あわせて DeviceDialog 側でも値を str() に通し、ConfigManager を
経由しない device_data でも編集ダイアログだけは必ず開くようにした。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "src")

from core.config_manager import ConfigManager       # noqa: E402


class _ConfigWithDevice(unittest.TestCase):
    """機器 1 件だけの config.json を読ませる土台。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _load(self, **overrides):
        """項目を差し替えた機器を読み込み、(ConfigManager, 機器) を返す。"""
        directory = Path(tempfile.mkdtemp(prefix="netbelt-devtype-"))
        path = directory / "config.json"
        device = {"name": "sw1", "host": "192.0.2.10",
                  "protocol": "ssh", "port": 22,
                  "username": "admin", "password": "", "ssh_key": ""}
        device.update(overrides)
        data = {"config_version": "1.0",
                "groups": [{"name": "Default", "devices": [device]}],
                "global_macros": [], "settings": {}}
        path.write_text(json.dumps(data, ensure_ascii=False),
                        encoding="utf-8")
        cm = ConfigManager(config_path=str(path))
        devices = [d for g in cm.config.get("groups", [])
                   for d in g.get("devices", [])]
        self.assertEqual([d.get("name") for d in devices], ["sw1"],
                         "機器が読み込まれていない")
        return cm, devices[0]

    def _open_dialog(self, device):
        """その機器の編集ダイアログを開く（例外はそのまま上げる）。"""
        from ui.dialogs.device_dialog import DeviceDialog
        dialog = DeviceDialog(groups=["Default"], device_data=device)
        self.addCleanup(dialog.deleteLater)
        return dialog


class DeviceTextTypesLoadTest(_ConfigWithDevice):
    """読み込み時に型をそろえること。"""

    def test_a_numeric_username_becomes_a_string(self):
        """数値のユーザー名は、そのまま文字列として扱うこと。"""
        cm, device = self._load(username=123)

        self.assertEqual(device["username"], "123",
                         "数値のユーザー名が文字列になっていない")
        self.assertIn("sw1", cm.load_warning or "",
                      "直したことを知らせていない: %r" % (cm.load_warning,))

    def test_a_list_ssh_key_is_emptied(self):
        """文字列でない秘密鍵の指定は、空に戻して知らせること。"""
        cm, device = self._load(ssh_key=[])

        self.assertEqual(device["ssh_key"], "",
                         "文字列でない秘密鍵がそのまま残っている")
        self.assertIn("sw1", cm.load_warning or "",
                      "直したことを知らせていない: %r" % (cm.load_warning,))

    def test_a_dict_protocol_falls_back_to_ssh(self):
        """文字列でないプロトコルは、ssh へ戻して知らせること。"""
        cm, device = self._load(protocol={"a": 1})

        self.assertEqual(device["protocol"], "ssh",
                         "文字列でないプロトコルがそのまま残っている")
        self.assertIn("sw1", cm.load_warning or "",
                      "直したことを知らせていない: %r" % (cm.load_warning,))

    def test_a_numeric_protocol_becomes_a_string(self):
        """数値のプロトコルも、パスワードと同じく文字列にすること。"""
        _, device = self._load(protocol=7)

        self.assertEqual(device["protocol"], "7",
                         "数値のプロトコルが文字列になっていない")

    def test_a_null_username_is_quietly_emptied(self):
        """null は「無し」と同じなので、黙って空文字にそろえること。"""
        cm, device = self._load(username=None)

        self.assertEqual(device["username"], "",
                         "null のユーザー名がそのまま残っている")
        self.assertIsNone(cm.load_warning,
                          "null を直したことで警告を出している: %r"
                          % (cm.load_warning,))

    def test_a_normal_device_is_left_alone(self):
        """型の正しい機器は、何も書き換えず警告も出さないこと。"""
        cm, device = self._load()

        self.assertEqual(device["username"], "admin")
        self.assertEqual(device["protocol"], "ssh")
        self.assertIsNone(cm.load_warning,
                          "正常な設定で警告が出ている: %r" % (cm.load_warning,))


class DeviceTextTypesDialogTest(_ConfigWithDevice):
    """読み込みを通した機器の編集ダイアログが開くこと。"""

    def test_the_edit_dialog_opens_for_a_numeric_username(self):
        """数値のユーザー名を持つ機器でも、編集ダイアログが開くこと。"""
        _, device = self._load(username=123)

        dialog = self._open_dialog(device)

        self.assertEqual(dialog.username_edit.text(), "123")

    def test_the_edit_dialog_opens_for_a_list_ssh_key(self):
        """文字列でない秘密鍵を持つ機器でも、編集ダイアログが開くこと。"""
        _, device = self._load(ssh_key=[])

        dialog = self._open_dialog(device)

        self.assertEqual(dialog.ssh_key_edit.text(), "")

    def test_the_edit_dialog_opens_for_a_numeric_protocol(self):
        """数値のプロトコルを持つ機器でも、編集ダイアログが開くこと。"""
        _, device = self._load(protocol=7)

        dialog = self._open_dialog(device)

        self.assertEqual(dialog.protocol_combo.currentText(), "ssh",
                         "読めないプロトコルが ssh に落ちていない")


class DeviceDialogReadsBrokenTypesTest(_ConfigWithDevice):
    """ConfigManager を通さない device_data でもダイアログが開くこと。

    複製や、そろえ直しより前のバージョンで作られた辞書がそのまま渡される
    経路があるので、読み手側でも守る。
    """

    def _device(self, **overrides):
        device = {"name": "sw1", "host": "192.0.2.10",
                  "protocol": "ssh", "port": 22,
                  "username": "admin", "password": "", "ssh_key": ""}
        device.update(overrides)
        return device

    def test_a_numeric_username_still_opens(self):
        """数値のユーザー名でも TypeError にならないこと。"""
        dialog = self._open_dialog(self._device(username=123))

        self.assertEqual(dialog.username_edit.text(), "123")

    def test_a_list_ssh_key_still_opens(self):
        """list の秘密鍵でも TypeError にならないこと。"""
        dialog = self._open_dialog(self._device(ssh_key=[]))

        self.assertEqual(dialog.ssh_key_edit.text(), "[]")

    def test_a_numeric_protocol_still_opens(self):
        """数値のプロトコルでも TypeError にならないこと。"""
        dialog = self._open_dialog(self._device(protocol=7))

        self.assertEqual(dialog.protocol_combo.currentText(), "ssh")

    def test_a_numeric_password_still_opens(self):
        """数値のパスワードでも TypeError にならないこと。"""
        dialog = self._open_dialog(self._device(password=999))

        self.assertEqual(dialog.password_edit.text(), "999")


if __name__ == "__main__":
    unittest.main()
