"""SNMP の「ファイアウォールで許可」で、失敗の理由が成功の文言に置き換わらないこと。

SNMPManager.fix_firewall はポート（SNMP Trap）の許可と自exeの許可を続けて行い、
ok は両方の論理積で返すが、msg は成否と関係なく最初のポート許可のものを
返していた（他のサーバは core.firewall.combine_results でまとめるように直した
が、SNMP だけ取りこぼしていた）。実測（core.firewall の 2 つの関数だけを
差し替え、fix_firewall とパネルは本物）: ポート許可は既存ルールで成功・自exe
は % を含むパスで失敗のとき、Trap の状態表示は「ファイアウォール許可:
未反映/失敗 (既存の許可ルールを使用: …)」で、失敗の理由は print にしか
出なかった。両方失敗したときも自exe の理由は出なかった。

直し方: 他のサーバと同じく combine_results で結果をまとめ、失敗した操作が
あればその理由を返す（複数なら連結）。すべて成功したときの文言は従来どおり
ポート許可の msg。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

PORT_OK = "既存の許可ルールを使用: NetBelt - SNMP Trap (UDP/162)"
PORT_FAIL = "UACが承認されず未追加: NetBelt - SNMP Trap (UDP/162)"
SELF_OK = "自exe受信許可を追加(昇格): NetBelt - app inbound (self)"
SELF_FAIL = "実行ファイルのパスに % が含まれるため自exe受信許可を設定できません"


class SnmpFirewallFailureReasonShownTest(unittest.TestCase):
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
        self.addCleanup(self._dispose)

    def _dispose(self):
        from PyQt6.QtCore import QCoreApplication, QEvent
        self.panel.close()
        self.panel.deleteLater()
        self.manager.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self.app.processEvents()

    def _shown(self, port_result, self_result):
        """パネルで「ファイアウォールで許可」を押し、Trap の状態表示を返す。"""
        with mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=port_result) as port, \
                mock.patch("core.firewall.ensure_self_program_allow",
                           return_value=self_result) as self_program:
            self.panel._on_fw_allow()
        self.assertEqual(port.call_count, 1)
        self.assertEqual(self_program.call_count, 1)
        return self.panel.trap_status_label.text()

    def test_a_failed_self_program_rule_is_the_reason_shown(self):
        shown = self._shown((True, PORT_OK), (False, SELF_FAIL))
        self.assertIn("未反映/失敗", shown)
        self.assertIn(SELF_FAIL, shown,
                      "自exe許可の失敗理由が出ていない: %r" % shown)
        self.assertNotIn(PORT_OK, shown)

    def test_a_failed_port_rule_is_the_reason_shown(self):
        shown = self._shown((False, PORT_FAIL), (True, SELF_OK))
        self.assertIn("未反映/失敗", shown)
        self.assertIn(PORT_FAIL, shown)

    def test_every_failure_is_shown_when_several_fail(self):
        shown = self._shown((False, PORT_FAIL), (False, SELF_FAIL))
        self.assertIn("未反映/失敗", shown)
        self.assertIn(PORT_FAIL, shown)
        self.assertIn(SELF_FAIL, shown,
                      "自exe許可の失敗理由が出ていない: %r" % shown)

    def test_the_success_message_is_unchanged(self):
        shown = self._shown((True, PORT_OK), (True, SELF_OK))
        self.assertEqual(shown, "ファイアウォール許可: 完了 (%s)" % PORT_OK)


if __name__ == "__main__":
    unittest.main()
