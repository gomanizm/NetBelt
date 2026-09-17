"""show tech-support のような大量の出力で、端末が固まらないことを検証する。

実測（オフスクリーン、受信と同じ 4096 バイトずつ append_output）:
文書が MAX_DOCUMENT_BLOCKS（20000 行）に達するまでは 1 回 約 9ms、達した後は
約 360ms（40 倍）。4 万行 1.9MB の描画に 87 秒かかり、受信スレッドと同じく
シグナルで流すと、送り終えてから画面が追いつくまで 87 秒、その間 GUI の
タイマーが最大 58 秒止まった。上限を 5000 にすると 5000 行から約 118ms に、
上限を外すと最後まで約 9ms のまま。

原因は二つ。
  1. setMaximumBlockCount による先頭行の切り捨て。挿入のたびに Qt が先頭の
     ブロックを 1 個ずつ捨て、QTextEdit では文書の大きさに比例して重い。
  2. 受信したかたまりごとに描き直していること。受信スレッドからのシグナルは
     イベントキューに積まれ、Qt はそれを 1 回の処理でまとめて配るので、
     積まれた数だけ描き終えるまでキー入力も再描画も受け付けない。
"""
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


def _chunks(start_line, count, width=40):
    """受信スレッドと同じ 4096 文字ずつのかたまりと、最後の行番号を返す。"""
    text = "".join("line %06d %s\r\n" % (i, "x" * width)
                   for i in range(start_line, start_line + count))
    return [text[i:i + 4096] for i in range(0, len(text), 4096)]


class TrimmingAtTheLineCapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_rendering_at_the_line_cap_is_not_much_slower_than_below_it(self):
        """上限に達した後の描画が、達する前より桁違いに遅くならないこと。"""
        from ui.terminal_widget import TerminalWidget
        cap = 5000
        patcher = mock.patch.object(TerminalWidget, "MAX_DOCUMENT_BLOCKS", cap)
        patcher.start()
        self.addCleanup(patcher.stop)
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.resize(900, 500)
        w.show()
        terminal = w.create_terminal_tab("dev")
        self.app.processEvents()

        below, at_cap = [], []
        for chunk in _chunks(0, cap + 2500):
            full = terminal.document().blockCount() >= cap
            t0 = time.perf_counter()
            w.append_output("dev", chunk)
            (at_cap if full else below).append(time.perf_counter() - t0)
        self.assertGreater(len(at_cap), 10, "前提: 上限に達した後も描いている")
        self.assertLessEqual(terminal.document().blockCount(), cap)

        baseline = sorted(below[-20:])[10]
        capped = sorted(at_cap)[len(at_cap) // 2]
        self.assertLess(capped / baseline, 4.0,
                        "上限に達した後の描画が %.1f 倍遅い（%.1fms → %.1fms）"
                        % (capped / baseline, baseline * 1e3, capped * 1e3))


class ReceivedBurstKeepsTheWindowResponsiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-burst-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        type(self)._windows.append(w)
        return w

    @staticmethod
    def _tail(terminal, blocks=40):
        doc = terminal.document()
        first = max(0, doc.blockCount() - blocks)
        return "\n".join(doc.findBlockByNumber(n).text()
                         for n in range(first, doc.blockCount()))

    def test_a_burst_of_output_does_not_block_the_event_loop(self):
        """受信が一気に届いても、1 回のイベント処理で長く止まらないこと。

        受信スレッドが送り終えてから、イベントループを回す 1 回ごとの所要時間を
        測る。積まれたかたまりを全部その場で描くと、その 1 回が描画の合計時間に
        なり、その間はキー入力（Ctrl+C での中断を含む）も再描画も効かない
        （実測: 1 万 1 千行 146 かたまりが processEvents 1 回で描かれ 0.61 秒）。
        """
        from core.ssh_connection import SSHConnection
        w = self._window()
        chunks = _chunks(0, 16000)
        last_line = "line %06d" % 15999
        sent = threading.Event()

        def burst_connect(conn):
            for chunk in chunks:
                conn.output_received.emit(chunk)
            sent.set()
            return True

        device = {"name": "R", "host": "192.0.2.1", "port": 22,
                  "protocol": "ssh", "username": "admin", "password": "pw"}
        with mock.patch.object(SSHConnection, "connect", burst_connect):
            w._connect_ssh(device)
            self.assertTrue(sent.wait(30), "前提: 受信スレッドが送り終えた")
            terminal = w.terminal_widget._terminals["R"]

            longest = 0.0
            deadline = time.time() + 120
            while time.time() < deadline and last_line not in self._tail(terminal):
                t0 = time.perf_counter()
                self.app.processEvents()
                longest = max(longest, time.perf_counter() - t0)
            drawn = last_line in self._tail(terminal)
            w._on_tab_closed("R")

        self.assertTrue(drawn, "前提: 最後の行まで描き終えた")
        self.assertLess(longest, 0.25,
                        "受信のかたまりを描き終えるまで %.1f 秒イベントが止まった"
                        % longest)

    def test_a_burst_is_drawn_completely_and_in_order(self):
        """まとめて描いても、行が欠けたり入れ替わったりしないこと。"""
        from core.ssh_connection import SSHConnection
        w = self._window()
        chunks = _chunks(0, 3000)
        sent = threading.Event()

        def burst_connect(conn):
            for chunk in chunks:
                conn.output_received.emit(chunk)
            sent.set()
            return True

        device = {"name": "R", "host": "192.0.2.1", "port": 22,
                  "protocol": "ssh", "username": "admin", "password": "pw"}
        with mock.patch.object(SSHConnection, "connect", burst_connect):
            w._connect_ssh(device)
            self.assertTrue(sent.wait(30))
            terminal = w.terminal_widget._terminals["R"]
            deadline = time.time() + 60
            while (time.time() < deadline
                   and "line 002999" not in self._tail(terminal)):
                self.app.processEvents()
            numbers = [int(n) for n in re.findall(r"line (\d{6})",
                                                  terminal.toPlainText())]
            w._on_tab_closed("R")

        self.assertEqual(numbers, list(range(3000)),
                         "欠けた・重複した・入れ替わった行がある")


class QueuedOutputOrderTest(unittest.TestCase):
    """溜めてから描く出力が、直接書く案内より後ろへ回らないことを検証する。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_queued_output_is_drawn_before_a_later_notice(self):
        """受信→切断の案内の順に届いたら、画面でもその順に並ぶこと。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")

        w.queue_output("dev", "Router#show clock\r\n")
        w.show_notice("dev", "\n接続が切断されました\n")
        self.app.processEvents()

        text = terminal.toPlainText()
        self.assertIn("Router#show clock", text)
        self.assertLess(text.index("Router#show clock"),
                        text.index("接続が切断されました"),
                        "受信した出力が、後から出した案内の下に回った")

    def test_output_queued_for_a_closed_tab_is_dropped(self):
        """描く前にタブを閉じたら、溜めていた出力は捨てて何も起こさないこと。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("dev")
        w.queue_output("dev", "late output\r\n")

        index = [w.tab_widget.tabText(i)
                 for i in range(w.tab_widget.count())].index("dev")
        w._close_tab(index)
        self.app.processEvents()

        self.assertNotIn("dev", w._terminals)


if __name__ == "__main__":
    unittest.main()
