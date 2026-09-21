"""古い updater.bat が、いまの updater.bat を含む更新を当てても壊れないことを固定する。

1.2.0 以前の updater.bat は、これから上書きするフォルダの中から走る。
cmd.exe はバッチファイルを「次に読むバイト位置」を覚えながら 1 行ずつ
読み直すので、実行中に自分自身が入れ替わると、続きは新しいファイルの
同じバイト位置から読まれる。行の途中に着地すれば、その行の後半だけが
コマンドとして実行され、そこから先は新しいファイルの構造に従って進む。

実測（この砂場、小さな作り物、インストール先に当時の updater.bat を置いて
いまの配布物を当てた）:

  - 当時の v1.2.0（= v1.1.1 と同一物、7,230 バイト）から当てると、子は
    バイト位置 5568、親は 1360 から読み直す。5568 の着地では
    `'56" 2>nul' is not recognized` と、事実に反する
    「ERROR: could not create a work folder in TEMP.」が出るが、
    インストール先は壊れず、親が最初からやり直して更新は当たる。
  - v1.1.0 / v1.1.1 / v1.3.0 から当てた場合も、exe と updater.bat は
    どちらも新しい版になり、「更新が完了しました」が旧版のまま出ることは
    なかった。

この着地点に何が来るかは、updater.bat を 1 文字直すたびに動く。古い版を
使っている人の「更新 1 回目」は必ずこの道を通るので、着地しても壊れない
ことを試験で押さえておく。ここでは当時のファイルそのものではなく、同じ
機構（xcopy で自分を上書きし、続きを新しいファイルの同じバイト位置から
読む）を作る最小のバッチを使い、実測した 2 つのバイト位置へ正確に
着地させる。当時のファイルは配布物にも履歴にもあるが、浅い clone では
取り出せないため、試験には持ち込まない。

併せて、呼び出し元が引数を増やしても当たることを見る。exe だけが差し
替わって updater.bat が旧版のまま残る混在は実際に起こりうるので、新しい
NetBelt が古い updater.bat を、今より多い引数で叩く形になりうる。
"""
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")

OLD_EXE = b"OLD-EXE" + b"\0" * 64
NEW_EXE = b"NEW-EXE" + b"\0" * 64

# 実測した読み直しの位置。v1.2.0（7,230 バイト）の updater.bat が
# 自分を上書きしたあと、新しいファイルのどこから読み直すか。
#   1360 … 親が再入行（cmd /d /c ...）を読み終えた位置
#   5568 … 子が xcopy 行を読み終えた位置
MEASURED_OFFSETS = (1360, 5568)

# 古いスクリプト側が xcopy の次の行まで読めてしまったときの目印。
OLD_SCRIPT_MARKER = "OLD-SCRIPT-CONTINUED"


def _rem_padding(size):
    """ちょうど size バイトになる rem 行を組み立てる（ASCII・CRLF）。"""
    if size < 0:
        raise ValueError("着地位置が近すぎる: %d" % size)
    out = []
    while size:
        chunk = size if size <= 200 else 200
        if size - chunk and size - chunk < 6:
            chunk -= 6
        out.append("rem " + "x" * (chunk - 6) + "\r\n")
        size -= chunk
    return "".join(out)


class OldScriptOverwritesItselfTest(unittest.TestCase):
    """自分を上書きした古いスクリプトの着地点が、壊れないこと。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_old2new_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        with io.open(self.app_path, "wb") as f:
            f.write(OLD_EXE)
        with io.open(os.path.join(self.app_dir, "zz_bundled.txt"), "wb") as f:
            f.write(b"old-bundled")

        # 当てる配布物。updater.bat はいまのものが入る。
        self.new_files = os.path.join(self.base, "newfiles")
        os.makedirs(self.new_files)
        self.updater_body = io.open(UPDATER, "rb").read()
        with io.open(os.path.join(self.new_files, "updater.bat"), "wb") as f:
            f.write(self.updater_body)
        with io.open(os.path.join(self.new_files, "NetBelt.exe"), "wb") as f:
            f.write(NEW_EXE)
        with io.open(os.path.join(self.new_files, "zz_bundled.txt"), "wb") as f:
            f.write(b"new-bundled")

        self.zip_path = os.path.join(self.base, "NetBelt-apply-test.zip")
        with zipfile.ZipFile(self.zip_path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", NEW_EXE)
            z.writestr("updater.bat", self.updater_body)
            z.writestr("zz_bundled.txt", b"new-bundled")
        for suffix, body in ((".sha256", "0" * 64), (".version", "9.9.9")):
            with io.open(self.zip_path + suffix, "w", encoding="ascii") as f:
                f.write(body)

    def _write_old_style_updater(self, handover_offset):
        """xcopy 行がちょうど handover_offset で終わる、最小の古い版を書く。

        これが当時の updater.bat の代わり。インストール先の中から走り、
        xcopy で自分自身を新しい updater.bat へ上書きするので、cmd.exe は
        続きを新しいファイルの handover_offset から読む。
        """
        head = "@echo off\r\nsetlocal enabledelayedexpansion\r\n"
        copy_line = ('xcopy "%s\\*" "%s\\" /E /I /Y /Q >nul 2>&1\r\n'
                     % (self.new_files, self.app_dir))
        pad = _rem_padding(handover_offset - len(head) - len(copy_line))
        # xcopy の次の行は、入れ替わりが起きていれば決して読まれない
        # （読まれるのは新しいファイルの handover_offset から先）。
        # 読まれてしまったら、この試験は機構ごと成り立っていない。
        tail = "echo " + OLD_SCRIPT_MARKER + "\r\n"
        path = os.path.join(self.app_dir, "updater.bat")
        with io.open(path, "wb") as f:
            f.write((head + pad + copy_line + tail).encode("ascii"))
        self.assertEqual(os.path.getsize(path) - len(tail), handover_offset,
                         "着地位置を作れていない")
        return path

    def _run(self, path):
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(
                'cmd /d /c ""%s" "%s" "%s""' % (path, self.zip_path,
                                                self.app_path),
                cwd=self.app_dir, stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env)
            try:
                code = proc.wait(timeout=240)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail("更新が終わらない（読み直しの着地で回り続けている）")
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    def _read(self, name):
        with io.open(os.path.join(self.app_dir, name), "rb") as f:
            return f.read()

    def _check_landing(self, offset):
        path = self._write_old_style_updater(offset)

        code, out = self._run(path)

        self.assertNotIn(OLD_SCRIPT_MARKER, out,
                         "前提が崩れている（走っている最中に入れ替わっていない）:"
                         "\n" + out)
        self.assertEqual(
            sorted(os.listdir(self.app_dir)),
            ["NetBelt.exe", "updater.bat", "zz_bundled.txt"],
            "着地した先がインストール先を壊した（位置 %d）:\n%s" % (offset, out))
        self.assertEqual(self._read("NetBelt.exe"), NEW_EXE,
                         "exe が新しい版になっていない（位置 %d）:\n%s"
                         % (offset, out))
        self.assertEqual(self._read("updater.bat"), self.updater_body,
                         "updater.bat が新しい版になっていない（位置 %d）:\n%s"
                         % (offset, out))
        if "更新が完了しました" in out:
            self.assertEqual(self._read("NetBelt.exe"), NEW_EXE,
                             "旧版のまま完了と表示した（位置 %d）:\n%s"
                             % (offset, out))
        return code, out

    def test_the_landing_of_the_parent_is_harmless(self):
        """親の読み直し位置（1360）へ着地しても、壊れないこと。"""
        self._check_landing(MEASURED_OFFSETS[0])

    def test_the_landing_of_the_child_is_harmless(self):
        """子の読み直し位置（5568）へ着地しても、壊れないこと。"""
        self._check_landing(MEASURED_OFFSETS[1])


class ExtraArgumentsAreToleratedTest(unittest.TestCase):
    """呼び出し元が引数を増やしても、更新が当たること。

    更新が半端に終わると、新しい exe と旧版の updater.bat が同じフォルダに
    残る（実測: アプリを掴んだまま 1.2.0 から当てた場合）。その状態の
    NetBelt が、今より多い引数で古い updater.bat を叩いても、余りは
    読み飛ばされて更新が当たること。

    追記（8 周目の統合時）: このテストは当初「第3引数（版のような値）も
    読み飛ばされる」を期待していた。同じ周に入った利用者の決定
    （2026-09-20 / release-03）で、第3引数は「NetBelt が確かめた ZIP の
    SHA-256」になり、食い違えば展開せずに中止する。つまり `9.9.9` のような
    値を第3引数に渡して当たってしまうほうが誤りなので、期待を「正しい
    ハッシュを渡せば当たり、第4引数以降は読み飛ばされる」へ直した。
    食い違うハッシュを拒むことは tests/test_updater_zip_hash_argument.py が見ている。
    """

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_extraargs_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        shutil.copyfile(UPDATER, os.path.join(self.app_dir, "updater.bat"))
        self.app_path = os.path.join(self.app_dir, "NetBelt.exe")
        with io.open(self.app_path, "wb") as f:
            f.write(OLD_EXE)
        self.zip_path = os.path.join(self.base, "NetBelt-apply-test.zip")
        with zipfile.ZipFile(self.zip_path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", NEW_EXE)
            z.writestr("zz_bundled.txt", b"new-bundled")

    def _run(self, *extra):
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        out_path = os.path.join(self.base, "out.txt")
        command = '"%s" "%s" "%s"' % (os.path.join(self.app_dir, "updater.bat"),
                                      self.zip_path, self.app_path)
        command += "".join(' "%s"' % a for a in extra)
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(
                command, stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env)
            code = proc.wait(timeout=180)
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    def _installed(self):
        with io.open(self.app_path, "rb") as f:
            return f.read()

    def _zip_sha256(self):
        """渡す ZIP の SHA-256（第3引数に載る値）。"""
        h = hashlib.sha256()
        with io.open(self.zip_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def test_the_hash_argument_lets_the_update_through(self):
        """第3引数に正しいハッシュを渡せば当たること。"""
        code, out = self._run(self._zip_sha256())

        self.assertEqual(code, 0, out)
        self.assertIn("更新が完了しました", out, out)
        self.assertEqual(self._installed(), NEW_EXE, out)

    def test_arguments_after_the_hash_are_ignored(self):
        """ハッシュより後ろの引数が増えても当たること。"""
        code, out = self._run(self._zip_sha256(), "--future", "x")

        self.assertEqual(code, 0, out)
        self.assertIn("更新が完了しました", out, out)
        self.assertEqual(self._installed(), NEW_EXE, out)


if __name__ == "__main__":
    unittest.main()
