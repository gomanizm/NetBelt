"""結果が届く前に次の要求を受け付けてしまう窓を塞いだことを検証する。

SNMPManager の受理判定は worker.isRunning() だけだった。result_ready は
run() の中から queued 接続で emit されるため、スレッドが終わってから結果が
メインスレッドへ配送されるまでの間は「isRunning() == False かつ結果は未配送」
になる。この窓で次の要求を受け付けると、パネルは要求時のホストを上書きし、
あとから届いた前の結果に次のホストが付いて表示・保存される。

ワーカーの参照が解放されるのは finished（run() が返った後）を受けたときで、
未配送の結果がある間は必ず None ではない。受理判定をそちらへ揃える。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

HOST_A = "192.0.2.10"
HOST_B = "192.0.2.99"
OID = "1.3.6.1.2.1.1.5.0"
ROWS = [(OID, "OctetString", "router-a")]


def _fake_run(worker):
    """実際の SNMP 通信をせずに結果だけ emit する run() の差し替え。"""
    worker.result_ready.emit(True, list(ROWS))


class SnmpPendingResultWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _drain(self, manager):
        """残っているキューを流し切ってからワーカーを手放す。"""
        for _ in range(5):
            self.app.processEvents()
        worker = getattr(manager, "worker", None)
        if worker is not None:
            worker.wait(5000)
        for _ in range(5):
            self.app.processEvents()

    def _manager(self):
        from core.snmp_manager import SNMPManager
        manager = SNMPManager()
        self.addCleanup(self._drain, manager)
        return manager

    def test_a_request_is_refused_while_a_result_is_still_undelivered(self):
        from core import snmp_manager as sm

        manager = self._manager()
        with mock.patch.object(sm.SNMPWorker, "run", _fake_run):
            self.assertTrue(manager.snmp_get(HOST_A, [OID]))
            first = manager.worker
            self.assertTrue(first.wait(5000), "ワーカースレッドが終わらない")
            # ここまでイベントを回していないので result_ready は未配送のまま
            self.assertFalse(first.isRunning(), "スレッドがまだ動いている")

            accepted = manager.snmp_get(HOST_B, [OID])

        self.assertFalse(accepted,
                         "前の結果が未配送なのに次の要求を受け付けた")
        self.assertIs(manager.worker, first,
                      "未配送の結果を持つワーカーを差し替えた")

    def test_the_next_request_is_accepted_once_the_result_was_delivered(self):
        """対照: 結果が配送されきれば、次の要求は普通に受理されること。"""
        from core import snmp_manager as sm

        manager = self._manager()
        delivered = []
        manager.operation_completed.connect(
            lambda ok, result: delivered.append((ok, result)))

        with mock.patch.object(sm.SNMPWorker, "run", _fake_run):
            self.assertTrue(manager.snmp_get(HOST_A, [OID]))
            self.assertTrue(manager.worker.wait(5000))
            for _ in range(5):
                self.app.processEvents()

            self.assertEqual(len(delivered), 1, "結果が配送されていない")
            self.assertIsNone(manager.worker, "finished 後も参照が残っている")
            self.assertTrue(manager.snmp_get(HOST_B, [OID]),
                            "配送済みなのに次の要求を断った")

    def test_a_late_result_keeps_the_host_it_came_from(self):
        """パネル側から見た実害: A の結果に B のホストが付かないこと。"""
        from core import snmp_manager as sm
        from ui.main_window import MainWindow

        window = MainWindow()
        self.addCleanup(window.close)
        panel = window.snmp_panel
        self.addCleanup(self._drain, panel.snmp_manager)
        panel.oid_edit.setText(OID)

        with mock.patch.object(sm.SNMPWorker, "run", _fake_run):
            panel.host_edit.setText(HOST_A)
            with mock.patch("ui.snmp_panel.QMessageBox"):
                panel._on_get_clicked()
            self.assertTrue(panel.snmp_manager.worker.wait(5000))

            # A の結果が未配送のまま、入力欄を B に変えてもう一度押す
            panel.host_edit.setText(HOST_B)
            with mock.patch("ui.snmp_panel.QMessageBox"):
                panel._on_get_clicked()

            for _ in range(5):
                self.app.processEvents()

        self.assertEqual(panel._result_host, HOST_A,
                         "A の結果に B のホストが付いている")


if __name__ == "__main__":
    unittest.main()
