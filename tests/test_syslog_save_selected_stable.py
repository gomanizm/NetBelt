"""選択行の保存が、ダイアログ中に行が流れても選んだ行を書くことを確認する。

_save_selected は選択の QModelIndex を先に取り、保存先ダイアログを閉じた
あとでその行番号からメッセージを引いていた。上限到達中はメッセージが
来るたびに先頭行が消えて行番号がずれるので、ダイアログを開いている間の
受信で別の行が保存される。実測: 選択は message-B、ファイルは message-C、
画面のハイライトは B のまま。

ダイアログを出す前にメッセージそのものを確定してから書く。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _Incoming:
    """受信側 SyslogMessage の最小形（panel.add_message が読む属性だけ）。"""

    def __init__(self, text):
        self.timestamp = "2026-09-09 14:12:02"
        self.hostname = "rtr"
        self.level = "Info"
        self.message = text
        self.raw_message = "<134>Sep  9 14:12:02 rtr " + text
        self.source_ip = "192.0.2.1"
        self.source_display = "192.0.2.1 (UDP/514)"


class SyslogSaveSelectedStableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_saves_the_row_that_was_selected_even_if_rows_scroll_off(self):
        from ui.syslog_panel import SyslogPanel
        panel = SyslogPanel()
        panel.model.max_messages = 3
        for t in ("message-A", "message-B", "message-C"):
            panel.add_message(_Incoming(t))
        self.app.processEvents()
        panel.table_view.selectRow(1)  # message-B
        selected = panel.table_view.selectionModel().selectedRows()
        self.assertEqual(panel.model.get_message(
            panel.proxy_model.mapToSource(selected[0]).row()).message, "message-B")

        out = os.path.join(tempfile.mkdtemp(prefix="netbelt-syslog-sel-"), "sel.txt")

        def dialog_during_which_a_message_arrives(*a, **k):
            # ダイアログを開いている間に上限で先頭行 (A) が押し出される
            panel.add_message(_Incoming("message-D"))
            self.app.processEvents()
            return out, "テキストファイル (*.txt)"

        with mock.patch("ui.syslog_panel.QFileDialog.getSaveFileName",
                        side_effect=dialog_during_which_a_message_arrives), \
             mock.patch("ui.syslog_panel.QMessageBox.information"), \
             mock.patch("ui.syslog_panel.QMessageBox.critical"), \
             mock.patch("ui.syslog_panel.QMessageBox.warning"):
            panel._save_selected()

        with open(out, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("message-B", content,
                      "選択した行ではなく別の行が保存された: %r" % content)
        self.assertNotIn("message-C", content)


if __name__ == "__main__":
    unittest.main()
