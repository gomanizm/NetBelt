"""取り直し用の目印を失った更新が、中止の直前に「The system cannot find the path specified.」を出していた件の回帰。

実測（ebbe593 の updater.bat、関門 1 か所、決定的。v1.3.2 の再調査
x1_release_takeover_stray.py と同じ並び。441ea02 でも同じ）:

  1) 置き土産の目印（NetBelt-update-lock、40 分前）がある
  2) A が取り直し用の目印（NetBelt-update-takeover、holder.txt 入り）を
     確保した直後で止まる（休止・スリープ相当）。その目印を 40 分前に見せる
  3) B を最後まで走らせる。B は A の取り直し用の目印を古いとみなして回収し、
     置き土産も回収して更新を当て（rc=0「更新が完了しました」）、自分の
     取り直し用の目印も外す。インストール先に目印は何も残らない
  4) A を再開する。正規名の目印はもう無いので、古さの見直しから :lock_busy
     へ抜け、「エラー: 別の更新が進行中です」で rc=1 になる（ここまでは
     設計どおり）。ところがその直前に

         The system cannot find the path specified.

     が 1 行出ていた。

出どころは :lock_busy が呼ぶ :release_takeover の :release_takeover_owned。
取り直し用の目印がまだ自分のものかを確かめるため、括弧で包んでいない

    set /p LOCK_OWNER=<"!TAKEOVER_DIR!\\holder.txt" 2>nul

で holder.txt を読むが、フォルダごと無いので入力を開けない。その失敗の
文言はリダイレクトを組み立てる段階で出るので、同じコマンドに付けた 2>nul
がまだ効いていない（:release_lock_sweep で直した upd-05 と同じ形）。
中止の案内の直前に、原因と関係のないエラーのような行が混ざっていた。

直した形: upd-05 と同じく (set /p ...) 2>nul と括弧で包み、リダイレクトの
失敗ごと 2>nul で受ける。holder.txt があるときはこれまでどおり 1 行目を
読み、自分の識別子のときだけ外す（B が作り直した目印を A が消さないのは
変わらない）。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")

LOCK_NAME = "NetBelt-update-lock"
TAKEOVER_NAME = "NetBelt-update-takeover"
DONE = "更新が完了しました"
BUSY = "別の更新が進行中です"

# chcp 65001 の下では英語で出る。念のため日本語の文言も見る。
MISSING_MESSAGES = ("The system cannot find the path specified",
                    "The system cannot find the file specified",
                    "指定されたパスが見つかりません",
                    "指定されたファイルが見つかりません")

# :claim_takeover が通って戻った直後（取り直し用の目印を保持中）。
HELD_ANCHOR = b'set "PS_LOCK=!LOCK_DIR!"'

# 関門で待つ上限（1 周およそ 1 秒）。テストが途中で落ちても、updater.bat が
# 待ち続けて残らないようにする。
GATE_LIMIT = b"300"


def _gate(var, prefix):
    return [
        b'if not defined ' + var + b' goto :' + prefix + b'_skip',
        b'echo reached>"!' + var + b'!.reached"',
        b'set "' + prefix + b'_n=0"',
        b':' + prefix + b'_wait',
        b'if exist "!' + var + b'!.go" goto :' + prefix + b'_skip',
        b'set /a ' + prefix + b'_n+=1',
        b'if !' + prefix + b'_n! geq ' + GATE_LIMIT + b' goto :'
        + prefix + b'_skip',
        b'ping -n 2 127.0.0.1 >nul',
        b'goto :' + prefix + b'_wait',
        b':' + prefix + b'_skip',
    ]


def _gated_updater():
    """取り直し用の目印を確保した直後に関門を 1 か所入れた updater.bat を返す。"""
    lines = io.open(UPDATER, "rb").read().split(b"\r\n")
    hits = [i for i, line in enumerate(lines) if line == HELD_ANCHOR]
    assert len(hits) == 2, "関門の位置がずれている: %r" % hits
    i = hits[1]
    assert lines[i - 2] == b"call :claim_takeover", lines[i - 2]
    lines[i:i] = _gate(b"NB_GATE_HELD", b"nb_held")
    return b"\r\n".join(lines)


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterReleaseTakeoverQuietTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_tkquiet_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        with io.open(os.path.join(self.app_dir, "updater.bat"), "wb") as f:
            f.write(_gated_updater())
        with io.open(os.path.join(self.app_dir, "NetBelt.exe"), "wb") as f:
            f.write(b"OLD-EXE")
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.lock = os.path.join(self.app_dir, LOCK_NAME)
        self.takeover = os.path.join(self.app_dir, TAKEOVER_NAME)
        self.procs = {}
        self.gates = []
        self.addCleanup(self._stop_everything)

    # ------------------------------------------------------------ 足場
    def _gate_path(self, name):
        path = os.path.join(self.base, "gate" + name)
        self.gates.append(path)
        return path

    def _make_zip(self, tag):
        path = os.path.join(self.base, "%s.zip" % tag)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", "EXE_FROM_%s" % tag)
        return path

    def _start(self, tag, **gates):
        out_path = os.path.join(self.base, "%s.txt" % tag)
        out = io.open(out_path, "wb")
        self.addCleanup(out.close)
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        env.pop("NB_GATE_HELD", None)
        env.update(gates)
        proc = subprocess.Popen(
            '"%s" "%s" "%s"' % (os.path.join(self.app_dir, "updater.bat"),
                                self._make_zip(tag),
                                os.path.join(self.app_dir, "NetBelt.exe")),
            stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env)
        self.procs[tag] = (proc, out_path)
        return proc

    def _output(self, tag):
        with io.open(self.procs[tag][1], encoding="utf-8",
                     errors="replace") as f:
            return f.read()

    def _must_reach(self, gate, tag):
        proc = self.procs[tag][0]
        deadline = time.time() + 180
        while time.time() < deadline:
            if os.path.exists(gate + ".reached"):
                return
            if proc.poll() is not None:
                break
            time.sleep(0.02)
        if not os.path.exists(gate + ".reached"):
            self.fail("前提が崩れている（%s が関門に着かない）:\n%s"
                      % (tag, self._output(tag)))

    @staticmethod
    def _release(gate):
        with io.open(gate + ".go", "wb") as f:
            f.write(b"go")

    def _finish(self, tag):
        proc = self.procs[tag][0]
        proc.wait(timeout=300)
        return proc.returncode, self._output(tag)

    def _stop_everything(self):
        for gate in self.gates:
            try:
                self._release(gate)
            except OSError:
                pass
        for proc, _ in self.procs.values():
            try:
                proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                subprocess.call("taskkill /F /T /PID %d" % proc.pid,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    @staticmethod
    def _age(path, minutes=40):
        old = time.time() - minutes * 60
        os.utime(path, (old, old))

    def _markers(self):
        return sorted(n for n in os.listdir(self.app_dir)
                      if n.startswith("NetBelt-update-"))

    @staticmethod
    def _stray(text):
        return [line for line in text.splitlines()
                if any(m in line for m in MISSING_MESSAGES)]

    # ------------------------------------------------------------ 本体
    def test_a_run_whose_takeover_marker_is_gone_aborts_quietly(self):
        """取り直し用の目印を失った側の中止に、見つからないという行が出ないこと。"""
        os.makedirs(self.lock)
        self._age(self.lock)
        held_a = self._gate_path("HeldA")

        # 1) A が取り直し用の目印を持ったまま止まる -> 40 分前に見せる
        self._start("A", NB_GATE_HELD=held_a)
        self._must_reach(held_a, "A")
        self.assertTrue(
            os.path.isfile(os.path.join(self.takeover, "holder.txt")),
            "前提が崩れている（A の取り直し用の目印に holder.txt が無い）:\n"
            + self._output("A"))
        self._age(self.takeover)

        # 2) B が A の取り直しと置き土産を回収して最後まで当てる
        self._start("B")
        b_code, b_text = self._finish("B")
        self.assertEqual(b_code, 0, "前提が崩れている（B が当たらない）:\n"
                         + b_text)
        self.assertIn(DONE, b_text, b_text)
        self.assertEqual(self._markers(), [],
                         "前提が崩れている（B の後に目印が残った）:\n" + b_text)
        self.assertEqual(self._stray(b_text), [],
                         "B の出力にエラーのような行が出た:\n" + b_text)

        # 3) A を再開する -> 取り直し用の目印はもう無い
        self._release(held_a)
        a_code, a_text = self._finish("A")
        self.assertNotEqual(a_code, 0, "A が成功として返った:\n" + a_text)
        self.assertIn(BUSY, a_text, a_text)
        self.assertNotIn(DONE, a_text, a_text)
        with io.open(os.path.join(self.app_dir, "NetBelt.exe"), "rb") as f:
            self.assertEqual(f.read(), b"EXE_FROM_B", a_text)
        self.assertEqual(self._markers(), [],
                         "A の中止の後に目印が残った:\n" + a_text)

        self.assertEqual(self._stray(a_text), [],
                         "中止の案内の前にエラーのような行が出た:\n" + a_text)


if __name__ == "__main__":
    unittest.main()
