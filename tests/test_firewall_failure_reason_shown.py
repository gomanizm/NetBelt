"""ファイアウォール許可の失敗理由が、別の操作の成功メッセージに置き換わらないこと。

各サーバの fix_firewall はポートの許可と自exeの許可を続けて行い、ok は
すべての結果の論理積で正しく返すが、msg は操作の成否と関係なく決め打ちだった
（SFTP・TFTP・FTP は最初のポート許可の msg、Syslog は最後の自exe許可の msg）。
実測（core.firewall の内部だけを差し替え、fix_firewall とパネルは本物）:
A（ポート許可は既存ルールで成功、自exe は % を含むパスで失敗）では、SFTP パネルに
「未反映/失敗 (既存の許可ルールを使用: …)」と出て、% の理由は print にしか
出なかった。B（ポート許可は UAC 拒否、自exe は成功）では、Syslog のステータスが
「未反映/失敗 (自exe受信許可を追加(昇格): …)」になった。TFTP・FTP も最後の行は
同じ集約だった（各操作の行はログに出る）。

直し方: 結果をまとめる core.firewall.combine_results を足し、失敗した操作が
あればその理由を返す（複数なら連結）。すべて成功したときの文言は従来どおり。
fix_firewall 全体はモックせず、ensure_inbound_allow と ensure_self_program_allow
の片方だけを失敗させて、パネルの表示で確かめる。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

PORT_OK = "既存の許可ルールを使用: NetBelt - test (TCP/2222)"
PORT_FAIL = "UACが承認されず未追加: NetBelt - test (TCP/2222)"
SELF_OK = "自exe受信許可を追加(昇格): NetBelt - app inbound (self)"
SELF_FAIL = "実行ファイルのパスに % が含まれるため自exe受信許可を設定できません"


class FirewallFailureReasonShownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._receivers = []
        self._panels = []

    def tearDown(self):
        # 受信スレッドを止めてから、パネルを GUI スレッドで破棄する。
        # 参照が残ったまま別スレッドの GC で消えると、ウィジェットを
        # GUI 以外のスレッドで破棄することになりプロセスごと落ちる
        from PyQt6.QtCore import QCoreApplication, QEvent
        for receiver in self._receivers:
            receiver.stop()
        for panel in self._panels:
            panel.close()
            panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self.app.processEvents()
        self._panels.clear()
        self._receivers.clear()

    def _config(self):
        from core.config_manager import ConfigManager
        workdir = tempfile.mkdtemp(prefix="netbelt-fwreason-")
        return ConfigManager(os.path.join(workdir, "config.json"))

    def _keep(self, panel):
        self._panels.append(panel)
        return panel

    def _results(self, port_result, self_result):
        for target, value in (("core.firewall.ensure_inbound_allow", port_result),
                              ("core.firewall.ensure_self_program_allow", self_result)):
            p = mock.patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _last_log_line(self, panel):
        self.app.processEvents()
        return panel.log_text.toPlainText().splitlines()[-1]

    def _shown(self, kind):
        """パネルで「ファイアウォールで許可」を押し、結果として出た文言を返す。"""
        if kind == "sftp":
            from ui.sftp_server_panel import SFTPServerPanel
            panel = self._keep(SFTPServerPanel())
            panel._on_fw_allow()
            return self._last_log_line(panel)
        if kind == "tftp":
            from ui.tftp_server_panel import TFTPServerPanel
            panel = self._keep(TFTPServerPanel(config_manager=self._config()))
            panel._on_fw_allow()
            return self._last_log_line(panel)
        if kind == "ftp":
            from ui.ftp_server_panel import FTPServerPanel
            panel = self._keep(FTPServerPanel(config_manager=self._config()))
            panel._on_fw_allow()
            return self._last_log_line(panel)
        if kind == "syslog":
            from core.syslog_receiver import SyslogReceiver
            from ui.syslog_panel import SyslogPanel
            receiver = SyslogReceiver()
            self._receivers.append(receiver)
            self.assertTrue(receiver.start_protocol("UDP", 0))
            panel = self._keep(SyslogPanel())
            panel.set_syslog_receiver(receiver)
            panel._on_fw_allow()
            return panel.status_label.text()
        raise AssertionError(kind)

    KINDS = ("sftp", "syslog", "tftp", "ftp")

    def test_a_failed_self_program_rule_is_the_reason_shown(self):
        self._results((True, PORT_OK), (False, SELF_FAIL))
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                shown = self._shown(kind)
                self.assertIn("未反映/失敗", shown)
                self.assertIn(SELF_FAIL, shown,
                              "自exe許可の失敗理由が出ていない: %r" % shown)

    def test_a_failed_port_rule_is_the_reason_shown(self):
        self._results((False, PORT_FAIL), (True, SELF_OK))
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                shown = self._shown(kind)
                self.assertIn("未反映/失敗", shown)
                self.assertIn(PORT_FAIL, shown,
                              "ポート許可の失敗理由が出ていない: %r" % shown)

    def test_every_failure_is_shown_when_several_fail(self):
        self._results((False, PORT_FAIL), (False, SELF_FAIL))
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                shown = self._shown(kind)
                self.assertIn(PORT_FAIL, shown)
                self.assertIn(SELF_FAIL, shown)

    def test_the_success_message_is_unchanged(self):
        self._results((True, PORT_OK), (True, SELF_OK))
        expected = {"sftp": PORT_OK, "syslog": SELF_OK, "tftp": PORT_OK, "ftp": PORT_OK}
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                shown = self._shown(kind)
                self.assertIn("完了", shown)
                self.assertIn(expected[kind], shown)


if __name__ == "__main__":
    unittest.main()
