"""シリアルで、まだ書いていない送信を端末側に残し、マクロの停止で取り消せることを検証する。

端末は送信を区切りごとに key_pressed で渡す。SSH と Telnet は受け取った
その場で送り切るが、シリアルは送信スレッドの列へ積むだけで戻る（GUI を
止めないため）。端末は相手が書き終えたかを知らずに次の区切りを渡すので、
貼り付けの残りも後ろに積んだマクロのコマンドも、すぐシリアル側の列へ移る。
マクロの停止（cancel_macro_sends）が取り除けるのは端末の列だけなので、
停止しても機器へ届いた。

実測（検証役、MainWindow._connect_serial と疑似ポート）:
  - write をゲートで止めて 1024 文字を貼り付け、マクロを始めて 0.1 秒後に
    停止すると、端末の列は空で、シリアル側の列に [b'PPPP...', b'cmd1\\r'] が
    残っていた。ゲートを開けると b'cmd1\\r' が書かれた。
  - 9600bps 相当（512 文字で 0.53 秒）で 4KB を貼り付けてマクロ 3 行（1000ms）を
    始め、実行中に停止しても、4.25 秒に cmd1\\r と cmd2\\r が続けて書かれた。

シリアル接続が「まだ書き終えていない送信があるか」と「書き終えた」知らせを
出し、端末は書き終えるまで次の区切りを渡さないようにした（背圧）。未送信の
分は端末の列に残るので、停止で取り消せる。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


class GatedPort:
    """write がゲートを開けるまで戻らない疑似ポート。書いた順に記録する。"""
    instances = []

    def __init__(self, **kwargs):
        self.is_open = True
        self.in_waiting = 0
        self.baudrate = kwargs.get("baudrate", 9600)
        self.writes = []
        self.gate = threading.Event()
        self.delay = 0.0
        GatedPort.instances.append(self)

    def write(self, data):
        self.gate.wait(10)
        if self.delay:
            time.sleep(self.delay)
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        return b""

    def close(self):
        self.is_open = False


class SerialSendBackpressureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.config_manager import ConfigManager
        GatedPort.instances = []
        d = Path(tempfile.mkdtemp(prefix="netbelt-serialbp-"))
        for patcher in (
                mock.patch("core.config_manager.app_data_dir", return_value=d),
                mock.patch("ui.main_window.ConfigManager",
                           lambda *a, **k: ConfigManager(str(d / "config.json"))),
                mock.patch("ui.device_tree.list_serial_ports", return_value=[]),
                mock.patch("core.serial_connection.serial.Serial", GatedPort)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.005)

    def _connected(self):
        """コンソール接続した MainWindow・端末・疑似ポートを返す。"""
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        window._on_connect_requested({"name": "con1", "host": "COM99",
                                      "protocol": "console", "baudrate": 9600})
        deadline = time.time() + 5
        terminal = window.terminal_widget._terminals["con1"]
        while time.time() < deadline and not terminal.can_send_input():
            self._pump(0.02)
        self.assertTrue(terminal.can_send_input(), "前提: 接続できている")
        port = GatedPort.instances[-1]
        self.addCleanup(window.macro_manager.cleanup_device, "con1")
        self.addCleanup(port.gate.set)      # 後片付けの前に書き込みを通す
        return window, terminal, port

    def _wait_written(self, port, count, seconds=5):
        deadline = time.time() + seconds
        while time.time() < deadline and len(port.writes) < count:
            self._pump(0.02)

    def test_a_macro_stopped_while_the_port_is_busy_never_reaches_it(self):
        """書き込み待ちの間に止めたマクロのコマンドが、ゲートを開けた後も書かれないこと。"""
        window, terminal, port = self._connected()
        chunk = terminal.SEND_CHUNK
        terminal.send_text("P" * (chunk * 2))
        window.macro_manager.start_command_list("con1", ["cmd1", "cmd2", "cmd3"], 1000)
        self._pump(0.1)

        window.macro_manager.stop_command_list("con1")
        port.gate.set()
        self._wait_written(port, 2)
        self._pump(0.3)

        self.assertEqual(port.writes, [b"P" * chunk, b"P" * chunk],
                         "停止したマクロのコマンドが書かれた: %r"
                         % [w[:8] for w in port.writes])

    def test_a_macro_stopped_during_a_slow_paste_sends_nothing(self):
        """遅い回線で貼り付けの途中に止めると、貼り付けは届き、マクロは届かないこと。"""
        window, terminal, port = self._connected()
        port.gate.set()
        port.delay = 0.05
        chunk = terminal.SEND_CHUNK
        terminal.send_text("P" * (chunk * 8))
        window.macro_manager.start_command_list("con1", ["cmd1", "cmd2", "cmd3"], 1000)
        self._pump(0.15)
        self.assertTrue(window.macro_manager.is_command_list_active("con1"),
                        "前提: マクロは実行中")

        window.macro_manager.stop_command_list("con1")
        self._wait_written(port, 8)
        self._pump(0.3)

        self.assertEqual(b"".join(port.writes), b"P" * (chunk * 8),
                         "停止したのにマクロのコマンドが書かれたか、貼り付けが欠けた: %r"
                         % [w[:8] for w in port.writes])

    def test_the_next_chunk_waits_until_the_port_has_written_the_last_one(self):
        """書き終えるまで次の区切りを渡さず、残りは端末の列にあること。"""
        window, terminal, port = self._connected()
        conn = window.connections["con1"]
        chunk = terminal.SEND_CHUNK
        terminal.send_text("P" * (chunk * 4))
        self._pump(0.2)

        self.assertEqual(conn._send_queue.unfinished_tasks, 1,
                         "書き終える前に次の区切りをシリアル側へ渡した")
        self.assertEqual(sum(len(entry[0]) for entry in terminal._send_queue),
                         chunk * 3, "未送信の分が端末の列に残っていない")

        port.gate.set()
        self._wait_written(port, 4)
        self.assertEqual(b"".join(port.writes), b"P" * (chunk * 4))

    def test_typing_after_a_paste_arrives_in_order(self):
        """貼り付けの後に打った文字も、貼り付けの後ろに順に届くこと。"""
        window, terminal, port = self._connected()
        port.gate.set()
        chunk = terminal.SEND_CHUNK
        terminal.send_text("A" * (chunk + 10))
        terminal._send_typed("x")
        terminal._send_typed("\r")
        self._wait_written(port, 4)

        self.assertEqual(b"".join(port.writes), b"A" * (chunk + 10) + b"x\r")


class SerialConnectionReportsPendingSendsTest(unittest.TestCase):
    """SerialConnection が、書き終えていない送信の有無と書き終えた知らせを出すこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_pending_until_written_then_drained(self):
        from core.serial_connection import SerialConnection
        port = GatedPort()
        conn = SerialConnection("COM99", 9600)
        conn.serial_conn = port
        conn._is_connected = True
        drained = []
        conn.send_drained.connect(lambda: drained.append(True))
        self.addCleanup(conn.dispose)
        self.addCleanup(port.gate.set)

        self.assertFalse(conn.has_pending_sends(), "前提: 何も積んでいない")
        conn.send_command("abc")
        self.assertTrue(conn.has_pending_sends(), "書き終える前なのに空と答えた")

        port.gate.set()
        deadline = time.time() + 5
        while time.time() < deadline and not drained:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(port.writes, [b"abc"])
        self.assertFalse(conn.has_pending_sends(), "書き終えたのに残っていると答えた")
        self.assertTrue(drained, "書き終えた知らせが届かない")


if __name__ == "__main__":
    unittest.main()
