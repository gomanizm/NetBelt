"""updater.bat が、更新を適用して正直に報告することを検証する。

v1.1.0 の更新で実際に起きたこと:

  '・・・ションを再起動中...' is not recognized as an internal or external command
  エラー: アプリケーションの起動に失敗しました

ファイルのコピーは成功しており、アプリも起動していた。それでも
「起動に失敗しました」と表示して exit /b 1 していた。理由は 2 つ。

  1. start は成功しても errorlevel を 0 に戻さない。直前に失敗した
     コマンドがあると、その値が残ったまま `if errorlevel 1` に入る。
  2. ファイルの途中で chcp すると cmd.exe の読み取り位置がずれ、
     日本語の行が途中で切れて残りがコマンドとして実行される。
     それが 1 の「直前の失敗」を作っていた。

更新は成功しているのに失敗と言われると、利用者は手で戻そうとする。
嘘の失敗報告は、失敗そのものより害が大きい。
"""
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

def _start_line(text):
    """アプリを起動している行を返す。変数の書き方に依存しない。"""
    for line in text.splitlines():
        if line.startswith('start "" '):
            return line
    raise AssertionError("起動している行が見つからない")



class UpdaterScriptTest(unittest.TestCase):
    """実際に cmd.exe で走らせて確かめる。"""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_upd_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        # updater.bat は自分の置き場所（%~dp0）へインストールする。
        # リポジトリの実物をそのまま走らせると、リポジトリ直下へ
        # 書き込むことになる。必ず写しを作業用ディレクトリで動かす。
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        self.app_path = self._make_dummy_app(self.app_dir)

    def _make_dummy_app(self, directory):
        """再起動先の代わり。すぐ終わり、窓を残さないものを置く。

        バッチを置くと start が cmd /K で開くため、コンソールが開いた
        まま残る。全体テストを一度回すと十数個たまって画面が埋まるので、
        引数なしで即終了する exe を借りてくる。
        """
        target = os.path.join(directory, "dummy_app.exe")
        shutil.copyfile(
            os.path.join(os.environ["SystemRoot"], "System32",
                         "rundll32.exe"), target)
        return target

    def _write(self, path, text):
        io.open(path, "w", encoding="ascii", newline="").write(text)

    def _make_zip(self, entries):
        """entries: {アーカイブ内のパス: 中身}"""
        path = os.path.join(self.base, "update.zip")
        with zipfile.ZipFile(path, "w") as z:
            for name, body in entries.items():
                z.writestr(name, body)
        return path

    def _run(self, zip_path, updater=None, app_path=None):
        # start で起動した子が標準出力を受け継ぐため、パイプで待つと
        # 閉じるまで戻らないことがある。ファイルへ流して親だけを待つ。
        out_path = os.path.join(self.base, "out.txt")
        with io.open(out_path, "wb") as out:
            # 本番と同じ渡し方にする。リストで渡すと Python は空白を
            # 含む引数しか包まず、cmd が , や = で切ってしまうため、
            # テストだけ本番と違う経路を通ることになる。
            command = '"{}" "{}" "{}"'.format(
                updater or self.updater, zip_path,
                app_path or self.app_path)
            proc = subprocess.Popen(
                command,
                stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            code = proc.wait(timeout=180)
        # コンソールは chcp 65001 されるので UTF-8 で読む
        return code, io.open(out_path, encoding="utf-8", errors="replace").read()

    def _installed(self):
        p = os.path.join(self.app_dir, "NetBelt.exe")
        if not os.path.exists(p):
            return None
        return io.open(p, encoding="ascii").read()

    # --- 適用できること -------------------------------------------------

    def test_the_new_files_replace_the_old_ones(self):
        """ルート直下に NetBelt.exe がある zip を適用できること。"""
        self._write(os.path.join(self.app_dir, "NetBelt.exe"), "old")
        zip_path = self._make_zip({"NetBelt.exe": "new",
                                   "README.txt": "new-readme"})

        code, out = self._run(zip_path)

        self.assertEqual(code, 0, out)
        self.assertEqual(self._installed(), "new", out)

    def test_a_zip_that_wraps_everything_in_a_folder_still_applies(self):
        """配布 zip はフォルダで包まれていることがある。そちらも探すこと。"""
        self._write(os.path.join(self.app_dir, "NetBelt.exe"), "old")
        zip_path = self._make_zip({
            "NetBelt-v9.9.9-Windows-Portable/NetBelt.exe": "new",
            "NetBelt-v9.9.9-Windows-Portable/README.txt": "new-readme",
        })

        code, out = self._run(zip_path)

        self.assertEqual(code, 0, out)
        self.assertEqual(self._installed(), "new", out)

    # --- 嘘をつかないこと -----------------------------------------------

    def test_a_stray_failure_before_the_restart_is_not_reported_as_one(self):
        """再起動の直前に別の失敗があっても、起動失敗と言わないこと。

        これが v1.1.0 で起きた不具合そのもの。start は成功しても
        errorlevel を 0 に戻さないので、直前の失敗を start の失敗として
        読んでしまう。失敗を注入した写しで確かめる。
        """
        self._write(os.path.join(self.app_dir, "NetBelt.exe"), "old")
        zip_path = self._make_zip({"NetBelt.exe": "new"})

        # updater.bat の写しに、再起動の直前で必ず失敗する行を入れる
        original = io.open(UPDATER, encoding="utf-8", newline="").read()
        anchor = _start_line(original)
        self.assertIn(anchor, original, "起動行の形が変わっている")
        injected = original.replace(
            anchor,
            "netbelt_no_such_command_for_test 2>nul\r\n" + anchor, 1)
        # %~dp0 がインストール先になるので、写しも作業用ディレクトリへ置く
        broken = os.path.join(self.app_dir, "updater_injected.bat")
        io.open(broken, "w", encoding="utf-8", newline="").write(injected)

        code, out = self._run(zip_path, updater=broken)

        self.assertNotIn("アプリケーションの起動に失敗", out,
                         "直前の失敗を start の失敗として報告している")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._installed(), "new", out)

    def test_a_folder_with_parentheses_still_updates(self):
        """括弧を含むフォルダでも更新できること。

        `Program Files (x86)` も、同じ zip を 2 回ダウンロードしたときの
        `... (1)` も、現実によくあるフォルダ名。%VAR% は解析の段階で
        展開されるため、その `)` が if / for の括弧を閉じてしまい
        「\\app\\ was unexpected at this time」で途中停止していた。
        v1.1.0 でも同じで、更新が当たらないまま終わっていた。
        """
        nested = os.path.join(self.base, "NetBelt-v9.9.9-Portable (1)")
        app_dir = os.path.join(nested, "app")
        os.makedirs(app_dir)
        updater = os.path.join(app_dir, "updater.bat")
        shutil.copyfile(UPDATER, updater)
        app_path = self._make_dummy_app(app_dir)
        self._write(os.path.join(app_dir, "NetBelt.exe"), "old")
        zip_path = self._make_zip({"NetBelt.exe": "new"})

        code, out = self._run(zip_path, updater=updater, app_path=app_path)

        self.assertEqual(code, 0, out)
        self.assertEqual(
            io.open(os.path.join(app_dir, "NetBelt.exe"),
                    encoding="ascii").read(), "new", out)
        self.assertNotIn("was unexpected at this time", out)

    def test_a_folder_with_an_apostrophe_still_updates(self):
        """アポストロフィを含むパスでも更新できること。

        展開は PowerShell に任せているが、パスを '...' の中へ直接
        埋めていたため、パスに ' が入ると文字列がそこで閉じて壊れた。
        O'Brien のような姓はユーザープロファイル名に普通に出てくる。
        """
        # ZIP 側も同じフォルダに置くこと。展開を任せている
        # PowerShell の行を通らないと、この不具合を見られない。
        self._in_folder("it's here", zip_in_folder=True)

    def test_a_folder_with_an_ampersand_still_updates(self):
        """& を含むパスでも更新できること。

        入り直しの行で %~f0 を展開すると、その & が解析され
        コマンドの区切りとして扱われていた。
        """
        self._in_folder("a & b", zip_in_folder=True)

    def test_a_folder_with_a_comma_still_updates(self):
        """コンマを含むパスでも更新できること。

        cmd はコンマも引数の区切りとして扱う。起動側が引用符で
        包まないと、updater 側の %1 が途中で切れる。
        """
        self._in_folder("a,b", zip_in_folder=True)

    def test_a_folder_with_an_equals_sign_still_updates(self):
        """等号も同じく引数の区切りとして扱われる。"""
        self._in_folder("a=b", zip_in_folder=True)

    def test_the_zip_may_also_sit_in_a_folder_with_parentheses(self):
        """括弧はインストール先だけでなく ZIP 側にもあり得ること。"""
        self._in_folder("dl (2)", zip_in_folder=True)

    def _in_folder(self, dirname, zip_in_folder=False):
        """厄介な名前のフォルダに一式を置いて、更新できるか見る。"""
        nested = os.path.join(self.base, dirname)
        app_dir = os.path.join(nested, "app")
        os.makedirs(app_dir)
        updater = os.path.join(app_dir, "updater.bat")
        shutil.copyfile(UPDATER, updater)
        app_path = self._make_dummy_app(app_dir)
        self._write(os.path.join(app_dir, "NetBelt.exe"), "old")

        zip_dir = nested if zip_in_folder else self.base
        zip_path = os.path.join(zip_dir, "update.zip")
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr("NetBelt.exe", "new")

        code, out = self._run(zip_path, updater=updater,
                              app_path=app_path)

        self.assertEqual(code, 0, out)
        self.assertEqual(
            io.open(os.path.join(app_dir, "NetBelt.exe"),
                    encoding="ascii").read(), "new",
            "%r で更新が当たらない:\n%s" % (dirname, out))

    def test_an_exclamation_mark_in_the_path_says_why(self):
        """! を含むパスでは、黙って失敗せずに理由を言うこと。

        遅延展開がこの文字を食べるため更新できない。CHANGELOG には
        書いたが、実行時は「The system cannot find the path specified」
        で終わり、原因が分からなかった。
        """
        nested = os.path.join(self.base, "hello!world")
        app_dir = os.path.join(nested, "app")
        os.makedirs(app_dir)
        updater = os.path.join(app_dir, "updater.bat")
        shutil.copyfile(UPDATER, updater)
        app_path = self._make_dummy_app(app_dir)
        self._write(os.path.join(app_dir, "NetBelt.exe"), "old")
        zip_path = self._make_zip({"NetBelt.exe": "new"})

        code, out = self._run(zip_path, updater=updater,
                              app_path=app_path)

        self.assertNotEqual(code, 0, "失敗として返していない\n" + out)
        self.assertIn("exclamation mark", out,
                      "理由を言っていない:\n" + out)
        self.assertEqual(
            io.open(os.path.join(app_dir, "NetBelt.exe"),
                    encoding="ascii").read(), "old",
            "中途半端に書き換えている")

    def test_a_zip_without_the_app_is_reported_as_a_failure(self):
        """実行ファイルが入っていない zip を、成功と報告しないこと。

        xcopy の戻り値だけを見ていると、肝心の NetBelt.exe が
        置かれていなくても「更新完了」と出てしまう。
        """
        zip_path = self._make_zip({"README.txt": "no exe here"})

        code, out = self._run(zip_path)

        self.assertNotEqual(code, 0, "実行ファイルが無いのに成功と報告した\n" + out)
        self.assertIn("NetBelt.exe", out)

    # --- 自分自身を上書きされても壊れないこと ---------------------------

    def _zip_with_a_longer_updater(self):
        """配布 zip と同じく updater.bat を含み、長さだけ変えた zip。

        CI は `copy updater.bat portable\\` で updater.bat を配布物へ
        入れるので、更新のたびに実行中の自分自身が上書きされる。
        cmd.exe はバッチファイルを行ではなくバイト位置で読み進めるため、
        新旧の長さが違うと、上書き後の続きが新しいファイルの見当違いの
        位置から読まれる。日本語の行の断片がコマンドとして実行され、
        更新の手順が二周三周する。長さが同じなら発火しないので、
        ここでは必ず長さを変える。
        """
        original = io.open(UPDATER, encoding="utf-8", newline="").read()
        marker = "\r\n:run\r\n"
        at = original.index(marker) + len(marker)
        padding = "".join("rem padding %03d\r\n" % i for i in range(40))
        longer = original[:at] + padding + original[at:]
        self.assertNotEqual(len(longer.encode("utf-8")),
                            len(original.encode("utf-8")),
                            "長さが変わっていないと再現しない")
        return self._make_zip({"NetBelt.exe": "new",
                               "updater.bat": longer})

    def test_an_update_that_replaces_the_updater_still_succeeds(self):
        """updater.bat 自身を含む更新でも、成功と報告して終わること。"""
        self._write(os.path.join(self.app_dir, "NetBelt.exe"), "old")
        zip_path = self._zip_with_a_longer_updater()

        code, out = self._run(zip_path)

        self.assertEqual(code, 0, out)
        self.assertEqual(self._installed(), "new", out)

    def test_replacing_the_updater_runs_the_sequence_exactly_once(self):
        """更新の手順がちょうど一周して終わること。

        読み取り位置がどこへ着地するかはファイル長の差で変わるため、
        症状は毎回同じではない（手順が二周三周してアプリが二重に起動
        することも、断片が実行されて途中で死ぬこともある）。症状では
        なく「一周して完了する」ことで判定する。0 回なら途中で死に、
        2 回以上なら周回している。
        """
        self._write(os.path.join(self.app_dir, "NetBelt.exe"), "old")
        zip_path = self._zip_with_a_longer_updater()

        code, out = self._run(zip_path)

        self.assertEqual(out.count("更新が完了しました"), 1,
                         "更新の手順が一周していない\n" + out)

    def test_the_new_updater_is_installed(self):
        """新しい updater.bat がちゃんと置かれること。

        自己上書きを避けるために配布物から外してしまうと、updater は
        二度と更新されず、将来の zip の形と食い違ったままになる。
        """
        self._write(os.path.join(self.app_dir, "NetBelt.exe"), "old")
        zip_path = self._zip_with_a_longer_updater()

        self._run(zip_path)

        installed = io.open(self.updater, encoding="utf-8", newline="").read()
        self.assertIn("rem padding 000", installed,
                      "新しい updater.bat が置かれていない")


class UpdaterEncodingTest(unittest.TestCase):
    """文字化けの原因を作らないことを、ファイルの形として固定する。

    化けそのものは、コンソール・コードページ・読み取り位置が絡むため
    手元では再現できなかった。再現できない不具合こそ、条件を作らない
    ことをファイル側で守る。
    """

    def setUp(self):
        self.raw = io.open(UPDATER, "rb").read()
        self.text = self.raw.decode("utf-8")

    def test_the_file_stays_utf8_without_a_bom(self):
        """BOM を付けないこと。cmd.exe が 1 行目ごと読み違える。"""
        self.assertNotEqual(self.raw[:3], b"\xef\xbb\xbf")

    def test_every_batch_file_is_checked_out_as_crlf(self):
        """.bat が誰の手元でも CRLF で取り出されること。

        test_the_line_endings_stay_crlf は作業ツリーのファイルを見る。
        core.autocrlf=true の環境ではチェックアウト時に CRLF へ変換
        されるので、その設定に頼っているだけの状態でも常に緑になり、
        この穴を検知できない。

        cmd.exe は LF だけのバッチファイルを 1 行も正しく実行できない
        （行の途中や日本語コメントの断片がコマンドとして実行され、
        更新が当たらない）。autocrlf に守られない経路は2つある:
          - GitHub が各リリースへ自動添付する Source code アーカイブ
            （.github/release-body.md が GPL の対応ソースとして案内する）
          - core.autocrlf=false / input での clone（Linux・macOS を含む）

        eol=crlf が付いていれば、どちらの経路でも CRLF で取り出される。
        index 側が LF なのは text 属性の正常な姿なので、そこは見ない。
        """
        listed = subprocess.run(
            ["git", "ls-files", "--", "*.bat"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        batch_files = [p for p in listed.stdout.split() if p]
        self.assertTrue(batch_files, "追跡下の .bat が見つからない")

        attrs = subprocess.run(
            ["git", "check-attr", "eol", "--"] + batch_files,
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(attrs.returncode, 0, attrs.stderr)

        unprotected = [line for line in attrs.stdout.splitlines()
                       if not line.endswith(": eol: crlf")]
        self.assertEqual(
            unprotected, [],
            "CRLF が保証されていない .bat がある（cmd.exe で動かない）: %s\n"
            ".gitattributes に `*.bat text eol=crlf` が要る" % unprotected)

    def test_the_line_endings_stay_crlf(self):
        self.assertEqual(self.raw.count(b"\n") - self.raw.count(b"\r\n"), 0)

    def test_nothing_but_ascii_runs_before_the_codepage_is_settled(self):
        """コードページを決めて入り直すまでは ASCII だけであること。

        ここに日本語が 1 文字でもあると、cmd.exe の読み取り位置が
        ずれて、行の残りがコマンドとして実行される。
        """
        marker = "--utf8"
        self.assertIn(marker, self.text, "入り直しの仕組みが無くなっている")
        entry = "\r\n:run\r\n"
        self.assertIn(entry, self.text, "入り直しの目印 :run が無い")
        head = self.text[:self.text.index(entry)]
        self.assertIn("chcp 65001", head, "入り直す前にコードページを決めていない")
        non_ascii = [c for c in head if ord(c) > 127]
        self.assertEqual(non_ascii, [],
                         "コードページ確定前に非 ASCII がある: %r" % non_ascii)

    def test_the_restart_is_not_judged_by_errorlevel(self):
        """start の戻り値で成否を判定しないこと。

        start は成功しても errorlevel を 0 に戻さない。実測で確認済み。
        """
        after = self.text[self.text.index(_start_line(self.text)):]
        head = after.split("\r\n")[1:4]
        self.assertNotIn("errorlevel", "\n".join(head),
                         "start の直後で errorlevel を見ている: %r" % head)


class UpdaterLaunchTest(unittest.TestCase):
    """起動側が、区切り文字を含むパスを引用符で包むことを検証する。

    cmd はコンマと等号も引数の区切りとして扱う。Python の
    subprocess.list2cmdline は空白かタブを含む引数しか包まないので、
    リストのまま渡すと updater 側の %1 が途中で切れ、更新が
    当たらないまま終わる。
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        sys.path.insert(0, "src")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_the_paths_are_quoted_for_cmd(self):
        from unittest import mock
        from ui.main_window import MainWindow

        zip_path = r"C:\lab\a,b\NetBelt-update.zip"
        with mock.patch.object(MainWindow,
                               "_check_for_updates_on_startup"):
            window = MainWindow()
        self.addCleanup(lambda: None)

        with mock.patch("subprocess.Popen") as popen:
            window._apply_pending_update(zip_path)

        self.assertTrue(popen.called, "updater を起動していない")
        sent = popen.call_args[0][0]
        self.assertIsInstance(
            sent, str,
            "リストのまま渡している。コンマや等号で %1 が切れる")
        self.assertIn('"%s"' % zip_path, sent,
                      "ZIP のパスが引用符で包まれていない: %r" % sent)
        self.assertEqual(sent.count('"'), 6,
                         "3 つの引数それぞれを包むこと: %r" % sent)


if __name__ == "__main__":
    unittest.main()
