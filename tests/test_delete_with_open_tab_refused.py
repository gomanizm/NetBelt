"""タブを開いている（接続が残っている）機器の削除を断ることを検証する。

何が起きていたか（実測）: マクロ（30 行）を「ツール → マクロ実行」で流して
いる rtr2 を、接続先リストの右クリック →「削除」で消すと、config と接続先
リストからは消えるが、接続とマクロの実行は残った。削除のあとも 1.2 秒の間に
1 行が送られ、実行中のマクロを止める「ツール → マクロ停止」は項目ごと
無くなっていた（止めるにはタブを閉じて接続ごと切るしかない）。

利用者の決定（2026-09-20）: 改名と同じ扱いにし、タブを開いている（接続が
残っている）機器の削除は断って『タブを閉じてから削除してください』と伝える。

実装: _on_device_delete の最初（削除の確認を出す前）に、その名前のタブか
接続が残っていれば警告を出して戻る。タブの無い機器は、これまでどおり確認
してから削除する。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QObject, Qt, pyqtSignal

sys.path.insert(0, "src")


def _device(name="rtrA"):
    return {"name": name, "host": "192.0.2.10", "port": 22, "protocol": "ssh",
            "username": "u", "password": "", "ssh_key": "", "macros": []}


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


def _choose(tree, device_item, path):
    """機器の右クリックメニューで path の項目を選んだことにする。項目の一覧を返す。"""
    captured = {}

    def collect(menu):
        items = {}
        for action in menu.actions():
            if action.isSeparator():
                continue
            sub = action.menu()
            items[action.text()] = (action.isEnabled(),
                                    collect(sub) if sub is not None else None)
        return items

    def fake_exec(menu, *args, **kwargs):
        captured["items"] = collect(menu)
        if not path:
            return None
        current = menu
        action = None
        for text in path:
            action = next(a for a in current.actions() if a.text() == text)
            if action.menu() is not None:
                current = action.menu()
        action.trigger()
        return action

    pos = tree.tree.visualItemRect(device_item).center()
    with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
        tree._show_context_menu(pos)
    return captured["items"]


class DeleteWithOpenTabRefusedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-delete-open-")
        FakeSSH.instances = []
        for patch in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=Path(self.dir)),
                mock.patch("ui.device_tree.list_serial_ports", return_value=[]),
                mock.patch("ui.main_window.SSHConnection", FakeSSH)):
            patch.start()
            self.addCleanup(patch.stop)

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(self.dir, "config.json"))
            window = MainWindow()
        type(self)._windows.append(window)
        return window

    def _delete(self, window, name="rtrA"):
        """削除を選び、確認に「はい」と答えたことにする。(確認, 警告, 削除) を返す。"""
        from PyQt6.QtWidgets import QMessageBox
        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes) as question, \
                mock.patch("ui.main_window.QMessageBox.warning") as warn, \
                mock.patch.object(window, "_load_devices"), \
                mock.patch.object(window.config_manager, "remove_device",
                                  return_value=True) as remove_device:
            window._on_device_delete("Default", name)
        return question, warn, remove_device

    def test_deleting_a_device_with_an_open_tab_is_refused(self):
        """タブを開いている機器は削除できず、タブを閉じるよう伝えること。"""
        window = self._window()
        window.terminal_widget.create_terminal_tab("rtrA")
        window.device_info["rtrA"] = _device()

        question, warn, remove_device = self._delete(window)

        self.assertFalse(remove_device.called, "タブを開いたまま削除できてしまう")
        warn.assert_called_once()
        message = warn.call_args[0][2]
        self.assertIn("rtrA", message)
        self.assertIn("タブを閉じてから削除してください", message,
                      "どうすれば削除できるかを伝えていない")
        self.assertFalse(question.called, "断るのに削除してよいかを聞いている")
        self.assertIn("rtrA", window.device_info, "断ったのに再接続用の写しを消した")

    def test_deleting_a_device_with_a_live_connection_is_refused(self):
        """接続だけが残っている場合（タブを作る前など）も断ること。"""
        window = self._window()
        window.connections["rtrA"] = object()
        self.addCleanup(window.connections.pop, "rtrA", None)

        question, warn, remove_device = self._delete(window)

        self.assertFalse(remove_device.called, "接続中のまま削除できてしまう")
        warn.assert_called_once()

    def test_a_device_without_a_tab_can_still_be_deleted(self):
        """タブを開いていない機器は、これまでどおり確認してから削除すること。"""
        window = self._window()
        window.device_info["rtrA"] = _device()

        question, warn, remove_device = self._delete(window)

        self.assertTrue(question.called)
        self.assertTrue(remove_device.called, "タブの無い機器まで削除を断っている")
        warn.assert_not_called()
        self.assertNotIn("rtrA", window.device_info)

    def test_a_running_macro_can_still_be_stopped_after_the_refusal(self):
        """削除を断ったあとも、実行中のマクロを「ツール → マクロ停止」で止められること。"""
        from PyQt6.QtWidgets import QMessageBox
        window = self._window()
        self.assertTrue(window.config_manager.add_device("Default", _device("rtr2")))
        window._load_devices()
        item = window.device_tree.tree.topLevelItem(0).child(0)
        window._on_connect_requested(item.data(0, Qt.ItemDataRole.UserRole))
        self._pump()
        conn = FakeSSH.instances[-1]
        window.macro_manager.start_command_list(
            "rtr2", ["cmd%02d" % i for i in range(30)], 200)
        self.addCleanup(window.macro_manager.cleanup_device, "rtr2")

        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            _choose(window.device_tree, item, ["削除"])

        self.assertEqual(window.config_manager.find_device_group("rtr2"), "Default",
                         "接続中の機器が設定から消えた")
        item = window.device_tree.tree.topLevelItem(0).child(0)
        _choose(window.device_tree, item, ["ツール", "マクロ停止"])
        self.assertFalse(window.macro_manager.is_command_list_active("rtr2"),
                         "実行中のマクロを止められない")
        sent = len(conn.sent)
        self._pump(0.5)
        self.assertEqual(len(conn.sent), sent, "止めたあとも送られ続けている")


if __name__ == "__main__":
    unittest.main()
