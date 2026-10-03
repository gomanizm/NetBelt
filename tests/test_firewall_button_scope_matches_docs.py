"""「ファイアウォールで許可（管理者）」の説明が、実際の UAC の回数と操作範囲に合っていることを検証する。

何が起きていたか（実測、基準 9fee4af。netsh は subprocess.run の差し替え、
昇格は ctypes.windll の差し替えで模擬し、実際のファイアウォールと UAC には
触れていない）: 5 つのパネルのツールチップは「管理者昇格/UACが1回出ます」、
README は「押したときだけ UAC が表示され、NetBelt - <サービス> (...) という
名前で受信許可ルールを追加します」だった。実際のボタンは、
  1. そのサービスのポート規則を追加する（ポートごとに昇格 1 回。既に有効な
     許可があれば昇格しない）
  2. NetBelt.exe 向けの既存の受信規則を name=all ですべて削除する
     （手動で作ったものやブロック規則も含む）
  3. NetBelt.exe 全体を全ポート・全プロファイルで許可する
     （2 と 3 はまとめて昇格 1 回。事前の確認が無いので押すたびに昇格する）
を行う。規則の無い初回は TFTP / SFTP / SNMP Trap / Syslog（片方のプロトコル）
が 2 回、FTP（制御とパッシブ）と Syslog（UDP と TCP の両方）が 3 回で、規則が
そろった後も 1 回出る。最初の UAC を拒んでも、次の UAC は出る。

直し方: 挙動は変えず、README の「ファイアウォールについて」とツールチップを
実態に合わせた。ここでは、模擬で数えた回数とツールチップ・README の回数が
一致すること、README が削除と自exe の許可に触れていることを確かめる。
"""
import ctypes
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

import core.firewall as fw  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(REPO_ROOT, "README.md")
HEADING = "## ファイアウォールについて"

PROG = "C:\\Program Files\\NetBelt\\NetBelt.exe"
NO_MATCH = b"No rules match the specified criteria.\r\n"
# ShellExecuteW が UAC の拒否で返す値（SE_ERR_ACCESSDENIED。32 以下は失敗）
DENIED = 5
_TOKEN = re.compile(r'(\S+?)="([^"]*)"|(\S+)')


def _tokens(params):
    """昇格に渡した引数の文字列を、netsh の引数のリストへ戻す。"""
    return [m.group(1) + "=" + m.group(2) if m.group(1) else m.group(3)
            for m in _TOKEN.finditer(params)]


def _arg(args, key):
    prefix = key + "="
    return next((a[len(prefix):] for a in args if a.startswith(prefix)), None)


class _RuleTable:
    """受信規則の表と、それを読み書きする netsh・昇格の模擬。

    show は今ある規則から netsh の出力（英語）を作る。昇格は承認された
    ときだけ、渡された netsh（cmd.exe 経由なら & で区切った各行）を同じ表に
    当てる。昇格の回数（= UAC の回数）を uac に数える。
    """

    def __init__(self, deny=()):
        self.rules = []   # {"name", "program", "action"}
        self.uac = 0
        self.deny = set(deny)   # 拒否する昇格の順番（1 始まり）

    def run(self, args, **_kwargs):
        if not args or args[0] != "netsh":
            raise AssertionError("netsh 以外を起動しようとした: %r" % (args,))
        verb = args[3]
        name = _arg(args, "name")
        if verb == "show":
            hits = [r for r in self.rules if r["name"] == name]
            if not hits:
                return subprocess.CompletedProcess(args, 1, NO_MATCH, b"")
            lines = []
            for r in hits:
                lines += ["Rule Name:                            " + r["name"],
                          "-" * 70,
                          "Enabled:                              Yes",
                          "Direction:                            In",
                          "Action:                               " + r["action"]]
                if "verbose" in args and r["program"]:
                    lines.append("Program:                              " + r["program"])
                lines.append("")
            text = "\r\n".join(lines + ["Ok.", ""])
            return subprocess.CompletedProcess(args, 0, text.encode("utf-8"), b"")
        if verb == "add":
            self.rules.append({"name": name, "program": _arg(args, "program"),
                               "action": _arg(args, "action").capitalize()})
            return subprocess.CompletedProcess(args, 0, b"Ok.\r\n", b"")
        if verb == "delete":
            program = _arg(args, "program")
            before = len(self.rules)
            self.rules = [r for r in self.rules
                          if not (name == "all" and fw._same_program(r["program"], program))]
            rc = 0 if len(self.rules) < before else 1
            return subprocess.CompletedProcess(args, rc, b"" if rc == 0 else NO_MATCH, b"")
        if verb == "set":
            hits = [r for r in self.rules if r["name"] == name]
            for r in hits:
                r["action"] = "Allow"
            return subprocess.CompletedProcess(args, 0 if hits else 1, b"", b"")
        raise AssertionError("想定外の netsh: %r" % (args,))

    def shell_execute(self, _hwnd, verb, file, params, _dir, _show):
        if verb != "runas":
            raise AssertionError("昇格以外の起動: %r" % ((verb, file, params),))
        self.uac += 1
        if self.uac in self.deny:
            return DENIED
        if file == "netsh":
            self.run(["netsh"] + _tokens(params))
        elif file == "cmd.exe":
            body = params[len("/c "):] if params.startswith("/c ") else params
            for line in body.split(" & "):
                parts = _tokens(line)
                self.run(parts)
        else:
            raise AssertionError("想定外の昇格: %r" % ((file, params),))
        return 42


class FirewallButtonScopeTest(unittest.TestCase):
    # (パネルの種類, Syslog で受信しているプロトコル)
    KINDS = (("ftp", None), ("tftp", None), ("sftp", None), ("snmp", None),
             ("syslog", ("UDP",)), ("syslog", ("UDP", "TCP")))

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._panels = []
        self._objects = []
        self.addCleanup(self._dispose)

    def _dispose(self):
        from PyQt6.QtCore import QCoreApplication, QEvent
        for widget in self._panels:
            widget.close()
            widget.deleteLater()
        for obj in self._objects:
            obj.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self.app.processEvents()

    def _config(self):
        from core.config_manager import ConfigManager
        workdir = tempfile.mkdtemp(prefix="netbelt-fwscope-")
        return ConfigManager(os.path.join(workdir, "config.json"))

    def _panel(self, kind, protocols):
        if kind == "ftp":
            from ui.ftp_server_panel import FTPServerPanel
            panel = FTPServerPanel(config_manager=self._config())
        elif kind == "tftp":
            from ui.tftp_server_panel import TFTPServerPanel
            panel = TFTPServerPanel(config_manager=self._config())
        elif kind == "sftp":
            from ui.sftp_server_panel import SFTPServerPanel
            panel = SFTPServerPanel()
        elif kind == "snmp":
            from core.snmp_manager import SNMPManager
            from ui.snmp_panel import SNMPPanel
            # MIB の読み込みはこの検査と関係が無いので走らせない
            with mock.patch.object(SNMPPanel, "_start_background_mib_loading"):
                panel = SNMPPanel()
            manager = SNMPManager()
            self._objects.append(manager)
            panel.set_snmp_manager(manager)
        elif kind == "syslog":
            from core.syslog_receiver import SyslogReceiver
            from ui.syslog_panel import SyslogPanel
            receiver = SyslogReceiver()
            self._objects.append(receiver)
            # fix_firewall が見るのは受信中のプロトコルとポートだけ。実際には
            # 待ち受けない（ソケットを開かない）
            servers = {p: {"port": 514} for p in protocols}
            patcher = mock.patch.object(receiver, "_servers", servers)
            patcher.start()
            self.addCleanup(patcher.stop)
            panel = SyslogPanel()
            panel.set_syslog_receiver(receiver)
        else:
            raise AssertionError(kind)
        self._panels.append(panel)
        return panel

    def _press(self, panel, table, admin=False):
        """模擬の表に対して「ファイアウォールで許可」を押し、UAC の回数を返す"""
        windll = mock.MagicMock()
        windll.shell32.ShellExecuteW.side_effect = table.shell_execute
        before = table.uac
        with mock.patch.object(fw.subprocess, "run", table.run), \
                mock.patch.object(ctypes, "windll", windll, create=True), \
                mock.patch.object(fw, "is_windows", return_value=True), \
                mock.patch.object(fw, "is_admin", return_value=admin), \
                mock.patch.object(fw, "_self_program", return_value=PROG), \
                mock.patch("time.sleep"):
            panel._on_fw_allow()
        return table.uac - before

    def _first_press(self, kind, protocols):
        """規則の無い表で 1 回押したときの UAC の回数"""
        return self._press(self._panel(kind, protocols), _RuleTable())

    def test_the_first_press_counts(self):
        """前提: 規則の無い初回の回数（README とツールチップの根拠）"""
        expected = {("ftp", None): 3, ("tftp", None): 2, ("sftp", None): 2,
                    ("snmp", None): 2, ("syslog", ("UDP",)): 2,
                    ("syslog", ("UDP", "TCP")): 3}
        for kind, protocols in self.KINDS:
            with self.subTest(kind=kind, protocols=protocols):
                self.assertEqual(self._first_press(kind, protocols),
                                 expected[(kind, protocols)])

    def test_later_presses_still_ask_once(self):
        """規則がそろった後も、自exe の分の UAC が毎回 1 回出ること。"""
        for kind, protocols in self.KINDS:
            with self.subTest(kind=kind, protocols=protocols):
                panel = self._panel(kind, protocols)
                table = _RuleTable()
                self._press(panel, table)
                self.assertEqual(self._press(panel, table), 1)
                self.assertEqual(self._press(panel, table), 1)

    def _later_press(self, kind, protocols):
        """規則がそろった後（規則の無い表で 1 回押した後）に押したときの UAC の回数"""
        panel = self._panel(kind, protocols)
        table = _RuleTable()
        self._press(panel, table)
        return self._press(panel, table)

    def test_a_refused_port_rule_is_asked_for_again(self):
        """ポート規則の UAC を拒んだ次の押下では、その分の UAC もまた出ること。

        README が「2 回目以降は 1 回」と言い切らず、「1 のルールがそろった後は」と
        条件を付けている根拠。2 回目は、拒んだポート規則の 1 回と自exe の 1 回。
        """
        for kind, protocols in self.KINDS:
            with self.subTest(kind=kind, protocols=protocols):
                panel = self._panel(kind, protocols)
                table = _RuleTable(deny={1})
                self._press(panel, table)
                self.assertEqual(self._press(panel, table), 2)
                self.assertEqual(self._press(panel, table), 1)

    def test_the_ftp_passive_range_is_one_rule(self):
        """FTP のパッシブのポート範囲は、ポートの数によらず規則 1 つ・UAC 1 回であること。"""
        panel = self._panel("ftp", None)
        panel.passive_lo_spin.setValue(50000)
        panel.passive_hi_spin.setValue(50999)
        table = _RuleTable()
        self.assertEqual(self._press(panel, table), 3)
        self.assertEqual(
            [r["name"] for r in table.rules if "FTP Passive" in r["name"]],
            [fw.rule_name("FTP Passive", "TCP", "50000-50999")])

    def test_refusing_the_first_uac_does_not_stop_the_next(self):
        """最初の UAC を拒んでも、残りの UAC は出ること。"""
        for kind, protocols in self.KINDS:
            with self.subTest(kind=kind, protocols=protocols):
                first = self._first_press(kind, protocols)
                panel = self._panel(kind, protocols)
                table = _RuleTable(deny={1})
                self.assertEqual(self._press(panel, table), first)
                # 拒んだのはポート規則の分だけで、自exe の許可は作られる
                self.assertIn(fw._self_rule_name(),
                              [r["name"] for r in table.rules])

    def test_running_as_administrator_asks_nothing(self):
        """対照: 管理者として実行していれば UAC は出ない。"""
        for kind, protocols in self.KINDS:
            with self.subTest(kind=kind, protocols=protocols):
                panel = self._panel(kind, protocols)
                self.assertEqual(self._press(panel, _RuleTable(), admin=True), 0)

    def test_each_tooltip_states_the_maximum_the_panel_asks(self):
        """ツールチップの回数が、そのパネルで出うる最大の回数と同じこと。"""
        worst = {}
        for kind, protocols in self.KINDS:
            worst[kind] = max(worst.get(kind, 0), self._first_press(kind, protocols))
        for kind, count in worst.items():
            with self.subTest(kind=kind):
                tip = self._panel(kind, ("UDP",)).fw_allow_btn.toolTip()
                self.assertIn("UACが最大%d回" % count, tip,
                              "%s のツールチップの回数が実際と違う: %r" % (kind, tip))
                self.assertIn("NetBelt.exe", tip,
                              "%s のツールチップが自exe の規則に触れていない: %r"
                              % (kind, tip))

    def test_the_readme_states_what_the_button_does(self):
        """README が回数と、削除・自exe の許可に触れていること。"""
        text = io.open(README, encoding="utf-8").read()
        start = text.find(HEADING)
        self.assertGreaterEqual(start, 0, "README に %s が無い" % HEADING)
        end = text.find("\n## ", start + len(HEADING))
        section = text[start:] if end < 0 else text[start:end]

        worst = max(self._first_press(kind, protocols)
                    for kind, protocols in self.KINDS)
        self.assertIn("最大 %d 回" % worst, section)
        self.assertIn(fw._self_rule_name(), section,
                      "NetBelt.exe 全体の許可の規則名を案内していない")
        self.assertIn("削除", section, "既存の規則を消すことを書いていない")
        self.assertIn("管理者として実行する必要はありません", section)

        # 折り返しをまたいでも読めるよう、改行を除いて調べる
        flat = section.replace("\r", "").replace("\n", "")
        later = {self._later_press(kind, protocols) for kind, protocols in self.KINDS}
        self.assertEqual(len(later), 1, later)
        self.assertIn("1 のルールがそろった後は %d 回" % later.pop(), flat)
        self.assertIn("パッシブのポート範囲は 1 つのルール", flat)
        # 拒否しても残りが出ることは test_refusing_the_first_uac_does_not_stop_the_next
        self.assertIn("途中の UAC を拒否しても、残りの UAC は表示されます", flat)

    def test_the_english_readme_states_the_same_counts(self):
        """英語の Firewall の節が、同じ回数と、削除・自exe の許可に触れていること。"""
        text = io.open(README, encoding="utf-8").read()
        start = text.find("\n### Firewall", text.find("\n## English"))
        self.assertGreaterEqual(start, 0, "英語の README に ### Firewall が無い")
        end = text.find("\n##", start + 1)
        section = " ".join((text[start:] if end < 0 else text[start:end]).split())

        worst = max(self._first_press(kind, protocols)
                    for kind, protocols in self.KINDS)
        later = {self._later_press(kind, protocols) for kind, protocols in self.KINDS}
        self.assertEqual(later, {1}, "英語は回数を once と書いているので、1 回でなければ直す")
        self.assertIn("up to %d times on the first press" % worst, section)
        self.assertIn("once per press after the step 1 rules exist", section)
        self.assertIn("the FTP passive port range is a single rule", section)
        self.assertIn(fw._self_rule_name(), section)
        self.assertIn("deletes every existing inbound rule for NetBelt.exe", section)
        self.assertIn("does not need to be run as administrator", section)


if __name__ == "__main__":
    unittest.main()
