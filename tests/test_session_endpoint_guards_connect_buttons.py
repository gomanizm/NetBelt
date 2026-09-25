"""接続先が食い違う項目からは、その名前のセッションを切断も乗っ取りもできないことを検証する。

何が起きていたか（実測）: 名前が同じで接続先が違う項目（COM3 が未検出の
うちに「COM3」という名前で登録した SSH 機器と、あとで挿した自動検出の
COM3）が並ぶと、機器名だけを見る 2 つの経路がそのセッションを掴んだ。

  (1) 「切断」ボタン (_on_disconnect_button_clicked) は device_data['name']
      で self.connections を引いて _on_tab_closed を呼ぶので、自動検出側の
      COM3 を選んで押すと無関係な SSH セッションが切れた（「ツール」側の
      照合 _tools_target_matches は False を返す組み合わせ）。
  (2) SSH「COM3」が切断されてタブとログ記録だけが残っている状態で自動検出
      COM3 へ接続すると、SSH 用に開いた記録ファイルへシリアルの出力が入り、
      device_info['COM3'] も自動検出側の写しで上書きされて、そのタブの
      Enter 再接続が以後シリアルへ向いた。

利用者の決定（2026-09-20）: 断る。接続先が食い違う項目からの切断・接続は
「選んだ項目は、この名前のセッションの接続先と違います」と伝えて何もしない
（「ツール」と同じ判定を使う）。

実装: MainWindow に _session_target_conflict() を足し、その名前のセッション
（タブか接続）があり、接続時の写し device_info と項目の接続先が
_tools_target_matches で一致しないときだけ True を返す。「切断」ボタンと
_on_connect_requested の先頭で聞き、食い違えばステータスバーに伝えて戻る。
写しが無いときは接続先を比べられないので、従来どおり通す。
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
        self.disconnect_calls = 0
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
        self.disconnect_calls += 1


def _ssh(name, host="192.0.2.10", **extra):
    data = {"name": name, "host": host, "port": 22, "protocol": "ssh",
            "username": "u", "password": "", "ssh_key": "", "macros": []}
    data.update(extra)
    return data


class MismatchedItemCannotTouchTheSessionTest(unittest.TestCase):
    """名前だけが同じ項目からの「切断」「接続」を断ること。"""

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
        self.dir = tempfile.mkdtemp(prefix="netbelt-endpoint-guard-")
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
        config_dir = tempfile.mkdtemp(dir=self.dir)
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(config_dir, "config.json"))
            window = MainWindow()
        type(self)._windows.append(window)
        for device in devices:
            self.assertTrue(
                window.config_manager.add_device("Default", dict(device)),
                "前提: 機器を登録できた")
        window._load_devices()
        return window

    def _item_data(self, window, group, name):
        item = _find_item(window.device_tree, group, name)
        self.assertIsNotNone(item, "前提: %s / %s がツリーに無い" % (group, name))
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _connect(self, window, group, name):
        window._on_connect_requested(self._item_data(window, group, name))
        self._pump()
        self.assertTrue(
            window.terminal_widget._terminals[name].can_send_input(),
            "前提: 接続できている")

    def _select(self, window, group, name):
        item = _find_item(window.device_tree, group, name)
        self.assertIsNotNone(item, "前提: %s / %s がツリーに無い" % (group, name))
        window.device_tree.tree.setCurrentItem(item)

    def _plug_com3(self, window):
        PORTS[:] = [{"port": "COM3", "description": "USB Serial Port"}]
        window.device_tree.refresh_serial_ports()

    def test_disconnect_from_a_later_detected_port_leaves_the_ssh_session(self):
        """後から挿した COM3 を選んで「切断」を押しても、SSH セッションは切れないこと。"""
        window = self._window([_ssh("COM3")])
        self._connect(window, "Default", "COM3")
        conn = window.connections["COM3"]
        self._plug_com3(window)

        self._select(window, "コンソール接続", "COM3")
        window.device_tree.btn_disconnect.click()

        self.assertEqual(conn.disconnect_calls, 0,
                         "別の接続先の項目から SSH セッションが切られた")
        self.assertIn("COM3", window.connections,
                      "別の接続先の項目からの切断で接続が消えた")
        self.assertIn("接続先と違います", window.status_bar.currentMessage(),
                      "接続先が違うことを伝えていない")

    def test_disconnect_from_the_matching_item_still_works(self):
        """接続先が一致する項目からの「切断」は、これまでどおり切ること。"""
        window = self._window([_ssh("COM3")])
        self._connect(window, "Default", "COM3")
        conn = window.connections["COM3"]
        self._plug_com3(window)

        self._select(window, "Default", "COM3")
        window.device_tree.btn_disconnect.click()

        self.assertEqual(conn.disconnect_calls, 1,
                         "一致する項目からの切断まで断っている")
        self.assertNotIn("COM3", window.connections)

    def test_connecting_a_different_endpoint_under_a_live_name_is_refused(self):
        """切断済みのタブが残る名前へ、別の接続先の項目から繋がせないこと。"""
        window = self._window([_ssh("COM3")])
        self._connect(window, "Default", "COM3")
        ssh = window.connections["COM3"]
        ssh.disconnected.emit()          # 機器側から切れた（タブは残る）
        self._pump()
        self.assertNotIn("COM3", window.connections, "前提: 接続は外れている")
        self._plug_com3(window)

        window._on_connect_requested(
            self._item_data(window, "コンソール接続", "COM3"))
        self._pump()

        self.assertEqual(len(FakeConnection.instances), 1,
                         "別の接続先の項目から、残ったタブへ繋がれた")
        session = window.device_info["COM3"]
        self.assertNotEqual(session.get("source"), "autodetect",
                            "セッションの再接続用の写しが自動検出側で上書きされた")
        self.assertEqual(session.get("host"), "192.0.2.10")
        self.assertIn("接続先と違います", window.status_bar.currentMessage(),
                      "接続先が違うことを伝えていない")

    def test_reconnecting_the_same_session_still_works(self):
        """同じ接続先（Enter の再接続と同じ写し）なら、これまでどおり繋ぎ直せること。"""
        window = self._window([_ssh("COM3")])
        self._connect(window, "Default", "COM3")
        window.connections["COM3"].disconnected.emit()
        self._pump()
        self._plug_com3(window)

        window._reconnect_device("COM3")
        self._pump()

        self.assertIn("COM3", window.connections, "同じ接続先の再接続まで断っている")
        self.assertEqual(len(FakeConnection.instances), 2)

    def test_a_session_without_a_saved_copy_is_left_alone(self):
        """接続先の写しが無い（比べられない）セッションは、これまでどおり切れること。"""
        window = self._window()
        conn = mock.Mock()
        window.connections["ルータA"] = conn
        window.device_tree.get_selected_device = lambda: (
            "Default", {"name": "ルータA"})

        window.device_tree.btn_disconnect.click()

        self.assertEqual(conn.disconnect.call_count, 1,
                         "写しが無いだけで切断を断っている")
        self.assertNotIn("ルータA", window.connections)


if __name__ == "__main__":
    unittest.main()
