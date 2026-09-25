"""破棄済みの CheckableComboBox へ届いたイベントで、フィルタが例外を出さないことを検証する。

何が起きていたか（検査役の実測、一時ブランチの 3f37f3c）。tests/ の 9 ファイル
（config_server_settings・ftp_server 系 6 本・main_window_layout・
pending_update_quit）を 1 プロセスで順に流すと、16 回中 14 回、プロセスが
0xC0000409 で落ちた。-s と QT_FORCE_STDERR_LOGGING=1 で出した traceback は
src/ui/syslog_panel.py の CheckableComboBox.eventFilter の先頭
（obj is self.lineEdit()）で

    RuntimeError: wrapped C/C++ object of type CheckableComboBox has been deleted

だった。CheckableComboBox は自分の入力欄と一覧に、自分をイベントフィルタとして
掛けている。窓が GC で片付けられる途中で、ラッパーが破棄済みになったあとも
フィルタとして呼ばれ、self を引いたところで例外になる。sys.excepthook が
既定のままのテストでは、PyQt が qFatal でプロセスを落とす（アプリは
excepthook を差し替えているので、落ちずに記録に残る）。main_window を
e143a5f・286d8fb・69af9ba に戻しても同じ落ち方をするので、以前からある。

GC の片付け順しだいで起きるので、テストではその形（破棄済みのラッパーで
フィルタが呼ばれる）を直接作る。

どう直したか: eventFilter の先頭で、自分が破棄済みなら何もせず False を返す
（イベントは素通しになる。破棄済みの窓部品に対して、することは無い）。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class CheckableComboDeletedFilterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_a_deleted_combo_passes_events_through(self):
        """破棄済みのラッパーでフィルタが呼ばれても、例外にせず False を返すこと。"""
        from PyQt6 import sip
        from PyQt6.QtCore import QEvent, QObject
        from ui.syslog_panel import CheckableComboBox
        combo = CheckableComboBox()
        combo.add_checkable("INFO")
        sip.delete(combo)
        other = QObject()
        self.addCleanup(other.deleteLater)

        result = combo.eventFilter(
            other, QEvent(QEvent.Type.MouseButtonRelease))

        self.assertIs(result, False)

    def test_a_live_combo_still_toggles_from_its_text_field(self):
        """生きている間は、入力欄のクリックでこれまでどおり一覧を開くこと（対照）。"""
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        from ui.syslog_panel import CheckableComboBox
        combo = CheckableComboBox()
        self.addCleanup(combo.deleteLater)
        combo.add_checkable("INFO")
        combo.show()
        release = QMouseEvent(
            QEvent.Type.MouseButtonRelease, QPointF(1, 1), QPointF(1, 1),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier)

        consumed = combo.eventFilter(combo.lineEdit(), release)

        self.assertTrue(consumed, "入力欄のクリックを消費していない")
        self.assertTrue(combo.view().isVisible(), "一覧が開いていない")
        combo.hidePopup()


if __name__ == "__main__":
    unittest.main()
