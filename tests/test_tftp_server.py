import os, socket, struct, tempfile, time, unittest, sys
import unittest.mock
sys.path.insert(0, "src")
from core.tftp_server import TFTPServer  # 低レベルサーバ（QObject非依存）

def _wrq(filename, mode=b"octet"):
    return b"\x00\x02" + filename.encode() + b"\x00" + mode + b"\x00"

class TftpWrqTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.srv = TFTPServer(port=0, root_dir=self.root)  # port=0 で任意空きポート
        self.srv.start()
        self.port = self.srv.port
    def tearDown(self):
        self.srv.stop()
    def test_wrq_upload_small_file(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        c.sendto(_wrq("cfg.txt"), ("127.0.0.1", self.port))
        # ACK block0 を受ける
        data, srvaddr = c.recvfrom(1024)
        self.assertEqual(data[:2], b"\x00\x04")           # OACK/ACK
        self.assertEqual(struct.unpack("!H", data[2:4])[0], 0)
        payload = b"hello tftp upload"
        c.sendto(b"\x00\x03" + struct.pack("!H", 1) + payload, srvaddr)  # DATA block1（<512で最終）
        ack, _ = c.recvfrom(1024)
        self.assertEqual(ack[:2], b"\x00\x04")
        self.assertEqual(struct.unpack("!H", ack[2:4])[0], 1)
        time.sleep(0.2)
        with open(os.path.join(self.root, "cfg.txt"), "rb") as f:
            self.assertEqual(f.read(), payload)
    def test_rrq_download(self):
        with open(os.path.join(self.root, "img.bin"), "wb") as f:
            f.write(b"X" * 1000)  # 512x1 + 488 → 2 データブロック
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        c.sendto(b"\x00\x01img.bin\x00octet\x00", ("127.0.0.1", self.port))
        got = b""
        expect = 1
        while True:
            data, srvaddr = c.recvfrom(2048)
            self.assertEqual(data[:2], b"\x00\x03")           # DATA
            blk = struct.unpack("!H", data[2:4])[0]
            self.assertEqual(blk, expect)
            got += data[4:]
            c.sendto(b"\x00\x04" + struct.pack("!H", blk), srvaddr)  # ACK
            if len(data[4:]) < 512:
                break
            expect += 1
        self.assertEqual(got, b"X" * 1000)
    def test_rrq_download_with_blksize_option(self):
        content = b"Y" * 1500  # blksize=1024 → 1024 + 476 の2データブロック
        with open(os.path.join(self.root, "big.bin"), "wb") as f:
            f.write(content)
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(c.close)
        c.settimeout(3)
        req = (b"\x00\x01big.bin\x00octet\x00"
               b"blksize\x001024\x00"
               b"tsize\x000\x00")
        c.sendto(req, ("127.0.0.1", self.port))
        # OACK でオプションのネゴシエート結果を受ける
        data, srvaddr = c.recvfrom(2048)
        self.assertEqual(data[:2], b"\x00\x06")            # OACK
        parts = data[2:].split(b"\x00")
        opts = dict(zip(parts[0::2], parts[1::2]))
        self.assertEqual(opts[b"blksize"], b"1024")
        self.assertEqual(opts[b"tsize"], b"1500")          # 実ファイルサイズに置換される
        c.sendto(b"\x00\x04\x00\x00", srvaddr)             # ACK block0 で転送開始
        got = b""
        expect = 1
        while True:
            data, srvaddr = c.recvfrom(2048)
            self.assertEqual(data[:2], b"\x00\x03")          # DATA
            blk = struct.unpack("!H", data[2:4])[0]
            self.assertEqual(blk, expect)
            got += data[4:]
            c.sendto(b"\x00\x04" + struct.pack("!H", blk), srvaddr)  # ACK
            if len(data[4:]) < 1024:
                break
            expect += 1
        self.assertEqual(got, content)
    def test_rrq_emits_progress_with_total(self):
        payload = os.path.join(self.root, "prog.bin")
        with open(payload, "wb") as f:
            f.write(b"Y" * 1500)  # 512x2 + 476 = 3 blocks
        events = []
        srv = TFTPServer(port=0, root_dir=self.root, on_event=lambda k, ip, p: events.append((k, p)))
        srv.start()
        try:
            c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); c.settimeout(3)
            self.addCleanup(c.close)
            c.sendto(b"\x00\x01prog.bin\x00octet\x00", ("127.0.0.1", srv.port))
            got = b""; expect = 1
            while True:
                data, srvaddr = c.recvfrom(2048)
                self.assertEqual(data[:2], b"\x00\x03")
                got += data[4:]
                c.sendto(b"\x00\x04" + struct.pack("!H", expect), srvaddr)
                if len(data[4:]) < 512: break
                expect += 1
            time.sleep(0.2)
        finally:
            srv.stop()
        starts = [p for k, p in events if k == "transfer_started"]
        comps = [p for k, p in events if k == "transfer_complete"]
        self.assertTrue(starts and starts[0][1] == 1500)          # total=1500
        self.assertTrue(comps and comps[0][1] == 1500)             # done=1500
        self.assertEqual(comps[0][3], "download")
    def test_manager_start_stop(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from core.tftp_server import TFTPServerManager
        m = TFTPServerManager()
        with unittest.mock.patch("core.firewall.ensure_inbound_allow", return_value=(True, "test stub")):
            self.assertTrue(m.start(port=0, root_dir=self.root))
            self.assertTrue(m.is_running)
            m.stop()
            self.assertFalse(m.is_running)

    def test_send_and_wait_ack_retransmits_on_timeout(self):
        import struct as _s
        from unittest import mock
        srv = TFTPServer(port=0, root_dir=self.root)
        xs = mock.Mock()
        addr = ("127.0.0.1", 12345)
        ack = _s.pack("!HH", 4, 7)  # OP_ACK=4, block 7
        # first recvfrom times out (forces a resend), second returns the correct ACK
        xs.recvfrom.side_effect = [socket.timeout(), (ack, addr)]
        packet = _s.pack("!HH", 3, 7) + b"data"  # OP_DATA=3, block 7
        srv._send_and_wait_ack(xs, packet, addr, 7)   # must return (not raise)
        self.assertEqual(xs.sendto.call_count, 2)     # original send + 1 retransmit
        for c in xs.sendto.call_args_list:
            self.assertEqual(c.args[0], packet)       # same bytes retransmitted (no corruption)

    def test_send_and_wait_ack_raises_timeout_on_total_loss(self):
        import struct as _s
        from unittest import mock
        srv = TFTPServer(port=0, root_dir=self.root)
        xs = mock.Mock()
        addr = ("127.0.0.1", 12345)
        xs.recvfrom.side_effect = socket.timeout
        packet = _s.pack("!HH", 3, 7) + b"data"
        self.assertRaises(socket.timeout, srv._send_and_wait_ack, xs, packet, addr, 7)

    def test_rrq_orphan_no_options_is_silent(self):
        # 重複RRQの敗者スレッド(オプション無し): block1のACKが来ないと started も error も出さず撤退。
        from unittest import mock
        fn = "orphan.bin"
        with open(os.path.join(self.root, fn), "wb") as fh:
            fh.write(b"x" * 2000)
        events = []
        srv = TFTPServer(port=0, root_dir=self.root, on_event=lambda k, ip, p: events.append(k))
        srv._send_and_wait_ack = mock.Mock(side_effect=socket.timeout)  # ACK が一切来ない
        rrq = struct.pack("!H", 1) + fn.encode() + bytes([0]) + b"octet" + bytes([0])  # OP_RRQ, オプション無し
        srv._handle_rrq(rrq, ("127.0.0.1", 55555))
        self.assertNotIn("transfer_started", events)  # 未確立なので履歴に載せない
        self.assertNotIn("error", events)              # 偽の RRQ timeout も出さない

    def test_rrq_orphan_oack_timeout_is_silent(self):
        # 重複RRQの敗者スレッド(オプション付き): OACKにACKが来ない場合も黙って撤退する。
        from unittest import mock
        fn = "orphan2.bin"
        with open(os.path.join(self.root, fn), "wb") as fh:
            fh.write(b"y" * 100)
        events = []
        srv = TFTPServer(port=0, root_dir=self.root, on_event=lambda k, ip, p: events.append(k))
        srv._send_and_wait_ack = mock.Mock(side_effect=socket.timeout)
        rrq = struct.pack("!H", 1) + fn.encode() + bytes([0]) + b"octet" + bytes([0]) + b"blksize" + bytes([0]) + b"512" + bytes([0])
        srv._handle_rrq(rrq, ("127.0.0.1", 55556))
        self.assertNotIn("transfer_started", events)
        self.assertNotIn("error", events)

    def test_manager_coalesces_duplicate_rrq_events(self):
        # 機器が別ポートで4回RRQ再送し4本が確立→勝者completeで敗者3つがtimeoutする実測パターン。
        # UIには started 1行・complete 1回のみ、偽 timeout は0件になること。
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from core.tftp_server import TFTPServerManager
        m = TFTPServerManager()
        started, completed, errors = [], [], []
        m.transfer_started.connect(lambda ip, fn, t, d: started.append((ip, fn)))
        m.transfer_complete.connect(lambda ip, fn, dn, t, d: completed.append((ip, fn)))
        # 転送ごとの事象は protocol_event へ移った（サーバ障害用の
        # error_occurred とは別の口。モーダルを出さないため）
        m.protocol_event.connect(
            lambda ip, fn, reason, d: errors.append((ip, fn, reason, d)))
        ip, fn = "192.0.2.31", "itch-setup.exe"
        for _ in range(4):
            m._on_event("transfer_started", ip, (fn, 100, "download"))
        m._on_event("transfer_complete", ip, (fn, 100, 100, "download"))
        for _ in range(3):
            m._on_event("protocol_error", ip,
                        (fn, "ダウンロードがタイムアウト", "download"))
        app.processEvents()
        self.assertEqual(len(started), 1)    # 4回の started を1行にコアレス
        self.assertEqual(len(completed), 1)  # 完了は1回だけ通す
        self.assertEqual(errors, [])         # 偽の RRQ timeout は全て握り潰す

    def test_manager_forwards_genuine_total_failure(self):
        # 全スレッドが完了せず timeout（真の失敗）なら、最後の1件は本物のエラーとして見せる。
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        from core.tftp_server import TFTPServerManager
        m = TFTPServerManager()
        started, errors = [], []
        m.transfer_started.connect(lambda ip, fn, t, d: started.append((ip, fn)))
        m.protocol_event.connect(
            lambda ip, fn, reason, d: errors.append((ip, fn, reason, d)))
        ip, fn = "192.0.2.31", "x.bin"
        for _ in range(2):
            m._on_event("transfer_started", ip, (fn, 100, "download"))
        for _ in range(2):
            m._on_event("protocol_error", ip,
                        (fn, "ダウンロードがタイムアウト", "download"))
        app.processEvents()
        self.assertEqual(len(started), 1)
        self.assertEqual(len(errors), 1)     # 全滅なら本物の失敗を1件通す

    def test_manager_start_does_not_touch_firewall(self):
        # 3CDaemon 方式: 起動時に FW 自動設定（UAC 昇格経路）を呼ばないこと。
        from unittest import mock
        from PyQt6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        import core.firewall as fw
        from core.tftp_server import TFTPServerManager
        with mock.patch.object(fw, "ensure_self_program_allow") as m_self:
            with mock.patch.object(fw, "ensure_inbound_allow") as m_in:
                m = TFTPServerManager()
                try:
                    self.assertTrue(m.start(port=0, root_dir=self.root,
                                            allow_upload=True, allow_download=True))
                    m_self.assert_not_called()   # 自exe許可の昇格を呼ばない
                    m_in.assert_not_called()     # ポート許可の昇格も呼ばない
                finally:
                    m.stop()

    def test_fix_firewall_invokes_helpers(self):
        # 手動FW許可ボタンの中身: ensure_inbound_allow + ensure_self_program_allow を呼ぶこと。
        from unittest import mock
        from PyQt6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        import core.firewall as fw
        from core.tftp_server import TFTPServerManager
        with mock.patch.object(fw, "ensure_inbound_allow", return_value=(True, "ok")) as m_in:
            with mock.patch.object(fw, "ensure_self_program_allow", return_value=(True, "ok")) as m_self:
                m = TFTPServerManager()
                ok, _ = m.fix_firewall(69)
                self.assertTrue(ok)
                m_in.assert_called_once()      # ポート許可
                m_self.assert_called_once()    # 自exe許可

if __name__ == "__main__":
    unittest.main()
