"""内蔵 SFTP サーバーが開いているファイルの全体の上限（2048 個）に達していても、
同じプロセスのほかの仕事が動くこと。

利用者の指示（2026-10-04）: 全体上限に達しても設定保存・ログ・他サーバーが
動くことを確かめる。

前提（実測）: 開いたファイルは 1 個ずつ C ランタイム（UCRT）の低水準ファイル
記述子を使う。その上限はプロセスで 8192 個（CRT のファイル入出力の上限で、
Windows のハンドル全体の上限ではない）。使い切ると、Python の open()（設定の
保存・ログ・FTP/TFTP のファイル）と名前解決（getaddrinfo。IP の文字列でも）が
EMFILE で失敗する。全体の上限 2048 は、残りを同じプロセスのほかの仕事へ残す
ための値。

ここでは本物の値（1 セッション 256 個 x 8 セッション = 2048 個）で上限まで開いた
まま、次を確かめる。
- 設定の保存（ConfigManager.save_config で実際にファイルが書ける）
- ログ（凍結ビルドの main._setup_logging と同じ経路: ログファイルを開いて
  sys.stdout / sys.stderr を差し替え、製品の print がそこへ書かれる）
- ほかのサーバー（127.0.0.1 の TFTP のアップロードとダウンロード、FTP の
  STOR と RETR、Syslog の UDP と TCP の受信）
- ほかの SFTP のクライアントの一覧と stat などは通り、新しい open だけが
  SFTP の失敗で断られる
- CRT の記述子の上限に余裕がある（使っている数を数え、さらに開いてみせる）
"""
import ftplib
import io
import json
import os
import socket
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"
# UCRT の低水準ファイル入出力の記述子の上限（Windows のハンドル全体の上限ではない）
UCRT_MAX_FDS = 8192
REPLY_TIMEOUT = 60.0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def used_fds():
    """このプロセスで使われている CRT の記述子の数（os.fstat が通る番号を数える）"""
    used = 0
    for fd in range(UCRT_MAX_FDS + 64):
        try:
            os.fstat(fd)
        except OSError:
            continue
        used += 1
    return used


def open_many(sftp, count, path="/a.txt"):
    """a.txt の読み取りの OPEN を応答を待たずに count 回送る。(ハンドル, 断られた数)"""
    from paramiko.sftp import CMD_OPEN, SFTP_FLAG_READ
    from paramiko.sftp_attr import SFTPAttributes
    nums = [sftp._async_request(type(None), CMD_OPEN, path, SFTP_FLAG_READ,
                                SFTPAttributes()) for _ in range(count)]
    handles, refused = [], 0
    for num in nums:
        try:
            _t, msg = sftp._read_response(num)
        except IOError:
            refused += 1
            continue
        handles.append(msg.get_binary())
    return handles, refused


class SftpFullServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        import paramiko
        cls.app = QApplication.instance() or QApplication([])
        cls.host_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        from core.config_manager import ConfigManager
        from core.sftp_server import SFTPServerManager
        self.work = tempfile.mkdtemp(prefix="netbelt-sftp-full-")
        data_dir = Path(self.work, "home")
        data_dir.mkdir()
        ap = mock.patch("core.config_manager.app_data_dir", return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        # ファイアウォールには触らない（起動では呼ばれないが、念のため止める）
        for name in ("ensure_inbound_allow", "ensure_self_program_allow"):
            p = mock.patch("core.firewall." + name, return_value=(True, "test stub"))
            p.start()
            self.addCleanup(p.stop)
        self.root = os.path.join(self.work, "sftp")
        os.mkdir(self.root)
        with open(os.path.join(self.root, "a.txt"), "wb") as f:
            f.write(b"abc")
        # 設定は起動のときに読み込む（上限に達するより前）
        self.config = ConfigManager(os.path.join(self.work, "config.json"))

        self.m = self._start_sftp()
        self.per_session = SFTPServerManager.MAX_OPEN_FILES_PER_SESSION
        self.total = SFTPServerManager.MAX_OPEN_FILES_TOTAL
        self.assertEqual((self.per_session, self.total), (256, 2048))
        # 2 接続 x 4 セッション x 256 個で、全体の上限まで開く
        self.holders = []
        for client in (self._connect(), self._connect()):
            for _ in range(self.total // self.per_session // 2):
                s = self._session(client)
                handles, refused = open_many(s, self.per_session)
                self.assertEqual((len(handles), refused), (self.per_session, 0))
                self.holders.append((s, handles))
        self.assertEqual(self.m._open_files._in_use, self.total)

    def _flush_qt(self):
        """配送待ちの通知を処理しておく"""
        for _ in range(3):
            self.app.processEvents()

    def _start_sftp(self):
        from core.sftp_server import SFTPServerManager
        m = SFTPServerManager()
        m.STOP_TIMEOUT_SECONDS = 1.0
        m.host_key = self.host_key
        self.addCleanup(self._flush_qt)
        self.addCleanup(m.stop)
        for _ in range(5):
            # 調べたポートを別のプロセスが先に取ることがある（排他の待受なので
            # start が失敗する）。そのときは別のポートで起動し直す
            self.port = free_port()
            if m.start(port=self.port, root_dir=self.root,
                       username=USER, password=PASSWORD):
                break
        else:
            self.fail("SFTP サーバーが起動しない")
        self.assertTrue(self._wait(lambda: m.is_running), "SFTP サーバーが起動しない")
        return m

    def _connect(self):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("127.0.0.1", port=self.port, username=USER, password=PASSWORD,
                  allow_agent=False, look_for_keys=False, timeout=10)
        self.addCleanup(c.close)
        return c

    def _session(self, client):
        s = client.open_sftp()
        s.get_channel().settimeout(REPLY_TIMEOUT)
        self.addCleanup(s.close)
        return s

    @staticmethod
    def _wait(predicate, seconds=10.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return bool(predicate())

    def _udp(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(10)
        self.addCleanup(s.close)
        return s

    def test_another_client_can_list_and_stat_but_a_new_open_is_refused(self):
        from paramiko.sftp import CMD_DATA, CMD_READ, int64
        s = self._session(self._connect())
        self.assertIn("a.txt", s.listdir("."))
        self.assertEqual(s.stat("a.txt").st_size, 3)
        self.assertEqual(s.lstat("a.txt").st_size, 3)
        s.mkdir("made")
        s.rename("made", "renamed")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "renamed")))
        # 新しい open だけが SFTP の失敗で断られる（無いファイルの ENOENT ではない）
        with self.assertRaises(IOError) as cm:
            s.open("a.txt", "rb")
        self.assertIsNone(cm.exception.errno, cm.exception)
        with self.assertRaises(IOError):
            s.open("new.bin", "wb")
        self.assertFalse(os.path.exists(os.path.join(self.root, "new.bin")),
                         "断った書き込みの open がファイルを作った")
        # 開いているハンドルは読める
        holder, handles = self.holders[0]
        t, msg = holder._request(CMD_READ, handles[0], int64(0), 3)
        self.assertEqual((t, msg.get_string()), (CMD_DATA, b"abc"))

    def test_settings_are_still_saved(self):
        self.assertTrue(self.config.set_server_settings(
            "tftp_server", {"port": 6969, "root_dir": "W1-full"}))
        with open(self.config.config_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["settings"]["tftp_server"]["root_dir"], "W1-full")
        # 読み込み直し（起動の経路）も通る
        from core.config_manager import ConfigManager
        again = ConfigManager(os.path.join(self.work, "config.json"))
        self.assertIsNone(again.load_error)
        self.assertEqual(again.get_server_settings("tftp_server")["port"], 6969)

    def test_the_log_is_still_written(self):
        """凍結ビルドのログ（main._setup_logging）が開け、製品の print が届くこと。"""
        import main
        import core.sftp_server as sftp_server
        saved = sys.stdout, sys.stderr
        local = os.path.join(self.work, "localappdata")
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.dict(os.environ, {"LOCALAPPDATA": local}):
            log_path = main._setup_logging()
        log_file = sys.stdout
        # 戻すのが先、閉じるのが後（後から登録した方が先に動く）
        if log_file is not saved[0]:
            self.addCleanup(log_file.close)
        self.addCleanup(lambda: (setattr(sys, "stdout", saved[0]),
                                 setattr(sys, "stderr", saved[1])))
        self.assertIsNotNone(log_path, "ログファイルを開けなかった")
        self.assertTrue(log_path.startswith(local), log_path)
        print("[W1] 全体の上限に達した状態のログ")
        sys.stderr.write("[W1] stderr line\n")
        # 製品の診断の行（SFTP のスレッドの print）。ほかのテストで同じ種類の
        # 行を使い切っていても出るよう、間引きの窓を新しくする
        with mock.patch.object(sftp_server, "_diag_log", sftp_server._LogLimiter()):
            s = self._session(self._connect())
            with self.assertRaises(IOError):
                s.open("a.txt", "rb")
            expected = "open refused, %d files already open (total)" % self.total

            def logged():
                with open(log_path, encoding="utf-8") as f:
                    return f.read()

            self.assertTrue(self._wait(lambda: expected in logged()),
                            "製品の行がログへ届かない: %r" % logged()[-500:])
        text = logged()
        self.assertIn("[W1] 全体の上限に達した状態のログ", text)
        self.assertIn("[W1] stderr line", text)

    def test_tftp_still_uploads_and_downloads(self):
        from core.tftp_server import TFTPServerManager
        root = os.path.join(self.work, "tftp")
        m = TFTPServerManager()
        self.addCleanup(self._flush_qt)
        self.addCleanup(m.stop)
        self.assertTrue(m.start(port=0, root_dir=root))
        port = m._srv.port
        # アップロード（WRQ、1 ブロック）
        payload = b"hostname R1\n"
        c = self._udp()
        c.sendto(b"\x00\x02up.cfg\x00octet\x00", ("127.0.0.1", port))
        ack, worker = c.recvfrom(1024)
        self.assertEqual(ack[:4], b"\x00\x04\x00\x00")
        c.sendto(b"\x00\x03\x00\x01" + payload, worker)
        ack, _ = c.recvfrom(1024)
        self.assertEqual(ack[:4], b"\x00\x04\x00\x01")
        target = os.path.join(root, "up.cfg")

        def uploaded():
            try:
                with open(target, "rb") as f:
                    return f.read() == payload
            except OSError:
                return False

        self.assertTrue(self._wait(uploaded), "TFTP のアップロードが書かれない")
        # ダウンロード（RRQ、512 + 488 の 2 ブロック）
        with open(os.path.join(root, "img.bin"), "wb") as f:
            f.write(b"X" * 1000)
        d = self._udp()
        d.sendto(b"\x00\x01img.bin\x00octet\x00", ("127.0.0.1", port))
        got, block = b"", 1
        while True:
            data, worker = d.recvfrom(2048)
            self.assertEqual(data[:4], b"\x00\x03" + struct.pack("!H", block))
            got += data[4:]
            d.sendto(b"\x00\x04" + data[2:4], worker)
            if len(data) - 4 < 512:
                break
            block += 1
        self.assertEqual(got, b"X" * 1000)

    def _start_ftp(self, root):
        """FTP を起動する。パッシブのポートは制御のポートと重ならないものにする。

        制御を port=0（OS が選ぶ）で待ち受け、パッシブの範囲に同じ番号が入ると、
        PASV の待受が 127.0.0.1 の同じ番号へ bind でき、制御の接続を横取りする
        （backlog_after_1_3_4.md の FTP の揺らぎ）。重なったら起動し直す
        """
        from core.ftp_server import FTPServerManager
        for _ in range(5):
            passive = free_port()
            m = FTPServerManager()
            if not m.start(port=0, root_dir=root, username="u", password="p",
                           passive_ports=(passive, passive)):
                continue
            if m.port != passive:
                self.addCleanup(self._flush_qt)
                self.addCleanup(m.stop)
                return m
            m.stop()
        self.fail("FTP サーバーが起動しない")

    def test_ftp_still_stores_and_retrieves(self):
        root = os.path.join(self.work, "ftp")
        m = self._start_ftp(root)
        f = ftplib.FTP()
        f.connect("127.0.0.1", m.port, timeout=10)
        self.addCleanup(f.close)
        f.login("u", "p")
        f.storbinary("STOR run.cfg", io.BytesIO(b"hostname R2\n"))
        buf = io.BytesIO()
        f.retrbinary("RETR run.cfg", buf.write)
        self.assertEqual(buf.getvalue(), b"hostname R2\n")
        with open(os.path.join(root, "run.cfg"), "rb") as fh:
            self.assertEqual(fh.read(), b"hostname R2\n")
        f.quit()

    def test_syslog_is_still_received(self):
        from PyQt6.QtCore import Qt
        from core.syslog_receiver import SyslogReceiver
        r = SyslogReceiver()
        got = []
        r.message_received.connect(lambda msg: got.append(msg.raw_message),
                                   Qt.ConnectionType.DirectConnection)
        self.addCleanup(self._flush_qt)
        self.addCleanup(r.stop)
        self.assertTrue(r.start_protocol("UDP", 0))
        self.assertTrue(r.start_protocol("TCP", 0))
        udp = self._udp()
        udp.sendto(b"<13>W1 full-udp", ("127.0.0.1", r._servers["UDP"]["port"]))
        tcp = socket.create_connection(("127.0.0.1", r._servers["TCP"]["port"]),
                                       timeout=10)
        self.addCleanup(tcp.close)
        tcp.sendall(b"<13>W1 full-tcp\n")
        self.assertTrue(self._wait(lambda: any("full-udp" in g for g in got)
                                   and any("full-tcp" in g for g in got)),
                        "Syslog を受け取れない: %r" % got)

    def test_the_crt_still_has_room_for_the_rest_of_the_process(self):
        used = used_fds()
        # 開いているハンドルは本当に記述子を使っている
        self.assertGreaterEqual(used, self.total)
        # 残りは CRT の上限の半分以上
        self.assertGreaterEqual(UCRT_MAX_FDS - used, UCRT_MAX_FDS // 2,
                                "使用中 %d 個" % used)
        # 実際にさらに 1024 個開ける（Python の open()）
        extra_dir = os.path.join(self.work, "extra")
        os.mkdir(extra_dir)
        files = []
        try:
            for i in range(1024):
                files.append(open(os.path.join(extra_dir, "f%d" % i), "wb"))
        finally:
            for f in files:
                f.close()
        self.assertEqual(len(files), 1024)


if __name__ == "__main__":
    unittest.main()
