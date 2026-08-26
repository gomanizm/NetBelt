"""機器の登録内容が、そもそも繋がる形かを検証する。

実機で踏んだ経路:

  ホスト 192.0.2.10 / 秘密鍵あり / ユーザー名は空 で保存できてしまい、
  接続すると「認証失敗: ユーザー名またはパスワードが間違っています」。

paramiko はユーザー名を必ず要求するので、空のまま保存できた機器は
二度と繋がらない。しかも失敗の理由が、指定してもいないパスワードの
せいにされる。設定の段階で止めるほうが早い。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class DeviceDialogValidationTest(unittest.TestCase):
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
        # offscreen では警告ダイアログを閉じられる相手がいない
        patcher = mock.patch("ui.dialogs.device_dialog.QMessageBox.warning")
        self.warning = patcher.start()
        self.addCleanup(patcher.stop)

    def _dialog(self, protocol, name="dev", host="192.0.2.10", username=""):
        from ui.dialogs.device_dialog import DeviceDialog
        dialog = DeviceDialog(groups=["Default"])
        type(self)._dialogs.append(dialog)
        dialog.protocol_combo.setCurrentText(protocol)
        dialog.name_edit.setText(name)
        dialog.host_edit.setText(host)
        dialog.username_edit.setText(username)
        return dialog

    def _accepted(self, dialog):
        """OK が通ったか。通れば accept() が呼ばれる。"""
        with mock.patch.object(type(dialog), "accept") as accept:
            dialog._on_ok()
            return accept.called

    def test_ssh_without_a_username_is_refused(self):
        """ユーザー名の無い SSH 機器を保存させないこと。"""
        dialog = self._dialog("ssh", username="")

        self.assertFalse(self._accepted(dialog),
                         "繋がらない機器を保存できてしまう")
        self.assertTrue(self.warning.called, "理由を伝えていない")
        self.assertIn("ユーザー名", self.warning.call_args[0][2])

    def test_ssh_with_a_username_is_accepted(self):
        dialog = self._dialog("ssh", username="admin")

        self.assertTrue(self._accepted(dialog),
                        "正しい入力なのに保存できない: %r"
                        % (self.warning.call_args,))

    def test_whitespace_is_not_a_username(self):
        """空白だけを名前として通さないこと。"""
        dialog = self._dialog("ssh", username="   ")

        self.assertFalse(self._accepted(dialog))

    def test_telnet_without_a_username_is_still_allowed(self):
        """telnet は利用者名を送らない機器が多い。塞いではいけない。"""
        dialog = self._dialog("telnet", username="")

        self.assertTrue(self._accepted(dialog),
                        "telnet までユーザー名必須にしている")

    def test_console_without_a_username_is_still_allowed(self):
        """シリアル接続にユーザー名は関係ない。"""
        dialog = self._dialog("console", host="COM1", username="")

        self.assertTrue(self._accepted(dialog),
                        "console までユーザー名必須にしている")


class AuthFailureMessageTest(unittest.TestCase):
    """認証に失敗したとき、疑うべきものを名指しすることを検証する。

    「ユーザー名またはパスワードが間違っています」で片付けると、
    鍵で認証している利用者は、渡してもいないパスワードを疑うことになる。
    """

    def _connection(self, username="", ssh_key="", password=""):
        from core.ssh_connection import SSHConnection
        return SSHConnection(host="192.0.2.10", port=22, username=username,
                             password=password, ssh_key=ssh_key)

    def test_an_empty_username_is_named(self):
        message = self._connection(username="")._auth_failure_message()

        self.assertIn("ユーザー名が設定されていません", message)
        self.assertNotIn("パスワードが間違って", message,
                         "設定漏れをパスワードのせいにしている")

    def test_a_key_failure_does_not_blame_the_password(self):
        message = self._connection(
            username="cisco", ssh_key=r"C:\keys\id_ed25519")._auth_failure_message()

        self.assertNotIn("パスワード", message,
                         "鍵で認証しているのにパスワードを疑わせている")
        self.assertIn("authorized_keys", message, "確かめる場所を示していない")
        self.assertIn("cisco", message, "どのユーザーで試したか出ていない")

    def test_a_password_failure_still_says_so(self):
        message = self._connection(
            username="cisco", password="secret")._auth_failure_message()

        self.assertIn("パスワード", message)

    def test_the_connect_path_actually_uses_it(self):
        """接続の失敗経路が、この文言を通ること。

        組み立てる関数だけを見ていると、例外処理が昔のべた書きに
        戻っても気づけない。実際に一度その経路を通す。
        """
        import tempfile
        import paramiko

        # この paramiko には Ed25519Key.generate() が無い。鍵の種類は
        # ここでの論点ではないので、確実に作れる RSA を使う。
        key_path = os.path.join(tempfile.mkdtemp(), "id_rsa")
        paramiko.RSAKey.generate(2048).write_private_key_file(key_path)

        conn = self._connection(username="cisco", ssh_key=key_path)
        seen = []
        conn.error_occurred.connect(seen.append)

        with mock.patch("core.ssh_connection.paramiko.SSHClient") as client:
            client.return_value.connect.side_effect = \
                paramiko.AuthenticationException()
            ok = conn.connect()

        self.assertFalse(ok)
        self.assertEqual(len(seen), 1, "エラーの数が想定と違う: %r" % (seen,))
        self.assertIn("authorized_keys", seen[0],
                      "接続経路が昔の文言のまま: %r" % seen[0])


if __name__ == "__main__":
    unittest.main()
