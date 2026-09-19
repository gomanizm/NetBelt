"""存在しないグループの削除を「削除しました」と案内しないことを検証する。

実測（62357d1）: ConfigManager.remove_group() は対象のグループが見つからなくても
何も消さずに save_config() を呼び、その結果の True を返していた。
MainWindow._on_delete_group はこの True を見て、ツリーを作り直したうえで
ステータスバーに「グループ 'X' を削除しました」と出していた。ツリーが設定と
食い違っていた（既に消えたグループが表示に残っていた）場合など、実際には
何も削除していないのに削除できたと利用者に伝わる。

修正: remove_group() は対象が無ければ保存せずに False を返す。
_on_delete_group は、確認ダイアログの前に引いたグループが無かった場合を
「実行中の設定にも適用されていない失敗」として扱い、「保存できませんでした
（このセッション中のみ有効）」ではなく「グループの削除に失敗しました」と知らせる。
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


def _write_config(path, groups):
    config = {
        "config_version": "1.0",
        "groups": groups,
        "global_macros": [],
        "settings": {},
        "update_settings": {"check_on_startup": False},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)


class RemoveMissingGroupConfigManagerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-rmgroup-")
        self.path = os.path.join(self.dir, "config.json")
        home = mock.patch(
            "core.config_manager.app_data_dir",
            return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)

    def _manager(self, groups):
        from core.config_manager import ConfigManager
        _write_config(self.path, groups)
        return ConfigManager(config_path=self.path)

    def test_removing_a_missing_group_returns_false(self):
        cm = self._manager([
            {"name": "Default", "auto_commands": [], "devices": [VALID]},
        ])

        self.assertFalse(cm.remove_group("存在しない"),
                         "何も消していないのに成功（True）を返している")
        self.assertEqual([g["name"] for g in cm.get_groups()], ["Default"])

    def test_removing_a_missing_group_does_not_write_the_file(self):
        cm = self._manager([
            {"name": "Default", "auto_commands": [], "devices": [VALID]},
        ])

        with mock.patch.object(cm, "save_config", return_value=True) as save:
            cm.remove_group("存在しない")
        save.assert_not_called()

    def test_removing_an_existing_group_still_returns_true(self):
        cm = self._manager([
            {"name": "Default", "auto_commands": [], "devices": [VALID]},
            {"name": "空", "auto_commands": [], "devices": []},
        ])

        self.assertTrue(cm.remove_group("空"))
        self.assertEqual([g["name"] for g in cm.get_groups()], ["Default"])


class DeleteMissingGroupMainWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-rmgroup-ui-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            w = MainWindow()
        self.addCleanup(w.deleteLater)
        return w

    def test_deleting_a_missing_group_is_not_reported_as_deleted(self):
        from PyQt6.QtWidgets import QMessageBox
        w = self._window()
        w.status_bar.clearMessage()
        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
             mock.patch("ui.main_window.QMessageBox.warning") as warn:
            w._on_delete_group("存在しない")

        self.assertNotIn("削除しました", w.status_bar.currentMessage(),
                         "何も消していないのに削除済みと案内している")
        warn.assert_called_once()
        message = warn.call_args[0][2]
        self.assertIn("失敗", message)
        # 実行中の設定にも何も適用されていないので、
        # 「このセッション中のみ有効」とは案内しない
        self.assertNotIn("セッション", message)


if __name__ == "__main__":
    unittest.main()
