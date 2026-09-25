"""保存ダイアログが「前回保存した場所」から開くことを検証する。

実測（1a206c2）: 保存の入口はどれも既定のファイル名しか渡しておらず、
初期の場所はその場で決め打ちになっていた。

  - 端末の全ログ保存 / ログ記録開始 …… 常に cwd/logs
    （ui/terminal_widget.py の _default_log_dir()）
  - SNMP の結果 / Trap のエクスポート …… 名前だけ（"snmp_result_....txt"）を
    渡すので、ダイアログの現在地から始まる
  - Syslog のエクスポート / 選択行の保存 …… 同じく名前だけ

そのため、D:\\work\\logs のような決まった場所へ保存している利用者は、
保存のたびにフォルダを辿り直していた。

利用者の決定（2026-09-23）:
「ログファイルの保存場所を前回保存した場所を保存しておいてほしい。
毎回フォルダ指定するのが面倒だから」
「多分エクスポートだけの話じゃなくてログの記録開始とかもそう」

直し方: 保存が成功したときだけ、そのフォルダを config.json の
settings.paths.last_save_dir へ覚える（覚えるのはフォルダのパス 1 つだけで、
機器の設定や資格情報は書かない）。次に保存ダイアログを開くときは
「覚えたフォルダ + 既定のファイル名」を初期値にする。覚えたフォルダが
無くなっていたら、これまでどおりの初期値へ黙って戻す。
記録開始とエクスポートは同じ場所を共有する。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

SETTINGS_SECTION = "paths"
SETTINGS_KEY = "last_save_dir"


class _Incoming:
    """SyslogReceiver が渡してくる形に合わせた最小の受信メッセージ"""

    def __init__(self, message="link down"):
        self.timestamp = "2026-09-23 10:00:00"
        self.hostname = "rtr1"
        self.level = "Info"
        self.message = message
        self.raw_message = "<134>Sep 23 10:00:00 rtr1 " + message
        self.source_ip = "192.0.2.1"
        self.source_display = "192.0.2.1 (UDP/514)"


class SaveDirRememberedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作ったウィンドウはクラス終了まで保持する（test_snmp_export_extension_case と同じ）
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def setUp(self):
        self.saved_dir = tempfile.mkdtemp(prefix="netbelt-savedir-target-")
        self.other_dir = tempfile.mkdtemp(prefix="netbelt-savedir-other-")
        self.addCleanup(shutil.rmtree, self.saved_dir, True)
        self.addCleanup(shutil.rmtree, self.other_dir, True)
        # cwd/logs を作られてもリポジトリを汚さないよう、作業場所を移す
        self.cwd = tempfile.mkdtemp(prefix="netbelt-savedir-cwd-")
        self.addCleanup(shutil.rmtree, self.cwd, True)
        old_cwd = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, old_cwd)

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-savedir-conf-")
        self.addCleanup(shutil.rmtree, d, True)
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            window = MainWindow()
        type(self)._windows.append(window)
        self.cm = cm
        return window

    def _terminal(self, window):
        w = window.terminal_widget
        w.create_terminal_tab("rtrA")
        w.append_output("rtrA", "Router#show version\r\n")
        return w

    def _remembered(self):
        section = self.cm.config.get("settings", {}).get(SETTINGS_SECTION, {})
        return section.get(SETTINGS_KEY)

    @staticmethod
    def _initial_dir(dialog):
        """ダイアログへ渡した初期パスのフォルダ部分"""
        initial = dialog.call_args[0][2]
        return os.path.normcase(os.path.dirname(os.path.abspath(initial)))

    @staticmethod
    def _norm(path):
        return os.path.normcase(os.path.abspath(path))

    # --- 端末 ------------------------------------------------------------

    def test_full_log_save_starts_from_the_last_saved_folder(self):
        """全ログ保存: 2 回目は前回保存したフォルダから始まること。"""
        w = self._terminal(self._window())
        target = os.path.join(self.saved_dir, "out.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(target, "")), \
                mock.patch("ui.dialogs.log_save_dialog.LogSaveProgressDialog.exec",
                           return_value=True), \
                mock.patch("PyQt6.QtWidgets.QMessageBox.information"):
            w.save_current_log()

        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=("", "")) as dialog:
            w.save_current_log()
        self.assertEqual(self._initial_dir(dialog), self._norm(self.saved_dir),
                         "前回保存したフォルダから始まっていない")

    def test_recording_start_and_export_share_the_folder(self):
        """ログ記録開始で覚えた場所を、全ログ保存も使うこと。"""
        w = self._terminal(self._window())
        target = os.path.join(self.saved_dir, "rec.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(target, "")):
            w.start_log_recording()
        self.assertIn("rtrA", w._log_files, "記録が始まっていない")
        w.stop_log_recording("rtrA")

        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=("", "")) as dialog:
            w.save_current_log()
        self.assertEqual(self._initial_dir(dialog), self._norm(self.saved_dir),
                         "記録開始で覚えた場所が全ログ保存に効いていない")

    def test_cancelled_dialog_does_not_change_the_folder(self):
        """取り消したときは覚えている場所を変えないこと。"""
        w = self._terminal(self._window())
        target = os.path.join(self.saved_dir, "out.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(target, "")), \
                mock.patch("ui.dialogs.log_save_dialog.LogSaveProgressDialog.exec",
                           return_value=True), \
                mock.patch("PyQt6.QtWidgets.QMessageBox.information"):
            w.save_current_log()

        cancelled = os.path.join(self.other_dir, "never.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=("", "")):
            w.save_current_log()
        self.assertEqual(self._norm(self._remembered() or ""),
                         self._norm(self.saved_dir),
                         "取り消しで覚えている場所が変わった")
        self.assertNotIn(os.path.basename(cancelled), str(self._remembered()))

    def test_failed_save_does_not_change_the_folder(self):
        """保存に失敗したときは覚えている場所を変えないこと。"""
        w = self._terminal(self._window())
        target = os.path.join(self.saved_dir, "out.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(target, "")), \
                mock.patch("ui.dialogs.log_save_dialog.LogSaveProgressDialog.exec",
                           return_value=True), \
                mock.patch("PyQt6.QtWidgets.QMessageBox.information"):
            w.save_current_log()

        failed = os.path.join(self.other_dir, "fail.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(failed, "")), \
                mock.patch("ui.dialogs.log_save_dialog.LogSaveProgressDialog.exec",
                           return_value=False), \
                mock.patch("PyQt6.QtWidgets.QMessageBox.information"):
            w.save_current_log()
        self.assertEqual(self._norm(self._remembered() or ""),
                         self._norm(self.saved_dir),
                         "失敗した保存先を覚えてしまっている")

    def test_missing_folder_falls_back_to_the_old_default(self):
        """覚えたフォルダが消えていたら、これまでどおりの初期値へ戻ること。"""
        w = self._terminal(self._window())
        target = os.path.join(self.saved_dir, "out.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(target, "")), \
                mock.patch("ui.dialogs.log_save_dialog.LogSaveProgressDialog.exec",
                           return_value=True), \
                mock.patch("PyQt6.QtWidgets.QMessageBox.information"):
            w.save_current_log()
        shutil.rmtree(self.saved_dir, ignore_errors=True)

        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=("", "")) as dialog:
            w.save_current_log()
        self.assertEqual(dialog.call_count, 1, "ダイアログに到達していない")
        self.assertEqual(self._initial_dir(dialog),
                         self._norm(os.path.join(self.cwd, "logs")),
                         "消えたフォルダを指し続けている")

    def test_only_the_folder_is_written_to_the_config(self):
        """覚えるのはフォルダのパスだけ（ファイル名も機器の情報も書かない）。"""
        w = self._terminal(self._window())
        target = os.path.join(self.saved_dir, "rtrA_secret.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(target, "")), \
                mock.patch("ui.dialogs.log_save_dialog.LogSaveProgressDialog.exec",
                           return_value=True), \
                mock.patch("PyQt6.QtWidgets.QMessageBox.information"):
            w.save_current_log()
        remembered = self._remembered()
        self.assertEqual(self._norm(remembered or ""), self._norm(self.saved_dir))
        self.assertNotIn("rtrA_secret.log", str(remembered))

    def test_the_folder_survives_a_restart(self):
        """覚えた場所が config.json に残り、次の起動でも使われること。"""
        w = self._terminal(self._window())
        target = os.path.join(self.saved_dir, "out.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(target, "")), \
                mock.patch("ui.dialogs.log_save_dialog.LogSaveProgressDialog.exec",
                           return_value=True), \
                mock.patch("PyQt6.QtWidgets.QMessageBox.information"):
            w.save_current_log()

        from core.config_manager import ConfigManager
        reloaded = ConfigManager(config_path=self.cm.config_path)
        section = reloaded.config.get("settings", {}).get(SETTINGS_SECTION, {})
        self.assertEqual(self._norm(section.get(SETTINGS_KEY) or ""),
                         self._norm(self.saved_dir),
                         "config.json に残っていない")

    # --- SNMP ------------------------------------------------------------

    def test_snmp_result_export_remembers_the_folder(self):
        """SNMP 結果のエクスポート: 2 回目は前回のフォルダから始まること。"""
        panel = self._window().snmp_panel
        panel.result_model.set_results([("1.3.6.1.2.1.1.5.0", "OctetString", "sw1")])
        panel._result_host = "192.0.2.10"
        target = os.path.join(self.saved_dir, "result.txt")
        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        return_value=(target, "")), \
                mock.patch("ui.snmp_panel.QMessageBox"):
            panel._on_export_clicked()

        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        return_value=("", "")) as dialog, \
                mock.patch("ui.snmp_panel.QMessageBox"):
            panel._on_export_clicked()
        self.assertEqual(self._initial_dir(dialog), self._norm(self.saved_dir))

    def test_snmp_trap_export_shares_the_folder(self):
        """Trap のエクスポートも同じ場所を使うこと。"""
        window = self._window()
        panel = window.snmp_panel
        panel._add_trap_to_tree({
            "source_ip": "192.0.2.20",
            "source_port": 162,
            "trap_oid": "1.3.6.1.4.1.9.9.41.2.0.1",
            "received_at": "2026-09-23T10:00:00",
            "varbinds": [{"oid": "1.3.6.1.2.1.1.5.0", "value": "sw1"}],
        })
        target = os.path.join(self.saved_dir, "trap.txt")
        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        return_value=(target, "")), \
                mock.patch("ui.snmp_panel.QMessageBox"):
            panel._on_trap_export_clicked()

        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        return_value=("", "")) as dialog, \
                mock.patch("ui.snmp_panel.QMessageBox"):
            panel._on_trap_export_clicked()
        self.assertEqual(self._initial_dir(dialog), self._norm(self.saved_dir))

    # --- Syslog ----------------------------------------------------------

    def test_syslog_export_remembers_the_folder(self):
        """Syslog のエクスポート: 2 回目は前回のフォルダから始まること。"""
        panel = self._window().syslog_panel
        panel.add_message(_Incoming())
        self.app.processEvents()
        target = os.path.join(self.saved_dir, "syslog.txt")
        with mock.patch("ui.syslog_panel.QFileDialog.getSaveFileName",
                        return_value=(target, "")), \
                mock.patch("ui.syslog_panel.QMessageBox"):
            panel._export_messages()

        with mock.patch("ui.syslog_panel.QFileDialog.getSaveFileName",
                        return_value=("", "")) as dialog, \
                mock.patch("ui.syslog_panel.QMessageBox"):
            panel._export_messages()
        self.assertEqual(self._initial_dir(dialog), self._norm(self.saved_dir))

    def test_syslog_save_selected_shares_the_folder(self):
        """選択行の保存も同じ場所を使うこと。"""
        panel = self._window().syslog_panel
        panel.add_message(_Incoming())
        self.app.processEvents()
        panel.table_view.selectRow(0)
        target = os.path.join(self.saved_dir, "selected.txt")
        with mock.patch("ui.syslog_panel.QFileDialog.getSaveFileName",
                        return_value=(target, "")), \
                mock.patch("ui.syslog_panel.QMessageBox"):
            panel._save_selected()

        with mock.patch("ui.syslog_panel.QFileDialog.getSaveFileName",
                        return_value=("", "")) as dialog, \
                mock.patch("ui.syslog_panel.QMessageBox"):
            panel._export_messages()
        self.assertEqual(self._initial_dir(dialog), self._norm(self.saved_dir))


if __name__ == "__main__":
    unittest.main()
