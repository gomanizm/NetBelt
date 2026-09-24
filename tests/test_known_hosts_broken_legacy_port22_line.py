"""旧版が "[host]:22" の名前で残した行が壊れていたら、22 番の接続を中止することを検証する。

何が起きていたか（基準 38e7b29 で実測。検査役 3 人が同じ再現）。cf9e727 で、
旧版が "[host]:22" の名前で保存した鍵を 22 番の照合に使うようにした。
ところが、その行の鍵欄が壊れている（paramiko が読めない）ときの扱いは
"host" の名前だけで照合していたので、この行を「この機器の行」と見なさず、
「名前欄が完全には一致しないので接続は続けます」という警告だけで進んだ。
TOFU が別の鍵を受け入れ、パスワードが相手へ届く。

    known_hosts: "[127.0.0.1]:22 ecdsa-sha2-nistp256 AAAA"（鍵欄が壊れている）だけ
    22 番へ接続 → 警告のあと認証へ進む
    （同じ壊れ方の "127.0.0.1 …" の行なら、読めない行として接続を中止する）

旧名の行を照合に使うようになった以上、壊れていたときも同じ重さで扱う
必要がある。

どう直したか。_refuse_or_warn_broken_lines で、22 番のときは "host" に
加えて "[host]:22" に一致する読めない行も、この機器の行として中止の
対象にする。22 番以外のポートの扱いは変えない。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402
from paramiko.hostkeys import HostKeys              # noqa: E402

HOST = "127.0.0.1"
LEGACY = "[127.0.0.1]:22"


class BrokenLegacyPort22LineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khlegacybroken-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "known_hosts"

    def _write(self, line):
        self.path.write_text(line + "\n", encoding="utf-8")

    def _setup(self, port):
        """本物の SSHClient に _setup_host_keys を通し、画面に出た文を返す。"""
        from core.ssh_connection import SSHConnection
        conn = SSHConnection(host=HOST, port=port, username="admin")
        self.addCleanup(conn.deleteLater)
        messages = []
        conn.output_received.connect(messages.append)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        conn._setup_host_keys(client)
        return messages

    def test_broken_legacy_line_refuses_port_22(self):
        """壊れた "[host]:22" の行は、22 番の接続を中止すること。"""
        from core.ssh_connection import HostKeyStoreError
        line = LEGACY + " ecdsa-sha2-nistp256 AAAA"
        for port in (22, "22"):
            with self.subTest(port=port):
                self._write(line)
                with self.assertRaises(HostKeyStoreError) as caught:
                    self._setup(port)
                self.assertIn(line, str(caught.exception),
                              "中止の理由にどの行かが出ていない")

    def test_broken_hashed_legacy_line_refuses_port_22(self):
        """名前をハッシュ化した "[host]:22" の壊れた行でも、中止すること。"""
        from core.ssh_connection import HostKeyStoreError
        self._write(HostKeys.hash_host(LEGACY) + " ecdsa-sha2-nistp256 AAAA")
        with self.assertRaises(HostKeyStoreError):
            self._setup(22)

    def test_broken_legacy_line_only_warns_for_another_port(self):
        """22 番以外のポートでは、"[host]:22" の壊れた行は警告だけにすること（対照）。"""
        self._write(LEGACY + " ecdsa-sha2-nistp256 AAAA")
        messages = self._setup(2222)
        self.assertTrue(any("読めない行" in m for m in messages),
                        "読めない行を知らせていない: %r" % (messages,))

    def test_broken_plain_host_line_still_refuses_port_22(self):
        """壊れた "host" の行は、これまでどおり 22 番の接続を中止すること（対照）。"""
        from core.ssh_connection import HostKeyStoreError
        self._write(HOST + " ecdsa-sha2-nistp256 AAAA")
        with self.assertRaises(HostKeyStoreError):
            self._setup(22)


if __name__ == "__main__":
    unittest.main()
