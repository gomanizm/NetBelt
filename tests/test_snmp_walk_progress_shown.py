"""長い WALK の途中で、取れた件数が画面に出ることを検証する。

何が起きていたか（実測、基準 441ea02）: localhost の偽エージェントが 350 行を
1 行 5ms の遅れで返す状態で、パネルの WALK から本物の SNMPWorker で取得した
（2.24 秒、351 リクエスト）。SNMPManager の progress_update は
「100件取得中...」「200件取得中...」「300件取得中...」と 3 回出たが、
status_label に出た文字列は「WALK実行中...」「完了: 350件」の 2 つだけ。
表は完了まで 0 行なので、実行中に件数を知る手段が無かった。パネルの
set_snmp_manager が progress_update をつないでおらず、src/ 全体に受け手が
無かった。

どう直したか: set_snmp_manager で progress_update を _on_operation_progress へ
つなぎ、「WALK実行中...（100件取得中）」のように出す。出すのは停止ボタンが
出ていて押せるとき（実行中で、停止を押す前）だけ。停止を押した後の
「停止中…」や、結果が届いた後の「完了: N件」は上書きしない。

ここでは実際の SNMPManager を使い、_snmp_walk（pysnmp 5.1.0 のころは
nextCmd）だけを差し替える。差し替えた _snmp_walk は before 行返した
ところで止まり、合図を受けてから続きを返す。
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

HOST = "127.0.0.1"
BASE = "1.3.6.1.4.1.99999.1"


def _row(index):
    from pysnmp.proto.rfc1902 import ObjectName, OctetString
    return (ObjectName("%s.%d" % (BASE, index)),
            OctetString("row-%d" % index))


class _GatedWalk:
    """before 行返したところで止まり、release の合図で続きを返す _snmp_walk。"""

    def __init__(self, before, rows):
        self.before = before
        self.rows = rows
        self.reached = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args, **kwargs):
        def generate():
            for index in range(1, self.before + 1):
                yield (None, None, None, [_row(index)])
            self.reached.set()
            self.release.wait(10)
            for index in range(self.before + 1, self.rows + 1):
                yield (None, None, None, [_row(index)])
        return generate()


class SnmpWalkProgressShownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.snmp_manager import SNMPManager
        from ui.snmp_panel import SNMPPanel
        # MIB の読み込みはこの検査と関係が無いので走らせない
        with mock.patch.object(SNMPPanel, "_start_background_mib_loading"):
            self.panel = SNMPPanel()
        self.manager = SNMPManager()
        self.panel.set_snmp_manager(self.manager)
        self.panel.host_edit.setText(HOST)
        self.panel.oid_edit.setText(BASE)
        self.gate = _GatedWalk(before=100, rows=120)
        for patch in (
                mock.patch("core.snmp_manager._snmp_walk",
                           side_effect=lambda *a, **k: self.gate(*a, **k)),
                # モーダルで止まらないよう塞ぐ
                mock.patch("ui.snmp_panel.QMessageBox")):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._drain)

    def _drain(self):
        """止めた _snmp_walk を流し切り、ワーカーを手放してから片付ける。"""
        self.gate.release.set()
        self._pump_until(lambda: self.manager.worker is None, 10)
        self.panel.deleteLater()
        self.manager.deleteLater()
        self.app.processEvents()

    def _pump_until(self, done, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if done():
                return True
            time.sleep(0.01)
        return done()

    def _start_and_wait_for_100_rows(self):
        self.panel.walk_button.click()
        self.assertTrue(self.gate.reached.wait(10), "WALK が 100 行に届かない")
        # 100 行目の進捗はワーカーから queued で積まれている。捌かせる
        self._pump_until(lambda: False, 0.2)

    def test_a_long_walk_shows_how_many_rows_it_has_so_far(self):
        self._start_and_wait_for_100_rows()
        self.assertTrue(
            self._pump_until(
                lambda: "100件" in self.panel.status_label.text(), 5),
            "100 行取れても件数が出ない: %r" % self.panel.status_label.text())
        self.assertIn("WALK実行中", self.panel.status_label.text())
        self.gate.release.set()
        self.assertTrue(self._pump_until(
            lambda: self.manager.worker is None, 10))
        self.assertEqual(self.panel.status_label.text(), "完了: 120件")

    def test_later_progress_replaces_the_earlier_count(self):
        """対照: 停止前なら、届いた進捗がそのまま画面に出ること。"""
        self._start_and_wait_for_100_rows()
        self.manager.progress_update.emit("200件取得中...")
        self.app.processEvents()
        self.assertIn("200件", self.panel.status_label.text())

    def test_progress_does_not_overwrite_stopping(self):
        self._start_and_wait_for_100_rows()
        self.panel.stop_button.click()
        shown = self.panel.status_label.text()
        self.assertIn("停止中", shown)
        self.manager.progress_update.emit("200件取得中...")
        self.app.processEvents()
        self.assertEqual(self.panel.status_label.text(), shown)

    def test_progress_after_the_result_does_not_overwrite_it(self):
        self._start_and_wait_for_100_rows()
        self.gate.release.set()
        self.assertTrue(self._pump_until(
            lambda: self.manager.worker is None, 10))
        self.manager.progress_update.emit("200件取得中...")
        self.app.processEvents()
        self.assertEqual(self.panel.status_label.text(), "完了: 120件")


if __name__ == "__main__":
    unittest.main()
