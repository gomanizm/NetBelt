r"""保存した折り返し待ちを解くかどうかを、保存した時点の桁で決める件。

「窓を狭めたら保存領域の折り返し待ちも解く」は利用者の決定
(2026-09-20)。狭めたまま復元すると、待ちを持ち越した次の 1 文字が
_linefeed(from_wrap=True) の切り詰めを呼び、新しい桁より右にある
受信済みの文字をまとめて消してしまうため。

その後、狭めて広げ直したときにまで解いてしまう件を直すために、解く
条件を「戻し先の行がいまの桁より長い」= 窓を狭めた跡がある、に変えた。
ところが行が桁より長いのは「保存より前に狭めた」ときだけではない。

実測 (この修正の前。いずれも保存・復元をしない対照 = 生きたカーソルと
比べて、受信済みの 1 文字が消える):

  (1) 狭めた『あと』に、いまの桁の右端まで受信して立った待ち
      Screen(3, 8) へ '01234567' -> set_size(3, 4)
      -> ESC[1;4H 'Q' + ESC 7 -> ESC 8 + 'S'
        修正前 : ['012S4567', '', '']   (受信した 'Q' が 'S' に潰れる)
        対照   : ['012Q', 'S', '']
      待ちは狭めた『あと』に立ったので、いまの桁で正しい。なのに
      「行が桁より長い」は狭めた跡が残っているだけで成り立つ。

  (2) 保存より前のどこかで広げて狭め直した (最大化 -> 戻す等)
      Screen(3, 8) へ '01234567' -> set_size(3, 12) -> set_size(3, 8)
      -> ESC[1;1H 'wxyzwxyz' + ESC 7 -> ESC 8 + 'AB'
        修正前 : ['wxyzwxyA', 'B', '']  (受信した 'z' が 'A' に潰れる)
        対照   : ['wxyzwxyz', 'AB', '']

  DECSC / ?1048 / ?1049 / 47 + DECSC のどれでも同じ。利用者が見る文書
  (TerminalWidget) でも、桁を倍にして戻したあと右端まで受信してから
  復元すると、最終桁が次の 1 文字に潰れる。
  「狭めた跡」は行が流れるまで全行に残る (TUI の全行書き直しでも EL 2
  でも消えない) ので、一度窓を狭めた端末では出続ける。

直し方: 行の長さは「窓を狭めた跡があるか」しか言わないので、保存領域
そのものに保存した時点の桁を持たせ、「いまの桁が保存した時点より狭い」
ときだけ解く。折り返し待ちは必ず保存時の右端 (桁 - 1) で立つので、
これは「その待ちが指していた右端が、いまの桁の外か」と同じ意味になる。
狭めたままのときの振る舞い (2026-09-20 の決定) は変わらない。
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
SCREEN_47_IN, SCREEN_47_OUT = ESC + "[?47h", ESC + "[?47l"
HOME = ESC + "[1;1H"


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


def text_of(screen):
    return ["".join(cell[0] for cell in line).rstrip()
            for line in screen.lines]


def ruler(n):
    return "".join(str(i % 10) for i in range(n))


def narrowed_then_printed():
    """8 桁で埋めてから 4 桁へ狭め、そのあと 4 桁の右端まで印字する。"""
    screen = feed(Screen(3, 8), "01234567")
    screen.set_size(3, 4)
    return feed(screen, ESC + "[1;4H" + "Q")


def widened_then_narrowed_back_then_printed():
    """8 桁を 12 桁へ広げて 8 桁へ戻し、そのあと右端まで印字する。"""
    screen = feed(Screen(3, 8), "01234567")
    screen.set_size(3, 12)
    screen.set_size(3, 8)
    return feed(screen, HOME + "wxyzwxyz")


class AWrapMadeAfterTheNarrowingIsKeptTest(unittest.TestCase):
    """狭めた『あと』に立った待ちは、いまの桁で正しいので解かないこと。"""

    def test_the_live_cursor_shows_what_the_restore_should_match(self):
        """対照: 保存・復元をしなければ 'Q' は残り 'S' は次の行へ行く。"""
        screen = feed(narrowed_then_printed(), "S")
        self.assertEqual(text_of(screen), ["012Q", "S", ""])

    def test_a_wrap_made_after_the_narrowing_is_not_released(self):
        screen = feed(narrowed_then_printed(), DECSC + DECRC + "S")
        self.assertEqual(text_of(screen), ["012Q", "S", ""],
                         "復元の次の 1 文字が受信済みの桁を潰している")

    def test_a_wrap_made_after_the_narrowing_survives_1048(self):
        screen = feed(narrowed_then_printed(),
                      SAVE_1048 + RESTORE_1048 + "S")
        self.assertEqual(text_of(screen), ["012Q", "S", ""])

    def test_a_wrap_made_after_the_narrowing_survives_1049(self):
        screen = feed(narrowed_then_printed(), ALT_IN + ALT_OUT + "S")
        self.assertEqual(text_of(screen), ["012Q", "S", ""])

    def test_a_wrap_made_after_the_narrowing_survives_47(self):
        screen = feed(narrowed_then_printed(),
                      DECSC + SCREEN_47_IN + SCREEN_47_OUT + DECRC + "S")
        self.assertEqual(text_of(screen), ["012Q", "S", ""])


class AWrapMadeAfterWideningBackIsKeptTest(unittest.TestCase):
    """広げて狭め直した跡の行でも、いまの桁で立った待ちは解かないこと。"""

    def test_the_live_cursor_shows_what_the_restore_should_match(self):
        screen = feed(widened_then_narrowed_back_then_printed(), "AB")
        self.assertEqual(text_of(screen), ["wxyzwxyz", "AB", ""])

    def test_a_wrap_after_widening_back_is_not_released(self):
        screen = feed(widened_then_narrowed_back_then_printed(),
                      DECSC + DECRC + "AB")
        self.assertEqual(text_of(screen), ["wxyzwxyz", "AB", ""],
                         "復元の次の 1 文字が受信済みの桁を潰している")

    def test_a_wrap_after_widening_back_survives_1048(self):
        screen = feed(widened_then_narrowed_back_then_printed(),
                      SAVE_1048 + RESTORE_1048 + "AB")
        self.assertEqual(text_of(screen), ["wxyzwxyz", "AB", ""])

    def test_a_wrap_after_widening_back_survives_1049(self):
        screen = feed(widened_then_narrowed_back_then_printed(),
                      ALT_IN + ALT_OUT + "AB")
        self.assertEqual(text_of(screen), ["wxyzwxyz", "AB", ""])


class NarrowingBeforeTheSaveStillReleasesTest(unittest.TestCase):
    """2026-09-20 の決定 (保存より後に狭めたら解く) が残っている対照。"""

    def test_a_wrap_saved_before_the_narrowing_is_still_released(self):
        screen = feed(Screen(3, 8), "01234567" + DECSC)
        screen.set_size(3, 4)
        feed(screen, DECRC + "A")
        self.assertEqual(text_of(screen), ["012A4567", "", ""])

    def test_a_1049_save_before_the_narrowing_is_still_released(self):
        screen = feed(Screen(3, 8), "01234567" + ALT_IN)
        screen.set_size(3, 4)
        feed(screen, ALT_OUT + "A")
        self.assertEqual(text_of(screen), ["012A4567", "", ""])


class WideningBackThroughWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget_rows(self, prefix):
        from ui.terminal_widget import TerminalWidget
        widget = TerminalWidget()
        self.addCleanup(widget.close)
        terminal = widget.create_terminal_tab("dev")
        screen = terminal._screen
        cols = screen.cols
        # 窓を一度広げてから戻す (最大化 -> 戻す、文字を小さくして戻す)。
        # 行は広げた桁のまま残るので、全行に「跡」が付く
        screen.set_size(screen.rows, cols * 2)
        screen.set_size(screen.rows, cols)
        widget.queue_output("dev", HOME + ruler(cols) + prefix + "AB")
        widget._flush_pending_output()
        return terminal.toPlainText().split("\n"), cols

    def test_the_document_keeps_the_last_column_after_widening_back(self):
        rows, cols = self._widget_rows(DECSC + DECRC)
        self.assertEqual(rows[0], ruler(cols),
                         "復元の次の 1 文字が受信済みの桁を潰している")
        self.assertEqual(rows[1], "AB")

    def test_the_document_keeps_the_last_column_through_1049(self):
        rows, cols = self._widget_rows(ALT_IN + ALT_OUT)
        self.assertEqual(rows[0], ruler(cols))
        self.assertEqual(rows[1], "AB")


if __name__ == "__main__":
    unittest.main()
