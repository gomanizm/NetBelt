"""拡張子を付け替えた先が既にあるとき、確認なしに上書きしないことを検証する。

何が起きていたか（実測、基準 63c27f5。検査役 2 人がそれぞれ再現）:
保存ダイアログの上書き確認は、利用者がその場で選んだ名前についてしか
行われない。ところがこちらは、選ばれた絞り込みに合わせて拡張子を
付け替えている（core/save_defaults.py の apply_filter_suffix）。
付け替えた後の名前が既にあるかどうかは誰も確かめていないため、

  - out.csv が既にあるフォルダで、ダイアログに out.txt と入れて
    絞り込みで CSV を選ぶ
  - 既定の名前のまま種類だけ CSV へ変え、同じ名前の .csv が既にある

のどちらでも、上書きの確認が出ないまま既存のファイルが置き換わっていた。
置き換わるのは利用者のファイルで、取り返しがつかない。

どう直したか（検査役の案）: 付け替えでパスが変わり、その先が既にある
ときだけ、こちらで上書きの確認を出す（apply_filter_suffix の直後、
保存の入口 6 か所）。文面は既存の上書き確認（ui/sftp_panel.py の
「上書き確認」「… が既にあります。上書きしますか？」）に合わせ、
「いいえ」なら保存しない（保存先も覚えない）。

なお 3 番目のテスト（検査役の再現そのもの）は、名前を打ち直した場合に
そもそも付け替えないという後続の直し（既定の名前で判定する）でも通る。
ここで守りたいのは「既存のファイルが黙って置き換わらない」ことなので、
確認が出たかどうかではなく既存ファイルの中身で判定している。
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 先にあったファイルの中身。置き換わったかどうかはこれで見る
EXISTING = "先にあった内容"


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


class SuffixSwapOverwriteConfirmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作ったウィンドウはクラス終了まで保持する
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-swapconfirm-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        # cwd/logs を作られてもリポジトリを汚さないよう、作業場所を移す
        cwd = tempfile.mkdtemp(prefix="netbelt-swapconfirm-cwd-")
        self.addCleanup(shutil.rmtree, cwd, True)
        old_cwd = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, old_cwd)

    # --- 道具 ------------------------------------------------------------

    def _config_manager(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-swapconfirm-conf-")
        self.addCleanup(shutil.rmtree, d, True)
        return ConfigManager(config_path=os.path.join(d, "config.json"))

    def _window(self):
        from ui.main_window import MainWindow
        cm = self._config_manager()
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            window = MainWindow()
        type(self)._windows.append(window)
        return window

    def _entry(self, filters, ext):
        """絞り込みの一覧から、その拡張子の 1 件を選ぶ"""
        for entry in filters.split(";;"):
            if "*" + ext in entry.lower():
                return entry
        raise AssertionError("絞り込みに %s が無い: %r" % (ext, filters))

    def _put(self, name):
        """先にあったファイルを 1 つ置く"""
        path = os.path.join(self.dir, name)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(EXISTING)
        return path

    def _read(self, path):
        with io.open(path, encoding="utf-8-sig") as f:
            return f.read()

    def _dialog(self, ext, type_name=None):
        """保存ダイアログの代わり。付け替え先を先に作っておいて返す。

        既定の名前には日時が入り、呼ぶまで決まらない。付け替え先
        （既定の名前の拡張子だけを ext にしたもの）は、ここで初めて
        分かるので、この中で作る。
        """
        seen = {}

        def fake(parent, title, initial, filters="", *args, **kwargs):
            default_name = os.path.basename(initial)
            name = type_name or default_name
            seen["target"] = self._put(
                os.path.splitext(name)[0] + ext)
            return os.path.join(self.dir, name), self._entry(filters, ext)

        return seen, fake

    def _run(self, ui_module, handler, ext, type_name=None, answer=None):
        """保存の入口を 1 回動かし、上書き確認のモックと見たものを返す"""
        from PyQt6.QtWidgets import QMessageBox
        if answer is None:
            answer = QMessageBox.StandardButton.No
        seen, fake = self._dialog(ext, type_name)
        with mock.patch(ui_module + ".QFileDialog.getSaveFileName",
                        side_effect=fake), \
                mock.patch(ui_module + ".QMessageBox"), \
                mock.patch.object(QMessageBox, "question",
                                  return_value=answer) as question:
            handler()
        return seen, question

    # --- 画面ごとの下ごしらえ --------------------------------------------

    def _snmp_results_panel(self):
        panel = self._window().snmp_panel
        panel.result_model.set_results(
            [("1.3.6.1.2.1.1.5.0", "OctetString", "sw1")])
        panel._result_host = "192.0.2.10"
        return panel

    def _snmp_traps_panel(self):
        panel = self._window().snmp_panel
        panel._add_trap_to_tree({
            "source_ip": "192.0.2.20",
            "source_port": 162,
            "trap_oid": "1.3.6.1.4.1.9.9.41.2.0.1",
            "received_at": "2026-09-23T10:00:00",
            "varbinds": [{"oid": "1.3.6.1.2.1.1.5.0", "value": "sw1"}],
        })
        return panel

    def _syslog_panel(self):
        panel = self._window().syslog_panel
        panel.add_message(_Incoming())
        self.app.processEvents()
        return panel

    def _terminal(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget(config_manager=self._config_manager())
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        w.append_output("rtrA", "Router#show version\r\n")
        self.app.processEvents()
        return w

    # --- 入口ごとの回帰 --------------------------------------------------

    def test_snmp_results_swap_asks_before_replacing(self):
        """SNMP 結果: 既定の名前のまま CSV を選んだら、既存の .csv を守ること。"""
        panel = self._snmp_results_panel()
        seen, question = self._run("ui.snmp_panel", panel._on_export_clicked,
                                   ".csv")
        self.assertTrue(question.called, "付け替え先の上書き確認が出ていない")
        self.assertEqual(self._read(seen["target"]), EXISTING,
                         "確認を待たずに既存ファイルを置き換えた")

    def test_snmp_traps_swap_asks_before_replacing(self):
        panel = self._snmp_traps_panel()
        seen, question = self._run("ui.snmp_panel",
                                   panel._on_trap_export_clicked, ".csv")
        self.assertTrue(question.called, "付け替え先の上書き確認が出ていない")
        self.assertEqual(self._read(seen["target"]), EXISTING,
                         "確認を待たずに既存ファイルを置き換えた")

    def test_syslog_export_swap_asks_before_replacing(self):
        panel = self._syslog_panel()
        seen, question = self._run("ui.syslog_panel", panel._export_messages,
                                   ".csv")
        self.assertTrue(question.called, "付け替え先の上書き確認が出ていない")
        self.assertEqual(self._read(seen["target"]), EXISTING,
                         "確認を待たずに既存ファイルを置き換えた")

    def test_syslog_selected_rows_swap_asks_before_replacing(self):
        """選択行の保存: 拡張子なしで打つと .txt が足される経路。"""
        panel = self._syslog_panel()
        panel.table_view.selectRow(0)
        seen, question = self._run("ui.syslog_panel", panel._save_selected,
                                   ".txt", type_name="out")
        self.assertTrue(question.called, "付け替え先の上書き確認が出ていない")
        self.assertEqual(self._read(seen["target"]), EXISTING,
                         "確認を待たずに既存ファイルを置き換えた")

    def test_terminal_log_save_swap_asks_before_replacing(self):
        """全ログ保存: 既定は .log なので、テキストを選ぶと付け替わる。"""
        from PyQt6.QtWidgets import QMessageBox
        w = self._terminal()
        seen, fake = self._dialog(".txt")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        side_effect=fake), \
                mock.patch("ui.dialogs.log_save_dialog.LogSaveProgressDialog"
                           ) as progress, \
                mock.patch("PyQt6.QtWidgets.QMessageBox.information"), \
                mock.patch.object(
                    QMessageBox, "question",
                    return_value=QMessageBox.StandardButton.No) as question:
            progress.return_value.exec.return_value = True
            w.save_current_log()
        self.assertTrue(question.called, "付け替え先の上書き確認が出ていない")
        self.assertFalse(progress.called,
                         "いいえと答えたのに保存へ進んだ: %r" % (progress.call_args,))
        self.assertEqual(self._read(seen["target"]), EXISTING,
                         "確認を待たずに既存ファイルを置き換えた")

    def test_terminal_log_recording_swap_asks_before_replacing(self):
        """ログ記録開始: 'w' で開くので、断られたら開いてはいけない。"""
        from PyQt6.QtWidgets import QMessageBox
        w = self._terminal()
        seen, fake = self._dialog(".txt")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        side_effect=fake), \
                mock.patch("ui.dialogs.log_recording_dialog.LogRecordingDialog"), \
                mock.patch("PyQt6.QtWidgets.QMessageBox.information"), \
                mock.patch("PyQt6.QtWidgets.QMessageBox.warning"), \
                mock.patch.object(
                    QMessageBox, "question",
                    return_value=QMessageBox.StandardButton.No) as question:
            w.start_log_recording()
        self.addCleanup(w.stop_log_recording, "rtrA")
        self.assertTrue(question.called, "付け替え先の上書き確認が出ていない")
        self.assertEqual(self._read(seen["target"]), EXISTING,
                         "確認を待たずに既存ファイルを切り詰めた")
        self.assertNotIn("rtrA", w._log_files,
                         "いいえと答えたのに記録を始めた")

    # --- 断ったとき / 承知したとき ---------------------------------------

    def test_saying_no_does_not_remember_the_folder(self):
        """断った保存先は覚えないこと（書けていない場所が次の初期値になる）。"""
        panel = self._snmp_results_panel()
        self._run("ui.snmp_panel", panel._on_export_clicked, ".csv")
        self.assertIsNone(panel.config_manager.get_last_save_dir(),
                          "保存していないフォルダを覚えている")

    def test_saying_yes_saves_and_remembers(self):
        """承知したら、これまでどおり書けて保存先も覚えること。"""
        from PyQt6.QtWidgets import QMessageBox
        panel = self._snmp_results_panel()
        seen, question = self._run("ui.snmp_panel", panel._on_export_clicked,
                                   ".csv",
                                   answer=QMessageBox.StandardButton.Yes)
        self.assertTrue(question.called, "付け替え先の上書き確認が出ていない")
        self.assertIn("OID,Type,Value", self._read(seen["target"]),
                      "はいと答えたのに CSV が書かれていない")
        self.assertEqual(panel.config_manager.get_last_save_dir(), self.dir,
                         "保存したフォルダを覚えていない")

    def test_the_reviewers_repro_keeps_the_other_file(self):
        """検査役の再現: out.csv があるのに out.txt + CSV で置き換わらないこと。

        名前を打ち直した場合は付け替え自体を止めるのが正しいので、ここでは
        確認が出たかどうかではなく、既存ファイルが残っているかだけを見る。
        """
        from PyQt6.QtWidgets import QMessageBox
        panel = self._snmp_results_panel()
        target = self._put("out.csv")

        def fake(parent, title, initial, filters="", *args, **kwargs):
            return (os.path.join(self.dir, "out.txt"),
                    self._entry(filters, ".csv"))

        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        side_effect=fake), \
                mock.patch("ui.snmp_panel.QMessageBox"), \
                mock.patch.object(
                    QMessageBox, "question",
                    return_value=QMessageBox.StandardButton.No):
            panel._on_export_clicked()

        self.assertEqual(self._read(target), EXISTING,
                         "確認なしに out.csv が置き換わった")

    def test_no_extra_question_when_nothing_was_swapped(self):
        """付け替えが起きていないなら、余計な確認を増やさないこと。

        利用者がダイアログで既存のファイルを直に選んだときは、ダイアログ側で
        既に上書きを訊かれている。同じことを二度訊かない。
        """
        from PyQt6.QtWidgets import QMessageBox
        panel = self._snmp_results_panel()
        target = self._put("out.csv")

        def fake(parent, title, initial, filters="", *args, **kwargs):
            return target, self._entry(filters, ".csv")

        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        side_effect=fake), \
                mock.patch("ui.snmp_panel.QMessageBox"), \
                mock.patch.object(
                    QMessageBox, "question",
                    return_value=QMessageBox.StandardButton.No) as question:
            panel._on_export_clicked()

        self.assertFalse(question.called,
                         "ダイアログが訊いた上書きを二度訊いている")
        self.assertIn("OID,Type,Value", self._read(target),
                      "選んだファイルへ書かれていない")


if __name__ == "__main__":
    unittest.main()
