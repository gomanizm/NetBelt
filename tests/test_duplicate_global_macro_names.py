"""同じ名前の共通マクロが複数ある設定で、別のマクロを送ったり消したりしないこと。

何が起きていたか（基準 028ebc2 で実測）。共通マクロ（global_macros）の実行・
編集・削除は、どれも名前で相手を引く。GUI の新規作成は同名を断るが、読み込みは
重複を知らせないので、手編集・持ち込みの config.json では同じ名前が並びうる。

    global_macros: X（説明 A: conf t / interface Gi0/1 / shutdown）
                   X（説明 B: show interfaces status）
    読み込み       → load_warning は None（何も知らせない）
    ツール > マクロ実行で「X - B」を選ぶ
                   → 機器へ送る列は ['conf t', 'interface Gi0/1', 'shutdown']
                     （get_macro_by_name が先頭の 1 件を返すため。意図しない送信）
    マクロ設定で 2 行目を削除 → 確認は「'X' を削除しますか？」、Yes で 2 件とも消える
    マクロ設定で 2 行目を編集 → 開くのは 1 件目（説明 A）の中身

どう直したか。読み込みのときに、同じ名前の機器と同じ要領で共通マクロ名の
重複を load_warning で知らせる。名前で引く操作（MainWindow の実行の受け口、
マクロ設定の編集・削除）は、その名前が複数あれば操作を断って理由を出す
（機器へは何も送らない）。重複の無い普通の構成の挙動は変えない。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

DUPLICATED = [
    {"name": "X", "commands": ["conf t", "interface Gi0/1", "shutdown"],
     "description": "A"},
    {"name": "X", "commands": ["show interfaces status"], "description": "B"},
    {"name": "Y", "commands": ["show clock"], "description": "C"},
]


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-dupmacro-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_path = self.dir / "config.json"

    def _config_manager(self, macros):
        from core.config_manager import ConfigManager
        self.config_path.write_text(json.dumps({
            "groups": [{"name": "Default", "devices": []}],
            "global_macros": macros}), encoding="utf-8")
        return ConfigManager(config_path=str(self.config_path))

    def _disk_macros(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))[
            "global_macros"]


class LoadWarningTest(_Base):

    def test_duplicate_names_are_reported(self):
        """同じ名前が複数あれば、読み込みの警告でその名前を挙げること。"""
        cm = self._config_manager(DUPLICATED)

        self.assertIsNotNone(cm.load_warning)
        self.assertIn("同じ名前", cm.load_warning)
        self.assertIn("X", cm.load_warning)
        self.assertNotIn("Y", cm.load_warning)

    def test_unique_names_are_not_reported(self):
        """重複の無い構成では何も知らせないこと（対照）。"""
        cm = self._config_manager(DUPLICATED[1:])

        self.assertIsNone(cm.load_warning)


class ExecuteRefusedTest(_Base):
    """MainWindow の実行の受け口と、接続先リストの「ツール」から選ぶ経路。"""

    def _window(self, macros):
        from PyQt6.QtWidgets import QMessageBox
        from ui.main_window import MainWindow
        cm = self._config_manager(macros)
        with mock.patch("ui.main_window.ConfigManager", return_value=cm), \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch.object(QMessageBox, "warning"):
            window = MainWindow()
        self.addCleanup(window.close)
        self.sent = []
        window.macro_manager.start_command_list = (
            lambda device, commands, delay: self.sent.append((device, commands)))
        return window

    def _run(self, window, trigger):
        from PyQt6.QtWidgets import QMessageBox
        with mock.patch.object(QMessageBox, "warning") as warning:
            trigger()
        return [call.args[2] for call in warning.call_args_list]

    def test_duplicate_name_sends_nothing(self):
        """同じ名前が複数あれば、どれも送らずに理由を出すこと。"""
        window = self._window(DUPLICATED)

        warnings = self._run(
            window, lambda: window._on_macro_execute_requested("sw1", "X"))

        self.assertEqual(self.sent, [], "同名のどれかが機器へ送られた")
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("複数", warnings[0])

    def test_second_item_in_tools_menu_sends_nothing(self):
        """「ツール > マクロ実行」で 2 件目を選んでも、1 件目を送らないこと。"""
        from PyQt6.QtWidgets import QMenu
        window = self._window(DUPLICATED)
        tree = window.device_tree
        tree.set_tools_state_provider(lambda name: {
            "connected": True,
            "macros": list(window.config_manager.get_global_macros())})
        menu = QMenu()
        self.addCleanup(menu.deleteLater)
        tree._add_tools_menu(menu, "sw1", {"name": "sw1"})
        tools = next(a.menu() for a in menu.actions() if a.text() == "ツール")
        run = next(a.menu() for a in tools.actions() if a.text() == "マクロ実行")
        second = next(a for a in run.actions() if a.text() == "X - B")

        warnings = self._run(window, second.trigger)

        self.assertEqual(self.sent, [])
        self.assertEqual(len(warnings), 1, warnings)

    def test_unique_name_still_runs(self):
        """重複の無い名前は今までどおり送ること（対照）。"""
        window = self._window(DUPLICATED)

        warnings = self._run(
            window, lambda: window._on_macro_execute_requested("sw1", "Y"))

        self.assertEqual(warnings, [])
        self.assertEqual(self.sent, [("sw1", ["show clock"])])


class PresetDialogRefusedTest(_Base):
    """マクロ設定（プリセット管理）の編集・削除。"""

    def _dialog(self, macros, row):
        from ui.dialogs.macro_dialog import MacroDialog
        cm = self._config_manager(macros)
        dialog = MacroDialog(device_name="sw1", config_manager=cm)
        self.addCleanup(dialog.deleteLater)
        dialog.preset_list_widget.setCurrentRow(row)
        return dialog

    def test_edit_refuses_duplicate_name(self):
        """同じ名前が複数あれば、編集ダイアログを開かずに理由を出すこと。"""
        from PyQt6.QtWidgets import QMessageBox
        from ui.dialogs.macro_dialog import MacroDialog
        dialog = self._dialog(DUPLICATED, 1)

        with mock.patch.object(MacroDialog, "_exec_preset_dialog") as opened, \
                mock.patch.object(QMessageBox, "warning") as warning:
            dialog._on_preset_edit()

        opened.assert_not_called()
        self.assertEqual(warning.call_count, 1)
        self.assertIn("複数", warning.call_args.args[2])

    def test_delete_refuses_duplicate_name(self):
        """同じ名前が複数あれば、確認も削除もせずに理由を出すこと。"""
        from PyQt6.QtWidgets import QMessageBox
        dialog = self._dialog(DUPLICATED, 1)

        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes) as asked, \
                mock.patch.object(QMessageBox, "information"), \
                mock.patch.object(QMessageBox, "warning") as warning:
            dialog._on_preset_delete()

        asked.assert_not_called()
        self.assertEqual([m["description"] for m in self._disk_macros()],
                         ["A", "B", "C"], "同名のマクロが消された")
        self.assertEqual(warning.call_count, 1)
        self.assertIn("複数", warning.call_args.args[2])

    def test_unique_name_edit_and_delete_still_work(self):
        """重複の無い名前は、編集で開けて削除もできること（対照）。"""
        from PyQt6.QtWidgets import QMessageBox
        from ui.dialogs.macro_dialog import MacroDialog
        dialog = self._dialog(DUPLICATED, 2)
        seen = {}

        def fake_exec(self_dialog, preset_dialog):
            seen["commands"] = preset_dialog.command_text.toPlainText()
            return 0

        with mock.patch.object(MacroDialog, "_exec_preset_dialog", fake_exec):
            dialog._on_preset_edit()
        self.assertEqual(seen.get("commands"), "show clock")

        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes), \
                mock.patch.object(QMessageBox, "information"), \
                mock.patch.object(QMessageBox, "warning") as warning:
            dialog._on_preset_delete()
        warning.assert_not_called()
        self.assertEqual([m["name"] for m in self._disk_macros()], ["X", "X"])


if __name__ == "__main__":
    unittest.main()
