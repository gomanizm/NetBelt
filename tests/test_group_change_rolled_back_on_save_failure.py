"""グループの変更が保存に失敗したら、メモリも元へ戻すこと。

実測（8b0c94e、os.replace を 1 回だけ失敗させて画面から操作）: グループの
追加・削除・改名・自動実行コマンドの変更は、どれも save_config() の前に
メモリの設定を書き換え、失敗しても巻き戻していなかった。画面には
『変更はこのセッション中のみ有効で、アプリを終了すると失われます。』と
出るのに、終了時の _save_layout がそのメモリを書き出すので、再起動後も
変更は残っていた（案内と逆のことが起きる）。

  add_group      → 再起動後も NewGroup が残る
  remove_group   → 再起動後も Empty が消えたまま
  rename_group   → 再起動後も Core-renamed
  set_group_auto_commands → 再起動後も新しいコマンド

改名の保存に失敗したときは、そのあと自動実行コマンドの保存へ進まずに
戻るため、同じダイアログで変えた自動実行コマンドは黙って捨てられていた。

直し方: 機器（add_device / update_device / remove_device）やマクロと同じく、
保存に失敗したらメモリも元に戻し、『保存できなかったので、変更は反映して
いません』と伝える。巻き戻すのでツリーは作り直さない（畳み具合と選択を
捨てない）。改名の巻き戻しで、改名前の名前と自動実行コマンドがそのまま残る。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

CONFIG = {
    "config_version": "1.0",
    "groups": [
        {"name": "Default", "auto_commands": [], "devices": []},
        {"name": "Core", "auto_commands": ["terminal length 0"], "devices": []},
        {"name": "Empty", "auto_commands": [], "devices": []},
    ],
    "global_macros": [],
    "settings": {},
}

_real_replace = os.replace


class GroupChangeRolledBackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-grouprollback-"))
        self.path = self.dir / "config.json"
        self.path.write_text(json.dumps(CONFIG), encoding="utf-8")
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _window(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        with mock.patch("ui.main_window.ConfigManager",
                        return_value=ConfigManager(config_path=str(self.path))), \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            window = MainWindow()
        self.addCleanup(window.close)
        return window

    @staticmethod
    def _dialog(name, commands):
        from PyQt6.QtWidgets import QDialog
        dlg = mock.Mock()
        dlg.exec.return_value = QDialog.DialogCode.Accepted
        dlg.get_group_name.return_value = name
        dlg.get_auto_commands.return_value = commands
        return mock.Mock(return_value=dlg)

    def _run(self, action):
        """os.replace を 1 回だけ失敗させて action を実行し、終了後に読み直す。

        Returns: (警告文のリスト, 再起動後のグループ -> auto_commands)
        """
        from core.config_manager import ConfigManager
        from PyQt6.QtWidgets import QMessageBox
        window = self._window()
        shown = []
        budget = {"fail": 1}

        def flaky_replace(src, dst):
            if budget["fail"]:
                budget["fail"] -= 1
                raise PermissionError(13, "denied")
            return _real_replace(src, dst)

        with mock.patch("core.config_manager.os.replace", flaky_replace), \
                mock.patch.object(QMessageBox, "warning",
                                  side_effect=lambda *a, **k: shown.append(a[2])), \
                mock.patch.object(QMessageBox, "question",
                                  return_value=QMessageBox.StandardButton.Yes):
            action(window)
        window.close()
        self.app.processEvents()
        self.assertEqual(budget["fail"], 0, "前提: 保存が 1 回失敗している")
        reloaded = ConfigManager(config_path=str(self.path))
        return shown, {g["name"]: g.get("auto_commands")
                       for g in reloaded.get_groups()}

    def _assert_rollback_message(self, shown):
        self.assertEqual(len(shown), 1, "警告は 1 回: %r" % (shown,))
        self.assertIn("反映していません", shown[0])
        self.assertNotIn("セッション", shown[0])

    def test_adding_a_group_is_rolled_back(self):
        def add(window):
            with mock.patch("ui.main_window.GroupDialog",
                            self._dialog("NewGroup", ["conf t"])):
                window._on_add_group()

        shown, groups = self._run(add)
        self._assert_rollback_message(shown)
        self.assertNotIn("NewGroup", groups)

    def test_deleting_a_group_is_rolled_back(self):
        shown, groups = self._run(lambda w: w._on_delete_group("Empty"))
        self._assert_rollback_message(shown)
        self.assertIn("Empty", groups)

    def test_renaming_a_group_is_rolled_back_with_its_auto_commands(self):
        def rename(window):
            with mock.patch("ui.main_window.GroupDialog",
                            self._dialog("Core-renamed", ["terminal length 0"])):
                window._on_edit_group("Core")

        shown, groups = self._run(rename)
        self._assert_rollback_message(shown)
        self.assertNotIn("Core-renamed", groups)
        self.assertEqual(groups.get("Core"), ["terminal length 0"],
                         "改名を戻したときに自動実行コマンドが消えている")

    def test_changing_auto_commands_is_rolled_back(self):
        def edit(window):
            with mock.patch("ui.main_window.GroupDialog",
                            self._dialog("Core", ["reload in 5"])):
                window._on_edit_group("Core")

        shown, groups = self._run(edit)
        self._assert_rollback_message(shown)
        self.assertEqual(groups.get("Core"), ["terminal length 0"])

    def test_a_failed_rename_keeps_the_running_configuration(self):
        """巻き戻しはメモリにも効く（終了を待たずにツリーと一致する）。"""
        def rename(window):
            with mock.patch("ui.main_window.GroupDialog",
                            self._dialog("Core-renamed", ["reload in 5"])):
                window._on_edit_group("Core")
            cm = window.config_manager
            self.assertIsNone(cm.get_group("Core-renamed"))
            self.assertIsNotNone(cm.get_group("Core"))
            self.assertEqual(cm.get_group("Core")["auto_commands"],
                             ["terminal length 0"])

        self._run(rename)

    def test_the_tree_is_not_rebuilt_when_nothing_was_applied(self):
        """巻き戻したので、畳み具合と選択を捨てるような作り直しはしない。"""
        window = self._window()
        from PyQt6.QtWidgets import QMessageBox
        window.config_manager.save_config = mock.Mock(return_value=False)
        with mock.patch("ui.main_window.GroupDialog",
                        self._dialog("NewGroup", [])), \
                mock.patch.object(QMessageBox, "warning"), \
                mock.patch.object(window, "_load_devices") as reload_tree:
            window._on_add_group()
        reload_tree.assert_not_called()


class GroupMutatorRollbackTest(unittest.TestCase):
    """ConfigManager 側だけを見る。保存が失敗したらメモリも戻ること。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-grouprollback-cm-"))
        self.path = self.dir / "config.json"
        self.path.write_text(json.dumps(CONFIG), encoding="utf-8")

    def _manager(self):
        from core.config_manager import ConfigManager
        cm = ConfigManager(config_path=str(self.path))
        cm.save_config = mock.Mock(return_value=False)
        return cm

    def test_add_group(self):
        cm = self._manager()
        self.assertFalse(cm.add_group("NewGroup", ["conf t"]))
        self.assertIsNone(cm.get_group("NewGroup"))
        self.assertTrue(cm.last_save_failed)

    def test_remove_group(self):
        cm = self._manager()
        self.assertFalse(cm.remove_group("Empty"))
        self.assertIsNotNone(cm.get_group("Empty"))
        self.assertEqual([g["name"] for g in cm.get_groups()],
                         ["Default", "Core", "Empty"], "並び順も戻すこと")
        self.assertTrue(cm.last_save_failed)

    def test_rename_group(self):
        cm = self._manager()
        self.assertFalse(cm.rename_group("Core", "Core-renamed"))
        self.assertIsNone(cm.get_group("Core-renamed"))
        self.assertEqual(cm.get_group("Core")["auto_commands"],
                         ["terminal length 0"])
        self.assertTrue(cm.last_save_failed)

    def test_set_group_auto_commands(self):
        cm = self._manager()
        self.assertFalse(cm.set_group_auto_commands("Core", ["reload in 5"]))
        self.assertEqual(cm.get_group("Core")["auto_commands"],
                         ["terminal length 0"])
        self.assertTrue(cm.last_save_failed)

    def test_a_rejected_change_is_not_reported_as_a_save_failure(self):
        """保存にすら行かない拒否（同名・対象なし）と見分けが付くこと。"""
        from core.config_manager import ConfigManager
        cm = ConfigManager(config_path=str(self.path))
        self.assertFalse(cm.add_group("Core"))          # 同名
        self.assertFalse(cm.last_save_failed)
        self.assertFalse(cm.remove_group("存在しない"))
        self.assertFalse(cm.last_save_failed)
        self.assertFalse(cm.rename_group("Core", "Empty"))   # 新名が重複
        self.assertFalse(cm.last_save_failed)
        self.assertFalse(cm.set_group_auto_commands("存在しない", []))
        self.assertFalse(cm.last_save_failed)


if __name__ == "__main__":
    unittest.main()
