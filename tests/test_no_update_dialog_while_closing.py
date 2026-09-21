"""アプリを閉じている最中に、更新の知らせでモーダルを開かないことを検証する。

何が起きていたか（検査役の実測: scratchpad\\cx5j-termui-check\\
t02_update_dialog_during_close.py、offscreen・偽の SSH）: 記録中の受信を
取りこぼさないために、終了処理は配送待ちのシグナルをその場で配る
（MainWindow._drain_output_before_log_finish の processEvents と
TerminalWidget._deliver_queued_output の sendPostedEvents(MetaCall)）。
これは受信だけを配るのではなく、その時点で未配送のキュー接続のスロットを
全部その場で呼ぶ。更新チェックは別スレッドから update_available を emit
するので（手動チェックと起動時チェックの両方）、記録中にアプリを閉じると
終了処理の途中で更新ダイアログが開き、答えるまで閉じない。そのとき
Syslog・SNMP・各サーバと接続は既に止まっていて、「今すぐ更新」を押せば
ダウンロードまで始まる。closeEvent は SNMP の待ちで最大 10 秒かかりうる
ので、その間にチェックが終わって emit される確率は無視できない。
同じ経路で _show_update_check_error（QMessageBox.warning）と
_show_no_update_message（QMessageBox.information）も出うる。

どう直したか: MainWindow.closeEvent の先頭で _shutting_down を立て、
update_available / update_check_error / no_update_available の受け手は
立っていたら何もせずに戻る。配送そのもの（受信を記録へ回すための寄り道）
は変えない。
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
from PyQt6.QtWidgets import QApplication

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


class NoUpdateDialogWhileClosingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-close-update-")
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(self.dir))
        ap.start()
        self.addCleanup(ap.stop)
        FakeSSH.instances = []
        ssh_patch = mock.patch("ui.main_window.SSHConnection", FakeSSH)
        ssh_patch.start()
        self.addCleanup(ssh_patch.stop)
        # offscreen ではモーダルを閉じる相手がいない（出たら数えて落とす）
        warn = mock.patch("PyQt6.QtWidgets.QMessageBox.warning")
        self.warning = warn.start()
        self.addCleanup(warn.stop)
        info = mock.patch("PyQt6.QtWidgets.QMessageBox.information")
        self.information = info.start()
        self.addCleanup(info.stop)

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    @staticmethod
    def _discard(window):
        """閉じた窓に描き残しを持たせたまま次のテストへ行かない。"""
        window.terminal_widget._output_timer.stop()
        window.terminal_widget._pending_output.clear()
        window.close()

    def _recording_window(self):
        """dev に接続して記録を始めたメインウィンドウ"""
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
        window.device_tree.load_from_config(
            [{"name": "Lab", "devices": [device]}])
        window._on_connect_requested(device)
        self._pump()
        log_path = os.path.join(self.dir, "dev.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(log_path, "")):
            window._on_start_log_recording()
        self.assertIn("dev", window.terminal_widget._log_files, "前提: 記録中")
        return window

    @staticmethod
    def _emit_from_a_thread(emit):
        """更新チェックのスレッドと同じように emit する（配送はキュー接続）。"""
        t = threading.Thread(target=emit, daemon=True)
        t.start()
        t.join(5)

    def test_a_queued_update_notice_does_not_open_a_dialog_while_closing(self):
        """未配送の update_available が、閉じている最中にダイアログを開かないこと。"""
        from ui.main_window import MainWindow
        window = self._recording_window()

        opened = []

        def fake_exec(self, dialog):
            opened.append(dialog)
            return 2        # UpdateDialog.UPDATE_LATER

        with mock.patch("ui.dialogs.update_dialog.UpdateDialog"), \
                mock.patch.object(MainWindow, "_exec_dialog", fake_exec):
            self._emit_from_a_thread(
                lambda: window.update_available.emit({"version": "9.9.9"}))
            self.assertEqual(opened, [],
                             "前提: まだ配送されていない（キュー接続）")

            window.close()
            during_close = list(opened)
            # 残った配送をここで使い切る（次のテストへ持ち越さない）
            self._pump(0.1)

        self.assertEqual(during_close, [],
                         "終了処理の途中で更新ダイアログが開いた")

    def test_a_queued_check_result_does_not_pop_a_message_while_closing(self):
        """更新チェックの成否の知らせも、閉じている最中に出さないこと。"""
        window = self._recording_window()

        self._emit_from_a_thread(
            lambda: window.update_check_error.emit("接続できません"))
        self._emit_from_a_thread(
            lambda: window.no_update_available.emit())
        warned = self.warning.call_count
        informed = self.information.call_count

        window.close()
        during_close = (self.warning.call_count - warned,
                        self.information.call_count - informed)
        self._pump(0.1)

        self.assertEqual(during_close, (0, 0),
                         "終了処理の途中で更新確認のモーダルが出た")


if __name__ == "__main__":
    unittest.main()
