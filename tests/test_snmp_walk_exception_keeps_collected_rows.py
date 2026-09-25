"""WALK が例外で中断したときも、取れた行を捨てないことを検証する。

_perform_walk は errorIndication / errorStatus なら部分結果として残すが、
nextCmd の反復そのものが例外を投げた場合（WALK の途中で経路が落ちた、
ソケットが閉じられた）は、SNMPWorker.run() の except が
result_ready(False, str(e)) だけを出し、_collected に貯まった行を誰にも
渡していなかった。src/ui/snmp_panel.py の success=False 分岐は
result_model を触らないので、表は直前の結果のまま更新されず、status は
「エラー」。取れていた行は表示も保存もできない。

実測（基準 f4cad23）:
- 3 行 yield してから OSError を投げる nextCmd に差し替えて WALK:
  result_ready=[(False, '[Errno 10054] transport closed')] / partial=[] /
  cancelled=[] / _collected=3 行（誰にも渡らない）。
- 本物の pysnmp で 127.0.0.1 の最小エージェントへ WALK し、5 回目の送信
  だけ OSError(ENETUNREACH) にすると、collected 4 行に対し
  result_ready=(False, 'poll error: Traceback ...')。同じ「WALK 中の
  通信断」でも errorIndication 経路なら残る 4 行が、例外経路では全部消える。

直し方: run() の except（取り消しでない側）で _collected が空でなければ、
既存の部分結果の道へ合流させる（partial_result → result_ready(True, 行)）。
空のときだけ、これまでどおり result_ready(False, str(e))。利用者の決定
（2026-09-20、部分結果は捨てずに「途中まで」と明記）の適用範囲を例外経路
へ広げるだけなので、失敗のモーダルは出さず status の「途中まで」に寄せる。
理由の文字列は 1 行に切り詰める（pysnmp の poll error は str(e) が
トレースバック丸ごとで実測 2097 バイト。そのまま status ラベルへ入れると
読めない）。
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

BASE = "1.3.6.1.4.1.65001.1"

# pysnmp の dispatcher が投げる PySnmpError を模した本文。実測と同じく
# traceback.format_exception() の各行を ';' で継いだ長い複数行になる
LONG_TRACEBACK = "poll error: Traceback (most recent call last):\n" + (
    ";  File \"pysnmp/carrier/asyncore/dgram/base.py\", line 148,"
    " in handle_write\n    self._sendto(\n") * 20 + (
    ";OSError: [Errno 10051] network is unreachable\n")


class _Pretty:
    """pysnmp の値を模す（prettyPrint と型名だけ持つ）。"""

    def __init__(self, text, type_name=None):
        self._text = text
        if type_name:
            self.__class__ = type(type_name, (_Pretty,), {})

    def prettyPrint(self):
        return self._text


class _VarBind:
    def __init__(self, oid, value, type_name):
        self._oid = _Pretty(oid)
        self._value = _Pretty(value, type_name)

    def __getitem__(self, index):
        return self._oid if index == 0 else self._value


def _rows_then_raise(error, rows=3, on_last=None):
    """rows 行 yield したあと error を投げる nextCmd の代わり。

    on_last を渡すと、最後の行を返した直後に呼ぶ（取り消しの再現用）。
    """
    def fake(*args, **kwargs):
        for index in range(1, rows + 1):
            yield (None, None, None,
                   [_VarBind("%s.%d" % (BASE, index), "row-%d" % index,
                             "OctetString")])
        if on_last is not None:
            on_last()
        raise error
    return fake


class SnmpWalkExceptionKeepsCollectedRowsTest(unittest.TestCase):
    """ワーカー単体: 例外で中断しても取れた行が出口へ届くこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _run_walk(self, fake_next_cmd):
        """WALK を1回まわし、(result_ready, partial, cancelled) を返す。"""
        from core.snmp_manager import SNMPWorker
        worker = SNMPWorker("walk", {
            "host": "192.0.2.1", "port": 161, "oid": BASE,
            "version": "v2c", "community": "public",
        })
        self.worker = worker
        results, partials, cancels = [], [], []
        worker.result_ready.connect(lambda ok, r: results.append((ok, r)))
        worker.partial_result.connect(partials.append)
        worker.cancelled.connect(cancels.append)
        with mock.patch("core.snmp_manager.nextCmd", fake_next_cmd):
            worker.run()
        return results, partials, cancels

    def test_rows_collected_before_the_exception_are_delivered(self):
        """例外で中断しても、そこまでの行を結果として渡すこと。"""
        results, _, cancels = self._run_walk(
            _rows_then_raise(OSError("[Errno 10054] transport closed")))

        self.assertEqual(cancels, [], "取り消していないのに cancelled が出た")
        self.assertEqual(len(results), 1, "結果が 1 度ではない: %r" % (results,))
        ok, rows = results[0]
        self.assertTrue(ok, "取れた行があるのにエラー扱いにしている")
        self.assertEqual([row[0] for row in rows],
                         ["%s.%d" % (BASE, i) for i in (1, 2, 3)],
                         "取得済みの行が捨てられている")

    def test_the_interruption_is_reported_as_partial(self):
        """黙って部分結果を渡さず、途中であることを伝えること。"""
        _, partials, _ = self._run_walk(
            _rows_then_raise(OSError("[Errno 10054] transport closed")))

        self.assertEqual(len(partials), 1,
                         "途中で切れたことが伝わっていない: %r" % (partials,))
        self.assertIn("transport closed", partials[0],
                      "何が起きたのか分からない文面: %r" % (partials[0],))

    def test_a_long_traceback_is_cut_down_to_one_line(self):
        """理由が長大なトレースバックでも、1 行に切り詰めること。"""
        from pysnmp.error import PySnmpError
        _, partials, _ = self._run_walk(
            _rows_then_raise(PySnmpError(LONG_TRACEBACK)))

        self.assertEqual(len(partials), 1, "部分結果として扱われていない")
        reason = partials[0]
        self.assertNotIn("\n", reason,
                         "改行入りの理由が status ラベルへ流れる: %r" % (reason,))
        self.assertLessEqual(len(reason), 200,
                             "理由が長すぎる（%d 文字）" % len(reason))
        self.assertIn("poll error", reason,
                      "切り詰めで手掛かりまで消えている: %r" % (reason,))

    def test_an_exception_with_nothing_collected_is_still_an_error(self):
        """1 件も取れていないなら、これまでどおりエラーにすること（据え置き）。"""
        results, partials, _ = self._run_walk(
            _rows_then_raise(OSError("connection refused"), rows=0))

        self.assertEqual(partials, [], "何も取れていないのに部分結果にしている")
        self.assertEqual(len(results), 1)
        ok, message = results[0]
        self.assertFalse(ok, "何も取れていないのに成功扱いにしている")
        self.assertIn("connection refused", str(message))

    def test_a_cancelled_walk_still_goes_out_as_cancelled(self):
        """取り消し済みなら、これまでどおり cancelled で渡すこと（据え置き）。"""
        holder = {}

        def cancel_now():
            holder["worker"].cancel()

        fake = _rows_then_raise(OSError("boom"), on_last=cancel_now)
        from core.snmp_manager import SNMPWorker
        worker = SNMPWorker("walk", {
            "host": "192.0.2.1", "port": 161, "oid": BASE,
            "version": "v2c", "community": "public",
        })
        holder["worker"] = worker
        results, partials, cancels = [], [], []
        worker.result_ready.connect(lambda ok, r: results.append((ok, r)))
        worker.partial_result.connect(partials.append)
        worker.cancelled.connect(cancels.append)
        with mock.patch("core.snmp_manager.nextCmd", fake):
            worker.run()

        self.assertEqual(results, [], "取り消しなのに result_ready が出た")
        self.assertEqual(partials, [], "取り消しなのに部分結果の理由が出た")
        self.assertEqual(len(cancels), 1)
        self.assertEqual(len(cancels[0]), 3, "取り消し時の行が減っている")


class InterruptedWalkReachesTheUserTest(unittest.TestCase):
    """実物のマネージャとパネルで、行が表と書き出しまで届くこと。"""

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
        self.panel.host_edit.setText("127.0.0.1")
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

    def test_the_panel_shows_what_was_collected(self):
        """例外で切れても、取れた行が表に出ること。"""
        self._walk(_rows_then_raise(OSError("[Errno 10054] transport closed")))

        self.assertEqual(self.errors, [],
                         "部分結果があるのに失敗の通知が出た: %r" % (self.errors,))
        self.assertEqual(self.panel.result_model.rowCount(), 3,
                         "取れた行が表に出ていない")
        shown = self.panel.status_label.text()
        self.assertIn("途中まで: 3件", shown,
                      "途中までの結果だと分からない表示: %r" % (shown,))

    def test_the_rows_can_be_saved_as_incomplete(self):
        """表に出た行が保存でき、完走と区別できること。"""
        self._walk(_rows_then_raise(OSError("[Errno 10054] transport closed")))

        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-snmp-exc-"),
                            "out.json")
        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        return_value=(path, "")), \
                mock.patch("ui.snmp_panel.QMessageBox.information"):
            self.panel._on_export_clicked()
        self.assertEqual(self.errors, [], "書き出しに失敗した")
        with io.open(path, encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["count"], 3)
        self.assertIs(data["complete"], False,
                      "途中までの結果が完走として保存された")
        self.assertIn("transport closed", data["partial_reason"] or "")


if __name__ == "__main__":
    unittest.main()
