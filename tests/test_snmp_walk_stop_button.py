"""GET/WALK の実行中に画面から止められ、止めた結果が途中までとして残ることを検証する。

何が起きていたか（実測、基準 62357d1）: GET/WALK タブのボタンは GET・WALK・
エクスポート・クリアだけで、停止操作が無かった（SNMPManager.cancel_operation
の製品側の呼び出しは終了処理だけ。「クリア」は表示を消すだけ）。行をゆっくり
返すサブツリーを WALK すると、終わるまで後続の GET/WALK はすべて「既に操作が
実行中です」で断られ、アプリを閉じる以外に抜けられない。さらに、取消フラグを
立てたワーカーは何も出さずに終わるため、止める操作を足すだけでは、3 行取れて
いた WALK でも表示は「WALK実行中...」のまま、表は 0 行だった。

利用者の決定（2026-09-20、snmp-04 / D13）:
  - GET/WALK の実行中は「停止」ボタンを出す。
  - 止めたら、そこまでに取れた行を「途中まで（利用者が中断）」として表示し、
    書き出しでも途中までであることを記録する（部分結果の書き出し
    complete=false と同じ扱い）。
  - 取り消しは次の応答を待ってから効く（最大 6 秒ほど）。その間は
    「停止中…」と出し、新しい要求は既存どおり断る。

どう実装したか（このファイルはパネルの側）: GET・WALK ボタンの隣に「停止」
ボタンを置き、要求が受理されたときだけ出して、結果・失敗・取り消しの
どれかが届いたら隠す（Trap 受信の停止ボタンと同じ出し方）。押すと
SNMPManager.request_cancel() で待たずに取り消しを頼み、ボタンを無効にして
「停止中…」を出す。operation_cancelled で届いた行は表へ出し、理由
「利用者が中断」を途中までの理由として持つので、書き出しは既存の部分結果と
同じく complete=false・partial_reason 付きになる。GET・WALK ボタンは押せる
ままにして、停止中の新しい要求はマネージャが既存どおり断る。

ここでは実際の SNMPManager を使い、nextCmd / getCmd だけを差し替える。
差し替えた nextCmd は 3 行返したところで止まり、合図を受けてから次の応答を
返す（利用者が停止を押すのはこの待ちの間）。
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

HOST = "127.0.0.1"
BASE = "1.3.6.1.4.1.99999.1"
TIMEOUT = "No SNMP response received before timeout"


def _row(index):
    from pysnmp.proto.rfc1902 import ObjectName, OctetString
    return (ObjectName("%s.%d" % (BASE, index)),
            OctetString("row-%d" % index))


class _GatedCmd:
    """before 行返したところで止まり、release の合図で続きを返す nextCmd / getCmd。

    before=0 なら最初の応答の前で止まる（応答待ちの GET を模す）。
    error があれば、続きの行のあとに errorIndication を返す。
    """

    def __init__(self, before=3, rows=8, error=None):
        self.before = before
        self.rows = rows
        self.error = error
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
            if self.error is not None:
                yield (self.error, None, None, [])
        return generate()


class SnmpWalkStopButtonTest(unittest.TestCase):
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
        # nextCmd / getCmd はワーカーを手放すまで差し替えたままにする
        self.gate = _GatedCmd()
        self.errors = []
        self.warnings = []
        for patch in (
                mock.patch("core.snmp_manager.nextCmd",
                           side_effect=lambda *a, **k: self.gate(*a, **k)),
                mock.patch("core.snmp_manager.getCmd",
                           side_effect=lambda *a, **k: self.gate(*a, **k)),
                # モーダルで止まらないよう、失敗と警告は記録だけする
                mock.patch("ui.snmp_panel.QMessageBox.critical",
                           side_effect=lambda parent, title, text, *a, **k:
                           self.errors.append(text)),
                mock.patch("ui.snmp_panel.QMessageBox.warning",
                           side_effect=lambda parent, title, text, *a, **k:
                           self.warnings.append(text))):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._drain)

    def _drain(self):
        """止めた nextCmd を流し切り、ワーカーを手放してから片付ける。"""
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

    def _stop_button(self):
        self.assertTrue(hasattr(self.panel, "stop_button"),
                        "GET/WALK を止めるボタンが無い")
        return self.panel.stop_button

    def _start(self, button):
        """button（GET / WALK）を押し、止まる所まで進むのを待つ。"""
        button.click()
        self.assertIsNotNone(self.manager.worker, "要求が受理されていない")
        self.assertTrue(self.gate.reached.wait(5), "止まる所まで進まない")

    def _finish(self):
        """次の応答を返し、ワーカーが手放されるまで待つ。"""
        self.gate.release.set()
        self.assertTrue(
            self._pump_until(lambda: self.manager.worker is None, 10),
            "操作が終わらない")

    def _export_json(self):
        """保存ダイアログを差し替えて、画面の書き出しから JSON を得る。"""
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-snmp-stop-"),
                            "out.json")
        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        return_value=(path, "")), \
                mock.patch("ui.snmp_panel.QMessageBox.information"):
            self.panel._on_export_clicked()
        self.assertEqual(self.errors, [], "書き出しに失敗した")
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_the_stop_button_is_shown_only_while_a_walk_runs(self):
        """停止ボタンは実行中だけ出て、完走したら隠れること。"""
        stop = self._stop_button()
        self.assertTrue(stop.isHidden(), "何も走っていないのに停止ボタンが出ている")

        self._start(self.panel.walk_button)
        self.assertFalse(stop.isHidden(), "WALK の実行中に停止ボタンが出ていない")
        self.assertTrue(stop.isEnabled())

        self._finish()
        self.assertEqual(self.panel.status_label.text(), "完了: 8件")
        self.assertTrue(stop.isHidden(), "完走したのに停止ボタンが残っている")

    def test_the_stop_button_is_shown_while_a_get_runs(self):
        """GET の実行中にも停止ボタンが出ること。"""
        self.gate = _GatedCmd(before=0, rows=1)
        stop = self._stop_button()
        self._start(self.panel.get_button)
        self.assertFalse(stop.isHidden(), "GET の実行中に停止ボタンが出ていない")
        self._finish()
        self.assertEqual(self.panel.status_label.text(), "完了: 1件")
        self.assertTrue(stop.isHidden())

    def test_stopping_says_so_until_the_next_response(self):
        """押してから次の応答までは『停止中…』で、新しい要求は断ること。"""
        stop = self._stop_button()
        self._start(self.panel.walk_button)

        stop.click()
        shown = self.panel.status_label.text()
        self.assertIn("停止中…", shown, "止めている最中だと分からない: %r" % shown)
        self.assertFalse(stop.isEnabled(), "停止中にもう一度押せる")

        self.panel.get_button.click()
        self.assertEqual(self.warnings, ["既に操作が実行中です"],
                         "停止中の新しい要求が断られていない")
        self.assertEqual(self.panel.status_label.text(), shown,
                         "断られた要求が『停止中…』を上書きした")

    def test_a_stopped_walk_shows_the_rows_collected_so_far(self):
        """止めた WALK は、取れた 3 行を『途中まで（利用者が中断）』で出すこと。"""
        stop = self._stop_button()
        self._start(self.panel.walk_button)
        stop.click()
        self._finish()

        self.assertEqual(self.errors, [], "止めた WALK がエラーになった")
        self.assertEqual(self.panel.result_model.rowCount(), 3,
                         "そこまでに取れた行が表に出ていない")
        shown = self.panel.status_label.text()
        self.assertIn("途中まで（利用者が中断）", shown,
                      "利用者が止めた途中までの結果だと分からない: %r" % shown)
        self.assertIn("3件", shown)
        self.assertTrue(stop.isHidden(), "止まったのに停止ボタンが残っている")
        self.assertTrue(stop.isEnabled(), "次の操作で停止ボタンが押せない")

    def test_a_stopped_walk_is_saved_as_incomplete(self):
        """止めた WALK の書き出しは complete=false で、理由が残ること。"""
        self._start(self.panel.walk_button)
        self._stop_button().click()
        self._finish()

        data = self._export_json()
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["host"], HOST)
        self.assertIs(data["complete"], False,
                      "止めた途中までの結果が完走として保存された")
        self.assertEqual(data["partial_reason"], "利用者が中断")

    def test_a_stopped_get_that_timed_out_is_not_an_error(self):
        """応答待ちの GET を止めてタイムアウトしても、エラーを出さないこと。"""
        # 前の結果が表にある状態から始める
        self.panel.result_model.set_results(
            [("1.3.6.1.2.1.1.5.0", "OctetString", "old")])
        self.gate = _GatedCmd(before=0, rows=0, error=TIMEOUT)
        self._start(self.panel.get_button)
        self._stop_button().click()
        self._finish()

        self.assertEqual(self.errors, [],
                         "止めた GET のタイムアウトがエラーとして出た")
        self.assertEqual(self.panel.result_model.rowCount(), 0,
                         "前の結果が止めた GET の結果として残っている")
        self.assertIn("途中まで（利用者が中断）: 0件",
                      self.panel.status_label.text())

    def test_the_stop_button_is_hidden_after_a_failure(self):
        """失敗で終わったときも停止ボタンを隠すこと。"""
        self.gate = _GatedCmd(before=0, rows=0, error=TIMEOUT)
        stop = self._stop_button()
        self._start(self.panel.walk_button)
        self._finish()

        self.assertEqual(len(self.errors), 1, "失敗が知らされていない")
        self.assertTrue(stop.isHidden(), "失敗したのに停止ボタンが残っている")

    def test_a_completed_walk_after_a_stop_is_complete_again(self):
        """止めたあとに完走した WALK は、途中まで扱いを引きずらないこと。"""
        self._start(self.panel.walk_button)
        self._stop_button().click()
        self._finish()

        self.gate = _GatedCmd()
        self._start(self.panel.walk_button)
        self._finish()

        self.assertEqual(self.panel.status_label.text(), "完了: 8件")
        data = self._export_json()
        self.assertIs(data["complete"], True)
        self.assertIsNone(data["partial_reason"])


if __name__ == "__main__":
    unittest.main()
