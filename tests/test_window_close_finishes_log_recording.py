"""ログ記録中にアプリを閉じても、受信済みの出力が記録から欠けないこと。

記録への書き込みは append_output の中（描いたあと）で行う。受信した出力は
queue_output で溜めて 16384 文字ずつ描くので、描き終わるまでは溜まり分が
まだファイルに無い。MainWindow.closeEvent は接続を切って終了を受け入れる
だけで、溜まり分を記録し切る処理も、記録を止めてファイルを閉じる処理も
無かった。

実測（main.py と同じく app.exec() を回した）: 記録中に 1,480,000 文字
（2 万行）を受信し、受信完了の 300ms 後に閉じると、閉じる時点で 69 万文字が
未描画のまま残り、exec() が戻ったあとの記録ファイルは 808,130 / 1,460,000
文字（44.6% が欠落。最後の行は 2 万行中の 11070 行目）。ファイルも閉じられて
いなかった。× を押すかファイル → 終了を選ぶだけで起きる。

直し方: TerminalWidget.finish_log_recordings() を足し、closeEvent で接続を
切る前に呼ぶ。記録中の機器ごとに、溜まり分を画面へは描かずパーサだけに
通して記録へ書き、そのあと記録を止めてファイルを閉じる。描くと 3MB で
約 2.2 秒かかり閉じるのが遅れるが、パーサだけなら約 0.05 秒で済む。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QObject, pyqtSignal

sys.path.insert(0, "src")


class FakeSSH(QObject):
    """接続の見た目だけを持つ偽物。connect() は即座に成功を知らせる。"""
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


class WindowCloseFinishesLogRecordingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-close-log-")
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(self.dir))
        ap.start()
        self.addCleanup(ap.stop)
        FakeSSH.instances = []
        ssh_patch = mock.patch("ui.main_window.SSHConnection", FakeSSH)
        ssh_patch.start()
        self.addCleanup(ssh_patch.stop)
        # offscreen では警告を閉じる相手がいない（出たら失敗として見る）
        warn = mock.patch("PyQt6.QtWidgets.QMessageBox.warning")
        self.warning = warn.start()
        self.addCleanup(warn.stop)

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    @staticmethod
    def _discard(window):
        """閉じた窓に描き残しを持たせたまま次のテストへ行かない

        描き残しがあると次のテストの最中にタイマーで描き始め、その途中で
        GC がこの窓を捨てるとプロセスごと落ちる（実測: 0xC0000409 で
        何も出さずに終わった。gc.disable() では落ちない）。直す前の
        この回帰テストが、失敗ではなく全体の停止として見えてしまう。
        """
        window.terminal_widget._output_timer.stop()
        window.terminal_widget._pending_output.clear()
        window.close()

    def _recording_window(self):
        """dev に接続して記録を始めたメインウィンドウと、記録先のパス"""
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(self.dir, "config.json"))
            window = MainWindow()
        self.addCleanup(self._discard, window)
        device ={"name": "dev", "host": "192.0.2.10", "port": 22,
                  "username": "u", "password": "", "protocol": "ssh"}
        window.device_tree.load_from_config([{"name": "Lab", "devices": [device]}])
        window._on_connect_requested(device)
        self._pump()
        self.assertEqual(window.terminal_widget.get_current_tab_name(), "dev",
                         "前提: dev のタブを表示している")
        log_path = os.path.join(self.dir, "dev.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(log_path, "")):
            window._on_start_log_recording()
        self.assertIn("dev", window.terminal_widget._log_files, "前提: 記録中")
        return window, log_path

    def test_closing_the_window_records_output_that_was_not_drawn_yet(self):
        """描き終わる前に閉じても、受信した全文が記録に残ること。"""
        window, log_path = self._recording_window()
        handle = window.terminal_widget._log_files["dev"]
        lines = ["line %05d %s" % (i, "x" * 60) for i in range(3000)]
        conn = FakeSSH.instances[-1]
        payload = "".join(line + "\r\n" for line in lines)
        for i in range(0, len(payload), 4096):
            conn.output_received.emit(payload[i:i + 4096])
        pending = sum(len(c) for c in
                      window.terminal_widget._pending_output.get("dev", []))
        self.assertGreater(pending, 100000, "前提: 描いていない出力が溜まっている")

        window.close()   # イベントループを回さずに閉じる

        with open(log_path, encoding="utf-8") as f:
            got = f.read()
        expected = "".join(line + "\n" for line in lines)
        self.assertEqual(len(got), len(expected),
                         "記録が %d / %d 文字しか無い" % (len(got), len(expected)))
        self.assertEqual(got, expected)
        self.assertTrue(handle.closed, "記録ファイルが開いたまま残っている")
        self.assertNotIn("dev", window.terminal_widget._log_files)
        self.warning.assert_not_called()

    def test_escape_sequences_split_across_chunks_are_not_recorded(self):
        """溜まり分をパーサに通すので、色の指定などは記録に入らないこと。"""
        window, log_path = self._recording_window()
        conn = FakeSSH.instances[-1]
        filler = "".join("fill %05d\r\n" % i for i in range(2000))
        # 色の指定を 2 つのかたまりにまたがらせる
        conn.output_received.emit(filler + "\x1b[3")
        conn.output_received.emit("1mRED\x1b[0m\tEND\r\n")

        window.close()

        with open(log_path, encoding="utf-8") as f:
            got = f.read()
        self.assertTrue(got.endswith("RED\tEND\n"),
                        "記録の末尾が違う: %r" % got[-40:])
        self.assertNotIn("\x1b", got)
        self.assertNotIn("[31m", got)


if __name__ == "__main__":
    unittest.main()
