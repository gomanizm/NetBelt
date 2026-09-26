"""シリアルで、送れない文字（孤立したサロゲート）を含む送信を黙って落とさないことを検証する。

何が起きていたか（基準 441ea02 で実測、MainWindow と pyserial の loop://）。
貼り付けは 'a'×600, CR, 'b'×10, U+D800, 'b'×10, CR, 'c'×1100, CR。端末は
512 文字ずつの区切りで渡すので、U+D800 は 2 つ目の区切りに入る。
  - 機器が受け取ったのは 1,212 バイトで、中身は 'a'×512＋'c'×699＋CR
    だった。2 つ目の区切り（1 行目の残り 'a'×88 と CR、2 行目、3 行目の頭
    'c'×401）が丸ごと消え、1 行目の頭と 3 行目の残りが 1 行に繋がって CR で
    実行された（意図しないコマンドの送信）。
  - SerialConnection.send_command の encode('utf-8') が try の外にあり、
    UnicodeEncodeError は key_pressed のスロットの中で起きた。PyQt は
    excepthook を呼んで emit から戻り、端末はその区切りを列から進め済み
    なので、次の区切りを続けて渡した。接続は保たれ、error_occurred は出ず、
    製品では『予期しないエラーが発生しました』のダイアログになる。

どう直したか（利用者の決定 (a)）。貼り付けは端末の入口
（InteractiveTerminal.send_text）で丸ごと断る（SSH・Telnet と共通の修正）。
保険として、SerialConnection.send_command でも符号化の失敗を捕まえ、
excepthook へ流さず、何も送らずに端末へ理由を出す。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

SUR = chr(0xD800)
TEXT = "a" * 600 + "\r" + "b" * 10 + SUR + "b" * 10 + "\r" + "c" * 1100 + "\r"
NOTICE = "送れない文字"


class RecordingPort:
    """書いたものを順に記録する疑似ポート"""
    instances = []

    def __init__(self, **kwargs):
        self.is_open = True
        self.in_waiting = 0
        self.baudrate = kwargs.get("baudrate", 9600)
        self.written = bytearray()
        RecordingPort.instances.append(self)

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        return b""

    def close(self):
        self.is_open = False


class _App:
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, seconds, until=None):
        end = time.time() + seconds
        while time.time() < end and not (until and until()):
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()


class SerialSendCommandTest(_App, unittest.TestCase):
    """端末の入口を通らずに届いた区切りの保険"""

    def _conn(self):
        from core.serial_connection import SerialConnection
        conn = SerialConnection("COM99", 9600)
        port = RecordingPort()
        conn.serial_conn = port
        conn._is_connected = True
        self.addCleanup(conn.dispose)
        return conn, port

    def test_an_unsendable_piece_is_refused_and_reported(self):
        conn, port = self._conn()
        errors, shown = [], []
        conn.error_occurred.connect(errors.append)
        conn.output_received.connect(shown.append)

        conn.send_command("ab" + SUR)     # 例外がスロットの外へ漏れないこと
        self._pump(0.3)

        self.assertEqual(b"", bytes(port.written))
        self.assertFalse(conn.has_pending_sends(),
                         "断った区切りが送信列に残った（端末が待ち続ける）")
        self.assertEqual([], errors)
        self.assertTrue(conn.is_connected)
        self.assertTrue(any(NOTICE in text for text in shown),
                        "断ったことが知らされていない: %r" % (shown,))

    def test_the_next_piece_still_goes_out(self):
        conn, port = self._conn()
        conn.send_command("ab" + SUR)

        conn.send_command("show clock\r")
        self._pump(3, until=lambda: port.written)

        self.assertEqual(b"show clock\r", bytes(port.written))


class SerialPasteTest(_App, unittest.TestCase):
    """MainWindow の配線（コンソール接続）で貼り付ける"""

    def setUp(self):
        from core.config_manager import ConfigManager
        RecordingPort.instances = []
        d = Path(tempfile.mkdtemp(prefix="netbelt-serial-surrogate-"))
        self.hooked = []
        for patcher in (
                mock.patch("core.config_manager.app_data_dir", return_value=d),
                mock.patch("ui.main_window.ConfigManager",
                           lambda *a, **k: ConfigManager(str(d / "config.json"))),
                mock.patch("ui.device_tree.list_serial_ports", return_value=[]),
                mock.patch("core.serial_connection.serial.Serial", RecordingPort),
                mock.patch("sys.excepthook",
                           lambda t, e, tb: self.hooked.append(t.__name__))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_nothing_reaches_the_port_and_the_session_stays(self):
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        window._on_connect_requested({"name": "con1", "host": "COM99",
                                      "protocol": "console", "baudrate": 9600})
        terminal = window.terminal_widget._terminals["con1"]
        self._pump(5, until=terminal.can_send_input)
        self.assertTrue(terminal.can_send_input(), "前提: 接続できている")
        conn = window.connections["con1"]
        port = RecordingPort.instances[-1]

        terminal.send_text(TEXT)
        self._pump(1)

        self.assertEqual(b"", bytes(port.written),
                         "送れない文字の前後の区切りがポートへ書かれた"
                         "（行が繋がって実行される）")
        self.assertEqual([], self.hooked, "例外が excepthook へ漏れた")
        self.assertIs(conn, window.connections.get("con1"))
        self.assertIn(NOTICE, terminal.toPlainText(),
                      "断ったことが端末に出ていない")


if __name__ == "__main__":
    unittest.main()
