"""受信文字列を端末命令の列へ直す状態機械。

Paul Williams の DEC 互換パーサ状態遷移図
(https://vt100.net/emu/dec_ansi_parser) を実装する。UI を知らない。
受信の切れ目は状態として持ち越されるので、呼び出し側での
継ぎ足し処理は要らない。

図からの意図的な違いは 3 つ:
- OSC を BEL (0x07) でも終端する。図の終端は ST だけだが、実際の
  シェルは ``ESC ] 0 ; title BEL`` を送ってくる (xterm の挙動)。
- 入力はデコード済みの str。0x80-0x9F の C1 制御は UTF-8 経由では
  現れないので、C1 の遷移は実装しない。図に無い 0x7E 超えの文字が
  列の途中に来たら、列を捨ててその文字を印字する。
- CAN/SUB で中断した OSC/DCS は、途中までの中身を届けず捨てる。
"""
import collections

Print = collections.namedtuple("Print", "text")
Ctrl = collections.namedtuple("Ctrl", "char")
Esc = collections.namedtuple("Esc", "intermediate final")
Osc = collections.namedtuple("Osc", "text")

(GROUND, ESCAPE, ESCAPE_INTERMEDIATE,
 OSC_STRING, SOS_PM_APC) = range(5)

MAX_PARAMS = 256      # 暴走した列でメモリを食わないための上限
MAX_STRING = 4096


class Parser(object):
    def __init__(self):
        self.state = GROUND
        self._clear()
        self._string = []

    def _clear(self):
        self._private = ""
        self._params = ""
        self._intermediate = ""

    def _end_string(self, out):
        """ESC / ST で文字列列 (OSC) を閉じ、確定した命令を出す。"""
        if self.state == OSC_STRING:
            out.append(Osc("".join(self._string)))
        self._string = []

    def feed(self, text):
        """デコード済み文字列を食わせ、確定した命令のリストを返す。"""
        out = []
        printable = []

        def flush():
            if printable:
                out.append(Print("".join(printable)))
                del printable[:]

        def abort_and_print(ch):
            # 図に無い文字 (0x7E 超え) が列の途中に来た
            self.state = GROUND
            printable.append(ch)

        for ch in text:
            code = ord(ch)

            # anywhere: どの状態でも同じ扱い
            if code == 0x1B:
                flush()
                self._end_string(out)
                self._clear()
                self.state = ESCAPE
                continue
            if code in (0x18, 0x1A):            # CAN / SUB は列を捨てる
                flush()
                self._string = []
                out.append(Ctrl(ch))
                self.state = GROUND
                continue

            state = self.state
            if state == GROUND:
                if code >= 0x20:                # 0x7F(DEL) の無視は画面側で
                    printable.append(ch)
                else:
                    flush()
                    out.append(Ctrl(ch))
                continue
            flush()

            if state == ESCAPE:
                if code < 0x20:
                    out.append(Ctrl(ch))
                elif code <= 0x2F:
                    self._intermediate += ch
                    self.state = ESCAPE_INTERMEDIATE
                elif ch == "]":
                    self._string = []
                    self.state = OSC_STRING
                elif ch in "PX^_":              # DCS はまだ中身を読まず捨てる
                    self.state = SOS_PM_APC
                elif code <= 0x7E:
                    out.append(Esc(self._intermediate, ch))
                    self.state = GROUND
                elif code == 0x7F:
                    pass
                else:
                    abort_and_print(ch)

            elif state == ESCAPE_INTERMEDIATE:
                if code < 0x20:
                    out.append(Ctrl(ch))
                elif code <= 0x2F:
                    self._intermediate += ch
                elif code <= 0x7E:
                    out.append(Esc(self._intermediate, ch))
                    self.state = GROUND
                elif code == 0x7F:
                    pass
                else:
                    abort_and_print(ch)

            elif state == OSC_STRING:
                if code == 0x07:                # xterm 拡張: BEL 終端
                    self._end_string(out)
                    self.state = GROUND
                elif code < 0x20:
                    pass
                elif len(self._string) < MAX_STRING:
                    self._string.append(ch)

            elif state == SOS_PM_APC:
                pass                            # ST/ESC/CAN/SUB まで無視

        flush()
        return out
