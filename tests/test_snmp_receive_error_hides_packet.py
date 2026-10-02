"""SNMP の受信処理で pysnmp が例外を出しても、受信したパケットの中身を出力しないことの検証。

何が起きていたか（pysnmp 5.1.0 から 7.1.28 へ移したときに実測）
  7.1.28 は asyncio のイベントループの上で受信する。受信のコールバックで
  例外が出ると、asyncio の既定の例外ハンドラは、そのコールバックの引数
  （受信したパケットのバイト列と送信元のアドレス）を省略つきで表示する。
  v1/v2c のパケットにはコミュニティが平文で入っている。BER として読めない
  パケットで pysnmp の復号が例外を出し、先頭のバイト列がそのまま stderr に
  出た。凍結ビルドでは stdout と stderr をログファイルへ恒久保存する
  （src/main.py の _setup_logging）。
どう直したか
  NetBelt が作るイベントループ（snmp_manager._new_event_loop。GET/WALK と
  Trap の受信の両方）に例外ハンドラを付け、例外の型名と出た場所（ファイル名と
  行番号）だけを出す。
  ここでは、先頭が目印のバイト列のパケットを Trap の受信機へ送ったときと、
  そうしたパケットを応答として返す相手へ GET / WALK したときに、出力に
  目印が出ないこと、例外の経路を実際に通ったこと、Trap はそのあとも受信を
  続けることを見る。通信は 127.0.0.1 だけ。
"""
import contextlib
import io
import logging
import os
import socket
import sys
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, "src")
from conftest import free_udp_port, trap_bytes   # noqa: E402

# 目印。先頭が b"hello " のパケットは、pysnmp 7.1.28 の復号が例外
# （TypeError）を出す（実測）。asyncio の表示は、バイト列の先頭から十数文字を
# 残して途中を省く（実測で b'hello LEAKM...' の形）ので、目印はそのすぐ後ろに置く
PREFIX = b"hello "
SECRET = b"NBSECRET42"
QUIET_LINE = "[SNMP] イベントループの処理で例外を捨てました"


class _GarbageResponder:
    """届いた要求すべてに、目印入りの壊れたパケットを返す相手（壊れた機器の代わり）"""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.replies = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="snmp-garbage-responder")
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                _data, address = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            self._sock.sendto(PREFIX + SECRET + b" broken reply", address)
            self.replies += 1

    def close(self):
        self._stop.set()
        self._thread.join(5)
        self._sock.close()


class ReceiveErrorHidesPacketTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        # 作った SNMPManager はクラス終了まで保持する（キューに残ったシグナルの
        # 配送先を先に捨てると落ちる。test_snmp_trap_receive.py と同じ理由）
        cls._managers = []

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtWidgets import QApplication
        for manager in cls._managers:
            manager.stop_trap_receiver()
        QApplication.processEvents()
        cls._managers.clear()

    def setUp(self):
        fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                 return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)

    def _pump_until(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.01)
        self.app.processEvents()
        return condition()

    def test_a_packet_that_breaks_the_decoder_is_not_printed(self):
        from core.snmp_manager import SNMPManager
        manager = SNMPManager()
        type(self)._managers.append(manager)
        traps = []
        manager.trap_received.connect(traps.append)
        port = free_udp_port()
        output = io.StringIO()
        # asyncio の既定の例外ハンドラは logging の "asyncio" へ出す。pytest は
        # logging を自分の受け皿へ回すので、stderr を見るだけでは拾えない
        handler = logging.StreamHandler(output)
        asyncio_logger = logging.getLogger("asyncio")
        asyncio_logger.addHandler(handler)
        self.addCleanup(asyncio_logger.removeHandler, handler)
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.addCleanup(manager.stop_trap_receiver)
            self.assertTrue(manager.start_trap_receiver(port, ["public"]))
            self.assertTrue(self._pump_until(manager.is_trap_receiver_running))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.sendto(PREFIX + SECRET + b" not a SNMP message",
                         ("127.0.0.1", port))
                # 同じソケットから送るので、これが届いたら前のパケットは処理済み
                s.sendto(trap_bytes("public"), ("127.0.0.1", port))
                self.assertTrue(self._pump_until(lambda: traps),
                                "壊れたパケットのあと、受信が続かない")
            manager.stop_trap_receiver()
        self._assert_hidden(output.getvalue())

    def _assert_hidden(self, text):
        self.assertIn(QUIET_LINE, text,
                      "例外の経路を通っていない（このテストが何も確かめていない）")
        self.assertNotIn(SECRET.decode("ascii"), text)
        self.assertNotIn(SECRET.decode("ascii")[:5], text)

    def _query_garbage(self, operation, stdout=None):
        """壊れた応答を返す相手へ GET / WALK し、(出力, 結果) を返す

        stdout を渡すと、標準出力だけをそれに差し替える（出力には stderr と
        asyncio のログが入る）。
        """
        from core.snmp_manager import SNMPManager
        manager = SNMPManager()
        type(self)._managers.append(manager)
        completed = []
        manager.operation_completed.connect(
            lambda ok, result: completed.append((ok, result)))
        responder = _GarbageResponder()
        self.addCleanup(responder.close)
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        asyncio_logger = logging.getLogger("asyncio")
        asyncio_logger.addHandler(handler)
        self.addCleanup(asyncio_logger.removeHandler, handler)
        with contextlib.redirect_stdout(output if stdout is None else stdout), \
                contextlib.redirect_stderr(output):
            params = {"port": responder.port, "version": "v2c",
                      "community": "public"}
            if operation == "get":
                self.assertTrue(manager.snmp_get(
                    "127.0.0.1", ["1.3.6.1.2.1.1.1.0"], **params))
            else:
                self.assertTrue(manager.snmp_walk(
                    "127.0.0.1", "1.3.6.1.2.1.1", **params))
            # 最初の壊れた応答で pysnmp の受信が例外を出し、ループの例外ハンドラが
            # 待っている GET / WALK をすぐ失敗させる（snmp_manager._LoopFailure）
            finished = self._pump_until(lambda: manager.worker is None, 30)
            if not finished:
                # 失敗したテストのあとに、動き続けるワーカーを残さない
                manager.request_cancel()
                self._pump_until(lambda: manager.worker is None, 15)
            self.assertTrue(finished, "GET / WALK が終わらない")
        self.assertGreater(responder.replies, 0, "壊れた応答を返していない")
        return output.getvalue(), completed

    def test_a_broken_reply_to_get_is_not_printed(self):
        text, completed = self._query_garbage("get")
        self._assert_hidden(text)
        self.assertEqual(len(completed), 1)
        self.assertFalse(completed[0][0], "読めない応答で成功になった")

    def test_a_broken_reply_to_walk_is_not_printed(self):
        text, completed = self._query_garbage("walk")
        self._assert_hidden(text)
        self.assertEqual(len(completed), 1)
        self.assertFalse(completed[0][0], "読めない応答で成功になった")

    def test_a_failing_print_in_the_handler_does_not_reveal_the_packet(self):
        """標準出力が日本語を書けないとき（英語ロケールの cp1252 など）も漏らさない。

        ハンドラの print が UnicodeEncodeError を出して外へ漏れると、asyncio は
        「Unhandled error in exception handler」として既定のハンドラへ回し、
        元の context（受信したバイト列を含む）をまるごとログへ出す（実測）。
        """
        cp1252_stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                                         errors="strict")
        text, completed = self._query_garbage("get", stdout=cp1252_stdout)
        self.assertNotIn(SECRET.decode("ascii"), text)
        self.assertNotIn(SECRET.decode("ascii")[:5], text)
        self.assertNotIn("exception handler", text)
        self.assertEqual(len(completed), 1)
        self.assertFalse(completed[0][0], "読めない応答で成功になった")


if __name__ == "__main__":
    unittest.main()
