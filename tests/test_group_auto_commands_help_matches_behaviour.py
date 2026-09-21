"""グループ編集ダイアログの自動実行コマンドの説明が、実際の送信先と合っていることを検証する。

何が起きていたか（実測）: 説明は「このグループの機器へ SSH / Telnet で
接続した直後に、上から順に送信されます」だったが、_run_auto_commands が
除外するのは自動検出のポート（source == 'autodetect'）だけで、プロトコルは
見ていない。グループ Lab に auto_commands=['terminal length 0'] を設定し、
protocol='console'（host='COM9'）の機器を登録して接続すると、シリアル側へ
'terminal length 0\\r' が送られた。説明を読んで「コンソール機器には送られ
ない」と考えると、機器の素の CLI へ意図しない行が流れる。

利用者の決定（2026-09-20）: コンソール（シリアル）機器へも送る今の振る舞いは
変えず、説明文を実際に合わせる（自動検出のポートだけ除外される点も書く）。

実装: GroupDialog の auto_commands_help_label の文言だけを直す。送信の
判定（_run_auto_commands）は触らない。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class AutoCommandsHelpTextTest(unittest.TestCase):
    """説明文が実際の送信先を言っていること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _help_text(self):
        from ui.dialogs.group_dialog import GroupDialog
        dialog = GroupDialog(None)
        self.addCleanup(dialog.deleteLater)
        return dialog.auto_commands_help_label.text()

    def test_it_names_console_connections_too(self):
        """コンソール（シリアル）機器へも送ることを書いてあること。"""
        text = self._help_text()
        self.assertIn("コンソール", text,
                      "SSH / Telnet だけのように読めるが、実際はコンソールへも送る")

    def test_it_says_autodetected_ports_are_excluded(self):
        """除外されるのは自動検出のポートだけ、と書いてあること。"""
        text = self._help_text()
        self.assertIn("自動検出", text, "唯一の除外（自動検出のポート）が書かれていない")
        self.assertIn("送信されません", text)


class AutoCommandsTargetsTest(unittest.TestCase):
    """説明文が言うとおりの送信先であること（振る舞いは変えない）。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _window(self, device):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-auto-help-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.device_tree.list_serial_ports", return_value=[]):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            window = MainWindow()
        type(self)._windows.append(window)
        self.assertTrue(window.config_manager.add_group("Lab", ["terminal length 0"]),
                        "前提: グループを作れた")
        self.assertTrue(window.config_manager.add_device("Lab", dict(device)),
                        "前提: 機器を登録できた")
        return window

    def _commands_started(self, window, name, device_info):
        """接続中として _run_auto_commands を通し、送信が始まるかを返す。"""
        from PyQt6.QtCore import QTimer
        window.connections[name] = object()
        self.addCleanup(window.connections.pop, name, None)
        window.device_info[name] = device_info
        with mock.patch.object(QTimer, "singleShot") as single_shot:
            window._run_auto_commands(name)
        return single_shot.called

    def test_a_console_device_receives_them(self):
        """コンソール機器（protocol='console'）へも送られること。"""
        device = {"name": "con1", "host": "COM9", "port": 22,
                  "protocol": "console", "username": "", "password": "",
                  "baudrate": 9600}
        window = self._window(device)
        self.assertTrue(self._commands_started(window, "con1", dict(device)),
                        "コンソール機器へ送らない振る舞いに変わっている")

    def test_an_autodetected_port_does_not(self):
        """自動検出のポートは、どのグループにも属さないので送られないこと。"""
        device = {"name": "COM9", "host": "COM9", "port": 22,
                  "protocol": "console", "username": "", "password": ""}
        window = self._window(device)
        started = self._commands_started(
            window, "COM9",
            {"name": "COM9", "type": "serial", "protocol": "serial",
             "port": "COM9", "baudrate": 9600, "source": "autodetect"})
        self.assertFalse(started, "自動検出のポートへ送られている")


if __name__ == "__main__":
    unittest.main()
