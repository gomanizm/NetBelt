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
Csi = collections.namedtuple("Csi", "private params intermediate final")
Osc = collections.namedtuple("Osc", "text")
Dcs = collections.namedtuple("Dcs", "private params intermediate final text")

(GROUND, ESCAPE, ESCAPE_INTERMEDIATE,
 CSI_ENTRY, CSI_PARAM, CSI_INTERMEDIATE, CSI_IGNORE,
 OSC_STRING, SOS_PM_APC,
 DCS_ENTRY, DCS_PARAM, DCS_INTERMEDIATE, DCS_PASSTHROUGH,
 DCS_IGNORE) = range(14)

MAX_PARAMS = 256      # 暴走した列でメモリを食わないための上限
MAX_STRING = 4096


class Parser(object):
    def __init__(self):
        self.state = GROUND
        self._clear()
        self._string = []
        self._hook = None

    def _clear(self):
        self._private = ""
        self._params = ""
        self._intermediate = ""

    def _split_params(self):
        if not self._params:
            return ()
        return tuple(int(p) if p else None for p in self._params.split(";"))

    def _end_string(self, out):
        """ESC / ST で文字列列 (OSC/DCS) を閉じ、確定した命令を出す。"""
        if self.state == OSC_STRING:
            out.append(Osc("".join(self._string)))
        elif self.state == DCS_PASSTHROUGH:
            private, params, intermediate, final = self._hook
            out.append(Dcs(private, params, intermediate, final,
                           "".join(self._string)))
        self._string = []
        self._hook = None

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
                self._hook = None
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
                elif ch == "[":
                    self._clear()
                    self.state = CSI_ENTRY
                elif ch == "]":
                    self._string = []
                    self.state = OSC_STRING
                elif ch == "P":
                    self._clear()
                    self.state = DCS_ENTRY
                elif ch in "X^_":
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

            elif state == CSI_ENTRY:
                if code < 0x20:
                    out.append(Ctrl(ch))
                elif code <= 0x2F:
                    self._intermediate += ch
                    self.state = CSI_INTERMEDIATE
                elif ch == ":":
                    self.state = CSI_IGNORE
                elif code <= 0x39 or ch == ";":
                    self._params += ch
                    self.state = CSI_PARAM
                elif code <= 0x3F:              # < = > ?
                    self._private += ch
                    self.state = CSI_PARAM
                elif code <= 0x7E:
                    out.append(Csi(self._private, self._split_params(),
                                   self._intermediate, ch))
                    self.state = GROUND
                elif code == 0x7F:
                    pass
                else:
                    abort_and_print(ch)

            elif state == CSI_PARAM:
                if code < 0x20:
                    out.append(Ctrl(ch))
                elif code <= 0x2F:
                    self._intermediate += ch
                    self.state = CSI_INTERMEDIATE
                elif (0x30 <= code <= 0x39 or ch == ";") and \
                        len(self._params) < MAX_PARAMS:
                    self._params += ch
                elif code <= 0x3F:              # : < = > ? と長すぎる列
                    self.state = CSI_IGNORE
                elif code <= 0x7E:
                    out.append(Csi(self._private, self._split_params(),
                                   self._intermediate, ch))
                    self.state = GROUND
                elif code == 0x7F:
                    pass
                else:
                    abort_and_print(ch)

            elif state == CSI_INTERMEDIATE:
                if code < 0x20:
                    out.append(Ctrl(ch))
                elif code <= 0x2F:
                    self._intermediate += ch
                elif code <= 0x3F:
                    self.state = CSI_IGNORE
                elif code <= 0x7E:
                    out.append(Csi(self._private, self._split_params(),
                                   self._intermediate, ch))
                    self.state = GROUND
                elif code == 0x7F:
                    pass
                else:
                    abort_and_print(ch)

            elif state == CSI_IGNORE:
                if code < 0x20:
                    out.append(Ctrl(ch))
                elif 0x40 <= code <= 0x7E:
                    self.state = GROUND
                elif code <= 0x7F:
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

            elif state == DCS_ENTRY:
                if code < 0x20:
                    pass
                elif code <= 0x2F:
                    self._intermediate += ch
                    self.state = DCS_INTERMEDIATE
                elif ch == ":":
                    self.state = DCS_IGNORE
                elif code <= 0x39 or ch == ";":
                    self._params += ch
                    self.state = DCS_PARAM
                elif code <= 0x3F:
                    self._private += ch
                    self.state = DCS_PARAM
                elif code <= 0x7E:
                    self._hook = (self._private, self._split_params(),
                                  self._intermediate, ch)
                    self._string = []
                    self.state = DCS_PASSTHROUGH
                elif code == 0x7F:
                    pass
                else:
                    abort_and_print(ch)

            elif state == DCS_PARAM:
                if code < 0x20:
                    pass
                elif code <= 0x2F:
                    self._intermediate += ch
                    self.state = DCS_INTERMEDIATE
                elif (0x30 <= code <= 0x39 or ch == ";") and \
                        len(self._params) < MAX_PARAMS:
                    self._params += ch
                elif code <= 0x3F:
                    self.state = DCS_IGNORE
                elif code <= 0x7E:
                    self._hook = (self._private, self._split_params(),
                                  self._intermediate, ch)
                    self._string = []
                    self.state = DCS_PASSTHROUGH
                elif code == 0x7F:
                    pass
                else:
                    abort_and_print(ch)

            elif state == DCS_INTERMEDIATE:
                if code < 0x20:
                    pass
                elif code <= 0x2F:
                    self._intermediate += ch
                elif code <= 0x3F:
                    self.state = DCS_IGNORE
                elif code <= 0x7E:
                    self._hook = (self._private, self._split_params(),
                                  self._intermediate, ch)
                    self._string = []
                    self.state = DCS_PASSTHROUGH
                elif code == 0x7F:
                    pass
                else:
                    abort_and_print(ch)

            elif state == DCS_PASSTHROUGH:
                if code == 0x7F:
                    pass
                elif len(self._string) < MAX_STRING:
                    self._string.append(ch)

            elif state == DCS_IGNORE:
                pass                            # ST/ESC/CAN/SUB まで無視

        flush()
        return out
