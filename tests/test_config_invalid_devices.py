"""name/host の無い機器を含む config.json で起動が落ちないことを検証する。

ConfigManager._load_config は復号と Default グループの確認しかせず、機器の
必須フィールド（name/host）を検証しない。手編集や他ツールで作られた
config.json に {} や {"host": ...} のような機器が混ざると load_error 無しで
受理され、MainWindow 構築時の DeviceTree.load_from_config が KeyError で落ちる
（実測: 「予期しないエラー KeyError: 'name'」のダイアログが出て起動しない）。
JSON 構文エラー用の「バックアップ＋既定設定で続行」の経路には乗らないので、
利用者には config.json が原因だと分からない。

不正な機器だけを一覧から外し（隔離）、元ファイルはバックアップしたうえで
警告として知らせる。正常な機器はそのまま使える。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

VALID = {
    "name": "ルータA", "host": "192.0.2.1", "port": 22, "protocol": "ssh",
    "username": "admin", "password": "", "ssh_key": "", "macros": [],
}
BROKEN = [
    {},
    {"host": "192.0.2.2"},
    {"name": "hostless"},
    {"name": "", "host": "192.0.2.3"},
]


def _write_config(path, devices):
    config = {
        "config_version": "1.0",
        "groups": [{"name": "Default", "auto_commands": [], "devices": devices}],
        "global_macros": [],
        "settings": {},
        "update_settings": {"check_on_startup": False},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)


class ConfigManagerQuarantinesInvalidDevicesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-baddev-")
        self.path = os.path.join(self.dir, "config.json")
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)

    def test_invalid_devices_are_kept_out_of_the_groups_with_a_warning(self):
        from core.config_manager import ConfigManager
        _write_config(self.path, BROKEN[:2] + [VALID] + BROKEN[2:])

        cm = ConfigManager(config_path=self.path)

        self.assertEqual(cm.get_groups()[0]["devices"], [VALID],
                         "不正な機器が一覧に残っている")
        self.assertIsNone(cm.load_error, "構文エラー扱い（既定設定へ退避）にしてはいけない")
        self.assertTrue(cm.load_warning, "警告が記録されていない")
        self.assertIn(str(len(BROKEN)), cm.load_warning)
        # 元のファイルは手を付けずにバックアップされる
        self.assertTrue(cm.backup_path and os.path.exists(cm.backup_path),
                        "元ファイルのバックアップが無い")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["groups"][0]["devices"]), 5,
                             "読み込みだけで元ファイルが書き換わった")

    def test_valid_config_raises_no_warning(self):
        from core.config_manager import ConfigManager
        _write_config(self.path, [VALID])

        cm = ConfigManager(config_path=self.path)

        self.assertEqual(cm.get_groups()[0]["devices"], [VALID])
        self.assertIsNone(cm.load_warning)
        self.assertIsNone(cm.backup_path)


class MainWindowSurvivesInvalidDevicesTest(unittest.TestCase):
    _windows = []   # 配送待ちシグナルの宛先を先に解放しない（他テストと同じ理由）

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        cls._windows.clear()

    def setUp(self):
        # MainWindow は CWD の config.json を読むので、一時ディレクトリへ移る
        self.dir = tempfile.mkdtemp(prefix="netbelt-baddev-win-")
        self.prev_cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self.prev_cwd)
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)

    def test_window_builds_shows_a_warning_and_lists_only_valid_devices(self):
        _write_config(os.path.join(self.dir, "config.json"), BROKEN + [VALID])
        from ui.main_window import MainWindow

        with mock.patch("PyQt6.QtWidgets.QMessageBox.warning") as warning:
            w = MainWindow()          # 修正前はここで KeyError: 'name'
        self._windows.append(w)

        self.assertEqual(warning.call_count, 1, warning.call_args_list)
        self.assertIn("機器", warning.call_args.args[2])
        group_item = w.device_tree.tree.topLevelItem(0)
        self.assertEqual(group_item.text(0), "Default")
        self.assertEqual(group_item.childCount(), 1)
        self.assertEqual(group_item.child(0).text(0), "ルータA (192.0.2.1)")


if __name__ == "__main__":
    unittest.main()
