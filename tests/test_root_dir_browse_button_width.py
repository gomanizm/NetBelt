"""ルートディレクトリ行の「参照」ボタンが潰れないこと（G3）の回帰テスト。

何が起きていたか（実測、基準 1a206c2）: TFTP / FTP / SFTP の
「ルートディレクトリ:」行は QHBoxLayout に入力欄とボタンを並べ、ボタンだけ
setMaximumWidth(60) で頭を押さえていた。ボタンの自然な幅（sizeHint）は 80px
なので、どの画面幅でも 60px まで詰められたまま画面の右端に貼り付く。
窓を縮めるほど押しにくくなる、という指摘のとおりの状態だった。

利用者の決定（2026-09-23）: 伸びるのは入力欄だけにして、ボタンは自分の幅を
保つ。レイアウトの伸縮の指定で行い、setMaximumWidth には頼らない。
ボタンと欄の間の余白も詰めすぎない。

どう直したか: setMaximumWidth(60) を外し、入力欄に伸縮係数 1、ボタンに 0 を
与えた。余った幅は入力欄だけが受け取り、ボタンは sizeHint の幅を保つ。
欄とボタンの間隔はレイアウト既定の 6px のまま触っていない。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

# 欄とボタンがくっついて見えない最低限の間隔（レイアウト既定は 6px）
MIN_GAP = 4


class RootDirBrowseButtonWidthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _config(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-browsewidth-")
        return ConfigManager(os.path.join(d, "config.json"))

    def _panels(self):
        from ui.ftp_server_panel import FTPServerPanel
        from ui.sftp_server_panel import SFTPServerPanel
        from ui.tftp_server_panel import TFTPServerPanel
        panels = [("FTP", FTPServerPanel(config_manager=self._config())),
                  ("TFTP", TFTPServerPanel(config_manager=self._config())),
                  ("SFTP", SFTPServerPanel())]
        for _, panel in panels:
            self.addCleanup(panel.close)
        return panels

    def _lay_out(self, panel, width):
        panel.resize(width, 900)
        panel.show()
        self.app.processEvents()

    def test_browse_button_keeps_its_natural_width(self):
        for name, panel in self._panels():
            for width in (520, 1100):
                with self.subTest(panel=name, width=width):
                    self._lay_out(panel, width)
                    button = panel.browse_btn
                    self.assertGreaterEqual(
                        button.width(), button.sizeHint().width(),
                        "%s: 幅 %d で参照ボタンが自然な幅より狭く潰れています "
                        "(width=%d sizeHint=%d)"
                        % (name, width, button.width(),
                           button.sizeHint().width()))
            panel.hide()

    def test_only_the_line_edit_grows(self):
        for name, panel in self._panels():
            with self.subTest(panel=name):
                self._lay_out(panel, 520)
                narrow_edit = panel.root_dir_edit.width()
                narrow_button = panel.browse_btn.width()
                self._lay_out(panel, 1100)
                wide_edit = panel.root_dir_edit.width()
                wide_button = panel.browse_btn.width()
                self.assertGreater(
                    wide_edit, narrow_edit,
                    "%s: 画面を広げても入力欄が伸びていません" % name)
                self.assertEqual(
                    wide_button, narrow_button,
                    "%s: 画面幅で参照ボタンの幅が変わっています (%d -> %d)"
                    % (name, narrow_button, wide_button))
                panel.hide()

    def test_gap_between_field_and_button_is_kept(self):
        for name, panel in self._panels():
            with self.subTest(panel=name):
                self._lay_out(panel, 520)
                edit = panel.root_dir_edit
                button = panel.browse_btn
                edit_right = edit.mapTo(panel, edit.rect().bottomRight()).x()
                button_left = button.mapTo(panel, button.rect().topLeft()).x()
                self.assertGreaterEqual(
                    button_left - edit_right, MIN_GAP,
                    "%s: 欄と参照ボタンの間が詰まりすぎです (%dpx)"
                    % (name, button_left - edit_right))
                panel.hide()


if __name__ == "__main__":
    unittest.main()
