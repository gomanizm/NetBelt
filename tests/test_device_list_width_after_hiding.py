"""接続先リストを隠して戻したとき、利用者が決めた幅へ戻ることを検証する。

何が起きていたか（実測、441ea02。offscreen で 1400x800 の窓を show）: 接続先
リストを 450 に広げて [450, 597, 345]、表示メニューで隠すと [0, 885, 511]、
戻すと [262, 626, 504] で、既定幅（DEVICE_LIST_WIDTH=250、最小幅で 262）に
なった。右クリックの「接続先リストを非表示」から隠しても同じ。

原因: 表示中の窓では、隠れたウィジェットに QSplitter.sizes() が 0 を返す。
_toggle_device_list は「幅 0 は畳まれている」と見なして、隠していただけの
リストを戻すときも既定幅で setSizes し、Qt が覚えている幅（450）を上書き
していた。Qt 自身は幅を覚えており、setVisible(True) のあとイベントを処理
すると 450 に戻る（その直前の sizes() はまだ 0）。

どう直したか: 隠れていたリストを出した直後に main_splitter.refresh() で
並べ直させ、Qt が覚えている幅を sizes() で見てから判断する。幅があれば
そのまま、無ければ（幅 0 に畳んでから隠した、隠している間にツールエリアの
復元が 0 で上書きした、など）これまでどおり既定幅で戻す。隠す前の幅を
自前で控えて Qt に任せる案は、ツールエリアの復元が隠れたリストの幅を 0 で
上書きした場合に幅 0 のまま「表示中」になるので採らなかった。

既存の tests/test_device_list_hiding.py は窓を show しない（その条件では
隠しても sizes() が変わらない）ので、ここでは show した窓で確かめる。
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


class DeviceListWidthAfterHidingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-list-width-")
        for patch in (
                mock.patch("core.config_manager.app_data_dir",
                           return_value=Path(self.dir)),
                mock.patch("ui.device_tree.list_serial_ports", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    def _pump(self, seconds=0.2):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    @staticmethod
    def _discard(window):
        window.terminal_widget._output_timer.stop()
        window.close()

    def _shown_window(self, list_width=450):
        """接続先リストを list_width に広げた、表示中のメインウィンドウ"""
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(self.dir, "config.json"))
            window = MainWindow()
        self.addCleanup(self._discard, window)
        window.resize(1400, 800)
        window.show()
        self._pump()
        sizes = window.main_splitter.sizes()
        window.main_splitter.setSizes(
            [list_width, sum(sizes) - list_width - sizes[2], sizes[2]])
        self._pump()
        self.assertEqual(window.main_splitter.sizes()[0], list_width,
                         "前提: 接続先リストを広げられた")
        return window

    def test_the_view_menu_brings_the_list_back_at_its_width(self):
        """表示メニューで隠して戻すと、広げた幅に戻ること"""
        window = self._shown_window(450)
        window._toggle_device_list()
        self._pump()
        self.assertTrue(window.device_tree.isHidden(), "前提: 隠れた")
        self.assertEqual(window.main_splitter.sizes()[0], 0,
                         "前提: 表示中の窓では、隠れたリストの幅は 0 と返る")

        window._toggle_device_list()
        self._pump()

        self.assertFalse(window.device_tree.isHidden())
        self.assertTrue(window.toggle_device_list_action.isChecked())
        self.assertEqual(window.main_splitter.sizes()[0], 450,
                         "利用者が広げた幅が既定幅で上書きされた")

    def test_hiding_from_the_context_menu_keeps_the_width(self):
        """右クリックの「非表示」から隠して表示メニューで戻しても、幅が戻ること"""
        window = self._shown_window(450)
        window.device_tree.hide_requested.emit()
        self._pump()
        self.assertTrue(window.device_tree.isHidden(), "前提: 隠れた")

        window._toggle_device_list()
        self._pump()

        self.assertEqual(window.main_splitter.sizes()[0], 450,
                         "利用者が広げた幅が既定幅で上書きされた")

    def test_a_list_collapsed_to_zero_still_comes_back_with_width(self):
        """対照: 表示中の窓で幅 0 まで畳んだ状態からは、1 回で既定幅に戻ること"""
        window = self._shown_window(450)
        sizes = window.main_splitter.sizes()
        window.main_splitter.setSizes([0, sum(sizes[:2])] + sizes[2:])
        self._pump()

        window._toggle_device_list()
        self._pump()

        self.assertFalse(window.device_tree.isHidden())
        self.assertGreaterEqual(window.main_splitter.sizes()[0], 100)

    def test_a_width_lost_while_hidden_falls_back_to_the_default(self):
        """対照: 隠している間に覚えている幅が 0 で上書きされても、幅 0 のまま出さないこと

        隠している間にツールエリアを 0 まで畳み、ツールを選んで戻すと、
        _restore_tool_area_width が隠れたリストの幅を 0 として setSizes する。
        """
        window = self._shown_window(450)
        window._toggle_device_list()
        self._pump()
        sizes = window.main_splitter.sizes()
        window.main_splitter.setSizes([sizes[0], sizes[1] + sizes[2], 0])
        self._pump()
        window._select_tool_tab(next(iter(window._tab_index)))
        self._pump()
        self.assertGreaterEqual(window.main_splitter.sizes()[2], 100,
                                "前提: ツールエリアが戻った")

        window._toggle_device_list()
        self._pump()

        self.assertFalse(window.device_tree.isHidden())
        self.assertGreaterEqual(window.main_splitter.sizes()[0], 100,
                                "表示中なのに幅 0 のままで、リストが見えない")

    def _hide_and_drag_the_tool_handle(self, window, dx):
        """リストを隠し、端末とツールエリアの仕切りを dx だけ引く（負で広げる）。

        利用者が仕切りを引くと QSplitter.moveSplitter が呼ばれ、splitterMoved が
        出る（setSizes では出ない）。このとき Qt は、隠れたリストの覚えている幅も
        0 にする。引く前と引いたあとの sizes() を返す。
        """
        window._toggle_device_list()
        self._pump()
        self.assertTrue(window.device_tree.isHidden(), "前提: 隠れた")
        splitter = window.main_splitter
        before = splitter.sizes()
        splitter.moveSplitter(splitter.handle(2).pos().x() + dx, 2)
        self._pump()
        after = splitter.sizes()
        self.assertGreater(abs(after[2] - before[2]), abs(dx) // 2,
                           "前提: 仕切りを引いてツールエリアの幅が変わった")
        return before, after

    def test_a_tool_width_dragged_while_hidden_is_kept(self):
        """隠している間に引いて決めたツールエリアの幅が、リストを戻しても削られないこと

        441ea02 と同じく、リストの幅は端末から取る。2ee577c では、並べ直させると
        リストの最小幅を端末とツールエリアから比例で取り、1400x800・リスト 400 で
        [0, 810, 586] → 戻す → [262, 656, 474] と、ツールエリアが 112 削られた
        （441ea02 は [262, 552, 578]）。
        """
        for dx in (-100, 150):
            with self.subTest(dx=dx):
                window = self._shown_window(400)
                _, dragged = self._hide_and_drag_the_tool_handle(window, dx)

                window._toggle_device_list()
                self._pump()

                sizes = window.main_splitter.sizes()
                self.assertFalse(window.device_tree.isHidden())
                self.assertGreaterEqual(sizes[0], 100, "リストが見えない")
                # 戻ってきた仕切りの幅と、最小幅に合わせた端数ぶんだけ揺れてよい
                self.assertAlmostEqual(
                    sizes[2], dragged[2], delta=20,
                    msg="隠している間に決めたツールエリアの幅が削られた: %s -> %s"
                        % (dragged, sizes))

    def test_the_next_round_trip_after_a_drag_keeps_the_widths(self):
        """対照: 引いたあとに戻し、もう一度隠して戻しても、幅が動かないこと

        仕切りを引いた印が残ると、次の往復でも既定幅を端末から取り直し、
        往復のたびにツールエリアが広がっていく。
        """
        window = self._shown_window(400)
        self._hide_and_drag_the_tool_handle(window, -100)
        window._toggle_device_list()
        self._pump()
        shown = window.main_splitter.sizes()

        window._toggle_device_list()
        self._pump()
        window._toggle_device_list()
        self._pump()

        self.assertEqual(window.main_splitter.sizes(), shown)

    def test_a_hidden_tool_area_still_comes_back_with_width_after_a_drag(self):
        """対照: 引いたあとにツールエリアも隠した場合、どちらも幅 0 のまま出さないこと

        ツールエリアが隠れている間の sizes() はそこに 0 を返す。それを
        setSizes へ渡すと、ツールエリアを出しても幅 0 のまま見えない。
        """
        window = self._shown_window(400)
        self._hide_and_drag_the_tool_handle(window, -100)
        window._toggle_tool_area()
        self._pump()
        self.assertTrue(window.tool_tabs.isHidden(), "前提: ツールエリアが隠れた")

        window._toggle_device_list()
        self._pump()
        window._toggle_tool_area()
        self._pump()

        sizes = window.main_splitter.sizes()
        self.assertFalse(window.device_tree.isHidden())
        self.assertFalse(window.tool_tabs.isHidden())
        self.assertGreaterEqual(sizes[0], 100, "リストが見えない")
        self.assertGreaterEqual(sizes[2], 100, "ツールエリアが幅 0 のまま戻った")


if __name__ == "__main__":
    unittest.main()
