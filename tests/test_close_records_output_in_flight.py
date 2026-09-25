"""終了処理の最中に受信した分も、ログ記録に残ることを検証する。

何が起きていたか（実測）: closeEvent は finish_log_recordings() を先に呼び、
受信スレッドを止める接続の切断はそのあとだった。受信スレッドのシグナルは
GUI スレッドのイベントキューへ積まれるだけなので、まだ配送されていない分は
queue_output にも届いておらず、finish_log_recordings では救えない。

    受信スレッドから 200 行 emit し、イベントループを回さずに閉じた場合:
      pending in _pending_output before close: 0
      recorded chars: 0
      missing lines: 200 of 200

    dev へ接続してログ記録中、2ms 間隔で受信しながら、MIB 読み込みの待機に
    0.5 秒かかる状況で閉じた場合:
      emitted before close: 167  emitted total: 380  during shutdown: 213
      lost lines: 216

終了処理は Syslog / SFTP / TFTP / FTP の停止、SNMP の停止、MIB 読み込みの待機を
通るので、そこに時間がかかるほど失う量が増えた。

どう直したか: closeEvent で、接続の後始末（マクロ → SFTP → 接続）を
finish_log_recordings() より前へ移し、受信スレッドを止めたあとに一度だけ
配送を捌いてから記録を閉じる。積まれたままの通知はここで queue_output まで
届く。接続を閉じるのに conn.disconnect() ではなく _dispose_connection() を
使うのは、disconnect() が出す disconnected の通知で切断バナーが端末へ入り、
まだ開いている記録へ混ざるため（それまでの記録の中身を変えない）。
マクロ → SFTP → 接続という後始末の順番は変えていない。
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
    """受信スレッドを持つ偽の接続。

    dispose() は本物と同じく、読み取りスレッドを止めて終わるまで待つ
    （通知は出さない）。disconnect() はそのあとに disconnected を出す。
    """
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)
    instances = []

    def __init__(self, host, port, username, password, ssh_key, parent=None):
        super().__init__(parent)
        self.client = None
        self.sent = []
        self.emitted = []
        self._stop = threading.Event()
        self._thread = None
        FakeSSH.instances.append(self)

    def set_terminal_size(self, cols, rows):
        pass

    def connect(self):
        self.connected.emit()
        return True

    def send_command(self, command):
        self.sent.append(command)

    def start_receiving(self, interval=0.002):
        """受信スレッドを起こし、止められるまで 1 行ずつ出し続ける"""
        def worker():
            index = 0
            while not self._stop.is_set():
                line = "tick %05d" % index
                self.emitted.append(line)
                self.output_received.emit(line + "\r\n")
                index += 1
                time.sleep(interval)

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def emit_lines(self, count):
        """受信スレッドから count 行だけ出して、スレッドの終了まで待つ"""
        def worker():
            for index in range(count):
                line = "tick %05d" % index
                self.emitted.append(line)
                self.output_received.emit(line + "\r\n")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(5)

    def dispose(self):
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(2)
        self._thread = None

    def disconnect(self):
        self.dispose()
        self.disconnected.emit()


class CloseRecordsOutputInFlightTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-close-inflight-")
        FakeSSH.instances = []
        for patch in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=Path(self.dir)),
                mock.patch("ui.main_window.SSHConnection", FakeSSH)):
            patch.start()
            self.addCleanup(patch.stop)
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
        window.terminal_widget._output_timer.stop()
        window.terminal_widget._pending_output.clear()
        window.close()

    def _recording_window(self):
        """dev に接続して記録を始めたメインウィンドウと、記録先のパス"""
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
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

    @staticmethod
    def _missing(conn, recorded):
        return [line for line in conn.emitted if line not in recorded]

    def test_notifications_posted_from_the_receive_thread_are_recorded(self):
        """emit 済みで GUI へ配送されていない分も、閉じたときに記録されること。"""
        window, log_path = self._recording_window()
        conn = FakeSSH.instances[-1]
        conn.emit_lines(200)
        # イベントループを回していないので、まだ queue_output へ届いていない
        self.assertEqual(len(window.terminal_widget._pending_output.get("dev", ())),
                         0, "前提: 溜まり分としてはまだ見えていない")

        window.close()

        with open(log_path, encoding="utf-8") as f:
            recorded = f.read()
        missing = self._missing(conn, recorded)
        self.assertEqual(missing, [],
                         "配送待ちの %d 行 / %d 行が記録に無い"
                         % (len(missing), len(conn.emitted)))
        self.warning.assert_not_called()

    def test_output_received_during_a_slow_shutdown_is_recorded(self):
        """終了処理に時間がかかっても、その間の受信が記録から欠けないこと。"""
        window, log_path = self._recording_window()
        conn = FakeSSH.instances[-1]
        conn.start_receiving()
        # 定常状態にする（ここまでの分は配送済み）
        self._pump(0.2)
        before_close = len(conn.emitted)

        # MIB 読み込みの待機に 0.3 秒かかる状況（その間も受信は続く）
        original = window.snmp_panel.wait_for_background_work

        def slow_wait():
            time.sleep(0.3)
            return original()

        with mock.patch.object(window.snmp_panel, "wait_for_background_work",
                               slow_wait):
            window.close()

        self.assertGreater(len(conn.emitted), before_close,
                           "前提: 終了処理の最中にも受信している")
        with open(log_path, encoding="utf-8") as f:
            recorded = f.read()
        missing = self._missing(conn, recorded)
        self.assertEqual(missing, [],
                         "終了処理中の %d 行 / %d 行が記録に無い"
                         % (len(missing), len(conn.emitted)))
        self.warning.assert_not_called()

    def test_the_shutdown_banner_does_not_enter_the_recording(self):
        """閉じるときの切断バナーを、記録へ混ぜないこと（記録の中身を変えない）。"""
        window, log_path = self._recording_window()
        conn = FakeSSH.instances[-1]
        conn.emit_lines(5)

        window.close()

        with open(log_path, encoding="utf-8") as f:
            recorded = f.read()
        self.assertNotIn("セッションが切断されました", recorded,
                         "終了時の案内が記録に入った: %r" % recorded[-120:])
        self.assertNotIn("Enterキー", recorded)


if __name__ == "__main__":
    unittest.main()
