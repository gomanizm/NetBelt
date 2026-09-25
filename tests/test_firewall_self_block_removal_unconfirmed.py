"""自exe受信許可の成功文言が、ブロック規則の除去まで保証したように読めないこと。

実測（基準 441ea02。netsh は subprocess.run の差し替えで模擬し、実際の
ファイアウォールには触れていない）: 同じ exe を対象にした有効なブロック規則が
ローカルの delete では消えない状態（GPO で配られた規則など。netsh の delete は
"No rules match" で rc=1）で ensure_self_program_allow() を呼ぶと、
  - 管理者の経路は delete と add の 2 回だけを呼び、add の終了コードだけで
    (True, '自exe受信許可を保証: NetBelt - app inbound (self)') を返した。
    delete は対象が無くても rc!=0 なので、結果を見ていない。
  - 昇格の経路は許可規則の show 1 回だけで確かめ、
    (True, '自exe受信許可を追加(昇格): NetBelt - app inbound (self)') を返した。
どちらもブロック規則は残ったまま（Windows はブロックを許可より優先するので
受信は通らない）なのに、各パネルには「ファイアウォール許可: 完了 (自exe受信
許可を保証: …)」と出ていた。

直し方（利用者の決定 B）: 判定は変えず、成功の文言だけを「許可規則を追加
（既存のブロック規則の除去は確認していません）」の趣旨にする。ブロック規則の
残りを netsh で確かめる案は、残る主な原因の GPO の規則がローカルの netsh から
見えない可能性が高く、効く範囲が狭いので採らなかった。
"""
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

import core.firewall as fw  # noqa: E402

PROG = "C:\\Program Files\\NetBelt\\NetBelt.exe"
NO_MATCH = b"No rules match the specified criteria.\r\n"


def _allow_rule_for(program):
    """show rule name=<自exe規則> verbose の出力（英語ロケール）"""
    return "\r\n".join([
        "Rule Name:                            " + fw._self_rule_name(),
        "----------------------------------------------------------------------",
        "Enabled:                              Yes",
        "Direction:                            In",
        "Action:                               Allow",
        "Program:                              " + program,
        "",
        "Ok.",
    ]).encode("utf-8")


class _Netsh:
    """netsh の模擬。ブロックは delete で消えず（rc=1）、add は通る"""

    def __init__(self):
        self.calls = []

    def __call__(self, args):
        verb = args[2]
        self.calls.append(verb)
        if verb == "delete":
            return subprocess.CompletedProcess(args, 1, stdout=NO_MATCH, stderr=b"")
        if verb == "add":
            return subprocess.CompletedProcess(args, 0, stdout=b"Ok.\r\n", stderr=b"")
        if verb == "show":
            return subprocess.CompletedProcess(args, 0, stdout=_allow_rule_for(PROG),
                                               stderr=b"")
        return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"")


class SelfProgramAllowMessageTest(unittest.TestCase):
    def setUp(self):
        for name, val in (("is_windows", True), ("_self_program", PROG)):
            p = mock.patch.object(fw, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("time.sleep")
        p.start()
        self.addCleanup(p.stop)

    def _assert_block_removal_not_claimed(self, ok, msg):
        # 判定は変えない（許可規則の追加は成功している）
        self.assertTrue(ok, msg)
        self.assertNotIn("保証", msg,
                         "ブロック規則の除去を確かめていないのに「保証」と出している: %s" % msg)
        self.assertIn("ブロック規則", msg,
                      "ブロック規則の除去を確認していないことが文言に出ていない: %s" % msg)
        self.assertIn("確認していません", msg,
                      "ブロック規則の除去を確認していないことが文言に出ていない: %s" % msg)
        self.assertIn(fw._self_rule_name(), msg, "規則名が文言から落ちている: %s" % msg)

    def test_admin_path_does_not_claim_the_block_rule_is_gone(self):
        netsh = _Netsh()
        with mock.patch.object(fw, "is_admin", return_value=True), \
                mock.patch.object(fw, "_netsh", netsh):
            ok, msg = fw.ensure_self_program_allow()
        # 前提: 呼ぶのは delete と add だけ（ブロックの残りは見ていない）
        self.assertEqual(netsh.calls, ["delete", "add"])
        self._assert_block_removal_not_claimed(ok, msg)

    def test_elevated_path_does_not_claim_the_block_rule_is_gone(self):
        import ctypes
        netsh = _Netsh()
        with mock.patch.object(fw, "is_admin", return_value=False), \
                mock.patch.object(ctypes.windll.shell32, "ShellExecuteW",
                                  return_value=42), \
                mock.patch.object(fw, "_netsh", netsh):
            ok, msg = fw.ensure_self_program_allow()
        # 前提: 確かめているのは許可規則の show だけ
        self.assertEqual(netsh.calls, ["show"])
        self._assert_block_removal_not_claimed(ok, msg)
        self.assertIn("昇格", msg, "昇格の経路であることが文言から落ちている: %s" % msg)

    def test_failures_are_unchanged(self):
        """失敗の判定と文言は変えない"""
        import ctypes
        with mock.patch.object(fw, "is_admin", return_value=True), \
                mock.patch.object(fw, "_netsh", return_value=subprocess.CompletedProcess(
                    [], 1, stdout=NO_MATCH, stderr=b"")):
            ok, msg = fw.ensure_self_program_allow()
        self.assertFalse(ok)
        self.assertEqual(msg, "自exe受信許可の追加に失敗: " + fw._self_rule_name())
        with mock.patch.object(fw, "is_admin", return_value=False), \
                mock.patch.object(ctypes.windll.shell32, "ShellExecuteW",
                                  return_value=42), \
                mock.patch.object(fw, "_netsh", return_value=subprocess.CompletedProcess(
                    [], 1, stdout=NO_MATCH, stderr=b"")):
            ok, msg = fw.ensure_self_program_allow()
        self.assertFalse(ok)
        self.assertEqual(msg, "自exe受信許可を要求したが反映を確認できず: "
                         + fw._self_rule_name())


if __name__ == "__main__":
    unittest.main()
