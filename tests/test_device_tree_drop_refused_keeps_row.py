# -*- coding: utf-8 -*-
"""機器のドロップ移動が断られても、掴んだ行が接続先リストから消えないことを検証する。

何が起きていたか。DeviceTree は QTreeWidget の dropEvent を差し替え、
device_moved を出したあと無条件に MoveAction のまま accept していた。
Qt の startDrag は、ドロップが MoveAction で終わると「自分で動かした」
印が無い限り選択中の行をモデルから消す。移動に成功したときは
MainWindow がツリーを作り直すので何も起きないが、断られたとき
（config.json を保存できない、など）は作り直さないため、掴んだ機器の
行だけが画面から消えていた（設定には残る）。

offscreen では QDrag::exec が走らないので、ドロップを処理し終えた時点で
「MoveAction で受け付けた」状態になっていないこと（= Qt に行を消させない
こと）を見る。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class DropRefusedKeepsRowTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _drop(self, on_moved):
        from PyQt6.QtCore import Qt, QPointF, QMimeData
        from PyQt6.QtGui import QDropEvent
        from ui.device_tree import DeviceTree

        with mock.patch("ui.device_tree.list_serial_ports", return_value=[]):
            tree = DeviceTree()
            tree.load_from_config([
                {"name": "A", "devices": [{"name": "rtr1",
                                           "host": "192.0.2.1"}]},
                {"name": "B", "devices": []},
            ])
        tree.stop_serial_monitor()
        self.addCleanup(tree.deleteLater)

        source_item = tree.tree.topLevelItem(0).child(0)
        target_item = tree.tree.topLevelItem(1)
        tree.tree.setCurrentItem(source_item)

        moved = []
        tree.device_moved.connect(lambda *args: (moved.append(args),
                                                 on_moved(tree)))

        event = QDropEvent(QPointF(1, 1), Qt.DropAction.MoveAction,
                           QMimeData(), Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)
        with mock.patch.object(tree.tree, "itemAt", return_value=target_item):
            tree._on_drop_event(event)
        return tree, event, moved

    def test_refused_move_does_not_report_move_action(self):
        """移動が断られた（ツリーを作り直さない）とき、MoveAction で受け付けないこと。"""
        from PyQt6.QtCore import Qt
        tree, event, moved = self._drop(lambda tree: None)

        self.assertEqual(len(moved), 1, "移動の依頼は出ること")
        self.assertFalse(
            event.isAccepted()
            and event.dropAction() == Qt.DropAction.MoveAction,
            "MoveAction で受け付けると Qt が掴んだ行を消す")
        # 行はまだツリーにある
        self.assertEqual(tree.tree.topLevelItem(0).childCount(), 1)

    def test_move_request_contents_unchanged(self):
        """移動の依頼（移動元・移動先・機器名）は今までどおりであること。"""
        tree, event, moved = self._drop(lambda tree: None)
        self.assertEqual(moved[0][0], "A")
        self.assertEqual(moved[0][1], "B")
        self.assertEqual(moved[0][2], "rtr1")


if __name__ == "__main__":
    unittest.main()
