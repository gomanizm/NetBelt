"""送れない文字（孤立したサロゲート）を含む自動実行コマンドを GUI から保存しようとしたとき、保存できなかったと知らせることを検証する。

何が起きていたか（ad7caa5 の後）。読み込み時の隔離のために
ConfigManager._is_valid_auto_commands へ「各行を UTF-8 にできる」条件を
足したところ、グループの追加（add_group）と自動実行コマンドの設定
（set_group_auto_commands）も、保存の前にこの検査で断るようになった。
戻り値は False のままだが last_save_failed が立たないので、画面の知らせが
441ea02 の『グループの追加を設定ファイルへ保存できませんでした。保存
できなかったので、変更は反映していません。』から『グループの追加に
失敗しました。』（改名と同時なら『自動実行コマンドの保存に失敗しました。』）へ
変わっていた（last_save_failed は 441ea02 が True、ad7caa5 が False）。

どう直したか。GUI からの保存は、441ea02 と同じく形（文字列だけの list）だけを
見る。送れない文字は config.json（UTF-8）へも書けないので、保存の失敗として
扱われ、変更は取り消される。読み込み時の隔離と、送る直前の検査
（MainWindow._run_auto_commands）は、送れる文字かどうかも見るまま。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

SUR = chr(0xD800)
BROKEN = ["terminal length 0", "show " + SUR]


class _ConfigCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-unsendable-save-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "config.json"
        self.path.write_text(json.dumps({
            "groups": [{"name": "Lab", "auto_commands": ["show clock"],
                        "devices": []}],
            "global_macros": []}), encoding="utf-8")

    def _manager(self):
        from core.config_manager import ConfigManager
        return ConfigManager(config_path=str(self.path))


class ConfigManagerSaveTest(_ConfigCase):
    def test_adding_a_group_is_reported_as_a_save_failure(self):
        manager = self._manager()
        before = self.path.read_bytes()

        self.assertFalse(manager.add_group("G2", BROKEN))

        self.assertTrue(manager.last_save_failed,
                        "保存できなかったことが分からない（last_save_failed）")
        self.assertIsNone(manager.get_group("G2"), "メモリに残った")
        self.assertEqual(before, self.path.read_bytes(), "ファイルが変わった")

    def test_setting_auto_commands_is_reported_as_a_save_failure(self):
        manager = self._manager()
        before = self.path.read_bytes()

        self.assertFalse(manager.set_group_auto_commands("Lab", BROKEN))

        self.assertTrue(manager.last_save_failed,
                        "保存できなかったことが分からない（last_save_failed）")
        self.assertEqual(["show clock"],
                         manager.get_group("Lab")["auto_commands"],
                         "メモリが元へ戻っていない")
        self.assertEqual(before, self.path.read_bytes(), "ファイルが変わった")

    def test_a_malformed_value_is_still_refused_without_saving(self):
        manager = self._manager()
        manager.save_config = mock.Mock(return_value=True)

        self.assertFalse(manager.add_group("G2", "show version"))
        self.assertFalse(manager.set_group_auto_commands("Lab", [1, 2]))

        manager.save_config.assert_not_called()
        self.assertFalse(manager.last_save_failed)


class GroupDialogNoticeTest(_ConfigCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from ui.main_window import MainWindow
        manager = self._manager()
        with mock.patch("ui.main_window.ConfigManager", return_value=manager):
            window = MainWindow()
        self.addCleanup(window.close)
        return window

    @staticmethod
    def _dialog(name, auto_commands):
        from PyQt6.QtWidgets import QDialog
        dlg = mock.Mock()
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.get_group_name.return_value = name
        dlg.get_auto_commands.return_value = auto_commands
        return dlg

    def _run(self, window, action, dialog):
        with mock.patch("ui.main_window.GroupDialog", return_value=dialog), \
             mock.patch("ui.main_window.QMessageBox.warning") as warn:
            action()
        warn.assert_called_once()
        return warn.call_args[0][2]

    def test_adding_a_group(self):
        window = self._window()

        message = self._run(window, window._on_add_group,
                            self._dialog("G2", BROKEN))

        self.assertIn("設定ファイルへ保存できませんでした", message)
        self.assertIn("反映していません", message)

    def test_editing_the_auto_commands(self):
        window = self._window()

        message = self._run(window, lambda: window._on_edit_group("Lab"),
                            self._dialog("Lab", BROKEN))

        self.assertIn("設定ファイルへ保存できませんでした", message)
        self.assertIn("反映していません", message)

    def test_renaming_and_editing_the_auto_commands(self):
        window = self._window()

        message = self._run(window, lambda: window._on_edit_group("Lab"),
                            self._dialog("Lab2", BROKEN))

        self.assertIn("グループ名の変更は保存しました", message)
        self.assertIn("自動実行コマンドは保存できなかったので、反映していません",
                      message)


if __name__ == "__main__":
    unittest.main()
