"""配色に追従する色を組み立てる。

決め打ちの色をウィジェットへ直接書くと、システムの配色が変わったときに
読めなくなる。実際 1.1.1 では SFTP パネルの接続先が、暗い配色で背景に
溶けて消えていた。

パレットの役割ごとの値が互いに整合している保証も無い。実測（Windows 11 /
Qt6 / windows11 スタイル / システムはダーク）:

    colorScheme      Dark
    Window           #1e1e1e
    AlternateBase    #ffffff   ← ダークなのに白
    Text             #ffffff
    PlaceholderText  #ffffff

背景に AlternateBase、文字色に Text を採ると白地に白になる。そのため
ここでは信用できる地の色（Window）だけを読み、明暗は自分で計算する。
"""
from PyQt6.QtGui import QColor, QPalette

# WCAG の本文基準。これを下回る組み合わせは作らない
MIN_CONTRAST = 4.5

# 帯を地から離す割合(%)。基準を満たす最初のものを使う
_BAND_SHIFTS = (12, 22, 32, 42, 52)


def relative_luminance(color: QColor) -> float:
    """sRGB の相対輝度（0=黒, 1=白）。"""
    def channel(value: int) -> float:
        v = value / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    return (0.2126 * channel(color.red())
            + 0.7152 * channel(color.green())
            + 0.0722 * channel(color.blue()))


def contrast(a: QColor, b: QColor) -> float:
    """2 色のコントラスト比（1.0〜21.0）。"""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def is_dark(color: QColor) -> bool:
    """暗い側の色か。配色が明暗どちらに寄っているかの判定に使う。"""
    return relative_luminance(color) < 0.5

def surface(widget) -> QColor:
    """そのウィジェットが乗っている地の色。

    Window だけを見る。他の役割は、スタイルによっては配色と食い違う。
    パレットの役割から取った色は常に有効なので、既定値は用意しない。
    """
    return widget.palette().color(QPalette.ColorRole.Window)


WHITE = QColor("#ffffff")
# 純黒にする。#101010 では中間輝度の地(#777777 付近)で 4.25:1 にしか
# ならず、白の 4.48:1 と合わせて基準に届く色が無くなる。
BLACK = QColor("#000000")


def readable_ink(background: QColor) -> QColor:
    """その背景の上で最も読める文字色。

    パレットからは採らない。白と黒の両方でコントラストを計算して、
    高いほうを採る。輝度 0.5 で切ると、中間輝度で不利な側を選ぶ
    （#808080 では白 3.9:1 に対して黒 4.8:1）。
    """
    # 写しを返す。定数そのものを渡すと、呼び出し側が書き換えたときに
    # モジュールの定数ごと壊れる。
    best = (WHITE if contrast(WHITE, background) >= contrast(BLACK, background)
            else BLACK)
    return QColor(best)


def blend(a: QColor, b: QColor, amount: int) -> QColor:
    """a を b の側へ amount%% 寄せた色。

    lighter()/darker() は HSV の明度を掛けるので、真っ黒は何倍しても
    黒のまま。地が #000000 のときに帯が出なくなる。ブレンドなら動く。
    """
    amount = max(0, min(100, amount))
    return QColor(
        (a.red() * (100 - amount) + b.red() * amount) // 100,
        (a.green() * (100 - amount) + b.green() * amount) // 100,
        (a.blue() * (100 - amount) + b.blue() * amount) // 100)


def band_colours(widget):
    """帯の (背景, 文字, 枠) を返す。

    地を文字色の側へ少しだけ寄せて帯にする。地が真っ黒でも真っ白でも
    必ず動く。文字色は帯そのものから決め直す。
    """
    base = surface(widget)
    ink = readable_ink(base)
    # 中間輝度の地は白からも黒からも遠い。少し寄せただけでは、帯の上に
    # 白を置いても黒を置いても基準に届かない（地 #888888 なら
    # 白 4.3:1 / 黒 4.4:1）。届くまで文字色の側へ寄せる。
    band = blend(base, ink, _BAND_SHIFTS[-1])
    for amount in _BAND_SHIFTS:
        candidate = blend(base, ink, amount)
        if contrast(candidate, readable_ink(candidate)) >= MIN_CONTRAST:
            band = candidate
            break
    edge = blend(band, ink, 28)
    return band, readable_ink(band), edge


def band_style(widget, bold: bool = False, padding: str = "6px",
               font_size: str = "") -> str:
    """目立たせたい帯のスタイル。背景と文字色は必ず対で決める。"""
    band, ink, edge = band_colours(widget)
    return "".join([
        "background-color: %s; color: %s; border: 1px solid %s;"
        % (band.name(), ink.name(), edge.name()),
        " padding: %s;" % padding,
        " font-size: %s;" % font_size if font_size else "",
        " font-weight: bold;" if bold else "",
    ])


def dim(background: QColor, amount: int = 30) -> QColor:
    """地の側へ寄せた、控えめだが読める文字色。

    amount は地へ寄せる割合(%)。寄せすぎると読めなくなるので、
    コントラストが基準を割ったら寄せるのをやめる。
    """
    ink = readable_ink(background)
    blended = blend(ink, background, max(0, min(60, amount)))
    return blended if contrast(blended, background) >= MIN_CONTRAST else ink


def dim_style(widget, font_size: str = "", padding: str = "") -> str:
    """背景を持たない、控えめな説明文のスタイル。"""
    return "".join([
        "color: %s;" % dim(surface(widget)).name(),
        " font-size: %s;" % font_size if font_size else "",
        " padding: %s;" % padding if padding else "",
    ])


def note_style(widget, font_size: str = "9pt") -> str:
    """使い方の案内など、囲って添える説明のスタイル。"""
    band, _ink, edge = band_colours(widget)
    return ("background-color: %s; color: %s; border: 1px solid %s;"
            " padding: 5px; font-size: %s;"
            % (band.name(), dim(band).name(), edge.name(), font_size))
