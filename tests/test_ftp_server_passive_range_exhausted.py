"""FTP のパッシブの範囲の番号がすべて使えないとき、PASV / EPSV が範囲の外の番号へ
切り替わらず、制御接続を切らずに 425 で断ること。

何が起きていたか（実測、43b2980・pyftpdlib 2.2.0。127.0.0.1 のみ）: pyftpdlib の
PassiveDTP は、範囲の番号を無作為な順に bind する。

- 最後に試した番号が使用中（EADDRINUSE。ほかのソケットが 127.0.0.1 の同じ番号で
  待ち受けているなど）だと、OS に任せた番号（範囲の外）で待ち受け、227 / 229 で
  その番号を返した。利用者がファイアウォールで許可した範囲の外の番号になる。
  理由はパネルのどこにも出なかった（pyftpdlib のログだけ）。
- 最後に試した番号が断られた（WSAEACCES。ほかのアプリが 0.0.0.0 の同じ番号を
  排他（SO_EXCLUSIVEADDRUSE）で待ち受けているなど）ときは、bind しないまま
  listen へ進んで例外になり、PASV に応答せず制御接続ごと切れた（同じ接続の
  NOOP は WinError 10053）。

どう直したか: PASV / EPSV の待ち受けを、待ち受ける直前に番号を確かめる
_RangePassiveDTP にした。範囲の外（bind していないときを含む）なら閉じて 425 で
断り、パネルのログへ理由を出す。範囲の外の番号は使わない。制御接続はそのまま
続き、範囲の番号が空けば次の PASV から使える。
"""
import errno
import ftplib
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")
sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import _FtpServerCase  # noqa: E402


def _drain_qt(app):
    """配送待ちの Qt の知らせを処理する"""
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


class _ExhaustedRangeCase(_FtpServerCase):
    """パッシブの範囲を 1 番号 Y だけにし、Y をほかのソケットで塞いで起動する土台。

    制御ポートは土台（_FtpServerCase）が範囲の外から選ぶ。テストは持たない
    （塞ぎ方ごとのクラスが check_* を呼ぶ）
    """

    def _make_blocker(self):
        """Y を塞ぐソケットを返す（塞ぎ方ごとのクラスで決める）"""
        raise NotImplementedError

    def _assert_refused_like_pyftpdlib_sees_it(self, err):
        """前提: PASV と同じ形の bind が、直した経路に入る理由で断られること"""
        raise NotImplementedError

    def _check_stock_outcome(self, ftp):
        """前提: 修正の前の PassiveDTP での結果（塞ぎ方ごとのクラスで決める）"""
        raise NotImplementedError

    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        # 後始末は後から積んだ順に走るので、これらは土台の停止の後に走る
        self.addCleanup(_drain_qt, app)
        self.blocker = self._make_blocker()
        self.addCleanup(self.blocker.close)
        self.y = self.blocker.getsockname()[1]
        self.passive_ports = (self.y, self.y)
        super().setUp()

    def _assert_blocked(self):
        """前提: PASV と同じ形（127.0.0.1、オプションなし）で Y へ bind できないこと"""
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", self.y))
            except OSError as err:
                self._assert_refused_like_pyftpdlib_sees_it(err)
            else:
                self.fail("前提: %d 番が塞がれていない" % self.y)

    def check_refused_with_425_and_the_session_goes_on(self):
        """PASV / EPSV を 425 で断り（範囲の外の番号を返さない）、制御接続は続くこと。
        理由をパネルのログへ出すこと。Y が空けば、同じ接続の次の PASV から Y を使うこと。"""
        self._assert_blocked()
        ftp = self.client()
        # 断った待ち受けのソケットを ioloop に残さない（PASV のたびに増えない）
        sockets = self.m._server.ioloop.socket_map
        before = len(sockets)
        for cmd in ("PASV", "EPSV"):
            with self.subTest(cmd):
                try:
                    resp = ftp.sendcmd(cmd)
                except ftplib.error_temp as err:
                    resp = str(err)
                except (EOFError, OSError) as err:
                    self.fail("%s で制御接続が切れた: %r" % (cmd, err))
                self.assertTrue(resp.startswith("425"),
                                "%s が断られず、範囲の外の番号で待ち受けた: %s"
                                % (cmd, resp))
                self.assertTrue(ftp.sendcmd("NOOP").startswith("200"))
                found = self._wait_activity(
                    "passiveポート範囲 %d-%d" % (self.y, self.y), cmd)
                self.assertIsNotNone(found, "断った理由がログに出ていない: %r"
                                     % (self.activity,))
                self.assertEqual(found[0], "127.0.0.1")
        self.assertEqual(len(sockets), before,
                         "断った PASV / EPSV の待ち受けが ioloop に残っている")
        # Y が空けば、同じ制御接続で Y を使って転送できる
        self.blocker.close()
        resp = ftp.sendcmd("PASV")
        self.assertEqual(ftplib.parse227(resp)[1], self.y, resp)
        with open(self.real("after.cfg"), "wb") as seed:
            seed.write(b"after")
        got = []
        self.assertTrue(ftp.retrbinary("RETR after.cfg", got.append)
                        .startswith("226"))
        self.assertEqual(b"".join(got), b"after")

    def check_the_premise_with_the_stock_passive_dtp(self):
        """前提の確かめ: 修正の前と同じ pyftpdlib の PassiveDTP に戻すと、この塞ぎ方で
        範囲の外の番号へ切り替わるか、制御接続ごと切れること（塞ぎ方が、直した経路を
        通ることの確かめ。pyftpdlib を更新して振る舞いが変わったら、ここで分かる）"""
        from pyftpdlib.handlers import FTPHandler
        self._assert_blocked()
        # _Handler は起動ごとに作るクラスなので、戻すのはこの起動の分だけ
        self.m._server.handler.passive_dtp = FTPHandler.passive_dtp
        self._check_stock_outcome(self.client())


class SpecificAddressInUseTest(_ExhaustedRangeCase):
    """範囲の番号を、ほかのソケットが 127.0.0.1 で待ち受けている（EADDRINUSE）"""

    def _make_blocker(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return sock

    def _assert_refused_like_pyftpdlib_sees_it(self, err):
        # pyftpdlib は errno が EADDRINUSE のとき、OS に任せた番号へ切り替える
        self.assertEqual(err.errno, errno.EADDRINUSE, repr(err))

    def _check_stock_outcome(self, ftp):
        resp = ftp.sendcmd("PASV")
        self.assertNotEqual(ftplib.parse227(resp)[1], self.y,
                            "前提: 範囲の外の番号へ切り替わっていない: %s" % resp)

    def test_pasv_and_epsv_are_refused_and_the_session_goes_on(self):
        self.check_refused_with_425_and_the_session_goes_on()

    def test_premise_stock_pyftpdlib_leaves_the_range(self):
        self.check_the_premise_with_the_stock_passive_dtp()


@unittest.skipUnless(hasattr(socket, "SO_EXCLUSIVEADDRUSE"),
                     "SO_EXCLUSIVEADDRUSE は Windows だけ")
class ExclusiveListenerTest(_ExhaustedRangeCase):
    """範囲の番号を、ほかのアプリが 0.0.0.0 で排他に待ち受けている（WSAEACCES）"""

    def _make_blocker(self):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("0.0.0.0", 0))
        sock.listen(1)
        return sock

    def _assert_refused_like_pyftpdlib_sees_it(self, err):
        # pyftpdlib は PermissionError のとき、次の候補へ進む（最後なら bind しない）
        self.assertIsInstance(err, PermissionError)

    def _check_stock_outcome(self, ftp):
        with self.assertRaises((EOFError, OSError),
                               msg="前提: 制御接続が切れていない"):
            ftp.sendcmd("PASV")

    def test_pasv_and_epsv_are_refused_and_the_session_goes_on(self):
        self.check_refused_with_425_and_the_session_goes_on()

    def test_premise_stock_pyftpdlib_drops_the_session(self):
        self.check_the_premise_with_the_stock_passive_dtp()


if __name__ == "__main__":
    unittest.main()
