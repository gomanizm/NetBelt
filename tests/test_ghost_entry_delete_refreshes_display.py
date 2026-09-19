"""表示にだけ残ったグループ・プリセットを、削除の操作で画面から消せることを検証する。

実測（8b0c94e）: 設定に無いグループの削除は「グループの削除に失敗しました。」と
案内するだけになった（remove_group が False を返すようになったため）。
そのため、接続先リストには残っているが設定からは消えているグループを、
利用者は表示から消せない。2 回試しても毎回「失敗しました」で、ツリーに
残り続ける。直す前はツリーを作り直していたので、案内は誤りだったが表示からは
消えていた。プリセット一覧（MacroDialog）も同じで、一覧にだけ残った
プリセットを選んで削除しても「削除に失敗しました。」が出るだけで残る。

修正: _on_delete_group の失敗の経路のうち、確認ダイアログの前から設定に
無かったとき（group is None）だけ _load_devices() でツリーを設定に合わせて
から知らせる。MacroDialog は削除に失敗したときに _load_presets() を呼ぶ。
保存に失敗しただけの経路（メモリは巻き戻し済み、または実行中の設定には
適用済み）は今までどおりで、作り直しは 1 回のまま。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


def _group_names(window):
    """接続先リストに並んでいるグループ名を返す。"""
    root = window.device_tree.tree.invisibleRootItem()
    return [root.child(i).text(0) for i in range(root.childCount())]


def _preset_names(dialog):
    """プリセット一覧に並んでいる名前を返す。"""
    return [dialog.preset_list_widget.item(i).text()
            for i in range(dialog.preset_list_widget.count())]


class GhostEntryDeleteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _config_manager(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-ghost-")
        return ConfigManager(config_path=os.path.join(d, "config.json"))

    def _window(self):
        from ui.main_window import MainWindow
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = self._config_manager()
            w = MainWindow()
        self.addCleanup(w.deleteLater)
        return w

    def _window_with_a_ghost_group(self):
        """ツリーには「幽霊」が並んでいるが、設定からは消えている状態を作る。"""
        w = self._window()
        w.config_manager.add_group("幽霊", [])
        w._load_devices()
        self.assertIn("幽霊", _group_names(w), "前提: ツリーに並んでいない")
        w.config_manager.remove_group("幽霊")
        self.assertIsNone(w.config_manager.get_group("幽霊"),
                          "前提: 設定から消えていない")
        return w

    def _delete_group(self, window, group_name):
        from PyQt6.QtWidgets import QMessageBox
        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window._on_delete_group(group_name)
        return warn

    def test_a_group_left_only_in_the_tree_disappears_from_the_tree(self):
        w = self._window_with_a_ghost_group()

        warn = self._delete_group(w, "幽霊")

        self.assertNotIn("幽霊", _group_names(w),
                         "設定に無いグループを表示から消せない")
        warn.assert_called_once()
        self.assertIn("失敗", warn.call_args[0][2])

    def test_a_save_failure_does_not_rebuild_the_tree_twice(self):
        """対照: 保存だけ失敗した経路の作り直しは、これまでどおり 1 回。"""
        from PyQt6.QtWidgets import QMessageBox
        w = self._window()
        w.config_manager.add_group("消える予定", [])
        w.config_manager.save_config = mock.Mock(return_value=False)
        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.main_window.QMessageBox.warning"), \
                mock.patch.object(w, "_load_devices") as reload_tree:
            w._on_delete_group("消える予定")
        self.assertEqual(reload_tree.call_count, 1)

    def test_an_existing_group_is_still_deleted(self):
        """対照: 設定にあるグループは、これまでどおり消えること。"""
        w = self._window()
        w.config_manager.add_group("空", [])
        w._load_devices()

        from PyQt6.QtWidgets import QMessageBox
        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            w._on_delete_group("空")

        warn.assert_not_called()
        self.assertNotIn("空", _group_names(w))
        self.assertIn("削除しました", w.status_bar.currentMessage())

    def _dialog_with_a_ghost_preset(self):
        """一覧には show-ver が並んでいるが、設定からは消えている状態を作る。"""
        from ui.dialogs.macro_dialog import MacroDialog
        cm = self._config_manager()
        cm.add_global_macro("show-ver", ["show version"], "確認")
        dialog = MacroDialog(None, device_name="ルータA", config_manager=cm)
        self.addCleanup(dialog.deleteLater)
        self.assertIn("show-ver", _preset_names(dialog), "前提: 一覧に無い")
        cm.remove_global_macro("show-ver")
        self.assertIsNone(cm.get_macro_by_name("show-ver"),
                          "前提: 設定から消えていない")
        dialog.preset_list_widget.setCurrentRow(
            _preset_names(dialog).index("show-ver"))
        return dialog

    def test_a_preset_left_only_in_the_list_disappears_from_the_list(self):
        from PyQt6.QtWidgets import QMessageBox
        dialog = self._dialog_with_a_ghost_preset()

        with mock.patch("ui.dialogs.macro_dialog.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.dialogs.macro_dialog.QMessageBox.information"
                           ) as info, \
                mock.patch("ui.dialogs.macro_dialog.QMessageBox.warning"
                           ) as warn:
            dialog._on_preset_delete()

        self.assertNotIn("show-ver", _preset_names(dialog),
                         "設定に無いプリセットを一覧から消せない")
        info.assert_not_called()
        warn.assert_called_once()
        self.assertIn("失敗", warn.call_args[0][2])

    def test_an_existing_preset_is_still_deleted(self):
        """対照: 設定にあるプリセットは、これまでどおり消えること。"""
        from PyQt6.QtWidgets import QMessageBox
        from ui.dialogs.macro_dialog import MacroDialog
        cm = self._config_manager()
        cm.add_global_macro("show-ver", ["show version"], "確認")
        dialog = MacroDialog(None, device_name="ルータA", config_manager=cm)
        self.addCleanup(dialog.deleteLater)
        dialog.preset_list_widget.setCurrentRow(
            _preset_names(dialog).index("show-ver"))

        with mock.patch("ui.dialogs.macro_dialog.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.dialogs.macro_dialog.QMessageBox.information"
                           ) as info, \
                mock.patch("ui.dialogs.macro_dialog.QMessageBox.warning"
                           ) as warn:
            dialog._on_preset_delete()

        warn.assert_not_called()
        info.assert_called_once()
        self.assertNotIn("show-ver", _preset_names(dialog))


if __name__ == "__main__":
    unittest.main()
