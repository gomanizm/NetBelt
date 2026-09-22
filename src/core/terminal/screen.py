"""行×桁の画面。パーサの命令を適用して格子を更新する。UI を知らない。

セルは (文字, Attr) のタプル。履歴 (上から押し出された行) は
メイン画面でだけ増える。動きの根拠は ECMA-48 と、標準に無い部分は
XTerm Control Sequences。

セルの幅は xterm と同じく unicodedata で決める: 東アジア幅 W/F は
2 セル (2 セル目は文字 "" の継続セル)、結合文字と書式文字 (ZWJ 等) は
幅 0 で直前のセルの文字列へ繋げる。残る制限: 曖昧幅 (A) は 1 セル、
絵文字の ZWJ 列や肌色修飾は 1 つの書記素にまとめず幅を足す、幅 0 の
文字が行頭に来たときは前の行へ繋げず捨てる。

折り返しの印 (wrapped[r]) は行単位の近似で、論理行そのものは追って
いない。消去・削除 (EL・ECH・DCH・ICH) は、残った行が丸ごと空白に
なったときだけ印を外す。中身が残るかぎり次の行はまだ続きなので、
行末まで届く消去でも印は保つ。印字での書き直しは、右端まで届いた
ときだけ印を保ち、届かなければ外す。そのため、折り返した行の一部だけを
(右端まで届かない形で) 書き直すと印が外れ、履歴・コピー・ログでは次の
行との間に改行が入る。印字が複数回に分かれて届いた場合も、右端までの
書き直しが途中で切れると印は外れる。続きの行そのものが無くなる命令
(ED 0・IL・DL・スクロール) では、その場で印を外す。

DECOM (ESC[?6h) は保持しない。有効なら CUP・VPA の行番号は
スクロール範囲の上端から数えるべきだが、ここでは常に画面の
原点から数える。そのため DECSTBM で範囲を狭めたまま ESC[?6h を
送る機器では、書き込まれる行が上端の分だけ上へずれる。terminfo に
対応する capability が無く ncurses 系のアプリは送らないため、対応する
とカーソル移動・DSR 応答・DECSTBM をまとめて触る割には見合わない。
"""
import collections
import itertools
import re
import unicodedata

from core.terminal import parser
from core.terminal.attrs import DEFAULT, apply_sgr

BLANK = (" ", DEFAULT)

# 1 セルへ繋げる幅 0 の文字の数の上限 (基底の文字を含む長さ)。書記素
# クラスタとして現実的な長さを超えると表示の意味が無く、化けた出力を
# UTF-8 として読んだときに可視行の 1 セルが伸び続ける
MAX_CELL_TEXT = 8

# DEC Special Graphics (ESC ( 0 で指示される罫線用文字集合)
DEC_GRAPHICS = dict(zip(
    "`abcdefghijklmnopqrstuvwxyz{|}~",
    "◆▒␉␌␍␊°±␤␋┘┐┌└┼⎺⎻─⎼⎽├┤┴┬│≤≥π≠£·"))

# 印字を、_cell_width が必ず 1 を返す DEL 以外の文字 (U+00AD より前) の
# 連なり (1 つ目の組) と、それ以外の文字の連なり (2 つ目の組) に分ける。
# 前者は _print_narrow でまとめて書き込める
_PRINT_RUNS = re.compile(r"([\x00-\x7e\x80-\xac]+)|([^\x00-\x7e\x80-\xac]+)")


def _param(params, i, default):
    if i < len(params) and params[i] is not None:
        return params[i]
    return default


def _cell_width(ch):
    """文字が占めるセル数 (0 / 1 / 2)。

    結合文字 (Mn/Me) と書式文字 (Cf: ZWJ・ZWNJ 等) は 0、東アジア幅
    W/F (漢字・かな・絵文字) は 2、それ以外は 1。xterm と同じ数え方。
    """
    if ch < "\u00ad":
        # U+00AD (SOFT HYPHEN, Cf) より前は例外なく 1 セル。機器の
        # 出力はほぼ全部ここで返る (unicodedata を 2 回引かない)
        return 1
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _is_blank(cells):
    """cells が全部 BLANK なら True。

    list.count は C 側で回るので、同じ判定を Python の生成式で書くより
    ひと桁速い。空の画面への消去の費用はこの走査で決まる
    """
    return cells.count(BLANK) == len(cells)


def _split_wide(line, i):
    """i 番目が全角の継続セルなら、その全角 (i-1 と i) を空白にする。

    全角の片側だけを書き換える・消すとき、残る半分を消す (xterm と同じ)。
    """
    if 0 < i < len(line) and line[i][0] == "":
        line[i - 1] = BLANK
        line[i] = BLANK


class Screen(object):
    # 描画側へ渡す履歴の差分 (_new_history) に残す論理行の数。
    # 描画側は文書を MAX_DOCUMENT_BLOCKS 行で頭から切り詰めるので、
    # それより古い行は書いた先から捨てられるだけ。上限をそれ以上に
    # 取り、捨てるのを古い方に限ってあるので、描き終えた文書は上限が
    # 無かったときと同じになる (下げると文書から行が消える)。
    # 数えるのは「行」ではなく「文書の 1 行を終える行 = 折り返しで
    # 続かない行」。折り返しで続く行は次の行と繋いで 1 行として書かれる
    # ので、行で数えると折り返しが混ざったぶん文書が上限に届かない
    MAX_NEW_HISTORY = 20000

    def __init__(self, rows=24, cols=80, max_history=5000):
        self.rows = rows
        self.cols = cols
        self.history = collections.deque(maxlen=max_history)
        self._new_history = collections.deque()
        self._new_history_lines = 0     # 差分に入っている論理行の数
        self._history_dropped = False
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
        # ESC 7 / ESC 8。位置・属性に加えて、VT100 と同じく
        # 文字集合の指示 (G0/G1) と SI/SO の状態、それに xterm と
        # 同じく右端の折り返し待ち (_pending_wrap) も持つ。最後の桁数は
        # 標準に無い持ち物で、戻すときに折り返し待ちを解くかどうかを
        # 決めるためだけに使う (_restore_cursor)
        self._saved = (0, 0, DEFAULT, {"(": "B", ")": "B"}, "(", False,
                       self.cols)
        # 保存領域は画面ごと (xterm の screen->sc[])。裏へ回った画面の
        # ぶんはここへ退避する
        self._other_saved = self._saved
        self._saved_main = None             # ?1049 用
        self.autowrap = True
        self.insert_mode = False                # IRM (ESC[4h / ESC[4l)
        self.cursor_visible = True
        self.application_cursor_keys = False    # ?1 DECCKM (入力側が見る)
        self.bracketed_paste = False            # ?2004 (入力側が見る)
        self._g = {"(": "B", ")": "B"}          # G0/G1 の指示文字
        self._charset = "("                     # SI/SO でどちらを使うか
        # self.lines が丸ごと空白で、折り返しの印も無いと分かっている。
        # 立てるのは白紙にした直後 (ここと ED の全消去) だけ。中身を
        # 書き込める経路 (印字) と、self.lines そのものが入れ替わる・
        # 行の長さが変わる経路 (代替画面の出入り・set_size) で落とす。
        # スクロールや消去は空白の行を動かすだけなので落とさない
        self._screen_blank = True
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

    def _record_new_history(self, line, wrapped):
        """描画側へ渡す履歴の差分へ 1 行足す。あふれたら古い方から捨てる。

        ESC[nS (SU) と ESC[nM (DL) が 1 命令で動かす行数は範囲の高さで
        頭打ちになっているが、命令の繰り返し回数には上限が無い。1 回の
        描画単位 16384 文字を 200x500 の画面へ流すと 546,000 行が作られ、
        2.2GB を確保して GUI が 20.6 秒止まった (実測)。文書はそのうち
        MAX_DOCUMENT_BLOCKS 行しか残さないので、大半は捨てられるためだけ
        に確保され書き込まれていた。self.history は deque(maxlen) で既に
        抑えられている。

        捨てるのは論理行の頭まで。折り返しの途中から残すと、切れ端が
        1 行として文書へ入る。新しく折り返しの印が付くには右端まで印字
        する必要があるので、印付きの行は 1 回の描画単位ぶんの入力で
        抑えられており、上限に数えなくても青天井にはならない。
        """
        self._new_history.append((line, wrapped))
        if not wrapped:
            self._new_history_lines += 1
        while self._new_history_lines > self.MAX_NEW_HISTORY:
            self._history_dropped = True
            if not self._new_history.popleft()[1]:
                self._new_history_lines -= 1

    def take_new_history(self):
        """前回から増えた履歴を (行, 折り返しで続くか) で返して忘れる。"""
        new = list(self._new_history)
        self._new_history.clear()
        self._new_history_lines = 0
        return new

    def take_history_dropped(self):
        """前回から履歴の差分を上限で捨てたかを返して忘れる。"""
        dropped = self._history_dropped
        self._history_dropped = False
        return dropped

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
        # 空でも行の長さは変わる (狭めた側は切らないので桁より長いまま
        # 残る)。次の消去では作り直させる
        self._screen_blank = False
        # 折り返しで次へ続く行は埋めない。行の長さがそのまま「どこで
        # 折り返したか」なので、埋めると繋いだときに埋め草ぶんの隙間が
        # 開く (窓を広げると鍵の途中に空白が入る、として報告された)
        for lines, marks in ((self.lines, self.wrapped),
                             (self._other, self._other_wrapped)):
            for r, line in enumerate(lines):
                if len(line) < cols and not (r < len(marks) and marks[r]):
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
                self._record_new_history(self.history[-1],
                                         main_marks.pop(0))
                keep_row = max(0, keep_row - 1)
        while len(main) < rows:
            main.append([BLANK] * cols)
            main_marks.append(False)
        if self.alt_active:
            if self._saved_main:
                # 位置だけ画面に収め、属性と文字集合はそのまま持ち越す
                self._saved_main = ((min(keep_row, rows - 1),
                                     min(self._saved_main[1], cols - 1))
                                    + self._saved_main[2:])
        else:
            self.cursor_row = keep_row

        del alt[rows:]
        del alt_marks[rows:]
        while len(alt) < rows:
            alt.append([BLANK] * cols)
            alt_marks.append(False)

        # 折り返し待ちは「カーソルは論理的に桁 self.cols にいる」状態。
        # 桁で丸める前に論理位置へ直しておかないと、右端ちょうどまで
        # 書いて出力が止まっている瞬間に窓の大きさや文字の大きさを変え
        # たとき、続きの 1 文字目が右端の文字を上書きして黙って消す
        was_cols = self.cols
        logical_col = self.cursor_col + (1 if self._pending_wrap else 0)
        self.rows, self.cols = rows, cols
        self.scroll_top, self.scroll_bottom = 0, rows - 1
        self.cursor_row = min(self.cursor_row, rows - 1)
        self.cursor_col = min(logical_col, cols - 1)
        # 桁が広がったら折り返す必要が無くなるので、待ちを解いて旧桁の
        # 位置から続ける。桁が狭くなるときも解く。持ち越すと次の 1 文字
        # が _linefeed(from_wrap=True) を呼び、その中の切り詰めが「新しい
        # 桁」で行を切って、右端の外に書かれていた 旧桁 - 新桁 文字が
        # 画面からも文書からも消える。持ち越しが要るのは、直したかった
        # 「右端の 1 文字が上書きされる」が起きる行数だけの変更
        self._pending_wrap = (self._pending_wrap and logical_col >= cols
                              and cols >= was_cols)
        self.dirty = set(range(rows))
        self._reflowed = True

    # ---- 印字と C0 -------------------------------------------------

    def _print(self, text):
        # この印字が今の行へ入った時点の折り返しの印。書き直しが右端
        # まで届いたときだけ残す判断に使う
        entry_row, entry_mark = None, False
        # 文字集合を変える命令 (ESC ( と SI/SO) は別の命令なので、1 つの
        # 印字の途中では変わらない。罫線は 1 文字ずつ置き換えながら書く。
        # 挿入モードも、1 文字ごとに右を送りながら書く
        if self._g[self._charset] == "0" or self.insert_mode:
            self._print_chars(text, entry_row, entry_mark)
            return
        # 幅 1 と決まっている文字の連なりはまとめて書き、それ以外
        # (全角・結合文字・DEL など) は 1 文字ずつ書く
        for narrow, other in _PRINT_RUNS.findall(text):
            if narrow:
                entry_row, entry_mark = self._print_narrow(
                    narrow, entry_row, entry_mark)
            else:
                entry_row, entry_mark = self._print_chars(
                    other, entry_row, entry_mark)

    def _print_chars(self, text, entry_row, entry_mark):
        """text を 1 文字ずつ書く。(entry_row, entry_mark) を更新して返す。"""
        # 画面へ中身が入るのはここと _print_narrow だけ。空だという
        # 覚えは、_print ではなくこの 2 つで落とす (_print を差し替える
        # 検証用の派生画面が、この下だけを呼ぶため)
        self._screen_blank = False
        for ch in text:
            if ch == "\x7f":            # DEL は表示しない
                continue
            if self._g[self._charset] == "0":
                ch = DEC_GRAPHICS.get(ch, ch)
            width = _cell_width(ch)
            if width == 0:              # 結合文字・ZWJ は桁を進めない
                self._join_previous(ch)
                continue
            if width == 2 and self.cols < 2:
                continue                # 1 桁の画面に全角は置けない
            from_wrap = False           # この文字で折り返して行が変わった
            if self._pending_wrap and not self.autowrap:
                # DECRC・1049 で戻した折り返し待ち。xterm と同じく、実行する
                # 時点で折り返しが無効なら捨てて右端へ重ねる
                self._pending_wrap = False
            if self._pending_wrap:      # 右端の 1 文字あとの折り返し
                self.cursor_col = 0
                self._linefeed(from_wrap=True)
                from_wrap = True
            if width == 2 and self.cursor_col + 1 >= self.cols:
                # 全角が右端の 1 セルに収まらない。xterm と同じく右端は
                # 空けたまま丸ごと次の行へ送る (折り返し無効なら手前に重ねる)
                if self.autowrap:
                    # 空けたセルは印字していない。折り返し行は描画側で
                    # 末尾を刈らずに次の行へ繋ぐので、残すとコピーと
                    # ログへ空白が 1 つ混ざる
                    skipped = self.lines[self.cursor_row]
                    keep = self.cols - 1
                    if keep < len(skipped) and skipped[keep][0] == "":
                        keep -= 1       # 右端は全角の後ろ半分。丸ごと落とす
                    del skipped[keep:]
                    self.cursor_col = 0
                    self._linefeed(from_wrap=True)
                    from_wrap = True
                else:
                    self.cursor_col = self.cols - 2
            if from_wrap:
                entry_row = None        # 行が変わった (巻き上げも含む)
            elif self.cursor_col == 0 and self.cursor_row:
                # 折り返しで来たのではなく行頭から書き始めた。ここは
                # 新しい論理行の先頭なので、前の行の古い印を落とす
                self.wrapped[self.cursor_row - 1] = False
            if entry_row != self.cursor_row:
                entry_row = self.cursor_row
                entry_mark = self.wrapped[entry_row]
            if self.insert_mode:
                # IRM: 書く前に、カーソルから右を文字の幅ぶん右へ送る。
                # 右端からあふれた文字は消える (ICH と同じ。xterm も同じ)
                self._shift_chars(width, insert=True)
            line = self.lines[self.cursor_row]
            end = self.cursor_col + width
            if end > len(line):
                # 折り返し行は埋めていないので、書くときに伸ばす
                line.extend([BLANK] * (end - len(line)))
            # 全角の半分に重ねて書いたら、残る半分は空白になる
            _split_wide(line, self.cursor_col)
            _split_wide(line, end)
            line[self.cursor_col] = (ch, self.attr)
            if width == 2:
                line[self.cursor_col + 1] = ("", self.attr)     # 継続セル
            # 折り返しの印は折り返しでだけ付く。書き直しでは、右端まで
            # 届いたときに限って元の印を残す (まだ次の行へ続いている)。
            # 届かなければ外す。残すと、EL 無しで書き直された行が履歴で
            # 次の行と連結される。空白で塗り潰して消した行も中身が無い
            # ので外す (EL / DCH と同じ基準。印が残るときしか調べない)
            self.wrapped[self.cursor_row] = (
                entry_mark and end >= self.cols and self.autowrap
                and not all(c == BLANK for c in line))
            self.dirty.add(self.cursor_row)
            if end < self.cols:
                self.cursor_col = end
            else:
                self.cursor_col = self.cols - 1
                if self.autowrap:
                    self._pending_wrap = True
                # autowrap 無効なら右端で上書きを続ける
        return entry_row, entry_mark

    def _print_narrow(self, text, entry_row, entry_mark):
        """幅 1 の文字だけの text を、今の行に収まる分ずつまとめて書く。

        _print_chars で 1 文字ずつ書いたのと同じ結果にする。1 文字ずつ
        だと、折り返し待ちの始末と行頭で前の行の印を落とす処理は区切りの
        1 文字目でしか起きず (2 文字目からは桁が 0 より右)、全角の片割れ
        の始末は区切りの両端しか効かず (内側はすぐ上書きされる)、折り返し
        の印とカーソルは最後の文字の結果が残る。(entry_row, entry_mark)
        を更新して返す。
        """
        self._screen_blank = False      # _print_chars と同じ (中身が入る)
        cols = self.cols
        cell_attr = itertools.repeat(self.attr)
        start, stop = 0, len(text)
        while start < stop:
            if self._pending_wrap and not self.autowrap:
                self._pending_wrap = False  # 戻した折り返し待ち (_print_chars)
            if self._pending_wrap:      # 右端の 1 文字あとの折り返し
                self.cursor_col = 0
                self._linefeed(from_wrap=True)
                entry_row = None        # 行が変わった (巻き上げも含む)
            elif self.cursor_col == 0 and self.cursor_row:
                # 行頭から書き始めた。前の行の古い印を落とす (_print_chars)
                self.wrapped[self.cursor_row - 1] = False
            row, col = self.cursor_row, self.cursor_col
            if entry_row != row:
                entry_row = row
                entry_mark = self.wrapped[row]
            # 右端までに収まる分。桁が右端にあっても 1 文字は書く
            count = max(1, min(stop - start, cols - col))
            end = col + count
            line = self.lines[row]
            if end > len(line):
                line.extend([BLANK] * (end - len(line)))
            _split_wide(line, col)
            _split_wide(line, end)
            line[col:end] = zip(text[start:start + count], cell_attr)
            start += count
            # 空白で塗り潰して消した行からは印を外す (_print_chars と同じ)
            self.wrapped[row] = (
                entry_mark and end >= cols and self.autowrap
                and not all(c == BLANK for c in line))
            self.dirty.add(row)
            if end < cols:
                self.cursor_col = end
            else:
                self.cursor_col = cols - 1
                if self.autowrap:
                    self._pending_wrap = True
        return entry_row, entry_mark

    def _join_previous(self, ch):
        """幅 0 の文字 (結合文字・ZWJ 等) を直前の文字のセルへ繋げる。

        右端で折り返し待ちなら今のセル、そうでなければ 1 つ左のセル。
        折り返しが無効 (ESC[?7l) なら右端で印字してもカーソルが動かず
        折り返し待ちも立たないので、最終桁にいるときは今のセルを選ぶ。
        そこが全角の継続セルなら、その全角本体へ繋げる。前に文字が無い
        (行頭) ときと、セルが MAX_CELL_TEXT まで伸びているときは捨てる。

        残る制限: 折り返しが無効なとき、最終桁で印字した直後なのか、
        最終桁へ CUP しただけなのかを区別していない。後者では 1 つ左の
        文字へ付けるべきだが、ここでは最終桁のセルへ繋げる。
        """
        line = self.lines[self.cursor_row]
        at_last_col = (not self.autowrap
                       and self.cursor_col == self.cols - 1)
        i = (self.cursor_col if self._pending_wrap or at_last_col
             else self.cursor_col - 1)
        if 0 < i < len(line) and line[i][0] == "":
            i -= 1
        if not 0 <= i < len(line):
            return
        text, attr = line[i]
        if len(text) >= MAX_CELL_TEXT:
            return                      # 伸びすぎたセルへはもう繋げない
        line[i] = (text + ch, attr)
        self.dirty.add(self.cursor_row)

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
        if from_wrap:
            # 折り返した時点で、この行の内容はちょうど今の桁幅ぶん。
            # 桁を狭めても行は切り詰めない設計 (触ると往復のたびに削れる)
            # なので、それより後ろには広かった頃の文字が残っている。
            # 描画側は折り返し行のセルを丸ごと次の行へ繋げるため、残して
            # おくと 1 行の途中へ古い文字や空白の塊が差し込まれる。
            # 「行の長さ = 折り返し位置」という前提をここで回復する。
            # 機器が送った改行 (from_wrap=False) では触らない。
            del self.lines[self.cursor_row][self.cols:]
        if self.cursor_row == self.scroll_bottom:
            self._scroll_up(1, from_wrap=from_wrap)
        elif self.cursor_row + 1 < self.rows:
            self.cursor_row += 1

    def _drop_mark_above(self, row):
        """row へ別の行が来たので、1 つ上の行の折り返しの印を外す。

        印は「この行は次の行へ続く」という意味しか持たないので、続きの
        行が動いて別の論理行や空行が下へ来たら外さないと、描画側が
        無関係な 2 行を 1 行に繋ぐ。画面の先頭には上の行が無い。
        """
        if row:
            self.wrapped[row - 1] = False

    def _scroll_up(self, n, from_wrap=False):
        # 範囲の下端にあった行は n 行ぶん上がるが、その続き (下端の 1 つ
        # 下の行) は範囲の外なので動かない。印を残すと、描画側が範囲の
        # 中の行と範囲外の行を 1 行に繋ぐ。動かす前に外すので、範囲の
        # 高さぶん回して下端の行が履歴へ押し出される場合も同じに直る
        # (印ごと履歴へ入れると、そのあと空いた先頭へ来た無関係な出力と
        # 1 行に繋がる)。最下行での折り返し (_linefeed(from_wrap=True))
        # の印だけはそこに載っているので残す
        if not from_wrap:
            self.wrapped[self.scroll_bottom] = False
        for _ in range(n):
            removed = self.lines.pop(self.scroll_top)
            removed_wrap = self.wrapped.pop(self.scroll_top)
            self.lines.insert(self.scroll_bottom, self._blank_line())
            self.wrapped.insert(self.scroll_bottom, False)
            # 記録するかどうかは上端が画面の先頭かどうかで決まる (xterm と同じ)。
            # 下端まで全画面であることを求めると、端末が 30 行あって機器が 24 行と
            # 信じている場合の ESC[1;24r がそのまま受理されたときに、以後の出力が
            # 黙って記録から落ちる。窓の大きさを伝えるのは SSH だけなので、
            # Telnet・シリアルでは普通に起こる。
            # 上端が先頭でないときは、押し出された行は画面上に残っているので
            # 記録しない。
            if not self.alt_active and self.scroll_top == 0:
                self.history.append(removed)
                self._record_new_history(removed, removed_wrap)
        # 範囲の上端から出ていった行が 1 つ上の行の続きだったなら、続きは
        # もう下に無い。上端が画面の先頭のときは上の行が無いので、押し
        # 出された行を履歴へ送る既存の扱いは変わらない
        self._drop_mark_above(self.scroll_top)
        self.dirty.update(range(self.scroll_top, self.scroll_bottom + 1))

    def _scroll_down(self, n):
        for _ in range(n):
            self.lines.pop(self.scroll_bottom)
            self.wrapped.pop(self.scroll_bottom)
            self.lines.insert(self.scroll_top, self._blank_line())
            self.wrapped.insert(self.scroll_top, False)
        # 範囲の上端へ空行が割り込んだ。1 つ上の行の続きはそこには無い
        self._drop_mark_above(self.scroll_top)
        # 下端も同じ。下端にあった行は範囲の外へ続きを置いたまま捨てられ、
        # 代わりに 1 つ上の行が上がってくる。その行の続きは今捨てた行
        # なので、印を残すと範囲の外の行と 1 行に繋がる
        self.wrapped[self.scroll_bottom] = False
        self.dirty.update(range(self.scroll_top, self.scroll_bottom + 1))

    # ---- CSI -------------------------------------------------------

    def _move(self, row, col):
        self.cursor_row = max(0, min(self.rows - 1, row))
        self.cursor_col = max(0, min(self.cols - 1, col))
        self._pending_wrap = False
        self.dirty.add(self.cursor_row)

    def _csi(self, seq):
        # 中間バイト付きは別の命令 (ESC[?1049$h は h/l を持つ実在列が無い)。
        # 最終文字だけで DECSET/DECRST と取り違えない
        if seq.private == "?" and not seq.intermediate and seq.final in "hl":
            return self._private_mode(seq)
        if not seq.private and seq.intermediate == "!" and seq.final == "p":
            # DECSTR (ソフトリセット)。表示に効くモードのうち IRM だけを
            # 既定 (上書き) へ戻す。画面の中身とカーソルには触らない
            self.insert_mode = False
            return
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
        elif f == "E":                  # CNL: xterm は CUD 経由なので
            limit = (self.scroll_bottom  # 範囲の下端で止まる
                     if self.cursor_row <= self.scroll_bottom
                     else self.rows - 1)
            self._move(min(limit, self.cursor_row + n), 0)
        elif f == "F":                  # CPL: 同じく CUU 経由
            limit = (self.scroll_top
                     if self.cursor_row >= self.scroll_top else 0)
            self._move(max(limit, self.cursor_row - n), 0)
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
            # 範囲の高さを超えて回すと、下端へ足した空行まで押し出して
            # 履歴へ入れる (xterm も範囲の高さで頭打ちにする)
            self._scroll_up(min(rows_n,
                                self.scroll_bottom - self.scroll_top + 1))
        elif f == "T":
            self._scroll_down(rows_n)
        elif f == "r":
            self._set_margins(p)
        elif f in "hl":                 # SM / RM。表示に効くのは IRM だけ
            if 4 in p:
                self.insert_mode = f == "h"
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
        elif seq.intermediate:
            # ESC # 8 (DECALN)・ESC * E (G2 指示)・ESC % G など、中間
            # バイト付きは別の命令。最終文字だけで DECRC・NEL・RIS と
            # 取り違えると、カーソルがずれたり画面が消えたりする
            return
        elif seq.final == "7":
            self._save_cursor()
        elif seq.final == "8":
            self._restore_cursor()
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
            # ED 2 と同じく、消す直前に見えていたメイン画面は履歴へ送る。
            # 代替画面の裏に退避していたメイン画面も同じ (代替画面の
            # 中身は記録しない)
            self._switch_screen(False, with_cursor=False)
            self._record_screen()
            self.reset()
        # = > \ H などは表示を変えない

    def _save_cursor(self):
        """DECSC (ESC 7) と ?1048h の保存。xterm も同じ保存領域を使う。"""
        self._saved = (self.cursor_row, self.cursor_col, self.attr,
                       dict(self._g), self._charset, self._pending_wrap,
                       self.cols)

    def _restore_cursor(self):
        """DECRC (ESC 8) と ?1048l の復元。"""
        row, col, attr, g, charset, pending, saved_cols = self._saved
        self.attr = attr
        self._g = dict(g)
        self._charset = charset
        self._move(row, col)
        # 保存してあった折り返し待ちは、保存したときより桁が狭くなって
        # いるときだけ解く。折り返し待ちは必ず保存時の右端 (桁 - 1) で
        # 立つので、これは「待っていた右端がいまの桁の外か」と同じ。
        # 持ち越すと次の 1 文字が _linefeed(from_wrap=True) を呼び、その
        # 中の切り詰めが新しい桁で行を切って、右端の外の 旧桁 - 新桁
        # 文字を消す。逆に、いまの桁で立った待ちまで解くと「待たずに
        # 上書き」で受信済みの桁を 1 つ潰す。行の長さで見分けようとする
        # と、窓を狭めた跡 (桁より長い行) は ED 2 が来るまで残るので、
        # 狭めたあとに立った待ちや、広げて狭め直した跡の行で立った待ち
        # まで巻き込んで解いてしまう
        if pending and self.cols < saved_cols:
            pending = False
        # _move が折り返し待ちを落とすので、復元はそのあと
        self._pending_wrap = pending

    def _osc(self, text):
        num, _, rest = text.partition(";")
        if num in ("0", "2"):
            self.title = rest

    def _private_mode(self, seq):
        on = seq.final == "h"
        for mode in seq.params:
            if mode in (1049, 1047, 47):
                # 代替画面を白紙にするのは 1049 の入場と 1047 の
                # 退場だけ (XTerm ctlseqs)。47 はどちらでも消さず、
                # 入り直したときに前の中身がそのまま見える
                clear = (mode == 1049) if on else (mode == 1047)
                self._switch_screen(on, with_cursor=(mode == 1049),
                                    clear=clear)
            elif mode == 1048:
                # 画面は切り替えず、DECSC / DECRC と同じ保存・復元だけ
                # を行う (XTerm ctlseqs)。保存領域も DECSC と共通の
                # _saved。xterm は 1049 = 1048 + 1047 でここも共通だが、
                # この実装の 1049 は _switch_screen の _saved_main へ
                # 保存するので、register は 2 つに分かれている
                if on:
                    self._save_cursor()
                else:
                    self._restore_cursor()
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
            # ほかの私用モードは表示に効かないので無視。
            # DECOM (?6) だけは表示に効くが保持しない (制限は
            # このファイル冒頭の docstring)

    def _switch_screen(self, to_alt, with_cursor, clear=True):
        """代替画面と行き来する。clear は代替画面を白紙にするか。"""
        if to_alt == self.alt_active:
            if to_alt and clear:
                # 代替画面にいるまま 1049h を受けた。xterm は切り替えが
                # 要らなくても ClearScreen は行う (カーソルは動かさない)。
                # 1049 用の保存 (_saved_main) は上書きしない。2 回目の保存で
                # メイン画面へ戻る位置を潰さないため
                for r in range(self.rows):
                    self.lines[r] = self._blank_line()
                    self.wrapped[r] = False
                self.dirty.update(range(self.rows))
                self._pending_wrap = False
            return
        # self.lines が別の画面に差し替わる。空だという覚えは持ち越せない。
        # 入れ替えない呼び出し (上の早期 return) は白紙にするだけで中身を
        # 増やさないので、印を落とさない (落とすと、続く消去が
        # 画面全体を走査する)
        self._screen_blank = False
        # 1049 の復元でだけ書き換わる。saved_cols を いまの桁 にして
        # おけば、復元しなかったときは下の判定が成り立たない
        pending, saved_cols = False, self.cols
        if to_alt and with_cursor:
            # 1049 は DECSC 相当の保存・復元 (XTerm ctlseqs)。文字集合
            # の指示まで持ち帰らないと、代替画面が ESC(0 のまま抜けた
            # ときに以降の出力も記録も罫線文字に化け続ける
            self._saved_main = (self.cursor_row, self.cursor_col, self.attr,
                                dict(self._g), self._charset,
                                self._pending_wrap, self.cols)
        elif to_alt:
            # 47h / 1047h は保存しない。前の 1049 の保存を残すと、この
            # 代替画面から 1049l で出たときに古い位置・属性・文字集合へ
            # 戻り、set_size も残す行をその古い位置で選んでしまう
            self._saved_main = None
        self.lines, self._other = self._other, self.lines
        # 折り返しの印も画面と一緒に入れ替える。裏へ回ったメイン画面の
        # 印を失うと、戻ってきたときに組み直しで繋ぎ直せなくなる
        self.wrapped, self._other_wrapped = (
            self._other_wrapped, self.wrapped)
        # DECSC の保存領域も画面と一緒に入れ替える。共有したままだと、
        # 代替画面のアプリの ESC 7 がメイン画面の保存位置を潰す
        self._saved, self._other_saved = self._other_saved, self._saved
        self.alt_active = to_alt
        if to_alt:
            if clear:               # 1049 の代替画面は白紙で始まる
                # 白紙にするだけ。xterm の 1049 入場は CursorSave →
                # ToAlternate → ClearScreen で、ClearScreen はカーソルを
                # 動かさない (1047/47 も動かさない)
                for r in range(self.rows):
                    self.lines[r] = self._blank_line()
                    self.wrapped[r] = False
        else:
            if clear:               # 1047 は出るときに代替画面を消す
                for r in range(self.rows):
                    self._other[r] = self._blank_line()
                    self._other_wrapped[r] = False
            if with_cursor:
                # 47 で入った代替画面では 1049 用の保存が無い。xterm の
                # 1049l は CursorRestore なので、そのときはメイン画面の
                # 保存領域 (入れ替えたあとの _saved、DECSC) から戻す
                row, col, attr, g, charset, pending, saved_cols = (
                    self._saved_main or self._saved)
                self.attr = attr
                self._g = dict(g)
                self._charset = charset
                self._move(row, col)
        self.dirty.update(range(self.rows))
        # 解く条件は _restore_cursor と同じ (理由もそちらに書いてある)。
        # 保存したときより桁が狭いときだけ解く
        if pending and self.cols < saved_cols:
            pending = False
        # 1049 で持ち帰った折り返し待ちだけは残す。_move も、白紙化の
        # あとの位置決めも落とすので、代入はいちばん最後
        self._pending_wrap = pending

    def _set_margins(self, p):
        # 0 は省略と同じく既定値 (xterm と同じ)。0-1 = -1 を丸めると
        # 上端は偶然 0 になるが、下端は 0 になって top < bottom を満たさず
        # 拒否され、直前の狭い範囲が残り続けた
        top = (_param(p, 0, 1) or 1) - 1
        bottom = (_param(p, 1, self.rows) or self.rows) - 1
        # xterm は画面からはみ出した指定を丸めて受理する。丸めずに捨てると、
        # 直前に受理した狭い範囲がそのまま残り続ける。ncurses は部分スクロール
        # の最適化で狭い範囲を設定し、最後に csr(0, lines-1) で全画面へ戻すが、
        # その lines はアプリが信じている行数 (多くは 24) なので、画面がそれより
        # 小さいと復帰側だけが拒否されて狭い範囲が固着する。
        top = max(0, min(top, self.rows - 1))
        bottom = max(0, min(bottom, self.rows - 1))
        if top < bottom:
            self.scroll_top = top
            self.scroll_bottom = bottom
            self._move(0, 0)            # DECSTBM はカーソルも戻す

    def _erase_display(self, mode):
        if mode == 3:
            # ED 3 は履歴 (スクロールバック) を消す命令で、可視画面には
            # 触らない。NetBelt はセッションの記録を消さない方針なので
            # 何もしない。画面まで消すと clear -x で表示が飛ぶ
            return
        if mode not in (0, 1, 2):
            # ED に定義があるのは 0-3 だけ (XTerm ctlseqs)。未定義の
            # 値を全消去として扱うと、機器が出した行が黙って消える
            return
        # 画面全体が消えるとき (clear は ESC[H ESC[J、つまり home からの
        # mode 0 で来る) は、消す前に見えていた中身を履歴へ送る。
        # clear でセッションの記録を失わない、という v1.1.1 の方針
        row = self.lines[self.cursor_row]
        # ED 0 の走査の開始桁も、実際に消える範囲の左端へ合わせる。
        # カーソルが全角の後半桁にあると _erase_line(0) は _split_wide で
        # その全角の前半桁まで戻って払うので、丸めずに (0, 0) と比べると
        # 画面は丸ごと空になるのに記録だけが残らない
        col = self.cursor_col
        if 0 < col < len(row) and row[col][0] == "":
            col -= 1
        wipes_all = (mode >= 2 or
                     (mode == 0 and (self.cursor_row, col) == (0, 0)))
        # 原点以外からの ED 0 も、カーソルより前がすべて空白なら画面は
        # 丸ごと空になる (1 行目が空の 1 画面目や、ホームへ戻らない ED 2 の
        # あとの最下行など)。下の ED 1 の blank_after と対称に履歴へ送る
        # (消し方は mode == 0 の枝のまま)
        blank_before = mode == 0 and (self._screen_blank or all(
            _is_blank(self.lines[r][:col if r == self.cursor_row else None])
            for r in range(0, self.cursor_row + 1)))
        # ED 1 も、カーソルより下に中身が残らなければ画面は丸ごと
        # 空白になる。最下行の右端に限らず、機器が数行出した直後の
        # ESC[1J (24x80 で 2 行だけ、など) が該当する。消える中身は
        # ED 2 と同じなので、同じく履歴へ送る
        # (消し方は下の mode == 1 の枝のまま。行が桁数より長い
        # ことがあり、全画面消去と同じに払うと右に残る分も消える)
        # 走査の開始桁は、実際に消える範囲の右端へ合わせる。カーソルが
        # 全角の前半桁にあると _erase_line(1) はその継続セルまで払うが、
        # 継続セル ("", attr) は BLANK と一致しないので、丸めずに数えると
        # 「下に中身が残る」と誤判定して画面だけが消え記録が残らない
        start = self.cursor_col + 1
        if start < len(row) and row[start][0] == "":
            start += 1
        blank_after = mode == 1 and (self._screen_blank or all(
            _is_blank(self.lines[r][start if r == self.cursor_row else 0:])
            for r in range(self.cursor_row, self.rows)))
        if wipes_all or blank_before or blank_after:
            self._record_screen()
        if wipes_all:
            rng = range(0, self.rows)
        elif mode == 0:
            self._erase_line(0)
            # ED 0 はカーソル行の下も丸ごと消すので、この行の続きは
            # 中身が残っていても無くなる。EL 0 は続きの行を触らないため
            # 印を保つ (_erase_line) が、ここは外さないと、あとから
            # 下の行へ出た無関係な出力と 1 行に繋がる
            self.wrapped[self.cursor_row] = False
            rng = range(self.cursor_row + 1, self.rows)
        else:
            self._erase_line(1)
            rng = range(0, self.cursor_row)
        # 画面が既に丸ごと空白なら、作り直しても同じ行になる。1 回の
        # 受信分に収まる ESC[2J の繰り返しだけで、履歴を 1 行も増やさない
        # まま 100x300 で 3.7 秒 GUI が止まっていた (走査と作り直しが
        # 命令ごとに 行 x 桁 ぶん走るため)
        for r in rng:
            # 覚えがあっても、窓を狭めた跡で桁より長い行は作り直す。
            # 長さまで同じでないと「作り直しても同じ行」にならない
            if not self._screen_blank or len(self.lines[r]) != self.cols:
                self.lines[r] = self._blank_line()
                self.wrapped[r] = False
        self.dirty.update(rng)
        # 走査で「残りは全部空白」と分かった ED 0 / ED 1 のあとも画面は
        # 丸ごと空白。ここで覚え直さないと、窓や文字の大きさを変えたあと
        # (set_size が落とす) や代替画面から戻ったあとの連打で、毎回また
        # 画面全体を走査する。折り返しの印が残るうちは覚えない (全セルが
        # 空白でも印だけ残る状態は実在する。右端の桁で折り返したあとなど)
        if wipes_all or ((blank_before or blank_after)
                         and not any(self.wrapped)):
            self._screen_blank = True
        self._pending_wrap = False

    def _record_screen(self):
        """画面全体が消える前に、最後の非空行までを履歴へ送る。"""
        # 空だと分かっている画面には送る中身が無い。走査ごと省く
        if self.alt_active or self._screen_blank:
            return
        last = -1
        for r in range(self.rows):
            if not _is_blank(self.lines[r]):
                last = r
        for r, line in enumerate(self.lines[:last + 1]):
            # 呼び出し元が同じ行をその場で消すことがある (ED 1)。
            # 履歴が巻き添えで空にならないよう写しを渡す
            line = list(line)
            self.history.append(line)
            # 最後の行の続き (空白の行) は記録しないので、次へ続く印を
            # 渡すと、描画側が消去のあとに来た出力と 1 行に繋げる
            self._record_new_history(line, self.wrapped[r] and r < last)

    def _erase_line(self, mode):
        if mode not in (0, 1, 2):
            return                  # EL に定義があるのは 0-2 だけ
        line = self.lines[self.cursor_row]
        # 行の長さは桁数と一致しない。窓を縮めても切らないので長いことが
        # あり、折り返しで続く行は広げても埋めないので短いこともある。
        # カーソルは桁数まで動けるので、必ず行の実際の長さで抑える
        end = min(self.cursor_col + 1, len(line))
        if mode == 0:
            rng = range(min(self.cursor_col, len(line)), len(line))
        elif mode == 1:
            rng = range(0, end)
        else:
            rng = range(0, len(line))
        # 範囲の端が全角の途中なら、その全角は丸ごと消える
        _split_wide(line, rng.start)
        _split_wide(line, rng.stop)
        for c in rng:
            line[c] = BLANK
        # 消したあとに中身が残る行は、次の行への続きを持ったまま。丸ごと
        # 空白になったときだけ外す (ECH / DCH / ICH と同じ基準)。行末まで
        # 届いたかどうかでは決めない。EL 0 は必ず届くので、そうすると
        # ECH・DCH・右端まで空白を印字した書き直しと同じ画面から、EL 0 の
        # ときだけ 1 本の論理行が履歴・コピー・文書で 2 行に割れる
        if all(c == BLANK for c in line):
            self.wrapped[self.cursor_row] = False
        self.dirty.add(self.cursor_row)
        self._pending_wrap = False

    def _shift_lines(self, n, insert):
        """IL / DL。スクロール範囲の中でだけ効く。"""
        if not self.scroll_top <= self.cursor_row <= self.scroll_bottom:
            return
        # 回数はカーソルから範囲の下端までの行数で頭打ち (xterm と同じ)。
        # 超えて回すと、DL は下端へ足した空行まで押し出して履歴へ入れる
        n = min(n, self.scroll_bottom - self.cursor_row + 1)
        # DL は範囲の下端にあった行を上げるが、その続き (下端の 1 つ下の
        # 行) は範囲の外なので動かない。動かす前に印を外す (_scroll_up と
        # 同じ)。IL は下端の行を捨てる側なので、下で別に始末する
        if not insert:
            self.wrapped[self.scroll_bottom] = False
        for _ in range(n):
            if insert:
                self.lines.pop(self.scroll_bottom)
                self.wrapped.pop(self.scroll_bottom)
                self.lines.insert(self.cursor_row, self._blank_line())
                self.wrapped.insert(self.cursor_row, False)
            else:
                removed = self.lines.pop(self.cursor_row)
                removed_wrap = self.wrapped.pop(self.cursor_row)
                self.lines.insert(self.scroll_bottom, self._blank_line())
                self.wrapped.insert(self.scroll_bottom, False)
                # 画面の先頭を削ると、押し出された行はどこにも残らない。
                # 上へ押し出す動きは SU と同じなので、_scroll_up と同じ
                # 条件で履歴へ送る。条件を揃えないと、画面に残っている
                # 行まで記録して二重に出る
                if (not self.alt_active and self.scroll_top == 0
                        and self.cursor_row == 0):
                    self.history.append(removed)
                    self._record_new_history(removed, removed_wrap)
        # カーソル行へは別の行 (DL) か空行 (IL) が来た。1 つ上の行の続きは
        # もう下に無いので印を外す。残すと、無関係な 2 つの論理行が履歴・
        # コピー・文書で 1 行に繋がる
        self._drop_mark_above(self.cursor_row)
        if insert:
            # IL は範囲の下端の行を、続きを範囲の外へ置いたまま捨てて
            # 1 つ上の行を下端へ上げる。上がってきた行の続きは今捨てた行
            # なので、印を残すと範囲の外の行と 1 行に繋がる
            self.wrapped[self.scroll_bottom] = False
        self.dirty.update(range(self.cursor_row, self.scroll_bottom + 1))
        # DEC の IL/DL はカーソルを左マージンへ戻す (xterm も同じ)。
        # 戻さないと、直後に位置指定なしで印字したとき桁がずれる
        self.cursor_col = 0
        self._pending_wrap = False

    def _shift_chars(self, n, insert):
        """ICH / DCH。行の右端は詰まる・押し出される。"""
        line = self.lines[self.cursor_row]
        # カーソルが行の実際の長さより右にあることがある (折り返しで
        # 続く行は広げても埋めないため)。そこで詰めても意味が無い
        if self.cursor_col >= len(line):
            return
        _split_wide(line, self.cursor_col)     # 全角の途中で割らない
        for _ in range(n):
            if insert:
                # 行が桁数いっぱいのときだけ右端を押し出す。折り返し行は
                # 広げても埋めないので桁数より短いことがあり、そこで
                # 無条件に pop すると余裕があるのに行末の文字が消える
                if len(line) >= self.cols:
                    if line.pop()[0] == "":     # 全角の半分だけ残さない
                        line[-1] = BLANK
                line.insert(self.cursor_col, BLANK)
            else:
                line.pop(self.cursor_col)
                line.append(BLANK)
                if line[self.cursor_col][0] == "":
                    line[self.cursor_col] = BLANK
        # 詰め・押し出しで行が丸ごと空白になったら、この行から次の行への
        # 続きは無い。EL 1 と同じ症状に DCH / ICH からも到達でき、印が
        # 残ると空になった行が履歴・コピーで次の行と繋がる。中身が残る
        # ときは折り返しのまま (EL 1 の判定と揃える)
        if all(c == BLANK for c in line):
            self.wrapped[self.cursor_row] = False
        self.dirty.add(self.cursor_row)
        self._pending_wrap = False

    def _erase_chars(self, n):
        line = self.lines[self.cursor_row]
        rng = range(self.cursor_col, min(len(line), self.cursor_col + n))
        _split_wide(line, rng.start)            # 全角は丸ごと消える
        _split_wide(line, rng.stop)
        for c in rng:
            line[c] = BLANK
        # 消したあとに中身が残る行は、次の行への続きを持ったまま。
        # 丸ごと空白になったときだけ外す (EL / DCH / ICH と同じ基準)。
        # 無条件に外すと、ECH だけが 1 本の論理行を履歴・コピー・文書で
        # 2 行に割る
        if all(c == BLANK for c in line):
            self.wrapped[self.cursor_row] = False
        self.dirty.add(self.cursor_row)
        self._pending_wrap = False
