"""同名グループの案内が、実際に直せる手段を指していることを検証する。

実測（16101ef）: 同じ名前のグループが複数ある config.json を読むと
「config.json でグループ名を分けてください」とだけ案内する。根拠として
コミット本文と _notify_duplicate_group_names の docstring には「rename_group も
get_group 経由で 1 つ目しか掴めないため」と書かれているが、これは実測と違う。
掴めるのが 1 つ目だというのは本当でも、その 1 つ目を別の名前にすれば重複は
解ける。接続先リストで右クリック →「グループを編集」→ 名前を変える、で
画面から直せる（下の test_renaming_from_the_group_edit_really_separates_them
が実際に MainWindow._on_edit_group を通して確かめる）。

設定ファイルを手で開くよう案内するのは、画面でできることに対して重い。
案内を「グループを編集」で直せる旨に変え、どちらが変わるのか（名前で探すので
先に並んでいる方）も添える。config.json を直接直す道も残して書く。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


def _device(name, host):
    return {"name": name, "host": host, "port": 22, "protocol": "ssh",
            "username": "admin", "password": "", "ssh_key": "", "macros": []}


def _group(name, devices):
    return {"name": name, "auto_commands": [], "devices": list(devices)}


DUPLICATED = [
    _group("Default", []),
    _group("kyoten", [_device("rtr1", "192.0.2.11")]),
    _group("kyoten", [_device("rtr2", "192.0.2.12")]),
]


class DuplicateGroupNoticePointsAtEditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-dupnotice-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config_manager(self):
        from core.config_manager import ConfigManager
        path = self.dir / "config.json"
        path.write_text(json.dumps({
            "config_version": "1.0",
            "groups": DUPLICATED,
            "global_macros": [],
            "settings": {},
        }), encoding="utf-8")
        return ConfigManager(config_path=str(path))

    def test_the_notice_points_at_the_group_edit_menu(self):
        """案内が、画面の「グループを編集」で直せることに触れること。"""
        cm = self._config_manager()

        self.assertIsNotNone(cm.load_warning)
        self.assertIn("グループを編集", cm.load_warning,
                      "画面で直せることを案内していない: %s" % cm.load_warning)

    def test_the_notice_still_offers_the_config_file(self):
        """config.json を直接直す道も、案内に残すこと。"""
        cm = self._config_manager()

        self.assertIn("config.json", cm.load_warning)

    def test_renaming_from_the_group_edit_really_separates_them(self):
        """前提の確認: 画面の「グループを編集」で重複が解けること。"""
        from PyQt6.QtWidgets import QDialog
        from ui.main_window import MainWindow
        cm = self._config_manager()
        dialog = mock.MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_group_name.return_value = "kyoten-A"
        dialog.get_auto_commands.return_value = []

        with mock.patch("ui.main_window.ConfigManager", return_value=cm), \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            window = MainWindow()
        self.addCleanup(window.close)
        with mock.patch("ui.main_window.GroupDialog", return_value=dialog), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window._on_edit_group("kyoten")

        self.assertFalse(warn.called, "改名が断られている")
        self.assertEqual([g["name"] for g in cm.get_groups()],
                         ["Default", "kyoten-A", "kyoten"],
                         "先に並んでいる方の名前が変わっていない")
        # 重複が解けたので、2 つ目にいた機器にも操作が届く
        self.assertTrue(cm.update_device("kyoten", "rtr2", "kyoten",
                                         _device("rtr2", "192.0.2.99")))

    def test_the_notice_says_which_group_changes(self):
        """名前で探すので、変わるのは先に並んでいる方だと添えること。"""
        cm = self._config_manager()

        self.assertIn("先に並んでいる", cm.load_warning,
                      "どちらが変わるのかを言っていない: %s" % cm.load_warning)


if __name__ == "__main__":
    unittest.main()
