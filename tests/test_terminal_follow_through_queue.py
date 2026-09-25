"""受信の経路（queue_output → タイマー → _flush_pending_output）で大量に流しても、
最下部への追従と、上へスクロールして読んでいる位置が崩れないことを検証する。

画面への書き込みを 1 つの編集にまとめると、描いた直後のスクロールバーの最大値が
古いことがある（イベントループを 1 回回すと揃う）。追従するかどうかは値と最大値で
決めているので、古い最大値で「最下部にいる／いない」を取り違えないかを、先頭の
切り捨てが走る量を流して確かめる。
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

LIMIT = 1500
CHUNK = 4096


class FollowThroughQueueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        patcher = mock.patch.object(TerminalWidget, "MAX_DOCUMENT_BLOCKS", LIMIT)
        patcher.start()
        self.addCleanup(patcher.stop)
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.resize(900, 520)
        w.show()
        terminal = w.create_terminal_tab("dev")
        # スクロールバーが出て桁数が変わる組み直し（200ms 後）を先に済ませる
        w.append_output("dev", self._text(0, 100))
        self._settle(w)
        return w, terminal

    @staticmethod
    def _text(start, count):
        out = []
        for i in range(start, start + count):
            if i % 5 == 0:
                # 折り返す長い行も混ぜる（組版の行数が文書の行数と揃わない）
                out.append("F%06d %s\r\n" % (i, "w" * (150 + i % 400)))
            else:
                out.append("F%06d interface Gi1/0/%d\r\n" % (i, i % 48))
        return "".join(out)

    def _settle(self, w):
        end = time.time() + 60
        while time.time() < end and (w._pending_output or w._output_timer.isActive()
                                     or w._resize_timer.isActive()):
            self.app.processEvents()
            time.sleep(0.002)
        for _ in range(5):
            self.app.processEvents()

    def _flood(self, w, start, count, on_pass):
        """4096 文字ずつ受信の経路へ積み、1 片描くごとに on_pass(回) を呼ぶ。"""
        text = self._text(start, count)
        chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
        for step, k in enumerate(range(0, len(chunks), 4)):
            for chunk in chunks[k:k + 4]:
                w.queue_output("dev", chunk)
            self.app.processEvents()
            on_pass(step)
        self._settle(w)

    def test_following_stays_at_the_bottom_through_a_flood(self):
        """最下部で追従中に大量に流れても、各回のあと最下部にいて、最後の行が見えること。"""
        w, terminal = self._widget()
        bar = terminal.verticalScrollBar()
        lost = []

        def check(step):
            if not terminal._follow_output or bar.value() != bar.maximum():
                lost.append((step, terminal._follow_output, bar.value(), bar.maximum()))

        self._flood(w, 100, 4000, check)

        self.assertEqual(lost, [], "追従が外れた・最下部から離れた回がある")
        self.assertEqual(bar.value(), bar.maximum())
        self.assertTrue(self._visible_near_bottom(terminal, "F004099"),
                        "最後の行が見えていない")
        self.assertLessEqual(terminal.document().blockCount(), LIMIT)

    @staticmethod
    def _visible_near_bottom(terminal, needle):
        from PyQt6.QtCore import QPoint
        viewport = terminal.viewport()
        block = terminal.cursorForPosition(QPoint(0, viewport.height() - 2)).block()
        for _ in range(60):
            if needle in block.text():
                return True
            block = block.previous()
            if not block.isValid():
                break
        return False

    def test_reading_just_above_the_screen_is_kept_through_a_flood(self):
        """画面のすぐ上の記録を読んでいるときに大量に流れても、追従へ戻らないこと。

        描き終えたら、控えた行へスクロール位置を戻す。戻し先は最大値のすぐ手前
        なので、最大値が古い（小さい）と最大値に丸められて追従へ戻ってしまう。
        見えている先頭が画面領域の中だと、その行は描くたびに動くので、画面領域が
        始まる行の 1 つ上（記録の最後の行）が先頭に来る位置で読む。
        """
        from PyQt6.QtCore import QPoint
        w, terminal = self._widget()
        w.queue_output("dev", self._text(100, 600))
        self._settle(w)
        bar = terminal.verticalScrollBar()
        layout = terminal.document().documentLayout()
        region_top = layout.blockBoundingRect(terminal._region.block()).top()
        bar.setValue(int(region_top) - 1)
        self.app.processEvents()
        self.assertGreater(bar.maximum() - bar.value(), 0)
        self.assertLess(bar.maximum() - bar.value(), terminal.viewport().height() * 2,
                        "前提: 最下部のすぐ近くを読んでいる")
        self.assertFalse(terminal._follow_output, "前提: 上へスクロールしている")
        top = terminal.cursorForPosition(QPoint(0, 0)).block().text()
        moved = []

        def check(step):
            now = terminal.cursorForPosition(QPoint(0, 0)).block().text()
            if terminal._follow_output or now != top:
                moved.append((step, terminal._follow_output, now[:12]))

        # 先頭の切り捨ては走るが、読んでいる行（700 行目あたり）はまだ捨てられない量
        self._flood(w, 700, 1200, check)

        self.assertGreaterEqual(terminal.document().blockCount(), LIMIT,
                                "前提: 先頭の切り捨てが走る量を流した")
        self.assertEqual(moved, [], "読んでいる位置が動いた・追従へ戻った回がある")
        self.assertLess(bar.value(), bar.maximum())

    def test_following_survives_resizing_during_a_flood(self):
        """流れている最中に窓を縮め、文字を大きくしても、追従し続けること。"""
        w, terminal = self._widget()
        bar = terminal.verticalScrollBar()
        lost = []

        def check(step):
            resized = True
            if step == 10:
                w.resize(900, 519)
            elif step == 20:
                w.resize(900, 370)
            elif step == 30:
                settings = dict(w.current_terminal_settings(), font_size=14)
                w.apply_terminal_settings(settings)
            else:
                resized = False
            if resized:
                self._pump_resize(w)
            # 縮めた直後は最大値だけが増え、値は次に描くまで動かない（その間も
            # 追従はやめない）。最下部にいるかは、描いた後の回で確かめる
            if not terminal._follow_output or (
                    not resized and bar.value() != bar.maximum()):
                lost.append((step, terminal._follow_output, bar.value(), bar.maximum()))

        passes = []
        self._flood(w, 100, 8000, lambda step: (passes.append(step), check(step)))

        self.assertGreater(len(passes), 35, "前提: 3 回とも変えた後に描く回がある")
        self.assertEqual(lost, [], "窓や文字の大きさを変えたあと追従が外れた")
        self.assertEqual(bar.value(), bar.maximum())

    def _pump_resize(self, w):
        # 格子の組み直しは 200ms まとめてから。その間も受信は描かれ続ける
        end = time.time() + 0.3
        while time.time() < end or w._resize_timer.isActive():
            self.app.processEvents()
            time.sleep(0.002)


if __name__ == "__main__":
    unittest.main()
