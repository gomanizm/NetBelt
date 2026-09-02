"""途中で切れた WALK の結果を保存しても、切れたと分かることを検証する。

途中で切れたことは status_label に一度出すだけで、_partial_reason は
その場で None にしていた。結果モデルには行しか入らず、txt/csv/json の
書き出しはモデルと件数しか見ないため、途中までの結果を保存した
ファイルは完走した結果と（日時以外）バイト単位で同じになる。

書き出しは「障害チケットや報告書へ回す」ためのもの（_csv_safe の
説明）なので、画面の表示だけでは足りない。ファイルを受け取った側が
「機器に無い」と読み違える。CHANGELOG の「件数と中断の理由を添える」も、
書き出しまで含めるとこの状態では満たしていない。

理由は次の完走か「クリア」まで持ち続け、3 形式すべてに書く。
"""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

ROWS = [
    ("1.3.6.1.2.1.1.1.0", "OctetString", "Cisco IOS Software"),
    ("1.3.6.1.2.1.1.3.0", "TimeTicks", "123456"),
    ("1.3.6.1.2.1.1.5.0", "OctetString", "router01"),
]
REASON = "requestTimedOut"


class PartialWalkExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        from ui.main_window import MainWindow
        window = MainWindow()
        self.addCleanup(window.close)
        return window.snmp_panel

    def _partial(self, panel):
        panel._on_operation_partial(REASON)
        panel._on_operation_completed(True, list(ROWS))

    def _complete(self, panel):
        panel._on_operation_completed(True, list(ROWS))

    def _export(self, panel, kind):
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-snmp-exp-"),
                            "out." + kind)
        writer = getattr(panel, "_export_results_to_" + kind)
        writer(path, panel.result_model.get_all_results())
        with io.open(path, encoding="utf-8") as f:
            return f.read()

    # --- 途中までの結果には印が付くこと ---

    def test_a_partial_walk_is_marked_in_the_text_export(self):
        panel = self._panel()
        self._partial(panel)
        text = self._export(panel, "txt")
        self.assertIn(REASON, text, "中断の理由が書かれていない")
        self.assertIn("全部ではありません", text)

    def test_a_partial_walk_is_marked_in_the_json_export(self):
        panel = self._panel()
        self._partial(panel)
        data = json.loads(self._export(panel, "json"))
        self.assertIs(data.get("complete"), False,
                      "complete が False になっていない: %r" % data.get("complete"))
        self.assertEqual(data.get("partial_reason"), REASON)
        self.assertEqual(len(data["results"]), len(ROWS), "行は全部残すこと")

    def test_a_partial_walk_is_marked_in_the_csv_export(self):
        panel = self._panel()
        self._partial(panel)
        text = self._export(panel, "csv")
        self.assertIn(REASON, text, "中断の理由が書かれていない")

    # --- 完走した結果には付かないこと ---

    def test_a_complete_walk_is_not_marked(self):
        panel = self._panel()
        self._complete(panel)
        data = json.loads(self._export(panel, "json"))
        self.assertIs(data.get("complete"), True)
        self.assertNotIn(REASON, self._export(panel, "txt"))
        self.assertNotIn(REASON, self._export(panel, "csv"))

    def test_a_complete_walk_after_a_partial_one_clears_the_mark(self):
        """次の完走で、前回の中断を持ち越さないこと。"""
        panel = self._panel()
        self._partial(panel)
        self._complete(panel)
        data = json.loads(self._export(panel, "json"))
        self.assertIs(data.get("complete"), True)
        self.assertNotIn(REASON, self._export(panel, "txt"))

    def test_clearing_the_results_clears_the_mark(self):
        panel = self._panel()
        self._partial(panel)
        panel._on_clear_clicked()
        panel._on_operation_completed(True, list(ROWS))
        self.assertNotIn(REASON, self._export(panel, "txt"))

    # --- 画面の表示はこれまでどおり ---

    def test_the_status_label_still_says_it_is_incomplete(self):
        panel = self._panel()
        self._partial(panel)
        self.assertIn(REASON, panel.status_label.text())


if __name__ == "__main__":
    unittest.main()
