"""console のポート欄に文字が入っていても、保存で落ちないことを検証する。

計測で確認した経路:

  プロトコルに console を選ぶとポート欄は空になるが、入力はできる。
  そこに "abc" と入れて OK → _on_ok は console をポート検査の対象外に
  しているので通り、そのあと _on_add_device が呼ぶ get_device_data() の
  int("abc") が ValueError。シグナルのスロット内の未捕捉例外なので、
  main.py の excepthook が無ければプロセスごと落ち、あっても
  「予期しないエラー」ダイアログが出て入力した機器は保存されない。

console ではポート番号を使わない。欄を無効にして触れなくし、
万一値が入っていても get_device_data() が例外を投げないようにする。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class DeviceDialogConsolePortTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        patcher = mock.patch("ui.dialogs.device_dialog.QMessageBox.warning")
        self.warning = patcher.start()
        self.addCleanup(patcher.stop)

    def _console_dialog(self):
        from ui.dialogs.device_dialog import DeviceDialog
        dialog = DeviceDialog(groups=["Default"])
        dialog.protocol_combo.setCurrentText("console")
        dialog.name_edit.setText("ConsoleSW")
        dialog.host_edit.setText("COM3")
        return dialog

    def test_the_port_field_is_disabled_for_console(self):
        """console ではポート欄に触れないこと。"""
        dialog = self._console_dialog()
        self.assertFalse(dialog.port_edit.isEnabled(),
                         "console なのにポート欄へ入力できる")

    def test_the_port_field_comes_back_for_ssh(self):
        """ssh へ戻したらポート欄がまた使えること。"""
        dialog = self._console_dialog()
        dialog.protocol_combo.setCurrentText("ssh")
        self.assertTrue(dialog.port_edit.isEnabled())

    def test_loading_a_console_device_disables_the_port_field(self):
        """保存済みの console 機器を開いたときも欄が無効であること。"""
        from ui.dialogs.device_dialog import DeviceDialog
        dialog = DeviceDialog(groups=["Default"], device_data={
            "name": "ConsoleSW", "host": "COM3", "port": 0,
            "protocol": "console", "username": "", "password": "",
            "ssh_key": ""})
        self.assertFalse(dialog.port_edit.isEnabled())

    def test_a_non_numeric_port_does_not_raise_on_save(self):
        """欄に文字が残っていても get_device_data() が例外を投げないこと。"""
        dialog = self._console_dialog()
        dialog.port_edit.setText("abc")   # 無効化していても値は入れられる

        with mock.patch.object(type(dialog), "accept") as accept:
            dialog._on_ok()
        self.assertTrue(accept.called, "console の OK が通らない")

        try:
            data = dialog.get_device_data()
        except ValueError as e:
            self.fail("保存時に落ちる: %s" % e)
        self.assertEqual(data["port"], 0)
        self.assertEqual(data["protocol"], "console")

    def test_a_saved_console_port_is_still_read_back(self):
        """数字が入っている既存の console 機器の値は、これまでどおり残すこと。"""
        from ui.dialogs.device_dialog import DeviceDialog
        dialog = DeviceDialog(groups=["Default"], device_data={
            "name": "ConsoleSW", "host": "COM3", "port": 1234,
            "protocol": "console", "username": "", "password": "",
            "ssh_key": ""})
        self.assertEqual(dialog.get_device_data()["port"], 1234)


if __name__ == "__main__":
    unittest.main()
