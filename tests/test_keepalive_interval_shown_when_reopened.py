"""開き直したマクロ設定ダイアログが、実際のキープアライブ間隔を出すことを検証する。

MacroDialog は keepalive_active（動作中かどうか）しか受け取らず、実間隔は
MainWindow の keepalive_intervals が持っているのに渡していなかった。
そのため 10 秒で動かしていても、送信間隔の初期値 60 のまま
「状態: 動作中（60秒間隔）」と表示される。さらにその画面で値を触らずに
停止 -> 開始を押すと、spin の 60 がそのまま送られ、実周期が黙って
60 秒へ変わり、MainWindow の保持値も 60 に書き換わる。

開いた時点の実間隔をダイアログへ渡し、表示にも次の開始にも使う。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


class KeepaliveIntervalShownWhenReopenedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        ap = mock.patch("core.config_manager.app_data_dir",
                        return_value=Path(tempfile.mkdtemp(prefix="netbelt-ka-")))
        ap.start()
        self.addCleanup(ap.stop)

    def _window(self):
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        return window

    @staticmethod
    def _open_and_capture(open_dialog):
        """ダイアログを開き、exec は素通りさせて中身だけ受け取る。"""
        captured = {}

        def fake_exec(dialog, *args, **kwargs):
            captured["dialog"] = dialog
            return 0

        with mock.patch("PyQt6.QtWidgets.QDialog.exec", fake_exec):
            open_dialog()
        return captured["dialog"]

    def test_the_dialog_shows_the_interval_it_was_given(self):
        """渡された間隔が、送信間隔の欄にも状態の文にも出ること。"""
        from ui.dialogs.macro_dialog import MacroDialog
        dialog = MacroDialog(None, device_name="dev",
                             keepalive_active=True, keepalive_interval=10)
        self.addCleanup(dialog.deleteLater)

        self.assertEqual(dialog.keepalive_interval_spin.value(), 10)
        self.assertEqual(dialog.keepalive_status_label.text(),
                         "状態: 動作中（10秒間隔）")

    def test_reopening_the_dialog_shows_the_running_interval(self):
        """10秒で動かしている機器の設定を開き直すと 10秒と出ること。"""
        window = self._window()
        window.terminal_widget.create_terminal_tab("dev")
        window.connections["dev"] = mock.Mock()
        window.macro_manager.register_send_callback("dev", lambda s: None)
        window._start_keepalive("dev", 10)
        self.addCleanup(window.macro_manager.cleanup_device, "dev")

        dialog = self._open_and_capture(
            lambda: window._on_macro_settings_from_context("dev"))

        self.assertEqual(dialog.keepalive_interval_spin.value(), 10,
                         "開き直したダイアログが実間隔を出していない")
        self.assertEqual(dialog.keepalive_status_label.text(),
                         "状態: 動作中（10秒間隔）")

    def test_stop_then_start_in_the_reopened_dialog_keeps_the_interval(self):
        """開き直して停止 -> 開始しても、間隔が黙って変わらないこと。"""
        window = self._window()
        window.terminal_widget.create_terminal_tab("dev")
        window.connections["dev"] = mock.Mock()
        window.macro_manager.register_send_callback("dev", lambda s: None)
        window._start_keepalive("dev", 10)
        self.addCleanup(window.macro_manager.cleanup_device, "dev")

        dialog = self._open_and_capture(
            lambda: window._on_macro_settings_from_context("dev"))
        dialog.keepalive_stop_btn.click()
        dialog.keepalive_start_btn.click()

        self.assertEqual(window.keepalive_intervals["dev"], 10,
                         "触っていないのに間隔が変わった: %r"
                         % window.keepalive_intervals["dev"])
        timer = window.macro_manager._keepalive_timers["dev"]
        self.assertEqual(timer.interval(), 10000,
                         "実際のタイマー周期が変わった: %d ms" % timer.interval())


if __name__ == "__main__":
    unittest.main()
