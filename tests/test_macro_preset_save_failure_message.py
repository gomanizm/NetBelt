"""プリセット新規作成の失敗理由を、ダイアログが取り違えないことを検証する。

PresetEditDialog._on_save は add_global_macro() が False を返すと理由を問わず
「プリセット '...' は既に存在します。」と出す。add_global_macro が False を
返すのは重複のときだけではなく、config.json への保存に失敗したときも同じ
（実測: 保存の atomic replace が WinError 5 で失敗する経路がある）。
存在しないプリセット名で保存に失敗した利用者は、名前を変えれば通ると考えて
同じ失敗を繰り返す。重複は先に自分で確かめ、それ以外は保存の失敗として伝える。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class FakeConfigManager:
    """add_global_macro の戻り値だけを操る最小の代役。"""

    def __init__(self, existing=(), save_succeeds=True):
        self.macros = [{"name": n, "commands": ["show version"], "description": ""}
                       for n in existing]
        self.save_succeeds = save_succeeds

    def get_macro_by_name(self, name):
        return next((m for m in self.macros if m["name"] == name), None)

    def add_global_macro(self, name, commands, description=""):
        if self.get_macro_by_name(name):
            return False
        if not self.save_succeeds:
            return False        # 保存に失敗（メモリは元のまま）
        self.macros.append({"name": name, "commands": commands,
                            "description": description})
        return True


class PresetEditDialogReportsTheRealReasonTest(unittest.TestCase):
    _dialogs = []

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls._dialogs.clear()

    def _dialog(self, config_manager, name):
        from ui.dialogs.macro_dialog import PresetEditDialog
        dialog = PresetEditDialog(config_manager=config_manager)
        self._dialogs.append(dialog)
        dialog.name_edit.setText(name)
        dialog.command_text.setPlainText("show version")
        return dialog

    def test_a_failed_save_is_not_reported_as_a_duplicate(self):
        dialog = self._dialog(FakeConfigManager(save_succeeds=False), "新しいプリセット")

        with mock.patch("PyQt6.QtWidgets.QMessageBox.warning") as warning:
            dialog._on_save()

        self.assertEqual(warning.call_count, 1, warning.call_args_list)
        message = warning.call_args.args[2]
        self.assertNotIn("既に存在します", message, message)
        self.assertIn("保存", message, message)

    def test_a_duplicate_is_still_reported_as_a_duplicate(self):
        dialog = self._dialog(FakeConfigManager(existing=["基本確認"]), "基本確認")

        with mock.patch("PyQt6.QtWidgets.QMessageBox.warning") as warning:
            dialog._on_save()

        self.assertEqual(warning.call_count, 1, warning.call_args_list)
        self.assertIn("既に存在します", warning.call_args.args[2])

    def test_a_successful_save_still_accepts_the_dialog(self):
        config = FakeConfigManager()
        dialog = self._dialog(config, "新しいプリセット")

        with mock.patch("PyQt6.QtWidgets.QMessageBox.information"), \
                mock.patch.object(dialog, "accept") as accept:
            dialog._on_save()

        self.assertEqual(accept.call_count, 1)
        self.assertEqual([m["name"] for m in config.macros], ["新しいプリセット"])


if __name__ == "__main__":
    unittest.main()
