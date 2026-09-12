"""機器ダイアログが「暗号化済みの値と同じ形の平文」を黙って通す不具合の回帰テスト。

core.crypto.is_encrypted は "DPAPI:" のあとが base64 として妥当かまでしか見ない。
そのため利用者が本当に "DPAPI:cisco123" のようなパスワードを設定すると、
実測で is_encrypted() が True を返し（下の test_the_premise_still_holds が確かめる）、
_encrypt_passwords が「もう暗号化されている」と誤認して config.json へ平文のまま
書き出す。起動のたびに「復号できませんでした」も出る。

保存側の判定はこれ以上詰められない（別環境で作られた復号不能な暗号文と
区別がつかない）ので、入力側で黙らせないことが最後の防波堤になる。

既存の契約は壊さないこと: 編集時に触っていない値（別環境の暗号文がそのまま
入っている場合を含む）は、これまでどおり無言で通ること。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 利用者が実際に設定しうる平文。"DPAPI:" のあとが base64 として妥当なので
# 暗号文と見分けがつかない
LOOKS_ENCRYPTED = "DPAPI:cisco123"
# 他環境で作られた（＝この環境では復号できない）DPAPI 形式の値
FOREIGN = "DPAPI:" + "b2xkLW1hY2hpbmUtY2lwaGVydGV4dC1oZXJl"


class DeviceDialogDpapiPrefixWarningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    _dialogs = []

    @classmethod
    def tearDownClass(cls):
        cls._dialogs.clear()

    def setUp(self):
        # offscreen ではモーダルを閉じられる相手がいない
        warning = mock.patch("ui.dialogs.device_dialog.QMessageBox.warning")
        self.warning = warning.start()
        self.addCleanup(warning.stop)
        question = mock.patch("ui.dialogs.device_dialog.QMessageBox.question")
        self.question = question.start()
        self.addCleanup(question.stop)
        self._answer("No")

    def _answer(self, name):
        """確認ダイアログの答えを決める。"""
        from PyQt6.QtWidgets import QMessageBox
        self.question.return_value = getattr(QMessageBox.StandardButton, name)

    def _dialog(self, password, device_data=None):
        from ui.dialogs.device_dialog import DeviceDialog
        dialog = DeviceDialog(groups=["Default"], device_data=device_data)
        type(self)._dialogs.append(dialog)
        dialog.protocol_combo.setCurrentText("ssh")
        dialog.name_edit.setText("dev")
        dialog.host_edit.setText("192.0.2.10")
        dialog.username_edit.setText("admin")
        dialog.password_edit.setText(password)
        return dialog

    def _accepted(self, dialog):
        """OK が通ったか。通れば accept() が呼ばれる。"""
        with mock.patch.object(type(dialog), "accept") as accept:
            dialog._on_ok()
            return accept.called

    def test_the_premise_still_holds(self):
        """保存側は今もこの平文を暗号文と見なすこと（入力側が要る理由）。"""
        from core.crypto import PasswordCrypto
        self.assertTrue(PasswordCrypto().is_encrypted(LOOKS_ENCRYPTED))

    def test_a_new_password_shaped_like_ciphertext_is_questioned(self):
        """平文で保存される入力を、黙って受け取らないこと。"""
        dialog = self._dialog(LOOKS_ENCRYPTED)

        accepted = self._accepted(dialog)

        self.assertTrue(self.question.called,
                        "平文で保存される入力を黙って受け取っている")
        self.assertFalse(accepted, "「いいえ」なのに保存している")

    def test_the_question_says_it_would_be_stored_in_cleartext(self):
        """何が起きるかを伝えること。"""
        dialog = self._dialog(LOOKS_ENCRYPTED)
        self._accepted(dialog)

        text = self.question.call_args[0][2]
        self.assertIn("平文", text, "起きることを伝えていない: %r" % text)

    def test_answering_yes_still_saves(self):
        """承知のうえなら、これまでどおり保存できること。"""
        self._answer("Yes")
        dialog = self._dialog(LOOKS_ENCRYPTED)

        self.assertTrue(self._accepted(dialog),
                        "「はい」でも保存させていない")

    def test_an_ordinary_password_is_not_questioned(self):
        """普通のパスワードで邪魔をしないこと。"""
        dialog = self._dialog("Cisco123!")

        self.assertTrue(self._accepted(dialog))
        self.assertFalse(self.question.called, "毎回確認を出している")

    def test_an_untouched_foreign_ciphertext_is_not_questioned(self):
        """触っていない別環境の暗号文は、これまでどおり無言で通ること。

        config.json を別 PC から持ってきた機器のポートだけ直す、といった
        操作で確認を出すと、実際には起きない「平文で保存」を警告することになる。
        """
        data = {"name": "dev", "host": "192.0.2.10", "port": 22,
                "protocol": "ssh", "username": "admin",
                "password": FOREIGN, "ssh_key": "", "macros": []}
        dialog = self._dialog(FOREIGN, device_data=data)

        self.assertTrue(self._accepted(dialog))
        self.assertFalse(self.question.called,
                         "触っていない暗号文にまで確認を出している")


if __name__ == "__main__":
    unittest.main()
