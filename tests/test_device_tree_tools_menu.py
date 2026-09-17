"""接続先リストで機器を右クリックしたとき、「ツール」からキープアライブと
マクロを操作できることを検証する。

キープアライブ開始/停止・マクロ実行・マクロ停止は端末の右クリックメニューに
あったが、端末の右クリックは Tera Term と同じく貼り付けにする（利用者判断
2026-09-17）。そこで接続先リストの機器メニューへ「ツール」として移す。

  - 接続していない機器では「ツール」を灰色にして選べないようにする（隠さない）
  - 全ログ保存・ログ記録はメニューバーの「ログ」にあるので、ここへは入れない
  - 「すべて選択」は廃止
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


def _open_device_menu(tree, device_item, choose=None):
    """機器の右クリックメニューを開いたつもりで、項目を集める。

    Args:
        choose: 選ぶ項目の文字列の並び（例: ["ツール", "マクロ停止"]）。
            None なら何も選ばずに閉じる。

    Returns:
        {文字列: (有効か, サブメニューの同じ形の辞書 or None)}
    """
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
        if choose is None:
            return None
        current = menu
        for text in choose:
            action = next(a for a in current.actions() if a.text() == text)
            if action.menu() is not None:
                current = action.menu()
        # 本物の exec と同じく、選ばれた項目の triggered を出してから返す
        action.trigger()
        return action

    pos = tree.tree.visualItemRect(device_item).center()
    with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
        tree._show_context_menu(pos)
    return captured["items"]


class DeviceTreeToolsMenuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _tree(self, state=None):
        """rtrA（SSH）を 1 台持つ接続先リスト。state は機器の状態を返す辞書。"""
        from ui.device_tree import DeviceTree
        tree = DeviceTree()
        self.addCleanup(tree.close)
        tree.load_from_config([
            {"name": "Lab", "devices": [
                {"name": "rtrA", "host": "192.0.2.10", "protocol": "ssh"},
            ]},
        ])
        if state is not None:
            asked = []

            def provider(name):
                asked.append(name)
                return dict(state)

            tree.set_tools_state_provider(provider)
            self.asked = asked
        device = tree.tree.topLevelItem(0).child(0)
        return tree, device

    CONNECTED = {"connected": True, "keepalive_active": False,
                 "command_list_active": False,
                 "macros": [{"name": "show-run", "description": "設定確認"},
                            {"name": "show-ver", "description": ""}]}

    def test_a_device_that_is_not_connected_has_grey_tools(self):
        """接続していない機器では「ツール」が灰色で、選べないこと。"""
        tree, device = self._tree(dict(self.CONNECTED, connected=False))
        items = _open_device_menu(tree, device)
        self.assertIn("ツール", items, "機器メニューに「ツール」が無い: %s"
                      % sorted(items))
        self.assertFalse(items["ツール"][0], "未接続なのに「ツール」を選べる")
        self.assertEqual(self.asked, ["rtrA"], "この機器の状態を聞いていない")

    def test_tools_are_grey_until_someone_tells_the_tree_the_state(self):
        """状態を教わっていなければ、灰色で出すこと（落ちない）。"""
        tree, device = self._tree()
        items = _open_device_menu(tree, device)
        self.assertIn("ツール", items)
        self.assertFalse(items["ツール"][0])

    def test_a_connected_device_offers_keepalive_and_macros(self):
        """接続中は、キープアライブ開始とマクロ実行の一覧が選べること。"""
        tree, device = self._tree(self.CONNECTED)
        enabled, tools = _open_device_menu(tree, device)["ツール"]
        self.assertTrue(enabled)
        self.assertEqual(tools["キープアライブ開始"][0], True)
        self.assertNotIn("キープアライブ停止", tools)
        macros = tools["マクロ実行"][1]
        self.assertEqual(sorted(macros), ["show-run - 設定確認", "show-ver"])
        self.assertNotIn("マクロ停止", tools, "動いていないのに止める項目がある")

    def test_running_keepalive_and_macro_offer_stop_items(self):
        """動いているときは、キープアライブ停止とマクロ停止が出ること。"""
        tree, device = self._tree(dict(self.CONNECTED, keepalive_active=True,
                                       command_list_active=True))
        tools = _open_device_menu(tree, device)["ツール"][1]
        self.assertIn("キープアライブ停止", tools)
        self.assertNotIn("キープアライブ開始", tools)
        self.assertIn("マクロ停止", tools)

    def test_log_items_and_select_all_are_not_under_tools(self):
        """ログ系（メニューバーにある）と「すべて選択」（廃止）は入れないこと。"""
        tree, device = self._tree(self.CONNECTED)
        tools = _open_device_menu(tree, device)["ツール"][1]
        for gone in ("全ログ保存", "ログ記録開始", "ログ記録停止",
                     "すべて選択", "コピー", "貼り付け"):
            self.assertNotIn(gone, tools)

    def test_a_console_device_has_tools_too(self):
        """コンソール（シリアル）の機器にも「ツール」があること。"""
        from ui.device_tree import DeviceTree
        tree = DeviceTree()
        self.addCleanup(tree.close)
        tree.load_from_config([
            {"name": "Lab", "devices": [
                {"name": "sw-console", "type": "serial", "protocol": "serial",
                 "host": "COM9", "port": "COM9", "baudrate": 9600},
            ]},
        ])
        tree.set_tools_state_provider(lambda name: dict(self.CONNECTED))
        device = tree.tree.topLevelItem(0).child(0)
        items = _open_device_menu(tree, device)
        self.assertIn("ツール", items)
        self.assertTrue(items["ツール"][0])

    def test_choosing_an_item_asks_for_it_on_that_device(self):
        """選んだ項目の要求が、その機器の名前つきで出ること。"""
        cases = [
            (self.CONNECTED, ["ツール", "キープアライブ開始"],
             "keepalive_start_requested", ("rtrA",)),
            (dict(self.CONNECTED, keepalive_active=True),
             ["ツール", "キープアライブ停止"],
             "keepalive_stop_requested", ("rtrA",)),
            (self.CONNECTED, ["ツール", "マクロ実行", "show-run - 設定確認"],
             "macro_execute_requested", ("rtrA", "show-run")),
            (dict(self.CONNECTED, command_list_active=True),
             ["ツール", "マクロ停止"],
             "macro_stop_requested", ("rtrA",)),
        ]
        for state, path, signal, expected in cases:
            with self.subTest(path=path):
                tree, device = self._tree(state)
                got = []
                getattr(tree, signal).connect(lambda *a: got.append(a))
                _open_device_menu(tree, device, choose=path)
                self.assertEqual(got, [expected])


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


class ToolsMenuDrivesTheSessionTest(unittest.TestCase):
    """メインウィンドウの配線を通して、実際に接続中のセッションが動くこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(tempfile.mkdtemp(prefix="netbelt-tools-")))
        ap.start()
        self.addCleanup(ap.stop)
        FakeSSH.instances = []
        ssh_patch = mock.patch("ui.main_window.SSHConnection", FakeSSH)
        ssh_patch.start()
        self.addCleanup(ssh_patch.stop)

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _connected_window(self):
        from ui.main_window import MainWindow
        with mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            window = MainWindow()
        self.addCleanup(window.close)
        device = {"name": "dev", "host": "192.0.2.10", "port": 22,
                  "username": "u", "password": "", "protocol": "ssh"}
        window.device_tree.load_from_config([{"name": "Lab", "devices": [device]}])
        window._on_connect_requested(device)
        self._pump()
        terminal = window.terminal_widget._terminals["dev"]
        self.assertTrue(terminal.can_send_input(), "前提: 接続できている")
        conn = FakeSSH.instances[-1]
        del conn.sent[:]
        item = window.device_tree.tree.topLevelItem(0).child(0)
        return window, terminal, conn, item

    def test_macro_stop_from_the_device_list_stops_the_running_macro(self):
        """実行中のマクロを「ツール → マクロ停止」で止められること。"""
        window, terminal, conn, item = self._connected_window()
        window.macro_manager.start_command_list("dev", ["cmd1", "cmd2", "cmd3"], 200)
        self.assertEqual(conn.sent, ["cmd1\r"], "前提: 最初のコマンドは即送られる")

        _open_device_menu(window.device_tree, item, choose=["ツール", "マクロ停止"])

        self.assertFalse(window.macro_manager.is_command_list_active("dev"))
        self._pump(0.6)
        self.assertEqual(conn.sent, ["cmd1\r"],
                         "停止したのに残りのコマンドが送られた: %r" % conn.sent)
        tools = _open_device_menu(window.device_tree, item)["ツール"][1]
        self.assertNotIn("マクロ停止", tools, "止めた後も項目が残っている")

    def test_keepalive_from_the_device_list_starts_and_stops(self):
        """「ツール → キープアライブ開始/停止」がその機器に効くこと。"""
        window, terminal, conn, item = self._connected_window()

        _open_device_menu(window.device_tree, item,
                          choose=["ツール", "キープアライブ開始"])
        self.assertTrue(window.macro_manager.is_keepalive_active("dev"))
        tools = _open_device_menu(window.device_tree, item)["ツール"][1]
        self.assertIn("キープアライブ停止", tools)

        _open_device_menu(window.device_tree, item,
                          choose=["ツール", "キープアライブ停止"])
        self.assertFalse(window.macro_manager.is_keepalive_active("dev"))

    def test_tools_turn_grey_while_waiting_to_reconnect(self):
        """切断されて再接続待ちになったら、「ツール」は灰色になること。"""
        window, terminal, conn, item = self._connected_window()
        self.assertTrue(_open_device_menu(window.device_tree, item)["ツール"][0])

        terminal.set_reconnect_mode(True)

        self.assertFalse(_open_device_menu(window.device_tree, item)["ツール"][0])


if __name__ == "__main__":
    unittest.main()
