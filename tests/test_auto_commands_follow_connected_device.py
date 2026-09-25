"""同名の機器が 2 つのグループにあるとき、繋いだ機器のグループの自動実行コマンドだけを送ることを検証する。

何が起きていたか（実測、470c538）: 手編集・持ち込みの config.json に G1/R
（192.0.2.11）と G2/R（192.0.2.12）があると、読み込みは警告だけで両方を残す。
タブが無い状態で G2/R へ繋ぐと、MainWindow._find_group_of_device が機器名
だけで全グループを探して先頭の G1 を返し、接続成功の約 800ms 後に G1 の
auto_commands が G2/R の機器へ流れていた。

    connected to G2 host 192.0.2.12 G2 auto_commands= []
    sent: [('192.0.2.12', '! from-G1\\r')]
    connected to G2 host 192.0.2.12 G2 auto_commands= ['! from-G2']
    sent: [('192.0.2.12', '! from-G1\\r')]

G2 の自動実行が空でも、G2 に自分のコマンドがあっても、G1 のものが送られる。
待っている間の再接続は接続オブジェクトの同一性で防いでいるが、コマンドを
どのグループから取るかの誤りは防げていなかった。

どう直したか: その名前の機器が複数のグループにあるときは、接続したときの
機器データの写し（device_info）の接続先で、どのグループの機器かを絞る。
同名の機器を編集・削除・移動で見分けるのと同じ device_endpoint() を使う。
それでも 1 つに決まらない（同じ接続先の同名機器が 2 つのグループにある）
ときは、どちらのコマンドか分からないので送らない。その名前の機器が
1 つのグループにしか無い普通の構成は、今までどおりそのグループのものを送る。

実機へは繋がない。SSHConnection を偽物に差し替え、接続要求から接続成功、
800ms 後の開始、ターミナルの送信キューを通って send_command に届くまでを
そのまま通す。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QObject, pyqtSignal

sys.path.insert(0, "src")


class FakeSSH(QObject):
    """接続の見た目だけを持つ偽物。送られた文字列を接続先ごとに控える。"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)
    instances = []

    def __init__(self, host, port, username, password, ssh_key, parent=None):
        super().__init__(parent)
        self.host = host
        self.client = None
        self.sent = []
        FakeSSH.instances.append(self)

    def set_terminal_size(self, cols, rows):
        pass

    def connect(self):
        # 別スレッドから呼ばれる。成功の通知はテストが GUI スレッドで出す
        return True

    def send_command(self, command):
        self.sent.append(command)

    def dispose(self):
        pass

    def disconnect(self):
        pass


def _device(name, host):
    return {"name": name, "host": host, "port": 22, "protocol": "ssh",
            "username": "admin", "password": "", "ssh_key": "", "macros": []}


class AutoCommandsFollowConnectedDeviceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-autocmd-dup-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        FakeSSH.instances = []
        for patch in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=self.dir),
                mock.patch("ui.main_window.SSHConnection", FakeSSH),
                mock.patch("ui.main_window.QMessageBox")):
            patch.start()
            self.addCleanup(patch.stop)

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _window(self, groups):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        config = {"config_version": "1.0", "groups": groups,
                  "global_macros": [], "settings": {}}
        path = self.dir / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        cm = ConfigManager(config_path=str(path))
        with mock.patch("ui.main_window.ConfigManager", return_value=cm), \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            window = MainWindow()
        self.addCleanup(self._discard, window)
        return window

    @staticmethod
    def _discard(window):
        for name in list(window.connections):
            window.macro_manager.stop_command_list(name)
        window.connections.clear()
        window.terminal_widget._output_timer.stop()
        window.terminal_widget._pending_output.clear()
        window.close()

    def _connect_and_collect(self, window, group_name, expect_send):
        """ツリーから渡るのと同じ機器データで繋ぎ、機器へ送られた文字列を返す"""
        device = dict(next(
            d for d in window.config_manager.get_group(group_name)["devices"]
            if d["name"] == "R"))
        window._on_connect_requested(device)
        conn = window.connections["R"]
        self.assertEqual(conn.host, device["host"], "前提: 選んだ機器へ繋いでいる")
        conn.connected.emit()
        # 開始は 800ms 後。混んでいても待ち切れるよう、送られるはずのときは
        # 届くまで待つ。送られないはずのときは開始の時刻を十分に過ぎるまで待つ
        deadline = time.time() + (8.0 if expect_send else 2.5)
        while time.time() < deadline:
            self._pump(0.05)
            if expect_send and conn.sent:
                break
        self._pump(0.3)   # 余計な行が続けて届かないかも見る
        return conn.sent

    def _duplicate_groups(self, g1_commands, g2_commands):
        return [
            {"name": "Default", "auto_commands": [], "devices": []},
            {"name": "G1", "auto_commands": g1_commands,
             "devices": [_device("R", "192.0.2.11")]},
            {"name": "G2", "auto_commands": g2_commands,
             "devices": [_device("R", "192.0.2.12")]},
        ]

    def test_second_entry_gets_its_own_group_commands(self):
        """2 台目（G2/R）へ繋ぐと、G2 のコマンドだけが送られること。"""
        window = self._window(self._duplicate_groups(["! from-G1"], ["! from-G2"]))
        sent = self._connect_and_collect(window, "G2", expect_send=True)
        self.assertNotIn("! from-G1\r", sent,
                         "1 台目のグループの自動実行コマンドが 2 台目へ送られた")
        self.assertEqual(sent, ["! from-G2\r"])

    def test_second_entry_with_empty_group_commands_gets_nothing(self):
        """G2 の自動実行が空なら、G2/R には何も送られないこと。"""
        window = self._window(self._duplicate_groups(["! from-G1"], []))
        sent = self._connect_and_collect(window, "G2", expect_send=False)
        self.assertEqual(sent, [],
                         "1 台目のグループの自動実行コマンドが 2 台目へ送られた")

    def test_first_entry_gets_its_own_group_commands(self):
        """1 台目（G1/R）へ繋ぐと、G1 のコマンドだけが送られること。"""
        window = self._window(self._duplicate_groups(["! from-G1"], ["! from-G2"]))
        sent = self._connect_and_collect(window, "G1", expect_send=True)
        self.assertEqual(sent, ["! from-G1\r"])

    def test_same_endpoint_in_two_groups_sends_nothing(self):
        """接続先まで同じで、どちらのグループの機器か決まらないときは送らないこと。"""
        groups = self._duplicate_groups(["! from-G1"], ["! from-G2"])
        groups[2]["devices"][0]["host"] = "192.0.2.11"
        window = self._window(groups)
        sent = self._connect_and_collect(window, "G2", expect_send=False)
        self.assertEqual(sent, [],
                         "どちらのグループのものか分からないコマンドが送られた")

    def test_unique_name_still_gets_its_group_commands(self):
        """同名の無い普通の構成は、今までどおりそのグループのコマンドが送られること。"""
        window = self._window([
            {"name": "Default", "auto_commands": [], "devices": []},
            {"name": "G1", "auto_commands": ["! from-G1"],
             "devices": [_device("other", "192.0.2.11")]},
            {"name": "G2", "auto_commands": ["! from-G2"],
             "devices": [_device("R", "192.0.2.12")]},
        ])
        sent = self._connect_and_collect(window, "G2", expect_send=True)
        self.assertEqual(sent, ["! from-G2\r"])

    def test_unique_name_is_found_even_without_a_session_copy(self):
        """名前が 1 つだけなら、接続先を比べずにそのグループを返すこと（今までどおり）。"""
        window = self._window([
            {"name": "Default", "auto_commands": [], "devices": []},
            {"name": "G2", "auto_commands": ["! from-G2"],
             "devices": [_device("R", "192.0.2.12")]},
        ])
        self.assertEqual(window._find_group_of_device("R")["name"], "G2")
        self.assertIsNone(window._find_group_of_device("missing"))


if __name__ == "__main__":
    unittest.main()
