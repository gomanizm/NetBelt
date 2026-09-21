"""プリセット編集ダイアログも、閉じたら解放されることを検証する。

何が起きていたか（実測）: exec() で開いたまま捨てていないダイアログを
つぶす修正は「最後の 2 か所」（バージョン情報とログ保存の進捗）で終わりと
していたが、マクロ設定の「プリセット管理」から開く PresetEditDialog が
2 か所（_on_preset_new / _on_preset_edit）残っていた。どちらも
parent=MacroDialog で作って exec() するだけなので、閉じても非表示のまま
子として積み上がる。

    PresetEditDialog children inside one MacroDialog: 3   ← 新規作成を 3 回
    PresetEditDialog children inside one MacroDialog: 3   ← 編集を 3 回

親が MacroDialog なので、マクロ設定を閉じれば一緒に消える（アプリ終了まで
残る先の 2 か所よりは軽い）が、マクロ設定を開いたままプリセットを何件も
作り直すあいだは溜まり続ける。

どう直したか: exec() を try/finally で包み、閉じたあとの破棄を予約する
（MainWindow._exec_dialog と同じ中身。MacroDialog からはそれを呼べない）。

setParent(None) を足さない理由と、この試験が何も壊さない理由（実測）:
同じリポジトリの PasteConfirmDialog は setParent(None) + deleteLater() の
形だが、ここで同じことをすると、閉じたダイアログが親を失ってトップレベルの
窓になる。その形で tests/ 全体を流すと、数ファイル先（実測では
test_reconnect_wait_survives_enter_reconnect.py や
test_sftp_carry_over_timeout.py）の processEvents でプロセスごと落ちた
（PowerShell の $LASTEXITCODE で 0xC0000005 access violation、別の回は
0xC0000409。落ちる場所は走らせるたびに動く）。この試験の中で破棄を流す形
（sendPostedEvents で DeferredDelete を配る）でも同じように落ちた。
setParent(None) を外し、この試験でも何も壊さないようにすると、同じ並びが
最後まで通る。conftest の冒頭にある注意（解放済みの C++ オブジェクトに
触れてプロセスごと落ちる）と同じ筋で、この試験一式には他にも解放待ちの
オブジェクトが残っているため、ここで実際に壊すと巻き添えになる。

そのため、手放せたかは「閉じたあとに deleteLater() を呼んだか」で見る。
破棄そのものは、アプリ本体と同じく Qt のイベントループに任せる。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

MACRO = {"name": "show", "commands": ["show version"], "description": ""}


class PresetEditDialogsAreReleasedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _recording_exec(opened):
        """開かれた PresetEditDialog を控えて、キャンセルとして閉じる exec

        控えるのは数を数えるためだけではない。控えずに（Python 側の参照を
        持たずに）開かせると、この試験一式の残りでプロセスごと落ちた（実測。
        冒頭の注意を参照）。
        """
        from PyQt6.QtWidgets import QDialog

        def fake_exec(dialog):
            opened.append(dialog)
            return QDialog.DialogCode.Rejected

        return fake_exec

    @staticmethod
    def _released_spy(released):
        """deleteLater() を控えつつ、本物も呼ぶ差し替え"""
        from ui.dialogs.macro_dialog import PresetEditDialog
        real = PresetEditDialog.deleteLater

        def spy(dialog):
            released.append(dialog)
            real(dialog)

        return spy

    def _macro_dialog(self):
        """プリセットを 1 件持つマクロ設定ダイアログ"""
        from ui.dialogs.macro_dialog import MacroDialog
        config_manager = mock.Mock()
        config_manager.get_global_macros.return_value = [MACRO]
        config_manager.get_macro_by_name.return_value = MACRO
        dialog = MacroDialog(None, device_name="R1",
                             config_manager=config_manager)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.preset_list_widget.count(), 1,
                         "前提: プリセットが一覧に出ている")
        return dialog

    def test_new_preset_dialogs_are_released(self):
        """「新規作成」を繰り返しても、閉じた分を手放すこと。"""
        from PyQt6.QtWidgets import QDialog
        from ui.dialogs.macro_dialog import PresetEditDialog
        dialog = self._macro_dialog()

        opened, released = [], []

        with mock.patch.object(PresetEditDialog, "exec",
                               self._recording_exec(opened)),                 mock.patch.object(PresetEditDialog, "deleteLater",
                                  self._released_spy(released)):
            for _ in range(3):
                dialog._on_preset_new()

        self.assertEqual(len(opened), 3, "前提: 3 回開いた")
        self.assertEqual(released, opened,
                         "閉じたプリセット新規作成 3 件のうち %d 件しか"
                         "手放していない" % len(released))

    def test_edit_preset_dialogs_are_released(self):
        """「編集」を繰り返しても、閉じた分を手放すこと。"""
        from PyQt6.QtWidgets import QDialog
        from ui.dialogs.macro_dialog import PresetEditDialog
        dialog = self._macro_dialog()
        dialog.preset_list_widget.setCurrentRow(0)

        opened, released = [], []

        with mock.patch.object(PresetEditDialog, "exec",
                               self._recording_exec(opened)),                 mock.patch.object(PresetEditDialog, "deleteLater",
                                  self._released_spy(released)):
            for _ in range(3):
                dialog._on_preset_edit()

        self.assertEqual(len(opened), 3, "前提: 3 回開いた")
        self.assertEqual(released, opened,
                         "閉じたプリセット編集 3 件のうち %d 件しか"
                         "手放していない" % len(released))

    def test_the_preset_list_is_still_reloaded_after_saving(self):
        """保存して閉じたときは、これまでどおり一覧を読み直すこと。"""
        from PyQt6.QtWidgets import QDialog
        from ui.dialogs.macro_dialog import PresetEditDialog
        dialog = self._macro_dialog()
        dialog.config_manager.get_global_macros.return_value = [
            MACRO, {"name": "added", "commands": ["show run"],
                    "description": ""}]

        opened = []

        def accept(child):
            opened.append(child)
            return QDialog.DialogCode.Accepted

        with mock.patch.object(PresetEditDialog, "exec", accept):
            dialog._on_preset_new()

        names = [dialog.preset_list_widget.item(i).text()
                 for i in range(dialog.preset_list_widget.count())]
        self.assertEqual(names, ["show", "added"],
                         "保存したのに一覧を読み直していない: %r" % names)


if __name__ == "__main__":
    unittest.main()
