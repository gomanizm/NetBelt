"""自exe受信許可の確認が、対象の実行体まで一致を見ることの回帰テスト。

何が起きていたか（実測、基準 f4cad23）: ルール名は firewall.py:229 の固定値
"NetBelt - app inbound (self)" で、置き場所に依存しない。_rule_state は
「有効」と「操作」しか読まないので、ポータブル版を別フォルダへ移した後も
旧配置先向けの同名ルールが残っていれば

    _rule_state('NetBelt - app inbound (self)') = 'ok'
    rule_exists(...) = True
    ensure_self_program_allow() -> ok=True
        msg='自exe受信許可を追加(昇格): NetBelt - app inbound (self)'

となり、新しい exe 向けの add が失敗していても・まだ終わっていなくても
1 回目（0.25 秒）の確認で「完了」と表示された。利用者は許可できたと信じて
別の原因を探し続けることになる。

どう直したか: _rule_state に「プログラム」/"Program" 行の読み取りを足し、
program= を渡されたときだけ os.path.normcase(normpath) で自 exe と一致する
ルールへ絞る。Program 行が 1 つも読めなかった出力（未知ロケール、プログラム
指定の無いルール）は、これまでどおり名前一致で判定して回帰を避ける。
ポート規則（ensure_inbound_allow）は Program を持たないので変えていない。

netsh は実行しない（すべてモック）。Program 行は verbose を付けないと出ない
（実測: netsh advfirewall firewall show rule name=all dir=in verbose）。
"""
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

import core.firewall as fw

RULE = "NetBelt - app inbound (self)"
OLD_EXE = "C:\\Tools\\old-place\\NetBelt.exe"
NEW_EXE = "D:\\portable\\NetBelt\\NetBelt.exe"


def _jp_rule(program=None, action="許可", enabled="はい"):
    """netsh verbose の日本語出力を 1 ルール分組み立てる"""
    lines = [
        "",
        "規則名:                               " + RULE,
        "-" * 70,
        "説明:                                 NetBelt",
        "有効:                                 " + enabled,
        "方向:                                 入力",
        "プロファイル:                         ドメイン,プライベート,パブリック",
        "ローカル IP:                          任意",
        "リモート IP:                          任意",
        "プロトコル:                           任意",
    ]
    if program is not None:
        lines.append("プログラム:                           " + program)
    lines += [
        "インターフェイスの種類:               任意",
        "セキュリティ:                         NotRequired",
        "操作:                                 " + action,
        "",
    ]
    return "\r\n".join(lines).encode("utf-8")


def _en_rule(program=None, action="Allow", enabled="Yes"):
    """netsh verbose の英語出力を 1 ルール分組み立てる"""
    lines = [
        "",
        "Rule Name:                            " + RULE,
        "-" * 70,
        "Enabled:                              " + enabled,
        "Direction:                            In",
        "Protocol:                             Any",
    ]
    if program is not None:
        lines.append("Program:                              " + program)
    lines += [
        "Action:                               " + action,
        "",
    ]
    return "\r\n".join(lines).encode("utf-8")


# 未知ロケール: キーも値も認識語彙に無い（従来どおり名前一致で真にする退避）
UNKNOWN_LOCALE = "\r\n".join([
    "",
    "Regelname:                            " + RULE,
    "-" * 70,
    "Aktiviert:                            Ja",
    "Anwendung:                            " + OLD_EXE,
    "Aktion:                               Zulassen",
    "",
]).encode("utf-8")

NO_MATCH = "\r\n指定された条件に一致する規則はありません。\r\n\r\n".encode("utf-8")


class _CP:
    def __init__(self, rc, out=b""):
        self.returncode = rc
        self.stdout = out
        self.stderr = b""


class _Netsh:
    """show の出力を固定し、呼ばれた引数を記録する差し替え"""

    def __init__(self, show_result, other_rc=0):
        self.show_result = show_result
        self.other_rc = other_rc
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:3] == ["advfirewall", "firewall", "show"]:
            return self.show_result
        return _CP(self.other_rc)

    def show_calls(self):
        return [a for a in self.calls
                if a[:3] == ["advfirewall", "firewall", "show"]]


class RuleStateProgramTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(fw, "is_windows", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    def _state(self, out, program=None):
        netsh = _Netsh(_CP(0, out))
        with mock.patch.object(fw, "_netsh", netsh):
            state = fw._rule_state(RULE, program=program)
        return state, netsh

    def test_a_rule_for_another_exe_is_not_our_rule(self):
        state, _ = self._state(_jp_rule(OLD_EXE), program=NEW_EXE)
        self.assertEqual(state, "absent",
                         "旧配置先の規則を新しい exe の許可と見なしている")
        state, _ = self._state(_en_rule(OLD_EXE), program=NEW_EXE)
        self.assertEqual(state, "absent")

    def test_a_rule_for_our_exe_is_ok(self):
        state, _ = self._state(_jp_rule(NEW_EXE), program=NEW_EXE)
        self.assertEqual(state, "ok")
        state, _ = self._state(_en_rule(NEW_EXE), program=NEW_EXE)
        self.assertEqual(state, "ok")

    def test_path_comparison_ignores_case_and_separators(self):
        state, _ = self._state(_jp_rule("D:/portable/netbelt/NETBELT.EXE"),
                               program=NEW_EXE)
        self.assertEqual(state, "ok",
                         "同じ実行体を別の書き方で指した規則を見落としている")

    def test_our_exe_among_other_rules_is_found(self):
        state, _ = self._state(_jp_rule(OLD_EXE) + _jp_rule(NEW_EXE),
                               program=NEW_EXE)
        self.assertEqual(state, "ok")

    def test_a_disabled_rule_for_our_exe_is_not_ok(self):
        state, _ = self._state(_jp_rule(NEW_EXE, enabled="いいえ"),
                               program=NEW_EXE)
        self.assertEqual(state, "present")

    def test_program_is_read_with_verbose(self):
        _, netsh = self._state(_jp_rule(NEW_EXE), program=NEW_EXE)
        self.assertIn("verbose", netsh.show_calls()[0],
                      "verbose を付けないと netsh は Program 行を出さない")

    def test_unreadable_program_falls_back_to_the_name(self):
        state, _ = self._state(UNKNOWN_LOCALE, program=NEW_EXE)
        self.assertEqual(state, "unknown",
                         "読めない出力で判定を厳しくすると既存環境が壊れる")

    def test_a_rule_without_a_program_line_still_matches_by_name(self):
        state, _ = self._state(_jp_rule(None), program=NEW_EXE)
        self.assertEqual(state, "ok",
                         "プログラム指定の無い許可規則は全ての exe を通す")

    def test_without_a_program_the_check_is_unchanged(self):
        state, netsh = self._state(_jp_rule(OLD_EXE))
        self.assertEqual(state, "ok", "名前だけの判定を変えてはいけない")
        self.assertNotIn("verbose", netsh.show_calls()[0],
                         "ポート規則の確認まで verbose にしている")


class EnsureSelfProgramAllowMatchTest(unittest.TestCase):
    def setUp(self):
        for name, val in (("is_windows", True), ("is_admin", False),
                          ("_self_program", NEW_EXE)):
            p = mock.patch.object(fw, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("time.sleep")
        p.start()
        self.addCleanup(p.stop)

    def _run(self, out):
        import ctypes
        netsh = _Netsh(_CP(0, out))
        with mock.patch.object(ctypes.windll.shell32, "ShellExecuteW",
                               return_value=42), \
                mock.patch.object(fw, "_netsh", netsh):
            ok, msg = fw.ensure_self_program_allow()
        return ok, msg, netsh

    def test_an_old_placement_rule_is_not_reported_as_done(self):
        ok, msg, _ = self._run(_jp_rule(OLD_EXE))
        self.assertFalse(
            ok, "旧配置先の規則を根拠に「完了」と返している: %s" % msg)
        self.assertIn("確認できず", msg)

    def test_a_rule_for_the_current_exe_is_reported_as_done(self):
        ok, msg, _ = self._run(_jp_rule(NEW_EXE))
        self.assertTrue(ok, msg)

    def test_no_rule_at_all_is_still_a_failure(self):
        import ctypes
        netsh = _Netsh(_CP(1, NO_MATCH))
        with mock.patch.object(ctypes.windll.shell32, "ShellExecuteW",
                               return_value=42), \
                mock.patch.object(fw, "_netsh", netsh):
            ok, msg = fw.ensure_self_program_allow()
        self.assertFalse(ok, msg)


class EnsureInboundAllowUnchangedTest(unittest.TestCase):
    """ポート規則は Program を持たないので、判定を変えない"""

    def setUp(self):
        for name, val in (("is_windows", True), ("is_admin", True)):
            p = mock.patch.object(fw, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)

    def test_existing_port_rule_is_still_accepted(self):
        out = "\r\n".join([
            "",
            "規則名:                               NetBelt - TFTP Server (UDP/69)",
            "-" * 70,
            "有効:                                 はい",
            "方向:                                 入力",
            "プロトコル:                           UDP",
            "ローカル ポート:                      69",
            "操作:                                 許可",
            "",
        ]).encode("utf-8")
        netsh = _Netsh(_CP(0, out))
        with mock.patch.object(fw, "_netsh", netsh):
            ok, msg = fw.ensure_inbound_allow("TFTP Server", "UDP", 69)
        self.assertTrue(ok, msg)
        self.assertNotIn("verbose", netsh.show_calls()[0])


if __name__ == "__main__":
    unittest.main()
