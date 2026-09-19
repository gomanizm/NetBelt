"""存在しない機器・プリセットの削除を「削除しました」と案内しないことを検証する。

実測（8b0c94e）: remove_group() は「対象が無ければ保存せず False」に直したが、
兄弟の remove_device() と remove_global_macro() は直す前の形のままだった。
どちらも「一致しない要素だけを残す」リスト内包を書いたあと、1 件も消えて
いなくても save_config() を呼び、その結果（True）を返していた。

  - remove_device('Default', '存在しない機器') -> True。設定ファイルへの
    保存が 1 回走り、MainWindow._on_device_delete がステータスバーへ
    「機器 '存在しない機器' を削除しました」と出す。
  - remove_global_macro('存在しないプリセット') -> True。MacroDialog が
    「プリセット 'show-ver' を削除しました。」と知らせる。

接続先リストやプリセット一覧が設定と食い違っていた（既に消えたものが表示に
残っていた）場合など、実際には何も消していないのに削除できたと伝わる。

修正: remove_group と同じ形に揃える。何も一致しなければ self.config を
書き換えず、save_config() も呼ばずに False を返す。呼び出し側の
「削除に失敗しました」の文言は、何も消えていない場合にも正しいので変えない。
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
    "name": "ルータA", "host": "192.0.2.1", "port": 22, "protocol": "ssh",
    "username": "admin", "password": "", "ssh_key": "", "macros": [],
}
MACRO = {"name": "show-ver", "commands": ["show version"], "description": "確認"}


def _write_config(path, groups, macros):
    config = {
        "config_version": "1.0",
        "groups": groups,
        "global_macros": macros,
        "settings": {},
        "update_settings": {"check_on_startup": False},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)


class RemoveMissingEntryConfigManagerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-rmmissing-")
        self.path = os.path.join(self.dir, "config.json")
        home = mock.patch(
            "core.config_manager.app_data_dir",
            return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)
        from core.config_manager import ConfigManager
        _write_config(
            self.path,
            [{"name": "Default", "auto_commands": [], "devices": [DEVICE]}],
            [dict(MACRO)])
        self.cm = ConfigManager(config_path=self.path)

    def test_removing_a_missing_device_returns_false(self):
        self.assertFalse(self.cm.remove_device("Default", "存在しない機器"),
                         "何も消していないのに成功（True）を返している")
        self.assertEqual(
            [d["name"] for d in self.cm.get_group("Default")["devices"]],
            ["ルータA"])

    def test_removing_a_missing_device_does_not_write_the_file(self):
        with mock.patch.object(self.cm, "save_config",
                               return_value=True) as save:
            self.cm.remove_device("Default", "存在しない機器")
        save.assert_not_called()

    def test_removing_an_existing_device_still_returns_true(self):
        self.assertTrue(self.cm.remove_device("Default", "ルータA"))
        self.assertEqual(self.cm.get_group("Default")["devices"], [])

    def test_removing_a_missing_macro_returns_false(self):
        self.assertFalse(self.cm.remove_global_macro("存在しないプリセット"),
                         "何も消していないのに成功（True）を返している")
        self.assertEqual([m["name"] for m in self.cm.get_global_macros()],
                         ["show-ver"])

    def test_removing_a_missing_macro_does_not_write_the_file(self):
        with mock.patch.object(self.cm, "save_config",
                               return_value=True) as save:
            self.cm.remove_global_macro("存在しないプリセット")
        save.assert_not_called()

    def test_removing_an_existing_macro_still_returns_true(self):
        self.assertTrue(self.cm.remove_global_macro("show-ver"))
        self.assertEqual(self.cm.get_global_macros(), [])


class RemoveMissingEntryUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _config_manager(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-rmmissing-ui-")
        return ConfigManager(config_path=os.path.join(d, "config.json"))

    def _window(self):
        from ui.main_window import MainWindow
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = self._config_manager()
            w = MainWindow()
        self.addCleanup(w.deleteLater)
        return w

    def test_deleting_a_missing_device_is_not_reported_as_deleted(self):
        from PyQt6.QtWidgets import QMessageBox
        w = self._window()
        w.status_bar.clearMessage()
        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            w._on_device_delete("Default", "存在しない機器")

        self.assertNotIn("削除しました", w.status_bar.currentMessage(),
                         "何も消していないのに削除済みと案内している")
        warn.assert_called_once()
        self.assertIn("失敗", warn.call_args[0][2])

    def test_deleting_a_missing_preset_is_not_reported_as_deleted(self):
        from PyQt6.QtWidgets import QMessageBox
        from ui.dialogs.macro_dialog import MacroDialog
        cm = self._config_manager()
        dialog = MacroDialog(None, device_name="ルータA", config_manager=cm)
        self.addCleanup(dialog.deleteLater)
        # 一覧にだけ残ったプリセット（設定からは既に消えている状態）
        dialog.preset_list_widget.addItem("show-ver")
        dialog.preset_list_widget.setCurrentRow(
            dialog.preset_list_widget.count() - 1)

        with mock.patch("ui.dialogs.macro_dialog.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.dialogs.macro_dialog.QMessageBox.information"
                           ) as info, \
                mock.patch("ui.dialogs.macro_dialog.QMessageBox.warning"
                           ) as warn:
            dialog._on_preset_delete()

        info.assert_not_called()
        warn.assert_called_once()
        self.assertIn("失敗", warn.call_args[0][2])


if __name__ == "__main__":
    unittest.main()
