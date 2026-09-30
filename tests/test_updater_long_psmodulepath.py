"""呼び出し元の PSModulePath が長いと、起動し直す NetBelt の PSModulePath が消えていた件の回帰。

1.3.1 の updater.bat は、powershell を 5.1 の標準のモジュールだけで動かす
ために、呼び出し元の PSModulePath を

    set "CALLER_PSMODULEPATH=!PSModulePath!"

で控えてから差し替え、NetBelt を起動し直す直前に

    set "PSModulePath=!CALLER_PSMODULEPATH!"

で戻していた。cmd は遅延展開した後の 1 行が 8191 文字を超えると、その
コマンドを黙って実行しない。そのため控えが作られず、戻しの行は空の代入に
なって、変数そのものを消していた。

実測（v1.3.1 = 441ea02 の updater.bat、起動し直す直前に子プロセスの Python で
環境変数をそのまま書き出した。echo や set での書き出しはそれ自体が 8191 文字で
切れるので使っていない。v1.3.2 の再調査 c1_long_psmodulepath.py）:
呼び出し元の長さが 8000・8150 のときは同じ値のまま。8180・8200・9000・20000
のときは変数が無くなった。更新自体はどの長さでも rc=0「更新が完了しました」で、
エラーの表示は無い（黙って失う）。

直した形: 控えて戻すのをやめる。powershell を呼ぶ 3 か所（展開と照合、
:lock_is_stale、:lock_is_foreign）だけを setlocal の中で 5.1 の標準の
置き場所にして、終わったら endlocal（サブルーチンは exit /b）で戻す。
setlocal / endlocal は環境をコマンド行を通さずに写すので、長さの上限に
当たらない。powershell の終了コードは endlocal の後も残る。
"""
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")

# NetBelt を起動し直す直前（errorlevel を均す行の手前）に、そのときの
# PSModulePath を子プロセスの Python で書き出す差し込み。
LEVEL = b"cmd /d /c exit 0\r\n"
DUMP = b'"!NB_PY!" "!NB_DUMPER!" "!NB_ENV_DUMP!"\r\n'

DUMPER = (
    "import io, os, sys\n"
    "v = os.environ.get('PSModulePath')\n"
    "with io.open(sys.argv[1], 'w', encoding='utf-8') as f:\n"
    "    f.write('<UNDEFINED>' if v is None else v)\n"
)

# cmd の 1 行の上限（8191 文字）を確実に超える長さ。
LONG_VALUE_LENGTH = 9000


@unittest.skipUnless(sys.platform == "win32", "cmd.exe と powershell が要る")
class UpdaterLongModulePathTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_longpsmod_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        with io.open(self.app_path, "wb") as f:
            f.write(b"OLD-EXE")
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        with io.open(UPDATER, "rb") as f:
            data = f.read()
        self.assertEqual(data.count(LEVEL), 1, "均す行が 1 つではない")
        self.updater = os.path.join(self.app_dir, "updater.bat")
        with io.open(self.updater, "wb") as f:
            f.write(data.replace(LEVEL, DUMP + LEVEL))
        self.dumper = os.path.join(self.base, "dump_env.py")
        with io.open(self.dumper, "w", encoding="ascii") as f:
            f.write(DUMPER)
        self.dump = os.path.join(self.base, "psmodulepath.txt")
        self.zip_path = os.path.join(self.base, "NetBelt-apply-test.zip")
        with zipfile.ZipFile(self.zip_path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", "EXE_FROM_NEW")

    def _long_value(self):
        """存在しない置き場所を並べて、ちょうど LONG_VALUE_LENGTH 文字にする。"""
        tail = os.environ.get("PSModulePath", "")
        parts = []
        i = 0
        while len(";".join(parts + [tail])) < LONG_VALUE_LENGTH:
            parts.append(os.path.join(self.base, "modules%04d" % i))
            i += 1
        value = ";".join(parts + [tail])
        return value[len(value) - LONG_VALUE_LENGTH:]

    def _sha256(self):
        digest = hashlib.sha256()
        with io.open(self.zip_path, "rb") as f:
            digest.update(f.read())
        return digest.hexdigest()

    def test_a_long_module_path_reaches_the_restarted_app_intact(self):
        """8191 文字を超える PSModulePath も、起動し直す NetBelt へそのまま渡ること。"""
        value = self._long_value()
        self.assertEqual(len(value), LONG_VALUE_LENGTH)
        env = {k: v for k, v in os.environ.items()
               if k.upper() != "PSMODULEPATH"}
        env["PSModulePath"] = value
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        env["NB_PY"] = sys.executable
        env["NB_DUMPER"] = self.dumper
        env["NB_ENV_DUMP"] = self.dump

        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(
                '"%s" "%s" "%s" "%s"' % (self.updater, self.zip_path,
                                         self.app_path, self._sha256()),
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env)
            code = proc.wait(timeout=300)
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            text = f.read()

        # 前提: 照合・展開・差し替えまで通ったこと
        self.assertEqual(code, 0, text)
        self.assertIn("更新が完了しました", text, text)
        with io.open(self.app_path, "rb") as f:
            self.assertEqual(f.read(), b"EXE_FROM_NEW", text)

        self.assertTrue(os.path.exists(self.dump),
                        "起動し直す直前まで来ていない:\n" + text)
        with io.open(self.dump, encoding="utf-8") as f:
            seen = f.read()
        self.assertNotEqual(seen, "<UNDEFINED>",
                            "起動し直す NetBelt の PSModulePath が消えた:\n"
                            + text)
        self.assertEqual(len(seen), len(value), text)
        self.assertEqual(seen, value,
                         "起動し直す NetBelt の PSModulePath が変わった")


if __name__ == "__main__":
    unittest.main()
