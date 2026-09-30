"""自exe受信許可の但し書き（ブロック規則の除去は確認していません）が、どのパネルの結果にも出ること。

何が起きていたか（実測、c2bb66a。netsh は subprocess.run の差し替え、昇格は
ctypes.windll の差し替えで模擬し、実際のファイアウォールと UAC には触れていない）:
0dd4fc2 で ensure_self_program_allow の成功の文言に但し書きを足したが、各サーバーの
fix_firewall は結果を combine_results へ「ポートの許可 → 自exe の許可」の順に
渡し、combine_results はすべて成功したとき最初の文言だけを返していた。そのため
ポートの許可と自exe の許可が両方成功すると、

  - SFTP パネルのログは「ファイアウォール許可: 完了 (許可ルールを追加: NetBelt -
    SFTP Server (TCP/2222))」だけで、但し書きは print にしか残らなかった
  - SNMP の Trap の状態表示も同じく、ポートの許可の文言だけだった
  - FTP・TFTP は、自exe の行がアクティビティとしてログに出るので但し書きは
    見えるが、結果の行（完了）には出ない（通知の配送待ちが上限に達している間は、
    その行ごと省かれる）
  - Syslog は結果の文言が自exe の文言なので出ていた

管理者の経路（netsh の delete + add）と昇格の経路（ShellExecuteW + show）の
どちらも同じだった。

どう直したか: combine_results は、すべて成功したときに返す文言に但し書きが無く、
ほかの操作の文言に但し書きがあれば、その文言を " / " で添える。但し書きの無い
結果（開発実行や Windows 以外、失敗を含む結果）の文言は変えない。
"""
import ctypes
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

import core.firewall as fw  # noqa: E402

PROG = "C:\\Program Files\\NetBelt\\NetBelt.exe"
NO_MATCH = b"No rules match the specified criteria.\r\n"


class _FakeNetsh:
    """subprocess.run の代わり。netsh の show / delete / add を模擬する。

    show は、追加済み（add、または昇格の実行の後）の規則だけを有効な許可として
    返す。delete は対象が無い（GPO の規則など）ときと同じく rc=1
    """

    def __init__(self):
        self.added = set()
        self.elevated = False

    def run(self, args, **_kwargs):
        if not args or args[0] != "netsh":
            raise AssertionError("netsh 以外を起動しようとした: %r" % (args,))
        verb = args[3]
        name = next((a[len("name="):] for a in args if a.startswith("name=")), "")
        if verb == "show":
            if not (self.elevated or name in self.added):
                return subprocess.CompletedProcess(args, 1, NO_MATCH, b"")
            lines = ["Rule Name:                            " + name,
                     "-" * 70,
                     "Enabled:                              Yes",
                     "Direction:                            In",
                     "Action:                               Allow"]
            if "verbose" in args:
                lines.append("Program:                              " + PROG)
            text = "\r\n".join(lines + ["", "Ok.", ""])
            return subprocess.CompletedProcess(args, 0, text.encode("utf-8"), b"")
        if verb == "delete":
            return subprocess.CompletedProcess(args, 1, NO_MATCH, b"")
        if verb == "add":
            self.added.add(name)
            return subprocess.CompletedProcess(args, 0, b"Ok.\r\n", b"")
        return subprocess.CompletedProcess(args, 1, b"", b"")


class FirewallBlockCaveatShownTest(unittest.TestCase):
    KINDS = ("sftp", "snmp", "ftp", "tftp", "syslog")

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._panels = []
        self._objects = []
        self._stoppers = []
        self.addCleanup(self._dispose)

    def _dispose(self):
        from PyQt6.QtCore import QCoreApplication, QEvent
        for stop in self._stoppers:
            stop()
        for widget in self._panels:
            widget.close()
            widget.deleteLater()
        for obj in self._objects:
            obj.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self.app.processEvents()

    def _config(self):
        from core.config_manager import ConfigManager
        workdir = tempfile.mkdtemp(prefix="netbelt-fwcaveat-")
        return ConfigManager(os.path.join(workdir, "config.json"))

    def _panel(self, kind):
        """パネルを作り、(パネル, 結果の文言を読む関数) を返す"""
        def last_log_line(panel):
            self.app.processEvents()
            return panel.log_text.toPlainText().splitlines()[-1]

        if kind == "sftp":
            from ui.sftp_server_panel import SFTPServerPanel
            panel = SFTPServerPanel()
            return panel, last_log_line
        if kind == "ftp":
            from ui.ftp_server_panel import FTPServerPanel
            panel = FTPServerPanel(config_manager=self._config())
            return panel, last_log_line
        if kind == "tftp":
            from ui.tftp_server_panel import TFTPServerPanel
            panel = TFTPServerPanel(config_manager=self._config())
            return panel, last_log_line
        if kind == "snmp":
            from core.snmp_manager import SNMPManager
            from ui.snmp_panel import SNMPPanel
            # MIB の読み込みはこの検査と関係が無いので走らせない
            with mock.patch.object(SNMPPanel, "_start_background_mib_loading"):
                panel = SNMPPanel()
            manager = SNMPManager()
            self._objects.append(manager)
            panel.set_snmp_manager(manager)
            return panel, lambda p: p.trap_status_label.text()
        if kind == "syslog":
            from core.syslog_receiver import SyslogReceiver
            from ui.syslog_panel import SyslogPanel
            receiver = SyslogReceiver()
            self._stoppers.append(receiver.stop)
            self.assertTrue(receiver.start_protocol("UDP", 0))
            panel = SyslogPanel()
            panel.set_syslog_receiver(receiver)
            return panel, lambda p: p.status_label.text()
        raise AssertionError(kind)

    def _shown(self, kind, admin):
        """netsh と昇格を模擬して「ファイアウォールで許可」を押し、結果の文言を返す"""
        panel, result_text = self._panel(kind)
        self._panels.append(panel)
        netsh = _FakeNetsh()
        windll = mock.MagicMock()

        def shell_execute(*_args):
            netsh.elevated = True    # 昇格した netsh が規則を足したことにする
            return 42
        windll.shell32.ShellExecuteW.side_effect = shell_execute
        with mock.patch.object(fw.subprocess, "run", netsh.run), \
                mock.patch.object(ctypes, "windll", windll, create=True), \
                mock.patch.object(fw, "is_windows", return_value=True), \
                mock.patch.object(fw, "is_admin", return_value=admin), \
                mock.patch.object(fw, "_self_program", return_value=PROG), \
                mock.patch("time.sleep"):
            panel._on_fw_allow()
        self.assertEqual(windll.shell32.ShellExecuteW.called, not admin,
                         "前提: %s の経路を通っていない"
                         % ("管理者" if admin else "昇格"))
        return result_text(panel)

    def _check(self, admin):
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                shown = self._shown(kind, admin)
                self.assertIn("完了", shown)
                self.assertIn(fw._BLOCK_NOT_CHECKED, shown,
                              "%s の結果に但し書きが出ていない: %r" % (kind, shown))
                self.assertIn(fw._self_rule_name(), shown)
                if kind != "syslog":
                    # ポートの許可の文言は消さない（Syslog は従来どおり自exe の文言）
                    self.assertIn("許可ルールを追加", shown)

    def test_the_caveat_is_shown_on_the_admin_path(self):
        self._check(admin=True)

    def test_the_caveat_is_shown_on_the_elevated_path(self):
        self._check(admin=False)


class CombineResultsCaveatTest(unittest.TestCase):
    """combine_results の文言の組み立て（パネルを通さない）"""

    PORT_OK = "許可ルールを追加: NetBelt - SFTP Server (TCP/2222)"
    SELF_OK = "自exe受信許可を追加（%s）: %s" % (fw._BLOCK_NOT_CHECKED,
                                              fw._self_rule_name())

    def test_the_caveat_of_another_operation_is_appended(self):
        ok, msg = fw.combine_results([(True, self.PORT_OK), (True, self.SELF_OK)])
        self.assertTrue(ok)
        self.assertEqual(msg, self.PORT_OK + " / " + self.SELF_OK)

    def test_a_message_that_already_has_the_caveat_is_not_repeated(self):
        """Syslog のように自exe の文言を結果にする場合は、そのまま"""
        ok, msg = fw.combine_results([(True, self.PORT_OK), (True, self.SELF_OK)],
                                     success_message=self.SELF_OK)
        self.assertTrue(ok)
        self.assertEqual(msg, self.SELF_OK)

    def test_results_without_the_caveat_are_unchanged(self):
        """対照: 開発実行（自exe はスキップ）では従来どおり最初の文言だけ"""
        skipped = "開発実行のためスキップ（frozen時のみ有効）"
        self.assertEqual(fw.combine_results([(True, self.PORT_OK), (True, skipped)]),
                         (True, self.PORT_OK))

    def test_failures_are_unchanged(self):
        """対照: 失敗を含むときは従来どおり失敗の理由だけ"""
        fail = "UACが承認されず未追加: NetBelt - SFTP Server (TCP/2222)"
        self.assertEqual(fw.combine_results([(False, fail), (True, self.SELF_OK)]),
                         (False, fail))


if __name__ == "__main__":
    unittest.main()
