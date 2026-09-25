"""取り直し用の目印を外すとき、他の更新が持っている分まで消していた件の回帰。

古い目印の回収は、取り直し専用の目印（NetBelt-update-takeover）を排他で
確保してから行う（利用者の決定 2026-09-23 / release-02）。その目印も
10 分より古ければ他の更新から回収されるので、休止・スリープや遅い共有
フォルダで持ち主が止まっている間に、同じ名前が別の更新のものへ入れ替わる
ことがある。インストール先の目印（:release_lock）は、この入れ替わりに
備えて holder.txt で自分のぶんか確かめてから外すのに、:release_takeover
は TAKEOVER_HELD が立っていれば名前だけで rd /s /q していた。

実測（検査役 cx7c-release の t2_release_takeover_steals.py、097550c、
関門 2 か所 = :claim_takeover から戻った直後 / 正規名の目印を .old へ
改名した直後、決定的）:

  1) A が取り直し用の目印を確保した直後で停止
     -> その目印を 40 分前に見せる（休止・スリープ相当）
  2) B が A の目印を回収して自分のぶんを確保し、正規名の目印を .old へ
     掴んだところで停止（B が取り直し用の目印を持っている -> True）
  3) A 再開 -> A は :lock_busy へ落ちるだけのはずが、道中の
     :release_takeover が B の目印を消した
     （B の取り直し用の目印はまだある -> False）

B が取り直しの最中なのに名前が空くので、3 本目が :claim_takeover を
通れる＝release-02 で閉じた「2 本が同時に取り直しへ入る」状態へ戻る。

直した形: インストール先の目印と同じく、確保した直後に holder.txt へ
自分の識別子（STAMP）を書き、外すときは中身が自分の識別子のときだけ
rd する。書けなかったときだけ、以前と同じ無条件の rd にする。
holder.txt が入っても :lock_is_foreign は「更新の目印」と見るので、
置き土産の回収（10 分超なら ren でつかんで消す）は変わらない。
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

# A 用。:claim_takeover が成功して戻った直後（取り直し用の目印を保持中）。
HELD_ANCHOR = b'set "PS_LOCK=!LOCK_DIR!"'
# B 用。正規名の目印を .old へ掴んだ直後（取り直し用の目印を保持中）。
GRAB_ANCHOR = b'set "PS_LOCK=!LOCK_OLD!"'


def _gate(var, prefix):
    return [
        b'if not defined ' + var + b' goto :' + prefix + b'_skip',
        b'echo reached>"!' + var + b'!.reached"',
        b':' + prefix + b'_wait',
        b'if not exist "!' + var + b'!.go" ( ping -n 2 127.0.0.1 >nul '
        b'& goto :' + prefix + b'_wait )',
        b':' + prefix + b'_skip',
    ]


def _gated_updater():
    """関門を 2 か所（A 用・B 用）入れた updater.bat を返す。"""
    lines = io.open(UPDATER, "rb").read().split(b"\r\n")

    hits = [i for i, line in enumerate(lines) if line == HELD_ANCHOR]
    assert len(hits) == 2, "保持中の関門の位置がずれている: %r" % (hits,)
    i = hits[1]
    assert lines[i - 2] == b"call :claim_takeover", lines[i - 2]
    lines = lines[:i] + _gate(b"NB_GATE_HELD", b"nb_held") + lines[i:]

    hits = [i for i, line in enumerate(lines) if line == GRAB_ANCHOR]
    assert len(hits) == 1, "掴んだ直後の関門の位置がずれている: %r" % (hits,)
    i = hits[0]
    assert lines[i - 1] == b'if not exist "!LOCK_OLD!" goto :lock_busy', \
        lines[i - 1]
    lines = lines[:i] + _gate(b"NB_GATE_GRAB", b"nb_grab") + lines[i:]
    return b"\r\n".join(lines)


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterTakeoverReleaseOwnerTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_tkrel_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        with io.open(os.path.join(self.app_dir, "updater.bat"), "wb") as f:
            f.write(_gated_updater())
        self._write("NetBelt.exe", b"OLD-EXE")
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.gate_held = os.path.join(self.base, "gateHeld")
        self.gate_grab = os.path.join(self.base, "gateGrab")
        self.lock = os.path.join(self.app_dir, LOCK_NAME)
        self.takeover = os.path.join(self.app_dir, TAKEOVER_NAME)

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

    def _start(self, tag, held=None, grab=None):
        out_path = os.path.join(self.base, "%s.txt" % tag)
        out = io.open(out_path, "wb")
        self.addCleanup(out.close)
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        for name in ("NB_GATE_HELD", "NB_GATE_GRAB"):
            env.pop(name, None)
        if held:
            env["NB_GATE_HELD"] = held
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

    def _age(self, path, minutes=40):
        old = time.time() - minutes * 60
        os.utime(path, (old, old))

    def _markers(self):
        return sorted(name for name in os.listdir(self.app_dir)
                      if name.startswith("NetBelt-update-"))

    def _run_alone(self, tag="A"):
        proc, out_path = self._start(tag)
        proc.wait(timeout=300)
        return proc.returncode, self._output(out_path)

    def test_a_stalled_holder_does_not_release_the_takeover_it_lost(self):
        """止まっている間に回収された側が、新しい持ち主の目印を消さないこと。"""
        os.makedirs(self.lock)
        self._age(self.lock)

        a_proc, a_path = self._start("A", held=self.gate_held)
        b_proc = None
        try:
            self._wait_for_the_gate(self.gate_held, a_proc, a_path, "A")
            self.assertTrue(os.path.isdir(self.takeover),
                            "前提が崩れている（A が取り直し用の目印を持って"
                            "いない）")
            # A は取り直しの最中に 10 分以上止まった（休止・スリープ相当）。
            self._age(self.takeover)

            b_proc, b_path = self._start("B", grab=self.gate_grab)
            self._wait_for_the_gate(self.gate_grab, b_proc, b_path, "B")
            # B は A の目印を回収して自分のぶんを確保し、正規名の目印を
            # .old へ掴んだところで止まっている。
            self.assertTrue(os.path.isdir(self.takeover),
                            "前提が崩れている（B が取り直し用の目印を持って"
                            "いない）:\n" + self._output(b_path))

            self._release(self.gate_held)
            a_proc.wait(timeout=300)
            a_text = self._output(a_path)
            self.assertNotEqual(a_proc.returncode, 0,
                                "A が成功として返った:\n" + a_text)
            self.assertIn("別の更新が進行中です", a_text, a_text)
            self.assertTrue(
                os.path.isdir(self.takeover),
                "A が B の持っている取り直し用の目印を消した（B が取り直しの"
                "最中なのに、他の更新が :claim_takeover を通れる）:\n"
                + a_text)
        finally:
            self._release(self.gate_held)
            self._release(self.gate_grab)
            a_proc.wait(timeout=300)
            if b_proc is not None:
                b_proc.wait(timeout=300)

        b_text = self._output(b_path)
        self.assertEqual(b_proc.returncode, 0,
                         "B が失敗として返った:\n" + b_text)
        self.assertIn("更新が完了しました", b_text, b_text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_B", b_text)
        self.assertEqual(self._markers(), [],
                         "B が自分の目印を外さなかった:\nA:\n%s\nB:\n%s"
                         % (a_text, b_text))

    def test_a_stale_takeover_marker_with_a_holder_is_reclaimed(self):
        """持ち主の書かれた取り直し用の置き土産も、古ければ回収すること。"""
        os.makedirs(self.lock)
        self._age(self.lock)
        os.makedirs(self.takeover)
        with io.open(os.path.join(self.takeover, "holder.txt"), "wb") as f:
            f.write(b"NetBeltUpdate_1_4242\r\n")
        self._age(os.path.join(self.takeover, "holder.txt"))
        self._age(self.takeover)

        code, text = self._run_alone()

        self.assertEqual(code, 0, "古い置き土産で更新が止まった:\n" + text)
        self.assertIn("目印を取り除きました", text, text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A", text)
        self.assertEqual(self._markers(), [],
                         "目印（改名した分を含む）が残った:\n" + text)


if __name__ == "__main__":
    unittest.main()
