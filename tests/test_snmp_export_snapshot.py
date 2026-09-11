"""保存ダイアログを開いている間に次の結果が届いても、保存されるのは
ダイアログを開いた時点の結果とその注記であることを検証する。

_on_export_clicked は行だけをダイアログの前にコピーし、対象ホストと
「途中まで」の理由はダイアログが閉じた後に self から読んでいた。
モーダルダイアログはネストしたイベントループで queued シグナルを
処理するので、途中までの結果 A を表示したまま WALK B を始め、その
実行中にエクスポートを押してダイアログを開いている間に B が完走すると、
A の 2 行に complete=true / partial_reason=null / B のホストが付いて
保存される（実測）。部分結果を完走した結果と誤読させる。

行・ホスト・理由をダイアログの前にまとめて固定し、書き出しへ渡す。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

ROWS_A = [
    ("1.3.6.1.2.1.1.1.0", "OctetString", "A-partial-row-1"),
    ("1.3.6.1.2.1.1.5.0", "OctetString", "A-partial-row-2"),
]
ROWS_B = [
    ("1.3.6.1.2.1.2.2.1.2.%d" % i, "OctetString", "B-row-%d" % i)
    for i in range(1, 9)
]
REASON = "requestTimedOut"
HOST_A = "192.0.2.10"
HOST_B = "192.0.2.99"


class SnmpExportSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        panel = window.snmp_panel
        # 実際の通信はしない。要求は受理されたことにする
        panel.snmp_manager.snmp_walk = mock.Mock(return_value=True)
        panel.oid_edit.setText("1.3.6.1.2.1.1")
        return panel

    def _walk_from(self, panel, host):
        panel.host_edit.setText(host)
        with mock.patch("ui.snmp_panel.QMessageBox"):
            panel._on_walk_clicked()

    def _export_while_b_completes(self, panel, kind):
        """ダイアログを開いている間に B の結果が届く状況で書き出す。"""
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-snmp-snap-"),
                            "out." + kind)

        def dialog_during_which_b_completes(*args, **kwargs):
            # ネストしたイベントループで queued シグナルが処理されるのと
            # 同じこと。ダイアログが閉じる前に B が完走する
            panel._on_operation_completed(True, list(ROWS_B))
            return path, ""

        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        side_effect=dialog_during_which_b_completes),                 mock.patch("ui.snmp_panel.QMessageBox"):
            panel._on_export_clicked()
        with io.open(path, encoding="utf-8") as f:
            return f.read()

    def _arrange(self):
        """A の途中までの結果を表示したまま、B の WALK を走らせる。"""
        panel = self._panel()
        self._walk_from(panel, HOST_A)
        panel._on_operation_partial(REASON)
        panel._on_operation_completed(True, list(ROWS_A))
        self._walk_from(panel, HOST_B)          # B が走り出す
        return panel

    def test_the_json_export_keeps_the_partial_mark_of_the_rows_it_saves(self):
        panel = self._arrange()
        data = json.loads(self._export_while_b_completes(panel, "json"))
        self.assertEqual([r["value"] for r in data["results"]],
                         [r[2] for r in ROWS_A], "保存された行は A のもの")
        self.assertIs(data.get("complete"), False,
                      "A の途中までの行に complete=true が付いている")
        self.assertEqual(data.get("partial_reason"), REASON)

    def test_the_json_export_keeps_the_host_of_the_rows_it_saves(self):
        panel = self._arrange()
        data = json.loads(self._export_while_b_completes(panel, "json"))
        self.assertEqual(data["host"], HOST_A,
                         "A の行に B のホストが付いている")

    def test_the_csv_export_keeps_the_partial_mark(self):
        panel = self._arrange()
        text = self._export_while_b_completes(panel, "csv")
        self.assertIn(REASON, text, "途中までの注記が消えている")
        self.assertIn("A-partial-row-1", text)
        self.assertNotIn("B-row-1", text)

    def test_the_text_export_keeps_the_partial_mark_and_the_host(self):
        panel = self._arrange()
        text = self._export_while_b_completes(panel, "txt")
        self.assertIn(REASON, text, "途中までの注記が消えている")
        self.assertIn("対象ホスト: " + HOST_A, text)
        self.assertIn("件数: 2", text)


if __name__ == "__main__":
    unittest.main()
