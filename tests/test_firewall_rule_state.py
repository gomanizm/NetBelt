"""firewall.py が「同名ルールがある」だけで許可済みと判定しないことを確認する。

rule_exists は `netsh ... show rule name=X` の returncode==0 だけを見ていた。
netsh は名前が一致すれば無効ルール・ブロックルール・送信ルールでも rc=0 を
返すので、ユーザーが Windows FW の UI/GPO で NetBelt のルールを無効化・
ブロック化していても「既存の許可ルールを使用」で完了扱いになる。
また admin 起動の自exe経路は netsh の失敗を見ず True、UAC 昇格経路は
6 回の確認に失敗しても「反映待ち」を True で返し、パネルは ok だけを見る
ので「完了」と表示していた（実測: A〜D いずれも True）。

netsh は実行しない（すべてモック）。show の出力はロケール依存なので
日本語・英語の両方を与える。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

JP_ENABLED_ALLOW = """
規則名:                               NetBelt - SFTP Server (TCP/2222)
----------------------------------------------------------------------
有効:                                 はい
方向:                                 入力
プロトコル:                           TCP
ローカル ポート:                      2222
操作:                                 許可
OK

""".encode("utf-8")

JP_DISABLED_ALLOW = JP_ENABLED_ALLOW.replace("有効:                                 はい".encode("utf-8"),
                                             "有効:                                 いいえ".encode("utf-8"))
JP_ENABLED_BLOCK = JP_ENABLED_ALLOW.replace("操作:                                 許可".encode("utf-8"),
                                            "操作:                                 ブロック".encode("utf-8"))

EN_ENABLED_ALLOW = b"""
Rule Name:                            NetBelt - SFTP Server (TCP/2222)
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Protocol:                             TCP
LocalPort:                            2222
Action:                               Allow
Ok.

"""
EN_DISABLED_ALLOW = EN_ENABLED_ALLOW.replace(b"Enabled:                              Yes",
                                             b"Enabled:                              No")
EN_ENABLED_BLOCK = EN_ENABLED_ALLOW.replace(b"Action:                               Allow",
                                            b"Action:                               Block")
NO_MATCH = "\n指定された条件に一致する規則はありません。\n\n".encode("utf-8")


class _CP:
    def __init__(self, rc, out=b""):
        self.returncode = rc
        self.stdout = out
        self.stderr = b""


def _netsh_router(show_result, other_rc=0):
    """show には show_result を、add/set/delete には other_rc を返すモック"""
    def _run(args):
        if args[:3] == ["advfirewall", "firewall", "show"]:
            return show_result
        return _CP(other_rc)
    return _run


class RuleExistsTest(unittest.TestCase):
    def setUp(self):
        import core.firewall as fw
        self.fw = fw
        p = mock.patch.object(fw, "is_windows", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def _exists(self, cp):
        with mock.patch.object(self.fw, "_netsh", side_effect=_netsh_router(cp)):
            return self.fw.rule_exists("NetBelt - SFTP Server (TCP/2222)")

    def test_no_match_is_false(self):
        self.assertFalse(self._exists(_CP(1, NO_MATCH)))

    def test_enabled_allow_is_true_in_both_locales(self):
        self.assertTrue(self._exists(_CP(0, JP_ENABLED_ALLOW)))
        self.assertTrue(self._exists(_CP(0, EN_ENABLED_ALLOW)))

    def test_disabled_rule_is_not_treated_as_allowed(self):
        self.assertFalse(self._exists(_CP(0, JP_DISABLED_ALLOW)),
                         "無効ルールを許可済みと判定している")
        self.assertFalse(self._exists(_CP(0, EN_DISABLED_ALLOW)))

    def test_block_rule_is_not_treated_as_allowed(self):
        self.assertFalse(self._exists(_CP(0, JP_ENABLED_BLOCK)),
                         "ブロックルールを許可済みと判定している")
        self.assertFalse(self._exists(_CP(0, EN_ENABLED_BLOCK)))

    def test_one_good_rule_among_bad_ones_is_true(self):
        self.assertTrue(self._exists(_CP(0, JP_DISABLED_ALLOW + JP_ENABLED_ALLOW)))

    def test_show_is_restricted_to_inbound(self):
        calls = []

        def _run(args):
            calls.append(args)
            return _CP(0, JP_ENABLED_ALLOW)
        with mock.patch.object(self.fw, "_netsh", side_effect=_run):
            self.fw.rule_exists("x")
        self.assertIn("dir=in", calls[0], "送信ルールまで一致させている")


class EnsureInboundAllowTest(unittest.TestCase):
    def setUp(self):
        import core.firewall as fw
        self.fw = fw
        for name, val in (("is_windows", True),):
            p = mock.patch.object(fw, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("time.sleep")
        p.start()
        self.addCleanup(p.stop)

    def test_admin_add_failure_is_reported_as_failure(self):
        with mock.patch.object(self.fw, "is_admin", return_value=True), \
             mock.patch.object(self.fw, "_netsh",
                               side_effect=_netsh_router(_CP(1, NO_MATCH), other_rc=1)):
            ok, msg = self.fw.ensure_inbound_allow("SFTP Server", "TCP", 2222)
        self.assertFalse(ok)

    def test_admin_add_success(self):
        with mock.patch.object(self.fw, "is_admin", return_value=True), \
             mock.patch.object(self.fw, "_netsh",
                               side_effect=_netsh_router(_CP(1, NO_MATCH), other_rc=0)):
            ok, msg = self.fw.ensure_inbound_allow("SFTP Server", "TCP", 2222)
        self.assertTrue(ok)

    def test_admin_disabled_same_name_rule_is_repaired_not_reused(self):
        calls = []

        def _run(args):
            calls.append(args)
            if args[2] == "show":
                return _CP(0, JP_DISABLED_ALLOW)
            return _CP(0)
        with mock.patch.object(self.fw, "is_admin", return_value=True), \
             mock.patch.object(self.fw, "_netsh", side_effect=_run):
            ok, msg = self.fw.ensure_inbound_allow("SFTP Server", "TCP", 2222)
        self.assertTrue(ok)
        self.assertNotIn("既存", msg, "無効ルールを既存の許可として使っている: %s" % msg)
        self.assertTrue(any(a[2] in ("add", "set") for a in calls),
                        "修復のための netsh が実行されていない: %s" % calls)

    def test_elevated_rule_never_appears_is_not_success(self):
        with mock.patch.object(self.fw, "is_admin", return_value=False), \
             mock.patch.object(self.fw, "_add_rule_elevated", return_value=True), \
             mock.patch.object(self.fw, "_netsh",
                               side_effect=_netsh_router(_CP(1, NO_MATCH))):
            ok, msg = self.fw.ensure_inbound_allow("SFTP Server", "TCP", 2222)
        self.assertFalse(ok, "反映を確認できないのに成功扱い: %s" % msg)
        self.assertIn("NetBelt - SFTP Server (TCP/2222)", msg)

    def test_elevated_rule_appears_is_success(self):
        with mock.patch.object(self.fw, "is_admin", return_value=False), \
             mock.patch.object(self.fw, "_add_rule_elevated", return_value=True), \
             mock.patch.object(self.fw, "_netsh",
                               side_effect=_netsh_router(_CP(0, EN_ENABLED_ALLOW))):
            ok, msg = self.fw.ensure_inbound_allow("SFTP Server", "TCP", 2222)
        self.assertTrue(ok)


class EnsureSelfProgramAllowTest(unittest.TestCase):
    def setUp(self):
        import core.firewall as fw
        self.fw = fw
        for name, val in (("is_windows", True), ("_self_program", r"C:\example\NetBelt.exe")):
            p = mock.patch.object(fw, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("time.sleep")
        p.start()
        self.addCleanup(p.stop)

    def test_admin_netsh_failure_is_reported_as_failure(self):
        with mock.patch.object(self.fw, "is_admin", return_value=True), \
             mock.patch.object(self.fw, "_netsh",
                               side_effect=_netsh_router(_CP(1, NO_MATCH), other_rc=1)):
            ok, msg = self.fw.ensure_self_program_allow()
        self.assertFalse(ok, "netsh が失敗しているのに成功扱い: %s" % msg)

    def test_admin_add_success(self):
        with mock.patch.object(self.fw, "is_admin", return_value=True), \
             mock.patch.object(self.fw, "_netsh",
                               side_effect=_netsh_router(_CP(1, NO_MATCH), other_rc=0)):
            ok, msg = self.fw.ensure_self_program_allow()
        self.assertTrue(ok)

    def test_elevated_rule_never_appears_is_not_success(self):
        import ctypes
        with mock.patch.object(self.fw, "is_admin", return_value=False), \
             mock.patch.object(ctypes.windll.shell32, "ShellExecuteW", return_value=42), \
             mock.patch.object(self.fw, "_netsh",
                               side_effect=_netsh_router(_CP(1, NO_MATCH))):
            ok, msg = self.fw.ensure_self_program_allow()
        self.assertFalse(ok, "反映を確認できないのに成功扱い: %s" % msg)


class PanelShowsFirewallMessageTest(unittest.TestCase):
    """パネルは ok だけでなく msg も画面に出す（「反映待ち」が「完了」に潰れない）"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    MSG = "許可ルールの追加を要求したが反映を確認できず: NetBelt - test (TCP/2222)"

    def _config(self):
        import tempfile
        from core.config_manager import ConfigManager
        workdir = tempfile.mkdtemp(prefix="netbelt-fwmsg-")
        return ConfigManager(os.path.join(workdir, "config.json"))

    def _check_log(self, panel, server_attr):
        server = getattr(panel, server_attr)
        with mock.patch.object(server, "fix_firewall", return_value=(False, self.MSG)):
            panel._on_fw_allow()
        text = panel.log_text.toPlainText()
        self.assertIn(self.MSG, text, "msg がログに出ていない: %r" % text)
        self.assertNotIn("完了", text.splitlines()[-1])

    def test_tftp_panel_logs_message(self):
        from ui.tftp_server_panel import TFTPServerPanel
        self._check_log(TFTPServerPanel(config_manager=self._config()), "tftp_server")

    def test_ftp_panel_logs_message(self):
        from ui.ftp_server_panel import FTPServerPanel
        self._check_log(FTPServerPanel(config_manager=self._config()), "ftp_server")

    def test_sftp_panel_logs_message(self):
        from ui.sftp_server_panel import SFTPServerPanel
        self._check_log(SFTPServerPanel(), "sftp_server")

    def test_syslog_panel_shows_message(self):
        from ui.syslog_panel import SyslogPanel
        panel = SyslogPanel()
        receiver = mock.Mock()
        receiver.fix_firewall.return_value = (False, self.MSG)
        panel.set_syslog_receiver(receiver)
        panel._on_fw_allow()
        self.assertIn(self.MSG, panel.status_label.text())

    def test_snmp_panel_shows_message(self):
        # SNMPPanel 単体生成は MIB 読み込みスレッドが残りヘッドレスが落ちるため
        # MainWindow 経由で作り、close() で後片付けする
        from ui.main_window import MainWindow
        window = MainWindow()
        try:
            panel = window.snmp_panel
            manager = mock.Mock()
            manager.fix_firewall.return_value = (False, self.MSG)
            panel.snmp_manager = manager
            panel._on_fw_allow()
            self.assertIn(self.MSG, panel.trap_status_label.text())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
