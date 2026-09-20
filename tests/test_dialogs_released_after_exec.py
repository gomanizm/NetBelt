"""exec() で開いたダイアログが、閉じたあとに解放されることを検証する。

何が起きていたか（実測）: 編集・設定のダイアログはどれも parent=MainWindow で
作られ、exec() から抜けても破棄していなかった。OK でもキャンセルでも、非表示の
ダイアログとその子ウィジェットが終了まで残る。

    DeviceDialog children after each edit: [1, 2, 3, 4, 5]
    SettingsDialog children: [1, 2, 3]
    QWidget children at start: 481
    after 20 device edits: DeviceDialog=20 QWidget=1481 (+1000)   # 1 回あたり 50
    after 20 group edits:  GroupDialog=20 QWidget +280            # 1 回あたり 14
    after 20 settings:     SettingsDialog=20 QWidget +940         # 1 回あたり 47
    after 10 macro dialogs: MacroDialog=10 QWidget +270           # 1 回あたり 27
    after 10 add-device dialogs: DeviceDialog=10 QWidget +500     # 1 回あたり 50

同じファイルの QMenu（2011 行）と接続オブジェクト（1384 行）は既に
deleteLater を通しており、ダイアログだけが取り残されていた。

どう直したか: MainWindow._exec_dialog(dialog) を足し、exec() を呼ぶ側を
すべてそこへ通す。try/finally で deleteLater() を予約するので、そのあとの
早期 return がどれだけあっても取りこぼさない。deleteLater() はイベント
ループへ戻るまで実際には消さないため、戻り値を見てから dialog.get_*() を
読む呼び出し側はそのまま動く。

なお deleteLater() の予約は、イベントループを回さないこのテストでは
sendPostedEvents(None, DeferredDelete) を呼ばないと実行されない
（Qt は、予約したときより深いループから戻るまで遅延させる）。
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


def _device(name="R1"):
    return {"name": name, "host": "192.0.2.1", "port": 22, "protocol": "ssh",
            "username": "u", "password": "", "ssh_key": "", "macros": []}


class DialogsReleasedAfterExecTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-dialog-release-")
        FakeSSH.instances = []
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

    def _left_behind(self, dialog_class):
        """予約された破棄を実行してから、窓に残っているダイアログを数える"""
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return len(self.window.findChildren(dialog_class))

    def _repeat(self, dialog_class, call, times=3):
        """キャンセルで閉じる操作を繰り返し、残ったダイアログの数を返す"""
        from PyQt6.QtWidgets import QDialog
        with mock.patch.object(QDialog, "exec",
                               return_value=QDialog.DialogCode.Rejected):
            for _ in range(times):
                call()
        return self._left_behind(dialog_class)

    def _connect_device(self, name="R1"):
        device = _device(name)
        self.window.device_tree.load_from_config(
            [{"name": "Lab", "devices": [device]}])
        self.window._on_connect_requested(device)
        self._pump()
        self.assertIn(name, self.window.connections, "前提: 接続できている")
        return device

    def test_device_dialogs_are_released(self):
        """機器の追加・編集・複製で開いたダイアログが残らないこと。"""
        from ui.dialogs.device_dialog import DeviceDialog
        self.window.config_manager.add_device("Default", _device())
        cases = {
            "追加": lambda: self.window._on_add_device(),
            "編集": lambda: self.window._on_device_edit("Default", _device()),
            "複製": lambda: self.window._on_device_duplicate("Default", _device()),
        }
        for what, call in cases.items():
            with self.subTest(what=what):
                left = self._repeat(DeviceDialog, call)
                self.assertEqual(left, 0,
                                 "閉じた DeviceDialog が %d 件残っている" % left)

    def test_group_dialogs_are_released(self):
        """グループの追加・編集で開いたダイアログが残らないこと。"""
        from ui.dialogs.group_dialog import GroupDialog
        self.window.config_manager.add_group("Lab", ["terminal length 0"])
        cases = {
            "追加": lambda: self.window._on_add_group(),
            "編集": lambda: self.window._on_edit_group("Lab"),
        }
        for what, call in cases.items():
            with self.subTest(what=what):
                left = self._repeat(GroupDialog, call)
                self.assertEqual(left, 0,
                                 "閉じた GroupDialog が %d 件残っている" % left)

    def test_settings_dialog_is_released(self):
        """設定ダイアログが残らないこと。"""
        from ui.dialogs.settings_dialog import SettingsDialog
        left = self._repeat(SettingsDialog, lambda: self.window._on_settings())
        self.assertEqual(left, 0,
                         "閉じた SettingsDialog が %d 件残っている" % left)

    def test_macro_dialogs_are_released(self):
        """マクロ設定（メニューバー・右クリックの両方）が残らないこと。"""
        from ui.dialogs.macro_dialog import MacroDialog
        self._connect_device()
        cases = {
            "メニューバー": lambda: self.window._on_macro_settings(),
            "右クリック":
                lambda: self.window._on_macro_settings_from_context("R1"),
        }
        for what, call in cases.items():
            with self.subTest(what=what):
                left = self._repeat(MacroDialog, call)
                self.assertEqual(left, 0,
                                 "閉じた MacroDialog が %d 件残っている" % left)

    def test_update_dialog_is_released(self):
        """更新のお知らせダイアログが残らないこと。"""
        from ui.dialogs.update_dialog import UpdateDialog
        info = {"version": "9.9.9", "body": "テスト", "url": "https://example.com"}
        left = self._repeat(UpdateDialog,
                            lambda: self.window._show_update_dialog(info))
        self.assertEqual(left, 0,
                         "閉じた UpdateDialog が %d 件残っている" % left)

    def test_accepting_the_dialog_still_reads_what_was_entered(self):
        """破棄を予約しても、OK のあとに入力値を読めること。"""
        from PyQt6.QtWidgets import QDialog
        from ui.dialogs.device_dialog import DeviceDialog

        def fill_and_accept(dialog):
            dialog.name_edit.setText("R2")
            dialog.host_edit.setText("192.0.2.2")
            return QDialog.DialogCode.Accepted

        with mock.patch.object(DeviceDialog, "exec", fill_and_accept):
            self.window._on_add_device()

        names = [d["name"] for d in
                 self.window.config_manager.get_group("Default")["devices"]]
        self.assertIn("R2", names,
                      "OK で入力した機器が保存されていない: %r" % names)
        self.assertEqual(self._left_behind(DeviceDialog), 0,
                         "OK で閉じた DeviceDialog が残っている")


if __name__ == "__main__":
    unittest.main()
