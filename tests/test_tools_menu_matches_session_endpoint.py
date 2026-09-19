"""接続先リストの「ツール」が、選んだ項目と同じ接続先のセッションだけを操作することを検証する。

何が起きていたか（実測）: 「ツール」の状態取得も実行要求も機器名だけで
行い、選んだ項目の接続先とセッションの接続先を照合していなかった。
COM3 が未検出の間に SSH 機器を「COM3」という名前で登録して接続し、その後
COM3 の USB アダプタを挿すと、自動検出側の COM3 の「ツール」が SSH セッション
の状態で有効になり、そこで選んだマクロは SSH 機器（192.0.2.10）へ送られた。
逆に、自動検出の COM3 へ繋いでいる間は、登録機器「COM3」の「ツール」が
シリアルのセッションを操作した。

利用者の決定（2026-09-20）: 項目の接続先（自動検出か登録か、プロトコル、
ホスト/ポート）と、その名前のセッションの接続先（MainWindow の device_info
の写し）が一致するときだけ「ツール」を有効にし、一致しなければ灰色にする。

実装: DeviceTree に set_tools_target_check(check) を足し、「ツール」を
作るときに check(機器名, 項目の機器データ) を聞く。MainWindow は
_endpoint_of() で項目と device_info の写しの接続先を同じ読み方で取り出し
（_connect_ssh / _connect_telnet / _connect_serial が使う値と同じ）、
一致するかを返す関数を登録する。
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

# 検出されているシリアルポート（テストの途中で差し替える）
PORTS = []


def _detected_ports():
    return [dict(p) for p in PORTS]


def _open_device_menu(tree, device_item):
    """機器の右クリックメニューを開いたつもりで、項目を集める。

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
        return None

    pos = tree.tree.visualItemRect(device_item).center()
    with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
        tree._show_context_menu(pos)
    return captured["items"]


def _find_item(tree, group, name):
    root = tree.tree.invisibleRootItem()
    for i in range(root.childCount()):
        group_item = root.child(i)
        if group_item.text(0) != group:
            continue
        for j in range(group_item.childCount()):
            item = group_item.child(j)
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, dict) and data.get("name") == name:
                return item
    return None


class FakeConnection(QObject):
    """接続の見た目だけを持つ偽物。connect() は即座に成功を知らせる。"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)
    send_drained = pyqtSignal()
    instances = []

    def __init__(self, *args):
        parent = args[-1] if args and isinstance(args[-1], QObject) else None
        super().__init__(parent)
        self.args = args
        self.client = None
        self.sent = []
        FakeConnection.instances.append(self)

    def set_terminal_size(self, cols, rows):
        pass

    def set_baudrate(self, baudrate):
        return True

    def has_pending_sends(self):
        return False

    def connect(self):
        self.connected.emit()
        return True

    def send_command(self, command):
        self.sent.append(command)

    def dispose(self):
        pass

    def disconnect(self):
        pass


def _ssh(name, host="192.0.2.10", **extra):
    data = {"name": name, "host": host, "port": 22, "protocol": "ssh",
            "username": "u", "password": "", "ssh_key": "", "macros": []}
    data.update(extra)
    return data


class ToolsMatchTheSessionEndpointTest(unittest.TestCase):
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
        PORTS[:] = []
        FakeConnection.instances = []
        self.dir = tempfile.mkdtemp(prefix="netbelt-tools-endpoint-")
        for patch in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=Path(self.dir)),
                mock.patch("ui.device_tree.list_serial_ports",
                           side_effect=_detected_ports),
                mock.patch("ui.main_window.SSHConnection", FakeConnection),
                mock.patch("ui.main_window.TelnetConnection", FakeConnection),
                mock.patch("ui.main_window.SerialConnection", FakeConnection)):
            patch.start()
            self.addCleanup(patch.stop)

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _window(self, devices=()):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        # 窓ごとに別の設定ファイルを使う（subTest で窓を作り直すため）
        config_dir = tempfile.mkdtemp(dir=self.dir)
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(config_dir, "config.json"))
            window = MainWindow()
        type(self)._windows.append(window)
        for device in devices:
            self.assertTrue(window.config_manager.add_device("Default", dict(device)),
                            "前提: 機器を登録できた")
        window._load_devices()
        return window

    def _connect(self, window, group, name):
        item = _find_item(window.device_tree, group, name)
        self.assertIsNotNone(item, "前提: %s / %s がツリーに無い" % (group, name))
        window._on_connect_requested(item.data(0, Qt.ItemDataRole.UserRole))
        self._pump()
        self.assertTrue(window.terminal_widget._terminals[name].can_send_input(),
                        "前提: 接続できている")

    def _tools_enabled(self, window, group, name):
        item = _find_item(window.device_tree, group, name)
        self.assertIsNotNone(item, "前提: %s / %s がツリーに無い" % (group, name))
        return _open_device_menu(window.device_tree, item)["ツール"][0]

    def test_a_port_detected_later_does_not_get_the_ssh_sessions_tools(self):
        """後から挿した COM3 の「ツール」は、SSH 機器「COM3」のセッションで有効にならないこと。"""
        window = self._window([_ssh("COM3")])
        self._connect(window, "Default", "COM3")

        PORTS[:] = [{"port": "COM3", "description": "USB Serial Port"}]
        window.device_tree.refresh_serial_ports()

        self.assertFalse(self._tools_enabled(window, "コンソール接続", "COM3"),
                         "自動検出の COM3 から SSH 機器のセッションを操作できる")
        self.assertTrue(self._tools_enabled(window, "Default", "COM3"),
                        "繋いだ SSH 機器自身の「ツール」まで灰色になった")

    def test_the_registered_item_does_not_get_the_consoles_tools(self):
        """自動検出の COM3 へ繋いでいる間、登録機器「COM3」の「ツール」は灰色であること。"""
        PORTS[:] = [{"port": "COM3", "description": "USB Serial Port"}]
        window = self._window([_ssh("COM3")])
        self._connect(window, "コンソール接続", "COM3")

        self.assertFalse(self._tools_enabled(window, "Default", "COM3"),
                         "登録機器の項目からシリアルのセッションを操作できる")
        self.assertTrue(self._tools_enabled(window, "コンソール接続", "COM3"),
                        "繋いだ COM3 自身の「ツール」まで灰色になった")

    def test_an_item_whose_endpoint_differs_from_the_session_is_grey(self):
        """項目の接続先がセッションと違えば灰色、接続先以外の違いなら有効のままであること。"""
        cases = [
            ("ホスト", dict(host="192.0.2.20"), False),
            ("ポート", dict(port=2222), False),
            ("プロトコル", dict(protocol="telnet"), False),
            ("パスワード", dict(password="changed"), True),
            ("ユーザー名", dict(username="admin"), True),
            ("ポートの型だけ", dict(port="22"), True),
        ]
        for label, change, expected in cases:
            with self.subTest(label):
                window = self._window([_ssh("rtr1")])
                self._connect(window, "Default", "rtr1")
                # 接続したあとで config の項目だけが別の接続先を指す状態を作る
                self.assertTrue(window.config_manager.update_device(
                    "Default", "rtr1", "Default", _ssh("rtr1", **change)))
                window._load_devices()

                self.assertEqual(self._tools_enabled(window, "Default", "rtr1"),
                                 expected)


class DeviceTreeTargetCheckTest(unittest.TestCase):
    """DeviceTree 単体: 項目のデータで接続先の一致を聞き、合わなければ灰色にする。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    CONNECTED = {"connected": True, "keepalive_active": False,
                 "command_list_active": False, "macros": []}

    def _tree(self, check):
        from ui.device_tree import DeviceTree
        tree = DeviceTree()
        self.addCleanup(tree.close)
        tree.load_from_config([{"name": "Lab", "devices": [_ssh("rtrA")]}])
        tree.set_tools_state_provider(lambda name: dict(self.CONNECTED))
        tree.set_tools_target_check(check)
        return tree, tree.tree.topLevelItem(0).child(0)

    def test_the_check_gets_the_items_data_and_can_grey_the_tools(self):
        asked = []

        def check(name, data):
            asked.append((name, data.get("host"), data.get("protocol")))
            return False

        tree, item = self._tree(check)
        self.assertFalse(_open_device_menu(tree, item)["ツール"][0],
                         "接続先が合わないのに「ツール」を選べる")
        self.assertEqual(asked, [("rtrA", "192.0.2.10", "ssh")])

    def test_a_matching_check_keeps_the_tools(self):
        tree, item = self._tree(lambda name, data: True)
        self.assertTrue(_open_device_menu(tree, item)["ツール"][0])


if __name__ == "__main__":
    unittest.main()
