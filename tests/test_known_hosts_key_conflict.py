"""初回接続の鍵を保存するとき、同じ接続先の別の鍵を上書きしないことを検証する。

実測（81664d2 / paramiko 4.0.0）: known_hosts が無い状態で _setup_host_keys を
通した本物の SSHClient を 2 つ用意し、同じ 192.0.2.5 に対して異なる ECDSA 鍵で
policy_a.missing_host_key → policy_b.missing_host_key を実行すると、
A 保存直後はファイル 1 行（A の鍵）、B 保存後はファイル 3 行になり 3 行とも
B の鍵で、A の鍵は消えた。HostKeys.check(host, A鍵)=False / (B鍵)=True。
画面への警告は A/B とも 0 件で、以後 A の機器へ繋ぐと BadHostKeyException で
拒否される。原因は 2 つ。(1) missing_host_key が受信鍵を add() してから
load_host_keys() するので、HostKeys.load の check() が「自分の鍵」と比べて
不一致になり、ディスク側を別エントリとして足すだけで競合を誰にも伝えない。
(2) paramiko の SSHClient.save_host_keys は冒頭でもう一度 load_host_keys を
呼ぶので同じ行が二重に足され、書き出しは SubDict.__getitem__ が先頭一致を
返すためエントリ数ぶん同じ鍵が並ぶ。

利用者の決定（2026-09-20）: 食い違いなら中止。初回接続の鍵を保存するとき、
同じ接続先の別の鍵がディスクにあると分かったら、上書きせず接続を中止して
「鍵が食い違います」と伝える（どちらの鍵かも示す）。

直し方: 保存の錠の中でディスクの known_hosts を読み直し、その接続先に別の鍵が
あれば HostKeyMismatchError を投げて保存も接続も行わない。例外には
known_hosts 側の鍵と今回提示された鍵の指紋（SHA256、OpenSSH と同じ形）を
両方載せる。SSHConnection.connect はこの例外を専用の節で受けて、
そのままの文言をエラーとして出す。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


class KnownHostsKeyConflictTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        import paramiko
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()
        cls.key_rsa = paramiko.RSAKey.generate(2048)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khconf-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "known_hosts"

    def _client(self, host="192.0.2.5"):
        """_setup_host_keys を通した本物の SSHClient を返す（起動直後と同じ）。"""
        import paramiko
        from core.ssh_connection import SSHConnection
        conn = SSHConnection(host=host, port=22, username="admin")
        self.addCleanup(conn.deleteLater)
        messages = []
        conn.output_received.connect(messages.append)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        conn._setup_host_keys(client)
        return client, client._policy, messages

    def _text(self):
        return self.path.read_text(encoding="utf-8")

    def test_the_stored_key_is_not_silently_replaced(self):
        """先に保存された鍵が、黙って別の鍵に差し替わらないこと。

        中止するかどうかに関わらず、ディスクの結果だけを見る。
        """
        client_a, policy_a, _ = self._client()
        client_b, policy_b, _ = self._client()
        policy_a.missing_host_key(client_a, "192.0.2.5", self.key_a)

        try:
            policy_b.missing_host_key(client_b, "192.0.2.5", self.key_b)
        except Exception:
            pass    # 中止されるのが正しい。ここでは保存の結果だけを見る

        self.assertIn(self.key_a.get_base64(), self._text(),
                      "先に保存されていた鍵が消えている")

    def test_a_conflicting_key_aborts_instead_of_overwriting(self):
        """同じ接続先に別の鍵が保存済みなら、上書きせず中止すること。"""
        from core.ssh_connection import HostKeyMismatchError
        client_a, policy_a, _ = self._client()
        client_b, policy_b, warnings_b = self._client()

        policy_a.missing_host_key(client_a, "192.0.2.5", self.key_a)
        self.assertIn(self.key_a.get_base64(), self._text(),
                      "前提: A の鍵が保存された")

        with self.assertRaises(HostKeyMismatchError) as caught:
            policy_b.missing_host_key(client_b, "192.0.2.5", self.key_b)

        self.assertIn(self.key_a.get_base64(), self._text(),
                      "先に保存されていた鍵が上書きされている")
        self.assertNotIn(self.key_b.get_base64(), self._text(),
                         "食い違う鍵が保存されている")
        self.assertEqual(warnings_b, [],
                         "中止ではなく警告で済ませている: %r" % (warnings_b,))
        self.assertIn("食い違", str(caught.exception),
                      "鍵が食い違うことを伝えていない")

    def test_the_message_shows_both_keys(self):
        """どちらの鍵かが分かるよう、両方の指紋を示すこと。"""
        import base64
        import hashlib
        from core.ssh_connection import HostKeyMismatchError

        def fingerprint(key):
            return "SHA256:" + base64.b64encode(
                hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")

        client_a, policy_a, _ = self._client()
        client_b, policy_b, _ = self._client()
        policy_a.missing_host_key(client_a, "192.0.2.5", self.key_a)

        with self.assertRaises(HostKeyMismatchError) as caught:
            policy_b.missing_host_key(client_b, "192.0.2.5", self.key_b)

        message = str(caught.exception)
        self.assertIn(fingerprint(self.key_a), message,
                      "known_hosts 側の鍵が分からない")
        self.assertIn(fingerprint(self.key_b), message,
                      "今回提示された鍵が分からない")
        self.assertIn("192.0.2.5", message, "どの接続先かが分からない")

    def test_a_key_of_another_type_for_the_same_host_also_aborts(self):
        """鍵の種別が違っても、同じ接続先の別の鍵なら中止すること。"""
        from core.ssh_connection import HostKeyMismatchError
        client_a, policy_a, _ = self._client()
        client_b, policy_b, _ = self._client()
        policy_a.missing_host_key(client_a, "192.0.2.5", self.key_a)

        with self.assertRaises(HostKeyMismatchError):
            policy_b.missing_host_key(client_b, "192.0.2.5", self.key_rsa)

        self.assertNotIn(self.key_rsa.get_base64(), self._text())

    def test_the_same_key_is_still_accepted(self):
        """同じ鍵なら食い違いではないので、今までどおり通すこと。"""
        client_a, policy_a, _ = self._client()
        client_b, policy_b, _ = self._client()

        policy_a.missing_host_key(client_a, "192.0.2.5", self.key_a)
        policy_b.missing_host_key(client_b, "192.0.2.5", self.key_a)

        lines = [ln for ln in self._text().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, "同じ鍵で行が増えている: %r" % (lines,))
        self.assertIn(self.key_a.get_base64(), lines[0])

    def test_another_host_is_still_saved(self):
        """別の接続先の初回接続は、今までどおり保存すること。"""
        client_a, policy_a, _ = self._client()
        client_b, policy_b, _ = self._client("192.0.2.6")

        policy_a.missing_host_key(client_a, "192.0.2.5", self.key_a)
        policy_b.missing_host_key(client_b, "192.0.2.6", self.key_b)

        text = self._text()
        self.assertIn(self.key_a.get_base64(), text)
        self.assertIn(self.key_b.get_base64(), text)

    def test_the_first_connection_is_still_saved(self):
        """known_hosts が空のときは、今までどおり初回の鍵を保存すること。"""
        client_a, policy_a, _ = self._client()

        policy_a.missing_host_key(client_a, "192.0.2.5", self.key_a)

        self.assertIn(self.key_a.get_base64(), self._text())

    def test_the_connection_reports_the_conflict(self):
        """接続処理は、この中止をそのままの文言でエラーとして出すこと。"""
        from core.ssh_connection import HostKeyMismatchError, SSHConnection
        conn = SSHConnection("192.0.2.5", 22, "admin", password="pw")
        self.addCleanup(conn.deleteLater)
        errors = []
        conn.error_occurred.connect(errors.append)
        client = mock.Mock()
        client.connect.side_effect = HostKeyMismatchError(
            "ホスト鍵が食い違います（テスト）")

        with mock.patch("core.ssh_connection.paramiko.SSHClient",
                        return_value=client), \
             mock.patch.object(SSHConnection, "_read_output"):
            ok = conn.connect()

        self.assertFalse(ok, "食い違ったまま接続している")
        self.assertTrue(any("食い違" in e for e in errors),
                        "食い違いを伝えていない: %r" % (errors,))


if __name__ == "__main__":
    unittest.main()
