"""データ接続で転送が進んでいる間に来た転送コマンドを断ること。

何が起きていたか（実測、e548f64。c2bb66a でも同じ。127.0.0.1 のみ）: pyftpdlib
2.2.0 は、データ接続が既にあるときの転送コマンドに 125 を返し、そのデータ接続が
送受信するファイル（data_channel.file_obj）を差し替える。そのデータ接続で転送が
進んでいる最中でも同じで、待ち行列を上書きする経路（push_dtp_data と STOR の
上書きの片付け）を通らない。そのため

  1) PASV → データ接続 → RETR big.cfg（125）→ 転送中に RETR b.cfg（125）:
     big.cfg は全部届いたのに通知は [開始 big, 開始 b, 中断 b] で、big.cfg の行が
     台帳（_tx）とパネルに開始のまま残った。同じ IP の別の接続で big.cfg を取り
     直すと、残った行に束ねられて開始の通知が出ず、完了だけが届いた
  2) PASV → データ接続 → STOR x.cfg（125）で 1000 バイト → STOR y.cfg（125）→
     1000 バイト → 閉じる: 受けた中身が x.cfg と y.cfg に 1000 バイトずつ分かれ、
     通知は [開始 x, 開始 y, 完了 y] で、x.cfg の行が開始のまま残った
  3) 転送中の RETR の上に LIST（125）: big.cfg は全部届いたのに 426 で中断と
     通知され、LIST の中身は届かなかった

どう直したか: _Handler.process_command で、データ接続が転送中（cmd が設定済み）
なら転送コマンド（RETR・STOR・APPE・STOU・LIST・NLST・MLSD）を 425 で断り、
パネルのログへ理由を出す。ファイルは開かず、開始も通知しない。続いている転送は
そのまま最後まで進む。張っただけで待っているデータ接続（cmd が None）は、
今までどおり 125 で使える（対照）。

向きの違う転送コマンドも同じく断る。441ea02 で実測（127.0.0.1 のみ）した壊れ方は
同じ向きより重く、
  4) RETR big.bin の転送中に STOR keep.cfg（125）: keep.cfg が 40 バイトから
     0 バイトに切り詰められ、big.bin は 393216 バイトで止まってクライアントの
     recv が時間切れになった。通知は [開始 big, 開始 keep, 完了 keep] で、
     big.bin の行が残った（APPE では keep.cfg は残ったが、同じく止まった）
  5) STOR up.bin の受信中に RETR keep.cfg（125）: 最終応答が来ずに時間切れ、
     up.bin は 2000 バイト中 1000 バイト、行 [up.bin, keep.cfg] と予約が残った
     （LIST では up.bin は 0 バイト、行と予約が残った）
だった。以前は同じ向きの組み合わせしか見ていなかったので、425 の条件を同じ向き
だけ（dc.receive と送ったコマンドの向きが揃うときだけ）に絞っても、FTP の
テストは 1 件も落ちなかった（実測）。下の向きの違う 4 件は、その絞り方では
125 で受け付けて落ちる。
"""
import ftplib
import os
import shutil
import socket
import sys
import time
import unittest

sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import _FtpServerCase  # noqa: E402

B = b"B-CONTENT-" * 8
# 受け手が読まない間にサーバーが送り切れない大きさ。受信の窓は下の
# _open_data で 64 KiB に固定する（実測で、読む前に送れたのは約 320 KiB）
BIG_SIZE = 32 * 1024 * 1024
RCVBUF = 64 * 1024


class TransferWhileDataBusyTest(_FtpServerCase):
    def setUp(self):
        # 大きなファイルを一時フォルダに残さない。サーバーの停止（土台の
        # 片付け）より後に消すよう、土台の setUp より先に登録する
        self.addCleanup(self._remove_root)
        super().setUp()
        with open(self.real("b.cfg"), "wb") as seed:
            seed.write(B)
        with open(self.real("big.cfg"), "wb") as seed:
            seed.truncate(BIG_SIZE)
        self.events = []
        for signal, kind in ((self.m.transfer_started, "started"),
                             (self.m.transfer_progress, "progress"),
                             (self.m.transfer_complete, "complete"),
                             (self.m.transfer_interrupted, "interrupted")):
            slot = (lambda *args, kind=kind: self.events.append((kind, args[1])))
            signal.connect(slot)
            self.addCleanup(signal.disconnect, slot)

    def _remove_root(self):
        root = getattr(self, "root", None)
        if root:
            shutil.rmtree(root, ignore_errors=True)

    def _pump(self, seconds=0.3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    def _sync(self, ftp):
        """直前のコマンドの処理を、応答の後の台帳の更新まで待つ"""
        self.assertTrue(ftp.sendcmd("NOOP").startswith("200"))
        self._pump()

    def _server_has_data_channel(self):
        try:
            handlers = list(self.m._server.ioloop.socket_map.values())
        except RuntimeError:     # 待受スレッドが書き換えている最中
            return False
        return any(getattr(h, "data_channel", None) is not None for h in handlers)

    def _open_data(self, ftp):
        """PASV でデータ接続を先に張り、サーバーが受け入れるまで待つ（転送コマンドはまだ）"""
        host, port = ftp.makepasv()
        data = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(self._close_socket, data)
        data.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
        data.settimeout(10)
        data.connect((host, port))
        deadline = time.monotonic() + 5.0
        while not self._server_has_data_channel():
            self.assertLess(time.monotonic(), deadline,
                            "前提: サーバーがデータ接続を受け入れなかった")
            time.sleep(0.02)
        return data

    @staticmethod
    def _command(ftp, line):
        """応答を返す（4xx・5xx も例外にせず文字列で返す）"""
        try:
            return ftp.sendcmd(line)
        except ftplib.Error as err:
            return str(err)

    @staticmethod
    def _drain(data):
        got = 0
        while True:
            chunk = data.recv(1 << 20)
            if not chunk:
                break
            got += len(chunk)
        data.close()
        return got

    def _start_big_download(self, ftp):
        data = self._open_data(ftp)
        resp = ftp.sendcmd("RETR big.cfg")
        self.assertTrue(resp.startswith("125"), resp)
        # 最初の塊が届いた＝データ接続で big.cfg の転送が進んでいる
        first = data.recv(RCVBUF)
        self.assertTrue(first, "前提: big.cfg の転送が始まっていない")
        return data, len(first)

    def _finish_big_download(self, ftp, data, received, refused):
        self.assertFalse(refused.startswith("226"),
                         "前提が崩れた: 次のコマンドの前に big.cfg の転送が終わっていた")
        received += self._drain(data)
        self.assertEqual(received, BIG_SIZE)
        self.assertTrue(ftp.voidresp().startswith("226"))
        self._sync(ftp)
        self.assertEqual([e for e in self.events if e[0] != "progress"],
                         [("started", "big.cfg"), ("complete", "big.cfg")],
                         "転送中の取得が完了として閉じられていない")
        self.assertEqual(self.m._tx, {}, "開始のまま残った行がある")

    def test_a_retr_while_a_download_is_running_is_refused(self):
        ftp = self.client()
        data, received = self._start_big_download(ftp)
        refused = self._command(ftp, "RETR b.cfg")
        self.assertTrue(refused.startswith("425"),
                        "転送中のデータ接続で次の RETR を受け付けた: %s" % refused)
        self._finish_big_download(ftp, data, received, refused)
        self.assertIsNotNone(self._wait_activity("別の転送", "RETR /b.cfg"),
                             "断った理由がパネルのログに出ていない: %r" % self.activity)

    def test_a_list_while_a_download_is_running_is_refused(self):
        ftp = self.client()
        data, received = self._start_big_download(ftp)
        refused = self._command(ftp, "LIST")
        self.assertTrue(refused.startswith("425"),
                        "転送中のデータ接続で LIST を受け付けた: %s" % refused)
        self._finish_big_download(ftp, data, received, refused)

    def test_a_stor_while_an_upload_is_running_is_refused(self):
        ftp = self.client()
        data = self._open_data(ftp)
        resp = ftp.sendcmd("STOR x.cfg")
        self.assertTrue(resp.startswith("125"), resp)
        data.sendall(b"X" * 1000)
        # 進捗が出た＝データ接続で x.cfg を受けている
        deadline = time.monotonic() + 5.0
        while ("progress", "x.cfg") not in self.events:
            self.assertLess(time.monotonic(), deadline,
                            "前提: x.cfg の受信が始まっていない")
            self._pump(0.05)
        refused = self._command(ftp, "STOR y.cfg")
        self.assertTrue(refused.startswith("425"),
                        "受信中のデータ接続で次の STOR を受け付けた: %s" % refused)
        data.sendall(b"x" * 1000)
        data.close()
        self.assertTrue(ftp.voidresp().startswith("226"))
        self._sync(ftp)
        self.assertEqual(self.read("x.cfg"), b"X" * 1000 + b"x" * 1000,
                         "受けた中身が別のファイルへ分かれた")
        self.assertFalse(os.path.exists(self.real("y.cfg")),
                         "断った STOR の保存先が作られた")
        self.assertEqual([e for e in self.events if e[0] != "progress"],
                         [("started", "x.cfg"), ("complete", "x.cfg")])
        self.assertEqual(self.m._tx, {}, "開始のまま残った行がある")
        self.assertEqual(self.m._uploads, {}, "予約が残った")

    def test_a_waiting_data_connection_still_serves_a_retr(self):
        """対照: 張っただけで待っているデータ接続は、今までどおり 125 で使える"""
        ftp = self.client()
        data = self._open_data(ftp)
        resp = ftp.sendcmd("RETR b.cfg")
        self.assertTrue(resp.startswith("125"), resp)
        got = b""
        while True:
            chunk = data.recv(65536)
            if not chunk:
                break
            got += chunk
        data.close()
        self.assertEqual(got, B)
        self.assertTrue(ftp.voidresp().startswith("226"))
        self._sync(ftp)
        self.assertEqual([e for e in self.events if e[0] != "progress"],
                         [("started", "b.cfg"), ("complete", "b.cfg")])

    def test_a_waiting_data_connection_still_takes_a_stor(self):
        """対照: 張っただけで待っているデータ接続へ、今までどおり 125 で上げられる"""
        ftp = self.client()
        data = self._open_data(ftp)
        resp = ftp.sendcmd("STOR up.cfg")
        self.assertTrue(resp.startswith("125"), resp)
        data.sendall(b"UP" * 50)
        data.close()
        self.assertTrue(ftp.voidresp().startswith("226"))
        self._sync(ftp)
        self.assertEqual(self.read("up.cfg"), b"UP" * 50)
        self.assertEqual([e for e in self.events if e[0] != "progress"],
                         [("started", "up.cfg"), ("complete", "up.cfg")])

    # --- 向きの違う転送コマンド（4・5）
    KEEP = b"KEEP-CONF-" * 4

    def _seed_keep(self):
        with open(self.real("keep.cfg"), "wb") as seed:
            seed.write(self.KEEP)

    def _check_upload_refused_during_download(self, verb):
        self._seed_keep()
        ftp = self.client()
        data, received = self._start_big_download(ftp)
        refused = self._command(ftp, verb + " keep.cfg")
        self.assertTrue(refused.startswith("425"),
                        "送信中のデータ接続で %s を受け付けた: %s" % (verb, refused))
        self.assertEqual(self.read("keep.cfg"), self.KEEP,
                         "断った %s の保存先が書き換えられた" % verb)
        self._finish_big_download(ftp, data, received, refused)
        self.assertEqual(self.read("keep.cfg"), self.KEEP)
        self.assertEqual(self.m._uploads, {}, "予約が残った")
        self.assertIsNotNone(self._wait_activity("別の転送", verb + " /keep.cfg"),
                             "断った理由がパネルのログに出ていない: %r" % self.activity)

    def test_a_stor_while_a_download_is_running_is_refused(self):
        self._check_upload_refused_during_download("STOR")

    def test_an_appe_while_a_download_is_running_is_refused(self):
        self._check_upload_refused_during_download("APPE")

    def _check_download_refused_during_upload(self, line):
        self._seed_keep()
        ftp = self.client()
        data = self._open_data(ftp)
        resp = ftp.sendcmd("STOR up.cfg")
        self.assertTrue(resp.startswith("125"), resp)
        data.sendall(b"U" * 1000)
        # 進捗が出た＝データ接続で up.cfg を受けている
        deadline = time.monotonic() + 5.0
        while ("progress", "up.cfg") not in self.events:
            self.assertLess(time.monotonic(), deadline,
                            "前提: up.cfg の受信が始まっていない")
            self._pump(0.05)
        refused = self._command(ftp, line)
        self.assertTrue(refused.startswith("425"),
                        "受信中のデータ接続で %s を受け付けた: %s" % (line, refused))
        data.sendall(b"u" * 1000)
        data.close()
        self.assertTrue(ftp.voidresp().startswith("226"))
        self._sync(ftp)
        self.assertEqual(self.read("up.cfg"), b"U" * 1000 + b"u" * 1000,
                         "受けている途中のアップロードが欠けた")
        self.assertEqual(self.read("keep.cfg"), self.KEEP)
        self.assertEqual([e for e in self.events if e[0] != "progress"],
                         [("started", "up.cfg"), ("complete", "up.cfg")])
        self.assertEqual(self.m._tx, {}, "開始のまま残った行がある")
        self.assertEqual(self.m._uploads, {}, "予約が残った")

    def test_a_retr_while_an_upload_is_running_is_refused(self):
        self._check_download_refused_during_upload("RETR keep.cfg")

    def test_a_list_while_an_upload_is_running_is_refused(self):
        self._check_download_refused_during_upload("LIST")


if __name__ == "__main__":
    unittest.main()
