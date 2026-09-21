"""更新で終わる経路でも、記録中のログを閉じてから終わることを検証する。

何が起きていたか（検査役の実測: scratchpad\\cx5j-check-termui\\exp_update_quit.py、
offscreen・偽の SSH）: 更新ダイアログの「今すぐ更新」（update_dialog.py の
_on_apply_clicked）と、起動時の未適用更新（main_window.py の
_apply_pending_update）は QApplication.quit() でイベントループを抜ける。
quit() はウィンドウへ closeEvent を送らないので、記録を止める
TerminalWidget.finish_log_recordings を呼ぶ唯一の場所
（MainWindow.closeEvent）を通らない。記録は止める処理を通らずファイルも
閉じられず、描き待ちの受信はそのまま捨てられていた（記録を始めてから
描き待ちを 19 文字作って quit すると、その分が記録に残らなかった）。
ヘルプ▸更新を確認 →「今すぐ更新」で普通に踏める経路。

どう直したか: update_dialog に quit_for_update() を足し、両方の経路から
これを通す。終わらせる前にウィンドウを閉じて closeEvent と同じ後始末
（接続の切断 → 配送待ちの取り込み → 記録の書き切りと停止）を通し、
そのうえで QApplication.quit() を呼ぶ。閉じる方が失敗しても updater は
既に起動しているので、終わらせる方は必ず通す。
"""
import hashlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

sys.path.insert(0, "src")

VERSION = "9.9.9"


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


class UpdateQuitFinishesLogRecordingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-update-quit-")
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(self.dir))
        ap.start()
        self.addCleanup(ap.stop)
        FakeSSH.instances = []
        ssh_patch = mock.patch("ui.main_window.SSHConnection", FakeSSH)
        ssh_patch.start()
        self.addCleanup(ssh_patch.stop)
        # offscreen ではモーダルを閉じる相手がいない（出たら失敗として見る）
        warn = mock.patch("PyQt6.QtWidgets.QMessageBox.warning")
        self.warning = warn.start()
        self.addCleanup(warn.stop)
        crit = mock.patch("PyQt6.QtWidgets.QMessageBox.critical")
        self.critical = crit.start()
        self.addCleanup(crit.stop)

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    @staticmethod
    def _discard(window):
        """閉じた窓に描き残しを持たせたまま次のテストへ行かない。

        描き残しがあると次のテストの最中にタイマーで描き始め、その途中で
        GC がこの窓を捨てるとプロセスごと落ちる
        （tests/test_window_close_finishes_log_recording.py の同名の注記）。
        """
        window.terminal_widget._output_timer.stop()
        window.terminal_widget._pending_output.clear()
        window.close()

    def _staged_update(self):
        """適用の直前の確認を通る更新ファイル一式を作り、ZIP のパスを返す。"""
        d = tempfile.mkdtemp(prefix="netbelt-update-zip-", dir=self.dir)
        zip_path = os.path.join(d, "NetBelt-%s.zip" % VERSION)
        body = b"PK\x03\x04 dummy netbelt update"
        with open(zip_path, "wb") as f:
            f.write(body)
        with open(zip_path + ".sha256", "w", encoding="ascii") as f:
            f.write(hashlib.sha256(body).hexdigest())
        with open(zip_path + ".version", "w", encoding="ascii") as f:
            f.write(VERSION)
        return zip_path

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
        # 更新ダイアログからの終了は表示中の窓を閉じる。実機と同じく
        # 表示しておく（ヘルプ▸更新を確認 は表示中の窓から開く）
        window.show()
        device = {"name": "dev", "host": "192.0.2.10", "port": 22,
                  "username": "u", "password": "", "protocol": "ssh"}
        window.device_tree.load_from_config(
            [{"name": "Lab", "devices": [device]}])
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

    def _pending_output(self, window, text):
        """描き待ちのまま残る受信を作る。

        溜まり分を描くタイマーを止めてから受信させる。止めないとイベントを
        捌いた時点で描かれてしまい、「閉じる処理が記録へ書いたか」を見られ
        ない。実機でも、受信の直後に終了を選べば同じ状態になる。
        """
        conn = FakeSSH.instances[-1]
        conn.output_received.emit(text)
        window.terminal_widget._output_timer.stop()
        self.assertGreater(
            len(window.terminal_widget._pending_output.get("dev", ())), 0,
            "前提: 描いていない受信が残っている")

    def test_the_update_dialog_quit_keeps_the_pending_output(self):
        """「今すぐ更新」で終わっても、描き待ちの受信が記録に残ること。"""
        from ui.dialogs.update_dialog import UpdateDialog
        window, log_path = self._recording_window()
        self._pending_output(window, "TAIL-FROM-UPDATE-DIALOG\r\n")

        dialog = UpdateDialog(window, {"version": VERSION})
        dialog.downloaded_zip_path = self._staged_update()
        # 配布物として動いているふり（ソース実行では更新を当てない）。
        # updater.bat はリポジトリ直下のものが使われる
        with mock.patch("ui.dialogs.update_dialog.running_from_source",
                        return_value=False), \
                mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(QApplication, "quit") as quit_call:
            dialog._on_apply_clicked()
            self._pump(0.2)

        self.critical.assert_not_called()
        popen.assert_called_once()
        self.assertTrue(quit_call.called, "前提: 終了しようとしている")
        with open(log_path, encoding="utf-8") as f:
            recorded = f.read()
        self.assertIn("TAIL-FROM-UPDATE-DIALOG", recorded,
                      "更新での終了が、描き待ちの受信を記録せずに捨てた")

    def test_the_startup_apply_quit_keeps_the_pending_output(self):
        """起動時の未適用更新で終わっても、描き待ちの受信が記録に残ること。"""
        window, log_path = self._recording_window()
        self._pending_output(window, "TAIL-FROM-PENDING-UPDATE\r\n")

        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch("subprocess.Popen") as popen, \
                mock.patch.object(QApplication, "quit") as quit_call:
            window._apply_pending_update(self._staged_update(), VERSION)
            self._pump(0.2)

        self.critical.assert_not_called()
        popen.assert_called_once()
        self.assertTrue(quit_call.called, "前提: 終了しようとしている")
        with open(log_path, encoding="utf-8") as f:
            recorded = f.read()
        self.assertIn("TAIL-FROM-PENDING-UPDATE", recorded,
                      "更新での終了が、描き待ちの受信を記録せずに捨てた")


if __name__ == "__main__":
    unittest.main()
