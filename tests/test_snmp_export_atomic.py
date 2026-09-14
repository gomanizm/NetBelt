"""エクスポートが失敗したとき、上書き先の元の内容が残ることを検証する。

6 経路（GET/WALK の csv/json/txt と Trap の csv/json/txt）はいずれも保存先を
直接 open('w') で開いていた。open した時点で中身は消えるので、書き込みの
途中で失敗（ディスク満杯・共有断）すると、新しい内容も元の内容も残らない。

同じディレクトリの一時ファイルへ書き切ってから os.replace() で置き換える。
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

KEEP = "これは上書き前からあった内容\n"
HOST = "192.0.2.10"


class ExplodingRow(object):
    """書き出しの途中で失敗する結果行。"""

    def __iter__(self):
        raise RuntimeError("disk full")

    def __getitem__(self, index):
        raise RuntimeError("disk full")


class ExplodingTrap(dict):
    """書き出しの途中で失敗する Trap。"""

    def __getitem__(self, key):
        raise RuntimeError("disk full")

    def get(self, key, default=None):
        raise RuntimeError("disk full")


# json.dump は set を直列化できず、indent 付きなので途中まで書いてから落ちる
UNSERIALIZABLE_ROW = ("1.3.6.1.2.1.1.5.0", "OctetString", set())
UNSERIALIZABLE_TRAP = {"timestamp": "2026-01-01 00:00:00", "varbinds": set()}


class SnmpExportAtomicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        from ui.snmp_panel import SNMPPanel
        panel = SNMPPanel()
        self.addCleanup(panel.close)
        return panel

    def _target(self, name):
        """上書き先として、中身の入ったファイルを1つ用意する。"""
        directory = tempfile.mkdtemp(prefix="netbelt-snmp-atomic-")
        path = os.path.join(directory, name)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(KEEP)
        return path

    def _assert_survived(self, path):
        with io.open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), KEEP, "書き込み失敗で元の内容が消えた")
        self.assertEqual(os.listdir(os.path.dirname(path)),
                         [os.path.basename(path)],
                         "一時ファイルが残っている")

    def _assert_export_fails_without_damage(self, kind, call):
        path = self._target("out." + kind)
        with self.assertRaises(Exception):
            call(path)
        self._assert_survived(path)

    def test_results_csv_failure_keeps_the_overwritten_file(self):
        panel = self._panel()
        self._assert_export_fails_without_damage(
            "csv",
            lambda p: panel._export_results_to_csv(p, [ExplodingRow()],
                                                   HOST, None))

    def test_results_json_failure_keeps_the_overwritten_file(self):
        panel = self._panel()
        self._assert_export_fails_without_damage(
            "json",
            lambda p: panel._export_results_to_json(p, [UNSERIALIZABLE_ROW],
                                                    HOST, None))

    def test_results_txt_failure_keeps_the_overwritten_file(self):
        panel = self._panel()
        self._assert_export_fails_without_damage(
            "txt",
            lambda p: panel._export_results_to_txt(p, [ExplodingRow()],
                                                   HOST, None))

    def test_trap_csv_failure_keeps_the_overwritten_file(self):
        panel = self._panel()
        self._assert_export_fails_without_damage(
            "csv",
            lambda p: panel._export_to_csv(p, [ExplodingTrap()]))

    def test_trap_json_failure_keeps_the_overwritten_file(self):
        panel = self._panel()
        self._assert_export_fails_without_damage(
            "json",
            lambda p: panel._export_to_json(p, [UNSERIALIZABLE_TRAP]))

    def test_trap_txt_failure_keeps_the_overwritten_file(self):
        panel = self._panel()
        self._assert_export_fails_without_damage(
            "txt",
            lambda p: panel._export_to_txt(p, [ExplodingTrap()]))

    def test_a_successful_export_replaces_the_file(self):
        """対照: 成功したときは、ちゃんと新しい内容へ置き換わること。"""
        panel = self._panel()
        path = self._target("out.txt")
        rows = [("1.3.6.1.2.1.1.5.0", "OctetString", "router-a")]
        panel._export_results_to_txt(path, rows, HOST, None)
        with io.open(path, encoding="utf-8") as f:
            written = f.read()
        self.assertIn("対象ホスト: " + HOST, written)
        self.assertNotIn(KEEP, written)
        self.assertEqual(os.listdir(os.path.dirname(path)),
                         [os.path.basename(path)],
                         "一時ファイルが残っている")


if __name__ == "__main__":
    unittest.main()
