"""待ち受けポートを他プロセスに奪われないことを確認する。

Windows の SO_REUSEADDR は Unix と意味が違い、既に待ち受けているポートへの
二重バインドを許す。ネットワーク技術者の PC には他の Syslog / TFTP ツールが
入っていることが多く、黙って割り込むとパケットの行き先が不定になり、
「ログが半分消える」といった追跡困難な症状を生む。
"""
import socket
import sys
import unittest

sys.path.insert(0, "src")

from core.sockets import set_exclusive_bind

WINDOWS = sys.platform == "win32"


class ExclusiveBindTest(unittest.TestCase):
    def _tcp(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        return s

    def _udp(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        return s

    @unittest.skipUnless(WINDOWS, "SO_EXCLUSIVEADDRUSE は Windows のみ")
    def test_tcp_port_cannot_be_stolen(self):
        first = self._tcp()
        set_exclusive_bind(first)
        first.bind(("0.0.0.0", 0))
        first.listen(5)
        port = first.getsockname()[1]

        thief = self._tcp()
        thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with self.assertRaises(OSError, msg="待ち受け中の TCP ポートを奪えてしまった"):
            thief.bind(("0.0.0.0", port))

    @unittest.skipUnless(WINDOWS, "SO_EXCLUSIVEADDRUSE は Windows のみ")
    def test_udp_port_cannot_be_stolen(self):
        first = self._udp()
        set_exclusive_bind(first)
        first.bind(("0.0.0.0", 0))
        port = first.getsockname()[1]

        thief = self._udp()
        thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with self.assertRaises(OSError, msg="待ち受け中の UDP ポートを奪えてしまった"):
            thief.bind(("0.0.0.0", port))

    def test_rebind_after_close_still_works(self):
        """停止→再起動で同じポートを取り直せること（TIME_WAIT で詰まらない）。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        set_exclusive_bind(s)
        s.bind(("0.0.0.0", 0))
        s.listen(5)
        port = s.getsockname()[1]
        s.close()

        again = self._tcp()
        set_exclusive_bind(again)
        again.bind(("0.0.0.0", port))   # 例外が出なければよい
        again.listen(5)

    def test_helper_is_safe_on_non_windows(self):
        """Windows 以外でも例外を出さないこと。"""
        s = self._udp()
        set_exclusive_bind(s)
        s.bind(("127.0.0.1", 0))


class RestartOnSamePortTest(unittest.TestCase):
    """実際にクライアントが接続したポートでも、停止直後に再起動できること。

    SO_REUSEADDR を外したので、TIME_WAIT のコネクションが残った状態で
    リスナーを張り直せるかを確認する。
    """

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import tempfile
        import unittest.mock
        from pathlib import Path
        fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                 return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)
        home = unittest.mock.patch(
            "core.config_manager.app_data_dir",
            return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)

    def test_sftp_restart_on_same_port_after_real_connection(self):
        import tempfile
        import time

        import paramiko
        from core.sftp_server import SFTPServerManager

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        root = tempfile.mkdtemp(prefix="netbelt-restart-")
        for cycle in (1, 2):
            m = SFTPServerManager()
            self.addCleanup(m.stop)
            self.assertTrue(
                m.start(port=port, root_dir=root, username="u", password="p"),
                "%d回目の起動でポート %d を取り直せなかった" % (cycle, port))
            deadline = time.time() + 15
            while not m.is_running and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(m.is_running)

            # 実接続を作って TIME_WAIT を発生させる
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect("127.0.0.1", port=port, username="u", password="p",
                      look_for_keys=False, allow_agent=False, timeout=10)
            c.open_sftp().listdir(".")
            c.close()
            m.stop()   # 間を置かずに次のサイクルへ


class ExclusiveBindOverridesReuseAddrTest(unittest.TestCase):
    """SO_REUSEADDR が既に立っているソケットにも排他バインドを効かせられること。

    pysnmp は自前のトランスポートソケットへ無条件に SO_REUSEADDR を立てる
    （pysnmp/carrier/asyncore/base.py）。Windows ではその状態で
    SO_EXCLUSIVEADDRUSE を立てようとすると WinError 10022 になるため、
    先に SO_REUSEADDR を戻す必要がある。
    """

    def test_can_be_applied_after_so_reuseaddr(self):
        import socket
        import sys
        from core.sockets import set_exclusive_bind

        if sys.platform != "win32":
            self.skipTest("SO_EXCLUSIVEADDRUSE は Windows のみ")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        set_exclusive_bind(sock)

        self.assertEqual(
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE), 1,
            "SO_REUSEADDR 済みのソケットに SO_EXCLUSIVEADDRUSE を立てられていない")
        self.assertEqual(
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0,
            "SO_REUSEADDR が戻されていない")

    def test_port_cannot_be_stolen_after_the_override(self):
        """他プロセスが素の bind で握っているポートを奪わないこと。"""
        import socket
        import sys
        from core.sockets import set_exclusive_bind

        if sys.platform != "win32":
            self.skipTest("SO_EXCLUSIVEADDRUSE は Windows のみ")

        victim = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(victim.close)
        victim.bind(("0.0.0.0", 0))
        port = victim.getsockname()[1]

        thief = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(thief.close)
        thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        set_exclusive_bind(thief)

        with self.assertRaises(OSError):
            thief.bind(("0.0.0.0", port))


class ServersUseExclusiveBindTest(unittest.TestCase):
    """各サーバが SO_REUSEADDR ではなく共通ヘルパーを使っていること。"""

    FILES = [
        "src/core/sftp_server.py",
        "src/core/snmp_manager.py",
        "src/core/syslog_receiver.py",
        "src/core/tftp_server.py",
    ]

    def test_no_raw_so_reuseaddr(self):
        import io
        for path in self.FILES:
            with self.subTest(path=path):
                text = io.open(path, encoding="utf-8").read()
                self.assertNotIn(
                    "SO_REUSEADDR", text,
                    "%s が SO_REUSEADDR を直接設定している（ポートを奪える）" % path)
                self.assertIn(
                    "set_exclusive_bind", text,
                    "%s が共通ヘルパーを使っていない" % path)


if __name__ == "__main__":
    unittest.main()
