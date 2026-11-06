# -*- coding: utf-8 -*-
"""SNMP パネルの Trap 受信まわりの 3 点を検証する。

* 受信中も Trap のポートの spinbox を編集でき、「FW 許可」は spinbox の値を
  使っていた。受信中に値を変えてから押すと、待ち受けていないポートの規則を
  足して「完了」と出た。受信中は spinbox を無効にし、FW 許可は受信中の
  ポートを使う。
* 名前解決器を作れない（ログへ書けない状態で MIB 読み込みの print が失敗
  する等）と、届いた Trap ごとに _add_trap_to_tree が未捕捉例外になり、
  一覧に 1 件も入らなかった。解決器が無いときは生の OID を出す。
* _trap_display_time の docstring が、実測と合わない「モーダルダイアログを
  開いている間」を GUI が滞留する例に挙げていた。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

from conftest import free_udp_port   # noqa: E402

TRAP = {
    'received_at': '2026-11-06T12:00:00',
    'source_ip': '192.0.2.1',
    'source_port': 50000,
    'trap_oid': '1.3.6.1.6.3.1.1.5.3',
    'varbinds': [
        {'oid': '1.3.6.1.2.1.1.3.0', 'value': '1', 'type': 'TimeTicks'},
        {'oid': '1.3.6.1.2.1.2.2.1.1.3', 'value': '3', 'type': 'Integer'},
    ],
}


class SnmpPanelTrapPortAndResolverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.snmp_manager import SNMPManager
        from ui.snmp_panel import SNMPPanel
        fw = mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)
        with mock.patch.object(SNMPPanel, "_start_background_mib_loading"):
            self.panel = SNMPPanel()
        self.manager = SNMPManager()
        self.panel.set_snmp_manager(self.manager)
        self.addCleanup(self._dispose)

    def _dispose(self):
        from PyQt6.QtCore import QCoreApplication, QEvent
        self.manager.stop_trap_receiver()
        self._pump(lambda: not self.manager.is_trap_receiver_running())
        self.app.processEvents()
        self.panel.close()
        self.panel.deleteLater()
        self.manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self.app.processEvents()

    def _pump(self, condition, timeout=5):
        deadline = time.time() + timeout
        while not condition() and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        return condition()

    def _start_receiving(self):
        port = free_udp_port()
        self.panel.trap_port_spinbox.setValue(port)
        with mock.patch("ui.snmp_panel.QMessageBox.warning"):
            self.panel._on_trap_start_clicked()
        self.assertTrue(self.manager.is_trap_receiver_running())
        return port

    def test_the_port_cannot_be_edited_while_receiving(self):
        self.assertTrue(self.panel.trap_port_spinbox.isEnabled())
        self._start_receiving()
        self.assertFalse(self.panel.trap_port_spinbox.isEnabled())
        self.panel._on_trap_stop_clicked()
        self.assertTrue(self._pump(
            lambda: self.panel.trap_port_spinbox.isEnabled()),
            "停止しても spinbox が戻らない")

    def test_the_firewall_button_uses_the_receiving_port(self):
        port = self._start_receiving()
        self.panel.trap_port_spinbox.setValue(port + 1 if port < 65535 else port - 1)
        with mock.patch.object(self.manager, "fix_firewall",
                               return_value=(True, "ok")) as fix:
            self.panel._on_fw_allow()
        fix.assert_called_once_with(port)

    def test_the_firewall_button_uses_the_spinbox_when_idle(self):
        self.panel.trap_port_spinbox.setValue(1162)
        with mock.patch.object(self.manager, "fix_firewall",
                               return_value=(True, "ok")) as fix:
            self.panel._on_fw_allow()
        fix.assert_called_once_with(1162)

    def test_a_trap_is_listed_without_a_resolver(self):
        with mock.patch("ui.snmp_panel.get_resolver",
                        side_effect=OSError(28, "No space left on device")):
            self.panel._add_trap_to_tree(dict(TRAP))
        model = self.panel.trap_tree_model
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.item(0, 3).text(), TRAP['trap_oid'])
        child = model.item(0, 0).child(0, 3)
        self.assertEqual(child.text(), '1.3.6.1.2.1.2.2.1.1.3')

    def test_a_txt_export_works_without_a_resolver(self):
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-snmp-txt-"), "t.txt")
        trap = {'timestamp': '2026-11-06 12:00:00', 'source_ip': '192.0.2.1',
                'source_port': 50000, 'security': 'v2c',
                'trap_oid': TRAP['trap_oid'], 'varbinds': TRAP['varbinds']}
        with mock.patch("ui.snmp_panel.get_resolver",
                        side_effect=OSError(28, "No space left on device")):
            self.panel._export_to_txt(path, [trap])
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Trap OID: %s" % TRAP['trap_oid'], text)
        self.assertIn("1.3.6.1.2.1.2.2.1.1.3", text)

    def test_the_display_time_docstring_matches_the_measurement(self):
        from ui.snmp_panel import SNMPPanel
        self.assertNotIn("モーダルダイアログを開いている間",
                         SNMPPanel._trap_display_time.__doc__)


if __name__ == "__main__":
    unittest.main()
