"""マクロが、コマンドを実際に送り出してから次の遅延を数え、最後の行を送るまで実行中のままであることを検証する。

マクロの送信は端末の送信列を通る（貼り付けの途中へ割り込ませないため）。
ところが MacroManager は、列へ積んだだけで行を進めて次のタイマーを始めて
いた。長い貼り付けの後ろに積まれると、指定の間隔は「列へ積む間隔」になり、
排出のときには行の間の待ちが消える。全行を積み終えると完了扱い（実行中で
ない）になり、その後の停止は取り消しを呼ばないので、残った行が後から届いた。

実測（検証役、MainWindow + 偽の SSH。送信が 512 文字ごとに 20ms 止まる）:
約 100KB を貼り付けてマクロ 3 行（遅延 1000ms）を始めると、3 行とも積んだ
だけで 3.05 秒に実行中でなくなった。そのとき列に cmd1〜cmd3 が残っており、
直後の停止は取り消さなかった。貼り付けが終わった 4.39 秒の後に、3 行が 22ms
間隔で送られた（設定は 1.0 秒）。実行中でなくなると「ツール」に「マクロ停止」
も出ない。

端末が行を実際に送り出したとき（シリアルは送信スレッドが書き終えたとき）に
積んだ側へ知らせ、MacroManager はその知らせを受けてから遅延を数えるように
した。最後の行を送り出して遅延が過ぎてから完了にする。
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

SEND_DELAY = 0.005      # 1 回の送信で止まる秒数（相手の受信窓の空き待ち）


class SlowSSH(QObject):
    """送信のたびに少し止まる偽の SSH。送った時刻と文字列を控える。"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)
    instances = []

    def __init__(self, host, port, username, password, ssh_key, parent=None):
        super().__init__(parent)
        self.client = None
        self.sent = []
        SlowSSH.instances.append(self)

    def set_terminal_size(self, cols, rows):
        pass

    def connect(self):
        self.connected.emit()
        return True

    def send_command(self, command):
        time.sleep(SEND_DELAY)
        self.sent.append((time.perf_counter(), command))

    def dispose(self):
        pass

    def disconnect(self):
        pass


class MacroWaitsForActualSendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.config_manager import ConfigManager
        SlowSSH.instances = []
        d = Path(tempfile.mkdtemp(prefix="netbelt-macrosent-"))
        for patcher in (
                mock.patch("core.config_manager.app_data_dir", return_value=d),
                mock.patch("ui.main_window.ConfigManager",
                           lambda *a, **k: ConfigManager(str(d / "config.json"))),
                mock.patch("ui.main_window.SSHConnection", SlowSSH)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _connected(self):
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        window._on_connect_requested({"name": "dev", "host": "192.0.2.10",
                                      "port": 22, "username": "u",
                                      "password": "", "protocol": "ssh"})
        self._pump(0.3)
        terminal = window.terminal_widget._terminals["dev"]
        self.assertTrue(terminal.can_send_input(), "前提: 接続できている")
        conn = SlowSSH.instances[-1]
        del conn.sent[:]
        self.addCleanup(window.macro_manager.cleanup_device, "dev")
        return window, terminal, conn

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.002)

    @staticmethod
    def _commands(conn):
        return [(at, text) for at, text in conn.sent if text.startswith("cmd")]

    def test_the_macro_stays_active_and_keeps_its_delay_behind_a_long_paste(self):
        """貼り付けが終わるまで実行中のままで、各行が遅延以上の間隔で届くこと。"""
        window, terminal, conn = self._connected()
        manager = window.macro_manager
        delay = 300
        terminal.send_text("P" * (terminal.SEND_CHUNK * 60))
        manager.start_command_list("dev", ["cmd1", "cmd2", "cmd3"], delay)

        active_while_pasting = []
        deadline = time.time() + 10
        while time.time() < deadline and manager.is_command_list_active("dev"):
            if any(entry[0].startswith("P") for entry in terminal._send_queue):
                active_while_pasting.append(True)
            self._pump(0.01)

        self.assertTrue(active_while_pasting, "前提: 貼り付けの排出中に様子を見た")
        pasted = [at for at, text in conn.sent if text.startswith("P")]
        commands = self._commands(conn)
        self.assertEqual([text for _, text in commands],
                         ["cmd1\r", "cmd2\r", "cmd3\r"], "コマンドが届いていない")
        self.assertGreater(commands[0][0], max(pasted), "貼り付けを追い越した")
        gaps = [later[0] - earlier[0]
                for earlier, later in zip(commands, commands[1:])]
        self.assertTrue(all(gap >= (delay - 50) / 1000.0 for gap in gaps),
                        "行の間の待ちが消えた: %s 秒（設定 %.1f 秒）"
                        % (["%.3f" % gap for gap in gaps], delay / 1000.0))

    def test_stopping_while_the_commands_wait_behind_a_paste_sends_none(self):
        """遅延が過ぎても、送り出す前なら実行中のままで、停止すれば 1 行も届かないこと。"""
        window, terminal, conn = self._connected()
        manager = window.macro_manager
        terminal.send_text("P" * (terminal.SEND_CHUNK * 100))
        manager.start_command_list("dev", ["cmd1", "cmd2", "cmd3"], 50)
        self._pump(0.25)            # 3 行 x 50ms の遅延はとうに過ぎている
        self.assertTrue(terminal._send_queue, "前提: 貼り付けはまだ排出中")
        self.assertTrue(manager.is_command_list_active("dev"),
                        "送り出していないのに完了扱いになった")

        manager.stop_command_list("dev")
        deadline = time.time() + 10
        while time.time() < deadline and terminal._send_queue:
            self._pump(0.01)
        self._pump(0.3)

        self.assertEqual(self._commands(conn), [],
                         "停止したのにマクロのコマンドが届いた")

    def test_a_macro_on_an_idle_session_keeps_its_timing(self):
        """列が空なら、これまでどおり最初の行はすぐ送り、遅延ごとに次を送ること。"""
        window, terminal, conn = self._connected()
        manager = window.macro_manager
        manager.start_command_list("dev", ["cmd1", "cmd2"], 200)
        self.assertEqual([text for _, text in self._commands(conn)], ["cmd1\r"],
                         "最初の行をその場で送っていない")
        deadline = time.time() + 5
        while time.time() < deadline and manager.is_command_list_active("dev"):
            self._pump(0.01)

        commands = self._commands(conn)
        self.assertEqual([text for _, text in commands], ["cmd1\r", "cmd2\r"])
        self.assertGreaterEqual(commands[1][0] - commands[0][0], 0.15)


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
        self.writes.append((time.perf_counter(), bytes(data)))
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        return b""

    def close(self):
        self.is_open = False


class SerialMacroWaitsForTheWriteTest(unittest.TestCase):
    """シリアルでは、送信スレッドが書き終えた時点から遅延を数えること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.config_manager import ConfigManager
        GatedPort.instances = []
        d = Path(tempfile.mkdtemp(prefix="netbelt-macroserial-"))
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

    def test_the_next_line_waits_until_the_port_has_written_the_last(self):
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        window._on_connect_requested({"name": "con1", "host": "COM99",
                                      "protocol": "console", "baudrate": 9600})
        terminal = window.terminal_widget._terminals["con1"]
        deadline = time.time() + 5
        while time.time() < deadline and not terminal.can_send_input():
            self._pump(0.02)
        port = GatedPort.instances[-1]
        manager = window.macro_manager
        self.addCleanup(manager.cleanup_device, "con1")
        self.addCleanup(port.gate.set)

        manager.start_command_list("con1", ["cmd1", "cmd2"], 50)
        self._pump(0.3)             # 遅延 50ms はとうに過ぎている
        self.assertTrue(manager.is_command_list_active("con1"),
                        "書き終える前に完了扱いになった")
        self.assertEqual(terminal._send_queue, [], "書き終える前に次の行を積んだ")
        self.assertEqual(window.connections["con1"]._send_queue.unfinished_tasks, 1,
                         "書き終える前に次の行をシリアル側へ渡した")

        written_at = time.perf_counter()
        port.gate.set()
        deadline = time.time() + 5
        while time.time() < deadline and manager.is_command_list_active("con1"):
            self._pump(0.01)

        self.assertEqual([data for _, data in port.writes], [b"cmd1\r", b"cmd2\r"])
        self.assertGreaterEqual(port.writes[1][0] - written_at, 0.04,
                                "書き終えてから遅延を数えていない")


if __name__ == "__main__":
    unittest.main()
