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


def _same_endpoint_groups(second_username="operator"):
    """両方の kyoten に、名前も接続先も同じ R@192.0.2.1 を置く（ユーザー名だけ違う）。"""
    groups = _duplicated_groups()
    groups[3]["devices"] = [_device("R", "192.0.2.1", second_username)]
    return groups


SAME_ENDPOINT_BEFORE = [("Default", []),
                        ("kyoten", [("R", "192.0.2.1", "admin")]),
                        ("other", []),
                        ("kyoten", [("R", "192.0.2.1", "operator")])]


class SameEndpointInBothGroupsTest(_ConfigFixture):
    """両方の同名グループに名前も接続先も同じ機器があるとき、画面の機器データで選ぶ。

    実測（14dc5c9）: kyoten[R@192.0.2.1 admin] / other / kyoten[R@192.0.2.1
    operator] で、1 つ目の kyoten の R を右クリックして削除・編集すると
    「機器の削除に失敗しました。」「機器の更新に失敗しました。設定は変更されて
    いません。」とだけ出て断られ、ドラッグでの移動も断られた（ebbe593 では
    1 つ目の R に正しく当たっていた）。接続先だけでは、どちらの kyoten の R か
    決まらないため（b9cbcfa の _group_of_device）。画面の項目が持つ機器データ
    （中身）と一致する機器を持つグループに絞れば決まる。中身まで同じなら
    決まらないので、今までどおり断る。
    """

    def setUp(self):
        super().setUp()
        from core.config_manager import device_endpoint
        self.cm = self._config_manager(_same_endpoint_groups())
        self.first = _device("R", "192.0.2.1")
        self.second = _device("R", "192.0.2.1", "operator")
        self.ep = device_endpoint(self.first)

    def test_removing_picks_the_group_whose_device_matches(self):
        for device, expected in [
                (self.first, [("Default", []), ("kyoten", []), ("other", []),
                              ("kyoten", [("R", "192.0.2.1", "operator")])]),
                (self.second, [("Default", []),
                               ("kyoten", [("R", "192.0.2.1", "admin")]),
                               ("other", []), ("kyoten", [])])]:
            with self.subTest(device["username"]):
                cm = self._config_manager(_same_endpoint_groups())
                self.assertTrue(cm.remove_device("kyoten", "R", endpoint=self.ep,
                                                 device=device))
                self.assertEqual(self._on_disk(), expected)

    def test_editing_picks_the_group_whose_device_matches(self):
        self.assertTrue(self.cm.update_device(
            "kyoten", "R", "kyoten", dict(self.first, username="changed"),
            old_endpoint=self.ep, old_device=self.first))

        self.assertEqual(self._on_disk(),
                         [("Default", []),
                          ("kyoten", [("R", "192.0.2.1", "changed")]),
                          ("other", []),
                          ("kyoten", [("R", "192.0.2.1", "operator")])],
                         "1 つ目の kyoten の R を編集できない、または別の R が変わった")

        self.assertTrue(self.cm.update_device(
            "kyoten", "R", "kyoten", dict(self.second, username="changed2"),
            old_endpoint=self.ep, old_device=self.second))
        self.assertEqual(self._on_disk()[3],
                         ("kyoten", [("R", "192.0.2.1", "changed2")]))
        self.assertEqual(self._on_disk()[1],
                         ("kyoten", [("R", "192.0.2.1", "changed")]))

    def test_moving_picks_the_group_whose_device_matches(self):
        self.assertTrue(self.cm.move_device("kyoten", "other", "R",
                                            endpoint=self.ep, device=self.first))

        self.assertEqual(self._on_disk(),
                         [("Default", []),
                          ("kyoten", []),
                          ("other", [("R", "192.0.2.1", "admin")]),
                          ("kyoten", [("R", "192.0.2.1", "operator")])])

    def test_identical_devices_in_both_groups_are_still_refused(self):
        """中身まで同じ機器が両方にあれば、どちらか決まらないので断ること。"""
        cm = self._config_manager(_same_endpoint_groups("admin"))
        before = self._on_disk()

        self.assertFalse(cm.remove_device("kyoten", "R", endpoint=self.ep,
                                          device=self.first))
        self.assertFalse(cm.update_device(
            "kyoten", "R", "kyoten", dict(self.first, username="changed"),
            old_endpoint=self.ep, old_device=self.first))
        self.assertFalse(cm.move_device("kyoten", "other", "R",
                                        endpoint=self.ep, device=self.first))

        self.assertEqual(self._on_disk(), before, "断ったのに設定が変わった")

    def test_a_device_that_matches_neither_group_is_refused(self):
        """画面の機器データがどちらとも一致しなければ、先頭を選ばず断ること。"""
        stale = _device("R", "192.0.2.1", "someone")

        self.assertFalse(self.cm.remove_device("kyoten", "R", endpoint=self.ep,
                                               device=stale))
        self.assertFalse(self.cm.move_device("kyoten", "other", "R",
                                             endpoint=self.ep, device=stale))

        self.assertEqual(self._on_disk(), SAME_ENDPOINT_BEFORE)


class SameEndpointInBothGroupsFromTheWindowTest(_ConfigFixture):
    """画面の経路でも、1 つ目の kyoten の R を削除・編集・移動できる（ebbe593 と同じ）。"""

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

    def _window(self, groups):
        from ui.main_window import MainWindow
        cm = self._config_manager(groups)
        with mock.patch("ui.main_window.ConfigManager", return_value=cm), \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            window = MainWindow()
        type(self)._windows.append(window)
        return window

    @staticmethod
    def _first_r(window):
        from PyQt6.QtCore import Qt
        tree = window.device_tree.tree
        items = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        item = [i for i in items if i.text(0) == "kyoten"][0].child(0)
        return item, item.data(0, Qt.ItemDataRole.UserRole)

    def _delete(self, window, data):
        from PyQt6.QtWidgets import QMessageBox
        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window.device_tree.device_delete.emit("kyoten", data["name"], data)
        return warn

    def _edit(self, window, data):
        from PyQt6.QtWidgets import QDialog
        dialog = mock.MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = dict(data, username="changed")
        dialog.get_selected_group.return_value = "kyoten"
        dialog.group_combo.findText.return_value = 1
        with mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window.device_tree.device_edit.emit("kyoten", data)
        return warn

    def _drag_to_other(self, window, item):
        tree = window.device_tree
        top = [tree.tree.topLevelItem(i)
               for i in range(tree.tree.topLevelItemCount())]
        other = [i for i in top if i.text(0) == "other"][0]
        tree.tree.setCurrentItem(item)
        event = mock.Mock()
        with mock.patch.object(tree.tree, "itemAt", return_value=other), \
                mock.patch.object(tree.tree, "dropIndicatorPosition",
                                  return_value=None), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            tree._on_drop_event(event)
        self.assertTrue(event.accept.called, "前提: ドロップが受け付けられている")
        return warn

    def test_deleting_the_first_groups_device(self):
        window = self._window(_same_endpoint_groups())
        _, data = self._first_r(window)

        warn = self._delete(window, data)

        self.assertFalse(warn.called, "1 つ目の kyoten の R の削除が断られた")
        self.assertEqual(self._on_disk(),
                         [("Default", []), ("kyoten", []), ("other", []),
                          ("kyoten", [("R", "192.0.2.1", "operator")])])

    def test_editing_the_first_groups_device(self):
        window = self._window(_same_endpoint_groups())
        _, data = self._first_r(window)

        warn = self._edit(window, data)

        self.assertFalse(warn.called, "1 つ目の kyoten の R の編集が断られた")
        self.assertEqual(self._on_disk(),
                         [("Default", []),
                          ("kyoten", [("R", "192.0.2.1", "changed")]),
                          ("other", []),
                          ("kyoten", [("R", "192.0.2.1", "operator")])])

    def test_dragging_the_first_groups_device(self):
        window = self._window(_same_endpoint_groups())
        item, _ = self._first_r(window)

        warn = self._drag_to_other(window, item)

        self.assertFalse(warn.called, "1 つ目の kyoten の R の移動が断られた")
        self.assertEqual(self._on_disk(),
                         [("Default", []), ("kyoten", []),
                          ("other", [("R", "192.0.2.1", "admin")]),
                          ("kyoten", [("R", "192.0.2.1", "operator")])])

    def test_a_refusal_says_why_and_how_to_separate_the_groups(self):
        """中身まで同じで決まらず断るときは、理由と直し方を添えること。

        実測（14dc5c9）: 「機器の削除に失敗しました。」などとだけ出て、なぜ断られた
        のか、どうすれば操作できるのかが分からなかった。
        """
        window = self._window(_same_endpoint_groups("admin"))
        before = self._on_disk()

        for label, operate in [
                ("削除", lambda: self._delete(window, self._first_r(window)[1])),
                ("編集", lambda: self._edit(window, self._first_r(window)[1])),
                ("移動", lambda: self._drag_to_other(window,
                                                    self._first_r(window)[0]))]:
            with self.subTest(label):
                warn = operate()
                self.assertTrue(warn.called, "断ったのに案内が出ていない")
                text = warn.call_args.args[2]
                self.assertIn("kyoten", text)
                self.assertIn("先に並んでいる方", text)
                self.assertIn("グループを編集", text)
        self.assertEqual(self._on_disk(), before, "断ったのに設定が変わった")


class AmbiguityIsReportedOnlyWhenItIsTheReasonTest(_ConfigFixture):
    """ConfigManager 単体: 同名グループのどれの機器か決められずに断るときだけ真。

    画面は、この答えを見て失敗の案内に同名グループの話を足す
    （MainWindow._same_name_group_hint）。
    """

    @staticmethod
    def _ep(host):
        from core.config_manager import device_endpoint
        return device_endpoint(_device("R", host))

    def test_identical_devices_in_both_groups_are_ambiguous(self):
        cm = self._config_manager(_same_endpoint_groups("admin"))
        device = _device("R", "192.0.2.1")

        self.assertTrue(cm.device_group_is_ambiguous(
            "kyoten", "R", self._ep("192.0.2.1"), device))
        self.assertFalse(cm.remove_device("kyoten", "R",
                                          endpoint=self._ep("192.0.2.1"),
                                          device=device),
                         "前提: この機器の削除は断られる")

    def test_a_device_that_one_group_holds_is_not_ambiguous(self):
        for label, groups, device in [
                ("接続先違い", _duplicated_groups(), _device("R", "192.0.2.2")),
                ("接続先は同じで中身違い", _same_endpoint_groups(),
                 _device("R", "192.0.2.1", "operator")),
                ("どの同名グループにも無い", _duplicated_groups(),
                 _device("R", "192.0.2.9"))]:
            with self.subTest(label):
                cm = self._config_manager(groups)
                self.assertFalse(cm.device_group_is_ambiguous(
                    "kyoten", "R", self._ep(device["host"]), device))

    def test_without_same_named_groups_or_endpoint_nothing_is_ambiguous(self):
        cm = self._config_manager([_group("Default", []),
                                   _group("kyoten", [_device("R", "192.0.2.1")])])
        self.assertFalse(cm.device_group_is_ambiguous(
            "kyoten", "R", self._ep("192.0.2.1"), _device("R", "192.0.2.1")))

        cm = self._config_manager(_same_endpoint_groups("admin"))
        self.assertFalse(cm.device_group_is_ambiguous("kyoten", "R"))


class UnrelatedRefusalsGetNoSameNameHintTest(_ConfigFixture):
    """同名グループと関係の無い理由で断ったときは、同名グループの案内を付けない。

    実測（b815dfb）: 案内は、同じ名前のグループが 2 つ以上あるというだけで
    付いていた。kyoten[R@192.0.2.1] / other[R@192.0.2.9] / kyoten[R@192.0.2.2]
    で 2 つ目の kyoten の R を other へドラッグすると、移動先に同じ名前の R が
    いるので断る（コンソールには「移動先グループに既に存在します」）のに、警告は
    「機器の移動に失敗しました。」に「先に並んでいる方を右クリックし「グループを
    編集」で別の名前にすると分かれます」を足した文になり、改名しても直らない
    操作へ誘導していた。2 つ目の kyoten の R の削除・編集で保存に失敗したときも
    同じ。同名グループの無い設定と同じ文言にする。
    """

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

    def _window(self, groups):
        from ui.main_window import MainWindow
        cm = self._config_manager(groups)
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

    def _second_r(self, window):
        from PyQt6.QtCore import Qt
        item = self._tree_group(window, "kyoten", 1).child(0)
        data = item.data(0, Qt.ItemDataRole.UserRole)
        self.assertEqual(data["host"], "192.0.2.2",
                         "前提: 2 つ目の kyoten の R は 192.0.2.2")
        return item, data

    def test_a_move_refused_by_the_target_gets_the_plain_message(self):
        groups = _duplicated_groups()
        groups[2]["devices"] = [_device("R", "192.0.2.9")]
        window = self._window(groups)
        before = self._on_disk()
        item, _ = self._second_r(window)
        tree = window.device_tree
        tree.tree.setCurrentItem(item)
        event = mock.Mock()

        with mock.patch.object(tree.tree, "itemAt",
                               return_value=self._tree_group(window, "other", 0)), \
                mock.patch.object(tree.tree, "dropIndicatorPosition",
                                  return_value=None), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            tree._on_drop_event(event)

        self.assertTrue(event.accept.called, "前提: ドロップが受け付けられている")
        self.assertEqual(self._on_disk(), before,
                         "前提: 移動先に同じ名前の R がいるので断られている")
        self.assertTrue(warn.called, "断ったのに案内が出ていない")
        self.assertEqual(warn.call_args.args[2], "機器の移動に失敗しました。",
                         "移動先の重複で断ったのに、同名グループの案内が付いた")

    def test_a_delete_whose_save_fails_gets_the_plain_message(self):
        from PyQt6.QtWidgets import QMessageBox
        window = self._window(_duplicated_groups())
        _, data = self._second_r(window)

        with mock.patch.object(window.config_manager, "save_config",
                               return_value=False), \
                mock.patch("ui.main_window.QMessageBox.question",
                           return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window.device_tree.device_delete.emit("kyoten", data["name"], data)

        self.assertEqual(self._on_disk(), BEFORE)
        self.assertTrue(warn.called, "断ったのに案内が出ていない")
        self.assertEqual(warn.call_args.args[2], "機器の削除に失敗しました。",
                         "保存の失敗なのに、同名グループの案内が付いた")

    def test_an_edit_whose_save_fails_gets_the_plain_message(self):
        from PyQt6.QtWidgets import QDialog
        window = self._window(_duplicated_groups())
        _, data = self._second_r(window)
        dialog = mock.MagicMock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.get_device_data.return_value = dict(data, username="changed")
        dialog.get_selected_group.return_value = "kyoten"
        dialog.group_combo.findText.return_value = 1

        with mock.patch.object(window.config_manager, "save_config",
                               return_value=False), \
                mock.patch("ui.main_window.DeviceDialog", return_value=dialog), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window.device_tree.device_edit.emit("kyoten", data)

        self.assertEqual(self._on_disk(), BEFORE)
        self.assertTrue(warn.called, "断ったのに案内が出ていない")
        self.assertEqual(warn.call_args.args[2],
                         "機器の更新に失敗しました。設定は変更されていません。",
                         "保存の失敗なのに、同名グループの案内が付いた")


if __name__ == "__main__":
    unittest.main()
