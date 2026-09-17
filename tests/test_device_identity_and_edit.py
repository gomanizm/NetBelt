"""機器名の一意性と、機器編集が 1 回の保存で済むことを検証する。

接続の管理も所属グループの検索も機器名だけで行っているのに、登録時に
名前の重複を禁止していなかった。G1 と G2 に同名・別ホストの R を登録して
G2 の R へ接続すると、先に見つかる G1 の auto_commands が G2 の機器へ
送られる（実測: 実ソケットに G1 のコマンドが届いた）。同一グループ内の
重複は remove_device でまとめて消える。

機器の編集は remove_device（保存）→ add_device（保存）の 2 段階で、
削除側の保存だけ失敗するとメモリからは消えたまま画面に残り、次の無関係な
保存で機器がディスクから消える。追加側だけ失敗すると旧データが復元されて
新旧の同名 2 件が保存される（いずれも実測）。

名前は全グループを通して一意にし、編集は差し替えを 1 回の保存で行い、
保存に失敗したらメモリも元に戻す。
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


class DeviceIdentityTest(unittest.TestCase):
    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp(prefix="netbelt-devid-"))
        ap = mock.patch("core.config_manager.app_data_dir", return_value=self.data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        from core.config_manager import ConfigManager
        # 引数なしの ConfigManager() は CWD の config.json（ソース実行の
        # ローカル設定）を使う。必ず一時ファイルを指定する
        self.cm = ConfigManager(config_path=str(self.data_dir / "config.json"))
        self.cm.add_group("A")
        self.cm.add_group("B")

    def _names(self, group):
        return [(d["name"], d["host"]) for d in self.cm.get_group(group)["devices"]]

    def _on_disk(self, group):
        cfg = json.load(open(self.cm.config_path, encoding="utf-8"))
        g = next(x for x in cfg["groups"] if x["name"] == group)
        return [(d["name"], d["host"]) for d in g["devices"]]

    # --- 一意性 ---

    def test_a_name_used_in_another_group_is_refused(self):
        self.assertTrue(self.cm.add_device("A", _device("R1", "192.0.2.1")))

        self.assertFalse(self.cm.add_device("B", _device("R1", "192.0.2.2")),
                         "別グループの同名機器を追加できている")
        self.assertEqual(self._names("B"), [])

    def test_a_duplicate_in_the_same_group_is_refused(self):
        self.assertTrue(self.cm.add_device("A", _device("R1", "192.0.2.1")))
        self.assertFalse(self.cm.add_device("A", _device("R1", "192.0.2.9")))
        self.assertEqual(self._names("A"), [("R1", "192.0.2.1")])

    def test_the_group_of_a_name_can_be_looked_up(self):
        self.cm.add_device("A", _device("R1", "192.0.2.1"))
        self.assertEqual(self.cm.find_device_group("R1"), "A")
        self.assertIsNone(self.cm.find_device_group("nobody"))

    # --- 編集は 1 回の保存 ---

    def test_editing_a_device_saves_once(self):
        self.cm.add_device("A", _device("R1", "192.0.2.1"))
        with mock.patch.object(self.cm, "save_config", wraps=self.cm.save_config) as save:
            ok = self.cm.update_device("A", "R1", "A", _device("R1", "192.0.2.99"))
        self.assertTrue(ok)
        self.assertEqual(save.call_count, 1, "編集で保存を %d 回している" % save.call_count)
        self.assertEqual(self._on_disk("A"), [("R1", "192.0.2.99")])

    def test_editing_can_rename_and_move_in_one_save(self):
        self.cm.add_device("A", _device("R1", "192.0.2.1"))
        ok = self.cm.update_device("A", "R1", "B", _device("R2", "192.0.2.1"))
        self.assertTrue(ok)
        self.assertEqual(self._names("A"), [])
        self.assertEqual(self._names("B"), [("R2", "192.0.2.1")])
        self.assertEqual(self._on_disk("B"), [("R2", "192.0.2.1")])

    def test_a_failed_save_during_edit_changes_nothing(self):
        self.cm.add_device("A", _device("R1", "192.0.2.1"))
        with mock.patch.object(self.cm, "save_config", return_value=False):
            ok = self.cm.update_device("A", "R1", "B", _device("R1", "192.0.2.99"))
        self.assertFalse(ok)
        self.assertEqual(self._names("A"), [("R1", "192.0.2.1")], "メモリが元に戻っていない")
        self.assertEqual(self._names("B"), [])
        self.assertEqual(self._on_disk("A"), [("R1", "192.0.2.1")])

    def test_renaming_onto_another_devices_name_is_refused(self):
        self.cm.add_device("A", _device("R1", "192.0.2.1"))
        self.cm.add_device("B", _device("R2", "192.0.2.2"))
        self.assertFalse(self.cm.update_device("A", "R1", "A", _device("R2", "192.0.2.1")))
        self.assertEqual(self._names("A"), [("R1", "192.0.2.1")])

    def test_a_failed_save_on_remove_restores_memory(self):
        self.cm.add_device("A", _device("R1", "192.0.2.1"))
        with mock.patch.object(self.cm, "save_config", return_value=False):
            self.assertFalse(self.cm.remove_device("A", "R1"))
        self.assertEqual(self._names("A"), [("R1", "192.0.2.1")],
                         "保存に失敗したのにメモリから消えている")

    def test_a_failed_save_on_move_restores_memory(self):
        """ドラッグ＆ドロップの移動（move_device）も、保存に失敗したら戻すこと。"""
        self.cm.add_device("A", _device("R1", "192.0.2.1"))
        with mock.patch.object(self.cm, "save_config", return_value=False):
            self.assertFalse(self.cm.move_device("A", "B", "R1"))
        self.assertEqual(self._names("A"), [("R1", "192.0.2.1")], "移動元から消えたまま")
        self.assertEqual(self._names("B"), [], "移動先に残ったまま")

    def test_a_failed_save_on_add_restores_memory(self):
        with mock.patch.object(self.cm, "save_config", return_value=False):
            self.assertFalse(self.cm.add_device("A", _device("R1", "192.0.2.1")))
        self.assertEqual(self._names("A"), [])


class DeviceIdentityUiTest(unittest.TestCase):
    """メインウィンドウの追加・編集が、上の規則を通ること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp(prefix="netbelt-devid-ui-"))
        ap = mock.patch("core.config_manager.app_data_dir", return_value=self.data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        # MainWindow は CWD の config.json を使うので、作業ディレクトリを移す
        self._cwd = os.getcwd()
        os.chdir(self.data_dir)
        self.addCleanup(os.chdir, self._cwd)
        from ui.main_window import MainWindow
        self.window = MainWindow()
        self.addCleanup(self.window.close)
        cm = self.window.config_manager
        for g in ("A", "B"):
            if cm.get_group(g) is None:
                cm.add_group(g)
        cm.add_device("A", _device("R1", "192.0.2.1"))
        self.window._load_devices()

    def _dialog_returning(self, device, group):
        from PyQt6.QtWidgets import QDialog
        dialog = mock.Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = device
        dialog.get_selected_group.return_value = group
        dialog.group_combo.findText.return_value = 0
        return dialog

    def test_adding_a_device_with_an_existing_name_is_refused_with_a_reason(self):
        dialog = self._dialog_returning(_device("R1", "192.0.2.2"), "B")
        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
             mock.patch("ui.main_window.QMessageBox") as box:
            self.window._on_add_device()

        self.assertTrue(box.warning.called, "重複を知らせていない")
        shown = " ".join(str(a) for a in box.warning.call_args[0])
        self.assertIn("R1", shown)
        self.assertEqual(self.window.config_manager.get_group("B")["devices"], [])

    def test_duplicating_onto_an_existing_name_says_why(self):
        """右クリックの「複製」でも、名前の重複は理由を示して断ること。"""
        dialog = self._dialog_returning(_device("R1", "192.0.2.3"), "B")
        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
             mock.patch("ui.main_window.QMessageBox") as box:
            self.window._on_device_duplicate("A", _device("R1", "192.0.2.1"))

        self.assertTrue(box.warning.called)
        shown = " ".join(str(a) for a in box.warning.call_args[0])
        self.assertIn("R1", shown, "理由（どの名前が重複か）を言っていない: %s" % shown)
        self.assertEqual(self.window.config_manager.get_group("B")["devices"], [])

    def test_editing_a_device_saves_once_and_keeps_it_on_a_failure(self):
        cm = self.window.config_manager
        dialog = self._dialog_returning(_device("R1", "192.0.2.99"), "A")
        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
             mock.patch("ui.main_window.QMessageBox"), \
             mock.patch.object(cm, "save_config", return_value=False) as save:
            self.window._on_device_edit("A", _device("R1", "192.0.2.1"))

        self.assertEqual(save.call_count, 1, "編集で保存を %d 回している" % save.call_count)
        self.assertEqual([(d["name"], d["host"]) for d in cm.get_group("A")["devices"]],
                         [("R1", "192.0.2.1")], "保存に失敗したのに機器が変わっている")


if __name__ == "__main__":
    unittest.main()
