"""アプリを閉じている最中に、各パネルの受け手がモーダルを開かないことを検証する。

何が起きていたか（実測）: 記録中の受信を取りこぼさないために、終了処理は
配送待ちのシグナルをその場で配る（MainWindow._drain_output_before_log_finish
の processEvents と TerminalWidget._deliver_queued_output の
sendPostedEvents(MetaCall)）。配られるのは受信だけではなく、その時点で
未配送のキュー接続すべてなので、別スレッドが出した知らせも全部届く。
更新チェックの 3 種は _closing_now() で止まるようになったが（
test_no_update_dialog_while_closing）、同じ窓で開くモーダルが他にも残って
いた。検査役の実測では、127.0.0.1 の SFTP サーバへ 400MB を転送中に記録
つきで閉じると、closeEvent の sftp_mgr.disconnect() が転送を切り、転送
スレッドの error_occurred が上の processEvents で配られて
SFTPPanel._on_error が QMessageBox.warning を開いた。SNMP も同じで、
closeEvent は cancel_operation() を最大 5 秒、wait_for_background_work()
を最大 10 秒待つので、その間に GET/WALK が失敗して
SNMPPanel._on_operation_completed(False) の QMessageBox.critical や、Trap
受信スレッド発の _on_error_occurred の QMessageBox.warning が開く。
TFTP/FTP/SFTP サーバパネルの _on_error（critical）も経路は同じ。
影響は更新ダイアログと同じで、答えるまで終了が止まる（そのとき Syslog・
SNMP・各サーバと接続は停止済みで、開いても何もできない）。

このテストは、実運用の発生源（転送スレッド・ワーカースレッド・受信
スレッド）と同じ「別スレッドからの emit（キュー接続）」を作り、未配送の
まま close() したときに各モーダルが開かないことを見る。

どう直したか: MainWindow.closeEvent の先頭で、_shutting_down と一緒に
各パネルへ「閉じている」印（_closing）を渡す。印が立っている間、
SFTPPanel._on_error・SNMPPanel._on_operation_completed／_on_error_occurred・
TFTP/FTP/SFTP サーバパネルの _on_error はモーダルを開かずに戻る（状態表示
とログへの記録は従来どおり）。配送そのもの（受信を記録へ回すための寄り道）
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


class NoPanelModalWhileClosingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-close-modal-")
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
        """転送・ワーカー・受信スレッドと同じように emit する（キュー接続）。"""
        t = threading.Thread(target=emit, daemon=True)
        t.start()
        t.join(5)

    def _modals_during_close(self, window, emit):
        """別スレッドから emit し、未配送を確かめてから閉じたときのモーダル数

        戻り値は (警告の回数, エラーの回数)。どちらも閉じている最中の分。
        """
        before = (self.warning.call_count, self.critical.call_count)
        self._emit_from_a_thread(emit)
        self.assertEqual((self.warning.call_count, self.critical.call_count),
                         before, "前提: まだ配送されていない（キュー接続）")

        window.close()
        during_close = (self.warning.call_count - before[0],
                        self.critical.call_count - before[1])
        # 残った配送をここで使い切る（次のテストへ持ち越さない）
        self._pump(0.1)
        return during_close

    def test_a_queued_sftp_transfer_error_opens_no_modal_while_closing(self):
        """転送中に記録つきで閉じたとき、SFTP エラーのモーダルを出さないこと。"""
        from core.sftp_manager import SFTPManager
        window = self._recording_window()
        sftp_manager = SFTPManager(window)
        window.sftp_managers["dev"] = sftp_manager
        window.sftp_panel.set_sftp_manager(sftp_manager, "dev")
        self._pump(0.1)

        during_close = self._modals_during_close(
            window,
            lambda: sftp_manager.error_occurred.emit(
                "アップロードエラー: EOFError。"
                "SFTP接続を切断しました。接続し直してください"))

        self.assertEqual(during_close, (0, 0),
                         "終了処理の途中で SFTP エラーのモーダルが出た")

    def test_a_queued_snmp_failure_opens_no_modal_while_closing(self):
        """GET/WALK の失敗通知が、閉じている最中にモーダルを出さないこと。"""
        window = self._recording_window()

        during_close = self._modals_during_close(
            window,
            lambda: window.snmp_manager.operation_completed.emit(
                False, "SNMP GET に失敗: タイムアウト"))

        self.assertEqual(during_close, (0, 0),
                         "終了処理の途中で SNMP 失敗のモーダルが出た")

    def test_a_queued_snmp_error_opens_no_modal_while_closing(self):
        """Trap 受信スレッド発のエラーも、閉じている最中に出さないこと。"""
        window = self._recording_window()

        during_close = self._modals_during_close(
            window,
            lambda: window.snmp_manager.error_occurred.emit(
                "Trap受信エラー: 待ち受けに失敗しました"))

        self.assertEqual(during_close, (0, 0),
                         "終了処理の途中で SNMP エラーのモーダルが出た")

    def test_queued_server_errors_open_no_modal_while_closing(self):
        """TFTP/FTP/SFTP サーバのエラーも、閉じている最中に出さないこと。"""
        for attr, server_attr in (("tftp_server_panel", "tftp_server"),
                                  ("ftp_server_panel", "ftp_server"),
                                  ("sftp_server_panel", "sftp_server")):
            with self.subTest(panel=attr):
                window = self._recording_window()
                server = getattr(getattr(window, attr), server_attr)

                during_close = self._modals_during_close(
                    window,
                    lambda s=server: s.error_occurred.emit(
                        "サーバーの起動に失敗しました: ポート使用中"))

                self.assertEqual(
                    during_close, (0, 0),
                    "終了処理の途中で %s のモーダルが出た" % attr)


if __name__ == "__main__":
    unittest.main()
