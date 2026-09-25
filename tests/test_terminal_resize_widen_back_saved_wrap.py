r"""狭めた窓を広げ直したあと、保存領域から戻す 1 文字が桁を潰す件を検証する。

「窓を狭めたら折り返し待ちを解く」は利用者の決定 (2026-09-20)。生きて
いるカーソルの待ちは set_size が解き、保存領域 (DECSC / ?1048 / 1049 /
47 / 1047) の待ちも set_size がまとめて解くようにしてあった。

ところが保存領域の位置は狭めても丸めないのに待ちだけ落とすので、狭めた
あと窓を広げ直してから復元すると、「旧桁の右端で待たずにそのまま上書き
する」状態になる。狭める前の桁へ戻っているのだから、待ちは要る。

実測 (基準 429bf80):
  Screen(3, 8) へ '01234567' + ESC 7 -> set_size(3, 4) -> set_size(3, 8)
  -> ESC 8 + 'A'
    HEAD : ['0123456A', '', '']    (受信した '7' が消える)
    正   : ['01234567', 'A', '']
  ?1049h -> set_size(3, 4) -> set_size(3, 8) -> ?1049l + 'A'
    HEAD : ['012A4567', '', '']    (受信した '3' が消える)
  TerminalWidget の文書でも同じ (既定 49 桁 -> 24 桁 -> 49 桁)。
    HEAD : ESC 8 + '!' の '!' が 49 桁目 (位置 48) を潰す。
           ?1049l + '!' は 24 桁目 (位置 23) を潰す
    正   : '!' は次の行へ落ち、受信した 49 桁がそのまま残る
窓を狭めてから元へ戻す・文字を大きくしてから戻す、どちらも日常操作。

直し方: set_size でまとめて解くのをやめ、戻すときに、戻し先の行がいまの
桁より長いとき (= 窓を狭めた跡があるとき) だけ解く。広げ直したあとは行が
また桁に収まるので、待ちはそのまま戻る。狭めたままのときの振る舞い
(2026-09-20 の決定) は変わらない。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)

DECSC, DECRC = ESC + "7", ESC + "8"
ALT_IN, ALT_OUT = ESC + "[?1049h", ESC + "[?1049l"
SAVE_1048, RESTORE_1048 = ESC + "[?1048h", ESC + "[?1048l"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def text_of(screen):
    return ["".join(cell[0] for cell in line).rstrip()
            for line in screen.lines]


def ruler(n):
    return "".join(str(i % 10) for i in range(n))


class WideningBackKeepsTheSavedPendingWrapTest(unittest.TestCase):
    def test_a_restored_cursor_after_widening_back_keeps_the_columns(self):
        """狭めて広げ直したあとの ESC 8 が、右端の桁を潰さないこと。"""
        screen = feed(Screen(3, 8), "01234567" + DECSC)
        screen.set_size(3, 4)
        screen.set_size(3, 8)
        feed(screen, DECRC + "A")
        self.assertEqual(text_of(screen), ["01234567", "A", ""])

    def test_leaving_the_alternate_screen_after_widening_back(self):
        """代替画面の中で狭めて広げ直しても、桁を潰さないこと。"""
        screen = feed(Screen(3, 8), "01234567" + ALT_IN)
        screen.set_size(3, 4)
        screen.set_size(3, 8)
        feed(screen, ALT_OUT + "A")
        self.assertEqual(text_of(screen), ["01234567", "A", ""])

    def test_a_restored_1048_cursor_after_widening_back(self):
        """?1048 の復元も同じ保存領域なので、同じく潰さないこと。"""
        screen = feed(Screen(3, 8), "01234567" + SAVE_1048)
        screen.set_size(3, 4)
        screen.set_size(3, 8)
        feed(screen, RESTORE_1048 + "A")
        self.assertEqual(text_of(screen), ["01234567", "A", ""])

    def test_a_save_kept_on_the_alternate_screen_survives_widening_back(self):
        """裏へ退避した保存領域の待ちも、広げ直したら戻ること。"""
        # メイン画面で ESC 7 したあと 47h で代替画面へ行き、そこで窓を
        # 狭めて広げ直す。メイン画面の保存領域は裏 (_other_saved) にある
        screen = feed(Screen(3, 8), "01234567" + DECSC + ESC + "[?47h")
        screen.set_size(3, 4)
        screen.set_size(3, 8)
        feed(screen, ESC + "[?47l" + DECRC + "A")
        self.assertEqual(text_of(screen), ["01234567", "A", ""])


class NarrowingStillReleasesThePendingWrapTest(unittest.TestCase):
    """2026-09-20 の決定 (狭めたら解く) が変わっていないことの対照。"""

    def test_a_restored_cursor_after_narrowing_alone_still_releases(self):
        """狭めたままの ESC 8 は、これまでどおり待ちを解くこと。"""
        screen = feed(Screen(3, 8), "01234567" + DECSC)
        screen.set_size(3, 4)
        feed(screen, DECRC + "A")
        self.assertEqual(text_of(screen), ["012A4567", "", ""])

    def test_leaving_the_alternate_screen_after_narrowing_alone(self):
        """狭めたままの 1049l も、これまでどおり待ちを解くこと。"""
        screen = feed(Screen(3, 8), "01234567" + ALT_IN)
        screen.set_size(3, 4)
        feed(screen, ALT_OUT + "A")
        self.assertEqual(text_of(screen), ["012A4567", "", ""])

    def test_widening_alone_still_wraps(self):
        """広げただけのときの戻し方は、これまでどおりであること。"""
        screen = feed(Screen(3, 4), "0123" + DECSC)
        screen.set_size(3, 8)
        feed(screen, DECRC + "A")
        self.assertEqual(text_of(screen), ["0123", "A", ""])

    def test_narrowing_twice_still_releases(self):
        """二段階で狭めても、戻した待ちが桁を消さないこと。"""
        screen = feed(Screen(3, 8), "01234567" + DECSC)
        screen.set_size(3, 6)
        screen.set_size(3, 4)
        feed(screen, DECRC + "A")
        self.assertEqual(text_of(screen), ["012A4567", "", ""])


class WideningBackThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_document_keeps_every_column_after_widening_back(self):
        """利用者が見る文書でも、広げ直したあとの 1 文字が桁を潰さないこと。"""
        from ui.terminal_widget import TerminalWidget
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        terminal = widget.create_terminal_tab("dev")
        screen = terminal._screen
        cols = screen.cols
        widget.queue_output("dev", ruler(cols) + DECSC)
        widget._flush_pending_output()
        screen.set_size(screen.rows, cols // 2)
        screen.set_size(screen.rows, cols)
        widget.queue_output("dev", DECRC + "!")
        widget._flush_pending_output()
        rows = terminal.toPlainText().split("\n")
        self.assertEqual(rows[0], ruler(cols),
                         "広げ直したあとの 1 文字が受信済みの桁を潰している")
        self.assertEqual(rows[1], "!")


if __name__ == "__main__":
    unittest.main()
