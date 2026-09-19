"""取り消した GET/WALK が、そこまでに取れた行をシグナルで知らせることを検証する。

何が起きていたか（実測、基準 62357d1）: SNMPWorker には取消フラグ
（cancel()）があるが、取り消したワーカーは result_ready も partial_result も
出さずに終わる。実際の SNMPManager とパネルをつなぎ、3 行返したところで
止まる nextCmd で WALK を始めて worker.cancel() を呼び、次の応答を返すと、
ワーカーは手放されるのに、パネルの表示は「WALK実行中...」のまま、表は
0 行、operation_completed / operation_partial はどちらも 1 度も出なかった。
取れていた 3 行はどこにも届かない。しかも取り消しの入口は
cancel_operation() だけで、これは最大 5 秒スレッドの終了を待つ（製品側の
呼び出しは終了処理だけ）。画面の操作から呼ぶと GUI が固まる。

利用者の決定（2026-09-20、snmp-04 / D13）: GET/WALK の実行中は停止操作を
出し、止めたらそこまでに取れた行を「途中まで（利用者が中断）」として表示し、
書き出しでも途中までであることを記録する。取り消しは次の応答を待ってから
効き（最大 6 秒ほど）、その間は「停止中…」と出して新しい要求は既存どおり
断る。取り消しをパネルへ知らせる経路（シグナル）を足す。

どう実装したか（このファイルはワーカーとマネージャの側）:
  - SNMPWorker に cancelled(object) を足した。取り消されて終わったときは、
    result_ready の代わりにこれで「そこまでに取れた行」を渡す。取り消した
    あとの失敗（応答待ちのタイムアウト等）もエラーにせず、ここまでの行
    （GET なら空）を渡す。
  - SNMPManager に operation_cancelled(object) を足してシグナル同士で中継し、
    待たずに取り消しを頼む request_cancel() を足した（cancel_operation() は
    終了処理用に待つまま）。
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

BASE = "1.3.6.1.4.1.99999.1"
TIMEOUT = "No SNMP response received before timeout"


def _row(index):
    from pysnmp.proto.rfc1902 import ObjectName, OctetString
    return (ObjectName("%s.%d" % (BASE, index)),
            OctetString("row-%d" % index))


def _walk_then_cancel(worker_ref, rows_before=3, rows_after=5):
    """rows_before 行返したあと、次の応答を待つ間に取り消される nextCmd。"""
    def fake(*args, **kwargs):
        for index in range(1, rows_before + 1):
            yield (None, None, None, [_row(index)])
        # 利用者が停止を押した（次の応答を待っている間）
        worker_ref[0].cancel()
        for index in range(rows_before + 1, rows_before + rows_after + 1):
            yield (None, None, None, [_row(index)])
    return fake


class _GatedWalk:
    """3 行返したところで止まり、release の合図で続きを返す nextCmd。"""

    def __init__(self, rows=8):
        self.rows = rows
        self.reached = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args, **kwargs):
        def generate():
            for index in range(1, 4):
                yield (None, None, None, [_row(index)])
            self.reached.set()
            self.release.wait(10)
            for index in range(4, self.rows + 1):
                yield (None, None, None, [_row(index)])
        return generate()


class WorkerCancelSignalTest(unittest.TestCase):
    """ワーカー単体: 取り消されたら cancelled でそこまでの行を渡すこと。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _run(self, operation, params, patch_name, fake_factory):
        """ワーカーを 1 回まわし、(result_ready, partial, cancelled) を返す。"""
        from core.snmp_manager import SNMPWorker
        worker = SNMPWorker(operation, params)
        ref = [worker]
        results, partials, cancels = [], [], []
        worker.result_ready.connect(lambda ok, r: results.append((ok, r)))
        worker.partial_result.connect(partials.append)
        if hasattr(worker, "cancelled"):
            worker.cancelled.connect(cancels.append)
        with mock.patch("core.snmp_manager." + patch_name, fake_factory(ref)):
            worker.run()
        return results, partials, cancels

    def test_a_cancelled_walk_hands_over_the_rows_collected_so_far(self):
        """取り消した WALK は、そこまでの 3 行を cancelled で渡すこと。"""
        results, partials, cancels = self._run(
            "walk", {"host": "192.0.2.1", "oid": BASE, "version": "v2c",
                     "community": "public"},
            "nextCmd", _walk_then_cancel)

        self.assertEqual(len(cancels), 1,
                         "取り消しが知らされていない（届いた結果: %r）" % results)
        self.assertEqual([row[2] for row in cancels[0]],
                         ["row-1", "row-2", "row-3"],
                         "そこまでに取れた行が渡っていない")
        self.assertEqual(results, [],
                         "取り消したのに完了として届いた")
        self.assertEqual(partials, [])

    def test_a_get_cancelled_before_a_timeout_is_not_an_error(self):
        """止めた GET の応答待ちがタイムアウトしても、エラーにしないこと。"""
        def fake_factory(ref):
            def fake(*args, **kwargs):
                def generate():
                    ref[0].cancel()   # 応答待ちの間に停止を押した
                    yield (TIMEOUT, None, None, [])
                return generate()
            return fake

        results, _, cancels = self._run(
            "get", {"host": "192.0.2.1", "oids": [BASE + ".1"],
                    "version": "v2c", "community": "public"},
            "getCmd", fake_factory)

        self.assertEqual(results, [],
                         "止めた GET のタイムアウトがエラーとして届いた")
        self.assertEqual(cancels, [[]], "取り消しが知らされていない")


class ManagerCancelRelayTest(unittest.TestCase):
    """実際の SNMPManager: 待たずに取り消しを頼め、行が中継されること。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.snmp_manager import SNMPManager
        self.manager = SNMPManager()
        self.gate = _GatedWalk()
        self.completed, self.cancelled, self.errors = [], [], []
        self.manager.operation_completed.connect(
            lambda ok, r: self.completed.append((ok, r)))
        self.manager.error_occurred.connect(self.errors.append)
        if hasattr(self.manager, "operation_cancelled"):
            self.manager.operation_cancelled.connect(self.cancelled.append)
        # ワーカーを手放すまで差し替えたままにする（戻すと本物が走る）
        patch = mock.patch("core.snmp_manager.nextCmd",
                           side_effect=lambda *a, **k: self.gate(*a, **k))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._drain)

    def _drain(self):
        self.gate.release.set()
        self._pump_until(lambda: self.manager.worker is None, 10)
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

    def _start_walk_and_wait_for_three_rows(self):
        self.assertTrue(self.manager.snmp_walk("127.0.0.1", BASE))
        self.assertTrue(self.gate.reached.wait(5), "WALK が 3 行目まで進まない")

    def test_request_cancel_returns_without_waiting_for_the_worker(self):
        """次の応答が来ないうちでも、取り消しの依頼はすぐ戻ること。"""
        self.assertTrue(hasattr(self.manager, "request_cancel"),
                        "画面から待たずに取り消しを頼む口が無い")
        self._start_walk_and_wait_for_three_rows()

        started = time.monotonic()
        accepted = self.manager.request_cancel()
        elapsed = time.monotonic() - started

        self.assertTrue(accepted, "実行中の WALK の取り消しを受け付けない")
        self.assertLess(elapsed, 1.0,
                        "取り消しの依頼が %.2f 秒 GUI を止めた" % elapsed)

    def test_the_manager_relays_the_rows_of_a_cancelled_walk(self):
        """止めた WALK の 3 行が operation_cancelled で届くこと。"""
        self.assertTrue(hasattr(self.manager, "request_cancel"),
                        "画面から待たずに取り消しを頼む口が無い")
        self._start_walk_and_wait_for_three_rows()
        self.manager.request_cancel()
        self.gate.release.set()   # 次の応答が届く
        self.assertTrue(
            self._pump_until(lambda: self.manager.worker is None, 10),
            "取り消した WALK が終わらない")

        self.assertEqual(len(self.cancelled), 1,
                         "取り消しがパネルへ知らされていない")
        self.assertEqual(len(self.cancelled[0]), 3,
                         "そこまでに取れた行が届いていない")
        self.assertEqual(self.completed, [],
                         "取り消したのに完了として届いた")

    def test_a_new_request_is_still_refused_while_stopping(self):
        """停止中（次の応答待ち）の新しい要求は、既存どおり断ること。"""
        self.assertTrue(hasattr(self.manager, "request_cancel"),
                        "画面から待たずに取り消しを頼む口が無い")
        self._start_walk_and_wait_for_three_rows()
        self.manager.request_cancel()

        accepted = self.manager.snmp_get("127.0.0.1", [BASE + ".1"])

        self.assertFalse(accepted, "停止中なのに次の要求を受け付けた")
        self.assertEqual(self.errors, ["既に操作が実行中です"])

    def test_nothing_to_cancel_when_idle(self):
        """対照: 何も走っていなければ、取り消しは受け付けないこと。"""
        self.assertTrue(hasattr(self.manager, "request_cancel"),
                        "画面から待たずに取り消しを頼む口が無い")
        self.assertFalse(self.manager.request_cancel())


if __name__ == "__main__":
    unittest.main()
