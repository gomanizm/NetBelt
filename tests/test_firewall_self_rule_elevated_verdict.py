"""自exe受信許可の昇格の経路が、昇格した delete → add を終える前に「完了」を返さないこと。

何が起きていたか（実測、基準 31b45ee。ShellExecuteW と netsh は差し替えの模擬で、
実際のファイアウォールにも UAC にも触れていない）: 未昇格のとき
ensure_self_program_allow は
  cmd.exe /c "netsh ... delete rule name=all dir=in program=<自exe>
              & netsh ... add rule name=... action=allow program=<自exe> ..."
を ShellExecuteW(runas) で起動し、その終わりを待たずに 0.25 秒おきに
rule_exists(name, program=<自exe>) を見て、最初に真になった時点で「完了」を
返していた。前回までの "NetBelt - app inbound (self)" が自exe 向けに既に
あると、1 回目の確認は昇格した delete より先に走って既存の許可を見るので、

  show #1 -> present       （既存の許可。昇格した delete はまだ）
  判定: (True, '自exe受信許可を追加（管理者昇格。…）: NetBelt - app inbound (self)')
  [昇格側] delete（既存の許可ごと消える） → add 失敗
  最後に残った自exe の規則: []

となり、「完了」と出たあとに既存の許可が消えていた（delete が 2〜6 回目の
確認の前に走る台本でも同じ。add が成功する台本でも、判定は add より前だった）。
41c9187 で delete から action=block を外し、delete が実際に規則を消すように
なったことで生まれた（441ea02 では delete が引数の誤りで何も消さなかった）。

どう直したか: 1 回目の確認で許可が見えても、それが昇格した add の結果か、
delete より前からあった許可かは見分けられない。そこで、
  - 途中では完了とせず、いつも最後（6 回目）の確認で決める。最後にも見えたら
    完了とする。見えなかった確認の後で見えても途中で完了としない（見えなかった
    のが show の一時的な失敗だと、次に見えたのは delete より前の既存の許可で
    ありうる。e22cce1 はこれで delete より前に完了と返した）
  - 見えていた許可が消えたまま最後まで戻らなければ失敗とし、既存の規則が
    消えたことを文言で知らせる（管理者の経路の _SELF_RULES_DELETED と同じ一言）。
    ただし起動前に無かった許可が最後の 1 回だけ見えないときは添えない
    （show の一時的な失敗と見分けられず、見えていたのは add が作った許可）
  - 昇格した delete → add は、1 回目の確認（0.25 秒後）より前に終わるのが
    普通の順序（この PC の netsh は 1 回約 0.05 秒）。既存の許可が消えて add が
    失敗しても確認では一度も見えないので、起動の前に 1 回だけ既存の許可を
    見ておき、あったのに最後まで見えなければ同じ一言を添える
起動後の確認の間隔と回数（0.25 秒 × 6 回）は変えず、昇格した処理を待つ上限は
延ばさない。増えるのは起動前（UAC の前）の show 1 回（約 0.05 秒）だけ。

子プロセスの終わりを待つ案（ShellExecuteExW の SEE_MASK_NOCLOSEPROCESS）は
採らなかった。441ea02 からある既存のテストは、本物の shell32 の ShellExecuteW
だけを差し替える（test_firewall_rule_state.py、test_firewall_self_program_match.py）
か、ShellExecuteW しか持たない偽の ctypes を使う（test_firewall_self_program_path.py）
ので、そちらへ替えると前者は本物の UAC と netsh を起こし、後者は失敗する。
代わりに、昇格した処理がどの順序で終わっても、判定までの待ち時間は確認の
期間いっぱい（0.25 秒 × 6 回と show 6 回分）になる。31b45ee は許可が見えた
時点（普通は 1 回目の確認）で返していた。
残る限界: 昇格した delete が確認の期間（約 1.5 秒と show 6 回分）より後に
走ったときは、今までどおり見分けられない。起動前の確認で無かったことを
根拠に途中で完了とする案も採らない。その確認が一時的に失敗すると、delete
より前に完了と返す経路が増えるため
（test_a_failed_pre_check_does_not_make_done_early）。
最後の確認の show が一時的に失敗すると、許可はあっても失敗とする（待つ上限を
延ばさずに確かめ直す手段が無い）。起動前に許可があったときは、そのうえ
『削除済み』の一言も添える（5 回目と 6 回目の確認の間に delete が走り add が
失敗した場合と見分けられない）。
判定の限界（一言）: 『削除済み』を添えるかは、起動前の確認と起動後の 6 回の
確認の真偽（show の一時的な失敗も「無い」と読む）だけで決める。観測が同じに
なる順序どうしは、許可が本当に残っているか・消えたかで分けられない
（模擬で確かめた。基準 1d9d5c5）:
  - 初めて足し、最後の 2 回の show が一時的に失敗する（起動前 無し、確認
    有有有有無無）と、許可は残っているのに添える。起動前の確認が一時的に
    失敗し、既存の許可が 4 回目と 5 回目の確認の間に消えて add が失敗した
    順序と同じ観測で、そちらでは添えるのが正しい。この添えすぎは、最後の
    2 回の show が続けて一時的に失敗したときだけ起きる。
  - 起動前の確認が一時的に失敗し、既存の許可が 5 回目と 6 回目の確認の間に
    消えて add が失敗する（起動前 無し、確認 有有有有有無）と、添えない。
    初めて足して最後の 1 回だけ show が一時的に失敗した順序
    （test_a_failed_last_check_does_not_claim_a_first_time_rule_was_deleted）と
    同じ観測で、そちらでは添えないのが正しい。この添え漏れは、起動前の確認の
    一時的な失敗に、昇格した delete の遅れ（約 1.5 秒）と add の失敗が
    重なったときだけ起きる。
  - 起動前の確認が一時的に失敗し、既存の許可が 1 回目の確認より前に消えて
    add が失敗する（起動前 無し、確認 無無無無無無）と、添えない。初めて足して
    add が失敗した順序（test_a_rule_that_never_shows_keeps_the_old_message）と
    同じ観測で、そちらでは添えないのが正しい。delete → add が 1 回目の確認より
    前に終わるのは普通の順序なので、この添え漏れは delete の遅れが無くても、
    起動前の確認の一時的な失敗と add の失敗が重なれば起きる（1 つ前の項目の
    添え漏れより起こりやすい）。
分けるには、確認を増やす（起動前は 1 回まで、起動後は 0.25 秒 × 6 回までと
test_the_checks_after_the_launch_do_not_wait_longer で決めている）か、show の
一時的な失敗と「規則が無い」を netsh の出力の文言（ロケールごとに違う）で
見分けるしかなく、どちらも採らなかった。
"""
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, "src")

import core.firewall as fw  # noqa: E402

PROG = "C:\\Program Files\\NetBelt\\NetBelt.exe"
NO_MATCH = b"No rules match the specified criteria.\r\n"
# 確認の回数と間隔（31b45ee と同じ）。GUI スレッドで待つ上限はこれで決まる
CHECKS = 6
INTERVAL = 0.25
_real_sleep = time.sleep


class _Elevated:
    """昇格した cmd.exe（delete → add）と、自exe 向けの受信規則の模擬。

    ShellExecuteW で起動されたあと、このスレッドが time.sleep を呼んだ回数で
    台本を進める（delete_at 回目の sleep の間に delete、add_at 回目の間に add）。
    show は今ある規則から netsh の verbose 出力（英語）を作る。
    """

    def __init__(self, pre_existing, delete_at, add_at, add_ok, pre_check_fails=False,
                 failing_checks=()):
        self.rules = [fw._self_rule_name()] if pre_existing else []
        self.delete_at = delete_at
        self.add_at = add_at
        self.add_ok = add_ok
        # 起動前の show を一時的に失敗させる（netsh の rc!=0）
        self.pre_check_fails = pre_check_fails
        # 起動後の何回目の確認（1 始まり）の show を一時的に失敗させるか
        self.failing_checks = set(failing_checks)
        self.launches = []
        self.sleeps = []
        self.shows = 0
        self.shows_before_launch = 0   # 昇格した処理を起動する前の show
        self.thread = threading.get_ident()

    @property
    def finished(self):
        """昇格した処理が最後（add）まで進んだか"""
        return bool(self.launches) and len(self.sleeps) >= self.add_at

    def shell_execute(self, hwnd, verb, file, params, directory, show):
        self.launches.append((verb, file, params))
        return 42        # >32 = 起動できた

    def sleep(self, seconds):
        if threading.get_ident() != self.thread:
            # ほかのテストが残したスレッドの sleep で台本を進めない
            return _real_sleep(seconds)
        self.sleeps.append(seconds)
        if not self.launches:
            return None
        n = len(self.sleeps)
        if n == self.delete_at:
            # name=all dir=in program=<自exe>: 自exe の受信規則は許可も含めて消える
            self.rules = []
        if n == self.add_at and self.add_ok:
            self.rules.append(fw._self_rule_name())
        return None

    def run_to_end(self):
        """判定のあとも昇格した処理は進む（台本の残りを流す）"""
        while len(self.sleeps) < max(self.delete_at, self.add_at):
            self.sleep(0)

    def netsh(self, args):
        if args[:4] != ["advfirewall", "firewall", "show", "rule"]:
            raise AssertionError("未昇格のまま netsh で規則を変えようとした: %r" % (args,))
        self.shows += 1
        if not self.launches:
            self.shows_before_launch += 1
            if self.pre_check_fails:
                return subprocess.CompletedProcess(args, 1, b"An error occurred.\r\n", b"")
        elif self.shows - self.shows_before_launch in self.failing_checks:
            return subprocess.CompletedProcess(args, 1, b"An error occurred.\r\n", b"")
        name =next(a[len("name="):] for a in args if a.startswith("name="))
        if name not in self.rules:
            return subprocess.CompletedProcess(args, 1, NO_MATCH, b"")
        out = "\r\n".join([
            "",
            "Rule Name:                            " + name,
            "----------------------------------------------------------------------",
            "Enabled:                              Yes",
            "Direction:                            In",
            "Profiles:                             Domain,Private,Public",
            "Program:                              " + PROG,
            "Action:                               Allow",
            "Ok.",
            "",
        ])
        return subprocess.CompletedProcess(args, 0, out.encode("utf-8"), b"")


def _no_real_process(*args, **_kwargs):
    raise AssertionError("実際のプロセスを起動しようとした: %r" % (args,))


class ElevatedSelfRuleVerdictTest(unittest.TestCase):
    def _run(self, **timeline):
        elevated = _Elevated(**timeline)
        fake_ctypes = types.ModuleType("ctypes")
        fake_ctypes.windll = types.SimpleNamespace(
            shell32=types.SimpleNamespace(ShellExecuteW=elevated.shell_execute))
        with mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}), \
                mock.patch.object(fw, "is_windows", return_value=True), \
                mock.patch.object(fw, "is_admin", return_value=False), \
                mock.patch.object(fw, "_self_program", return_value=PROG), \
                mock.patch.object(fw, "_netsh", elevated.netsh), \
                mock.patch.object(fw.subprocess, "run", _no_real_process), \
                mock.patch("time.sleep", elevated.sleep):
            ok, msg = fw.ensure_self_program_allow()
            # 判定までに待った sleep と、そのときに昇格側が終わっていたか
            elevated.waited = list(elevated.sleeps)
            finished = elevated.finished
            elevated.run_to_end()
        # 前提: 昇格の経路（cmd.exe を runas で 1 回）を通っている
        self.assertEqual([c[:2] for c in elevated.launches], [("runas", "cmd.exe")],
                         "前提: 昇格の経路を通っていない: %r" % elevated.launches)
        return ok, msg, finished, elevated

    def test_an_existing_allow_lost_to_a_failed_replacement_is_not_done(self):
        """既存の許可が delete で消え add が失敗したら、完了と返さないこと"""
        for delete_at in range(2, CHECKS + 1):
            with self.subTest(delete_at=delete_at):
                ok, msg, _finished, elevated = self._run(
                    pre_existing=True, delete_at=delete_at, add_at=delete_at,
                    add_ok=False)
                # 前提: 最後には自exe の許可が残っていない
                self.assertEqual(elevated.rules, [])
                self.assertFalse(
                    ok, "昇格した delete が既存の許可を消し add が失敗したのに"
                        "「完了」と返した（sleep %d 回目で判定）: %s"
                        % (len(elevated.waited), msg))
                self.assertIn("削除済み", msg,
                              "既存の許可が消えたことが文言に出ていない: %s" % msg)
                self.assertIn("手動", msg, "手動での追加を促していない: %s" % msg)
                self.assertTrue(msg.endswith(": " + fw._self_rule_name()), msg)

    def test_an_existing_allow_lost_before_the_first_check_is_reported(self):
        """既存の許可が 1 回目の確認より前に消え add が失敗したときも、消えたことを知らせること

        この順序（delete → add が 1 回目の確認の前に終わる）が普通で、確認では
        許可が一度も見えない。3ffabc4 では文言が「反映を確認できず」だけで、
        既存の許可が消えたことが画面から分からなかった（模擬で再現）。
        """
        ok, msg, _finished, elevated = self._run(
            pre_existing=True, delete_at=1, add_at=1, add_ok=False)
        # 前提: 最後には自exe の許可が残っていない
        self.assertEqual(elevated.rules, [])
        self.assertFalse(ok, msg)
        self.assertIn("削除済み", msg,
                      "既存の許可が消えたことが文言に出ていない: %s" % msg)
        self.assertIn("手動", msg, "手動での追加を促していない: %s" % msg)
        self.assertTrue(msg.endswith(": " + fw._self_rule_name()), msg)
        # 既存の許可は、昇格した処理を起動する前に見ていること（起動した後に
        # 見ると、昇格した delete と競って、消えた後の状態を見ることがある）
        self.assertGreaterEqual(elevated.shows_before_launch, 1,
                                "起動前に既存の許可を見ていない")

    def test_done_is_not_returned_before_the_elevated_add_ran(self):
        """既存の許可があるとき、完了は昇格した add が終わった後でだけ返すこと"""
        timelines = (
            # 確認の間に delete、次の確認の間に add（消えた確認が挟まる）
            dict(pre_existing=True, delete_at=2, add_at=3, add_ok=True),
            # delete と add が同じ確認の間に終わる（消えた確認が挟まらない）
            dict(pre_existing=True, delete_at=2, add_at=2, add_ok=True),
            dict(pre_existing=True, delete_at=5, add_at=6, add_ok=True),
        )
        for timeline in timelines:
            with self.subTest(**timeline):
                ok, msg, finished, elevated = self._run(**timeline)
                self.assertTrue(ok, msg)
                self.assertTrue(
                    finished, "昇格した add より前（sleep %d 回目）に「完了」と"
                              "返した: %s" % (len(elevated.waited), msg))
                self.assertIn(fw._BLOCK_NOT_CHECKED, msg)

    def test_a_rule_seen_again_after_a_gap_is_decided_at_the_last_check(self):
        """消えた確認の後で見えても途中で完了とせず、最後の確認で完了とすること

        見えなかった確認が一時的な失敗（netsh の rc!=0）でも同じ形になるので、
        途中で完了とすると delete より前の既存の許可を見て完了と返しうる
        （test_a_failed_check_then_the_old_allow_is_not_done）。
        """
        ok, msg, finished, elevated = self._run(
            pre_existing=True, delete_at=2, add_at=3, add_ok=True)
        self.assertTrue(ok, msg)
        self.assertTrue(finished, "昇格した add より前に「完了」と返した: %s" % msg)
        self.assertEqual(len(elevated.waited), CHECKS, elevated.waited)

    def test_a_first_time_add_is_done_after_the_add_ran(self):
        """対照: 初めて足すとき（既存の許可が無い）も、add の後の最後の確認で完了"""
        ok, msg, finished, elevated = self._run(
            pre_existing=False, delete_at=1, add_at=2, add_ok=True)
        self.assertTrue(ok, msg)
        self.assertTrue(finished, "昇格した add より前に「完了」と返した: %s" % msg)
        self.assertEqual(len(elevated.waited), CHECKS, elevated.waited)

    def test_a_failed_pre_check_does_not_make_done_early(self):
        """起動前の確認が一時的に失敗しても、delete より前に完了と返さないこと

        起動前の確認は文言にだけ使う。これを「既存の許可は無い」の根拠にして
        1 回目に見えた時点で完了とすると、その確認が一時的に失敗しただけで、
        delete より前の既存の許可を見て完了と返し、そのあと許可が消える。
        """
        ok, msg, _finished, elevated = self._run(
            pre_existing=True, delete_at=3, add_at=3, add_ok=False,
            pre_check_fails=True)
        # 前提: 起動前に 1 回見て失敗し、最後には自exe の許可が残っていない
        self.assertEqual(elevated.shows_before_launch, 1)
        self.assertEqual(elevated.rules, [])
        self.assertFalse(
            ok, "起動前の確認の失敗を「既存の許可は無い」と読み、delete より前"
                "（sleep %d 回目）に「完了」と返した: %s"
                % (len(elevated.waited), msg))
        self.assertIn("削除済み", msg,
                      "見えていた許可が消えたことが文言に出ていない: %s" % msg)

    def test_a_failed_check_then_the_old_allow_is_not_done(self):
        """起動後の確認が一時的に失敗し、次の確認で delete より前の既存の許可が
        見えても、完了と返さないこと

        e22cce1 は「見えなかった確認のすぐ後で見えた」ときに途中で完了とした
        ので、1 回目の確認の show が一時的に失敗し（rc!=0）、2 回目に既存の
        許可が見えると、昇格した delete より前（sleep 2 回目）に完了と返し、
        そのあと delete が既存の許可を消して add が失敗した（模擬で再現）。
        """
        ok, msg, finished, elevated = self._run(
            pre_existing=True, delete_at=3, add_at=3, add_ok=False,
            failing_checks=(1,))
        # 前提: 起動前に既存の許可を見ており、最後には自exe の許可が残っていない
        self.assertEqual(elevated.shows_before_launch, 1)
        self.assertEqual(elevated.rules, [])
        self.assertFalse(
            ok, "一時的に失敗した確認の次に既存の許可を見て、昇格した delete より前"
                "（sleep %d 回目）に「完了」と返した: %s" % (len(elevated.waited), msg))
        self.assertTrue(finished, "前提: 判定までに昇格した処理が終わっていない")
        self.assertIn("削除済み", msg,
                      "既存の許可が消えたことが文言に出ていない: %s" % msg)

    def test_a_failed_last_check_does_not_claim_a_first_time_rule_was_deleted(self):
        """初めて足したとき、最後の確認だけが一時的に失敗しても「削除済み」と言わないこと

        起動前の確認では許可が無く、見えていた許可は昇格した add が作ったもの
        （add の後に delete は走らない）。最後の 1 回だけ見えないのは show の
        一時的な失敗と見分けられないので判定は失敗のままだが、f0ad44d までは
        一度でも見えていれば『既存の受信規則は削除済み。手動で追加を』と添え、
        許可があるのに手で重ねて足させていた（模擬で再現）。
        """
        ok, msg, _finished, elevated = self._run(
            pre_existing=False, delete_at=1, add_at=1, add_ok=True,
            failing_checks=(CHECKS,))
        # 前提: 起動前には無く、最後には自exe の許可が残っている
        self.assertEqual(elevated.shows_before_launch, 1)
        self.assertEqual(elevated.rules, [fw._self_rule_name()])
        self.assertNotIn("削除済み", msg,
                         "許可は消えていないのに削除済みと添えた: %s" % msg)
        if not ok:
            self.assertEqual(msg, "自exe受信許可を要求したが反映を確認できず: "
                             + fw._self_rule_name())

    def test_a_rule_that_never_shows_keeps_the_old_message(self):
        """対照: 一度も見えなければ、今までどおりの失敗の文言"""
        ok, msg, _finished, _elevated = self._run(
            pre_existing=False, delete_at=1, add_at=2, add_ok=False)
        self.assertFalse(ok, msg)
        self.assertEqual(msg, "自exe受信許可を要求したが反映を確認できず: "
                         + fw._self_rule_name())

    def test_the_checks_after_the_launch_do_not_wait_longer(self):
        """起動した後の確認の間隔と回数（昇格した処理を待つ上限）を延ばしていないこと

        起動した後の確認は 31b45ee と同じく 0.25 秒 × 6 回まで。起動の前に
        既存の許可を見る show は 1 回まで許す（UAC の前。約 0.05 秒）ので、
        GUI スレッドを止める時間の合計は 31b45ee より show 1 回分長い。
        """
        timelines = (
            dict(pre_existing=True, delete_at=2, add_at=2, add_ok=False),
            dict(pre_existing=True, delete_at=9, add_at=9, add_ok=True),
            dict(pre_existing=False, delete_at=1, add_at=9, add_ok=True),
            dict(pre_existing=True, delete_at=1, add_at=1, add_ok=False),
        )
        for timeline in timelines:
            with self.subTest(**timeline):
                _ok, _msg, _finished, elevated = self._run(**timeline)
                waited = elevated.waited
                self.assertLessEqual(len(waited), CHECKS, waited)
                self.assertTrue(all(s <= INTERVAL for s in waited), waited)
                self.assertLessEqual(elevated.shows_before_launch, 1)
                self.assertLessEqual(
                    elevated.shows - elevated.shows_before_launch, CHECKS)


if __name__ == "__main__":
    unittest.main()
