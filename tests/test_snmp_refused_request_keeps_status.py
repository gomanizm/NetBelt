"""実行中に断られた GET/WALK が、状態表示を書き換えないことを検証する。

_on_get_clicked / _on_walk_clicked は、snmp_get / snmp_walk が要求を断って
False を返しても、status_label を「GET実行中...」「WALK実行中...」に
書き換えていた。断るときマネージャは error_occurred を出し、パネルは
「既に操作が実行中です」の警告（モーダル）を開く。警告を開いている間は
ネストしたイベントループで元の WALK の完了が届くので、完了表示になった
あと警告を閉じた時点で「実行中」に上書きされる。実測（本物の
QMessageBox）: 閉じる直前は「途中まで: 4件（No SNMP response received
before timeout のため中断。全部ではありません）」、閉じた後は
「GET実行中...」、実行中の操作は None。完走（完了: 8件）でも GET・WALK
どちらを押しても同じ。途中までの警告も隠れる。警告を開いている間に
終わらなくても、WALK 中に GET を押すと WALK の完了までは「GET実行中...」
になる（操作名の取り違え）。

直し方: 断られたら（False が返ったら）status_label に触れずに戻る。
「…実行中」は受理したときだけ出す。

ここでは実際の SNMPManager を使い、nextCmd / getCmd だけを差し替える。
差し替えた nextCmd は 1 行返したところで止まり、合図を受けてから残りを
返す（途中で切れる場合は errorIndication）。警告の代わりに、合図を出して
WALK の完了までイベントループを回す関数を差し込む。
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
REASON = "No SNMP response received before timeout"


def _row(index):
    from pysnmp.proto.rfc1902 import ObjectName, OctetString
    return (ObjectName("%s.%d" % (BASE, index)),
            OctetString("row-%d" % index))


class _GatedCmd:
    """1 行返したところで止まり、release の合図で続きを返す nextCmd / getCmd。"""

    def __init__(self, rows=8, error=None):
        self.rows = rows
        self.error = error
        self.release = threading.Event()

    def __call__(self, *args, **kwargs):
        def generate():
            yield (None, None, None, [_row(1)])
            self.release.wait(10)
            for index in range(2, self.rows + 1):
                yield (None, None, None, [_row(index)])
            if self.error is not None:
                yield (self.error, None, None, [])
        return generate()


class SnmpRefusedRequestKeepsStatusTest(unittest.TestCase):
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
        # nextCmd / getCmd はテストの終わり（ワーカーを手放した後）まで
        # 差し替えたままにする。ワーカーが呼ぶ前に戻すと本物が走る
        self.gate = _GatedCmd()
        self.errors = []
        for patch in (
                mock.patch("core.snmp_manager.nextCmd",
                           side_effect=lambda *a, **k: self.gate(*a, **k)),
                mock.patch("core.snmp_manager.getCmd",
                           side_effect=lambda *a, **k: self.gate(*a, **k)),
                # 失敗の通知（モーダル）で止まらないよう、記録だけする
                mock.patch("ui.snmp_panel.QMessageBox.critical",
                           side_effect=lambda parent, title, text, *a, **k:
                           self.errors.append(text))):
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

    def _start_walk(self):
        self.panel.walk_button.click()
        self.assertIsNotNone(self.manager.worker, "WALK が受理されていない")
        self.assertEqual(self.panel.status_label.text(), "WALK実行中...")

    def _warning_left_open_until_the_walk_ends(self, seen):
        """警告を開いたまま、元の WALK の完了を待つ利用者を模す。

        モーダルのネストしたイベントループの代わりに、警告の中で
        processEvents を回して完了を届ける。QEventLoop.exec() は使わない。
        先に走ったテストが exec() の外で QApplication.exit() を呼ぶと
        （test_pending_update_quit の見張りのタイマーが後から発火する）、
        以後の QEventLoop.exec() はすぐ戻ってしまう。
        """
        def warning(parent, title, text, *args, **kwargs):
            seen.append(text)
            self.gate.release.set()
            self._pump_until(lambda: self.manager.worker is None, 10)
            seen.append(self.panel.status_label.text())
        return warning

    def _press_while_walking(self, button, error=None):
        """WALK 中に button を押し、警告を開いている間に WALK を終わらせる。

        (警告を閉じる直前の表示, 閉じた後の表示) を返す。
        """
        self.gate = _GatedCmd(error=error)
        seen = []
        with mock.patch("ui.snmp_panel.QMessageBox.warning",
                        side_effect=self._warning_left_open_until_the_walk_ends(
                            seen)):
            self._start_walk()
            button.click()
        self._pump_until(lambda: self.manager.worker is None, 10)
        self.assertEqual(seen[:1], ["既に操作が実行中です"],
                         "実行中の要求が断られていない: %r" % seen)
        self.assertIsNone(self.manager.worker, "WALK が終わっていない")
        self.assertEqual(self.errors, [], "WALK が失敗した")
        return seen[-1], self.panel.status_label.text()

    def test_a_refused_get_does_not_overwrite_the_completed_walk(self):
        """警告を閉じたあとも『完了』のままであること（GET を押した場合）。"""
        inside, after = self._press_while_walking(self.panel.get_button)
        self.assertEqual(inside, "完了: 8件")
        self.assertEqual(after, "完了: 8件",
                         "断られた GET が完了表示を上書きした")

    def test_a_refused_walk_does_not_overwrite_the_completed_walk(self):
        """警告を閉じたあとも『完了』のままであること（WALK を押した場合）。"""
        inside, after = self._press_while_walking(self.panel.walk_button)
        self.assertEqual(inside, "完了: 8件")
        self.assertEqual(after, "完了: 8件",
                         "断られた WALK が完了表示を上書きした")

    def test_a_refused_get_does_not_hide_the_partial_warning(self):
        """途中までの警告が、断られた GET で消されないこと。"""
        inside, after = self._press_while_walking(self.panel.get_button,
                                                  error=REASON)
        self.assertIn("途中まで", inside)
        self.assertEqual(after, inside,
                         "途中までの警告が『実行中』で消された")
        self.assertEqual(self.panel._last_partial_reason, REASON)

    def test_a_refused_get_does_not_rename_the_running_walk(self):
        """WALK 中に断られた GET が、表示を『GET実行中』にしないこと。"""
        with mock.patch("ui.snmp_panel.QMessageBox.warning") as warning:
            self._start_walk()
            self.panel.get_button.click()
        self.assertTrue(warning.called, "実行中の GET が断られていない")
        self.assertEqual(self.panel.status_label.text(), "WALK実行中...",
                         "実行中の操作名が取り違えられた")
        self.gate.release.set()
        self._pump_until(lambda: self.manager.worker is None, 10)
        self.assertEqual(self.panel.status_label.text(), "完了: 8件")

    def test_an_accepted_get_still_shows_that_it_is_running(self):
        """受理された GET は、これまでどおり『GET実行中』を出すこと。"""
        self.gate = _GatedCmd(rows=1)
        self.panel.get_button.click()
        self.assertIsNotNone(self.manager.worker, "GET が受理されていない")
        self.assertEqual(self.panel.status_label.text(), "GET実行中...")
        self._pump_until(lambda: self.manager.worker is None, 10)
        self.assertEqual(self.errors, [], "GET が失敗した")
        self.assertEqual(self.panel.status_label.text(), "完了: 1件")


if __name__ == "__main__":
    unittest.main()
