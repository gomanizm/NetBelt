"""機器ダイアログの確認文が、起動時に出ない警告を予告しないことを検証する。

何が起きていたか（実測、基準 9fee4af。本物の DPAPI、合成のパスワード）:
"DPAPI:cisco123" のような「暗号化済みの値と見分けがつかない形」のパスワードを
入れると、DeviceDialog は「このまま保存すると、設定ファイルに平文で書き出され、
起動のたびに『パスワードを復号できませんでした』と出ます」と確認していた。
v1.3.1（d5d4628）から、起動時の件数は本物の DPAPI 暗号文だけを数える
（is_dpapi_ciphertext）ので、その形の平文を保存して読み直しても警告は出ない
（load_warning=None、2 回読み直しても同じ）。接続も断られない。平文で
書き出されることだけが事実で、後半の予告は外れていた。

直し方: 確認文から起動時の警告の予告を消した（「平文」で保存されることは
残す。tests/test_device_dialog_dpapi_prefix_warning.py が確かめている）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 利用者が実際に設定しうる平文。"DPAPI:" のあとが base64 として妥当なので
# 暗号文と見分けがつかない
LOOKS_ENCRYPTED = "DPAPI:cisco123"


class DpapiPrefixQuestionMatchesLoadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _config(self):
        from core.config_manager import ConfigManager
        workdir = tempfile.mkdtemp(prefix="netbelt-dpapiq-")
        return ConfigManager(os.path.join(workdir, "config.json"))

    def test_such_a_password_reloads_without_a_warning(self):
        """前提: その形の平文を保存して読み直しても、起動時の警告は出ないこと。"""
        from core.config_manager import ConfigManager
        cm = self._config()
        self.assertTrue(cm.add_device("Default", {
            "name": "dev", "host": "192.0.2.10", "port": 22, "protocol": "ssh",
            "username": "admin", "password": LOOKS_ENCRYPTED, "ssh_key": "",
            "macros": []}))
        self.assertTrue(cm.save_config())

        for _ in range(2):
            reloaded = ConfigManager(str(cm.config_path))
            self.assertIsNone(reloaded.load_warning)
            self.assertFalse(reloaded.has_undecryptable_password("dev", LOOKS_ENCRYPTED))
            self.assertTrue(reloaded.save_config())

    def test_the_question_does_not_promise_a_startup_warning(self):
        """確認文が、起きない「復号できませんでした」を予告しないこと。"""
        from PyQt6.QtWidgets import QMessageBox
        from ui.dialogs.device_dialog import DeviceDialog

        dialog = DeviceDialog(groups=["Default"])
        self.addCleanup(dialog.deleteLater)
        dialog.protocol_combo.setCurrentText("ssh")
        dialog.name_edit.setText("dev")
        dialog.host_edit.setText("192.0.2.10")
        dialog.username_edit.setText("admin")
        dialog.password_edit.setText(LOOKS_ENCRYPTED)
        with mock.patch("ui.dialogs.device_dialog.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.No) as question, \
                mock.patch("ui.dialogs.device_dialog.QMessageBox.warning"), \
                mock.patch.object(DeviceDialog, "accept"):
            dialog._on_ok()

        self.assertTrue(question.called, "前提: 確認が出ていない")
        text = question.call_args[0][2]
        self.assertNotIn("復号できませんでした", text,
                         "起動時に出ない警告を予告している: %r" % text)
        self.assertIn("平文", text)


if __name__ == "__main__":
    unittest.main()
