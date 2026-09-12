"""自exe受信許可が、パスに % を含むときに別のパスへルールを作らないこと。

未昇格の経路は netsh 2 本を cmd.exe /c の 1 本の文字列に組み立てて
runas で起動する。cmd.exe は二重引用符の中でも %VAR% を展開するため、
exe の置き場所に定義済み環境変数名を挟んだ文字列がそのまま含まれると、
delete も add も展開後の別パスを対象にする（実測: C:\\Tools\\%USERNAME%\\…
が C:\\Tools\\goma\\… になった）。ルール名は一致するので rule_exists は
真を返し、受信は通らないのに「完了」と表示される。

昇格済みの経路は netsh へ引数のリストで渡すので展開されない（実測済み）。
ここでは未昇格の経路だけを見る。実ファイアウォールにも UAC にも触れない
よう、ShellExecuteW は差し替える。
"""
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, "src")

import core.firewall as fw

PERCENT_PATH = "C:\\Tools\\%USERNAME%\\NetBelt\\NetBelt.exe"
PLAIN_PATH = "C:\\Tools\\NetBelt\\NetBelt.exe"


class _FakeShellExecute:
    """ShellExecuteW の呼び出しを記録するだけの差し替え。"""

    def __init__(self):
        self.calls = []

    def __call__(self, hwnd, verb, file, params, directory, show):
        self.calls.append((verb, file, params))
        return 42        # >32 = 起動できた


class SelfProgramPathTest(unittest.TestCase):
    def _run(self, prog, rule_exists=True):
        shell = _FakeShellExecute()
        fake_ctypes = types.ModuleType("ctypes")
        fake_ctypes.windll = types.SimpleNamespace(
            shell32=types.SimpleNamespace(ShellExecuteW=shell))
        with mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}), \
                mock.patch.object(fw, "is_windows", return_value=True), \
                mock.patch.object(fw, "is_admin", return_value=False), \
                mock.patch.object(fw, "_self_program", return_value=prog), \
                mock.patch.object(fw, "rule_exists", return_value=rule_exists):
            ok, msg = fw.ensure_self_program_allow()
        return ok, msg, shell.calls

    def test_a_path_with_percent_is_refused_instead_of_expanded(self):
        """% を含むパスでは、別のパスへルールを作らずに断ること。"""
        ok, msg, calls = self._run(PERCENT_PATH)
        self.assertFalse(ok, "展開される恐れのあるパスで成功を返した: %s" % msg)
        self.assertIn("%", msg, "何が理由で断ったのか分からない: %s" % msg)
        self.assertEqual(calls, [],
                         "cmd.exe に生パスを渡して昇格実行した: %s" % calls)

    def test_a_normal_path_still_goes_through(self):
        """% を含まない通常のパスは、これまでどおり昇格経路へ進むこと。"""
        ok, msg, calls = self._run(PLAIN_PATH)
        self.assertTrue(ok, msg)
        self.assertEqual(len(calls), 1, "昇格実行が行われていない")
        self.assertEqual(calls[0][0], "runas")
        self.assertIn(PLAIN_PATH, calls[0][2])


if __name__ == "__main__":
    unittest.main()
