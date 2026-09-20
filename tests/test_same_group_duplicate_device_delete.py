"""同じグループに同名の機器が 2 台あるとき、削除が 1 台だけを消すことを検証する。

実測（16101ef）: remove_device() は remaining = [d for d in before
if d["name"] != device_name] と名前で絞るので、kyoten に rtr1@192.0.2.11 と
rtr1@192.0.2.12 がある状態で 1 回呼ぶと 2 台とも消える。画面の確認は
「機器 'rtr1' を削除しますか？」と 1 台の話をし、完了も「機器 'rtr1' を
削除しました」なので、2 台消えたことは伝わらない。グループの削除
（remove_group）は前の周で「消すのは get_group() が返すのと同じ 1 件だけ」に
直っているのに、機器の削除だけ逆のままだった。

直し方: remove_device も 1 件だけ消す形に揃える。どちらを消すのかは名前
だけでは決まらないので、機器の編集（R7-conn-b）と同じく接続先で 1 件に絞る。
接続先リストの右クリックは、その項目の機器データを削除の要求に添える。
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


def _config():
    return {
        "config_version": "1.0",
        "groups": [
            {"name": "Default", "auto_commands": [], "devices": []},
            {"name": "kyoten", "auto_commands": [], "devices": [
                _device("rtr1", "192.0.2.11"),
                _device("rtr1", "192.0.2.12"),
            ]},
        ],
        "global_macros": [],
        "settings": {},
    }


class SameGroupDuplicateDeviceDeleteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-dupdel-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        from core.config_manager import ConfigManager
        path = self.dir / "config.json"
        path.write_text(json.dumps(_config()), encoding="utf-8")
        self.cm = ConfigManager(config_path=str(path))

    def _hosts(self, group_name="kyoten"):
        return [(d["name"], d["host"])
                for d in self.cm.get_group(group_name)["devices"]]

    def _on_disk(self, group_name="kyoten"):
        raw = json.loads(Path(self.cm.config_path).read_text(encoding="utf-8"))
        group = next(g for g in raw["groups"] if g["name"] == group_name)
        return [(d["name"], d["host"]) for d in group["devices"]]

    # --- ConfigManager ---

    def test_only_one_entry_is_removed(self):
        """1 回の削除で消えるのは 1 台だけであること。"""
        self.assertTrue(self.cm.remove_device("kyoten", "rtr1"))

        self.assertEqual(self._hosts(), [("rtr1", "192.0.2.12")],
                         "同名の機器がまとめて消えている")
        self.assertEqual(self._on_disk(), [("rtr1", "192.0.2.12")])

    def test_the_endpoint_picks_the_entry_to_remove(self):
        """接続先を渡せば、その 1 台が消えること。"""
        from core.config_manager import device_endpoint
        second = dict(self.cm.get_group("kyoten")["devices"][1])

        self.assertTrue(self.cm.remove_device("kyoten", "rtr1",
                                              endpoint=device_endpoint(second)))

        self.assertEqual(self._hosts(), [("rtr1", "192.0.2.11")])

    def test_removing_twice_empties_the_group(self):
        """2 台とも消したいときは 2 回呼べば消えること。"""
        self.assertTrue(self.cm.remove_device("kyoten", "rtr1"))
        self.assertTrue(self.cm.remove_device("kyoten", "rtr1"))

        self.assertEqual(self._hosts(), [])

    def test_a_missing_device_is_still_refused(self):
        """居ない機器の削除は、これまでどおり False であること。"""
        with mock.patch.object(self.cm, "save_config") as save:
            self.assertFalse(self.cm.remove_device("kyoten", "nobody"))
        self.assertFalse(save.called, "何も消していないのに保存している")

    def test_a_failed_save_restores_both(self):
        """保存に失敗したら、メモリも元の 2 台に戻すこと。"""
        with mock.patch.object(self.cm, "save_config", return_value=False):
            self.assertFalse(self.cm.remove_device("kyoten", "rtr1"))

        self.assertEqual(self._hosts(),
                         [("rtr1", "192.0.2.11"), ("rtr1", "192.0.2.12")])

    # --- 画面から ---

    def _window(self):
        from ui.main_window import MainWindow
        with mock.patch("ui.main_window.ConfigManager", return_value=self.cm), \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            window = MainWindow()
        self.addCleanup(window.close)
        return window

    def test_the_tree_deletes_the_entry_it_was_opened_from(self):
        """画面の削除は、右クリックした項目の 1 台だけを消すこと。"""
        from PyQt6.QtWidgets import QMessageBox
        window = self._window()
        second = dict(self.cm.get_group("kyoten")["devices"][1])

        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window._on_device_delete("kyoten", "rtr1", second)

        warn.assert_not_called()
        self.assertEqual(self._hosts(), [("rtr1", "192.0.2.11")],
                         "右クリックした 192.0.2.12 以外が消えている")

    def test_the_delete_request_carries_the_device_data(self):
        """接続先リストの「削除」が、その項目の機器データを添えること。"""
        from ui.device_tree import DeviceTree
        tree = DeviceTree()
        self.addCleanup(tree.close)
        tree.load_from_config([
            {"name": "kyoten", "devices": [_device("rtr1", "192.0.2.11"),
                                           _device("rtr1", "192.0.2.12")]},
        ])
        received = []
        tree.device_delete.connect(lambda *args: received.append(args))
        group_item = tree.tree.topLevelItem(0)
        second_item = group_item.child(1)

        def fake_exec(menu, *args, **kwargs):
            action = next(a for a in menu.actions() if a.text() == "削除")
            action.trigger()
            return action

        pos = tree.tree.visualItemRect(second_item).center()
        with mock.patch("PyQt6.QtWidgets.QMenu.exec", fake_exec):
            tree._show_context_menu(pos)

        self.assertEqual(len(received), 1, "削除の要求が出ていない")
        self.assertEqual(received[0][0], "kyoten")
        self.assertEqual(received[0][1], "rtr1")
        self.assertEqual(received[0][2].get("host"), "192.0.2.12",
                         "右クリックした項目の機器データが届いていない")


if __name__ == "__main__":
    unittest.main()
