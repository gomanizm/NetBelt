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
  - 見えなかった確認のすぐ後で見えた（delete の後の add まで終わった）ときだけ、
    その場で完了とする（初めて足すときは今までどおりすぐ返る）
  - 見えたままのときは最後（6 回目）の確認まで待ち、最後にも見えたら完了とする
  - 見えていた許可が消えたまま最後まで戻らなければ失敗とし、既存の規則が
    消えたことを文言で知らせる（管理者の経路の _SELF_RULES_DELETED と同じ一言）
確認の間隔と回数（0.25 秒 × 6 回）は変えず、GUI スレッドで待つ上限は延ばさない。

子プロセスの終わりを待つ案（ShellExecuteExW の SEE_MASK_NOCLOSEPROCESS）は
採らなかった。441ea02 からある既存のテストは、本物の shell32 の ShellExecuteW
だけを差し替える（test_firewall_rule_state.py、test_firewall_self_program_match.py）
か、ShellExecuteW しか持たない偽の ctypes を使う（test_firewall_self_program_path.py）
ので、そちらへ替えると前者は本物の UAC と netsh を起こし、後者は失敗する。
残る限界: 昇格した delete が確認の期間（約 1.5 秒と show 6 回分）より後に
走ったときは、今までどおり見分けられない。
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

    def __init__(self, pre_existing, delete_at, add_at, add_ok):
        self.rules = [fw._self_rule_name()] if pre_existing else []
        self.delete_at = delete_at
        self.add_at = add_at
        self.add_ok = add_ok
        self.launches = []
        self.sleeps = []
        self.shows = 0
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
        name = next(a[len("name="):] for a in args if a.startswith("name="))
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

    def test_a_gap_then_the_rule_is_done_without_waiting_out_the_checks(self):
        """消えた確認の直後に見えたら、残りの確認を待たずに完了すること"""
        ok, msg, finished, elevated = self._run(
            pre_existing=True, delete_at=2, add_at=3, add_ok=True)
        self.assertTrue(ok, msg)
        self.assertTrue(finished)
        self.assertEqual(len(elevated.waited), 3,
                         "add を確かめた後も待ち続けている: %r" % elevated.waited)

    def test_a_first_time_add_is_still_reported_as_soon_as_it_shows(self):
        """対照: 初めて足すとき（既存の許可が無い）は今までどおり見えた時点で完了"""
        ok, msg, finished, elevated = self._run(
            pre_existing=False, delete_at=1, add_at=2, add_ok=True)
        self.assertTrue(ok, msg)
        self.assertTrue(finished)
        self.assertEqual(len(elevated.waited), 2, elevated.waited)

    def test_a_rule_that_never_shows_keeps_the_old_message(self):
        """対照: 一度も見えなければ、今までどおりの失敗の文言"""
        ok, msg, _finished, _elevated = self._run(
            pre_existing=False, delete_at=1, add_at=2, add_ok=False)
        self.assertFalse(ok, msg)
        self.assertEqual(msg, "自exe受信許可を要求したが反映を確認できず: "
                         + fw._self_rule_name())

    def test_the_wait_on_the_gui_thread_is_not_longer(self):
        """確認の間隔と回数（GUI スレッドで待つ上限）を延ばしていないこと"""
        timelines = (
            dict(pre_existing=True, delete_at=2, add_at=2, add_ok=False),
            dict(pre_existing=True, delete_at=9, add_at=9, add_ok=True),
            dict(pre_existing=False, delete_at=1, add_at=9, add_ok=True),
        )
        for timeline in timelines:
            with self.subTest(**timeline):
                _ok, _msg, _finished, elevated = self._run(**timeline)
                waited = elevated.waited
                self.assertLessEqual(len(waited), CHECKS, waited)
                self.assertTrue(all(s <= INTERVAL for s in waited), waited)
                self.assertLessEqual(elevated.shows, CHECKS)


if __name__ == "__main__":
    unittest.main()
