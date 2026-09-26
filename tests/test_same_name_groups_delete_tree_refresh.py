"""同じ名前のグループが並ぶとき、機器の削除に失敗した後のツリーの扱いを検証する。

_on_device_delete は、削除に失敗したときに「設定に元から無かった（ツリーに
だけ残っていた）」のか「保存だけ失敗した」のかを見分け、前者のときだけ
_load_devices() でツリーを作り直す（test_ghost_device_delete_refreshes_tree）。
その見分けに使う existed を get_group(グループ名) で求めていたため、同じ名前の
グループが並ぶと、先頭の同名グループしか見ていなかった。

実測（c2bb66a）: 手編集の config.json に Default / kyoten（機器なし）/ other[X] /
kyoten[R@192.0.2.2] を置き、other を畳んで、save_config を失敗させてから
2 つ目の kyoten の R を削除すると、設定は変わらず「機器の削除に失敗しました。」
と出るのに、ツリーが作り直されて other が開き直り、項目も別のオブジェクトに
替わる（選択も外れる）。同名グループの無い設定なら畳んだまま。
逆に、1 つ目の kyoten に R@192.0.2.1 が居て、2 つ目の kyoten の R@192.0.2.2 が
設定からだけ消えている（ツリーにだけ残っている）ときは、1 つ目の R を見て
「設定にある」と取り違え、作り直さないので、何度削除しても行が消えない。

直し方: 同じ名前のグループが複数あり、右クリックした項目の接続先が分かる
ときは、その機器（名前と接続先）を実際に持っているグループがあるかで existed を
決める。remove_device が操作するグループを選ぶのと同じ読み方
（ConfigManager._group_of_device）。複数のグループが持っていて決められずに
断ったときも、設定にはあるのでツリーは作り直さない。同名グループの無い設定と
接続先の無い呼び出しは今までどおり（そのグループの名前だけで見る）。
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


class SameNameGroupsDeleteTreeRefreshTest(unittest.TestCase):
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

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-dupgrpdel-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.dir / "config.json"

    def _window(self, groups):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        self.path.write_text(json.dumps({
            "config_version": "1.0",
            "groups": groups,
            "global_macros": [],
            "settings": {},
        }), encoding="utf-8")
        cm = ConfigManager(config_path=str(self.path))
        with mock.patch("ui.main_window.ConfigManager", return_value=cm), \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            window = MainWindow()
        type(self)._windows.append(window)
        return window

    def _on_disk(self):
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [(g["name"], [(d["name"], d["host"], d["username"])
                             for d in g["devices"]])
                for g in raw["groups"]]

    @staticmethod
    def _tree_group(window, name, nth=0):
        tree = window.device_tree.tree
        items = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
        return [item for item in items if item.text(0) == name][nth]

    @staticmethod
    def _data(item):
        from PyQt6.QtCore import Qt
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _collapse_other(self, window):
        other = self._tree_group(window, "other")
        other.setExpanded(False)
        self.assertFalse(other.isExpanded(), "前提: other を畳めていない")
        return other

    def _delete(self, window, group_name, data, save_ok=True):
        """接続先リストのシグナルから削除を頼み、出た警告の文言を返す。"""
        from PyQt6.QtWidgets import QMessageBox
        with mock.patch("ui.main_window.QMessageBox.question",
                        return_value=QMessageBox.StandardButton.Yes), \
                mock.patch("ui.main_window.QMessageBox.warning") as warn, \
                mock.patch.object(window.config_manager, "save_config",
                                  return_value=save_ok):
            window.device_tree.device_delete.emit(group_name, data["name"], data)
        return [c.args[2] for c in warn.call_args_list]

    def _assert_tree_kept(self, window, other):
        self.assertIs(self._tree_group(window, "other"), other,
                      "ツリーが作り直された（項目が別のオブジェクトに替わった）")
        self.assertFalse(other.isExpanded(),
                         "ツリーが作り直され、畳んでいたグループが開き直った")

    def test_save_failure_keeps_the_tree_for_a_device_only_in_a_later_group(self):
        """後ろの同名グループにしか居ない機器の保存の失敗で、ツリーを作り直さないこと。"""
        window = self._window([
            _group("Default", []),
            _group("kyoten", [], ["show clock"]),
            _group("other", [_device("X", "192.0.2.9")]),
            _group("kyoten", [_device("R", "192.0.2.2")], ["terminal length 0"]),
        ])
        other = self._collapse_other(window)
        item = self._tree_group(window, "kyoten", 1).child(0)
        before = self._on_disk()

        warns = self._delete(window, "kyoten", self._data(item), save_ok=False)

        self.assertEqual(self._on_disk(), before, "前提: 設定が変わった")
        self.assertEqual(warns, ["機器の削除に失敗しました。"])
        self._assert_tree_kept(window, other)

    def test_save_failure_keeps_the_tree_when_both_groups_hold_the_name(self):
        """対照: 両方の同名グループに同じ名前の機器が居るときも作り直さないこと。"""
        window = self._window([
            _group("Default", []),
            _group("kyoten", [_device("R", "192.0.2.1")], ["show clock"]),
            _group("other", [_device("X", "192.0.2.9")]),
            _group("kyoten", [_device("R", "192.0.2.2")], ["terminal length 0"]),
        ])
        other = self._collapse_other(window)
        item = self._tree_group(window, "kyoten", 1).child(0)
        before = self._on_disk()

        warns = self._delete(window, "kyoten", self._data(item), save_ok=False)

        self.assertEqual(self._on_disk(), before, "前提: 設定が変わった")
        self.assertEqual(warns, ["機器の削除に失敗しました。"])
        self._assert_tree_kept(window, other)

    def test_an_undecidable_refusal_keeps_the_tree(self):
        """対照: どの同名グループの機器か決められずに断ったときも作り直さないこと。

        両方の kyoten に名前も接続先も中身も同じ R が居ると、remove_device は
        別の機器を消さないよう断る（設定はそのまま＝ツリーと一致している）。
        """
        window = self._window([
            _group("Default", []),
            _group("kyoten", [_device("R", "192.0.2.1")], ["show clock"]),
            _group("other", [_device("X", "192.0.2.9")]),
            _group("kyoten", [_device("R", "192.0.2.1")], ["terminal length 0"]),
        ])
        other = self._collapse_other(window)
        item = self._tree_group(window, "kyoten", 1).child(0)
        before = self._on_disk()

        warns = self._delete(window, "kyoten", self._data(item))

        self.assertEqual(self._on_disk(), before, "前提: 設定が変わった")
        self.assertEqual(len(warns), 1)
        self.assertIn("決められない", warns[0], "前提: 同名グループの案内が出ていない")
        self._assert_tree_kept(window, other)

    def test_a_row_left_only_in_a_later_group_disappears_from_the_tree(self):
        """後ろの同名グループにツリーだけ残った機器は、削除の操作で表示から消えること。

        先頭の同名グループに同じ名前の別の機器（接続先違い）が居ても、
        それを「設定にある」と取り違えないこと。
        """
        from core.config_manager import device_endpoint
        window = self._window([
            _group("Default", []),
            _group("kyoten", [_device("R", "192.0.2.1")], ["show clock"]),
            _group("other", [_device("X", "192.0.2.9")]),
            _group("kyoten", [_device("R", "192.0.2.2")], ["terminal length 0"]),
        ])
        item = self._tree_group(window, "kyoten", 1).child(0)
        data = self._data(item)
        self.assertEqual(data["host"], "192.0.2.2", "前提: 2 つ目の kyoten の R ではない")
        # 設定からだけ消す（ツリーは作り直さない＝ツリーにだけ残った状態）
        self.assertTrue(window.config_manager.remove_device(
            "kyoten", "R", endpoint=device_endpoint(data)))
        self.assertEqual(self._on_disk()[3], ("kyoten", []), "前提: 設定から消えていない")
        self.assertEqual(self._tree_group(window, "kyoten", 1).childCount(), 1,
                         "前提: ツリーから消えている")

        warns = self._delete(window, "kyoten", data)

        self.assertEqual(len(warns), 1)
        self.assertIn("失敗", warns[0])
        self.assertEqual(self._tree_group(window, "kyoten", 1).childCount(), 0,
                         "設定に無い機器の行が、ツリーに残ったまま消せない")
        self.assertEqual(self._on_disk()[1], ("kyoten", [("R", "192.0.2.1", "admin")]),
                         "1 つ目の kyoten の R が変わった")


if __name__ == "__main__":
    unittest.main()
