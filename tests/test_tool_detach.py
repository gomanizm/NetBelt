"""全ツールの別ウィンドウ化（タブ右クリック / タブのドラッグ）の検証。"""
import os
import sys
import unittest

sys.path.insert(0, "src")

ALL_TOOLS = ["sftp", "sftp_server", "tftp_server", "ftp_server", "syslog", "snmp"]


class ToolDetachTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _win(self):
        from ui.main_window import MainWindow
        return MainWindow()

    def test_every_tool_can_detach_and_reattach(self):
        from unittest import mock
        w = self._win()
        with mock.patch("PyQt6.QtWidgets.QWidget.show"):   # offscreen で top-level show() は落ちる
            for key in ALL_TOOLS:
                idx = w._tab_index[key]
                scroll = w.tool_tabs.widget(idx)
                panel = scroll.widget()
                w._detach_tool(key)
                self.assertIn(key, w._detached, "%s がデタッチされていない" % key)
                self.assertIsNot(scroll.widget(), panel)      # タブはプレースホルダ
                w._reattach_tool(key)
                self.assertNotIn(key, w._detached)
                self.assertIs(scroll.widget(), panel)         # 同一インスタンスが戻る

    def test_tool_key_at_maps_index_to_key(self):
        w = self._win()
        for key in ALL_TOOLS:
            self.assertEqual(w._tool_key_at(w._tab_index[key]), key)
        self.assertIsNone(w._tool_key_at(-1))
        self.assertIsNone(w._tool_key_at(999))

    def test_detach_by_index_used_by_drag(self):
        from unittest import mock
        w = self._win()
        key = "tftp_server"
        with mock.patch("PyQt6.QtWidgets.QWidget.show"):
            w._detach_tool_by_index(w._tab_index[key])       # ドラッグ経路が呼ぶ入口
            self.assertIn(key, w._detached)
            w._detach_tool_by_index(w._tab_index[key])       # 二重デタッチしない
            self.assertIn(key, w._detached)
            w._reattach_tool(key)

    def test_tabbar_is_detachable(self):
        from ui.main_window import DetachableTabBar
        w = self._win()
        self.assertIsInstance(w.tool_tabs.tabBar(), DetachableTabBar)

    def test_drag_threshold_triggers_detach(self):
        # 縦に十分ドラッグしたら切り離しコールバックが呼ばれる（閾値未満では呼ばれない）
        from PyQt6.QtCore import QPoint, QPointF, Qt, QEvent
        from PyQt6.QtGui import QMouseEvent
        from ui.main_window import DetachableTabBar
        called = []
        bar = DetachableTabBar(lambda i: called.append(i))
        bar.addTab("A")
        bar.addTab("B")
        bar.resize(200, 30)

        def press(pos):
            return QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(pos),
                               Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier)

        def move(pos):
            return QMouseEvent(QEvent.Type.MouseMove, QPointF(pos),
                               Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier)

        start = QPoint(20, 10)
        bar.mousePressEvent(press(start))
        bar.mouseMoveEvent(move(QPoint(20, 20)))      # わずかな移動では切り離さない
        self.assertEqual(called, [])
        bar.mousePressEvent(press(start))
        bar.mouseMoveEvent(move(QPoint(20, 200)))     # 大きく引き剥がす
        self.assertEqual(len(called), 1, "ドラッグでデタッチが呼ばれていない")

    def test_close_detached_window_returns_panel_to_tab(self):
        # 実際にウィンドウを閉じた場合（バツ印）にクラッシュせずタブへ戻ること
        from unittest import mock
        w = self._win()
        with mock.patch("PyQt6.QtWidgets.QWidget.show"):
            for key in ALL_TOOLS:
                w._detach_tool(key)
            self.assertEqual(sorted(w._detached.keys()), sorted(ALL_TOOLS))
            for key in ALL_TOOLS:
                win = w._detached[key]
                idx = w._tab_index[key]
                panel = win.layout().itemAt(0).widget()
                win.close()                       # closeEvent -> singleShot で戻す
                self.app.processEvents()          # 遅延実行を回す
                self.assertNotIn(key, w._detached, "%s が戻っていない" % key)
                self.assertIs(w.tool_tabs.widget(idx).widget(), panel)

    def test_closing_window_twice_is_safe(self):
        from unittest import mock
        w = self._win()
        with mock.patch("PyQt6.QtWidgets.QWidget.show"):
            w._detach_tool("syslog")
            win = w._detached["syslog"]
            win.close()
            self.app.processEvents()
            win.close()          # 二重クローズしても再入しない
            self.app.processEvents()
            self.assertNotIn("syslog", w._detached)

if __name__ == "__main__":
    unittest.main()
