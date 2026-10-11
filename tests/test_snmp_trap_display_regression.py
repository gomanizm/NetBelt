"""SNMP Trap の一覧の、表示・並び・選択・行の削除・画面の終了の今の挙動を押さえる。

_add_trap_to_tree の行の置き方を、リストを渡す insertRow / appendRow から、
項目を 1 つずつ置く形へ変えた（漏れの話は test_snmp_trap_display_heap_steady）。
9fee4af で確かめた今の挙動を押さえる。
- 表示: 5 列。親の行は時刻・送信元・セキュリティ・Trap 名・VarBind の数、子の行は
  OID の名前と値だけ。アイコン・色・字体・寄せは付けない（行の色が交互なのは
  ビューの設定）。
- 並び: 新しい Trap が先頭。見出しを押しても並べ替えない（ソートは無効）。
- 選択: 1 行ずつ。新しい Trap が届くと、選んでいた行（子の行でも）は 1 行下へ
  ずれて同じ Trap を指したまま。選んでいた Trap が上限で押し出されると、Qt の
  1 行選択の決まりで、残った中でいちばん古い Trap へ移る。選択に依存する操作は
  ダブルクリックした行の展開・折りたたみだけ（エクスポートは選択に関係なく全件）。
- 行の削除: max_traps を超えた古い行（末尾）を、一覧とエクスポート用のデータの
  両方から同じだけ捨てる。クリアは全部を消して見出しを残す。1 件ずつ消す操作は無い。
- 画面の終了: 行がある状態で閉じても落ちない。捨てれば、使用中のヒープブロックは
  作る前の数へ戻る（9fee4af では、表示した Trap 1 件あたり 2 ×（1 + 子の行数）
  ブロックが戻らない）。
"""
import gc
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

from test_snmp_trap_display_heap_steady import _busy_heap_blocks, _trap  # noqa: E402

# 揺れの許し幅。漏れていれば、画面を捨てる試験で 1 回 1000、区切りごとの
# 試験で 1 区切り 1000 増える。直した後に増えるのは、数える側の確保の 4〜6 と、
# 測っている間に OS がスレッドを起こした分（1 本で 40〜41。プロセスの
# スレッドの数と一致した）だけ
SLACK = 200
HEADER = ["時刻", "送信元IP", "セキュリティ", "Trap OID / VarBind", "値"]


class TrapDisplayRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作ったウィンドウはクラス終了まで保持する（test_snmp_trap_limit と同じ）
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _flush(self):
        """配送待ちの知らせと、deleteLater した物の破棄を済ませる

        2 回まわす。破棄の途中で deleteLater された物（シグナルの中継など）は、
        同じ回では消えない。
        """
        from PyQt6.QtCore import QCoreApplication, QEvent
        for _ in range(2):
            for _ in range(3):
                self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def _new_panel(self, max_traps):
        from ui.snmp_panel import SNMPPanel
        panel = SNMPPanel()
        panel.wait_for_background_work()
        panel.max_traps = max_traps
        panel.resize(1000, 900)
        panel.main_tabs.setCurrentIndex(1)      # Trap受信のタブ
        panel.show()
        self.app.processEvents()
        return panel

    def _dispose(self, panel):
        from PyQt6 import sip
        if sip.isdeleted(panel):
            return
        panel.close()
        panel.deleteLater()
        self._flush()

    def _panel(self, max_traps=5):
        panel = self._new_panel(max_traps)
        self.addCleanup(self._dispose, panel)
        return panel

    @staticmethod
    def _sources(panel):
        model = panel.trap_tree_model
        return [model.item(r, 1).text() for r in range(model.rowCount())]

    @staticmethod
    def _expanded(panel):
        model = panel.trap_tree_model
        return [panel.trap_tree.isExpanded(model.index(r, 0))
                for r in range(model.rowCount())]

    @staticmethod
    def _selection(panel):
        """選ばれている行の (親の行, 行) の一覧と、current の (親の行, 行, 列)"""
        tree = panel.trap_tree
        rows = sorted({(i.parent().row(), i.row())
                       for i in tree.selectionModel().selectedIndexes()})
        cur = tree.currentIndex()
        current = ((cur.parent().row(), cur.row(), cur.column())
                   if cur.isValid() else None)
        return rows, current

    def _click(self, panel, index, double=False):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        tree = panel.trap_tree
        tree.scrollTo(index)
        args = (tree.viewport(), Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier, tree.visualRect(index).center())
        QTest.mouseClick(*args)
        if double:
            # 実際の操作と同じく、1 回目の押し離しのあとに 2 回目を送る
            # （mouseDClick だけでは押した行が決まらず、doubleClicked が出ない）
            QTest.mouseDClick(*args)
        self.app.processEvents()

    def test_each_cell_shows_only_its_text(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QStandardItem
        panel = self._panel()
        panel._add_trap_to_tree(_trap(1, extra=2))
        panel._add_trap_to_tree(_trap(2, extra=0))
        model = panel.trap_tree_model
        self.assertEqual([model.headerData(c, Qt.Orientation.Horizontal)
                          for c in range(model.columnCount())], HEADER)
        self.assertTrue(panel.trap_tree.alternatingRowColors())
        self.assertEqual(model.item(0, 4).text(), "VarBindsなし")
        self.assertEqual(model.item(1, 4).text(), "2 VarBinds")

        R = Qt.ItemDataRole
        nothing = (R.DecorationRole, R.ToolTipRole, R.StatusTipRole,
                   R.WhatsThisRole, R.FontRole, R.TextAlignmentRole,
                   R.BackgroundRole, R.ForegroundRole, R.CheckStateRole,
                   R.SizeHintRole, R.UserRole)
        default_flags = QStandardItem("x").flags()
        cells = []
        for r in range(model.rowCount()):
            parent = model.index(r, 0)
            cells += [(model.index(r, c), "%d 行 %d 列" % (r, c)) for c in range(5)]
            for k in range(model.rowCount(parent)):
                cells += [(model.index(k, c, parent), "%d 行の子 %d の %d 列" % (r, k, c))
                          for c in range(5)]
        self.assertEqual(len(cells), 5 * (2 + 2))
        for index, where in cells:
            self.assertEqual(model.flags(index), default_flags, where)
            self.assertEqual(model.data(index, R.EditRole),
                             model.data(index, R.DisplayRole), where)
            for role in nothing:
                self.assertIsNone(model.data(index, role),
                                  "%s に %s が付いている" % (where, role))
            # 子を持つのは、VarBind のある親の第 0 列だけ
            self.assertEqual(model.hasChildren(index),
                             where == "1 行 0 列", where)

    def test_the_header_does_not_sort_and_the_newest_stays_on_top(self):
        from PyQt6.QtCore import QPoint, Qt
        from PyQt6.QtTest import QTest
        panel = self._panel()
        for n in range(4):
            panel._add_trap_to_tree(_trap(n, extra=n % 3))
        self.assertFalse(panel.trap_tree.isSortingEnabled())
        order = ["192.0.2.4", "192.0.2.3", "192.0.2.2", "192.0.2.1"]
        self.assertEqual(self._sources(panel), order)
        header = panel.trap_tree.header()
        for column in range(5):
            for _ in range(2):      # 2 回目は、並べ替えが効くなら逆順に当たる
                x = (header.sectionViewportPosition(column)
                     + header.sectionSize(column) // 2)
                QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.NoModifier,
                                 QPoint(x, header.height() // 2))
                self.app.processEvents()
                self.assertEqual(self._sources(panel), order,
                                 "見出し %d を押したら並びが変わった" % column)

        panel._add_trap_to_tree(_trap(4, extra=1))
        order.insert(0, "192.0.2.5")
        self.assertEqual(self._sources(panel), order)
        self.assertEqual([t["source_ip"] for t in panel.trap_data_list], order)

    def test_a_widened_column_does_not_squeeze_the_last_one_as_traps_arrive(self):
        """列を広げて横にあふれている間に Trap が届いても、列の幅が変わらないこと。

        最終列（値）は見出しが余白へ伸ばしている。9fee4af では、利用者が列を広げて
        横にあふれたあとも、伸ばした幅のまま残る。モデルが layoutChanged を出すと、
        見出しは最終列を伸ばす前の幅（100 px）へ戻し、あふれている間は伸ばし直さ
        ないので、Trap が届いた時点で値の列が縮んで戻らなくなる。
        """
        panel = self._panel(max_traps=10)
        for n in range(10):
            panel._add_trap_to_tree(_trap(n, extra=1))
        self.app.processEvents()
        tree = panel.trap_tree
        header = tree.header()
        stretched = header.sectionSize(4)
        self.assertGreater(stretched, header.defaultSectionSize(),
                           "前提: 最終列が余白へ伸びていない")
        header.resizeSection(3, tree.viewport().width() + 300)
        self.app.processEvents()
        widths = [header.sectionSize(c) for c in range(5)]
        self.assertGreater(sum(widths), tree.viewport().width(),
                           "前提: 列の合計が横にあふれていない")
        self.assertEqual(widths[4], stretched)

        panel._add_trap_to_tree(_trap(10, extra=1))     # 上限を超え、古い行も捨てる
        self.app.processEvents()
        self.assertEqual([header.sectionSize(c) for c in range(5)], widths,
                         "Trap が 1 件届いたら列の幅が変わった")
        for n in range(11, 16):
            panel._add_trap_to_tree(_trap(n, extra=n % 3))
        self.app.processEvents()
        self.assertEqual([header.sectionSize(c) for c in range(5)], widths,
                         "Trap が続けて届いたら列の幅が変わった")
        self.assertEqual(panel.trap_tree_model.rowCount(), 10)

    def test_the_selection_follows_its_trap_when_new_ones_arrive(self):
        panel = self._panel()
        model = panel.trap_tree_model
        for n in range(3):
            panel._add_trap_to_tree(_trap(n, extra=2))
        self._click(panel, model.index(1, 3))
        self.assertEqual(self._selection(panel), ([(-1, 1)], (-1, 1, 3)))

        panel._add_trap_to_tree(_trap(3, extra=2))
        self.assertEqual(self._selection(panel), ([(-1, 2)], (-1, 2, 3)))
        self.assertEqual(model.item(2, 1).text(), "192.0.2.2")

        # 子の行（VarBind）を選んでいても、同じ Trap の同じ子を指したまま
        panel.trap_tree.expand(model.index(2, 0))
        self._click(panel, model.index(1, 4, model.index(2, 0)))
        self.assertEqual(self._selection(panel), ([(2, 1)], (2, 1, 4)))
        panel._add_trap_to_tree(_trap(4, extra=2))
        self.assertEqual(self._selection(panel), ([(3, 1)], (3, 1, 4)))
        self.assertEqual(model.item(3, 1).text(), "192.0.2.2")
        self.assertEqual(model.item(3, 0).child(1, 4).text(), "if-00001-1")

    def test_a_trimmed_selection_moves_to_the_oldest_trap_left(self):
        """選んでいた Trap が上限で押し出されると、残ったいちばん古い Trap へ移る
        （Qt の 1 行選択の決まり。9fee4af と同じ）。"""
        panel = self._panel(max_traps=3)
        model = panel.trap_tree_model
        for n in range(3):
            panel._add_trap_to_tree(_trap(n, extra=1))
        self._click(panel, model.index(2, 0))
        self.assertEqual(self._selection(panel), ([(-1, 2)], (-1, 2, 0)))
        self.assertEqual(model.item(2, 1).text(), "192.0.2.1")

        panel._add_trap_to_tree(_trap(3, extra=1))
        self.assertEqual(self._sources(panel),
                         ["192.0.2.4", "192.0.2.3", "192.0.2.2"])
        self.assertEqual(self._selection(panel), ([(-1, 2)], (-1, 2, 0)))

    def test_double_click_toggles_only_the_row_clicked(self):
        panel = self._panel()
        model = panel.trap_tree_model
        for n in range(3):
            panel._add_trap_to_tree(_trap(n, extra=2))
        panel._on_trap_collapse_all_clicked()
        self._click(panel, model.index(1, 4), double=True)
        self.assertEqual(self._expanded(panel), [False, True, False])
        self._click(panel, model.index(1, 2), double=True)
        self.assertEqual(self._expanded(panel), [False, False, False])

    def test_expanded_rows_stay_expanded_when_new_traps_arrive(self):
        panel = self._panel()
        for n in range(3):
            panel._add_trap_to_tree(_trap(n, extra=2))
        panel._on_trap_expand_all_clicked()
        self.assertEqual(self._expanded(panel), [True, True, True])
        panel._add_trap_to_tree(_trap(3, extra=2))
        self.assertEqual(self._expanded(panel), [False, True, True, True])

    def test_the_oldest_rows_leave_the_tree_and_the_export_data_alike(self):
        panel = self._panel(max_traps=5)
        model = panel.trap_tree_model
        for n in range(8):
            panel._add_trap_to_tree(_trap(n, extra=n % 3))
        kept = list(range(7, 2, -1))            # 新しい順に Trap 7〜3
        self.assertEqual(self._sources(panel),
                         ["192.0.2.%d" % (n + 1) for n in kept])
        self.assertEqual([t["source_ip"] for t in panel.trap_data_list],
                         self._sources(panel))
        self.assertEqual([t["timestamp"] for t in panel.trap_data_list],
                         [model.item(r, 0).text() for r in range(5)])
        for r, n in enumerate(kept):
            parent = model.item(r, 0)
            self.assertEqual(parent.rowCount(), n % 3, "Trap %d の子の数" % n)
            self.assertEqual([parent.child(k, 4).text() for k in range(n % 3)],
                             ["if-%05d-%d" % (n, k) for k in range(n % 3)])

    def test_clear_empties_both_and_keeps_the_header(self):
        from PyQt6.QtCore import Qt
        panel = self._panel()
        model = panel.trap_tree_model
        for n in range(3):
            panel._add_trap_to_tree(_trap(n, extra=2))
        panel._on_trap_clear_clicked()
        self.assertEqual((model.rowCount(), model.columnCount()), (0, 5))
        self.assertEqual([model.headerData(c, Qt.Orientation.Horizontal)
                          for c in range(5)], HEADER)
        self.assertEqual(panel.trap_data_list, [])

        panel._add_trap_to_tree(_trap(9, extra=2))
        self.assertEqual([model.item(0, c).text() for c in (1, 2, 4)],
                         ["192.0.2.10", "v2c", "2 VarBinds"])
        self.assertEqual(model.item(0, 0).rowCount(), 2)
        self.assertEqual(len(panel.trap_data_list), 1)

    @unittest.skipUnless(sys.platform == "win32", "HeapWalk は Windows だけ")
    def test_the_growth_does_not_build_up_round_after_round(self):
        """上限に達したあと、区切りごとに増え続けないこと（傾きが 0）。"""
        panel = self._panel(max_traps=20)
        # 上限まで埋め、MIB の名前解決などの一度きりの確保も済ませておく
        for n in range(200):
            panel._add_trap_to_tree(_trap(n))
        self.app.processEvents()
        gc.collect()
        counts = [_busy_heap_blocks()]
        n = 200
        for _ in range(4):
            for _ in range(100):
                panel._add_trap_to_tree(_trap(n))
                n += 1
            self.app.processEvents()
            gc.collect()
            counts.append(_busy_heap_blocks())
        self.assertEqual(panel.trap_tree_model.rowCount(), 20)
        steps = [b - a for a, b in zip(counts, counts[1:])]
        # 漏れはどの区切りでも同じだけ増える。OS のスレッドの分は 1 区切りまで許す
        self.assertLessEqual(
            sum(step >= SLACK for step in steps), 1,
            "100 件ごとに使用中のヒープブロックが増え続ける: %s（1 件あたり %.1f）"
            % (steps, sorted(steps)[1] / 100))

    def _cycle(self, traps):
        """パネルを作って Trap を traps 件表示し、閉じて捨てる"""
        from PyQt6 import sip
        panel = self._new_panel(20)
        try:
            for n in range(traps):
                panel._add_trap_to_tree(_trap(n))
            self.app.processEvents()
            self.assertEqual(panel.trap_tree_model.rowCount(), min(traps, 20))
        finally:
            self._dispose(panel)
        self.assertTrue(sip.isdeleted(panel), "前提: パネルが破棄されていない")
        del panel
        gc.collect()

    @unittest.skipUnless(sys.platform == "win32", "HeapWalk は Windows だけ")
    def test_closing_the_panel_gives_the_memory_back(self):
        """行を並べたパネルを閉じて捨てると、作る前のブロック数へ戻ること。"""
        # パネルを作るときの一度きりの確保を済ませる
        for _ in range(2):
            self._cycle(30)
        grown = []
        # 漏れは毎回同じだけ残る。OS のスレッドの分を避けるため 3 回測って小さい方
        for _ in range(3):
            before = _busy_heap_blocks()
            self._cycle(100)
            grown.append(_busy_heap_blocks() - before)
        self.assertLess(
            min(grown), SLACK,
            "行を並べたパネルを閉じて捨てても、使用中のヒープブロックが残った: %s"
            "（表示で C++ 側に残った分は、パネルを捨てても戻らない）" % grown)

    def test_closing_the_main_window_with_rows_does_not_crash(self):
        """Trap と SFTP の一覧に行がある状態で、メインウィンドウを閉じられること。"""
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = tempfile.mkdtemp(prefix="netbelt-trap-close-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        cm.config.setdefault("settings", {})["snmp"] = {"max_traps": 20}
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            window = MainWindow()
        type(self)._windows.append(window)
        window.show()
        for n in range(50):
            window.snmp_panel._add_trap_to_tree(_trap(n))
        listing = [{"name": "file-%02d.cfg" % i, "size": 100, "mtime": 1700000000,
                    "mode": 0o100644, "is_dir": False, "is_link": False,
                    "permissions": "-rw-r--r--"} for i in range(30)]
        for _ in range(3):
            window.sftp_panel._update_file_list(listing)
        window.snmp_panel.trap_tree.expandAll()
        self.app.processEvents()

        window.close()
        self._flush()
        self.assertFalse(window.isVisible())
        self.assertEqual(window.snmp_panel.trap_tree_model.rowCount(), 20)
        self.assertEqual(window.sftp_panel.model.rowCount(), 30)


if __name__ == "__main__":
    unittest.main()
