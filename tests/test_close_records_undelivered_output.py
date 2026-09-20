"""閉じる処理の最中に受信した分が、記録から丸ごと欠けないことを検証する。

受信スレッドの output_received は Qt のイベントキューに積まれ、GUI スレッドへ
配られてはじめて queue_output が呼ばれる。記録を書き切る finish_log_recordings
／_log_pending_on_close は _pending_output しか見ないので、emit 済みでまだ
配られていない分はどこにも現れないまま捨てられていた。

閉じる処理はイベントループへ戻らないまま何秒も進む。MainWindow.closeEvent は
記録を書き切る前に SNMP の cancel_operation と MIB 読み込み待ちを通るので、
SNMPPanel.wait_for_background_work の説明どおり最大 10 秒かかる。その間の
受信はすべて未配送のまま溜まるので、欠ける量もその間に受信した全部になる。

実測（基準 16101ef、offscreen、接続は FakeSSH）:
  - GUI スレッドから 1 片、受信スレッドから 1 片 emit して window.close():
      pending after gui emit = 15 / pending after thread emit = 15
      log contents = 'HEAD-FROM-GUI\\n'   ← 受信スレッドの分は入っていない
      warning called: []                  ← 欠けたことは利用者に伝わらない
  - wait_for_background_work を 0.5 秒の固まりに差し替えて、その間ずっと
    受信スレッドから送った場合: emitted lines = 241 / logged lines = 0。

直し方: TerminalWidget._log_pending_on_close の先頭で、すでにキューに積まれた
シグナル呼び出しだけを配る（QCoreApplication.sendPostedEvents(None, MetaCall)）。
描画やタイマーは走らせないので、閉じるのが遅くなることはない。受信スレッドが
生きている限り取りこぼしの窓を 0 にはできないが、「固まっていた間ぶん」から
「1 回の配送ぶん」へ縮まる。閉じる順（接続を切る前に記録を閉じる）は変えない。
切断バナーがログへ入るようになってしまうため。
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


class _ThreadedSource(QObject):
    """受信スレッドの代わり。別スレッドから emit してキューに積ませる"""
    output = pyqtSignal(str, str)


def _emit_from_thread(emit):
    """別スレッドから emit して、戻るまで待つ（配送はまだされない）"""
    thread = threading.Thread(target=emit)
    thread.start()
    thread.join(5)


class CloseRecordsUndeliveredOutputTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "dev")
        self.dir = tempfile.mkdtemp(prefix="netbelt-undelivered-")
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(self.dir))
        ap.start()
        FakeSSH.instances = []
        mock.patch("ui.main_window.SSHConnection", FakeSSH).start()
        # offscreen では警告を閉じる相手がいない（出たら失敗として見る）
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    @staticmethod
    def _discard(widget):
        """描き残しを持ったまま次のテストへ行かない（タイマーで描き始める）"""
        terminal = getattr(widget, "terminal_widget", widget)
        terminal._output_timer.stop()
        terminal._pending_output.clear()
        widget.close()

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
        device = {"name": "dev", "host": "192.0.2.10", "port": 22,
                  "username": "u", "password": "", "protocol": "ssh"}
        window.device_tree.load_from_config([{"name": "Lab", "devices": [device]}])
        window._on_connect_requested(device)
        self._pump()
        log_path = os.path.join(self.dir, "dev.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(log_path, "")):
            window._on_start_log_recording()
        self.assertIn("dev", window.terminal_widget._log_files, "前提: 記録中")
        return window, log_path

    def test_output_emitted_while_the_close_is_blocked_is_recorded(self):
        """閉じる処理が固まっている間に受信スレッドが送った分も記録に入ること。"""
        from ui.snmp_panel import SNMPPanel
        window, log_path = self._recording_window()
        conn = FakeSSH.instances[-1]
        lines = ["blocked %04d" % i for i in range(50)]

        def emit_while_blocked(_panel):
            # closeEvent は MIB 読み込み待ちで固まる（最大 10 秒）。その間に
            # 受信スレッドが送った分がどうなるかを見る
            _emit_from_thread(
                lambda: [conn.output_received.emit(line + "\r\n")
                         for line in lines])

        mock.patch.object(SNMPPanel, "wait_for_background_work",
                          emit_while_blocked).start()
        conn.output_received.emit("HEAD-FROM-GUI\r\n")

        window.close()

        got = self._read(log_path)
        self.assertIn("HEAD-FROM-GUI", got, "前提: GUI スレッドの分は入っている")
        missing = [line for line in lines if line not in got]
        self.assertEqual(missing, [],
                         "閉じる間に受信した %d / %d 行が記録から欠けた"
                         % (len(missing), len(lines)))
        self.assertEqual(got.splitlines(),
                         ["HEAD-FROM-GUI"] + lines, "記録の中身が崩れた")
        self.warning.assert_not_called()

    def test_closing_a_tab_records_output_that_was_not_delivered_yet(self):
        """タブを閉じる経路でも、配られていない受信が記録に入ること。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(self._discard, w)
        w.create_terminal_tab("dev")
        w.tab_widget.setCurrentIndex(w.tab_widget.count() - 1)
        path = os.path.join(self.dir, "tab.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        self.assertIn("dev", w._log_files, "前提: 記録中")

        source = _ThreadedSource()
        source.output.connect(w.queue_output)
        w.queue_output("dev", "HEAD-FROM-GUI\r\n")
        _emit_from_thread(
            lambda: source.output.emit("dev", "TAIL-FROM-THREAD\r\n"))
        self.assertEqual(len(w._pending_output.get("dev", ())),
                         len("HEAD-FROM-GUI\r\n"),
                         "前提: 受信スレッドの分はまだ配られていない")

        w._close_tab(w.tab_widget.indexOf(w._terminals["dev"]))

        self.assertEqual(self._read(path),
                         "HEAD-FROM-GUI\nTAIL-FROM-THREAD\n",
                         "閉じたタブの、配られていない受信が記録から欠けた")


if __name__ == "__main__":
    unittest.main()
