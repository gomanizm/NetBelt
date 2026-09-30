"""向きの違う転送コマンドを待ち行列に積んだとき、先の方の予約・行・待ちを残さないこと。

何が起きていたか（実測、b003624。127.0.0.1 のみ）: pyftpdlib 2.2.0 は受信
（_in_dtp_queue）と送信（_out_dtp_queue）の待ち行列を別々に持ち、データ接続が
来ると送信の方だけを使う（受信の方は残る）。待ち行列の上書きの片付け
（push_dtp_data と _leave_overwritten_stor）は同じ向きの上書きしか見ていな
かったため、

  1) PASV → STOR hold.cfg（150）→ RETR b.cfg（150）→ データ接続で b.cfg を受け
     取る（226）: 通知は [開始 hold, 開始 b, 完了 b] で、hold.cfg の行（_tx）と
     予約（_uploads）が残った。その間、同じ IP の別の接続からの STOR hold.cfg は
     「450 File busy」で断られた
  2) 続けて同じ接続で普通に取得すると（ftplib の retrbinary: PASV → 接続 →
     RETR a.cfg）、残った STOR がその新しいデータ接続に結びつき、「425 Data
     connection busy」で断られた。クライアントがデータ接続を閉じると、hold.cfg は
     何も送られていないのに完了（0 バイト）と記録された
  3) PASV → RETR b.cfg → STOR hold.cfg → データ接続で hold.cfg を送る: データ接続
     では b.cfg が返され、送った中身は hold.cfg に書かれず、1)・2) と同じことが
     起きた
  4) PASV → STOR hold.cfg → LIST → データ接続で一覧を受け取る、PASV → RETR b.cfg
     → STOU でも同じ（STOU は予約を持たないので、別の接続は断られない）

どう直したか: 後の転送コマンドが待ち行列を置き換えたら、向きによらず先の待ちを
捨てる。push_dtp_data（RETR・LIST など）で送信を積んだら、待っていた受信を
REIN / ABOR と同じく片付ける（ファイルを閉じ、予約を外し、行を中断で閉じる）。
STOR / APPE / STOU で受信を積んだら、待っていた送信を RETR の上書きと同じく
片付ける（ファイルを閉じ、行を離れる）。
"""
import ftplib
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import _FtpServerCase  # noqa: E402

A = b"A-CONTENT-" * 8
B = b"B-CONTENT-" * 8
HOLD = b"HOLD-CONTENT-" * 8
HOLD_AGAIN = b"HOLD-AGAIN-" * 8

# 混ぜた転送を終えた後に見込む状態（_aftermath の戻り値）
AFTERMATH = {
    # 台帳の行と予約が残っていない
    "rows": [],
    "uploads": [],
    # 同じ IP の別の接続から hold.cfg を上げ直せる（開始から出る）
    "other_stor": "226",
    "other_events": [("started", "hold.cfg"), ("complete", "hold.cfg")],
    "hold": HOLD_AGAIN,
    # 同じ接続での次の普通の取得が通り、0 バイトの偽の完了が出ない
    "next_retr": "226",
    "next_retr_data": A,
    "next_events": [("started", "a.cfg"), ("complete", "a.cfg")],
}


class MixedDirectionQueueTest(_FtpServerCase):
    maxDiff = None   # 落ちたとき、違う項目を全部見せる

    def setUp(self):
        super().setUp()
        for name, payload in (("a.cfg", A), ("b.cfg", B)):
            with open(self.real(name), "wb") as seed:
                seed.write(payload)
        self.events = []
        for signal, kind in ((self.m.transfer_started, "started"),
                             (self.m.transfer_complete, "complete"),
                             (self.m.transfer_interrupted, "interrupted")):
            slot = (lambda *args, kind=kind: self.events.append((kind, args[1])))
            signal.connect(slot)
            self.addCleanup(signal.disconnect, slot)

    def _pump(self, seconds=0.3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    def _sync(self, ftp):
        """直前のコマンドの処理を、応答の後の台帳の更新まで待つ"""
        self.assertTrue(ftp.sendcmd("NOOP").startswith("200"))
        self._pump()

    def _queue(self, ftp, *commands):
        """データ接続を張らずに転送コマンドだけを受けさせ、応答を返す"""
        ftp.sendcmd("PASV")
        responses = []
        for command in commands:
            resp = ftp.sendcmd(command)
            self.assertTrue(resp.startswith("150"), resp)
            responses.append(resp)
        self._sync(ftp)
        return responses

    def _final(self, ftp):
        """データ接続の転送の最終応答の番号（断られたときも番号を返す）"""
        try:
            resp = ftp.voidresp()
        except ftplib.Error as err:
            resp = str(err)
        self._sync(ftp)
        return resp[:3]

    def _open_data(self, ftp):
        host, port = ftp.makepasv()
        data = socket.create_connection((host, port), timeout=10)
        self.addCleanup(self._close_socket, data)
        return data

    @staticmethod
    def _read_all(data):
        got = b""
        try:
            while True:
                chunk = data.recv(65536)
                if not chunk:
                    break
                got += chunk
        except OSError:
            pass
        return got

    def _receive_queued(self, ftp):
        """PASV を張り直し、待ち行列の転送をデータ接続で受け取る"""
        data = self._open_data(ftp)
        got = self._read_all(data)
        data.close()
        return self._final(ftp), got

    def _send_queued(self, ftp, payload):
        """PASV を張り直し、待ち行列の受信へデータ接続で payload を送る。
        サーバーが何かを返してきたら（送信の方が使われた）それも返す"""
        data = self._open_data(ftp)
        got = b""
        try:
            data.sendall(payload)
            data.shutdown(socket.SHUT_WR)
            got = self._read_all(data)
        except OSError:
            pass
        data.close()
        return self._final(ftp), got

    def _names(self):
        return sorted(os.path.basename(path) for (_ip, path, _d) in self.m._tx)

    def _aftermath(self, ftp):
        """混ぜた転送の後の状態を集める。

        落ちるときにどれが違うかを全部見せるため、1 つの辞書にまとめて比べる。
        別の接続の上げ直しは、同じ接続の次の取得より先に行う（直す前は、次の
        取得で残った STOR が偽の完了になり、そこで予約が外れていた）
        """
        left = {"rows": self._names(),
                "uploads": sorted(os.path.basename(k) for k in self.m._uploads)}
        del self.events[:]
        other = self.client()
        try:
            left["other_stor"] = self.upload(other, "hold.cfg", HOLD_AGAIN)[:3]
        except ftplib.Error as err:
            left["other_stor"] = str(err)[:3]
        self._sync(other)
        left["other_events"] = list(self.events)
        left["hold"] = self.read("hold.cfg")
        del self.events[:]
        got = []
        try:
            left["next_retr"] = ftp.retrbinary("RETR a.cfg", got.append)[:3]
        except ftplib.Error as err:
            left["next_retr"] = str(err)[:3]
        self._sync(ftp)
        # 残った受信がデータ接続に結びついていれば、閉じた後に偽の完了が届く
        self._pump(0.5)
        left["next_retr_data"] = b"".join(got)
        left["next_events"] = list(self.events)
        return left

    def _check(self, ftp, transfer, expected_transfer, expected_events):
        observed = {"transfer": transfer, "events": list(self.events)}
        observed.update(self._aftermath(ftp))
        expected = dict(AFTERMATH, transfer=expected_transfer, events=expected_events)
        self.assertEqual(observed, expected)

    def test_retr_after_a_queued_stor_drops_the_stor(self):
        ftp = self.client()
        self._queue(ftp, "STOR hold.cfg", "RETR b.cfg")
        transfer = self._receive_queued(ftp)
        self._check(ftp, transfer, ("226", B),
                    [("started", "hold.cfg"), ("interrupted", "hold.cfg"),
                     ("started", "b.cfg"), ("complete", "b.cfg")])

    def test_stor_after_a_queued_retr_drops_the_retr(self):
        ftp = self.client()
        self._queue(ftp, "RETR b.cfg", "STOR hold.cfg")
        resp, got = self._send_queued(ftp, HOLD)
        # 送った中身が hold.cfg に書かれ、データ接続では何も返されない
        transfer = (resp, got, self.read("hold.cfg"))
        self._check(ftp, transfer, ("226", b"", HOLD),
                    [("started", "b.cfg"), ("interrupted", "b.cfg"),
                     ("started", "hold.cfg"), ("complete", "hold.cfg")])

    def test_stor_after_a_queued_retr_of_the_same_file(self):
        # 同じファイルの RETR を捨てると、その片付けがこの接続の転送（_tx_*）を
        # 降ろす。ftp_STOR が捨てるより先にアップロードの _tx_* を設定すると、
        # それが消えて、名前の空の行ができ、進捗も出なくなる（変異で実測）
        ftp = self.client()
        self._queue(ftp, "RETR b.cfg", "STOR b.cfg")
        resp, got = self._send_queued(ftp, HOLD)
        transfer = (resp, got, self.read("b.cfg"))
        self._check(ftp, transfer, ("226", b"", HOLD),
                    [("started", "b.cfg"), ("interrupted", "b.cfg"),
                     ("started", "b.cfg"), ("complete", "b.cfg")])

    def test_list_after_a_queued_stor_drops_the_stor(self):
        ftp = self.client()
        self._queue(ftp, "STOR hold.cfg", "LIST")
        resp, got = self._receive_queued(ftp)
        transfer = (resp, b"b.cfg" in got)
        self._check(ftp, transfer, ("226", True),
                    [("started", "hold.cfg"), ("interrupted", "hold.cfg")])

    def test_stou_after_a_queued_retr_drops_the_retr(self):
        ftp = self.client()
        responses = self._queue(ftp, "RETR b.cfg", "STOU")
        unique = responses[1].split("FILE:", 1)[1].strip()
        resp, got = self._send_queued(ftp, HOLD)
        transfer = (resp, got, self.read(unique))
        self._check(ftp, transfer, ("226", b"", HOLD),
                    [("started", "b.cfg"), ("interrupted", "b.cfg"),
                     ("complete", unique)])

    def test_refused_stou_keeps_the_queued_retr(self):
        # 断られた STOU は受信を積まないので、待っている RETR を捨てない。
        # 捨てると、張ったデータ接続に何も流れず、クライアントが固まる（実測）
        ftp = self.client()
        self._queue(ftp, "RETR b.cfg")
        self.assertTrue(ftp.sendcmd("REST 5").startswith("350"))
        with self.assertRaises(ftplib.error_temp) as refused:
            ftp.sendcmd("STOU")   # REST の後の STOU は 450 で断られる
        self.assertTrue(str(refused.exception).startswith("450"),
                        str(refused.exception))
        self._sync(ftp)
        # データ接続を張る前に確かめる（捨てていれば、ここで行が中断になっている）
        self.assertEqual(self.events, [("started", "b.cfg")])
        # 待っていた RETR が先頭から流れる（REST の位置は断られた STOU で消える）
        transfer = self._receive_queued(ftp)
        self._check(ftp, transfer, ("226", B),
                    [("started", "b.cfg"), ("complete", "b.cfg")])


if __name__ == "__main__":
    unittest.main()
