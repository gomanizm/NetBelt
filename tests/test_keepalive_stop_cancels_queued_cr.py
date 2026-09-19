"""キープアライブを止めたら、まだ送っていないキープアライブの CR も取り消すことを検証する。

キープアライブの CR は、打鍵や貼り付けと同じ端末の送信列を通る（貼り付けの
途中へ割り込ませないため）。ところが印を付けずに積んでおり、stop_keepalive は
タイマーを止めるだけだったので、長い貼り付けの排出待ちで列に残っていた CR が
停止の後で機器へ届いた。改行なしで貼った 1 行の末尾に CR が付くと、その行が
実行される。

実測（検証役）:
  - SSH（端末の列）: 改行なしで 1536 文字を貼り付け、キープアライブを発火させて
    すぐ止めると、列は ['xxx', '\\r'] で、停止の後に末尾へ '\\r' が届いた。
  - シリアル（9600bps 相当）: 改行なしの 2KB を貼り付け、0.1 秒後に発火、
    0.21 秒に停止すると、2.12 秒に b'\\r' が書かれた。

キープアライブの CR に由来の印（keepalive）を付けて積み、stop_keepalive（と、
それを呼ぶ cleanup_device）で、端末の送信列からまだ送っていないものを取り除く
ようにした。シリアルは、書き終えるまで次を渡さない背圧で端末の列に残る。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QObject, pyqtSignal

sys.path.insert(0, "src")


class FakeSSH(QObject):
    """接続の見た目だけを持つ偽物。送ったものを控える。"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)
    instances = []

    def __init__(self, host, port, username, password, ssh_key, parent=None):
        super().__init__(parent)
        self.client = None
        self.sent = []
        FakeSSH.instances.append(self)

    def set_terminal_size(self, cols, rows):
        pass

    def connect(self):
        self.connected.emit()
        return True

    def send_command(self, command):
        self.sent.append(command)

    def dispose(self):
        pass

    def disconnect(self):
        pass


class GatedPort:
    """write がゲートを開けるまで戻らない疑似シリアルポート。"""
    instances = []

    def __init__(self, **kwargs):
        self.is_open = True
        self.in_waiting = 0
        self.baudrate = kwargs.get("baudrate", 9600)
        self.writes = []
        self.gate = threading.Event()
        GatedPort.instances.append(self)

    def write(self, data):
        self.gate.wait(10)
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        return b""

    def close(self):
        self.is_open = False


class KeepaliveStopCancelsQueuedCrTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.config_manager import ConfigManager
        FakeSSH.instances = []
        GatedPort.instances = []
        d = Path(tempfile.mkdtemp(prefix="netbelt-kacancel-"))
        for patcher in (
                mock.patch("core.config_manager.app_data_dir", return_value=d),
                mock.patch("ui.main_window.ConfigManager",
                           lambda *a, **k: ConfigManager(str(d / "config.json"))),
                mock.patch("ui.main_window.SSHConnection", FakeSSH),
                mock.patch("ui.device_tree.list_serial_ports", return_value=[]),
                mock.patch("core.serial_connection.serial.Serial", GatedPort)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.005)

    def _window(self, device):
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        window._on_connect_requested(device)
        terminal = window.terminal_widget._terminals[device["name"]]
        deadline = time.time() + 5
        while time.time() < deadline and not terminal.can_send_input():
            self._pump(0.02)
        self.assertTrue(terminal.can_send_input(), "前提: 接続できている")
        self.addCleanup(window.macro_manager.cleanup_device, device["name"])
        return window, terminal

    def _ssh(self):
        window, terminal = self._window({
            "name": "dev", "host": "192.0.2.10", "port": 22, "username": "u",
            "password": "", "protocol": "ssh"})
        conn = FakeSSH.instances[-1]
        del conn.sent[:]
        return window, terminal, conn

    def test_a_stopped_keepalive_sends_no_queued_cr(self):
        """発火して列で待っていた CR が、停止の後に届かないこと。"""
        window, terminal, conn = self._ssh()
        chunk = terminal.SEND_CHUNK
        terminal.send_text("x" * (chunk * 3))       # 改行なしの長い 1 行
        window._start_keepalive("dev", 60)
        window.macro_manager._send_keepalive("dev")  # タイマーの発火と同じ

        window._stop_keepalive("dev")
        self._pump()

        self.assertEqual(conn.sent, ["x" * chunk] * 3,
                         "停止したキープアライブの CR が届いた: %r"
                         % [(len(s), s[:3]) for s in conn.sent])

    def test_stopping_the_keepalive_leaves_macro_commands_alone(self):
        """キープアライブの停止で、マクロのコマンドまで消えないこと（逆も同じ）。"""
        window, terminal, conn = self._ssh()
        chunk = terminal.SEND_CHUNK
        terminal.send_text("x" * (chunk * 2))
        window._start_keepalive("dev", 60)
        window.macro_manager._send_keepalive("dev")
        window.macro_manager.start_command_list("dev", ["cmd1"], 50)

        window._stop_keepalive("dev")
        self._pump()

        self.assertEqual(conn.sent, ["x" * chunk, "x" * chunk, "cmd1\r"],
                         "キープアライブの停止でマクロのコマンドが消えた: %r"
                         % [(len(s), s[:4]) for s in conn.sent])

    def test_stopping_a_macro_leaves_a_running_keepalive_alone(self):
        """マクロの停止で、動いているキープアライブの CR まで消えないこと。"""
        window, terminal, conn = self._ssh()
        chunk = terminal.SEND_CHUNK
        terminal.send_text("x" * (chunk * 2))
        window._start_keepalive("dev", 60)
        window.macro_manager._send_keepalive("dev")
        window.macro_manager.start_command_list("dev", ["cmd1"], 50)

        window.macro_manager.stop_command_list("dev")
        self._pump()

        self.assertEqual(conn.sent, ["x" * chunk, "x" * chunk, "\r"],
                         "マクロの停止でキープアライブの CR が消えた: %r"
                         % [(len(s), s[:4]) for s in conn.sent])
        window._stop_keepalive("dev")

    def test_a_stopped_keepalive_sends_no_cr_on_a_busy_serial_port(self):
        """シリアルで書き込み待ちの間に止めても、CR が後から書かれないこと。"""
        window, terminal = self._window({"name": "con1", "host": "COM99",
                                         "protocol": "console", "baudrate": 9600})
        port = GatedPort.instances[-1]
        self.addCleanup(port.gate.set)
        chunk = terminal.SEND_CHUNK
        terminal.send_text("abc " * chunk)    # 改行なしの 2KB
        self._pump(0.1)
        window._start_keepalive("con1", 60)
        window.macro_manager._send_keepalive("con1")
        self._pump(0.1)

        window._stop_keepalive("con1")
        port.gate.set()
        deadline = time.time() + 5
        while time.time() < deadline and len(port.writes) < 4:
            self._pump(0.02)
        self._pump(0.3)

        self.assertEqual(b"".join(port.writes), b"abc " * chunk,
                         "停止したキープアライブの CR が書かれた: %r"
                         % [w[-4:] for w in port.writes])


if __name__ == "__main__":
    unittest.main()
