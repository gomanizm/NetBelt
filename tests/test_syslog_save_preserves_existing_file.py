"""Syslog パネルの保存が、失敗時に上書き先の既存ログを失わないことを検証する。

_export_messages（JSON / テキスト）と _save_selected は保存先を直接
open(filename, 'w') で開いていた。with を抜けた時点で旧内容は失われ、
書き込み中の失敗（満杯・共有切断・USB 取り外し）では新旧どちらでもない
部分ファイルが残る。except は QMessageBox を出すだけで復旧しない。

全ログ保存側（ui/dialogs/log_save_dialog.py）は同じ欠陥を一時ファイル →
os.replace で既に直してあるので、syslog パネルもそれに揃える。
"""
import builtins
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

ORIGINAL = "old syslog contents that must survive\n" * 20
_real_open = builtins.open


class _Incoming:
    """受信側 SyslogMessage の最小形（panel.add_message が読む属性だけ）。"""

    def __init__(self, text):
        self.timestamp = "2026-09-09 14:12:02"
        self.hostname = "rtr1"
        self.level = "Info"
        self.message = text
        self.raw_message = "<134>Sep  9 14:12:02 rtr1 " + text
        self.source_ip = "192.0.2.1"
        self.source_display = "192.0.2.1 (UDP/514)"


class _FileProxy:
    """書き込みを数えて、指定回数目で「ディスク満杯」を起こす代役。"""

    def __init__(self, inner, fail_at=None):
        self._inner = inner
        self._fail_at = fail_at
        self.writes = 0

    def write(self, text):
        self.writes += 1
        if self._fail_at is not None and self.writes >= self._fail_at:
            raise OSError(28, "No space left on device")
        return self._inner.write(text)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _patched_open(fail_at):
    """open() で得た書き込み用ファイルを _FileProxy で包む（読み取りは素通し）。"""
    def fake_open(file, mode="r", *args, **kwargs):
        f = _real_open(file, mode, *args, **kwargs)
        if "w" in mode or "a" in mode:
            return _FileProxy(f, fail_at)
        return f
    return mock.patch("builtins.open", fake_open)


class SyslogSavePreservesExistingFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.syslog_panel import SyslogPanel
        self.panel = SyslogPanel()
        for text in ("link down", "link up"):
            self.panel.add_message(_Incoming(text))
        self.app.processEvents()
        self.tmp = tempfile.mkdtemp(prefix="netbelt-syslog-save-")

    def _target(self, name):
        path = os.path.join(self.tmp, name)
        with _real_open(path, "w", encoding="utf-8", newline="") as f:
            f.write(ORIGINAL)
        return path

    def _read(self, path):
        with _real_open(path, "r", encoding="utf-8", newline="") as f:
            return f.read()

    def _leftovers(self, keep):
        return sorted(n for n in os.listdir(self.tmp) if n != keep)

    def _run(self, method, filename):
        with mock.patch("ui.syslog_panel.QFileDialog.getSaveFileName",
                        return_value=(filename, "")), \
             mock.patch("ui.syslog_panel.QMessageBox.information"), \
             mock.patch("ui.syslog_panel.QMessageBox.critical") as critical, \
             mock.patch("ui.syslog_panel.QMessageBox.warning"):
            method()
        return critical

    def test_text_export_failure_leaves_the_old_contents(self):
        target = self._target("all.txt")
        with _patched_open(fail_at=1):
            critical = self._run(self.panel._export_messages, target)

        self.assertTrue(critical.called, "失敗が利用者へ通知されていない")
        self.assertEqual(self._read(target), ORIGINAL,
                         "書き込み失敗で上書き先の既存ログが失われた")
        self.assertEqual(self._leftovers("all.txt"), [], "一時ファイルが残っている")

    def test_json_export_failure_leaves_the_old_contents(self):
        target = self._target("all.json")
        with _patched_open(fail_at=2):
            critical = self._run(self.panel._export_messages, target)

        self.assertTrue(critical.called, "失敗が利用者へ通知されていない")
        self.assertEqual(self._read(target), ORIGINAL,
                         "書き込み失敗で上書き先の既存ログが失われた")
        self.assertEqual(self._leftovers("all.json"), [], "一時ファイルが残っている")

    def test_save_selected_failure_leaves_the_old_contents(self):
        self.panel.table_view.selectRow(0)
        target = self._target("sel.txt")
        with _patched_open(fail_at=1):
            critical = self._run(self.panel._save_selected, target)

        self.assertTrue(critical.called, "失敗が利用者へ通知されていない")
        self.assertEqual(self._read(target), ORIGINAL,
                         "書き込み失敗で上書き先の既存ログが失われた")
        self.assertEqual(self._leftovers("sel.txt"), [], "一時ファイルが残っている")

    def test_successful_text_export_replaces_the_file(self):
        target = self._target("all.txt")
        self._run(self.panel._export_messages, target)

        content = self._read(target)
        self.assertNotIn("old syslog contents", content)
        self.assertIn("link down", content)
        self.assertIn("link up", content)
        self.assertEqual(self._leftovers("all.txt"), [], "一時ファイルが残っている")

    def test_successful_json_export_replaces_the_file(self):
        target = self._target("all.json")
        self._run(self.panel._export_messages, target)

        with _real_open(target, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual([rec["message"] for rec in data], ["link down", "link up"])
        self.assertEqual(self._leftovers("all.json"), [], "一時ファイルが残っている")

    def test_successful_save_selected_replaces_the_file(self):
        self.panel.table_view.selectRow(0)
        target = self._target("sel.txt")
        self._run(self.panel._save_selected, target)

        content = self._read(target)
        self.assertNotIn("old syslog contents", content)
        self.assertIn("link down", content)
        self.assertEqual(self._leftovers("sel.txt"), [], "一時ファイルが残っている")


if __name__ == "__main__":
    unittest.main()
