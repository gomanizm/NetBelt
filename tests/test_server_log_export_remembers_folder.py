"""サーバーのログのエクスポートも、保存先のフォルダを覚えることを検証する。

何が起きていたか（実測、基準 63c27f5）: 保存先を覚える仕組み
（core/save_defaults.py、settings.paths.last_save_dir）を作った時点では
FTP / TFTP / SFTP のログのエクスポートがまだ無く、後から足された
ui/log_export.py には配線されていない。そのため 3 つのサーバーパネルの
「エクスポート」だけが、他の保存と違ってダイアログの現在地から始まり、
保存しても次の初期値に反映されなかった。

  file_path, _ = QFileDialog.getSaveFileName(
      panel, title, default_file_name(stem), FILE_FILTER)

どう直したか: 3 つのパネルが同じ入口（log_export.export_log_text）を
使っているので、そこで save_defaults.initial_path / remember を呼ぶ。
覚える場所は端末・SNMP・Syslog と同じ 1 つで、どの画面で保存しても
次はそこから始まる。SFTP サーバーパネルだけ config_manager を受け取って
いなかったので、他の 2 つと同じ形で受け取れるようにした。
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

LOG_LINES = "[10:00:00] 192.0.2.10 RRQ startup-config\n[10:00:01] 転送完了"


class ServerLogExportRemembersFolderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.saved_dir = tempfile.mkdtemp(prefix="netbelt-srvmem-saved-")
        self.other_dir = tempfile.mkdtemp(prefix="netbelt-srvmem-other-")
        self.addCleanup(shutil.rmtree, self.saved_dir, True)
        self.addCleanup(shutil.rmtree, self.other_dir, True)

    def _config(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-srvmem-conf-")
        self.addCleanup(shutil.rmtree, d, True)
        return ConfigManager(config_path=os.path.join(d, "config.json"))

    def _panels(self):
        """3 つのサーバーパネルを、同じ形で config_manager を渡して作る"""
        from ui.ftp_server_panel import FTPServerPanel
        from ui.sftp_server_panel import SFTPServerPanel
        from ui.tftp_server_panel import TFTPServerPanel
        panels = []
        for name, factory in (("FTP", FTPServerPanel),
                              ("TFTP", TFTPServerPanel),
                              ("SFTP", SFTPServerPanel)):
            cm = self._config()
            panel = factory(config_manager=cm)
            self.addCleanup(panel.close)
            panels.append((name, panel, cm))
        return panels

    def _export(self, panel, chosen_path):
        """エクスポートを 1 回動かし、ダイアログへ渡された初期値を返す"""
        seen = {}

        def fake(parent, title, initial, filters="", *args, **kwargs):
            seen["initial"] = initial
            return chosen_path, ""

        panel.log_text.setPlainText(LOG_LINES)
        with mock.patch("ui.log_export.QFileDialog.getSaveFileName",
                        side_effect=fake), \
                mock.patch("ui.log_export.QMessageBox"):
            panel.export_log_btn.click()
        return seen.get("initial")

    def test_the_dialog_starts_from_the_remembered_folder(self):
        for name, panel, cm in self._panels():
            with self.subTest(panel=name):
                cm.set_last_save_dir(self.saved_dir)
                initial = self._export(
                    panel, os.path.join(self.other_dir, "activity.log"))
                self.assertEqual(
                    os.path.dirname(initial), self.saved_dir,
                    "%s: 覚えたフォルダから始まっていません: %r" % (name, initial))
                self.assertTrue(
                    os.path.basename(initial).endswith(".txt"),
                    "%s: 既定のファイル名が付いていません: %r" % (name, initial))

    def test_a_successful_export_remembers_its_folder(self):
        for name, panel, cm in self._panels():
            with self.subTest(panel=name):
                target = os.path.join(self.other_dir, "activity.log")
                self._export(panel, target)
                with io.open(target, encoding="utf-8") as f:
                    self.assertIn("転送完了", f.read(),
                                  "%s: 保存されていません" % name)
                self.assertEqual(
                    cm.get_last_save_dir(), self.other_dir,
                    "%s: 保存したフォルダを覚えていません" % name)

    def test_cancelling_does_not_change_the_remembered_folder(self):
        for name, panel, cm in self._panels():
            with self.subTest(panel=name):
                cm.set_last_save_dir(self.saved_dir)
                self._export(panel, "")
                self.assertEqual(
                    cm.get_last_save_dir(), self.saved_dir,
                    "%s: 取り消したのに覚えた場所が変わりました" % name)

    def test_a_refused_export_does_not_remember_its_folder(self):
        """端末のログ記録中のファイルを選ばれたら、覚えないこと。"""
        from core import log_recording
        for name, panel, cm in self._panels():
            with self.subTest(panel=name):
                recorded = os.path.join(self.other_dir, "rtrA.log")
                with io.open(recorded, "w", encoding="utf-8") as f:
                    f.write("記録済みの内容")
                log_recording.start("rtrA", recorded)
                self.addCleanup(log_recording.stop, "rtrA")
                self._export(panel, recorded)
                log_recording.stop("rtrA")
                self.assertIsNone(
                    cm.get_last_save_dir(),
                    "%s: 断った保存先を覚えています" % name)

    def test_the_folder_is_shared_with_the_other_saves(self):
        """他の画面の保存と同じ 1 つの場所を使うこと。"""
        from core import save_defaults
        for name, panel, cm in self._panels():
            with self.subTest(panel=name):
                self._export(panel,
                             os.path.join(self.other_dir, "activity.log"))
                self.assertEqual(save_defaults.last_dir(cm), self.other_dir,
                                 "%s: 共通の置き場所に入っていません" % name)


if __name__ == "__main__":
    unittest.main()
