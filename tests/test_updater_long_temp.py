"""TEMP のパスが長いと、exe だけ旧版・同梱は新しい版の混在が残っていた件の回帰。

updater.bat は展開先（%TEMP%\\NetBeltUpdate_N_R\\zip）の中で、新しい exe を
一時名 NetBelt.exe.<STAMP>.new（STAMP = NetBeltUpdate_N_R、ふだんは 17〜21
文字）へ改名してから、xcopy でインストール先へ写す。TEMP が約 196〜204 文字を
超えると、この一時名のパスが 260 文字に届く。

実測（v1.3.1 = 441ea02 の updater.bat、LongPathsEnabled=1 のこのパソコン、
配布物と同じ並びの ZIP を %TEMP%\\NetBeltUpdates\\NetBelt-apply-*.zip に置き、
SHA-256 を第 3 引数で渡した。v1.3.2 の再調査 d1_long_temp.py）:

  - TEMP 175〜195 文字: rc=0、全部新しい版
  - TEMP 200〜210 文字: rc=1「エラー: NetBelt.exe を差し替えられませんでした /
    アプリがまだ起動したままだと…」。同梱の 5 ファイルは新しい版、
    NetBelt.exe だけ旧版の混在が残った
  - TEMP 220 文字: 同じ文言で、新しくなったのは 3/6
  - TEMP 235 文字: 「ファイルのコピーに失敗しました」

cmd の ren は長いパスでも通るが、xcopy は 260 文字以上の元のファイルを
黙って飛ばして成功（errorlevel 0）を返す。そのため move が届いていない
一時名の exe を探して 5 回失敗し、アプリは起動していないのに「起動した
ままだと」という事実と違う理由で止まっていた。インストール先が深く、
インストール先側の一時名のパスが 260 文字に届く場合も、同じく xcopy の
写し先になるので、同じ形で止まる。

利用者の決定（v1.3.2、upd-04 の (1)）: インストール先へ何も書く前に止めて、
理由を出す。

直した形: 一時名への改名の手前（インストール先へまだ何も書いていない位置）で、
展開先の一時名のパスと、インストール先の一時名のパスの長さを見る。どちらかが
260 文字以上なら「パスが長すぎるため、更新を当てられません」と場所を出し、
展開先と適用用の写しを片付けて中止する。一時名は配布物のどのファイルより
名前が長い（38 文字前後 > 同梱の最長 23 文字）ので、この 2 つを見れば足りる。
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

# 配布物と同じ並び（exe は起動しない作り物）。
FILES = {
    "NetBelt.exe": b"NEW-EXE",
    "README.txt": b"NEW-README",
    "LICENSE.txt": b"NEW-LICENSE",
    "CHANGELOG.txt": b"NEW-CHANGELOG",
    "THIRD-PARTY-NOTICES.txt": b"NEW-NOTICES",
    "mibs/README.md": b"NEW-MIBS",
}

# 展開先の一時名のパス = TEMP + 22 + 2 * len(STAMP)。STAMP は 17 文字以上なので、
# 205 文字なら %RANDOM% の桁数によらず 260 文字を超える。展開先の
# NetBelt.exe（TEMP + 17 + len(STAMP)）は 260 文字に届かない。
LONG_TEMP = 205
# インストール先の一時名のパス = フォルダ + 17 + len(STAMP)。228 文字なら
# 必ず 260 文字を超え、同梱の最長（フォルダ + 24）は 260 文字に届かない。
LONG_APP = 228


def _long_folder(root, length):
    """root の下に、ちょうど length 文字のフォルダを作って返す。"""
    path = root
    i = 0
    while len(path) < length:
        room = length - len(path) - 1
        if room <= 0:
            break
        path = os.path.join(path, ("t%d" % i + "x" * 60)[:min(60, room)])
        i += 1
    while len(path) < length:
        path += "y"
    os.makedirs(path)
    assert len(path) == length, (len(path), length)
    return path


def _long(path):
    """260 文字を超えるパスも扱えるように \\\\?\\ を前に付ける。"""
    return "\\\\?\\" + os.path.abspath(path)


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterLongTempTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_longtemp_")
        self.addCleanup(shutil.rmtree, _long(self.base), True)
        self.updater_body = io.open(UPDATER, "rb").read()

    def _prepare(self, temp_length=None, app_length=None):
        """インストール先・TEMP・適用用の写しを用意する。長さの指定は文字数。"""
        for length in (temp_length, app_length):
            if length is not None and len(self.base) > length - 20:
                self.skipTest("一時フォルダの場所が長すぎて、%d 文字の"
                              "フォルダを作れない: %s" % (length, self.base))
        if app_length is None:
            self.app_dir = os.path.join(self.base, "app")
            os.makedirs(self.app_dir)
        else:
            self.app_dir = _long_folder(os.path.join(self.base, "A"),
                                        app_length)
        os.makedirs(os.path.join(self.app_dir, "mibs"))
        with io.open(os.path.join(self.app_dir, "updater.bat"), "wb") as f:
            f.write(self.updater_body)
        for name in FILES:
            with io.open(os.path.join(self.app_dir, *name.split("/")),
                         "wb") as f:
                f.write(b"OLD")
        if temp_length is None:
            self.temp = os.path.join(self.base, "temp")
            os.makedirs(self.temp)
        else:
            self.temp = _long_folder(os.path.join(self.base, "T"),
                                     temp_length)
        updates = os.path.join(self.temp, "NetBeltUpdates")
        os.makedirs(updates)
        self.zip_path = os.path.join(updates, "NetBelt-apply-k3j9x2qa.zip")
        with zipfile.ZipFile(self.zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for name, body in FILES.items():
                z.writestr(name, body)
            z.writestr("updater.bat", self.updater_body)
        digest = hashlib.sha256()
        with io.open(self.zip_path, "rb") as f:
            digest.update(f.read())
        self.sha = digest.hexdigest()
        with io.open(self.zip_path + ".sha256", "w", encoding="ascii") as f:
            f.write(self.sha)
        with io.open(self.zip_path + ".version", "w", encoding="ascii") as f:
            f.write("9.9.9")

    def _run(self):
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            proc = subprocess.Popen(
                '"%s" "%s" "%s" "%s"' % (
                    os.path.join(self.app_dir, "updater.bat"), self.zip_path,
                    os.path.join(self.app_dir, "NetBelt.exe"), self.sha),
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env)
            code = proc.wait(timeout=300)
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    def _assert_stopped_before_writing(self, code, text):
        self.assertNotEqual(code, 0, text)
        self.assertNotIn("更新が完了しました", text, text)
        changed = []
        for name in FILES:
            with io.open(os.path.join(self.app_dir, *name.split("/")),
                         "rb") as f:
                if f.read() != b"OLD":
                    changed.append(name)
        self.assertEqual(
            changed, [],
            "インストール先の一部が新しい版に置き換わった（混在）:\n" + text)
        with io.open(os.path.join(self.app_dir, "updater.bat"), "rb") as f:
            self.assertEqual(f.read(), self.updater_body, text)
        self.assertIn("パスが長すぎる", text,
                      "止まった理由が伝わっていない:\n" + text)
        self.assertNotIn("起動したまま", text,
                         "事実と違う理由を出した:\n" + text)
        self.assertEqual(
            sorted(n for n in os.listdir(self.app_dir)
                   if n.startswith("NetBelt-update-")
                   or n.startswith("NetBelt.exe.")), [],
            "目印や一時名の exe が残った:\n" + text)
        self.assertFalse(os.path.exists(self.zip_path),
                         "中止したのに適用用の写しが残った:\n" + text)
        self.assertFalse(os.path.exists(self.zip_path + ".sha256"), text)
        self.assertFalse(os.path.exists(self.zip_path + ".version"), text)
        self.assertEqual(
            [n for n in os.listdir(self.temp)
             if n.startswith("NetBeltUpdate_")], [],
            "TEMP の作業フォルダが残った:\n" + text)

    def test_a_long_temp_stops_before_writing_the_install_folder(self):
        """TEMP が長すぎるときは、インストール先へ何も書かずに理由を出して止まること。"""
        self._prepare(temp_length=LONG_TEMP)

        code, text = self._run()

        self._assert_stopped_before_writing(code, text)

    def test_a_deep_install_folder_stops_before_writing_it(self):
        """インストール先側の一時名が長すぎるときも、何も書かずに止まること。"""
        self._prepare(app_length=LONG_APP)

        code, text = self._run()

        self._assert_stopped_before_writing(code, text)


if __name__ == "__main__":
    unittest.main()
