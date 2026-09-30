"""停止して書き終えていない記録のファイルを選んだとき、「先に停止してください」と案内しないことを検証する（4 周目 term の使用中の案内）。

何が起きていたか（cd8d9c1 で実測。scratchpad\\cx132e-term-fix1\\。このテストの
直す前の実行）: 記録先が応答しないまま記録を停止すると、停止した記録の使用中の
登録（core.log_recording）は、書き込みスレッドが書き終えて閉じるまで残る（先に
外すと、同じファイルへの次の書き込みと混ざる）。その間にそのファイルを記録・
全ログ保存・エクスポートの保存先に選ぶと、『このファイルは dev のログ記録に
使用中です: … 別のファイルを選ぶか、先にそのログ記録を停止してください。』で
断られた。記録はすでに停止しているので、利用者はこの案内に従いようがない。

どう直したか: 停止した記録の登録に印を付ける（log_recording.mark_stopped。
書き終えて閉じたら stop が外す）。断るときの案内文は log_recording.in_use_message
にまとめ、停止した記録のファイルなら『停止した dev のログ記録が、停止より前に
受信した分をまだ書き込んでいます … 別のファイルを選んでください。』と案内する。
記録中のファイルの案内は変えない。端末の記録・全ログ保存、SNMP・Syslog・
サーバーパネルのエクスポートは、同じ案内を使う。
"""
import builtins
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 記録中のファイルを選んだときの案内（これまでどおり）
ACTIVE_HINT = "別のファイルを選ぶか、先にそのログ記録を停止してください。"


class _StallingFile:
    """放されるまで write / flush / close が戻らない記録ファイル（応答しない共有フォルダの代わり）。

    放されないまま limit 秒たったら戻る（直す前の作りでテストが止まったままに
    ならないように）。
    """

    def __init__(self, f, release, limit):
        self._f = f
        self.name = f.name
        self._release = release
        self._limit = limit

    def write(self, text):
        self._release.wait(self._limit)
        return self._f.write(text)

    def flush(self):
        self._release.wait(self._limit)
        return self._f.flush()

    def close(self):
        self._release.wait(self._limit)
        return self._f.close()

    @property
    def closed(self):
        return self._f.closed


def _pump(ms):
    """ms ミリ秒だけイベントループを回す。"""
    from PyQt6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class LogRecordingIoStallInUseMessageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "dev")
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-inuse-")
        self._old_cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self._old_cwd)
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.critical").start()
        self.addCleanup(mock.patch.stopall)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("dev")
        w.tab_widget.setCurrentWidget(w._terminals["dev"])
        return w

    def _start(self, w, name, stalled):
        path = os.path.join(self.dir, name)
        real_open = builtins.open

        def fake_open(file, mode="r", *args, **kwargs):
            f = real_open(file, mode, *args, **kwargs)
            if stalled and isinstance(file, str) and file == path and "w" in mode:
                return _StallingFile(f, self.release, 30.0)
            return f

        with mock.patch("builtins.open", fake_open), \
             mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        self.assertIn("dev", w._log_files, "前提: 記録が始まっている")
        return path

    @staticmethod
    def _wait_until(predicate, seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            _pump(20)
        return predicate()

    def _stopped_while_stalled(self, w):
        """記録先が詰まったまま記録を停止し、停止した記録が書き終えていない状態を作る。"""
        from core import log_recording
        path = self._start(w, "dev.log", stalled=True)
        w.append_output("dev", "show clock\r\n")
        w.stop_log_recording("dev")
        self.assertNotIn("dev", w._log_files, "前提: 記録は停止した")
        self.assertEqual(log_recording.device_using(path), "dev",
                         "前提: 停止した記録が書き終えるまで使用中の登録が残る")
        return path

    def _refusal(self, action, path):
        """保存先に path を選んで action を行い、出た警告の本文を返す（出なければ None）。"""
        self.warning.reset_mock()
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            action()
        texts = [c.args[2] for c in self.warning.call_args_list if len(c.args) > 2]
        self.assertLessEqual(len(texts), 1, "警告が 2 回以上出た: %r" % texts)
        return texts[0] if texts else None

    def _assert_stopped_message(self, text, path, where):
        self.assertIsNotNone(text, "%s: 使用中のファイルなのに断らなかった" % where)
        self.assertNotIn("先にそのログ記録を停止してください", text,
                         "%s: 停止済みの記録に「停止してください」と案内した: %s"
                         % (where, text))
        self.assertIn("停止した dev のログ記録", text, where)
        self.assertIn(path, text, where)
        self.assertIn("別のファイルを選んでください", text, where)

    def _assert_active_message(self, text, path, where):
        self.assertIsNotNone(text, "%s: 記録中のファイルなのに断らなかった" % where)
        self.assertEqual(
            text, "このファイルは dev のログ記録に使用中です:\n%s\n%s"
            % (path, ACTIVE_HINT),
            "%s: 記録中のファイルの案内が変わった" % where)

    def test_the_terminal_does_not_ask_to_stop_a_recording_already_stopped(self):
        """停止して書き終えていない記録のファイルへの記録・全ログ保存は、停止済みと分かる案内で断ること。"""
        from core import log_recording
        w = self._widget()
        path = self._start(w, "dev.log", stalled=True)
        w.append_output("dev", "show clock\r\n")

        # 対照: 記録中のファイルへの全ログ保存は、これまでどおりの案内で断る
        dialog = mock.patch("ui.dialogs.log_save_dialog.LogSaveProgressDialog").start()
        self._assert_active_message(
            self._refusal(w.save_current_log, path), path, "記録中の全ログ保存")

        w.stop_log_recording("dev")
        self.assertEqual(log_recording.device_using(path), "dev",
                         "前提: 停止した記録が書き終えるまで使用中の登録が残る")
        self._assert_stopped_message(
            self._refusal(w.start_log_recording, path), path, "記録の開始")
        self.assertNotIn("dev", w._log_files, "使用中のファイルへ記録を始めてしまった")
        self._assert_stopped_message(
            self._refusal(w.save_current_log, path), path, "全ログ保存")

        # 同じ機器で別のファイルへ記録を始め直しても、停止した記録のファイルは
        # 停止済みとして案内し、記録中のファイルはこれまでどおりに案内する
        path2 = self._start(w, "dev2.log", stalled=False)
        self._assert_stopped_message(
            self._refusal(w.save_current_log, path), path, "始め直したあとの全ログ保存")
        self._assert_active_message(
            self._refusal(w.save_current_log, path2), path2, "始め直した記録の全ログ保存")
        self.assertEqual(dialog.call_count, 0, "使用中のファイルへ保存を始めてしまった")
        w.stop_log_recording("dev")

        # 書き終えて閉じたら登録も印も外れ、同じファイルへまた記録できる。その記録の
        # 案内は「記録中」に戻る（停止した記録の印が残っていない）
        self.release.set()
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None, 5.0),
            "詰まりが解けても停止した記録が閉じない")
        self._start(w, "dev.log", stalled=False)
        self._assert_active_message(
            self._refusal(w.save_current_log, path), path, "記録し直したファイルの全ログ保存")
        self.assertEqual(dialog.call_count, 0)
        w.stop_log_recording("dev")

    def test_the_other_screens_use_the_same_message_for_a_stopped_recording(self):
        """SNMP・Syslog・サーバーパネルのエクスポートも、停止した記録のファイルには同じ案内で断ること。"""
        from PyQt6.QtWidgets import QWidget
        from ui import log_export
        from ui.snmp_panel import SNMPPanel
        from ui.syslog_panel import SyslogPanel
        w = self._widget()
        path = self._stopped_while_stalled(w)

        snmp = SNMPPanel()
        self.addCleanup(snmp.close)
        syslog = SyslogPanel()
        self.addCleanup(syslog.close)
        owner = QWidget()
        self.addCleanup(owner.close)

        self.warning.reset_mock()
        self.assertTrue(snmp._refuse_if_recording("エクスポート", path))
        self._assert_stopped_message(self.warning.call_args.args[2], path, "SNMP")
        self.warning.reset_mock()
        self.assertTrue(syslog._refuse_if_recording("エクスポート", path))
        self._assert_stopped_message(self.warning.call_args.args[2], path, "Syslog")
        saved = []
        self._assert_stopped_message(
            self._refusal(lambda: saved.append(
                log_export.export_log_text(owner, "log line\n", "tftp_log")), path),
            path, "サーバーパネル")
        self.assertEqual(saved, [None], "使用中のファイルへエクスポートした")


if __name__ == "__main__":
    unittest.main()
