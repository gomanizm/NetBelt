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

_FALLBACK_SURFACE = "#f0f0f0"


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
    return relative_luminance(color) < 0.5


def surface(widget) -> QColor:
    """そのウィジェットが乗っている地の色。

    Window だけを見る。他の役割は、スタイルによっては配色と食い違う。
    """
    color = widget.palette().color(QPalette.ColorRole.Window)
    return color if color.isValid() else QColor(_FALLBACK_SURFACE)


def readable_ink(background: QColor) -> QColor:
    """その背景の上で確実に読める文字色。

    パレットからは採らない。背景の明暗だけで決める。
    """
    return QColor("#ffffff") if is_dark(background) else QColor("#101010")


def band_colours(widget):
    """帯の (背景, 文字, 枠) を返す。地から少し離して帯だと分かるようにする。"""
    base = surface(widget)
    dark = is_dark(base)
    band = base.lighter(160) if dark else base.darker(108)
    edge = band.lighter(150) if dark else band.darker(118)
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
    amount = max(0, min(60, amount))
    blended = QColor(
        (ink.red() * (100 - amount) + background.red() * amount) // 100,
        (ink.green() * (100 - amount) + background.green() * amount) // 100,
        (ink.blue() * (100 - amount) + background.blue() * amount) // 100)
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
