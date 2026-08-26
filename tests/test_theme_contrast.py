"""画面に出す文字が、どの配色でも読めることを検証する。

1.1.1 で足した SFTP の接続先表示は、暗い配色で背景に溶けて消えた。
一度目の修正はパレットの AlternateBase を背景に、Text を文字色に採った
が、それでも消えたままだった。報告者のマシンで測ると理由が出た:

    colorScheme      Dark
    Window           #1e1e1e
    AlternateBase    #ffffff   ← ダークなのに白
    Text             #ffffff
    PlaceholderText  #ffffff

役割ごとの値が互いに整合している保証は無い。WINDOWS11_DARK はその実測値
そのもので、一度目の修正はこれで落ちる。自分で書いた「まともな」ダーク
パレットで検証したから、壊れたまま通ってしまった。
"""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

# WCAG の本文基準
MIN = 4.5

# Windows 11 / Qt6 / windows11 スタイル / システムはダーク の実測値
WINDOWS11_DARK = {
    "Window": "#1e1e1e", "WindowText": "#ffffff",
    "Base": "#2d2d2d", "AlternateBase": "#ffffff",
    "Text": "#ffffff", "Button": "#3c3c3c",
    "ButtonText": "#ffffff", "Mid": "#282828",
    "PlaceholderText": "#ffffff",
}

PLAIN_LIGHT = {
    "Window": "#f0f0f0", "WindowText": "#000000",
    "Base": "#ffffff", "AlternateBase": "#f7f7f7",
    "Text": "#000000", "Button": "#e1e1e1",
    "ButtonText": "#000000", "Mid": "#a0a0a0",
    "PlaceholderText": "#7f7f7f",
}

THEMES = (("ダーク(実測)", WINDOWS11_DARK), ("ライト", PLAIN_LIGHT))


def luminance(name):
    r, g, b = (int(name[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def channel(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def colours_of(style, fallback_background):
    """スタイルから (背景, 文字) を取り出す。背景の指定が無ければ地を使う。"""
    bg = re.search(r"background-color:\s*(#[0-9a-fA-F]{6})", style)
    fg = re.search(r"(?<!-)\bcolor:\s*(#[0-9a-fA-F]{6})", style)
    return (bg.group(1) if bg else fallback_background,
            fg.group(1) if fg else None)


class ThemeContrastTest(unittest.TestCase):
    """theme.py が返す組み合わせが、必ず読めること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self, colours):
        from PyQt6.QtGui import QColor, QPalette
        from PyQt6.QtWidgets import QWidget

        palette = QPalette()
        for role, value in colours.items():
            palette.setColor(getattr(QPalette.ColorRole, role), QColor(value))
        widget = QWidget()
        widget.setPalette(palette)
        return widget

    def test_the_band_is_readable_in_every_theme(self):
        from ui import theme
        for name, colours in THEMES:
            with self.subTest(theme=name):
                bg, fg = colours_of(theme.band_style(self._widget(colours)),
                                    colours["Window"])
                self.assertIsNotNone(fg, "文字色が無い")
                ratio = contrast(bg, fg)
                self.assertGreaterEqual(
                    ratio, MIN,
                    "帯が読めない（%s / %s = %.1f:1）" % (fg, bg, ratio))

    def test_the_note_is_readable_in_every_theme(self):
        from ui import theme
        for name, colours in THEMES:
            with self.subTest(theme=name):
                bg, fg = colours_of(theme.note_style(self._widget(colours)),
                                    colours["Window"])
                ratio = contrast(bg, fg)
                self.assertGreaterEqual(
                    ratio, MIN,
                    "説明が読めない（%s / %s = %.1f:1）" % (fg, bg, ratio))

    def test_the_dim_text_is_readable_in_every_theme(self):
        """控えめでも読めること。地に寄せすぎない。"""
        from ui import theme
        for name, colours in THEMES:
            with self.subTest(theme=name):
                bg, fg = colours_of(theme.dim_style(self._widget(colours)),
                                    colours["Window"])
                ratio = contrast(bg, fg)
                self.assertGreaterEqual(
                    ratio, MIN,
                    "説明文が読めない（%s / %s = %.1f:1）" % (fg, bg, ratio))

    def test_dim_text_is_actually_dimmer_than_body_text(self):
        """控えめにする意味があること。本文と同じでは意味がない。"""
        from PyQt6.QtGui import QColor
        from ui import theme
        for name, colours in THEMES:
            with self.subTest(theme=name):
                base = QColor(colours["Window"])
                self.assertNotEqual(theme.dim(base).name(),
                                    theme.readable_ink(base).name())

    def test_the_band_stands_out_from_the_surface(self):
        """帯だと分かること。"""
        from ui import theme
        for name, colours in THEMES:
            with self.subTest(theme=name):
                band, _ink, _edge = theme.band_colours(self._widget(colours))
                self.assertNotEqual(band.name().lower(),
                                    colours["Window"].lower())

    def test_the_band_matches_the_theme(self):
        """暗い画面には暗い帯、明るい画面には明るい帯を返すこと。

        読めるかどうかだけを見ていると、暗い画面に真っ白な帯が出ても
        気づけない（文字が黒くなるので contrast は足りてしまう）。
        報告された見え方は、まさにその白いチップだった。
        """
        from PyQt6.QtGui import QColor
        from ui import theme
        for name, colours in THEMES:
            with self.subTest(theme=name):
                band, _ink, _edge = theme.band_colours(self._widget(colours))
                self.assertEqual(
                    theme.is_dark(band),
                    theme.is_dark(QColor(colours["Window"])),
                    "地(%s)と帯(%s)で明暗が食い違う"
                    % (colours["Window"], band.name()))

    def test_dimming_never_goes_below_the_floor(self):
        """地へ寄せすぎたら、寄せるのをやめること。

        既定の寄せ幅では下限に当たらないので、限界まで寄せて確かめる。
        """
        from PyQt6.QtGui import QColor
        from ui import theme
        for name, colours in THEMES:
            for amount in (30, 45, 60, 100):
                with self.subTest(theme=name, amount=amount):
                    base = QColor(colours["Window"])
                    ratio = theme.contrast(theme.dim(base, amount), base)
                    self.assertGreaterEqual(
                        ratio, MIN,
                        "%d%% 寄せたら読めなくなった（%.1f:1）"
                        % (amount, ratio))

    def test_every_shade_of_surface_stays_readable(self):
        """地が真っ黒から真っ白まで、どこでも読めること。

        2 つのテーマを試すだけでは端に届かない。#000000 では明度の
        掛け算が効かず帯が出ず、#808080 付近では白より黒のほうが
        読みやすいのに白が選ばれていた。
        """
        from ui import theme
        for level in range(0, 256, 17):
            shade = "#%02x%02x%02x" % (level, level, level)
            with self.subTest(surface=shade):
                colours = dict(PLAIN_LIGHT)
                colours["Window"] = shade
                widget = self._widget(colours)

                bg, fg = colours_of(theme.band_style(widget), shade)
                ratio = contrast(bg, fg)
                self.assertGreaterEqual(
                    ratio, MIN,
                    "帯が読めない（地 %s / %s on %s = %.1f:1）"
                    % (shade, fg, bg, ratio))

                _bg, dim_fg = colours_of(theme.dim_style(widget), shade)
                dim_ratio = contrast(shade, dim_fg)
                self.assertGreaterEqual(
                    dim_ratio, MIN,
                    "説明文が読めない（地 %s / %s = %.1f:1）"
                    % (shade, dim_fg, dim_ratio))

    def test_a_band_appears_even_on_pure_black_or_white(self):
        """真っ黒・真っ白の地でも、帯が地と別の色になること。

        QColor.lighter() は HSV の明度を掛けるので、#000000 は
        何倍しても黒のまま。帯が消える。
        """
        from ui import theme
        for shade in ("#000000", "#ffffff"):
            with self.subTest(surface=shade):
                colours = dict(PLAIN_LIGHT)
                colours["Window"] = shade
                band, _ink, _edge = theme.band_colours(self._widget(colours))
                self.assertNotEqual(
                    band.name().lower(), shade,
                    "地 %s で帯が出ない" % shade)

    def test_the_ink_is_the_better_of_black_and_white(self):
        """白と黒のうち、コントラストが高いほうを選ぶこと。"""
        from PyQt6.QtGui import QColor
        from ui import theme
        for level in range(0, 256, 17):
            shade = "#%02x%02x%02x" % (level, level, level)
            with self.subTest(background=shade):
                background = QColor(shade)
                ink = theme.readable_ink(background)
                other = (theme.BLACK if ink.name() == theme.WHITE.name()
                         else theme.WHITE)
                self.assertGreaterEqual(
                    theme.contrast(ink, background),
                    theme.contrast(other, background),
                    "%s の上で不利なほうを選んでいる" % shade)

    def test_a_missing_palette_does_not_produce_an_unreadable_pair(self):
        """パレットが空でも、読める組み合わせを返すこと。"""
        from PyQt6.QtWidgets import QWidget
        from ui import theme
        style = theme.band_style(QWidget())
        bg, fg = colours_of(style, "#f0f0f0")
        self.assertGreaterEqual(contrast(bg, fg), MIN, style)


class PanelStyleTest(unittest.TestCase):
    """実際のパネルが、実測の配色で読めること。"""

    # 破棄済みウィジェットへのシグナル配送で落ちるため保持する
    _windows = []


    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _with_theme(self, colours):
        from PyQt6.QtGui import QColor, QPalette
        palette = QPalette()
        for role, value in colours.items():
            palette.setColor(getattr(QPalette.ColorRole, role), QColor(value))
        previous = self.app.palette()
        self.app.setPalette(palette)
        self.addCleanup(self.app.setPalette, previous)

    def _assert_readable(self, style, background, what):
        bg, fg = colours_of(style, background)
        self.assertIsNotNone(fg, "%s に文字色が無い: %r" % (what, style))
        ratio = contrast(bg, fg)
        self.assertGreaterEqual(
            ratio, MIN,
            "%s が読めない（%s / %s = %.1f:1）" % (what, fg, bg, ratio))

    def test_the_sftp_client_panel_is_readable(self):
        for name, colours in THEMES:
            with self.subTest(theme=name):
                self._with_theme(colours)
                from ui.sftp_panel import SFTPPanel
                panel = SFTPPanel()
                for what, label in (("接続先", panel.target_label),
                                    ("パス", panel.path_label),
                                    ("案内", panel.hint_label)):
                    self._assert_readable(label.styleSheet(),
                                          colours["Window"], what)

    def test_the_server_panels_notes_are_readable(self):
        """FTP / SFTP / TFTP の説明ラベル。

        以前は #f9f9f9 の上に #666 の決め打ちで、読めはするものの
        暗い配色では白いチップが浮いていた。
        """
        panels = (("FTP", "ui.ftp_server_panel", "FTPServerPanel"),
                  ("SFTPサーバ", "ui.sftp_server_panel", "SFTPServerPanel"),
                  ("TFTP", "ui.tftp_server_panel", "TFTPServerPanel"))
        import importlib
        for name, colours in THEMES:
            self._with_theme(colours)
            for what, module_name, class_name in panels:
                with self.subTest(theme=name, panel=what):
                    module = importlib.import_module(module_name)
                    panel = getattr(module, class_name)()
                    style = self._find_note_style(panel)
                    self.assertIsNotNone(style, "%s の説明ラベルが無い" % what)
                    self._assert_readable(style, colours["Window"],
                                          "%s の説明" % what)

    def _find_note_style(self, panel):
        from PyQt6.QtWidgets import QLabel
        for label in panel.findChildren(QLabel):
            style = label.styleSheet()
            if "background-color" in style and "font-size" in style:
                return style
        return None

    def test_the_version_dialog_builds_and_is_readable(self):
        """バージョン情報が組み立てられ、文字が読めること。

        この画面を通るテストが無く、色の差し込みを書き損じても
        気づけなかった。開いた瞬間に落ちる類の間違いだった。
        """
        import re
        from ui.main_window import MainWindow

        for name, colours in THEMES:
            with self.subTest(theme=name):
                self._with_theme(colours)
                with mock.patch.object(MainWindow,
                                       "_check_for_updates_on_startup"):
                    window = MainWindow()
                type(self)._windows.append(window)
                with mock.patch("ui.main_window.QMessageBox") as box:
                    window._on_version_info()

                self.assertTrue(box.return_value.setText.called,
                                "本文を組み立てていない")
                text = box.return_value.setText.call_args[0][0]
                self.assertNotIn("{", text, "差し込みが残っている: %r" % text)
                found = re.findall(r"color:\s*(#[0-9a-fA-F]{6})", text)
                self.assertTrue(found, "色を指定していない")
                for colour in found:
                    ratio = contrast(colours["Window"], colour)
                    self.assertGreaterEqual(
                        ratio, MIN,
                        "バージョン情報が読めない（%s / %s = %.1f:1）"
                        % (colour, colours["Window"], ratio))

    def test_no_hard_coded_light_background_is_left(self):
        """地に依存する色を、決め打ちで残していないこと。

        ボタンのように前景と背景を対で決めているものは対象外。
        """
        import io
        offenders = []
        for root, _dirs, files in os.walk("src/ui"):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                for number, line in enumerate(
                        io.open(path, encoding="utf-8"), 1):
                    if "background-color: #f9f9f9" in line:
                        offenders.append("%s:%d" % (path, number))
        self.assertEqual(offenders, [],
                         "決め打ちの薄い背景が残っている: %s" % offenders)


if __name__ == "__main__":
    unittest.main()
