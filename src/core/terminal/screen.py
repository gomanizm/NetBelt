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
        self.reset()

    def reset(self):
        """RIS 相当。履歴とタイトルだけは残す (セッションの記録なので)。"""
        self.lines = self._blank_lines()
        self.cursor_row = 0
        self.cursor_col = 0
        self.attr = DEFAULT
        self.scroll_top = 0
        self.scroll_bottom = self.rows - 1
        self._pending_wrap = False
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
            # Esc / Osc / Dcs は後段のコミットで

    def text(self):
        """画面の見た目を行のリストで返す (検証と描画の共通口)。"""
        return ["".join(cell[0] for cell in line).rstrip()
                for line in self.lines]

    def take_new_history(self):
        """前回から増えた履歴行を返して忘れる。"""
        new = self._new_history
        self._new_history = []
        return new

    def take_dirty(self):
        """描き直しが要る行番号を返して忘れる。"""
        dirty = self.dirty
        self.dirty = set()
        return dirty

    # ---- 印字と C0 -------------------------------------------------

    def _print(self, text):
        for ch in text:
            if ch == "\x7f":            # DEL は表示しない
                continue
            if self._pending_wrap:      # 右端の 1 文字あとの折り返し
                self.cursor_col = 0
                self._linefeed()
            line = self.lines[self.cursor_row]
            line[self.cursor_col] = (ch, self.attr)
            self.dirty.add(self.cursor_row)
            if self.cursor_col + 1 < self.cols:
                self.cursor_col += 1
            else:
                self._pending_wrap = True

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
        # BEL・NUL・SI/SO などは (今は) 何もしない

    def _linefeed(self):
        self._pending_wrap = False
        if self.cursor_row == self.scroll_bottom:
            self._scroll_up(1)
        elif self.cursor_row + 1 < self.rows:
            self.cursor_row += 1

    def _scroll_up(self, n):
        for _ in range(n):
            removed = self.lines.pop(self.scroll_top)
            self.lines.insert(self.scroll_bottom, self._blank_line())
            if self.scroll_top == 0 and self.scroll_bottom == self.rows - 1:
                self.history.append(removed)
                self._new_history.append(removed)
        self.dirty.update(range(self.scroll_top, self.scroll_bottom + 1))

    def _scroll_down(self, n):
        for _ in range(n):
            self.lines.pop(self.scroll_bottom)
            self.lines.insert(self.scroll_top, self._blank_line())
        self.dirty.update(range(self.scroll_top, self.scroll_bottom + 1))

    # ---- CSI -------------------------------------------------------

    def _move(self, row, col):
        self.cursor_row = max(0, min(self.rows - 1, row))
        self.cursor_col = max(0, min(self.cols - 1, col))
        self._pending_wrap = False
        self.dirty.add(self.cursor_row)

    def _csi(self, seq):
        if seq.private or seq.intermediate:
            return                      # 私用モードは後段のコミットで
        p = seq.params
        f = seq.final
        n = max(1, _param(p, 0, 1))
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
        elif f == "m":
            self.attr = apply_sgr(self.attr, p)
        # 知らない最終文字は黙って捨てる (画面を壊さないことが仕事)
