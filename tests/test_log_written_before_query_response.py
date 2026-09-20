"""受信を記録へ書き切ってから、機器への問い合わせ応答を送ることを検証する。

append_output は、描いた片を記録へ書く（_write_logs）より先に、画面が作った
問い合わせ応答（ESC[6n へのカーソル位置など）を terminal._queue_send で送って
いた。応答の送信は key_pressed → SSHConnection.send_command と同期でつながって
いて、そこで失敗すると error_occurred → MainWindow._on_connection_error →
（「送信エラー」は切断扱い）_on_connection_closed → show_notice → append_output
と、その場で append_output へ再入する。

再入側の _write_logs が「残量 0 になった停止ログを閉じる」ループを先に回すので、
外側は閉じたハンドルへ書きに行って失敗していた。

実測（基準 16101ef、offscreen、実機なし）:
  - 描き待ちに 'TAIL-BODY\\r\\n\\x1b[6n' を置いて記録を停止し、応答の送信で
    切断案内が出るようにすると、停止したログは空のまま閉じられ
    （A.log = ''）、「停止より前に受信した分を書き終えられませんでした:
    I/O operation on closed file.」というモーダル警告が出た。
  - 停止せず記録中のままでも順序が入れ替わる。画面は本文 → 案内なのに
    ファイルは 'セッションが切断されました' → 'BODY-LINE' の順になった。

直し方: append_output で _write_logs を、問い合わせ応答を送るループより前へ
移す（パーサへ通してログへ書き切ってから応答を送る）。再入はログを書き終えた
後に起きるので、停止したログは本文を書いてから閉じられ、案内は本文の後ろへ
記録される。
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
    """送信が失敗する接続。fail_sends を立てると send_command が切断を知らせる"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)
    instances = []

    def __init__(self, host, port, username, password, ssh_key, parent=None):
        super().__init__(parent)
        self.client = None
        self.sent = []
        self.fail_sends = False
        FakeSSH.instances.append(self)

    def set_terminal_size(self, cols, rows):
        pass

    def connect(self):
        self.connected.emit()
        return True

    def send_command(self, command):
        self.sent.append(command)
        if self.fail_sends:
            # paramiko の書き込みが落ちたときと同じ知らせ方
            self.error_occurred.emit("送信エラー: Socket is closed")

    def dispose(self):
        pass

    def disconnect(self):
        pass


class LogWrittenBeforeQueryResponseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "dev")
        self.dir = tempfile.mkdtemp(prefix="netbelt-logorder-")
        mock.patch("core.config_manager.app_data_dir",
                   return_value=Path(self.dir)).start()
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
    def _discard(window):
        """描き残しを持ったまま次のテストへ行かない（タイマーで描き始める）"""
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

    def test_a_stopped_log_keeps_its_tail_when_the_response_send_fails(self):
        """応答の送信失敗で再入しても、停止したログに停止前の受信が残ること。"""
        window, path = self._recording_window()
        widget = window.terminal_widget
        conn = FakeSSH.instances[-1]
        handle = widget._log_files["dev"]

        # 本文とカーソル位置の問い合わせを、描き待ちに残したまま記録を止める
        widget.queue_output("dev", "TAIL-BODY\r\n\x1b[6n")
        widget.stop_log_recording("dev")
        self.assertIn("dev", widget._closing_logs, "前提: 書き終えてから閉じる")
        conn.fail_sends = True

        widget._flush_pending_output()

        self.assertTrue([s for s in conn.sent if s.endswith("R")],
                        "前提: カーソル位置の応答を送ろうとした: %r" % conn.sent)
        self.assertIn("セッションが切断されました",
                      widget._terminals["dev"].toPlainText(),
                      "前提: 送信失敗が切断として扱われ、再入が起きた")
        self.assertEqual(self._read(path), "TAIL-BODY\n",
                         "停止したログが、停止前の受信を書く前に閉じられた")
        self.assertTrue(handle.closed, "書き終えたのにファイルを閉じていない")
        self.warning.assert_not_called()

    def test_the_disconnect_notice_is_recorded_after_the_output_it_follows(self):
        """記録中でも、切断案内が元の受信本文より先に記録されないこと。"""
        window, path = self._recording_window()
        widget = window.terminal_widget
        conn = FakeSSH.instances[-1]

        widget.queue_output("dev", "BODY-LINE\r\n\x1b[6n")
        conn.fail_sends = True

        widget._flush_pending_output()

        screen = widget._terminals["dev"].toPlainText()
        self.assertLess(screen.index("BODY-LINE"),
                        screen.index("セッションが切断されました"),
                        "前提: 画面は本文 → 案内の順")
        got = self._read(path)
        self.assertTrue(got.startswith("BODY-LINE\n"),
                        "記録が本文から始まっていない: %r" % got[:60])
        self.assertLess(got.index("BODY-LINE"),
                        got.index("セッションが切断されました"),
                        "記録だけ案内 → 本文の順になった: %r" % got[:60])
        self.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
