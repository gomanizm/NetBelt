"""SNMP と Syslog のクリア／エクスポートの配置（G1 の前半）の回帰テスト。

何が起きていたか（実測、基準 1a206c2）:
  - SNMP GET/WALK: `[GET][WALK][停止]` の後ろに addStretch() があり、
    エクスポートとクリアだけが右端へ押し出されていた。幅 1100 で描くと
    クリアの右端は x=1085、GET の右端は x=95 で、同じ行の両端に分かれる。
  - SNMP Trap 受信: 4 つのボタンは 1 行に並ぶが行末に addStretch() が無く、
    余った幅がボタン自身に配られるため、幅 1100 では 1 個あたり 264px まで
    間延びして左寄せになっていなかった。
  - Syslog: クリアとエクスポートはツールバーの中の 🗑/💾 付きの項目で、
    一覧の上のボタン行という形になっていなかった。

利用者の決定（2026-09-23）: どの画面も、一覧／ログのすぐ上に左寄せ 1 行で
ボタンを並べる（Trap 受信と同じ形）。GET/WALK も
`[GET][WALK][停止][エクスポート][クリア]` の左寄せ 1 行にする（addStretch()
を外す）。Syslog のツールバーからは 🗑/💾 を外して同じボタン行へ移し、
ツールバーには受信の開始・停止・一時停止とポート指定を残す。

どう直したか: 各ボタン行の末尾に addStretch() を置いて、余った幅がボタンでは
なく行末の空きへ行くようにした（＝ボタンは自然な幅のまま左へ寄る）。
Syslog は 🗑 クリア／💾 エクスポートのツールバー項目を捨て、一覧のすぐ上に
`[エクスポート][クリア]` のボタン行を置いた。押したときの動作
（確認ダイアログ・記録中の拒否）は変えていない。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

# ボタンが自然な幅から広がっていないとみなす余裕（px）
STRETCH_TOLERANCE = 2
# 「左寄せ」とみなす、一覧の左端からの距離（px）
LEFT_MARGIN = 40
# 隣り合うボタンの間に許す空き（レイアウト既定の間隔は 6px）。これを超えて
# 空くのは、間に addStretch() が挟まって行が左右へ割れている状態
MAX_BUTTON_GAP = 12


class ButtonsAboveListTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _config(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-btnrow-")
        return ConfigManager(config_path=os.path.join(d, "config.json"))

    def _snmp(self, tab_index):
        from ui.snmp_panel import SNMPPanel
        panel = SNMPPanel(config_manager=self._config())
        self.addCleanup(panel.close)
        panel.main_tabs.setCurrentIndex(tab_index)
        return panel

    def _syslog(self):
        from ui.syslog_panel import SyslogPanel
        panel = SyslogPanel(config_manager=self._config())
        self.addCleanup(panel.close)
        return panel

    def _lay_out(self, panel, width):
        panel.resize(width, 900)
        panel.show()
        self.app.processEvents()

    def _top(self, panel, widget):
        return widget.mapTo(panel, widget.rect().topLeft()).y()

    def _left(self, panel, widget):
        return widget.mapTo(panel, widget.rect().topLeft()).x()

    def _check_row(self, name, panel, buttons, listing, width):
        """ボタンが一覧のすぐ上に左寄せ 1 行で並び、切れていないことを見る。"""
        self._lay_out(panel, width)
        # 1 行かどうかは上端の一致ではなく縦の重なりで見る。ボタンの自然な
        # 高さは文字によって 22〜25px と違い、行の中で上下中央へ置かれるので、
        # 同じ行でも上端は 1px ずれる
        tops = {b.text(): self._top(panel, b) for b in buttons}
        bottoms = {b.text(): self._top(panel, b) + b.height() for b in buttons}
        self.assertLess(
            max(tops.values()), min(bottoms.values()),
            "%s(幅%d): ボタンが 1 行に並んでいません: 上端=%r 下端=%r"
            % (name, width, tops, bottoms))

        row_bottom = max(bottoms.values())
        list_top = self._top(panel, listing)
        self.assertLessEqual(
            row_bottom, list_top,
            "%s(幅%d): ボタン行が一覧より上にありません (行の下端=%d 一覧の上端=%d)"
            % (name, width, row_bottom, list_top))

        leftmost = min(self._left(panel, b) for b in buttons)
        self.assertLessEqual(
            leftmost - self._left(panel, listing), LEFT_MARGIN,
            "%s(幅%d): ボタン行が左寄せになっていません (ボタン=%d 一覧=%d)"
            % (name, width, leftmost, self._left(panel, listing)))

        # 行が途中で割れていないこと（間に空きが挟まると、後ろのボタンだけが
        # 右端へ飛ばされる）
        for previous, following in zip(buttons, buttons[1:]):
            gap = (self._left(panel, following)
                   - previous.mapTo(panel, previous.rect().bottomRight()).x())
            self.assertLessEqual(
                gap, MAX_BUTTON_GAP,
                "%s(幅%d): 「%s」と「%s」の間が %dpx 空いており、1 行に続いて"
                "いません" % (name, width, previous.text(), following.text(), gap))

        for button in buttons:
            self.assertLessEqual(
                button.width(),
                button.sizeHint().width() + STRETCH_TOLERANCE,
                "%s(幅%d): 「%s」が自然な幅より広がっています (width=%d sizeHint=%d)"
                % (name, width, button.text(), button.width(),
                   button.sizeHint().width()))
            right = button.mapTo(panel, button.rect().bottomRight()).x()
            self.assertGreaterEqual(
                self._left(panel, button), 0,
                "%s(幅%d): 「%s」が左へはみ出しています" % (name, width, button.text()))
            self.assertLessEqual(
                right, panel.width(),
                "%s(幅%d): 「%s」が右へはみ出しています (right=%d width=%d)"
                % (name, width, button.text(), right, panel.width()))
        panel.hide()

    def test_snmp_get_walk_row(self):
        for width in (520, 1100):
            panel = self._snmp(0)
            self._check_row("SNMP GET/WALK", panel,
                            [panel.get_button, panel.walk_button,
                             panel.export_button, panel.clear_button],
                            panel.result_table, width)

    def test_snmp_trap_row(self):
        for width in (520, 1100):
            panel = self._snmp(1)
            self._check_row("SNMP Trap受信", panel,
                            [panel.trap_expand_button, panel.trap_collapse_button,
                             panel.trap_export_button, panel.trap_clear_button],
                            panel.trap_tree, width)

    def test_syslog_row(self):
        for width in (520, 1100):
            panel = self._syslog()
            self.assertTrue(hasattr(panel, "export_button"),
                            "Syslog にエクスポートのボタンがありません")
            self.assertTrue(hasattr(panel, "clear_button"),
                            "Syslog にクリアのボタンがありません")
            self._check_row("Syslog", panel,
                            [panel.export_button, panel.clear_button],
                            panel.table_view, width)

    def test_button_labels_are_the_same_everywhere(self):
        get_walk = self._snmp(0)
        trap = self._snmp(1)
        syslog = self._syslog()
        for name, button in (("SNMP GET/WALK エクスポート", get_walk.export_button),
                             ("SNMP Trap エクスポート", trap.trap_export_button),
                             ("Syslog エクスポート", syslog.export_button)):
            self.assertEqual(button.text(), "エクスポート",
                             "%s の表記が他と違います: %r" % (name, button.text()))
        for name, button in (("SNMP GET/WALK クリア", get_walk.clear_button),
                             ("SNMP Trap クリア", trap.trap_clear_button),
                             ("Syslog クリア", syslog.clear_button)):
            self.assertEqual(button.text(), "クリア",
                             "%s の表記が他と違います: %r" % (name, button.text()))

    def test_syslog_toolbar_keeps_only_reception_controls(self):
        """ツールバーに残すのは受信まわりだけ（クリア／エクスポートは外す）。"""
        from PyQt6.QtWidgets import QToolBar
        panel = self._syslog()
        toolbar = panel.findChild(QToolBar)
        self.assertIsNotNone(toolbar, "ツールバーが見つかりません")
        texts = [action.text() for action in toolbar.actions()]
        for unwanted in ("🗑 クリア", "💾 エクスポート"):
            self.assertNotIn(
                unwanted, texts,
                "ツールバーに %r が残っています: %r" % (unwanted, texts))
        self.assertIn("⏸ 一時停止", texts,
                      "ツールバーから一時停止が消えています: %r" % (texts,))


if __name__ == "__main__":
    unittest.main()
