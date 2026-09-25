"""改名を戻す側と、名前によらず掃く側がすれ違うと、目印がまた生き返る（回帰）。

tests/test_updater_lock_rename_window.py の直しで、:release_lock は「正規名の
holder.txt が自分のものでなければ、!APP_DIR!NetBelt-update-lock.*.old を for /d
で掃く」形になった。ところがこの 2 段は「正規名を見た時点のスナップショット」で
分岐するだけなので、見てから掃くまでの間に、つかんだ側が改名を元へ戻すと、
どちらの段にも引っかからない。

実測（検査役 cx5m-check-release の p21_release_lock_window.py と
cx5m-check-release-2 の b1_putback_vs_sweep.py、どちらも 2/2 で決定的）:
古い目印を B が「古い」と判定した直後で止め、A にそれを引き継がせて自分の目印を
作らせる。B を進めて A の目印を .old へ改名させ、A を :release_lock の掃きの手前で
止めてから B を進めると、B は「まだ新しい」と分かって .old を正規名へ戻す。その
あと A の掃きを動かしても .old はもう無いので、A は自分の目印を片付けられない。
結果、誰も持っていない NetBelt-update-lock が A の holder.txt 入り・更新日時ほぼ
現在で残り、続けて走らせた更新は rc=1『別の更新が進行中です』。正常に終わった
直後から約 10 分間、更新が弾かれ続ける（tests/test_updater_lock_rename_window.py
が閉じたはずの (a) と同じ症状）。

直し方: :release_lock を、スナップショットに頼らない形にする。「正規名を見る →
.old を掃く」を 2 周させる。つかんだ側の戻しは 1 回きり（:lock_put_back は
:lock_busy へ抜けて終わる）なので、目印がどちらの名前にあるかの移り変わりも 1 回
しか起きない。正規名・.old・正規名・.old の順に 4 回見れば、その 1 回がどこで
起きても必ずどちらかに引っかかる。消す条件は holder.txt が自分の識別子と一致する
ことのままなので、動いている別の更新の目印にも、利用者が置いたものにも当たらない。

前提の作り方だけ、利用者の決定（2026-09-23 / release-02、取り直しの直列化）に
合わせて移した。「古いと判定した後に、別の更新が取り直した生きている目印を
掴む」筋は無くなったが、窓そのものは残る: 更新が 10分以上かかると、持ち主が
動いていても目印は「古い」と見えるので、取り直しは生きている目印を掴む。
そこで A を差し替えの直後で止め、その目印を古く見せてから B を走らせ、B が
掴んだ後に更新日時を戻して「掴んだものはまだ新しい」状態を作る。確かめている
中身（改名の戻しと掃きのすれ違いで目印が生き返らないこと）は変えていない。
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

# A 用その1。move /y の直後、差し替えの確認へ進む手前で待たせる。
# （tests/test_updater_lock_rename_window.py と同じ位置）
SWAP_ANCHOR = b'if not exist "!APP_DIR!NetBelt.exe" ('
SWAP_GATE = [
    b'if not defined NB_TEST_GATE goto :nb_test_skip',
    b'echo reached>"!NB_TEST_GATE!.reached"',
    b':nb_test_wait',
    b'if not exist "!NB_TEST_GATE!.go" ( ping -n 2 127.0.0.1 >nul '
    b'& goto :nb_test_wait )',
    b':nb_test_skip',
]

# A 用その2。:release_lock が正規名を見終えて、.old を掃く手前で待たせる。
# ここが本件の窓そのもの。掃く行を目印にするので、直しの前後どちらの
# 並びでも同じ位置に入る。
SWEEP_MARK = b"NetBelt-update-lock.*.old"
SWEEP_GATE = [
    b'if not defined NB_GATE_SWEEP goto :nb_sweep_skip',
    b'echo reached>"!NB_GATE_SWEEP!.reached"',
    b':nb_sweep_wait',
    b'if not exist "!NB_GATE_SWEEP!.go" ( ping -n 2 127.0.0.1 >nul '
    b'& goto :nb_sweep_wait )',
    b':nb_sweep_skip',
]

# B 用。ren が通った直後、つかめたかを見直す手前で待たせる。
GRAB_ANCHOR = b'set "PS_LOCK=!LOCK_OLD!"'
GRAB_GATE = [
    b'if not defined NB_GATE_GRAB goto :nb_grab_skip',
    b'echo reached>"!NB_GATE_GRAB!.reached"',
    b':nb_grab_wait',
    b'if not exist "!NB_GATE_GRAB!.go" ( ping -n 2 127.0.0.1 >nul '
    b'& goto :nb_grab_wait )',
    b':nb_grab_skip',
]


def _gated_updater():
    """関門を 3 か所（A 用 2・B 用 1）入れた updater.bat を返す。"""
    lines = io.open(UPDATER, "rb").read().split(b"\r\n")

    idx = lines.index(SWAP_ANCHOR)
    assert lines[idx - 1] == b":swapped", "A 用の関門の位置がずれている"
    lines = lines[:idx] + SWAP_GATE + lines[idx:]

    hits = [i for i, line in enumerate(lines)
            if SWEEP_MARK in line and line.lstrip().startswith(b"for /d")]
    assert len(hits) == 1, "掃きの関門の位置がずれている: %r" % (hits,)
    lines = lines[:hits[0]] + SWEEP_GATE + lines[hits[0]:]

    idx = lines.index(GRAB_ANCHOR)
    assert lines[idx - 1] == b'if not exist "!LOCK_OLD!" goto :lock_busy', \
        "つかんだ直後の関門の位置がずれている"
    lines = lines[:idx] + GRAB_GATE + lines[idx:]

    return b"\r\n".join(lines)


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterLockPutBackWindowTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_putback_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        with io.open(os.path.join(self.app_dir, "updater.bat"), "wb") as f:
            f.write(_gated_updater())
        self._write("NetBelt.exe", b"OLD-EXE")
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.gate_swap = os.path.join(self.base, "gateSwap")
        self.gate_sweep = os.path.join(self.base, "gateSweep")
        self.gate_grab = os.path.join(self.base, "gateGrab")
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
        return path

    def _start(self, tag, swap=None, sweep=None, grab=None):
        out_path = os.path.join(self.base, "%s.txt" % tag)
        out = io.open(out_path, "wb")
        self.addCleanup(out.close)
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        for name in ("NB_TEST_GATE", "NB_GATE_SWEEP", "NB_GATE_GRAB"):
            env.pop(name, None)
        if swap:
            env["NB_TEST_GATE"] = swap
        if sweep:
            env["NB_GATE_SWEEP"] = sweep
        if grab:
            env["NB_GATE_GRAB"] = grab
        proc = subprocess.Popen(
            '"%s" "%s" "%s"' % (os.path.join(self.app_dir, "updater.bat"),
                                self._make_zip(tag),
                                os.path.join(self.app_dir, "NetBelt.exe")),
            stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env)
        return proc, out_path

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

    def _age_the_marker(self):
        """動いている更新の目印を「10分より古い」状態にする。

        更新が 10分以上かかると、持ち主が動いていても目印は「古い」と
        見える。取り直しが生きている目印を掴みうるのは、取り直しが
        直列化された後はこの筋だけになった。
        """
        old = time.time() - 40 * 60
        os.utime(self.lock, (old, old))

    def _freshen_the_grabbed_marker(self):
        """掴まれた目印（.old）を「まだ新しい」状態へ戻す。

        掴んだ側は、掴んだものが置き土産かどうかを掴んだ後に見直す。
        ここで新しくしておくと、見直しで「まだ新しい」と分かり、元の名前へ
        戻す道（:lock_put_back）へ入る。それが本件の窓そのもの。
        """
        olds = [name for name in os.listdir(self.app_dir)
                if name.startswith(LOCK_NAME + ".") and name.endswith(".old")]
        self.assertEqual(len(olds), 1,
                         "前提が崩れている（掴まれた目印が 1 つでない）: %r"
                         % (olds,))
        now = time.time()
        os.utime(os.path.join(self.app_dir, olds[0]), (now, now))

    def _leftovers(self):
        return sorted(name for name in os.listdir(self.app_dir)
                      if name.startswith(LOCK_NAME))

    def test_a_put_back_between_the_two_looks_does_not_resurrect_the_marker(self):
        """正規名を見た後・.old を掃く前に改名が戻されても、目印を残さないこと。"""
        a_proc, a_path = self._start("A", swap=self.gate_swap,
                                     sweep=self.gate_sweep)
        self._wait_for_the_gate(self.gate_swap, a_proc, a_path, "A")
        # A は自分の目印 L1 を作り、差し替えの直後で止まっている。
        self.assertTrue(os.path.isdir(self.lock),
                        "前提が崩れている（A の目印が無い）")
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A",
                         "前提が崩れている（A がまだ据えていない）")
        self._age_the_marker()
        # A はまだ動いているが、目印は 10分より古く見える。

        b_proc, b_path = self._start("B", grab=self.gate_grab)
        self._wait_for_the_gate(self.gate_grab, b_proc, b_path, "B")
        # B は L1 を .old へ改名し、つかめたかを見直す手前で止まっている。
        self.assertFalse(os.path.exists(self.lock),
                         "前提が崩れている（窓が開いていない）")
        self._freshen_the_grabbed_marker()

        self._release(self.gate_swap)
        self._wait_for_the_gate(self.gate_sweep, a_proc, a_path, "A")
        # A は正規名を見終え、.old を掃く手前で止まっている。

        self._release(self.gate_grab)
        b_code = b_proc.wait(timeout=300)
        b_text = self._output(b_path)
        self.assertNotEqual(b_code, 0, "B が成功として返った:\n" + b_text)
        self.assertTrue(os.path.isdir(self.lock),
                        "前提が崩れている（B が改名を戻していない）:\n" + b_text)
        # B は「まだ新しい」と分かり、L1 を正規名へ戻して中止した。

        self._release(self.gate_sweep)
        a_code = a_proc.wait(timeout=300)
        a_text = self._output(a_path)
        self.assertEqual(a_code, 0, "A が失敗として返った:\n" + a_text)
        self.assertIn("更新が完了しました", a_text, a_text)

        self.assertEqual(self._leftovers(), [],
                         "終わった更新の目印が残った:\nA:\n%s\nB:\n%s"
                         % (a_text, b_text))

        # 誰も持っていないのに弾かれ続けないこと（10 分待たされる件）
        c_proc, c_path = self._start("C")
        c_code = c_proc.wait(timeout=300)
        c_text = self._output(c_path)
        self.assertEqual(c_code, 0, "次の更新が弾かれた:\n" + c_text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_C", c_text)


if __name__ == "__main__":
    unittest.main()
