"""add_group() と接続直前の送信が、壊れた auto_commands を通さないことを検証する。

実測（81664d2）: グループの auto_commands は「読み込み」と
set_group_auto_commands() の 2 経路では文字列だけの list かを検査している
のに、ConfigManager.add_group() は検査せず `"auto_commands": auto_commands or []`
でそのまま config.json へ書いていた。検査役の実測:

    add_group('Lab', 'show version')  -> True
    ディスク上の auto_commands       -> 'show version'
    list() したもの                  -> ['s', 'h', 'o', 'w', ' ', ...]
    add_group('Lab2', {'a': 1})       -> True / ディスク -> {'a': 1}
    （同じ ConfigManager で set_group_auto_commands('Lab', 'show ver') は
      False で正しく断る）

そのセッションの間は MainWindow._run_auto_commands が
group.get('auto_commands') をそのまま list() して MacroManager へ渡すので、
その機器へ接続した瞬間に 1 文字ずつが実機へ送られる。隔離が効くのは次回
起動時（再読み込みで [] に直り警告が出る）だけだった。

直し方: 同じ値を書く 3 経路をそろえる。add_group() の先頭でも
_is_valid_auto_commands() を見て、外れていれば set_group_auto_commands と
同じく理由を print して False を返す（グループは作らない）。あわせて
MainWindow._run_auto_commands でも送信を仕掛ける直前に同じ検査をして、
すでにメモリへ入ってしまった壊れた値でも実機へ送らないようにする。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

DEVICE = {
    "name": "rtr-01", "host": "192.0.2.1", "port": 22, "protocol": "ssh",
    "username": "admin", "password": "", "ssh_key": "", "macros": [],
}

BROKEN = [
    ("a string", "show version"),
    ("a dict", {"show version": 1}),
    ("a number", 5),
    ("a list with a non-string", ["show version", 5]),
]


class AddGroupAutoCommandsTypeTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-addgrp-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "config.json"

    def _manager(self):
        from core.config_manager import ConfigManager
        return ConfigManager(config_path=str(self.path))

    def _on_disk_group(self, name):
        config = json.loads(self.path.read_text(encoding="utf-8"))
        for group in config.get("groups", []):
            if group.get("name") == name:
                return group
        return None

    def test_a_broken_auto_commands_is_refused(self):
        """文字列・辞書・数値・非文字列を含む list は受け付けないこと。"""
        cm = self._manager()
        for label, value in BROKEN:
            with self.subTest(label):
                name = "Lab-%s" % label.replace(" ", "-")
                self.assertFalse(
                    cm.add_group(name, value),
                    "壊れた auto_commands (%s) のグループが作られた" % label)
                self.assertIsNone(cm.get_group(name),
                                  "断ったグループがメモリに残っている")
                self.assertIsNone(self._on_disk_group(name),
                                  "断ったグループがディスクに書かれている")

    def test_the_refusal_names_the_group(self):
        """断った理由を、グループ名を挙げて知らせること。"""
        cm = self._manager()
        printed = []
        with mock.patch("builtins.print",
                        side_effect=lambda *a, **k: printed.append(
                            " ".join(str(x) for x in a))):
            cm.add_group("Lab", "show version")
        self.assertTrue(
            any("Lab" in line and "自動実行コマンド" in line
                for line in printed),
            "どのグループを断ったのかが分からない: %r" % (printed,))

    def test_a_valid_auto_commands_is_accepted(self):
        """正しい list は今までどおり受けること。"""
        cm = self._manager()
        self.assertTrue(cm.add_group("Lab", ["terminal length 0", "show clock"]))
        self.assertEqual(cm.get_group("Lab")["auto_commands"],
                         ["terminal length 0", "show clock"])
        self.assertEqual(self._on_disk_group("Lab")["auto_commands"],
                         ["terminal length 0", "show clock"])

    def test_an_omitted_auto_commands_is_accepted(self):
        """省略（None）と空リストは「無し」なので今までどおり受けること。"""
        cm = self._manager()
        self.assertTrue(cm.add_group("Lab"))
        self.assertEqual(cm.get_group("Lab")["auto_commands"], [])
        self.assertTrue(cm.add_group("Lab2", []))
        self.assertEqual(cm.get_group("Lab2")["auto_commands"], [])

    def test_a_duplicate_group_name_is_still_refused(self):
        """同名グループを断る今までの判定を壊していないこと。"""
        cm = self._manager()
        self.assertTrue(cm.add_group("Lab", ["show clock"]))
        self.assertFalse(cm.add_group("Lab", ["show version"]))


class RunAutoCommandsGuardTest(unittest.TestCase):
    """メモリに壊れた auto_commands が入っていても、実機へ送らないこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 破棄済みウィジェットへのシグナル配送で落ちるため保持する
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-runauto-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            window = MainWindow()
        type(self)._windows.append(window)
        self.assertTrue(window.config_manager.add_group("Lab", ["show clock"]),
                        "前提: グループを作れた")
        self.assertTrue(window.config_manager.add_device("Lab", dict(DEVICE)),
                        "前提: 機器を登録できた")
        return window

    def _scheduled(self, window, auto_commands):
        """auto_commands をメモリへ直接差し込んで _run_auto_commands を通す。"""
        from PyQt6.QtCore import QTimer
        window.config_manager.get_group("Lab")["auto_commands"] = auto_commands
        window.connections["rtr-01"] = object()
        self.addCleanup(window.connections.pop, "rtr-01", None)
        window.device_info["rtr-01"] = dict(DEVICE)
        with mock.patch.object(QTimer, "singleShot") as single_shot:
            window._run_auto_commands("rtr-01")
        return single_shot.called

    def test_a_broken_auto_commands_is_not_sent(self):
        window = self._window()
        for label, value in BROKEN:
            with self.subTest(label):
                self.assertFalse(
                    self._scheduled(window, value),
                    "壊れた auto_commands (%s) の送信が仕掛けられた" % label)

    def test_a_valid_auto_commands_is_still_sent(self):
        window = self._window()
        self.assertTrue(self._scheduled(window, ["terminal length 0"]),
                        "正しい auto_commands が送られなくなっている")


if __name__ == "__main__":
    unittest.main()
