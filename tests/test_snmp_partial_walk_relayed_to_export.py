"""途中で切れた WALK の知らせが、実際の SNMPManager を通ってパネルの
表示と書き出しまで届くことを検証する。

製品は正しく動いているが、この中継を守るテストが無かった。
tests/test_snmp_partial_walk.py の「マネージャが部分結果の警告を中継する」
テストは operation_partial 属性があるかしか見ておらず、他の部分結果の
テストもワーカー単体か、パネルのスロットを直接呼ぶ形だった。変異実験
（snmp_walk の `self.worker.partial_result.connect(self.operation_partial)`
を消す）では、snmp を含むテスト 37 ファイルが 288 passed のまま、
tests/ 全体でもこの変異では 1 件も落ちなかった。そのとき localhost の
エージェントが 4 行返して黙る条件で実際に動かすと、表示は「完了: 4件」、
JSON の書き出しは complete=True・partial_reason=None になった（正しくは
「途中まで: 4件（…）」、complete=False）。途中までの結果が完走として
保存される回帰を、どのテストも検出できなかった。

ここでは実際の SNMPManager をパネルに set_snmp_manager し、nextCmd だけを
「数行返してから errorIndication を返す」ものに差し替えて WALK ボタンを
押す。完了を待ってから、保存ダイアログを差し替えた _on_export_clicked で
JSON に書き出し、complete=false と partial_reason を確かめる。
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

HOST = "127.0.0.1"
BASE = "1.3.6.1.4.1.99999.1"
REASON = "No SNMP response received before timeout"


def _rows_then(error, rows=4):
    """rows 行返したあと error（None なら何も）を返す nextCmd の代わり。"""
    from pysnmp.proto.rfc1902 import ObjectName, OctetString

    def fake(*args, **kwargs):
        for index in range(1, rows + 1):
            yield (None, None, None,
                   [(ObjectName("%s.%d" % (BASE, index)),
                     OctetString("row-%d" % index))])
        if error is not None:
            yield (error, None, None, [])
    return fake


class PartialWalkRelayedToExportTest(unittest.TestCase):
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
        self.errors = []
        # 失敗の通知（モーダル）で止まらないよう、記録だけする
        patch = mock.patch("ui.snmp_panel.QMessageBox.critical",
                           side_effect=lambda parent, title, text, *a, **k:
                           self.errors.append(text))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._drain)

    def _drain(self):
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

    def _walk(self, fake_next_cmd):
        """WALK ボタンを押し、結果がパネルに届くまで待つ。"""
        # ワーカーを手放すまで差し替えたままにする（戻すと本物が走る）
        with mock.patch("core.snmp_manager.nextCmd", fake_next_cmd):
            self.panel.walk_button.click()
            self.assertIsNotNone(self.manager.worker, "WALK が受理されていない")
            self.assertTrue(
                self._pump_until(lambda: self.manager.worker is None, 10),
                "WALK が終わらない")
        self.assertEqual(self.errors, [], "WALK が失敗した")

    def _export_json(self):
        """保存ダイアログを差し替えて、画面の書き出しから JSON を得る。"""
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-snmp-relay-"),
                            "out.json")
        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        return_value=(path, "")), \
                mock.patch("ui.snmp_panel.QMessageBox.information"):
            self.panel._on_export_clicked()
        self.assertEqual(self.errors, [], "書き出しに失敗した")
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_an_interrupted_walk_is_saved_as_incomplete(self):
        """途中で切れた WALK は、書き出しで complete=false になること。"""
        self._walk(_rows_then(REASON))
        data = self._export_json()
        self.assertEqual(data["count"], 4)
        self.assertIs(data["complete"], False,
                      "途中までの結果が完走として保存された")
        self.assertEqual(data["partial_reason"], REASON)

    def test_an_interrupted_walk_is_shown_as_incomplete(self):
        """途中で切れた WALK は、画面にも『途中まで』と出ること。"""
        self._walk(_rows_then(REASON))
        shown = self.panel.status_label.text()
        self.assertIn("途中まで: 4件", shown,
                      "途中までの結果が完了と表示された: %r" % shown)
        self.assertIn(REASON, shown)

    def test_a_complete_walk_is_saved_as_complete(self):
        """対照: 最後まで回れた WALK は complete=true のままであること。"""
        self._walk(_rows_then(None))
        self.assertEqual(self.panel.status_label.text(), "完了: 4件")
        data = self._export_json()
        self.assertIs(data["complete"], True)
        self.assertIsNone(data["partial_reason"])


if __name__ == "__main__":
    unittest.main()
