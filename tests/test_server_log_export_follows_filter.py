"""FTP / TFTP / SFTP のログエクスポートも選んだ拡張子に従うことの回帰テスト。

何が起きていたか（実測、基準 f4cad23）: ui/log_export.py:77 は保存ダイアログの
selectedFilter を `file_path, _ =` で捨て、:98 で返されたパスへそのまま保存
していた。既定名を編集せず種類だけ「ログファイル (*.log)」へ変えると、

    ダイアログが返したパス : tftp_log_20260923_103604.txt
    選んだ種類             : ログファイル (*.log)
    実際に出来たファイル   : ['tftp_log_20260923_103604.txt']
    共通処理を通した場合   : tftp_log_20260923_103604.log

となり、.txt のまま保存された。SNMP（snmp_panel.py:815, 1193）、
Syslog（syslog_panel.py:786, 915）、端末（terminal_widget.py:1951, 2027）の
6 箇所は save_defaults.apply_filter_suffix_confirmed を通しており、
2026-09-23 の利用者の決定「csv や log といった拡張子で保存したい」
（tests/test_export_extension_follows_filter.py 冒頭）から、3 つの
サーバーパネルだけが取りこぼされていた。中身は素のテキストなので壊れるのは
ファイル名だけ。

どう直したか: log_export.export_log_text で selectedFilter を受け取り、
記録中ファイルの確認より前に apply_filter_suffix_confirmed を通す。
既定名は先に 1 回だけ作って使い回す（2 回作ると秒がずれ、名前を編集して
いないのに「編集した」と判定されて付け替えが効かない）。
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

LOG_LINES = "[10:00:00] [192.0.2.10] RRQ startup-config\n[10:00:01] 転送完了"
LOG_FILTER = "ログファイル (*.log)"
TXT_FILTER = "テキストファイル (*.txt)"


class ServerLogExportFilterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _config(self):
        from core.config_manager import ConfigManager
        directory = tempfile.mkdtemp(prefix="netbelt-exportfilter-")
        return ConfigManager(config_path=os.path.join(directory,
                                                      "config.json"))

    def _panels(self):
        from ui.ftp_server_panel import FTPServerPanel
        from ui.sftp_server_panel import SFTPServerPanel
        from ui.tftp_server_panel import TFTPServerPanel
        panels = [("FTP", FTPServerPanel(config_manager=self._config())),
                  ("TFTP", TFTPServerPanel(config_manager=self._config())),
                  ("SFTP", SFTPServerPanel(config_manager=self._config()))]
        for _, panel in panels:
            self.addCleanup(panel.close)
            panel.log_text.setPlainText(LOG_LINES)
        return panels

    def test_the_default_name_follows_the_selected_type(self):
        from ui import log_export
        for name, panel in self._panels():
            with self.subTest(panel=name):
                out_dir = tempfile.mkdtemp(prefix="netbelt-exportout-")
                # 既定名は export_log_text が作る。同じ名前を返して
                # 「名前は編集せず種類だけ変えた」を再現する
                chosen = {}

                def fake_dialog(parent, title, initial, filters):
                    chosen["initial"] = initial
                    return (os.path.join(out_dir,
                                         os.path.basename(initial)),
                            LOG_FILTER)

                with mock.patch("ui.log_export.QFileDialog.getSaveFileName",
                                side_effect=fake_dialog), \
                        mock.patch("ui.log_export.QMessageBox"):
                    panel._on_export_log()

                written = os.listdir(out_dir)
                self.assertEqual(
                    len(written), 1,
                    "%s: 保存されたファイルが 1 つではありません: %r"
                    % (name, written))
                self.assertTrue(
                    written[0].endswith(".log"),
                    "%s: 選んだ種類に従っていません: %r (既定名 %r)"
                    % (name, written[0], os.path.basename(chosen["initial"])))
                with io.open(os.path.join(out_dir, written[0]),
                             encoding="utf-8") as handle:
                    self.assertIn("RRQ startup-config", handle.read())

    def test_a_name_typed_by_the_user_is_respected(self):
        for name, panel in self._panels():
            with self.subTest(panel=name):
                out_dir = tempfile.mkdtemp(prefix="netbelt-exportout2-")
                typed = os.path.join(out_dir, "activity.txt")
                with mock.patch("ui.log_export.QFileDialog.getSaveFileName",
                                return_value=(typed, LOG_FILTER)), \
                        mock.patch("ui.log_export.QMessageBox"):
                    panel._on_export_log()
                self.assertEqual(
                    os.listdir(out_dir), ["activity.txt"],
                    "%s: 利用者が書いた名前を勝手に変えています" % name)

    def test_the_default_name_is_built_only_once(self):
        """既定名を 2 回作ると秒がずれ、付け替えの判定が外れる"""
        from ui import log_export
        _, panel = self._panels()[1]
        out_dir = tempfile.mkdtemp(prefix="netbelt-exportout3-")
        real = log_export.default_file_name
        calls = []

        def counting(stem):
            calls.append(stem)
            return real(stem)

        def fake_dialog(parent, title, initial, filters):
            return (os.path.join(out_dir, os.path.basename(initial)),
                    LOG_FILTER)

        with mock.patch.object(log_export, "default_file_name",
                               side_effect=counting), \
                mock.patch("ui.log_export.QFileDialog.getSaveFileName",
                           side_effect=fake_dialog), \
                mock.patch("ui.log_export.QMessageBox"):
            panel._on_export_log()

        self.assertEqual(len(calls), 1,
                         "既定名を %d 回作っています" % len(calls))

    def test_refusing_the_overwrite_saves_nothing(self):
        from PyQt6.QtWidgets import QMessageBox
        from ui import log_export
        _, panel = self._panels()[1]
        out_dir = tempfile.mkdtemp(prefix="netbelt-exportout4-")
        existing = None

        def fake_dialog(parent, title, initial, filters):
            nonlocal existing
            stem = os.path.splitext(os.path.basename(initial))[0]
            existing = os.path.join(out_dir, stem + ".log")
            with io.open(existing, "w", encoding="utf-8") as handle:
                handle.write("先にあった内容")
            return (os.path.join(out_dir, os.path.basename(initial)),
                    LOG_FILTER)

        with mock.patch("ui.log_export.QFileDialog.getSaveFileName",
                        side_effect=fake_dialog), \
                mock.patch("ui.log_export.QMessageBox"), \
                mock.patch("core.save_defaults.QMessageBox.question",
                           return_value=QMessageBox.StandardButton.No):
            saved = log_export.export_log_text(panel, LOG_LINES, "tftp_log")

        self.assertIsNone(saved, "断られたのに保存しています")
        with io.open(existing, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "先にあった内容",
                             "断られたのに既存のファイルを置き換えています")

    def test_an_empty_filter_still_saves_as_given(self):
        for name, panel in self._panels():
            with self.subTest(panel=name):
                out_dir = tempfile.mkdtemp(prefix="netbelt-exportout5-")
                out = os.path.join(out_dir, "activity.log")
                with mock.patch("ui.log_export.QFileDialog.getSaveFileName",
                                return_value=(out, "")), \
                        mock.patch("ui.log_export.QMessageBox"):
                    panel._on_export_log()
                self.assertEqual(os.listdir(out_dir), ["activity.log"],
                                 "%s: 種類を選ばない環境で保存先が変わりました"
                                 % name)


if __name__ == "__main__":
    unittest.main()
