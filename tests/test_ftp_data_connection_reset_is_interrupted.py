"""受信中のデータ接続が RST で切れたアップロードを、時間によらず中断とすること（R03）。

何が起きていたか（実測、9fee4af。127.0.0.1 のみ）: STOR の受信中に、書き手が
データ接続を RST（SO_LINGER 0 での close。FIN を送らない）で切ると、最後の
データから DATA_KEEPALIVE_SECONDS − 1 秒未満なら「226 Transfer complete.」を
返し、パネルにも完了と記録していた。Windows は読み終えていない受信データを
RST の到着で捨てる（recv は 10054 で 0 バイト）ので、送り切った直後の RST では、
0 バイトや途中までのファイルが完了になっていた（調査時の実測: 262144 バイト
送って 0 バイト、8388608 バイト送って 65536 バイト）。何も送らずに RST しても、
空のファイルが完了になっていた。

どう直したか: 受信中の recv が ECONNRESET を返したら、経過時間によらず
「426 Connection lost; transfer aborted.」で未完了として閉じる（予約は
on_incomplete_file_received が外し、行は中断で閉じる）。vsftpd も受信中の
読み取りエラーは 426 にしている。FIN による通常の EOF と、送信（RETR）の
向きは変えない。

限界: 中身を救うものではない。途中までのファイルは本来の名前で残り、上書きなら
前のファイルは STOR の開始時に切り詰められている。中身が揃っていても RST で
終われば中断と出る。書き手が中止して FIN で閉じた転送は、これまでどおり完了。
"""
import ftplib
import os
import socket
import struct
import sys
import time
import unittest

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import _FtpServerCase  # noqa: E402


def _reset(sock):
    """sock を RST で閉じる（SO_LINGER 0。FIN を送らない）"""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
    sock.close()


class FtpDataConnectionResetTest(_FtpServerCase):
    # サーバーが読み終える前に RST が届きうる量（調査時は 0 バイトで保存された）
    BURST = 262144

    def setUp(self):
        super().setUp()
        self.events = []
        for signal, kind in ((self.m.transfer_complete, "complete"),
                             (self.m.transfer_interrupted, "interrupted")):
            slot = (lambda *args, kind=kind:
                    self.events.append((kind, args[1], args[-1])))
            signal.connect(slot)
            self.addCleanup(signal.disconnect, slot)

    def _receiving_handler(self, received=0, timeout=10.0):
        """受信中で、received バイトを受け取り終えたサーバー側データ接続を返す"""
        from pyftpdlib.handlers import DTPHandler
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                handlers = list(self.m._server.ioloop.socket_map.values())
            except RuntimeError:
                handlers = []
            for handler in handlers:
                if (isinstance(handler, DTPHandler) and handler.receive
                        and handler.tot_bytes_received >= received):
                    return handler
            time.sleep(0.02)
        self.fail("受信中のデータ接続が見つからない")

    def _start(self, name):
        """STOR を始め、サーバーが受信を始めたデータ接続を返す。

        受け入れられる前に RST すると、遅い環境ではサーバーがデータ接続を
        受け取れずに STOR が待ったままになりうるので、受信中になるまで待つ
        """
        ftp = self.client()
        data = ftp.transfercmd("STOR " + name)
        self.addCleanup(self._close_socket, data)
        self._receiving_handler(0)
        return ftp, data

    @staticmethod
    def _reply(ftp):
        try:
            return ftp.voidresp()
        except ftplib.Error as exc:
            return str(exc)

    def _wait_event(self, event, timeout=10.0):
        """パネルへ渡る転送の知らせ（待受スレッドから届く）を待つ"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if event in self.events:
                return True
            time.sleep(0.02)
        return False

    def _assert_interrupted(self, name, reply):
        self.assertTrue(reply.startswith("426"),
                        "RST で切れた受信が未完了の応答になっていない: %r" % reply)
        self.assertTrue(self._wait_event(("interrupted", name, "upload")),
                        "中断と記録されていない: %r" % (self.events,))
        self.assertNotIn(("complete", name, "upload"), self.events,
                         "RST で切れた受信が完了と記録された")
        # 予約は外れている（B の同名の STOR が待たずに通る）
        b = self.client()
        self.assertTrue(self.upload(b, name, b"Z").startswith("226"))

    def test_a_reset_right_after_sending_everything_is_interrupted(self):
        """送り切った直後の RST は 426 と中断にする（未読の分は Windows が捨てる）"""
        for size in (20480, self.BURST):
            with self.subTest(size=size):
                name = "burst-%d.cfg" % size
                ftp, data = self._start(name)
                data.sendall(b"x" * size)
                _reset(data)
                reply = self._reply(ftp)
                # 0 バイトや途中までになりうる（どこまで読めたかは環境による）
                self.assertLessEqual(os.path.getsize(self.real(name)), size)
                self._assert_interrupted(name, reply)

    def test_a_reset_after_the_server_read_everything_is_interrupted(self):
        """サーバーが全部読んだ後の RST も中断にする。ファイルは消さない。

        ストリームモードの終わりはデータ接続の閉じ方でしか示されないので、
        中身が揃っていても RST なら揃ったとは言えない
        """
        # 受け取った量で見分ける後退（多く読めた受信だけ完了に戻す）も落とせるよう、
        # 64 KiB を送り、サーバーが読み終えるのを待ってから RST する
        payload = b"y" * 65536
        ftp, data = self._start("read-all.cfg")
        data.sendall(payload)
        self._receiving_handler(len(payload))
        _reset(data)
        reply = self._reply(ftp)
        self.assertEqual(self.read("read-all.cfg"), payload)
        self._assert_interrupted("read-all.cfg", reply)

    def test_a_reset_before_any_data_is_interrupted(self):
        """何も送らずに RST した場合も、空のファイルを完了とせず中断にする"""
        ftp, data = self._start("empty.cfg")
        _reset(data)
        reply = self._reply(ftp)
        self.assertEqual(self.read("empty.cfg"), b"")
        self._assert_interrupted("empty.cfg", reply)

    def test_a_normal_close_after_sending_everything_is_complete(self):
        """FIN（普通の close）で終えた受信は、これまでどおり 226 と完了で、全量が残る"""
        payload = bytes(range(256)) * (self.BURST // 256)
        ftp, data = self._start("fin.cfg")
        data.sendall(payload)
        data.close()
        reply = self._reply(ftp)
        self.assertTrue(reply.startswith("226"), reply)
        self.assertTrue(self._wait_event(("complete", "fin.cfg", "upload")),
                        "完了と記録されていない: %r" % (self.events,))
        self.assertNotIn(("interrupted", "fin.cfg", "upload"), self.events)
        self.assertEqual(self.read("fin.cfg"), payload)

    def test_downloads_are_not_affected(self):
        """送信（RETR）の向きは変えない: 取り切れば完了、相手の RST は従来の 426"""
        payload = b"r" * (4 * 1024 * 1024)
        with open(self.real("big.bin"), "wb") as handle:
            handle.write(payload)
        ftp = self.client()
        with self.subTest("normal"):
            got = []
            reply = ftp.retrbinary("RETR big.bin", got.append)
            self.assertTrue(reply.startswith("226"), reply)
            self.assertEqual(b"".join(got), payload)
            self.assertTrue(self._wait_event(("complete", "big.bin", "download")))
        with self.subTest("reset"):
            self.events.clear()
            host, port = ftp.makepasv()
            data = socket.socket()
            self.addCleanup(self._close_socket, data)
            # 受信バッファを小さくして、サーバーが送り切る前に RST が届くようにする
            data.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            data.settimeout(10)
            data.connect((host, port))
            ftp.sendcmd("RETR big.bin")
            data.recv(4096)
            _reset(data)
            reply = self._reply(ftp)
            # pyftpdlib の送信側の応答のまま（受信用の 426 Connection lost ではない）
            self.assertTrue(reply.startswith("426 Transfer aborted"), reply)
            self.assertTrue(self._wait_event(("interrupted", "big.bin", "download")),
                            "中断と記録されていない: %r" % (self.events,))
            self.assertNotIn(("complete", "big.bin", "download"), self.events)


if __name__ == "__main__":
    unittest.main()
