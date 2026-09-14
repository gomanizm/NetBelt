"""Syslog エクスポートの形式判定が拡張子の大文字小文字に引きずられないことを確認する。

SNMP 側は tests/test_snmp_export_extension_case.py で
os.path.splitext()[1].lower() へ直したが、隣の syslog エクスポートは
`if filename.endswith('.json')` のままだった。endswith は大小を区別する
ので、out.JSON を選ぶと中身はテキスト形式で書かれ、しかも
「エクスポートしました」と成功扱いになる。

実測: out.JSON の中身の先頭が
      '2026-01-01 00:00:00 192.0.2.5 sw1 [info] hello\\n' で、
      json.loads すると Extra data: line 1 column 5 (char 4)。

ファイルダイアログはフィルタに従って拡張子を小文字で補うので、ここへ
来るのは利用者が自分で .JSON と打ったときと、大文字名の既存ファイルを
選び直したとき。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _Incoming:
    """SyslogReceiver が渡してくる形に合わせた最小の受信メッセージ"""

    def __init__(self):
        self.timestamp = "2026-09-09 14:12:02"
        self.hostname = "rtr1"
        self.level = "Info"
        self.message = "link down"
        self.raw_message = "<134>Sep  9 14:12:02 rtr1 link down"
        self.source_ip = "192.0.2.1"
        self.source_display = "192.0.2.1 (UDP/514)"


class SyslogExportExtensionCaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.syslog_panel import SyslogPanel
        self.panel = SyslogPanel()
        self.addCleanup(self.panel.close)
        self.panel.add_message(_Incoming())
        self.app.processEvents()
        self.dir = tempfile.mkdtemp(prefix="netbelt-syslog-extcase-")

    def _export(self, name):
        """保存ダイアログで name を選んだことにして書き出し、中身を返す。"""
        path = os.path.join(self.dir, name)
        with mock.patch("ui.syslog_panel.QFileDialog.getSaveFileName",
                        return_value=(path, "")), \
             mock.patch("ui.syslog_panel.QMessageBox.information"), \
             mock.patch("ui.syslog_panel.QMessageBox.critical"), \
             mock.patch("ui.syslog_panel.QMessageBox.warning"):
            self.panel._export_messages()
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_uppercase_json_is_json(self):
        text = self._export("out.JSON")
        data = json.loads(text)
        self.assertEqual(data[0]["message"], "link down")

    def test_mixed_case_json_is_json(self):
        text = self._export("out.Json")
        data = json.loads(text)
        self.assertEqual(data[0]["hostname"], "rtr1")

    def test_lowercase_json_still_is_json(self):
        """小文字の従来どおりの経路を壊していないこと。"""
        text = self._export("out.json")
        data = json.loads(text)
        self.assertEqual(len(data), 1)

    def test_txt_still_is_text(self):
        """既定の txt（および未知の拡張子）はこれまでどおりテキスト。"""
        text = self._export("out.txt")
        self.assertIn("[Info] link down", text)

    def test_json_only_inside_the_name_stays_text(self):
        """名前の途中の .json で JSON にしないこと（拡張子は末尾のみ）。"""
        text = self._export("out.json.txt")
        self.assertIn("[Info] link down", text)


if __name__ == "__main__":
    unittest.main()
