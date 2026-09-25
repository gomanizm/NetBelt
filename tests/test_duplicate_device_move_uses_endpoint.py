"""同名の機器が 2 台あるとき、ドラッグで掴んだ方が動くことを検証する。

実測（81664d2）: 機器の編集と削除は「接続先（device_endpoint）」で 1 台に
絞るようになったが、3 つ目の経路であるドラッグ＆ドロップの移動だけが機器名の
ままだった。ConfigManager.move_device() は source_group['devices'] を名前で
線形探索して最初の 1 件を取り出し、DeviceTree の dropEvent は手元に
device_data を持ちながら device_moved = pyqtSignal(str, str, str) で機器名
しか載せていなかったので、MainWindow._on_device_moved には絞り込む材料が
届かない。

検査役の実測（ConfigManager 単体）: kyoten に rtr1@192.0.2.11 と
rtr1@192.0.2.12 を置き、other は空にして
move_device('kyoten', 'other', 'rtr1') -> True。結果は
kyoten: ['192.0.2.12'] / other: ['192.0.2.11'] で、掴んでいない 1 台目が
動いた。画面には警告も出ず、状態バーは「機器 'rtr1' を 'kyoten' から
'other' に移動しました」と出る。

直し方: 削除（device_delete）と同じ形にそろえる。device_moved に
device_data を載せ、MainWindow._on_device_moved が device_endpoint() で
接続先を作って move_device へ渡し、move_device は名前の線形探索を
_device_index(devices, name, endpoint) に置き換えて、その位置の 1 件だけを
取り出す（同一内容の辞書が 2 つあると list.remove が先頭を消すため、
位置で消す）。endpoint を省略したときは今までどおり先頭の 1 件。
移動先の同名チェックは「同名は移動できない」仕様のままなので変えない。
"""
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


class MoveDeviceEndpointTest(unittest.TestCase):
    """ConfigManager.move_device が接続先で 1 台に絞ること。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-movedup-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        from core.config_manager import ConfigManager
        self.cm = ConfigManager(config_path=str(self.dir / "config.json"))
        self.assertTrue(self.cm.add_group("kyoten"))
        self.assertTrue(self.cm.add_group("other"))
        # 同名 2 台は手編集の config.json でしか作れないので、直接置く
        self.cm.get_group("kyoten")["devices"] = [
            _device("rtr1", "192.0.2.11"),
            _device("rtr1", "192.0.2.12", username="operator"),
        ]
        self.assertTrue(self.cm.save_config())

    def _hosts(self, group_name):
        return [d["host"] for d in self.cm.get_group(group_name)["devices"]]

    def test_the_second_entry_moves_when_its_endpoint_is_given(self):
        from core.config_manager import device_endpoint
        target = self.cm.get_group("kyoten")["devices"][1]

        self.assertTrue(self.cm.move_device(
            "kyoten", "other", "rtr1", endpoint=device_endpoint(target)))

        self.assertEqual(self._hosts("kyoten"), ["192.0.2.11"],
                         "掴んでいない 1 台目が動いている")
        self.assertEqual(self._hosts("other"), ["192.0.2.12"])

    def test_the_first_entry_moves_when_its_endpoint_is_given(self):
        from core.config_manager import device_endpoint
        target = self.cm.get_group("kyoten")["devices"][0]

        self.assertTrue(self.cm.move_device(
            "kyoten", "other", "rtr1", endpoint=device_endpoint(target)))

        self.assertEqual(self._hosts("kyoten"), ["192.0.2.12"])
        self.assertEqual(self._hosts("other"), ["192.0.2.11"])

    def test_the_moved_entry_keeps_its_own_fields(self):
        """動くのは掴んだ 1 台そのもの（別の 1 台の中身ではない）こと。"""
        from core.config_manager import device_endpoint
        target = self.cm.get_group("kyoten")["devices"][1]

        self.cm.move_device("kyoten", "other", "rtr1",
                            endpoint=device_endpoint(target))

        moved = self.cm.get_group("other")["devices"][0]
        self.assertEqual((moved["host"], moved["username"]),
                         ("192.0.2.12", "operator"))

    def test_an_omitted_endpoint_still_moves_the_first_entry(self):
        """endpoint を渡さない呼び出しは今までどおり先頭の 1 件を動かすこと。"""
        self.assertTrue(self.cm.move_device("kyoten", "other", "rtr1"))

        self.assertEqual(self._hosts("kyoten"), ["192.0.2.12"])
        self.assertEqual(self._hosts("other"), ["192.0.2.11"])

    def test_a_missing_device_is_still_refused(self):
        self.assertFalse(self.cm.move_device("kyoten", "other", "sw1"))
        self.assertEqual(self._hosts("kyoten"), ["192.0.2.11", "192.0.2.12"])

    def test_a_duplicate_name_in_the_target_group_is_still_refused(self):
        """移動先に同名がいれば、今までどおり断ること。"""
        from core.config_manager import device_endpoint
        self.cm.get_group("other")["devices"] = [_device("rtr1", "192.0.2.20")]
        target = self.cm.get_group("kyoten")["devices"][1]

        self.assertFalse(self.cm.move_device(
            "kyoten", "other", "rtr1", endpoint=device_endpoint(target)))

        self.assertEqual(self._hosts("kyoten"), ["192.0.2.11", "192.0.2.12"])
        self.assertEqual(self._hosts("other"), ["192.0.2.20"])


class MoveDeviceFromTheTreeTest(unittest.TestCase):
    """接続先リストのドラッグ＆ドロップが、掴んだ 1 台を動かすこと。"""

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
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-movedupui-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            window = MainWindow()
        type(self)._windows.append(window)
        cm = window.config_manager
        self.assertTrue(cm.add_group("kyoten"))
        self.assertTrue(cm.add_group("other"))
        cm.get_group("kyoten")["devices"] = [
            _device("rtr1", "192.0.2.11"),
            _device("rtr1", "192.0.2.12", username="operator"),
        ]
        self.assertTrue(cm.save_config())
        window._load_devices()
        return window

    def _entries(self, window, group_name):
        return [(d["host"], d["username"])
                for d in window.config_manager.get_group(group_name)["devices"]]

    def _drop_second_entry_on_other(self, window):
        """2 台目の項目を掴んで other グループへ落とす（ツリーの実際の経路）。"""
        from PyQt6.QtCore import Qt
        tree = window.device_tree

        group_item = None
        for i in range(tree.tree.topLevelItemCount()):
            item = tree.tree.topLevelItem(i)
            if item.text(0) == "kyoten":
                group_item = item
                break
        self.assertIsNotNone(group_item, "前提: kyoten グループがツリーにある")
        second = group_item.child(1)
        self.assertEqual(
            second.data(0, Qt.ItemDataRole.UserRole)["host"], "192.0.2.12",
            "前提: 2 台目は 192.0.2.12")

        target_item = None
        for i in range(tree.tree.topLevelItemCount()):
            item = tree.tree.topLevelItem(i)
            if item.text(0) == "other":
                target_item = item
                break
        self.assertIsNotNone(target_item, "前提: other グループがツリーにある")

        tree.tree.setCurrentItem(second)
        event = mock.Mock()
        with mock.patch.object(tree.tree, "itemAt", return_value=target_item), \
             mock.patch.object(tree.tree, "dropIndicatorPosition",
                               return_value=None):
            tree._on_drop_event(event)
        self.assertTrue(event.accept.called, "ドロップが受け付けられていない")

    def test_dropping_the_second_entry_moves_that_entry(self):
        """ツリーのドロップから移動まで通して、動くのは掴んだ 2 台目だけ。

        画面から実際に踏める経路をそのまま通す（device_moved は
        MainWindow._on_device_moved に繋がっている）。
        """
        window = self._window()

        self._drop_second_entry_on_other(window)

        self.assertEqual(self._entries(window, "other"),
                         [("192.0.2.12", "operator")],
                         "掴んでいない 1 台目が移動している")
        self.assertEqual(self._entries(window, "kyoten"),
                         [("192.0.2.11", "admin")])

    def test_the_tree_sends_the_dragged_device_data(self):
        """dropEvent が device_moved に device_data も載せること。"""
        window = self._window()
        received = []
        window.device_tree.device_moved.connect(
            lambda *args: received.append(args))

        self._drop_second_entry_on_other(window)

        self.assertTrue(received, "device_moved が出ていない")
        args = received[0]
        self.assertEqual(args[:3], ("kyoten", "other", "rtr1"))
        self.assertEqual(len(args), 4,
                         "device_moved に device_data が載っていない")
        self.assertEqual(args[3].get("host"), "192.0.2.12",
                         "掴んだ 1 台の device_data が載っていない")

    def test_dragging_the_second_entry_keeps_the_first_one(self):
        """2 台目をドラッグしたら、動くのは 2 台目だけであること。"""
        window = self._window()
        dragged = window.config_manager.get_group("kyoten")["devices"][1]

        window._on_device_moved("kyoten", "other", "rtr1", dict(dragged))

        self.assertEqual(self._entries(window, "kyoten"),
                         [("192.0.2.11", "admin")],
                         "掴んでいない 1 台目が動いている")
        self.assertEqual(self._entries(window, "other"),
                         [("192.0.2.12", "operator")])

    def test_dragging_the_first_entry_keeps_the_second_one(self):
        window = self._window()
        dragged = window.config_manager.get_group("kyoten")["devices"][0]

        window._on_device_moved("kyoten", "other", "rtr1", dict(dragged))

        self.assertEqual(self._entries(window, "kyoten"),
                         [("192.0.2.12", "operator")])
        self.assertEqual(self._entries(window, "other"),
                         [("192.0.2.11", "admin")])


if __name__ == "__main__":
    unittest.main()
