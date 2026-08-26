"""文字属性 (ECMA-48 の SGR)。画面のセルがそのまま持つ値オブジェクト。

色は番号のまま持つ。0-7 が基本色、8-15 が明色、16-255 が 256 色
パレット、(r, g, b) のタプルが直接指定。番号をどの実色に写すかは
描画側 (テーマ) の仕事で、ここでは決めない。
"""
import collections

Attr = collections.namedtuple("Attr", "fg bg bold underline reverse")
DEFAULT = Attr(None, None, False, False, False)


def apply_sgr(attr, params):
    """SGR パラメータ列を attr へ適用した結果を返す。

    知らない番号は黙って飛ばす (ECMA-48 にはこちらが対応しない
    属性も多い)。省略されたパラメータは 0 (全解除) として扱う。
    """
    if not params:
        return DEFAULT
    params = list(params)
    i = 0
    while i < len(params):
        p = params[i] or 0
        if p == 0:
            attr = DEFAULT
        elif p == 1:
            attr = attr._replace(bold=True)
        elif p == 4:
            attr = attr._replace(underline=True)
        elif p == 7:
            attr = attr._replace(reverse=True)
        elif p == 22:
            attr = attr._replace(bold=False)
        elif p == 24:
            attr = attr._replace(underline=False)
        elif p == 27:
            attr = attr._replace(reverse=False)
        elif 30 <= p <= 37:
            attr = attr._replace(fg=p - 30)
        elif p == 39:
            attr = attr._replace(fg=None)
        elif 40 <= p <= 47:
            attr = attr._replace(bg=p - 40)
        elif p == 49:
            attr = attr._replace(bg=None)
        elif 90 <= p <= 97:
            attr = attr._replace(fg=p - 90 + 8)
        elif 100 <= p <= 107:
            attr = attr._replace(bg=p - 100 + 8)
        elif p in (38, 48):
            colour, used = _extended_colour(params[i + 1:])
            if colour is None:
                break               # 形が崩れていたら残りごと捨てる
            if p == 38:
                attr = attr._replace(fg=colour)
            else:
                attr = attr._replace(bg=colour)
            i += used
        i += 1
    return attr


def _extended_colour(rest):
    """38/48 に続く色指定を読む。(色, 消費した個数) を返す。"""
    if len(rest) >= 2 and rest[0] == 5 and rest[1] is not None:
        return min(max(rest[1], 0), 255), 2
    if len(rest) >= 4 and rest[0] == 2:
        r, g, b = (min(max(v or 0, 0), 255) for v in rest[1:4])
        return (r, g, b), 4
    return None, 0
