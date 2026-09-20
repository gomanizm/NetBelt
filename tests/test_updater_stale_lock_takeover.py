"""古い目印の回収中に、インストール先の排他が二重に取られる件の回帰。

tests/test_updater_shared_appdir_lock.py は「新しい目印での中止」と
「古い目印の回収」を別々に見ており、その二つが重なる順番を見ていなかった。

実測（検査役 cx5j-check-release の p2_stale_lock_race.py、16101ef）:
古い目印 L0 を置き、B を「Get-Item で古いと判定した直後」で止めてから
A を走らせる（A は L0 を回収して自分の目印 L1 を作り、差し替えの直後で
停止）。そこで B を再開すると、

    B: rc=0、『別の更新が進行中です』=False、『更新が完了しました』=True
    A: rc=0、『更新が完了しました』=True
    据わった exe=EXE_FROM_B / 同梱=BUNDLED_FROM_B

B の Remove-Item は L0 ではなく A の生きている目印 L1 を消し、その後の md が
通って B も目印を持てた。A は A の版を承認したのに B の版が据わり、両方が
完了を表示した＝目印を入れた動機がそのまま破れていた。
関門なしで自然に重ねた試験（p6_stale_race_natural.py、12 回）でも 2 回、
両方が「目印を取り除きました」を出している（負けた側はその後の md に失敗して
中止したので、被害は「走っている側の目印が消える」まで）。

直し方: 回収を「見てから消す」ではなく「つかんでから消す」へ変える。
  1. 古いと判定したら、まず ren で一意な名前（NetBelt-update-lock.<STAMP>.old）
     へ改名して掴む。ren は同時に 1 つしか成功しないので、負けた側は中止する。
  2. 掴んでから、掴んだものがまだ古いかを見直す。見てから掴むまでの間に
     別の更新が取り直していたら、それは生きている目印なので元の名前へ戻す。
  3. 目印の中の holder.txt へ実行ごとの識別子（親が md で確保した作業
     フォルダ名＝STAMP）を書き、終わりに外すのは中身が自分の識別子と
     一致するときだけにする（以前は LOCK_HELD だけを見て無条件に消しており、
     奪われた後に他人の目印を消していた）。
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

# A 用。move /y の直後、差し替えの確認へ進む手前で待たせる。
# （tests/test_updater_shared_appdir_lock.py と同じ位置）
SWAP_ANCHOR = b'if not exist "!APP_DIR!NetBelt.exe" ('
SWAP_GATE = [
    b'if not defined NB_TEST_GATE goto :nb_test_skip',
    b'echo reached>"!NB_TEST_GATE!.reached"',
    b':nb_test_wait',
    b'if not exist "!NB_TEST_GATE!.go" ( ping -n 2 127.0.0.1 >nul '
    b'& goto :nb_test_wait )',
    b':nb_test_skip',
]

# B 用。目印を「古い」と判定した直後、それに手を付ける手前で待たせる。
# 判定の中括弧の中へ差し込むので、古くないと判定した回は素通りする。
STALE_MARK = b"$d.LastWriteTime -lt (Get-Date).AddMinutes(-10)) { "
STALE_PAUSE = (
    b"Set-Content -LiteralPath ($env:NB_GATE_STALE + '.reached') -Value 'x'; "
    b"while (-not (Test-Path -LiteralPath ($env:NB_GATE_STALE + '.go'))) { "
    b"Start-Sleep -Milliseconds 20 }; ")


def _gated_updater():
    """A 用と B 用の関門を 1 か所ずつ入れた updater.bat を返す。"""
    lines = io.open(UPDATER, "rb").read().split(b"\r\n")

    idx = lines.index(SWAP_ANCHOR)
    assert lines[idx - 1] == b":swapped", "A 用の関門の位置がずれている"
    lines = lines[:idx] + SWAP_GATE + lines[idx:]

    hits = [i for i, line in enumerate(lines) if STALE_MARK in line]
    assert len(hits) == 1, "B 用の関門の位置がずれている: %r" % (hits,)
    i = hits[0]
    gated = lines[i].replace(STALE_MARK, STALE_MARK + STALE_PAUSE)
    lines = (lines[:i]
             + [b'if defined NB_GATE_STALE goto :nb_gate_ps',
                lines[i],
                b'goto :nb_gate_done',
                b':nb_gate_ps',
                gated,
                b':nb_gate_done']
             + lines[i + 1:])
    return b"\r\n".join(lines)


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterStaleLockTakeoverTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_stalelock_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        with io.open(os.path.join(self.app_dir, "updater.bat"), "wb") as f:
            f.write(_gated_updater())
        self._write("NetBelt.exe", b"OLD-EXE")
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.gate_a = os.path.join(self.base, "gateA")
        self.gate_b = os.path.join(self.base, "gateB")
        self.lock = os.path.join(self.app_dir, LOCK_NAME)

    def _write(self, name, body):
        with io.open(os.path.join(self.app_dir, name), "wb") as f:
            f.write(body)

    def _read(self, name):
        with io.open(os.path.join(self.app_dir, name), "rb") as f:
            return f.read()

    def _make_zip(self, tag):
        path = os.path.join(self.base, "%s.zip" % tag)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", "EXE_FROM_%s" % tag)
            z.writestr("zz_bundled.txt", "BUNDLED_FROM_%s" % tag)
        return path

    def _start(self, tag, swap_gate=None, stale_gate=None):
        out_path = os.path.join(self.base, "%s.txt" % tag)
        out = io.open(out_path, "wb")
        self.addCleanup(out.close)
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        env.pop("NB_TEST_GATE", None)
        env.pop("NB_GATE_STALE", None)
        if swap_gate:
            env["NB_TEST_GATE"] = swap_gate
        if stale_gate:
            env["NB_GATE_STALE"] = stale_gate
        proc = subprocess.Popen(
            '"%s" "%s" "%s"' % (os.path.join(self.app_dir, "updater.bat"),
                                self._make_zip(tag),
                                os.path.join(self.app_dir, "NetBelt.exe")),
            stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env)
        return proc, out_path

    def _snapshot(self):
        """インストール先の中身（相対パス→バイト列）を写し取る。"""
        taken = {}
        for root, _dirs, names in os.walk(self.app_dir):
            for name in names:
                path = os.path.join(root, name)
                with io.open(path, "rb") as f:
                    taken[os.path.relpath(path, self.app_dir)] = f.read()
        return taken

    def _output(self, out_path):
        with io.open(out_path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def _wait_for_the_gate(self, gate, proc, out_path, who):
        deadline = time.time() + 300
        while time.time() < deadline:
            if os.path.exists(gate + ".reached"):
                return
            if proc.poll() is not None:
                self.fail("%s が関門へ着く前に終わった:\n%s"
                          % (who, self._output(out_path)))
            time.sleep(0.02)
        self.fail("%s が関門へ着かない:\n%s" % (who, self._output(out_path)))

    def _release(self, gate):
        with io.open(gate + ".go", "wb") as f:
            f.write(b"go")

    def _put_a_stale_marker(self):
        os.makedirs(self.lock)
        old = time.time() - 40 * 60
        os.utime(self.lock, (old, old))

    def test_a_stale_marker_is_not_handed_to_two_updates(self):
        """古い目印を見た二つの更新が、両方とも排他を取らないこと。"""
        self._put_a_stale_marker()

        b_proc, b_path = self._start("B", stale_gate=self.gate_b)
        self._wait_for_the_gate(self.gate_b, b_proc, b_path, "B")
        # B は「この目印は古い」と判定したところで止まっている。

        a_proc, a_path = self._start("A", swap_gate=self.gate_a)
        try:
            self._wait_for_the_gate(self.gate_a, a_proc, a_path, "A")
            # A は古い目印を回収し、自分の目印を持ったまま止まっている。
            self.assertTrue(os.path.isdir(self.lock),
                            "前提が崩れている（A の目印が無い）")
            self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A",
                             "前提が崩れている（A がまだ据えていない）")
            before = self._snapshot()

            self._release(self.gate_b)
            b_code = b_proc.wait(timeout=300)
            b_text = self._output(b_path)

            self.assertNotEqual(b_code, 0, "B が成功として返った:\n" + b_text)
            self.assertIn("別の更新が進行中です", b_text, b_text)
            self.assertNotIn("更新が完了しました", b_text, b_text)
            self.assertEqual(self._snapshot(), before,
                             "B がインストール先を書き換えた:\n" + b_text)
        finally:
            self._release(self.gate_a)
            a_code = a_proc.wait(timeout=300)
        a_text = self._output(a_path)

        self.assertEqual(a_code, 0, "A が失敗として返った:\n" + a_text)
        self.assertIn("更新が完了しました", a_text, a_text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A",
                         "A が承認した版とは別の exe が据わった:\n" + a_text)
        self.assertEqual(self._read("zz_bundled.txt"), b"BUNDLED_FROM_A",
                         "同梱ファイルが混ざった:\n" + a_text)

    def test_the_marker_is_still_taken_over_once_it_is_old(self):
        """掴んでから消す形にしても、古い目印は回収できること。"""
        self._put_a_stale_marker()

        proc, out_path = self._start("A")
        code = proc.wait(timeout=300)
        text = self._output(out_path)

        self.assertEqual(code, 0, "古い目印で更新が止まった:\n" + text)
        self.assertIn("目印を取り除きました", text, text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A", text)
        self.assertEqual([name for name in os.listdir(self.app_dir)
                          if name.startswith(LOCK_NAME)], [],
                         "目印（改名した分を含む）が残った:\n" + text)

    def test_a_marker_that_was_taken_over_is_left_alone_at_the_end(self):
        """自分の目印でなくなっていたら、終わりに消さないこと。"""
        foreign = b"NetBeltUpdate_9_99999"
        proc, out_path = self._start("A", swap_gate=self.gate_a)
        try:
            self._wait_for_the_gate(self.gate_a, proc, out_path, "A")
            self.assertTrue(os.path.isdir(self.lock),
                            "前提が崩れている（A の目印が無い）")
            # 目印が別の更新のものへ入れ替わった状態にする
            shutil.rmtree(self.lock)
            os.makedirs(self.lock)
            with io.open(os.path.join(self.lock, "holder.txt"), "wb") as f:
                f.write(foreign + b"\r\n")
        finally:
            self._release(self.gate_a)
            code = proc.wait(timeout=300)
        text = self._output(out_path)

        self.assertEqual(code, 0, "A が失敗として返った:\n" + text)
        self.assertTrue(os.path.isdir(self.lock),
                        "他の更新の目印まで消した:\n" + text)
        with io.open(os.path.join(self.lock, "holder.txt"), "rb") as f:
            self.assertEqual(f.read().strip(), foreign,
                             "他の更新の目印を書き換えた:\n" + text)


if __name__ == "__main__":
    unittest.main()
