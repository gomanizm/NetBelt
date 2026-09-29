"""自exe の受信ブロックを消す netsh の delete に、この命令が受け付けない引数を付けないこと。

何が起きていたか（実測、b2858c4。netsh は差し替えで組み立てた引数を取り出した
だけで、実際のファイアウォールには触れていない）: ensure_self_program_allow は
管理者の経路で
  ['advfirewall', 'firewall', 'delete', 'rule', 'name=all', 'dir=in',
   'action=block', 'program=<自exe>']
を、昇格の経路で cmd.exe へ
  '/c netsh advfirewall firewall delete rule name=all dir=in action=block
   program="<自exe>" & netsh advfirewall firewall add rule ...'
を渡していた。netsh advfirewall firewall delete rule の引数は name / dir /
profile / program / service / localip / remoteip / localport / remoteport /
protocol だけで（この PC の `delete rule /?` と Microsoft の文書
「Netsh AdvFirewall Firewall Commands」の delete rule）、action は無い。未昇格の
まま存在しない規則名で試すと、action=block 付きは権限の確認より前に
『'action' はこのコマンドの有効な引数ではありません。』（rc=1）で断られ、
action の無い同じ命令は『要求された操作には、権限の昇格が必要です。』まで
進んだ。つまり delete は毎回引数の誤りで何もせず、自exe のブロック規則は
一度も消えていなかった（v1.0.0 から。add だけが通っていた）。

どう直したか: delete から action=block を外す。netsh の文書どおり、
name=all は「ほかの引数に一致する規則をすべて」消し、program= は「その
プログラムに一致する規則だけ」に絞るので、消えるのは自exe を対象にした受信
規則で、ブロックだけでなく許可も消える（初回のプロンプトが作った許可、
前回までに足した "NetBelt - app inbound (self)"）。その直後の add が自exe の
許可（全プロファイル・相手を問わない）を作り直すので、消えた許可より狭く
なることは無い。そのため順序は delete → add のまま、昇格の経路の区切りは
& のまま（delete は消す物が無いと rc=1 なので、&& にすると add が走らない）に
する。ブロックの除去の成否は確かめていないので、成功の文言の但し書き
（_BLOCK_NOT_CHECKED）は残す。GPO で配られた規則はローカルの delete では
消えない。
"""
import shlex
import subprocess
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, "src")

import core.firewall as fw  # noqa: E402

PROG = "C:\\Program Files\\NetBelt\\NetBelt.exe"
NO_MATCH = b"No rules match the specified criteria.\r\n"

# netsh advfirewall firewall delete rule が受け付ける引数（`delete rule /?`）
DELETE_RULE_KEYS = {"name", "dir", "profile", "program", "service", "localip",
                    "remoteip", "localport", "remoteport", "protocol"}


def _keys(args):
    return [a.split("=", 1)[0].lower() for a in args if "=" in a]


class _Netsh:
    """netsh の模擬。呼ばれた引数を記録し、delete は消す物が無い（rc=1）、add は通る"""

    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        rc = 0 if args[2] == "add" else 1
        out = b"Ok.\r\n" if rc == 0 else NO_MATCH
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr=b"")


class SelfRuleDeleteArgsTest(unittest.TestCase):
    def setUp(self):
        for name, val in (("is_windows", True), ("_self_program", PROG)):
            p = mock.patch.object(fw, name, return_value=val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("time.sleep")
        p.start()
        self.addCleanup(p.stop)

    def _check_delete(self, args):
        """delete の引数が netsh の受け付けるものだけで、自exe の受信規則に絞られていること"""
        self.assertEqual([a.lower() for a in args[:4]],
                         ["advfirewall", "firewall", "delete", "rule"], args)
        rest = args[4:]
        unknown = [k for k in _keys(rest) if k not in DELETE_RULE_KEYS]
        self.assertEqual(unknown, [],
                         "delete rule が受け付けない引数を付けている（netsh は "
                         "何もせずに断る）: %s" % rest)
        self.assertTrue(all("=" in a for a in rest), rest)
        # 消す範囲: 受信（dir=in）の、自exe を対象にした規則だけ。どちらかが
        # 抜けると、ほかのプログラムや送信の規則まで消える
        self.assertIn("name=all", rest)
        self.assertIn("dir=in", rest)
        self.assertIn("program=" + PROG, rest)

    def test_admin_path_deletes_with_valid_arguments_then_adds(self):
        netsh = _Netsh()
        with mock.patch.object(fw, "is_admin", return_value=True), \
                mock.patch.object(fw, "_netsh", netsh):
            ok, msg = fw.ensure_self_program_allow()
        self.assertEqual([c[2] for c in netsh.calls], ["delete", "add"],
                         "許可を足す前に消していない（後で消すと足した許可も消える）")
        self._check_delete(netsh.calls[0])
        self.assertIn("action=allow", netsh.calls[1])
        self.assertIn("program=" + PROG, netsh.calls[1])
        self.assertTrue(ok, msg)
        # 除去の成否は確かめていないので、但し書きは残す
        self.assertIn(fw._BLOCK_NOT_CHECKED, msg)

    def test_elevated_path_deletes_with_valid_arguments_then_adds(self):
        calls = []

        def shell(hwnd, verb, file, params, directory, show):
            calls.append((verb, file, params))
            return 42        # >32 = 起動できた
        fake_ctypes = types.ModuleType("ctypes")
        fake_ctypes.windll = types.SimpleNamespace(
            shell32=types.SimpleNamespace(ShellExecuteW=shell))
        with mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}), \
                mock.patch.object(fw, "is_admin", return_value=False), \
                mock.patch.object(fw, "rule_exists", return_value=True):
            ok, msg = fw.ensure_self_program_allow()
        self.assertEqual(len(calls), 1, calls)
        verb, file, params = calls[0]
        self.assertEqual((verb, file.lower()), ("runas", "cmd.exe"))
        self.assertTrue(params.startswith("/c "), params)
        # delete の成否によらず add を走らせる（消す物が無いと delete は rc=1）
        self.assertNotIn("&&", params)
        self.assertNotIn("||", params)
        parts = params[len("/c "):].split(" & ")
        self.assertEqual(len(parts), 2, params)
        delete, add = (shlex.split(p) for p in parts)
        self.assertEqual(delete[0].lower(), "netsh", delete)
        self._check_delete(delete[1:])
        self.assertEqual([a.lower() for a in add[:5]],
                         ["netsh", "advfirewall", "firewall", "add", "rule"], add)
        self.assertIn("action=allow", add)
        self.assertIn("program=" + PROG, add)
        self.assertTrue(ok, msg)
        self.assertIn(fw._BLOCK_NOT_CHECKED, msg)


if __name__ == "__main__":
    unittest.main()
