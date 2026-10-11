"""内蔵 FTP の制御の待ち受けが排他でなく、同じ PC の同じユーザーのほかのソケットに横取りされる件。

何が起きていたか（実測、82f4165。127.0.0.1 のみ）: pyftpdlib の待ち受けは
SO_EXCLUSIVEADDRUSE を立てない（bind の直前に呼ぶ set_reuse_addr() は、Windows では
何もしない形に上書きされている）。Windows では、0.0.0.0 で待ち受けている排他でない
ポートにも、同じユーザーのソケットなら、特定アドレス（127.0.0.1 や LAN の IP）の
同じ番号への bind が通り（別のユーザーの bind は今の形でも OS が断る）、その
宛先への接続は後から bind した側へ届く。

  1) 起動中の制御ポートへ、別のソケットが 127.0.0.1 で bind して待ち受けると、
     127.0.0.1 への制御接続はそちらへ届き、FTP の挨拶が返らなかった
  2) PASV の待ち受けも 127.0.0.1 の特定アドレスへ bind するので、パッシブの範囲が
     制御ポートを含むと、PASV が制御ポートの番号で待ち受けた（範囲 2 番号で 20 回中
     7〜12 回）。その間に張った別の制御接続は、データ接続として受け付けられる

どう直したか: 制御の待ち受けを、ほかの受信機能（SFTP / TFTP / Syslog / SNMP Trap）と
同じく排他（core.sockets の set_exclusive_bind）にする。pyftpdlib が bind の直前に
呼ぶ set_reuse_addr() で掛ける。特定アドレスの bind は WSAEACCES で断られ、PASV は
範囲のほかの番号を使う。

2) は、start() がパッシブの範囲から制御ポートを除く修正
（test_ftp_server_passive_range_excludes_control_port.py）でも起きない。そのため
test_pasv_does_not_listen_on_the_control_port は、排他にしなくても通る（その修正と
重なる。排他を外した変異で確かめた）。排他そのものを守るのは
test_another_socket_cannot_take_the_control_port_on_a_specific_address と、
test_ftp_server_exclusive_control_lan_restart.py の (a)。
"""
import ftplib
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"


@unittest.skipUnless(sys.platform == "win32", "Windows の bind の規則に依る")
class FtpExclusiveControlPortTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        patcher = mock.patch("core.firewall.ensure_inbound_allow",
                             return_value=(True, "test stub"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _start(self, port=0, passive_ports=(50100, 50150)):
        from core.ftp_server import FTPServerManager
        m = FTPServerManager()
        root = tempfile.mkdtemp(prefix="netbelt-ftp-excl-")
        self.assertTrue(m.start(port=port, root_dir=root, username=USER,
                                password=PASSWORD, passive_ports=passive_ports))
        self.addCleanup(self.app.processEvents)
        self.addCleanup(m.stop)
        return m

    @staticmethod
    def _bindable_like_pasv(port):
        """127.0.0.1:port へ、PASV の待ち受けと同じ形（オプションなし）で bind できるか"""
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True

    def _control_port_with_free_next(self):
        """制御ポート X を選ぶ。パッシブの範囲 (X, X+1) の X+1 は PASV が使える番号にする。

        X+1 がほかのソケットに 127.0.0.1 や排他のワイルドカードで使われていると、
        PASV の bind はそこで断られる。start() は範囲から X を除く（X+1 だけが残る）
        ので、PASV は 425 で断られる（_RangePassiveDTP。除く修正と 425 の前は、
        X を最後に試した回に制御接続ごと切れた）。製品の退行と見分けがつかないので、
        その X は使わない。
        確かめは、PASV と同じ形（127.0.0.1、オプションなし）の bind だけで行う。
        同じユーザーの既定のワイルドカードの待ち受けは、この bind を断らない（PASV は
        その番号で待ち受けて通る）ので、0.0.0.0 では確かめない。別のユーザーの
        待ち受けは既定のワイルドカードでも断る（Microsoft の表）が、確かめも同じ形の
        bind なので、その番号は確かめで見つかり、X を選び直す。

        OS は番号を連番で渡す（この PC の実測）ので、X を受け取った後にもう 1 つ
        受け取って閉じ、X+1 を先に使っておく。そうしないと、このテストの
        クライアントが connect() で X+1 を自分側の番号として受け取ることが多く
        （この PC で 3 回測り、5 回中 3 回・10 回中 9 回・30 回中 30 回）、PASV の
        127.0.0.1:X+1 の bind がその接続と同じ番号になる。この形で bind が通るかは
        Microsoft の表に無く、この PC でしか確かめていない
        """
        for _ in range(50):
            with socket.socket() as probe:
                probe.bind(("0.0.0.0", 0))
                port = probe.getsockname()[1]
            with socket.socket() as spare:
                spare.bind(("0.0.0.0", 0))
            if port < 65535 and self._bindable_like_pasv(port + 1):
                return port
        self.skipTest("X+1 を PASV が使える制御ポート X を選べなかった")

    def _pasv(self, ftp, port):
        """PASV を張り、データの番号を返す。

        断られたか制御接続が切れたら、X+1 の様子を添えて落とす"""
        try:
            return ftp.makepasv()[1]
        except (EOFError, OSError, ftplib.error_temp) as e:
            try:
                client = ftp.sock.getsockname()[1]
            except (AttributeError, OSError):
                client = "不明"
            self.fail(
                "PASV が断られたか、制御接続が切れた（%r）。いま %d 番へ 127.0.0.1 で"
                " bind %s（このクライアントの自分側の番号は %s）。できない場合は、起動の後に"
                "ほかのソケットが %d 番を使い、PASV の候補が尽きた可能性がある"
                "（製品の退行とは限らない）"
                % (e, port + 1,
                   "できる" if self._bindable_like_pasv(port + 1) else "できない",
                   client, port + 1))

    def _greeting(self, port):
        ftp = ftplib.FTP()
        self.addCleanup(ftp.close)
        ftp.connect("127.0.0.1", port, timeout=5)
        return ftp

    def test_another_socket_cannot_take_the_control_port_on_a_specific_address(self):
        m = self._start()
        for reuse in (False, True):
            # subTest ごとに閉じる。前の subTest で bind できたソケットが残ると、
            # 修正前の形でも、次の subTest はそれに断られて通ってしまう
            with self.subTest(SO_REUSEADDR=reuse), socket.socket() as other:
                if reuse:
                    other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with self.assertRaises(OSError,
                                       msg="制御ポートへ 127.0.0.1 で bind できた"):
                    other.bind(("127.0.0.1", m.port))
                    other.listen(1)
        self.assertTrue(self._greeting(m.port).getwelcome().startswith("220"))

    def test_pasv_does_not_listen_on_the_control_port(self):
        """PASV が制御ポートで待ち受けず、その間の新しい制御接続へ挨拶が返ること。

        start() が範囲から制御ポートを除くので、排他にしなくても通る（排他の確かめは
        上の試験。冒頭の docstring を参照）。範囲 2 番号で取り違えが起きた形を、
        そのまま流す回帰の確かめとして残す
        """
        port = self._control_port_with_free_next()
        m = self._start(port=port, passive_ports=(port, port + 1))
        ftp = self._greeting(m.port)
        ftp.login(USER, PASSWORD)
        seen = set()
        for _ in range(20):
            seen.add(self._pasv(ftp, port))
            ftp.sendcmd("ABOR")
        self.assertNotIn(m.port, seen, "PASV が制御ポートの番号で待ち受けた")
        # PASV の待ち受けを開いたまま張った制御接続が、データ接続として
        # 受け付けられないこと
        self._pasv(ftp, port)
        self.assertTrue(self._greeting(m.port).getwelcome().startswith("220"))


if __name__ == "__main__":
    unittest.main()
