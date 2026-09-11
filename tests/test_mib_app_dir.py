"""mibs/・custom_mibs.json・mib_cache.json を、作業ディレクトリではなく
アプリのディレクトリから読み書きすることを検証する。

MIBResolver はこの 3 つを相対パスで開いていたので、NetBelt.exe を別の
作業ディレクトリから起動する（ショートカットの「作業フォルダー」が
違う、別のフォルダから起動する）と、別の、あるいは存在しない MIB と
custom_mibs.json を読み、同じ OID が別の名前に解決される／解決されなく
なる。さらに起動したフォルダに mib_cache.json を書き散らす（実測）。

凍結時は exe のあるディレクトリ、それ以外はリポジトリの直下を基準にし、
そこに無いときだけ作業ディレクトリを見る。キャッシュは読んだ mibs/ の
隣（通常はアプリのディレクトリ）へ書く。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

SMI_HEAD = "TEST-SMI DEFINITIONS ::= BEGIN\n"
SMI_TAIL = "\nEND\n"


def _write_mib(directory, name, body):
    os.makedirs(os.path.join(directory, "mibs"), exist_ok=True)
    with io.open(os.path.join(directory, "mibs", name), "w",
                 encoding="utf-8") as f:
        f.write(SMI_HEAD + body + SMI_TAIL)


def _write_custom(directory, mibs):
    with io.open(os.path.join(directory, "custom_mibs.json"), "w",
                 encoding="utf-8") as f:
        json.dump({"mibs": mibs}, f)


class MibAppDirTest(unittest.TestCase):
    def setUp(self):
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-exe-")
        self.cwd_dir = tempfile.mkdtemp(prefix="netbelt-mib-cwd-")
        self._cwd = os.getcwd()
        os.chdir(self.cwd_dir)
        self.addCleanup(os.chdir, self._cwd)

    def _frozen_in_exe_dir(self):
        """exe が exe_dir にある凍結ビルドとして動かす。"""
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        frozen = mock.patch.object(sys, "frozen", True, create=True)
        executable = mock.patch.object(sys, "executable", exe)
        frozen.start()
        executable.start()
        self.addCleanup(frozen.stop)
        self.addCleanup(executable.stop)

    def _resolver(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    def test_the_app_dir_is_the_repo_root_when_not_frozen(self):
        from core import mib_resolver
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(mib_resolver.__file__))))
        self.assertEqual(os.path.normcase(mib_resolver.app_dir()),
                         os.path.normcase(repo_root))
        self.assertTrue(os.path.isdir(os.path.join(repo_root, "src")))

    def test_the_app_dir_is_the_exe_dir_when_frozen(self):
        self._frozen_in_exe_dir()
        from core import mib_resolver
        self.assertEqual(os.path.normcase(mib_resolver.app_dir()),
                         os.path.normcase(self.exe_dir))

    def test_the_exe_dir_copies_win_over_the_working_directory(self):
        """exe の隣の custom_mibs.json / mibs/ が読まれること。"""
        self._frozen_in_exe_dir()
        _write_custom(self.exe_dir, {"1.3.6.1.4.1.5555.1": "exeName"})
        _write_mib(self.exe_dir, "EXE.my",
                   "exeChild OBJECT IDENTIFIER ::= { exeName 1 }\n")
        _write_custom(self.cwd_dir, {"1.3.6.1.4.1.5555.1": "otherSideName"})
        _write_mib(self.cwd_dir, "OTHER.my",
                   "otherChild OBJECT IDENTIFIER ::= { otherSideName 1 }\n")

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.5555.1"), "exeName",
                         "作業ディレクトリの custom_mibs.json を読んでいる")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.5555.1.1"),
                         "exeChild", "作業ディレクトリの mibs/ を読んでいる")

    def test_the_cache_is_written_next_to_the_exe_not_into_the_cwd(self):
        self._frozen_in_exe_dir()
        _write_mib(self.exe_dir, "EXE.my",
                   "exeRoot OBJECT IDENTIFIER ::= { enterprises 5555 }\n")

        self._resolver()
        self.assertTrue(os.path.exists(os.path.join(self.exe_dir,
                                                    "mib_cache.json")),
                        "キャッシュが exe の隣に無い")
        self.assertEqual(os.listdir(self.cwd_dir), [],
                         "起動したフォルダにファイルを作っている")

    def test_the_working_directory_is_used_only_when_the_exe_dir_has_nothing(self):
        """exe の隣に無いときだけ、これまでどおり作業ディレクトリを見る。"""
        self._frozen_in_exe_dir()
        _write_custom(self.cwd_dir, {"1.3.6.1.4.1.5555.1": "cwdName"})
        _write_mib(self.cwd_dir, "CWD.my",
                   "cwdChild OBJECT IDENTIFIER ::= { cwdName 2 }\n")

        resolver = self._resolver()
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.5555.1"), "cwdName")
        self.assertEqual(resolver.resolve_oid("1.3.6.1.4.1.5555.1.2"), "cwdChild")


if __name__ == "__main__":
    unittest.main()
