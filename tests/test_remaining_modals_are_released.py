"""exec() で開いたまま捨てていない、残り 2 か所のダイアログが解放されることを検証する。

何が起きていたか（実測）: ダイアログを _exec_dialog へ通す修正から、同じ
ファイル内の 2 か所が漏れていた。どちらも親（MainWindow / TerminalWidget）が
アプリと同じ寿命なので、閉じても終了まで残る。

    QMessageBox children after each 「バージョン情報」: [1, 2, 3]
    LogSaveProgressDialog children after each save: [1, 2, 3]

  - MainWindow._on_version_info は QMessageBox(self) を毎回作って exec()
    するだけで、_exec_dialog の並びに入っていなかった。
  - TerminalWidget.save_current_log の LogSaveProgressDialog も exec() のみ。
    こちらは MainWindow._exec_dialog を呼べない。

どう直したか: 前者は _exec_dialog へ通す（QMessageBox も QDialog なので
そのまま渡せる）。後者は、同じファイルの PasteConfirmDialog と同じ形
（try/finally で setParent(None) + deleteLater()）を当てる。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QEvent, QObject, pyqtSignal

sys.path.insert(0, "src")


class FakeSSH(QObject):
    """接続の見た目だけを持つ偽物。connect() は即座に成功を知らせる。"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)

    def __init__(self, host, port, username, password, ssh_key, parent=None):
        super().__init__(parent)
        self.client = None
        self.sent = []

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


def _device(name="R1"):
    return {"name": name, "host": "192.0.2.1", "port": 22, "protocol": "ssh",
            "username": "u", "password": "", "ssh_key": "", "macros": []}


class RemainingModalsAreReleasedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-modal-release-")
        for patch in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=Path(self.dir)),
                mock.patch("ui.main_window.SSHConnection", FakeSSH)):
            patch.start()
            self.addCleanup(patch.stop)
        self.window = self._window()

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(self.dir, "config.json"))
            window = MainWindow()
        self.addCleanup(self._discard, window)
        return window

    @staticmethod
    def _discard(window):
        window.terminal_widget._output_timer.stop()
        window.terminal_widget._pending_output.clear()
        window.close()

    def _pump(self, seconds=0.2):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _alive(self, dialog_class):
        """予約された破棄を実行してから、生きているダイアログを数える

        親から外して手放す形（setParent(None) + deleteLater）もあるので、
        窓の子だけでなく、親無しで残っているものも数える。
        """
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        found = {id(w) for w in self.window.findChildren(dialog_class)}
        found |= {id(w) for w in self.app.topLevelWidgets()
                  if isinstance(w, dialog_class)}
        return len(found)

    def test_version_info_dialog_is_released(self):
        """ヘルプ→「バージョン情報」を繰り返しても、閉じた分が残らないこと。"""
        from PyQt6.QtWidgets import QMessageBox
        before = self._alive(QMessageBox)

        with mock.patch.object(QMessageBox, "exec",
                               return_value=QMessageBox.StandardButton.Ok):
            for _ in range(3):
                self.window._on_version_info()

        left = self._alive(QMessageBox) - before
        self.assertEqual(left, 0,
                         "閉じたバージョン情報が %d 件残っている" % left)

    def test_log_save_progress_dialog_is_released(self):
        """ログ保存を繰り返しても、閉じたプログレスダイアログが残らないこと。"""
        from PyQt6.QtWidgets import QDialog, QMessageBox
        from ui.dialogs.log_save_dialog import LogSaveProgressDialog
        device = _device()
        self.window.device_tree.load_from_config(
            [{"name": "Lab", "devices": [device]}])
        self.window._on_connect_requested(device)
        self._pump()
        terminal = self.window.terminal_widget._terminals["R1"]
        terminal.setPlainText("保存する行\n")
        before = self._alive(LogSaveProgressDialog)

        # 保存のワーカー（QThread）は止めておく。本物の exec() は保存が
        # 終わる（か、キャンセルで待ち切る）まで戻らないが、ここでは
        # exec() を差し替えるので、動いたままのスレッドごと捨てることになる
        with mock.patch.object(LogSaveProgressDialog, "_start_save"), \
                mock.patch.object(QDialog, "exec",
                                  return_value=QDialog.DialogCode.Rejected), \
                mock.patch.object(QMessageBox, "information"), \
                mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                           return_value=(os.path.join(self.dir, "R1.log"), "")):
            for _ in range(3):
                self.window.terminal_widget.save_current_log()

        left = self._alive(LogSaveProgressDialog) - before
        self.assertEqual(left, 0,
                         "閉じたログ保存ダイアログが %d 件残っている" % left)


if __name__ == "__main__":
    unittest.main()
