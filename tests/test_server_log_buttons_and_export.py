"""FTP / TFTP / SFTP のログ操作ボタン（G1 の後半）の回帰テスト。

何が起きていたか（実測、基準 1a206c2）: 3 つのサーバーパネルは
アクティビティログの「下」に「ログをクリア」だけを置いていた
（setMaximumWidth(120)）。エクスポートは無く、画面に出ている受信記録を
ファイルへ残す手段が無かった。ボタンの置き場所も名前も、SNMP の Trap 受信
（一覧のすぐ上に左寄せ 1 行で `…[エクスポート][クリア]`）と違っていた。

利用者の決定（2026-09-23）:
  - どの画面も、一覧／ログのすぐ上に左寄せ 1 行でボタンを並べる。
  - FTP / TFTP / SFTP に「エクスポート」を足し、表示中のログをファイルへ
    保存できるようにする（保存の作法は syslog_panel の書き出しに合わせる）。
  - ボタン名は他と同じ「エクスポート」「クリア」。

どう直したか: 保存の作法（一時ファイルへ書き切ってから os.replace、端末の
ログ記録に使われているファイルは断る）を ui/log_export.py にまとめ、3 つの
パネルから同じように呼ぶ。ボタンはログのすぐ上へ移し、行末の addStretch()
で左寄せにした。
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

LOG_LINES = "[10:00:00] 192.0.2.10 RRQ startup-config\n[10:00:01] 転送完了"
# ボタンが自然な幅から広がっていないとみなす余裕（px）
STRETCH_TOLERANCE = 2
# 「左寄せ」とみなす、ログの左端からの距離（px）
LEFT_MARGIN = 40
# 隣り合うボタンの間に許す空き（レイアウト既定の間隔は 6px）
MAX_BUTTON_GAP = 12


class ServerLogButtonsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _config(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-srvlog-")
        return ConfigManager(config_path=os.path.join(d, "config.json"))

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

    def _left(self, panel, widget):
        return widget.mapTo(panel, widget.rect().topLeft()).x()

    def _top(self, panel, widget):
        return widget.mapTo(panel, widget.rect().topLeft()).y()

    def test_export_and_clear_sit_above_the_log(self):
        for name, panel in self._panels():
            for width in (520, 1100):
                with self.subTest(panel=name, width=width):
                    panel.resize(width, 900)
                    panel.show()
                    self.app.processEvents()
                    buttons = [panel.export_log_btn, panel.clear_log_btn]
                    tops = [self._top(panel, b) for b in buttons]
                    bottoms = [t + b.height() for t, b in zip(tops, buttons)]
                    self.assertLess(
                        max(tops), min(bottoms),
                        "%s(幅%d): ボタンが 1 行に並んでいません" % (name, width))
                    self.assertLessEqual(
                        max(bottoms), self._top(panel, panel.log_text),
                        "%s(幅%d): ボタン行がログより上にありません "
                        "(行の下端=%d ログの上端=%d)"
                        % (name, width, max(bottoms),
                           self._top(panel, panel.log_text)))
                    self.assertLessEqual(
                        min(self._left(panel, b) for b in buttons)
                        - self._left(panel, panel.log_text), LEFT_MARGIN,
                        "%s(幅%d): ボタン行が左寄せになっていません"
                        % (name, width))
                    gap = (self._left(panel, buttons[1])
                           - buttons[0].mapTo(
                               panel, buttons[0].rect().bottomRight()).x())
                    self.assertLessEqual(
                        gap, MAX_BUTTON_GAP,
                        "%s(幅%d): ボタンの間が %dpx 空いています"
                        % (name, width, gap))
                    for button in buttons:
                        self.assertLessEqual(
                            button.width(),
                            button.sizeHint().width() + STRETCH_TOLERANCE,
                            "%s(幅%d): 「%s」が自然な幅より広がっています "
                            "(width=%d sizeHint=%d)"
                            % (name, width, button.text(), button.width(),
                               button.sizeHint().width()))
                        right = button.mapTo(
                            panel, button.rect().bottomRight()).x()
                        self.assertLessEqual(
                            right, panel.width(),
                            "%s(幅%d): 「%s」が右へはみ出しています"
                            % (name, width, button.text()))
            panel.hide()

    def test_button_labels_match_the_other_screens(self):
        for name, panel in self._panels():
            with self.subTest(panel=name):
                self.assertEqual(panel.export_log_btn.text(), "エクスポート",
                                 "%s のエクスポートの表記が他と違います" % name)
                self.assertEqual(panel.clear_log_btn.text(), "クリア",
                                 "%s のクリアの表記が他と違います" % name)

    def test_export_writes_the_displayed_log(self):
        for name, panel in self._panels():
            with self.subTest(panel=name):
                panel.log_text.setPlainText(LOG_LINES)
                out = os.path.join(tempfile.mkdtemp(prefix="netbelt-srvout-"),
                                   "activity.log")
                with mock.patch("ui.log_export.QFileDialog.getSaveFileName",
                                return_value=(out, "")), \
                        mock.patch("ui.log_export.QMessageBox"):
                    panel.export_log_btn.click()
                with io.open(out, encoding="utf-8") as f:
                    saved = f.read()
                self.assertIn("192.0.2.10 RRQ startup-config", saved,
                              "%s: 表示中のログが保存されていません" % name)
                self.assertIn("転送完了", saved,
                              "%s: 保存されたログが途中で切れています" % name)

    def test_export_refuses_a_file_used_for_device_recording(self):
        from core import log_recording
        for name, panel in self._panels():
            with self.subTest(panel=name):
                recorded = os.path.join(
                    tempfile.mkdtemp(prefix="netbelt-srvrec-"), "rtrA.log")
                with io.open(recorded, "w", encoding="utf-8") as f:
                    f.write("記録済みの内容")
                log_recording.start("rtrA", recorded)
                self.addCleanup(log_recording.stop, "rtrA")
                panel.log_text.setPlainText(LOG_LINES)
                with mock.patch("ui.log_export.QFileDialog.getSaveFileName",
                                return_value=(recorded, "")), \
                        mock.patch("ui.log_export.QMessageBox") as box:
                    panel.export_log_btn.click()
                with io.open(recorded, encoding="utf-8") as f:
                    self.assertEqual(
                        f.read(), "記録済みの内容",
                        "%s: 記録中のファイルが上書きされました" % name)
                self.assertTrue(box.warning.called,
                                "%s: 記録中だと知らせていません" % name)
                log_recording.stop("rtrA")

    def test_clear_button_empties_the_log(self):
        for name, panel in self._panels():
            with self.subTest(panel=name):
                panel.log_text.setPlainText(LOG_LINES)
                panel.clear_log_btn.click()
                self.assertEqual(panel.log_text.toPlainText(), "",
                                 "%s: クリアでログが消えていません" % name)


if __name__ == "__main__":
    unittest.main()
