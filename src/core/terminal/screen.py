"""行×桁の画面。パーサの命令を適用して格子を更新する。UI を知らない。

セルは (文字, Attr) のタプル。履歴 (上から押し出された行) は
メイン画面でだけ増える。動きの根拠は ECMA-48 と、標準に無い部分は
XTerm Control Sequences。

制限: 全角文字も 1 セルとして扱う (桁計算は ASCII 前提。機器 CLI の
出力は ASCII で、ここが崩れる実害は Linux 側の全角編集のみ)。
"""
import collections

from core.terminal import parser
from core.terminal.attrs import DEFAULT, apply_sgr

BLANK = (" ", DEFAULT)

# DEC Special Graphics (ESC ( 0 で指示される罫線用文字集合)
DEC_GRAPHICS = dict(zip(
    "`abcdefghijklmnopqrstuvwxyz{|}~",
    "◆▒␉␌␍␊°±␤␋┘┐┌└┼⎺⎻─⎼⎽├┤┴┬│≤≥π≠£·"))


def _param(params, i, default):
    if i < len(params) and params[i] is not None:
        return params[i]
    return default


class Screen(object):
    def __init__(self, rows=24, cols=80, max_history=5000):
        self.rows = rows
        self.cols = cols
        self.history = collections.deque(maxlen=max_history)
        self._new_history = []
        self.title = ""
        self.responses = []             # 機器へ送り返す応答 (DSR/DA)
        self.reset()

    def reset(self):
        """RIS 相当。履歴とタイトルだけは残す (セッションの記録なので)。"""
        self.lines = self._blank_lines()
        self._other = self._blank_lines()   # 裏画面 (代替画面用)
        # wrapped[r] = その行は折り返しで次の行へ続いている。
        # 機器が送った改行との区別が付かないと、窓を戻したときに
        # 繋ぎ直せず、出力が刻まれたまま残る
        self.wrapped = [False] * self.rows
        self._other_wrapped = [False] * self.rows
        self._reflowed = False
        self.alt_active = False
        self.cursor_row = 0
        self.cursor_col = 0
        self.attr = DEFAULT
        self.scroll_top = 0
        self.scroll_bottom = self.rows - 1
        self._pending_wrap = False
        self._saved = (0, 0, DEFAULT)       # ESC 7 / ESC 8
        self._saved_main = None             # ?1049 用
        self.autowrap = True
        self.cursor_visible = True
        self.application_cursor_keys = False    # ?1 DECCKM (入力側が見る)
        self.bracketed_paste = False            # ?2004 (入力側が見る)
        self._g = {"(": "B", ")": "B"}          # G0/G1 の指示文字
        self._charset = "("                     # SI/SO でどちらを使うか
        self.dirty = set(range(self.rows))

    def _blank_lines(self):
        return [self._blank_line() for _ in range(self.rows)]

    def _blank_line(self):
        return [BLANK] * self.cols

    # ---- 外から使う口 ----------------------------------------------

    def apply(self, events):
        """パーサの命令列を画面へ適用する。"""
        for event in events:
            if isinstance(event, parser.Print):
                self._print(event.text)
            elif isinstance(event, parser.Ctrl):
                self._ctrl(event.char)
            elif isinstance(event, parser.Csi):
                self._csi(event)
            elif isinstance(event, parser.Esc):
                self._esc(event)
            elif isinstance(event, parser.Osc):
                self._osc(event.text)
            # Dcs は画面に出るものではないので捨てる

    def text(self):
        """画面の見た目を行のリストで返す (検証と描画の共通口)。"""
        return ["".join(cell[0] for cell in line).rstrip()
                for line in self.lines]

    def take_new_history(self):
        """前回から増えた履歴を (行, 折り返しで続くか) で返して忘れる。"""
        new = self._new_history
        self._new_history = []
        return new

    def take_dirty(self):
        """描き直しが要る行番号を返して忘れる。"""
        dirty = self.dirty
        self.dirty = set()
        return dirty

    def take_reflowed(self):
        """前回の描画のあとに組み直しが起きたかを返して忘れる。

        組み直すと行の切れ目が全部変わるので、描画側が持っている
        「前回描いた内容」との突き合わせが当てにならなくなる。
        """
        reflowed = self._reflowed
        self._reflowed = False
        return reflowed

    def take_responses(self):
        """機器へ送り返すべき応答 (DSR/DA) を返して忘れる。"""
        responses = self.responses
        self.responses = []
        return responses

    def set_size(self, rows, cols):
        """端末の大きさを変える。書かれた行には触らない。

        桁が広がったら足りない分を空白で埋めるだけ。狭くなっても
        切らないし、割り直しもしない。割り直すと窓を往復するたびに
        行の切れ目が変わり、そのたびに中身が削れていった。実端末も
        既に書かれた行は組み直さない。狭いときの見た目は表示側が
        折り返して面倒を見る。

        行数が減ったぶんは、まず下の空行を捨て、足りなければ上の行を
        履歴へ送る (書かれた行を黙って消さないため)。
        """
        if (rows, cols) == (self.rows, self.cols) or rows < 1 or cols < 1:
            return
        for line in self.lines + self._other:
            if len(line) < cols:
                line.extend([BLANK] * (cols - len(line)))

        # メイン画面は記録なので、あふれたら履歴へ送る。代替画面
        # (vi 等) はアプリが描き直すので、切っても構わない
        if self.alt_active:
            main, main_marks = self._other, self._other_wrapped
            alt, alt_marks = self.lines, self.wrapped
            keep_row = (self._saved_main[0] if self._saved_main else 0)
        else:
            main, main_marks = self.lines, self.wrapped
            alt, alt_marks = self._other, self._other_wrapped
            keep_row = self.cursor_row

        while len(main) > rows:
            if keep_row < len(main) - 1 and all(c == BLANK for c in main[-1]):
                main.pop()
                main_marks.pop()
            else:
                self.history.append(main.pop(0))
                self._new_history.append((self.history[-1],
                                          main_marks.pop(0)))
                keep_row = max(0, keep_row - 1)
        while len(main) < rows:
            main.append([BLANK] * cols)
            main_marks.append(False)
        if self.alt_active:
            if self._saved_main:
                self._saved_main = (min(keep_row, rows - 1),
                                    min(self._saved_main[1], cols - 1),
                                    self._saved_main[2])
        else:
            self.cursor_row = keep_row

        del alt[rows:]
        del alt_marks[rows:]
        while len(alt) < rows:
            alt.append([BLANK] * cols)
            alt_marks.append(False)

        self.rows, self.cols = rows, cols
        self.scroll_top, self.scroll_bottom = 0, rows - 1
        self.cursor_row = min(self.cursor_row, rows - 1)
        self.cursor_col = min(self.cursor_col, cols - 1)
        self._pending_wrap = False
        self.dirty = set(range(rows))
        self._reflowed = True

    # ---- 印字と C0 -------------------------------------------------

    def _print(self, text):
        for ch in text:
            if ch == "\x7f":            # DEL は表示しない
                continue
            if self._pending_wrap:      # 右端の 1 文字あとの折り返し
                self.cursor_col = 0
                self._linefeed(from_wrap=True)
            if self._g[self._charset] == "0":
                ch = DEC_GRAPHICS.get(ch, ch)
            line = self.lines[self.cursor_row]
            line[self.cursor_col] = (ch, self.attr)
            self.dirty.add(self.cursor_row)
            if self.cursor_col + 1 < self.cols:
                self.cursor_col += 1
            elif self.autowrap:
                self._pending_wrap = True
            # autowrap 無効なら右端で上書きを続ける

    def _ctrl(self, ch):
        if ch == "\r":
            self.cursor_col = 0
            self._pending_wrap = False
        elif ch in "\n\x0b\x0c":
            self._linefeed()
        elif ch == "\b":
            if self.cursor_col:
                self.cursor_col -= 1
            self._pending_wrap = False
        elif ch == "\t":
            self.cursor_col = min(self.cols - 1,
                                  (self.cursor_col // 8 + 1) * 8)
            self._pending_wrap = False
        elif ch == "\x0e":              # SO: G1 へ
            self._charset = ")"
        elif ch == "\x0f":              # SI: G0 へ
            self._charset = "("
        # BEL・NUL などは何もしない

    def _linefeed(self, from_wrap=False):
        self._pending_wrap = False
        # 折り返しで送られたのか、機器が改行を送ったのかを覚える
        self.wrapped[self.cursor_row] = from_wrap
        if self.cursor_row == self.scroll_bottom:
            self._scroll_up(1)
        elif self.cursor_row + 1 < self.rows:
            self.cursor_row += 1

    def _scroll_up(self, n):
        for _ in range(n):
            removed = self.lines.pop(self.scroll_top)
            removed_wrap = self.wrapped.pop(self.scroll_top)
            self.lines.insert(self.scroll_bottom, self._blank_line())
            self.wrapped.insert(self.scroll_bottom, False)
            if (not self.alt_active and self.scroll_top == 0
                    and self.scroll_bottom == self.rows - 1):
                self.history.append(removed)
                self._new_history.append((removed, removed_wrap))
        self.dirty.update(range(self.scroll_top, self.scroll_bottom + 1))

    def _scroll_down(self, n):
        for _ in range(n):
            self.lines.pop(self.scroll_bottom)
            self.wrapped.pop(self.scroll_bottom)
            self.lines.insert(self.scroll_top, self._blank_line())
            self.wrapped.insert(self.scroll_top, False)
        self.dirty.update(range(self.scroll_top, self.scroll_bottom + 1))

    # ---- CSI -------------------------------------------------------

    def _move(self, row, col):
        self.cursor_row = max(0, min(self.rows - 1, row))
        self.cursor_col = max(0, min(self.cols - 1, col))
        self._pending_wrap = False
        self.dirty.add(self.cursor_row)

    def _csi(self, seq):
        if seq.private == "?" and seq.final in "hl":
            return self._private_mode(seq)
        if seq.private or seq.intermediate:
            return                      # DECSCUSR 等、表示に関わらない
        p = seq.params
        f = seq.final
        n = max(1, _param(p, 0, 1))
        # 繰り返す命令の回数は、画面より大きくても意味を持たない。
        # 頭打ちにしないと ESC[999999999S だけで画面処理が何十分も
        # 回り、その間 GUI が固まる (パラメータは 256 桁まで書ける)。
        # カーソル移動は _move が行桁で丸めるので、ここでは触らない
        rows_n = min(n, self.rows)
        cols_n = min(n, self.cols)
        if f == "A":
            limit = (self.scroll_top
                     if self.cursor_row >= self.scroll_top else 0)
            self._move(max(limit, self.cursor_row - n), self.cursor_col)
        elif f in "Be":
            limit = (self.scroll_bottom
                     if self.cursor_row <= self.scroll_bottom
                     else self.rows - 1)
            self._move(min(limit, self.cursor_row + n), self.cursor_col)
        elif f in "Ca":
            self._move(self.cursor_row, self.cursor_col + n)
        elif f == "D":
            self._move(self.cursor_row, self.cursor_col - n)
        elif f == "E":
            self._move(self.cursor_row + n, 0)
        elif f == "F":
            self._move(self.cursor_row - n, 0)
        elif f in "G`":
            self._move(self.cursor_row, _param(p, 0, 1) - 1)
        elif f == "d":
            self._move(_param(p, 0, 1) - 1, self.cursor_col)
        elif f in "Hf":
            self._move(_param(p, 0, 1) - 1, _param(p, 1, 1) - 1)
        elif f == "J":
            self._erase_display(_param(p, 0, 0))
        elif f == "K":
            self._erase_line(_param(p, 0, 0))
        elif f == "L":
            self._shift_lines(rows_n, insert=True)
        elif f == "M":
            self._shift_lines(rows_n, insert=False)
        elif f == "@":
            self._shift_chars(cols_n, insert=True)
        elif f == "P":
            self._shift_chars(cols_n, insert=False)
        elif f == "X":
            self._erase_chars(cols_n)
        elif f == "S":
            self._scroll_up(rows_n)
        elif f == "T":
            self._scroll_down(rows_n)
        elif f == "r":
            self._set_margins(p)
        elif f == "m":
            self.attr = apply_sgr(self.attr, p)
        elif f == "n":
            if _param(p, 0, 0) == 6:    # DSR: カーソル位置を答える
                self.responses.append("\x1b[%d;%dR" % (
                    self.cursor_row + 1, self.cursor_col + 1))
            elif _param(p, 0, 0) == 5:
                self.responses.append("\x1b[0n")
        elif f == "c":                  # DA: VT100 with AVO と名乗る
            self.responses.append("\x1b[?1;2c")
        # 知らない最終文字は黙って捨てる (画面を壊さないことが仕事)

    def _esc(self, seq):
        if seq.intermediate in ("(", ")"):      # 文字集合の指示
            self._g[seq.intermediate] = seq.final
        elif seq.final == "7":
            self._saved = (self.cursor_row, self.cursor_col, self.attr)
        elif seq.final == "8":
            row, col, attr = self._saved
            self.attr = attr
            self._move(row, col)
        elif seq.final == "D":          # IND
            self._linefeed()
        elif seq.final == "M":          # RI: 上端では下へスクロール
            if self.cursor_row == self.scroll_top:
                self._scroll_down(1)
            elif self.cursor_row:
                self.cursor_row -= 1
            self._pending_wrap = False
        elif seq.final == "E":          # NEL
            self.cursor_col = 0
            self._linefeed()
        elif seq.final == "c":          # RIS。履歴は reset が残す
            self.reset()
        # = > \ H などは表示を変えない

    def _osc(self, text):
        num, _, rest = text.partition(";")
        if num in ("0", "2"):
            self.title = rest

    def _private_mode(self, seq):
        on = seq.final == "h"
        for mode in seq.params:
            if mode in (1049, 1047, 47):
                self._switch_screen(on, with_cursor=(mode == 1049))
            elif mode == 7:
                self.autowrap = on
                if not on:
                    # 右端で保留していた折り返しも一緒に捨てる。
                    # 残すと、折り返しを切った直後の 1 文字だけが
                    # 次の行へ流れる
                    self._pending_wrap = False
            elif mode == 25:
                self.cursor_visible = on
            elif mode == 1:
                self.application_cursor_keys = on
            elif mode == 2004:
                self.bracketed_paste = on
            # ほかの私用モードは表示に効かないので無視

    def _switch_screen(self, to_alt, with_cursor):
        if to_alt == self.alt_active:
            return
        if to_alt and with_cursor:
            self._saved_main = (self.cursor_row, self.cursor_col, self.attr)
        self.lines, self._other = self._other, self.lines
        # 折り返しの印も画面と一緒に入れ替える。裏へ回ったメイン画面の
        # 印を失うと、戻ってきたときに組み直しで繋ぎ直せなくなる
        self.wrapped, self._other_wrapped = (
            self._other_wrapped, self.wrapped)
        self.alt_active = to_alt
        if to_alt:                      # 代替画面は白紙で始まる
            for r in range(self.rows):
                self.lines[r] = self._blank_line()
                self.wrapped[r] = False
            self._move(0, 0)
        elif with_cursor and self._saved_main:
            row, col, attr = self._saved_main
            self.attr = attr
            self._move(row, col)
        self.dirty.update(range(self.rows))
        self._pending_wrap = False

    def _set_margins(self, p):
        top = _param(p, 0, 1) - 1
        bottom = _param(p, 1, self.rows) - 1
        if 0 <= top < bottom <= self.rows - 1:
            self.scroll_top = top
            self.scroll_bottom = bottom
            self._move(0, 0)            # DECSTBM はカーソルも戻す

    def _erase_display(self, mode):
        if mode == 3:
            # ED 3 は履歴 (スクロールバック) を消す命令で、可視画面には
            # 触らない。NetBelt はセッションの記録を消さない方針なので
            # 何もしない。画面まで消すと clear -x で表示が飛ぶ
            return
        # 画面全体が消えるとき (clear は ESC[H ESC[J、つまり home からの
        # mode 0 で来る) は、消す前に見えていた中身を履歴へ送る。
        # clear でセッションの記録を失わない、という v1.1.1 の方針
        wipes_all = (mode >= 2 or
                     (mode == 0 and (self.cursor_row, self.cursor_col)
                      == (0, 0)))
        if wipes_all and not self.alt_active:
            last = -1
            for r in range(self.rows):
                if any(c != BLANK for c in self.lines[r]):
                    last = r
            for r, line in enumerate(self.lines[:last + 1]):
                self.history.append(line)
                self._new_history.append((line, self.wrapped[r]))
        if wipes_all:
            rng = range(0, self.rows)
        elif mode == 0:
            self._erase_line(0)
            rng = range(self.cursor_row + 1, self.rows)
        else:
            self._erase_line(1)
            rng = range(0, self.cursor_row)
        for r in rng:
            self.lines[r] = self._blank_line()
            self.wrapped[r] = False
        self.dirty.update(rng)
        self._pending_wrap = False

    def _erase_line(self, mode):
        line = self.lines[self.cursor_row]
        # 行は桁より長いことがある (窓を縮めても切らないため)。
        # 消すときは行の実際の長さで見る
        if mode == 0:
            rng = range(self.cursor_col, len(line))
        elif mode == 1:
            rng = range(0, self.cursor_col + 1)
        else:
            rng = range(0, len(line))
        for c in rng:
            line[c] = BLANK
        if mode != 1:               # 行末まで消したら続きは無い
            self.wrapped[self.cursor_row] = False
        self.dirty.add(self.cursor_row)
        self._pending_wrap = False

    def _shift_lines(self, n, insert):
        """IL / DL。スクロール範囲の中でだけ効く。"""
        if not self.scroll_top <= self.cursor_row <= self.scroll_bottom:
            return
        for _ in range(n):
            if insert:
                self.lines.pop(self.scroll_bottom)
                self.wrapped.pop(self.scroll_bottom)
                self.lines.insert(self.cursor_row, self._blank_line())
                self.wrapped.insert(self.cursor_row, False)
            else:
                self.lines.pop(self.cursor_row)
                self.wrapped.pop(self.cursor_row)
                self.lines.insert(self.scroll_bottom, self._blank_line())
                self.wrapped.insert(self.scroll_bottom, False)
        self.dirty.update(range(self.cursor_row, self.scroll_bottom + 1))
        self._pending_wrap = False

    def _shift_chars(self, n, insert):
        """ICH / DCH。行の右端は詰まる・押し出される。"""
        line = self.lines[self.cursor_row]
        for _ in range(n):
            if insert:
                line.pop()
                line.insert(self.cursor_col, BLANK)
            else:
                line.pop(self.cursor_col)
                line.append(BLANK)
        self.dirty.add(self.cursor_row)
        self._pending_wrap = False

    def _erase_chars(self, n):
        line = self.lines[self.cursor_row]
        for c in range(self.cursor_col,
                       min(len(line), self.cursor_col + n)):
            line[c] = BLANK
        self.dirty.add(self.cursor_row)
        self._pending_wrap = False
