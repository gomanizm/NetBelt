"""「ツール」を開いている間に切れた機器へ、キープアライブを始めないことを検証する。

何が起きていたか（実測）: 接続中の機器で右クリック →「ツール」を開くと、
その時点の状態で「キープアライブ開始」が有効な項目として作られる。メニューを
開いたまま相手機器が切れると、切断側の後始末（MacroManager.cleanup_device）で
タイマーは止まるが、開いたままの項目を選べば MainWindow._start_keepalive が
そのまま呼ばれ、切断済みの機器名で新しいタイマーが立った。

    connections after disconnect: []
    keepalive active after disconnect: False   ← 切断時の後始末は効いている
    keepalive active after trigger: True       ← 切断後に新しいタイマーが立つ
    new conn sent: ['\r']                      ← 再接続後に発火すると新セッションへ CR

しかも、この状態の「ツール」は connected=False で灰色になるため、ツリーからは
止められない（止めるにはメニューバーのマクロ設定へ回るしかない）。

どう直したか: 要求を受け取る側（MainWindow._on_keepalive_start_requested /
_on_keepalive_stop_requested）で、いま self.connections にその機器があるかを
実行時に確かめる。無ければ何もせず、ステータスバーで知らせる。メニューを
作った時点の有効・無効（DeviceTree._add_tools_menu）は、開いている間の状態変化を
追えないため。
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
        self.disconnected.emit()


def _find_item(tree, name):
    root = tree.tree.invisibleRootItem()
    for i in range(root.childCount()):
        group_item = root.child(i)
        for j in range(group_item.childCount()):
            item = group_item.child(j)
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, dict) and data.get("name") == name:
                return item
    return None


def _grab_tools_action(tree, item, text):
    """機器の右クリックメニューを開いて、「ツール」の中の項目を掴んだまま返す"""
    captured = {}

    def walk(menu):
        for action in menu.actions():
            sub = action.menu()
            if sub is not None:
                walk(sub)
            elif action.text() == text:
                captured["action"] = action

    def fake_exec(menu, *args, **kwargs):
        walk(menu)
        return None

    pos = tree.tree.visualItemRect(item).center()
    with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
        tree._show_context_menu(pos)
    return captured.get("action")


class KeepaliveBlockedAfterMenuDisconnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-keepalive-stale-")
        FakeSSH.instances = []
        for patch in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=Path(self.dir)),
                mock.patch("ui.main_window.SSHConnection", FakeSSH)):
            patch.start()
            self.addCleanup(patch.stop)

    def _pump(self, seconds=0.2):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    @staticmethod
    def _discard(window):
        window.terminal_widget._output_timer.stop()
        window.terminal_widget._pending_output.clear()
        window.close()

    def _connected_window(self):
        """rtrA へ接続済みのメインウィンドウと、その機器データ"""
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(self.dir, "config.json"))
            window = MainWindow()
        self.addCleanup(self._discard, window)
        device = {"name": "rtrA", "host": "192.0.2.10", "port": 22,
                  "protocol": "ssh", "username": "u", "password": "",
                  "ssh_key": "", "macros": []}
        self.assertTrue(window.config_manager.add_device("Default", dict(device)),
                        "前提: 機器を登録できた")
        window._load_devices()
        window._on_connect_requested(device)
        self._pump()
        self.assertIn("rtrA", window.connections, "前提: 接続できている")
        return window, device

    def test_a_menu_opened_while_connected_cannot_start_keepalive_after_the_drop(self):
        """メニューを開いたまま切断されたら、「キープアライブ開始」は何もしないこと。"""
        window, device = self._connected_window()
        item = _find_item(window.device_tree, "rtrA")
        self.assertIsNotNone(item, "前提: ツリーに rtrA がある")
        action = _grab_tools_action(window.device_tree, item, "キープアライブ開始")
        self.assertIsNotNone(action, "前提: 「キープアライブ開始」の項目ができた")

        # メニューを開いたまま、相手機器が切断した
        FakeSSH.instances[-1].disconnected.emit()
        self._pump()
        self.assertNotIn("rtrA", window.connections, "前提: 切断されている")

        action.trigger()
        self._pump(0.1)

        self.assertFalse(window.macro_manager.is_keepalive_active("rtrA"),
                         "切断済みのセッションでキープアライブが始まった")
        self.assertFalse(
            window.terminal_widget.tools_state_for("rtrA")["keepalive_active"],
            "タブ側にも「動作中」の印が付いた（ツリーからは止められない）")
        message = window.status_bar.currentMessage()
        self.assertIn("rtrA", message, "どの機器の話か分からない: %r" % message)
        self.assertIn("接続", message,
                      "切れていて実行できないことを知らせていない: %r" % message)

        # 繋ぎ直して送信コールバックが戻っても、残ったタイマーが CR を送らないこと
        window._on_connect_requested(device)
        self._pump()
        timer = window.macro_manager._keepalive_timers.get("rtrA")
        if timer is not None:
            timer.timeout.emit()
            self._pump(0.1)
        self.assertEqual(FakeSSH.instances[-1].sent, [],
                         "新しいセッションへキープアライブの CR が送られた")

    def test_stopping_keepalive_for_a_dropped_session_is_reported(self):
        """切断済みの機器で「キープアライブ停止」を選んでも、黙って通らないこと。"""
        window, _device = self._connected_window()
        item = _find_item(window.device_tree, "rtrA")
        window._on_keepalive_start_requested("rtrA")
        self.assertTrue(window.macro_manager.is_keepalive_active("rtrA"),
                        "前提: 接続中はキープアライブを開始できる")
        action = _grab_tools_action(window.device_tree, item, "キープアライブ停止")
        self.assertIsNotNone(action, "前提: 「キープアライブ停止」の項目ができた")

        FakeSSH.instances[-1].disconnected.emit()
        self._pump()
        self.assertNotIn("rtrA", window.connections, "前提: 切断されている")
        window.status_bar.clearMessage()

        action.trigger()
        self._pump(0.1)

        self.assertFalse(window.macro_manager.is_keepalive_active("rtrA"),
                         "切断時の後始末でキープアライブは止まっているはず")
        message = window.status_bar.currentMessage()
        self.assertIn("rtrA", message, "どの機器の話か分からない: %r" % message)
        self.assertIn("接続", message,
                      "切れていて実行できないことを知らせていない: %r" % message)

    def test_keepalive_still_starts_while_the_session_is_alive(self):
        """繋がっている間は、これまでどおり「ツール」から開始・停止できること。"""
        window, _device = self._connected_window()
        item = _find_item(window.device_tree, "rtrA")
        start = _grab_tools_action(window.device_tree, item, "キープアライブ開始")
        start.trigger()
        self._pump(0.1)
        self.assertTrue(window.macro_manager.is_keepalive_active("rtrA"),
                        "接続中なのにキープアライブを開始できない")
        self.assertTrue(
            window.terminal_widget.tools_state_for("rtrA")["keepalive_active"],
            "タブ側の「動作中」の印が付かない")

        stop = _grab_tools_action(window.device_tree, item, "キープアライブ停止")
        self.assertIsNotNone(stop, "前提: 開始後の「ツール」は停止を出す")
        stop.trigger()
        self._pump(0.1)
        self.assertFalse(window.macro_manager.is_keepalive_active("rtrA"),
                         "接続中なのにキープアライブを止められない")


if __name__ == "__main__":
    unittest.main()
