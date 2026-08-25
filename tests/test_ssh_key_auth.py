"""SSH 公開鍵認証の鍵読み込みを検証する。

v1.1.0 では、鍵タイプの候補に paramiko.DSSKey を並べていた。paramiko 4.0.0
で DSA は削除されており、リストを組む時点で AttributeError になるため、
**正常な鍵でも必ず「秘密鍵の読み込みエラー」で接続できなかった**（実測）。
パスワード認証は別分岐なので動いており、気づかれないまま公開された。

SSHConnection を通るテストが1件も無かったことが、そのまま原因である。
接続そのものは相手が要るので試せないが、鍵の読み込みまでは検証できる。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

HOST = "192.0.2.10"   # RFC 5737。到達しないので接続は必ず失敗する


class SshKeyLoadingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.dir = tempfile.mkdtemp()

    def _write_key(self, name, generator, **kwargs):
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            generator().write_private_key_file(path, **kwargs)
        return path

    def _attempt(self, key_path):
        """鍵を指定して接続を試み、(戻り値, 最初のエラーメッセージ) を返す。"""
        from core.ssh_connection import SSHConnection
        conn = SSHConnection(host=HOST, port=22, username="admin",
                             ssh_key=key_path)
        errors = []
        conn.error_occurred.connect(errors.append)
        ok = conn.connect()
        return ok, (errors[0] if errors else "")

    def test_supported_key_types_load(self):
        """対応を謳う鍵タイプが実際に読み込めること。

        読み込めれば接続へ進み、到達しないアドレスなので通信エラーになる。
        「秘密鍵の読み込みエラー」で止まるなら、鍵を読めていない。
        """
        import paramiko
        cases = (
            ("rsa", lambda: paramiko.RSAKey.generate(2048)),
            ("ed25519", lambda: paramiko.Ed25519Key.generate()),
            ("ecdsa", lambda: paramiko.ECDSAKey.generate()),
        )
        for name, gen in cases:
            with self.subTest(key_type=name):
                try:
                    path = self._write_key(name, gen)
                except Exception as e:      # 生成できない型は検証対象外
                    self.skipTest("%s を生成できない: %s" % (name, e))
                _ok, message = self._attempt(path)
                self.assertNotIn("秘密鍵の読み込みエラー", message,
                                 "%s を読み込めていない: %s" % (name, message))

    def test_a_missing_paramiko_attribute_cannot_break_key_loading(self):
        """候補の鍵タイプがすべて実在すること。

        存在しない属性を1つ混ぜるだけで、リストを組む時点で AttributeError に
        なり、全部の鍵タイプが巻き添えで読めなくなる。DSSKey がまさにそれだった。
        """
        import inspect
        import paramiko
        from core import ssh_connection

        source = inspect.getsource(ssh_connection.SSHConnection.connect)
        referenced = set()
        for name in ("RSAKey", "Ed25519Key", "ECDSAKey", "DSSKey"):
            if "paramiko." + name in source:
                referenced.add(name)
        self.assertTrue(referenced, "鍵タイプを1つも参照していない")
        for name in sorted(referenced):
            with self.subTest(key_class=name):
                self.assertTrue(
                    hasattr(paramiko, name),
                    "paramiko に %s が無いのに参照している" % name)

    def test_a_passphrase_protected_key_says_so(self):
        """パスフレーズ付きの鍵は、その旨を伝えること。

        「対応する鍵タイプが見つかりません」とだけ出ると、利用者は鍵の種類を
        疑って原因に辿り着けない。
        """
        import paramiko
        path = self._write_key("rsa_enc", lambda: paramiko.RSAKey.generate(2048),
                               password="passphrase123")
        _ok, message = self._attempt(path)
        self.assertIn("パスフレーズ", message,
                      "パスフレーズが原因だと伝えていない: %s" % message)

    def test_an_unreadable_key_reports_why(self):
        """読めない鍵は、集めた理由を伝えること。"""
        path = os.path.join(self.dir, "garbage")
        with open(path, "w", encoding="ascii") as f:
            f.write("this is not a private key\n")
        _ok, message = self._attempt(path)
        self.assertIn("秘密鍵の読み込みエラー", message)
        self.assertIn("RSAKey", message,
                      "どの鍵タイプで何が起きたのかを捨てている: %s" % message)

    def test_an_explicit_key_is_not_mixed_with_local_keys(self):
        """鍵を指定したら、その鍵だけを使うこと。

        look_for_keys を True にすると、指定した鍵が拒否されたときに
        ~/.ssh の別の鍵で認証が通り、利用者が意図しない身元で接続してしまう。
        """
        from unittest import mock
        import paramiko
        from core.ssh_connection import SSHConnection

        path = self._write_key("rsa", lambda: paramiko.RSAKey.generate(2048))
        conn = SSHConnection(host=HOST, port=22, username="admin", ssh_key=path)
        with mock.patch.object(paramiko.SSHClient, "connect") as connect:
            conn.connect()
        self.assertTrue(connect.called, "connect が呼ばれていない")
        kwargs = connect.call_args.kwargs
        self.assertIn("pkey", kwargs, "指定した鍵を渡していない")
        self.assertFalse(kwargs.get("look_for_keys", False),
                         "鍵を指定したのにローカルの鍵も探している")
        self.assertFalse(kwargs.get("allow_agent", False),
                         "鍵を指定したのにエージェントも使っている")


if __name__ == "__main__":
    unittest.main()
