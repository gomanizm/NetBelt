"""記録中ダイアログのバイト数表示が、保存先ファイルを見に行かないことを検証する。

何が起きていたか: 記録中ダイアログは 1 秒ごとに os.path.getsize を GUI スレッドで
呼んでいた。共有フォルダ（SMB）へ記録していると、この 1 回が接続の都合で秒単位に
延びる。getsize を 3 秒待つ偽物に替えると、受信が 1 つも無くても 50ms の GUI
タイマーの最大間隔が 3.06 秒になった（実測: scratchpad\\cx5b-termui\\t08.py。
基準 0.067 秒）。その間は再描画も打鍵も通らない。

どう直したか（利用者の決定 2026-09-20）: 表示するバイト数は、ファイルを見に行かず、
アプリがその記録に書いた量を数えて出す。記録は
open(path, 'w', encoding='utf-8') のテキストモードなので、LF は os.linesep へ
直されてから UTF-8 で符号化される。その分を書くたびに数えれば、通常のローカル
フォルダでは停止後の os.path.getsize と一致する。書き込み（write/flush/close）
自体は今までどおり GUI スレッドのまま。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class RecordingDialogDoesNotStatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "rtrA")
        self.path = os.path.join(tempfile.mkdtemp(prefix="netbelt-logstat-"),
                                 "rtrA.log")
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        self.addCleanup(mock.patch.stopall)

    def _recording_widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(self.path, "")):
            w.start_log_recording()
        self.assertIn("rtrA", w._log_files, "前提: 記録が始まっている")
        self.addCleanup(w._stop_log_recording_for, "rtrA")
        w._log_dialogs["rtrA"].timer.stop()
        return w

    @staticmethod
    def _size_label(dialog):
        from PyQt6.QtWidgets import QLabel
        layout = dialog.layout()
        texts = [layout.itemAt(i).widget().text()
                 for i in range(layout.count())
                 if isinstance(layout.itemAt(i).widget(), QLabel)]
        found = [t for t in texts if t.startswith("記録したバイト数")]
        assert len(found) == 1, "バイト数の表示が無い: %r" % texts
        return found[0]

    def test_the_one_second_update_touches_no_file(self):
        """1 秒ごとの更新で、保存先の大きさを調べに行かないこと。"""
        w = self._recording_widget()
        dialog = w._log_dialogs["rtrA"]
        w.append_output("rtrA", "show version\r\n")

        looked_at = []
        real_getsize = os.path.getsize

        def spy(path):
            looked_at.append(path)
            return real_getsize(path)

        with mock.patch("os.path.getsize", spy):
            dialog.timer.timeout.emit()

        self.assertEqual(
            looked_at, [],
            "記録中ダイアログが GUI スレッドでファイルを見に行っている"
            "（共有フォルダだと 1 回で数秒止まる）")
        self.assertNotIn("取得できません", self._size_label(dialog))

    def test_the_counted_bytes_match_the_file_after_stopping(self):
        """数えたバイト数が、停止後の実ファイルの大きさと一致すること。

        日本語（UTF-8 で 3 バイト）と改行（テキストモードで CRLF になる）を
        含めて数え違えていないことを見る。
        """
        w = self._recording_widget()
        dialog = w._log_dialogs["rtrA"]
        w.append_output("rtrA", "".join("行 %03d ok\ttab\r\n" % i
                                        for i in range(40)))
        dialog.timer.timeout.emit()
        shown = self._size_label(dialog)

        w.stop_log_recording("rtrA")

        size = os.path.getsize(self.path)
        self.assertGreater(size, 0, "前提: ファイルへ書けている")
        self.assertEqual(shown, "記録したバイト数: {:,} バイト".format(size))

    def test_a_new_recording_counts_from_zero(self):
        """記録を始め直したら、前の記録の分を持ち越さないこと。"""
        w = self._recording_widget()
        w.append_output("rtrA", "".join("line %03d\r\n" % i for i in range(20)))
        w.stop_log_recording("rtrA")

        second = os.path.join(os.path.dirname(self.path), "rtrA-2.log")
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(second, "")):
            w.start_log_recording()
        self.addCleanup(w._stop_log_recording_for, "rtrA")
        from core import log_recording
        self.addCleanup(log_recording.stop, "rtrA", second)
        dialog = w._log_dialogs["rtrA"]
        dialog.timer.stop()

        self.assertEqual(self._size_label(dialog), "記録したバイト数: 0 バイト")
        w.append_output("rtrA", "hello\r\n")
        dialog.timer.timeout.emit()
        self.assertEqual(
            self._size_label(dialog),
            "記録したバイト数: {:,} バイト".format(len("hello" + os.linesep)))


if __name__ == "__main__":
    unittest.main()
