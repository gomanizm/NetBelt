"""「コンソール接続」の折りたたみが、一覧の作り直しで開き直らないことを検証する。

実測（ac1dee7、検査役の probe_main.py P6）: ツリーの「コンソール接続」を
畳んでからボーレートを変えると、拒まれても受け付けられても expanded=True へ
戻っていた。DeviceTree._add_serial_ports_group が最後に無条件で
console_group.setExpanded(True) を呼んでおり、refresh_serial_ports はグループを
丸ごと作り直すので、畳んだ状態が項目と一緒に消えていた。USB シリアルの
抜き差しを検出したとき（_check_serial_ports）や、グループの追加・削除で
ツリーを読み直したとき（load_from_config）も同じ。

修正: 作り直す直前に isExpanded() を控え、作り直した項目へ戻す。まだ一度も
作っていないときは、これまでどおり開いた状態で作る。
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from PyQt6.QtCore import Qt

sys.path.insert(0, "src")

CONSOLE_GROUP = "コンソール接続"


class ConsoleGroupKeepsCollapsedTest(unittest.TestCase):
    """DeviceTree の「コンソール接続」グループの折りたたみ状態。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # 試験の中から差し替えられるポート一覧（USB の抜き差しを模す）
        self.ports = [{"port": "COM3", "description": "USB Serial Port"}]
        patcher = mock.patch(
            "ui.device_tree.list_serial_ports",
            side_effect=lambda: [dict(p) for p in self.ports])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _tree(self):
        """「コンソール接続」を作った直後の DeviceTree を返す。"""
        from ui.device_tree import DeviceTree
        tree = DeviceTree()
        # 定期チェックは試験の中から明示的に呼ぶ
        tree._serial_monitor_timer.stop()
        self.addCleanup(tree.deleteLater)
        tree.load_from_config([])
        return tree

    def _console_group(self, tree):
        root = tree.tree.invisibleRootItem()
        for i in range(root.childCount()):
            if root.child(i).text(0) == CONSOLE_GROUP:
                return root.child(i)
        self.fail("「%s」がツリーに無い" % CONSOLE_GROUP)

    def _collapse(self, tree):
        self._console_group(tree).setExpanded(False)
        self.assertFalse(self._console_group(tree).isExpanded(),
                         "前提が崩れている（畳めていない）")

    def _item_baudrate(self, tree, index=0):
        data = self._console_group(tree).child(index).data(
            0, Qt.ItemDataRole.UserRole)
        return (data or {}).get("baudrate")

    @staticmethod
    def _set_baudrate(tree, port, baudrate):
        """ボーレート変更の告知ログを飲み込んで _set_baudrate を呼ぶ。"""
        with redirect_stdout(io.StringIO()):
            tree._set_baudrate(port, baudrate)

    def test_the_console_group_is_expanded_when_it_is_first_built(self):
        """初めて作るときは、これまでどおり開いた状態であること。"""
        tree = self._tree()

        self.assertTrue(self._console_group(tree).isExpanded())

    def test_a_collapsed_console_group_survives_a_refresh(self):
        tree = self._tree()
        self._collapse(tree)

        tree.refresh_serial_ports()

        self.assertFalse(self._console_group(tree).isExpanded(),
                         "作り直しで開き直っている")

    def test_an_expanded_console_group_stays_expanded(self):
        """対照: 開いたまま使っている側は、作り直しても開いたままであること。"""
        tree = self._tree()

        tree.refresh_serial_ports()

        self.assertTrue(self._console_group(tree).isExpanded())

    def test_a_collapsed_console_group_survives_an_accepted_baudrate_change(self):
        tree = self._tree()
        self._collapse(tree)

        self._set_baudrate(tree, "COM3", 115200)

        self.assertFalse(self._console_group(tree).isExpanded(),
                         "受け付けられたボーレート変更で開き直っている")
        self.assertEqual(self._item_baudrate(tree), 115200)

    def test_a_collapsed_console_group_survives_a_refused_baudrate_change(self):
        """拒否の経路（MainWindow が restore_baudrate で書き戻す）でも畳んだまま。"""
        tree = self._tree()
        tree.serial_baudrate_changed.connect(
            lambda port, baudrate: tree.restore_baudrate(port, 9600))
        self._collapse(tree)

        self._set_baudrate(tree, "COM3", 115200)

        self.assertFalse(self._console_group(tree).isExpanded(),
                         "拒まれたボーレート変更で開き直っている")
        self.assertEqual(self._item_baudrate(tree), 9600,
                         "前提が崩れている（拒否が反映されていない）")

    def test_a_collapsed_console_group_survives_a_port_being_plugged_in(self):
        tree = self._tree()
        self._collapse(tree)

        self.ports.append({"port": "COM7", "description": "USB Serial Port"})
        tree._check_serial_ports()

        group = self._console_group(tree)
        self.assertEqual(group.childCount(), 2, "挿したポートが出ていない")
        self.assertFalse(group.isExpanded(),
                         "ポートの抜き差しで開き直っている")

    def test_a_collapsed_console_group_survives_a_config_reload(self):
        """グループの追加・削除でツリーを読み直しても畳んだままであること。"""
        tree = self._tree()
        self._collapse(tree)

        tree.load_from_config([{"name": "GroupA", "devices": [
            {"name": "R1", "host": "192.0.2.1"}]}])

        self.assertFalse(self._console_group(tree).isExpanded(),
                         "設定の読み直しで開き直っている")


if __name__ == "__main__":
    unittest.main()
