"""自exe受信許可の追加に失敗したとき、先の delete で既存の規則を消したことを文言で知らせること。

何が起きていたか（実測、5879190。netsh は差し替えで模擬し、実際のファイア
ウォールには触れていない）: 90be3e0 で delete から action=block を外したので、
管理者の経路の delete（name=all dir=in program=<自exe>）は、自exe を対象にした
受信規則を許可も含めて実際に消すようになった。その直後の add が失敗すると
（delete rc=0、add rc=1）、初回のプロンプトが作った許可や前回までの
"NetBelt - app inbound (self)" が消えたまま、
  (False, '自exe受信許可の追加に失敗: NetBelt - app inbound (self)')
だけが返り、既存の規則を消したことが画面から分からなかった（b2858c4 までは
delete が引数の誤りで何もしなかったので、同じ失敗でも許可は残っていた）。

どう直したか: delete が成功した（rc=0 ＝ 一致する規則を消した）ときだけ、失敗の
文言に「自exe向けの既存の受信規則は削除済み」と手動での追加を促す一言を添える。
消す物が無かった（rc!=0）ときの文言は今までどおり（test_failures_are_unchanged）。
判定・呼ぶ netsh の回数と順序は変えない。
"""
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

import core.firewall as fw  # noqa: E402

PROG = "C:\\Program Files\\NetBelt\\NetBelt.exe"
DELETED = b"\r\nDeleted 2 rule(s).\r\nOk.\r\n\r\n"
NO_MATCH = b"No rules match the specified criteria.\r\n"


class _Netsh:
    """netsh の模擬。delete は delete_rc で返し、add は失敗する"""

    def __init__(self, delete_rc):
        self.delete_rc = delete_rc
        self.calls = []

    def __call__(self, args):
        verb = args[2]
        self.calls.append(verb)
        if verb == "delete":
            out = DELETED if self.delete_rc == 0 else NO_MATCH
            return subprocess.CompletedProcess(args, self.delete_rc, stdout=out,
                                               stderr=b"")
        return subprocess.CompletedProcess(args, 1, stdout=b"An error occurred.\r\n",
                                           stderr=b"")


class SelfRuleAddFailureAfterDeleteTest(unittest.TestCase):
    def setUp(self):
        for name, val in (("is_windows", True), ("_self_program", PROG),
                          ("is_admin", True)):
            p = mock.patch.object(fw, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("time.sleep")
        p.start()
        self.addCleanup(p.stop)

    def _run(self, delete_rc):
        netsh = _Netsh(delete_rc)
        with mock.patch.object(fw, "_netsh", netsh):
            ok, msg = fw.ensure_self_program_allow()
        # 判定と呼ぶ netsh は変えない（再試行や確認の呼び出しを足さない）
        self.assertEqual(netsh.calls, ["delete", "add"])
        self.assertFalse(ok, msg)
        self.assertTrue(msg.startswith("自exe受信許可の追加に失敗"), msg)
        self.assertTrue(msg.endswith(": " + fw._self_rule_name()), msg)
        return msg

    def test_failure_after_a_successful_delete_says_the_rules_are_gone(self):
        msg = self._run(0)
        self.assertIn("削除済み", msg,
                      "既存の自exe受信規則を消したことが失敗の文言に出ていない: %s" % msg)
        self.assertIn("手動", msg, "手動での追加を促していない: %s" % msg)

    def test_failure_without_a_delete_is_unchanged(self):
        """対照: 消す物が無かった（delete rc=1）ときは今までどおりの文言"""
        msg = self._run(1)
        self.assertEqual(msg, "自exe受信許可の追加に失敗: " + fw._self_rule_name())

    def test_the_note_reaches_the_panel_result(self):
        """各パネルが使う combine_results を通っても、一言が落ちないこと"""
        msg = self._run(0)
        ok, shown = fw.combine_results([(True, "既存の許可ルールを使用: x"),
                                        (False, msg)])
        self.assertFalse(ok)
        self.assertIn("削除済み", shown)


if __name__ == "__main__":
    unittest.main()
