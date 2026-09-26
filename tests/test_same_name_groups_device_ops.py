"""同じ名前のグループが 2 つあるとき、機器の削除・編集・移動が狙った 1 台に当たることを検証する。

実測（ebbe593）: 手編集の config.json に kyoten を 2 つ置き、1 つ目に
R@192.0.2.1、2 つ目に R@192.0.2.2（同じ名前・接続先違い）を入れる。接続先
リストで 2 つ目の kyoten の R を操作すると、
  削除: 1 つ目の R@192.0.2.1 が設定ファイルから消え、R@192.0.2.2 は残る。
        状態バーは「機器 'R' を削除しました」、警告は出ない。
  編集: ユーザー名を changed にすると、1 つ目の R@192.0.2.1 が
        R@192.0.2.2/changed で上書きされる（192.0.2.1 の機器が消える）。
        2 つ目は変わらない。「機器 'R' を更新しました」と出る。
  移動: ドラッグで other へ移すと、1 つ目の R@192.0.2.1 が other へ動く。
ConfigManager の remove_device / update_device / move_device は、グループを
get_group(グループ名) で引くので必ず 1 つ目の kyoten に当たり、_device_index は
そのグループに候補が 1 件しか無いと接続先を見ずにそれを返していたため。

直し方: 呼び出し側が操作対象の機器の接続先を渡したとき、同じ名前のグループが
複数あれば、その接続先の機器を実際に持っているグループを選ぶ
（ConfigManager._group_of_device）。どのグループも持っていない・複数が持って
いて 1 つに決まらないときは、別の機器を書き換えないよう断る（何も変えない）。
機器の編集でグループ名を変えないときは、元のグループに置いたままにする
（名前で引き直すと 1 つ目の kyoten へ移ってしまう）。設定ファイルのグループ名は
変えない。同じ名前のグループが無い設定と、接続先を渡さない呼び出しは今までどおり。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


def _device(name, host, username="admin"):
    return {"name": name, "host": host, "port": 22, "protocol": "ssh",
            "username": username, "password": "", "ssh_key": "", "macros": []}


def _group(name, devices, auto_commands=()):
    return {"name": name, "auto_commands": list(auto_commands),
            "devices": list(devices)}


def _duplicated_groups():
    return [
        _group("Default", []),
        _group("kyoten", [_device("R", "192.0.2.1")], ["show clock"]),
        _group("other", []),
        _group("kyoten", [_device("R", "192.0.2.2")], ["terminal length 0"]),
    ]


class _ConfigFixture(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-dupgrpops-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "config.json"

    def _config_manager(self, groups):
        from core.config_manager import ConfigManager
        self.path.write_text(json.dumps({
            "config_version": "1.0",
            "groups": groups,
            "global_macros": [],
            "settings": {},
        }), encoding="utf-8")
        return ConfigManager(config_path=str(self.path))

    def _on_disk(self):
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [(g["name"], [(d["name"], d["host"], d["username"])
                             for d in g["devices"]])
                for g in raw["groups"]]


BEFORE = [("Default", []),
          ("kyoten", [("R", "192.0.2.1", "admin")]),
          ("other", []),
          ("kyoten", [("R", "192.0.2.2", "admin")])]


class SameNameGroupsConfigManagerTest(_ConfigFixture):
    """ConfigManager 単体: 接続先を渡せば、その機器を持つ同名グループに当たる。"""

    def setUp(self):
        super().setUp()
        self.cm = self._config_manager(_duplicated_groups())

    @staticmethod
    def _ep(host):
        from core.config_manager import device_endpoint
        return device_endpoint(_device("R", host))

    def test_removing_the_second_groups_device_removes_that_device(self):
        self.assertTrue(self.cm.remove_device("kyoten", "R",
                                              endpoint=self._ep("192.0.2.2")))

        self.assertEqual(self._on_disk(),
                         [("Default", []),
                          ("kyoten", [("R", "192.0.2.1", "admin")]),
                          ("other", []),
                          ("kyoten", [])],
                         "2 つ目の kyoten の R を消したのに、別の機器が消えた")

    def test_removing_the_first_groups_device_still_removes_that_device(self):
        self.assertTrue(self.cm.remove_device("kyoten", "R",
                                              endpoint=self._ep("192.0.2.1")))

        self.assertEqual(self._on_disk(),
                         [("Default", []),
                          ("kyoten", []),
                          ("other", []),
                          ("kyoten", [("R", "192.0.2.2", "admin")])])

    def test_editing_the_second_groups_device_changes_that_device_in_place(self):
        """編集した 1 台だけが変わり、元のグループ（2 つ目の kyoten）に残ること。"""
        self.assertTrue(self.cm.update_device(
            "kyoten", "R", "kyoten", _device("R", "192.0.2.2", "changed"),
            old_endpoint=self._ep("192.0.2.2")))

        self.assertEqual(self._on_disk(),
                         [("Default", []),
                          ("kyoten", [("R", "192.0.2.1", "admin")]),
                          ("other", []),
                          ("kyoten", [("R", "192.0.2.2", "changed")])],
                         "2 つ目の kyoten の R を編集したのに、別の機器が書き換わった")

    def test_editing_the_second_groups_device_into_another_group(self):
        """編集でグループを変えたときも、動くのは編集した 1 台であること。"""
        self.assertTrue(self.cm.update_device(
            "kyoten", "R", "other", _device("R", "192.0.2.2"),
            old_endpoint=self._ep("192.0.2.2")))

        self.assertEqual(self._on_disk(),
                         [("Default", []),
                          ("kyoten", [("R", "192.0.2.1", "admin")]),
                          ("other", [("R", "192.0.2.2", "admin")]),
                          ("kyoten", [])])

    def test_moving_the_second_groups_device_moves_that_device(self):
        self.assertTrue(self.cm.move_device("kyoten", "other", "R",
                                            endpoint=self._ep("192.0.2.2")))

        self.assertEqual(self._on_disk(),
                         [("Default", []),
                          ("kyoten", [("R", "192.0.2.1", "admin")]),
                          ("other", [("R", "192.0.2.2", "admin")]),
                          ("kyoten", [])],
                         "2 つ目の kyoten の R を動かしたのに、別の機器が動いた")

    def test_an_endpoint_that_no_same_named_group_holds_is_refused(self):
        """どの同名グループにも無い接続先なら、先頭の機器を選ばず断ること。"""
        other = self._ep("192.0.2.9")

        self.assertFalse(self.cm.remove_device("kyoten", "R", endpoint=other))
        self.assertFalse(self.cm.update_device(
            "kyoten", "R", "kyoten", _device("R", "192.0.2.9", "changed"),
            old_endpoint=other))
        self.assertFalse(self.cm.move_device("kyoten", "other", "R",
                                             endpoint=other))

        self.assertEqual(self._on_disk(), BEFORE, "断ったのに設定が変わった")
        self.assertEqual([(g["name"], [d["host"] for d in g["devices"]])
                          for g in self.cm.get_groups()],
                         [(name, [d[1] for d in devices])
                          for name, devices in BEFORE],
                         "断ったのにメモリ上の設定が変わった")

    def test_an_endpoint_held_by_both_same_named_groups_is_refused(self):
        """両方の同名グループに同じ接続先の機器があれば、決まらないので断ること。"""
        groups = _duplicated_groups()
        groups[3]["devices"] = [_device("R", "192.0.2.1", "operator")]
        cm = self._config_manager(groups)
        before = self._on_disk()

        self.assertFalse(cm.remove_device("kyoten", "R",
                                          endpoint=self._ep("192.0.2.1")))
        self.assertFalse(cm.update_device(
            "kyoten", "R", "kyoten", _device("R", "192.0.2.1", "changed"),
            old_endpoint=self._ep("192.0.2.1")))
        self.assertFalse(cm.move_device("kyoten", "other", "R",
                                        endpoint=self._ep("192.0.2.1")))

        self.assertEqual(self._on_disk(), before)

    def test_a_device_only_in_the_second_group_can_be_edited(self):
        """2 つ目の kyoten にしか居ない機器も、接続先を渡せば編集できること。"""
        groups = _duplicated_groups()
        groups[3]["devices"] = [_device("rtr2", "192.0.2.12")]
        cm = self._config_manager(groups)
        from core.config_manager import device_endpoint

        self.assertTrue(cm.update_device(
            "kyoten", "rtr2", "kyoten", _device("rtr2", "192.0.2.12", "changed"),
            old_endpoint=device_endpoint(_device("rtr2", "192.0.2.12"))))

        self.assertEqual(self._on_disk()[3],
                         ("kyoten", [("rtr2", "192.0.2.12", "changed")]))
        self.assertEqual(self._on_disk()[1],
                         ("kyoten", [("R", "192.0.2.1", "admin")]))

    def test_the_group_names_on_disk_are_not_changed(self):
        """設定ファイルのグループ名は変えないこと（読み込み時にも改名しない）。"""
        self.cm.remove_device("kyoten", "R", endpoint=self._ep("192.0.2.2"))

        self.assertEqual([name for name, _ in self._on_disk()],
                         ["Default", "kyoten", "other", "kyoten"])


class UniqueGroupNamesUnchangedTest(_ConfigFixture):
    """前提の確認: 同名グループの無い設定の振る舞いは変えない。"""

    def test_a_single_device_is_still_found_whatever_endpoint_is_given(self):
        from core.config_manager import device_endpoint
        cm = self._config_manager([
            _group("Default", []),
            _group("kyoten", [_device("R", "192.0.2.1")]),
            _group("other", []),
        ])
        stale = device_endpoint(_device("R", "192.0.2.99"))

        self.assertTrue(cm.update_device(
            "kyoten", "R", "kyoten", _device("R", "192.0.2.1", "changed"),
            old_endpoint=stale))
        self.assertTrue(cm.move_device("kyoten", "other", "R", endpoint=stale))
        self.assertTrue(cm.remove_device("other", "R", endpoint=stale))

        self.assertEqual(self._on_disk(),
                         [("Default", []), ("kyoten", []), ("other", [])])


class SameNameGroupsFromTheWindowTest(_ConfigFixture):
    """画面の経路（接続先リストのシグナル → MainWindow）を通しても狙った 1 台に当たる。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 破棄済みウィジェットへのシグナル配送で落ちるため保持する
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _window(self):
        from ui.main_window import MainWindow
        cm = self._config_manager(_duplicated_groups())
        with mock.patch("ui.main_window.ConfigManager", return_value=cm), \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            window = MainWindow()
        type(self)._windows.append(window)
        return window

    @staticmethod
    def _tree_group(window, name, nth):
        tree = window.device_tree.tree
        items = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        return [item for item in items if item.text(0) == name][nth]

    def _second_r_item(self, window):
        from PyQt6.QtCore import Qt
        item = self._tree_group(window, "kyoten", 1).child(0)
        self.assertEqual(item.data(0, Qt.ItemDataRole.UserRole)["host"],
                         "192.0.2.2", "前提: 2 つ目の kyoten の R は 192.0.2.2")
        return item

    def test_deleting_from_the_tree_removes_the_clicked_device(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QMessageBox
        window = self._window()
        data = self._second_r_item(window).data(0, Qt.ItemDataRole.UserRole)

        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window.device_tree.device_delete.emit("kyoten", data["name"], data)

        self.assertFalse(warn.called, "削除が断られた")
        self.assertEqual(self._on_disk(),
                         [("Default", []),
                          ("kyoten", [("R", "192.0.2.1", "admin")]),
                          ("other", []),
                          ("kyoten", [])],
                         "右クリックしていない 1 つ目の kyoten の R が消えた")

    def test_editing_from_the_tree_changes_the_clicked_device(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QDialog
        window = self._window()
        data = self._second_r_item(window).data(0, Qt.ItemDataRole.UserRole)
        dialog = mock.MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = dict(data, username="changed")
        dialog.get_selected_group.return_value = "kyoten"
        dialog.group_combo.findText.return_value = 1

        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window.device_tree.device_edit.emit("kyoten", data)

        self.assertFalse(warn.called, "編集が断られた")
        self.assertEqual(self._on_disk(),
                         [("Default", []),
                          ("kyoten", [("R", "192.0.2.1", "admin")]),
                          ("other", []),
                          ("kyoten", [("R", "192.0.2.2", "changed")])],
                         "開いていない 1 つ目の kyoten の R が書き換わった")

    def test_dragging_from_the_tree_moves_the_dragged_device(self):
        window = self._window()
        tree = window.device_tree
        tree.tree.setCurrentItem(self._second_r_item(window))
        event = mock.Mock()

        with mock.patch.object(tree.tree, "itemAt",
                               return_value=self._tree_group(window, "other", 0)), \
                mock.patch.object(tree.tree, "dropIndicatorPosition",
                                  return_value=None), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            tree._on_drop_event(event)

        self.assertTrue(event.accept.called, "前提: ドロップが受け付けられている")
        self.assertFalse(warn.called, "移動が断られた")
        self.assertEqual(self._on_disk(),
                         [("Default", []),
                          ("kyoten", [("R", "192.0.2.1", "admin")]),
                          ("other", [("R", "192.0.2.2", "admin")]),
                          ("kyoten", [])],
                         "掴んでいない 1 つ目の kyoten の R が動いた")


if __name__ == "__main__":
    unittest.main()
