r"""1049 の保存を一度使ったら捨てること、捨てずに戻す枝が折り返し待ちも戻すことを検証する。

3a0d7b5 で、47l / 1047l で先にメイン画面へ戻ったあとの 1049l が 1049h の
保存 (_saved_main) から戻り、使った保存を捨てる枝を足した。ところが代替
画面から出る通常の 1049l は、保存から戻したあとも捨てていなかった。その
ため 1049 を一度往復したあとに来る 1049l は、その枝へ入って古い保存へ
飛ぶ。1049h を受けていなければ何もしない経路なので、3a0d7b5 より前には
起きなかった (退行)。またその枝は位置・属性・文字集合・「最終桁へ印字
した」覚えだけを戻し、右端の折り返し待ちを戻していなかった。

実測 (基準 097550c):
  (1) シェルスクリプトの定番 trap 'tput rmcup' EXIT の形。Screen(5, 20)
        '$ ./menu.sh\r\n' ESC[?1049h 'MENU' ESC[?1049l 'done\r\n'
        ESC[?1049l '$ '
          HEAD : ['$ ./menu.sh', '$ ne', ...]   (プロンプトが 'done' を潰す)
          正   : ['$ ./menu.sh', 'done', '$', ...]
      往復のあと出力でスクロールしてから来た rmcup も、昔の行へ戻る。
  (2) 右端ちょうどまで書いて折り返し待ちが立った位置で 1049h した場合。
      Screen(3, 10)、'MAINMAINMA' のあと
        ESC[?1049h ESC[?47l ESC[?1049l 'X'
          HEAD : ['MAINMAINMX', '', '']   (受信済みの 'A' が黙って消える)
          正   : ['MAINMAINMA', 'X', '']  (47l を挟まない素の往復と同じ)
      ESC[?1049h ESC[?1049l ESC[?1049l 'X' も同じ形になっていた (3a0d7b5
      より前は正しかった)。結合文字は 1 つ左の 'M' に付いていた。

利用者の決定 (2026-09-23): 保存があるときだけ戻す。使った保存は捨てる
(同じ保存を使い回さない)。1049h を一度往復したあとの 2 回目の 1049l は、
迷子の 1049l と同じく何もしない。

直し方: 代替画面を出る通常の 1049l でも、戻したあとに _saved_main を
捨てる。早期 return の枝では保存の 8 要素をすべて戻し、折り返し待ちは
通常の経路と同じく「保存したときより桁が狭いときだけ解く」規則で、
_move のあとに代入する。
"""
import sys
import unittest

sys.path.insert(0, "src")

from core.terminal.parser import Parser     # noqa: E402
from core.terminal.screen import Screen     # noqa: E402

ESC = chr(0x1B)
CSI = ESC + "["
SMCUP = CSI + "?1049h"
RMCUP = CSI + "?1049l"
LEAVE_47 = CSI + "?47l"
AC = chr(0x0301)
FULL_ROW = "MAINMAINMA"          # Screen(3, 10) の右端ちょうどまで


def feed(screen, text):
    screen.apply(Parser().feed(text))
    return screen


class UsedSaveIsDroppedTest(unittest.TestCase):
    def test_leaving_by_1049l_drops_the_save(self):
        """代替画面を 1049l で出たら、使った保存を捨てること。"""
        s = feed(Screen(3, 10), "MAIN" + SMCUP + CSI + "3;2H" + RMCUP)
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 4))
        self.assertIsNone(s._saved_main)

    def test_trap_rmcup_does_not_overwrite_the_last_line(self):
        """trap の 2 回目の rmcup が、直前の出力行へプロンプトを重ねないこと。"""
        s = feed(Screen(5, 20), "$ ./menu.sh\r\n" + SMCUP + "MENU" + RMCUP
                 + "done\r\n" + RMCUP + "$ ")
        self.assertEqual(s.text()[:3], ["$ ./menu.sh", "done", "$"])
        self.assertEqual((s.cursor_row, s.cursor_col), (2, 2))

    def test_message_then_rmcup_keeps_the_message(self):
        """終了メッセージのあとの rmcup が、メッセージを潰さないこと。"""
        s = feed(Screen(5, 20), "$ app\r\n" + SMCUP + "UI" + RMCUP
                 + "Saved 3 files.\r\n" + RMCUP + "$ ")
        self.assertEqual(s.text()[:3], ["$ app", "Saved 3 files.", "$"])

    def test_rmcup_after_scrolling_stays_on_the_current_line(self):
        """往復のあと出力でスクロールしてから来た rmcup も、動かないこと。"""
        s = feed(Screen(3, 20), "$ less f\r\n" + SMCUP + "PAGE" + RMCUP
                 + "$ ls\r\na b c\r\n$ tput rmcup\r\n" + RMCUP + "$ ")
        self.assertEqual(s.text(), ["a b c", "$ tput rmcup", "$"])
        self.assertEqual((s.cursor_row, s.cursor_col), (2, 2))


class EarlyReturnRestoresPendingWrapTest(unittest.TestCase):
    """47l で先に戻ったあとの 1049l も、素の往復と同じ位置へ戻すこと。"""

    def test_plain_round_trip_keeps_the_pending_wrap(self):
        """対照: 47l を挟まない素の往復は、もとから右端の文字を残す。"""
        s = feed(Screen(3, 10), FULL_ROW + SMCUP + RMCUP + "X")
        self.assertEqual(s.text(), ["MAINMAINMA", "X", ""])

    def test_47l_then_1049l_keeps_the_last_column(self):
        """右端の 'A' を続く 'X' が上書きしないこと。"""
        s = feed(Screen(3, 10), FULL_ROW + SMCUP + LEAVE_47 + RMCUP + "X")
        self.assertEqual(s.text(), ["MAINMAINMA", "X", ""])
        self.assertIsNone(s._saved_main)

    def test_47l_then_1049l_attaches_combining_to_the_last_char(self):
        """結合文字が 1 つ左の文字へずれないこと。"""
        s = feed(Screen(3, 10), FULL_ROW + SMCUP + LEAVE_47 + RMCUP + AC)
        cells = [cell[0] for cell in s.lines[0]]
        self.assertEqual(cells[8:], ["M", "A" + AC])

    def test_second_1049l_after_round_trip_keeps_the_last_column(self):
        """往復のあとの 2 回目の 1049l も、右端の文字を潰さないこと。"""
        s = feed(Screen(3, 10), FULL_ROW + SMCUP + RMCUP + RMCUP + "X")
        self.assertEqual(s.text(), ["MAINMAINMA", "X", ""])

    def test_narrowed_window_still_releases_the_saved_wrap(self):
        """対照: 保存したときより狭めたら、これまでどおり待ちを解くこと。"""
        s = feed(Screen(3, 10), FULL_ROW + SMCUP + LEAVE_47)
        s.set_size(3, 8)
        feed(s, RMCUP + "X")
        self.assertEqual((s.cursor_row, s.cursor_col), (0, 7))
        self.assertEqual(s.text()[1:], ["", ""])


if __name__ == "__main__":
    unittest.main()
