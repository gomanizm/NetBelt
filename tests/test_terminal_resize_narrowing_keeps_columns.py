r"""桁を狭めると、受信済みの桁が黙って消える件を検証する。

折り返し待ち (_pending_wrap) を set_size で持ち越すようにした修正
(tests/test_terminal_resize_pending_wrap.py) が、桁が狭くなる側まで
持ち越していた。持ち越したまま次の 1 文字が来ると
_linefeed(from_wrap=True) の `del self.lines[row][self.cols:]` が
「新しい (狭い) 桁」で行を切るため、右端の外に書かれていた文字が
画面・文書・コピー・全ログ保存からまとめて消えていた。

実測 (基準 81664d2):
  Screen(3, 8) へ '01234567' -> set_size(3, 4) -> 'A'
    HEAD: text() == ['0123', 'A', '']      (4 文字が消える)
    正 : text() == ['012A4567', '', '']    (1 文字が上書きされるだけ)
  Screen(24, 80) へ 80 文字 -> set_size(24, 40) -> '!'
    HEAD: 1 行目が 40 文字 (40 文字が消える)
  TerminalWidget を通した文書でも同じ (80 -> 60 で 20 文字、
  20 -> 8 で 12 文字)。set_size を呼ぶのは窓を横に狭める操作と
  文字を大きくする操作なので、どちらも日常操作で起きる。

直し方: 桁が狭くなるときは折り返し待ちを持ち越さない (基準の
振る舞いへ戻す)。持ち越しが要るのは「桁が変わらない行数だけの
変更」で、直したかった不具合 (右端の 1 文字が上書きされる) は
そこで起きていた。狭める側でも持ち越すには、折り返し待ちを作った
時点の桁を覚えて切り詰めをそこで行う必要があり、状態が 1 つ増える。
失う量 (旧桁 - 新桁 文字) が直る量 (1 文字) より大きいので、
保守的な側を採った。

これに合わせて tests/test_terminal_resize_pending_wrap.py の
test_narrowing_keeps_the_pending_wrap を、新しい仕様
(狭めたら落とす) の期待へ書き換えてある。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def ruler(width):
    """桁が分かる目盛り ('0123456789' の繰り返し)。"""
    return "".join(str(i % 10) for i in range(width))


class NarrowingKeepsColumnsTest(unittest.TestCase):
    def test_narrowing_drops_the_pending_wrap(self):
        """桁が狭くなったら、折り返し待ちを持ち越さないこと。"""
        s = feed(Screen(24, 80), "-" * 80)
        self.assertTrue(s._pending_wrap, "前提: 折り返し待ちになっていない")
        s.set_size(24, 40)
        self.assertFalse(s._pending_wrap, "折り返し待ちを持ち越している")

    def test_the_next_character_does_not_cut_the_row_to_the_new_width(self):
        """狭めた直後の 1 文字が、右端の外の文字を消さないこと。"""
        s = feed(Screen(3, 8), ruler(8))
        s.set_size(3, 4)
        feed(s, "A")
        self.assertEqual(s.text(), ["012A4567", "", ""])

    def test_every_received_column_survives_a_narrowing(self):
        """狭め方を変えても、受信した桁数がそのまま残ること。"""
        for was, now in ((80, 40), (80, 60), (20, 8)):
            with self.subTest(was=was, now=now):
                s = feed(Screen(24, was), ruler(was))
                s.set_size(24, now)
                feed(s, "!")
                self.assertEqual(len(s.text()[0]), was,
                                 "%d -> %d で %d 文字が消えた"
                                 % (was, now, was - len(s.text()[0])))
                self.assertEqual(s.text()[0],
                                 ruler(was)[:now - 1] + "!"
                                 + ruler(was)[now:])

    def test_a_row_count_change_still_keeps_the_pending_wrap(self):
        """桁が変わらない変更では、これまでどおり持ち越すこと。"""
        s = feed(Screen(2, 4), "ABCD")
        s.set_size(3, 4)
        self.assertTrue(s._pending_wrap, "行数だけの変更で落ちている")
        feed(s, "X")
        self.assertEqual(s.text(), ["ABCD", "X", ""])

    def test_widening_still_continues_past_the_old_right_edge(self):
        """桁が広がったときの続き方は、これまでどおりであること。"""
        s = feed(Screen(24, 80), "-" * 80)
        s.set_size(24, 100)
        self.assertFalse(s._pending_wrap)
        feed(s, "continued")
        self.assertEqual(s.text()[0], "-" * 80 + "continued")


class NarrowingThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_every_received_column(self):
        """利用者が見る文書でも、狭めた外の桁が消えないこと。"""
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        screen = terminal._screen
        cols = screen.cols
        w.queue_output("dev", ruler(cols))
        w._flush_pending_output()
        screen.set_size(screen.rows, cols // 2)
        w.queue_output("dev", "!")
        w._flush_pending_output()
        self.assertEqual(len(terminal.toPlainText().split("\n")[0]), cols,
                         "窓を狭めたら受信済みの桁が消えている")


if __name__ == "__main__":
    unittest.main()
