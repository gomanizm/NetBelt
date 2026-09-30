"""説明（description）が文字列でないプリセットでも、編集が開くことを検証する。

何が起きていたか（基準 441ea02 で実測）。手編集の config.json などで
global_macros の "description" が 123 / 1.5 / true / ["a"] / {"k": 1} の
プリセットを選んで「編集」を押すと、MacroDialog._on_preset_edit がその値を
そのまま PresetEditDialog へ渡し、QTextEdit.setPlainText が

    TypeError: setPlainText(self, text: Optional[str]): argument 1 has unexpected type 'int'

になって編集ダイアログが開かなかった（製品では「予期しないエラー」の
表示だけ）。読み込み時の警告も出ないので、利用者には直す手がかりが無い。
ツール > マクロ実行の一覧は f"{name} - {description}" で組むので
「Y - 123」と表示され、こちらは落ちない。None / 0 / 空の値は開いていた。

どう直したか。_on_preset_edit で、文字列でない説明は、空（偽）なら ""、
それ以外は str() にしてから渡す。ツールメニューの表示と同じ見え方になり、
今まで開いていた None / 0 は今までどおり空欄で開く。編集して保存すると
説明は文字列になる（例: 123 → "123"）。
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


class PresetDescriptionNotStringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-presetdesc-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 開いたダイアログは Python 側でも持っておく（持たずに捨てると、
        # 試験一式の後ろでプロセスごと落ちることがある。
        # test_preset_edit_dialogs_are_released.py の注意を参照）
        self.kept = []

    def _open_edit(self, description):
        """説明が description のプリセット 1 件で「編集」を押し、開いたダイアログを返す。"""
        from PyQt6.QtWidgets import QDialog
        from core.config_manager import ConfigManager
        from ui.dialogs.macro_dialog import MacroDialog
        config_path = self.dir / "config.json"
        config_path.write_text(json.dumps({
            "groups": [],
            "global_macros": [{"name": "Y", "commands": ["show version"],
                               "description": description}],
        }), encoding="utf-8")
        config_manager = ConfigManager(config_path=str(config_path))
        dialog = MacroDialog(None, device_name="sw1",
                             config_manager=config_manager)
        self.kept.append(dialog)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.preset_list_widget.count(), 1,
                         "前提: プリセットが一覧に出ている")
        dialog.preset_list_widget.setCurrentRow(0)

        opened = []

        def fake_exec(macro_dialog, preset_dialog):
            opened.append(preset_dialog)
            return QDialog.DialogCode.Rejected

        with mock.patch.object(MacroDialog, "_exec_preset_dialog", fake_exec):
            dialog._on_preset_edit()
        self.kept.extend(opened)
        self.assertEqual(len(opened), 1, "編集ダイアログが開かなかった")
        return opened[0]

    def test_non_string_description_is_shown_as_text(self):
        """数値・真偽値・リスト・辞書の説明でも開き、表示と同じ文字列が入ること。"""
        for description, shown in ((123, "123"), (1.5, "1.5"),
                                   (True, "True"), (["a"], "['a']"),
                                   ({"k": 1}, "{'k': 1}")):
            with self.subTest(description=description):
                edit = self._open_edit(description)
                self.assertEqual(edit.description_edit.toPlainText(), shown)
                self.assertEqual(edit.command_text.toPlainText(),
                                 "show version")

    def test_empty_values_still_open_blank(self):
        """None / 0 / 空のリストは、今までどおり空欄で開くこと。"""
        for description in (None, 0, False, []):
            with self.subTest(description=description):
                edit = self._open_edit(description)
                self.assertEqual(edit.description_edit.toPlainText(), "")

    def test_string_description_is_unchanged(self):
        """文字列の説明は、そのまま入ること（対照）。"""
        edit = self._open_edit("基本確認")
        self.assertEqual(edit.description_edit.toPlainText(), "基本確認")


if __name__ == "__main__":
    unittest.main()
