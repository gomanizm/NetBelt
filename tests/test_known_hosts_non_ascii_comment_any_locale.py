"""known_hosts の日本語の注釈のテストが、既定の文字コードによらず直した経路を通ることを検証する。

対象は test_known_hosts_non_ascii_comment.py（注釈欄・コメント行に日本語が
あっても全機器が中止にならないこと、ファイルそのものを開けないときは今までどおり
中止すること）。

何が起きていたか。1.3.1 のタグで走ったリリースのワークフロー（GitHub の
windows-latest、英語の Windows・Python 3.11）で、
test_unreadable_file_still_refuses_every_host が『HostKeyStoreError not raised』
で落ちた。このパソコン（日本語の Windows・Python 3.12）では通る。製品の
振る舞いは正しい。対象のテストは、paramiko の HostKeys.load（open(filename,
"r")。既定の文字コードで読む）が UTF-8 の注釈で UnicodeDecodeError になり、
NetBelt 自前の読み込み（load_known_hosts）の経路へ入ることを前提にしていた。
既定の文字コードが cp932 なら「ラボ」の UTF-8（E3 83 A9 E3 83 9C）は読めずに
例外になるが、cp1252（英語の Windows）や UTF-8（Python の UTF-8 モード）では
読めてしまい、その経路に入らない。load_known_hosts を PermissionError に
差し替えても呼ばれないので、中止にならなかった。
  - 注釈の UTF-8 を各文字コードで読んだ結果（実測）: 「ラボ」「予備機（待機）」は
    cp932 だけ失敗し、cp1252 と UTF-8 では読める。「# 検証用ﾙｰﾀ ★重要★」は
    cp932 と cp1252 で失敗し、UTF-8 では読める。CI ではほかの 3 件も、直した
    経路を通らないまま通っていた。
  - 直す前の対象ファイルを、paramiko の読み込みの既定の文字コードを cp1252 に
    して流すと（英語の Windows の代わり）、CI と同じ『HostKeyStoreError not
    raised』で落ちた。PYTHONUTF8=1（UTF-8 モード）で流しても同じ 1 件が落ちた。

どう直したか（テスト側だけ。製品のコードは変えない）。対象のテストの setUp で、
paramiko.hostkeys が使う open を、文字コードを指定しないテキストの読み書きを
cp932 で開くものに差し替える（日本語の Windows の既定と同じ）。どの環境でも
paramiko の読み込みは cp932 で読んで失敗し、直した経路に入る。あわせて、
known_hosts を書いた直後に、paramiko の読み込みが UnicodeDecodeError になる
ことを前提として確かめる（崩れたら黙って通らずにそこで落ちる）。

このファイルは、既定の文字コードが違う環境を 2 通り作り（paramiko の読み込み
だけ cp1252 にする・Python の UTF-8 モードの別プロセスで流す）、その中で
対象のテストがすべて通ることを確かめる。
"""
import builtins
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

TARGET = Path(__file__).with_name("test_known_hosts_non_ascii_comment.py")
TARGET_CLASS = "KnownHostsNonAsciiCommentTest"


def _open_with_cp1252_default(file, mode="r", buffering=-1, encoding=None,
                              *args, **kwargs):
    """文字コードを指定しないテキストの open を、英語の Windows と同じ cp1252 で開く"""
    if "b" not in mode and encoding is None:
        encoding = "cp1252"
    return builtins.open(file, mode, buffering, encoding, *args, **kwargs)


class KnownHostsNonAsciiCommentAnyLocaleTest(unittest.TestCase):
    def _problems(self, result):
        return ["%s\n%s" % (test.id(), trace)
                for test, trace in result.failures + result.errors]

    def test_target_passes_when_paramiko_reads_with_cp1252(self):
        import paramiko.hostkeys
        spec = importlib.util.spec_from_file_location("_cp1252_target", TARGET)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite = unittest.defaultTestLoader.loadTestsFromName(TARGET_CLASS, module)
        result = unittest.TestResult()

        with mock.patch.object(paramiko.hostkeys, "open",
                               _open_with_cp1252_default, create=True):
            suite.run(result)

        problems = self._problems(result)
        if problems:
            self.fail("既定の文字コードが cp1252 の環境で、対象のテストが通らない:\n\n"
                      + "\n\n".join(problems))
        self.assertEqual(5, result.testsRun)
        self.assertEqual([], result.skipped)

    def test_target_passes_in_python_utf8_mode(self):
        root = TARGET.parent.parent
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("PYTHONUTF8", None)
        done = subprocess.run(
            [sys.executable, "-X", "utf8", "-m", "pytest", "-q",
             "-p", "no:cacheprovider", str(TARGET)],
            cwd=str(root), env=env, capture_output=True, timeout=300)
        output = done.stdout.decode("utf-8", "replace")
        self.assertEqual(0, done.returncode,
                         "UTF-8 モードで対象のテストが通らない:\n" + output[-4000:])
        self.assertIn("5 passed", output)


if __name__ == "__main__":
    unittest.main()
