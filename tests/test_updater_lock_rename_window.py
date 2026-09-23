"""古い目印を「つかむための改名」が開けた窓の回帰。

tests/test_updater_stale_lock_takeover.py の直しで、古い目印の回収は
「見てから消す」から「ren でつかんでから消す」へ変わった。つかむための改名は、
生きている目印を一瞬だけ正規名から消す。その窓の中で、改名した側が元へ戻せない
／戻すのが遅れると、目印の意味が壊れる。

実測（検査役 cx5j-check-release の p11_lock_resurrection.py / p12_grabber_dies.py、
どちらも 2/2 で決定的）:

  (a) 外したはずの目印が生き返る ― 古い目印 L0 を B が「古い」と判定した直後で
      止め、A に L0 を引き継がせて自分の目印 L1 を作らせる。B を進めて L1 を
      .old へ改名させ、「つかめたか」の手前で止めてから A を最後まで走らせると、
      A の :release_lock は改名で消えた NetBelt-update-lock を見に行くので何も
      できない。B は「まだ新しい」と分かって元の名前へ戻す。結果、誰も持って
      いない NetBelt-update-lock が A の holder.txt 入り・更新日時ほぼ現在で
      復活し、続けて走らせた更新は rc=1『別の更新が進行中です』。正常に終わった
      直後から約 10 分間、更新が弾かれ続ける。

  (b) つかんだ側が窓の中で死ぬと後始末が残る ― 同じ並びで、B が L1 を .old へ
      改名した直後にコンソールを閉じられる（taskkill /T /F）と、A は自分の目印を
      名前で探すので .old を片付けられず、インストール先に置き去りになる。

直し方: :release_lock で、自分の目印を名前に依存せず片付ける。正規名に自分の
holder.txt が無いときは、!APP_DIR!NetBelt-update-lock.*.old を for /d で回し、
中の holder.txt が !STAMP! と一致するものだけ rd /s /q する。これで (a) の
「改名を戻されて目印が生き返る」と、(b) の .old の置き去りが消える。

前提の作り方だけ、利用者の決定（2026-09-23 / release-02、取り直しの直列化）に
合わせて移した。「古いと判定した後に、別の更新が取り直した生きている目印を
掴む」筋は無くなったが、窓そのものは残る: 更新が 10分以上かかると、持ち主が
動いていても目印は「古い」と見えるので、取り直しは生きている目印を掴む。
そこで A を差し替えの直後で止め、その目印を古く見せてから B を走らせる。
確かめている中身（(a) の生き返りと (b) の置き去り）は変えていない。
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

# B 用。ren が通った直後、つかめたかを見直す手前で待たせる。
# ここが「生きている目印が正規名から消えている」窓そのもの。
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
    """関門を 2 か所（A 用 1・B 用 1）入れた updater.bat を返す。"""
    lines = io.open(UPDATER, "rb").read().split(b"\r\n")

    idx = lines.index(SWAP_ANCHOR)
    assert lines[idx - 1] == b":swapped", "A 用の関門の位置がずれている"
    lines = lines[:idx] + SWAP_GATE + lines[idx:]

    idx = lines.index(GRAB_ANCHOR)
    assert lines[idx - 1] == b'if not exist "!LOCK_OLD!" goto :lock_busy', \
        "つかんだ直後の関門の位置がずれている"
    lines = lines[:idx] + GRAB_GATE + lines[idx:]

    return b"\r\n".join(lines)


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterLockRenameWindowTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_lockwindow_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        with io.open(os.path.join(self.app_dir, "updater.bat"), "wb") as f:
            f.write(_gated_updater())
        self._write("NetBelt.exe", b"OLD-EXE")
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.gate_swap = os.path.join(self.base, "gateSwap")
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

    def _start(self, tag, swap=None, grab=None):
        out_path = os.path.join(self.base, "%s.txt" % tag)
        out = io.open(out_path, "wb")
        self.addCleanup(out.close)
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        for name in ("NB_TEST_GATE", "NB_GATE_GRAB"):
            env.pop(name, None)
        if swap:
            env["NB_TEST_GATE"] = swap
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

    def _leftovers(self):
        return sorted(name for name in os.listdir(self.app_dir)
                      if name.startswith(LOCK_NAME))

    def _open_the_window(self):
        """B が A の生きた目印を .old へ改名した状態まで進める。"""
        a_proc, a_path = self._start("A", swap=self.gate_swap)
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
        return (a_proc, a_path), (b_proc, b_path)

    def test_the_loser_of_the_window_does_not_resurrect_a_finished_marker(self):
        """窓の中で持ち主が終わったら、改名を戻して目印を生き返らせないこと。"""
        (a_proc, a_path), (b_proc, b_path) = self._open_the_window()

        self._release(self.gate_swap)
        a_code = a_proc.wait(timeout=300)
        a_text = self._output(a_path)
        self.assertEqual(a_code, 0, "A が失敗として返った:\n" + a_text)
        self.assertIn("更新が完了しました", a_text, a_text)

        self._release(self.gate_grab)
        b_code = b_proc.wait(timeout=300)
        b_text = self._output(b_path)
        self.assertNotEqual(b_code, 0, "B が成功として返った:\n" + b_text)
        self.assertNotIn("更新が完了しました", b_text, b_text)

        self.assertEqual(self._leftovers(), [],
                         "終わった更新の目印が残った:\nA:\n%s\nB:\n%s"
                         % (a_text, b_text))

        # 誰も持っていないのに弾かれ続けないこと（10 分待たされる件）
        c_proc, c_path = self._start("C")
        c_code = c_proc.wait(timeout=300)
        c_text = self._output(c_path)
        self.assertEqual(c_code, 0, "次の更新が弾かれた:\n" + c_text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_C", c_text)

    def test_a_grabber_that_dies_in_the_window_leaves_nothing_behind(self):
        """つかんだ側が窓の中で死んでも、改名された目印を残さないこと。"""
        (a_proc, a_path), (b_proc, _b_path) = self._open_the_window()

        subprocess.run(["taskkill", "/T", "/F", "/PID", str(b_proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        b_proc.wait(timeout=60)

        self._release(self.gate_swap)
        a_code = a_proc.wait(timeout=300)
        a_text = self._output(a_path)
        self.assertEqual(a_code, 0, "A が失敗として返った:\n" + a_text)

        self.assertEqual(self._leftovers(), [],
                         "改名された目印が置き去りになった:\n" + a_text)


if __name__ == "__main__":
    unittest.main()
