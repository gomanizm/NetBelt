"""制御の待ち受けを排他にした内蔵 FTP を、LAN の IP 宛てと停止→即座の再起動で確かめる。

制御の待ち受けを排他（SO_EXCLUSIVEADDRUSE）にする修正の新しいテスト
（test_ftp_server_exclusive_control_port.py）は、127.0.0.1 だけを見る。採否を決める前に
確かめることにした次の点を、127.0.0.1 と、この PC の非ループバックの IPv4（以下 LAN の
IP。実行時に取得する。出力では <LAN-IP> と伏せる）の両方で確かめる。

  (a) 排他にした制御の待ち受けが、特定アドレス（127.0.0.1・LAN の IP）の同じ番号への
      bind を、オプションなしでも SO_REUSEADDR 付きでも断ること
  (b) 制御接続とデータ接続（PASV）で STOR・RETR ができること
  (c) STOR・RETR の転送の最中に停止し、同じ番号で即座に起動し直せること。クライアントが
      閉じてから起動する形と、閉じる前に起動する形の両方
  (d) 停止の後もクライアントが接続を閉じないとき（ログインしたまま／応答を読まずに
      サーバーの送信を詰まらせたまま）も、即座に起動し直せること

(c)(d) は、Microsoft の文書（Using SO_REUSEADDR and SO_EXCLUSIVEADDRUSE）が排他の
ソケットについて注意する「閉じた直後には同じ番号を使えないことがある」が起きるかを見る。
手元（Windows 11）の 127.0.0.1 では起きなかった。起きたら、そのときの TCP の状態と、
何秒後に起動できたかを添えて落とす。待つのは 1 件 RESTART_WAIT_PER_CASE 秒、この
ファイル全体で RESTART_WAIT_TOTAL 秒まで（全部の件で待っても CI の上限に収まるように）。

(a) は修正の前の形では落ちる（同じユーザーのソケットの bind が通る）。(b)(c)(d) は修正の
前の形でも通る（修正で変わっていないことを確かめる）。
"""
import ftplib
import functools
import ipaddress
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"
LAN_LABEL = "<LAN-IP>"

# RETR で送るファイルの大きさ。クライアントは受信バッファを READ_CHUNK にし、PACE 秒に
# READ_CHUNK ずつしか読まない（毎秒 410 KB 未満）ので、送り終えるまで 10 秒近くかかる
RETR_SIZE = 4 * 1024 * 1024
READ_CHUNK = 4096
# STOR は PACE 秒に SEND_CHUNK ずつ送る。STOR_LIMIT まで送ったら、閉じずに待つ
STOR_LIMIT = 2 * 1024 * 1024
SEND_CHUNK = 16384
PACE = 0.01
# 停止するのは、転送した量（サーバー側の数え方）がこれを超えてから。手元では、RETR で
# クライアントが読まないとき、サーバーが送り出せたのは 128 KiB で頭打ちだった
IN_FLIGHT = 192 * 1024

# 即座の起動が断られたときに待つ上限（1 件あたり／このファイル全体）
RESTART_WAIT_PER_CASE = 260.0
RESTART_WAIT_TOTAL = 300.0
_wait_left = [RESTART_WAIT_TOTAL]


def _lan_ipv4():
    """この PC の非ループバックの IPv4 を返す。取れなければ (None, 理由)。

    UDP のソケットを 192.0.2.1（RFC 5737 の文書用のアドレス）へ connect し、OS が経路から
    選んだ自分側のアドレスを読む。UDP の connect は宛先を覚えるだけで、何も送らない
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.connect(("192.0.2.1", 9))
            ip = udp.getsockname()[0]
    except OSError as e:
        return None, "既定の経路が無い（errno %s）" % e.errno
    addr = ipaddress.ip_address(ip)
    if addr.is_loopback or addr.is_unspecified:
        return None, "非ループバックのアドレスが選ばれなかった"
    return ip, None


def _unreachable(ip):
    """この PC 自身のアドレス ip へ、同じ PC の中で TCP の接続を張れなければ理由を返す"""
    try:
        with socket.socket() as listener:
            listener.settimeout(10)
            listener.bind(("0.0.0.0", 0))
            listener.listen(1)
            with socket.create_connection((ip, listener.getsockname()[1]),
                                          timeout=10):
                conn, _ = listener.accept()
                conn.close()
    except OSError as e:
        return "errno %s" % e.errno
    return None


def _tcp_states(port):
    """自分側の番号が port の TCP の状態を数えて返す（netstat を読むだけ。アドレスは出さない）"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as e:
        return "netstat を読めなかった（%s）" % e.__class__.__name__
    counts = {}
    for line in out.decode("ascii", "replace").splitlines():
        parts = line.split()
        if (len(parts) >= 4 and parts[0] == "TCP"
                and parts[1].endswith(":%d" % port)):
            counts[parts[3]] = counts.get(parts[3], 0) + 1
    return ", ".join("%s×%d" % kv for kv in sorted(counts.items())) or "なし"


def _quiet_close(obj):
    try:
        obj.close()
    except (OSError, EOFError):
        pass


class _Reader(threading.Thread):
    """データ接続を PACE 秒に READ_CHUNK ずつ読む。finish() の後は急いで最後まで読む"""

    def __init__(self, sock):
        super().__init__(daemon=True)
        self.sock = sock
        self.error = None
        self.hurry = threading.Event()

    def run(self):
        try:
            while self.sock.recv(READ_CHUNK):
                if not self.hurry.is_set():
                    time.sleep(PACE)
        except OSError as e:
            self.error = e

    def finish(self):
        self.hurry.set()
        self.join(15)
        if self.is_alive():
            _quiet_close(self.sock)
            self.join(15)


class _Sender(threading.Thread):
    """データ接続へ PACE 秒に SEND_CHUNK ずつ送る（STOR_LIMIT まで）。送れなくなったら抜ける"""

    def __init__(self, sock):
        super().__init__(daemon=True)
        self.sock = sock
        self.sent = 0
        self.error = None
        self.halt = threading.Event()

    def run(self):
        chunk = b"s" * SEND_CHUNK
        try:
            while not self.halt.is_set():
                if self.sent < STOR_LIMIT:
                    self.sock.sendall(chunk)
                    self.sent += len(chunk)
                self.halt.wait(PACE)
        except OSError as e:
            self.error = e

    def finish(self):
        self.halt.set()
        self.join(15)
        if self.is_alive():
            _quiet_close(self.sock)
            self.join(15)


class _MaskLanIp(logging.Filter):
    """pyftpdlib のログ（相手のアドレスを含む）の LAN の IP を伏せる"""

    def __init__(self, ip):
        super().__init__()
        self.ip = ip

    def filter(self, record):
        text = record.getMessage()
        if self.ip in text:
            record.msg, record.args = text.replace(self.ip, LAN_LABEL), None
        return True


def _masked(test):
    """テストの例外の文言に LAN の IP が入っていたら、伏せた文言の失敗に置き換える"""
    @functools.wraps(test)
    def wrapper(self):
        try:
            return test(self)
        except unittest.SkipTest:
            raise
        except Exception as e:
            text = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            if self.host != self.label and self.host in text:
                raise self.failureException(
                    "元の例外（%s を伏せた）:\n%s" % (self.label, self._mask(text))
                ) from None
            raise
    return wrapper


class _Cases:
    """127.0.0.1 と LAN の IP で同じ確かめを流すための中身（host と label は子が決める）"""
    host = None
    label = None

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
        if self.host != self.label:
            log_filter = _MaskLanIp(self.host)
            logging.getLogger("pyftpdlib").addFilter(log_filter)
            self.addCleanup(logging.getLogger("pyftpdlib").removeFilter, log_filter)
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-excl-restart-")
        from core.ftp_server import FTPServerManager
        self.m = FTPServerManager()
        # 後始末は逆順に走る: 知らせの口を外す → 停止 → 残った配送を処理
        self.addCleanup(self.app.processEvents)
        self.addCleanup(self.m.stop)
        self.events = []
        self.errors = []
        for signal, slot in (
                (self.m.transfer_complete,
                 lambda *a: self.events.append(("complete", a[1], a[-1]))),
                (self.m.transfer_interrupted,
                 lambda *a: self.events.append(("interrupted", a[1], a[-1]))),
                (self.m.error_occurred, self.errors.append)):
            signal.connect(slot)
            self.addCleanup(signal.disconnect, slot)

    def _mask(self, text):
        return text.replace(self.host, self.label) if self.host != self.label else text

    def _start(self, port=0):
        started = self.m.start(port=port, root_dir=self.root,
                               username=USER, password=PASSWORD)
        self.assertTrue(started, self._mask("起動できない: %s" % self.errors))
        return self.m.port

    def _client(self):
        ftp = ftplib.FTP()
        self.addCleanup(_quiet_close, ftp)
        ftp.connect(self.host, self.m.port, timeout=30)
        ftp.login(USER, PASSWORD)
        ftp.voidcmd("TYPE I")
        return ftp

    def _data_socket(self, ftp, rcvbuf=None):
        """PASV を張り、そこへ繋いだデータ接続を返す。返答のアドレスが接続先と同じことも見る"""
        host, port = ftplib.parse227(ftp.sendcmd("PASV"))
        self.assertTrue(host == self.host,
                        "PASV の返答のアドレスが %s でない" % self.label)
        sock = socket.socket()
        self.addCleanup(_quiet_close, sock)
        if rcvbuf:
            # 小さくすると、サーバーが送り切る前に停止が届く（読まなければ送れない）
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        sock.settimeout(30)
        sock.connect((self.host, port))
        self.assertTrue(sock.getpeername()[0] == self.host,
                        "データ接続の相手が %s でない" % self.label)
        return sock

    def _server_handlers(self, server):
        try:
            return list(server.ioloop.socket_map.values())
        except RuntimeError:   # 待受スレッドが一覧を書き換えている最中
            return []

    def _wait_in_flight(self, direction):
        """サーバー側のデータ接続が IN_FLIGHT を超えて転送している最中になるまで待つ"""
        from pyftpdlib.handlers import DTPHandler
        server = self.m._server
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for h in self._server_handlers(server):
                if (isinstance(h, DTPHandler)
                        and h.receive == (direction == "upload")
                        and h.get_transmitted_bytes() >= IN_FLIGHT):
                    return
            time.sleep(0.01)
        self.fail("前提: %s の転送が %d バイトを超えない" % (direction, IN_FLIGHT))

    def _restart(self, port):
        """同じ番号で即座に起動し直す。断られたら、起動できるまでの秒数を測ってから落とす"""
        if self.m.start(port=port, root_dir=self.root,
                        username=USER, password=PASSWORD):
            self.assertEqual(self.m.port, port)
            return
        first = self.errors[-1] if self.errors else "理由の知らせなし"
        states = _tcp_states(port)
        limit = min(RESTART_WAIT_PER_CASE, max(0.0, _wait_left[0]))
        began = time.monotonic()
        started = False
        while not started and time.monotonic() - began < limit:
            time.sleep(0.5)
            started = self.m.start(port=port, root_dir=self.root,
                                   username=USER, password=PASSWORD)
        waited = time.monotonic() - began
        _wait_left[0] -= waited
        detail = ("%s 宛て: 停止の直後に同じ %d 番で起動できなかった（失敗の知らせ: %s"
                  "／そのときの %d 番の TCP の状態: %s）"
                  % (self.label, port, first, port, states))
        if started:
            self.fail(self._mask("%s。%.1f 秒後に起動できた" % (detail, waited)))
        self.fail(self._mask(
            "%s。%.1f 秒待っても起動できなかった（待ちの上限 %.0f 秒。このファイル"
            "全体の待ちの残り %.0f 秒）" % (detail, waited, limit,
                                            max(0.0, _wait_left[0]))))

    def _assert_serving(self, port):
        """起動し直したサーバーへ、同じ宛先でログインして取得できること"""
        self.assertEqual(self.m.port, port)
        with open(os.path.join(self.root, "hello.txt"), "wb") as f:
            f.write(b"hello after restart")
        got = []
        reply = self._client().retrbinary("RETR hello.txt", got.append)
        self.assertTrue(reply.startswith("226"), reply)
        self.assertEqual(b"".join(got), b"hello after restart")

    def _assert_cut_short(self, name, direction):
        """前提: 停止の時点で転送は終わっておらず、中断として記録されたこと"""
        event = ("interrupted", name, direction)
        deadline = time.monotonic() + 10
        while event not in self.events and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        self.assertIn(event, self.events,
                      "前提: 停止で転送が中断として記録されていない")
        self.assertNotIn(("complete", name, direction), self.events,
                         "前提: 停止の前に転送が終わっていた")

    def _restart_during(self, direction, client_closes_first):
        port = self._start()
        ftp = self._client()
        if direction == "download":
            name = "big.bin"
            with open(os.path.join(self.root, name), "wb") as f:
                f.write(b"r" * RETR_SIZE)
            data = self._data_socket(ftp, rcvbuf=READ_CHUNK)
            self.assertTrue(ftp.sendcmd("RETR " + name).startswith("1"))
            worker = _Reader(data)
        else:
            name = "up.bin"
            data = self._data_socket(ftp)
            self.assertTrue(ftp.sendcmd("STOR " + name).startswith("1"))
            worker = _Sender(data)
        worker.start()
        self.addCleanup(worker.finish)
        self._wait_in_flight(direction)
        self.m.stop()
        if client_closes_first:
            # よく振る舞うクライアント: 切れたのに気づいたら閉じる
            worker.finish()
            data.close()
            ftp.close()
        self._restart(port)
        self._assert_cut_short(name, direction)
        self._assert_serving(port)

    # (a)
    @_masked
    def test_a_specific_address_bind_on_the_control_port_is_refused(self):
        port = self._start()
        for reuse in (False, True):
            # subTest ごとに閉じる。残すと、次の subTest がそれに断られて通ってしまう
            with self.subTest(SO_REUSEADDR=reuse), socket.socket() as other:
                if reuse:
                    other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with self.assertRaises(OSError, msg="制御ポートへ %s で bind できた"
                                       % self.label):
                    other.bind((self.host, port))
                    other.listen(1)
        ftp = ftplib.FTP()
        self.addCleanup(_quiet_close, ftp)
        ftp.connect(self.host, port, timeout=30)
        self.assertTrue(ftp.getwelcome().startswith("220"))

    # (b)
    @_masked
    def test_stor_and_retr_through_pasv(self):
        self._start()
        ftp = self._client()
        payload = bytes(range(256)) * 4096
        data = self._data_socket(ftp)
        self.assertTrue(ftp.sendcmd("STOR pasv.bin").startswith("1"))
        data.sendall(payload)
        data.close()
        self.assertTrue(ftp.voidresp().startswith("226"))
        with open(os.path.join(self.root, "pasv.bin"), "rb") as f:
            self.assertTrue(f.read() == payload, "STOR の中身が違う")
        data = self._data_socket(ftp)
        self.assertTrue(ftp.sendcmd("RETR pasv.bin").startswith("1"))
        got = []
        while True:
            chunk = data.recv(65536)
            if not chunk:
                break
            got.append(chunk)
        data.close()
        self.assertTrue(ftp.voidresp().startswith("226"))
        self.assertTrue(b"".join(got) == payload, "RETR の中身が違う")

    # (c)
    @_masked
    def test_restart_during_stor_after_the_client_closes(self):
        self._restart_during("upload", client_closes_first=True)

    @_masked
    def test_restart_during_stor_while_the_client_is_still_open(self):
        self._restart_during("upload", client_closes_first=False)

    @_masked
    def test_restart_during_retr_after_the_client_closes(self):
        self._restart_during("download", client_closes_first=True)

    @_masked
    def test_restart_during_retr_while_the_client_is_still_open(self):
        self._restart_during("download", client_closes_first=False)

    # (d)
    @_masked
    def test_restart_while_an_idle_client_keeps_the_connection(self):
        port = self._start()
        old = self._client()   # ログインしたまま、停止の後も閉じない
        old.voidcmd("NOOP")
        self.m.stop()
        self._restart(port)
        self._assert_serving(port)

    @_masked
    def test_restart_while_a_client_stalls_the_control_connection(self):
        """応答を読まずにコマンドを送り続け、サーバーの送信が詰まった制御接続を残す。

        閉じるときに送り残しがある接続は、Microsoft の文書が「すぐには解放されない
        ことがある」とする形（相手が受信窓 0 のまま）。停止の後も閉じない
        """
        port = self._start()
        sock = socket.socket()
        self.addCleanup(_quiet_close, sock)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        sock.settimeout(30)
        sock.connect((self.host, port))
        reader = sock.makefile("rb")
        self.addCleanup(_quiet_close, reader)
        self.assertTrue(reader.readline().startswith(b"220"))
        sock.sendall(("USER %s\r\n" % USER).encode())
        self.assertTrue(reader.readline().startswith(b"331"))
        sock.sendall(("PASS %s\r\n" % PASSWORD).encode())
        self.assertTrue(reader.readline().startswith(b"230"))
        self._stall(sock)
        self.m.stop()
        self._restart(port)
        self._assert_serving(port)

    def _stall(self, sock):
        """sock の制御接続で、サーバーの送り残し（producer_fifo）が溜まったままになるまで NOOP を送る"""
        from pyftpdlib.handlers import FTPHandler
        me = sock.getsockname()[1]
        handler = None
        deadline = time.monotonic() + 10
        while handler is None and time.monotonic() < deadline:
            handler = next((h for h in self._server_handlers(self.m._server)
                            if isinstance(h, FTPHandler)
                            and getattr(h, "remote_port", None) == me), None)
            time.sleep(0.01)
        self.assertIsNotNone(handler, "前提: サーバー側の制御接続が見つからない")
        sock.settimeout(5)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                sock.sendall(b"NOOP\r\n" * 500)
            except socket.timeout:
                pass   # サーバーが読むのを止めた（これも詰まった形）
            if handler.producer_fifo:
                time.sleep(0.3)
                if handler.producer_fifo:
                    return
        self.skipTest("前提を作れなかった: 60 秒送ってもサーバーの送信が詰まらない")


@unittest.skipUnless(sys.platform == "win32", "Windows の bind の規則に依る")
class FtpExclusiveControlLoopbackTest(_Cases, unittest.TestCase):
    host = label = "127.0.0.1"


@unittest.skipUnless(sys.platform == "win32", "Windows の bind の規則に依る")
class FtpExclusiveControlLanTest(_Cases, unittest.TestCase):
    label = LAN_LABEL

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ip, why = _lan_ipv4()
        if ip is None:
            raise unittest.SkipTest("この PC の非ループバックの IPv4 を取れない: %s" % why)
        why = _unreachable(ip)
        if why:
            raise unittest.SkipTest("この PC の %s へ繋げない: %s" % (LAN_LABEL, why))
        cls.host = ip


if __name__ == "__main__":
    unittest.main()
