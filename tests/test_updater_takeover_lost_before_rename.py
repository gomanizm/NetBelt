"""取り直しを失った更新が、それに気づかず回収を続けて生きた目印を掴ませていた件の回帰。

古い目印（NetBelt-update-lock）の回収は、取り直し用の目印
（NetBelt-update-takeover）を排他で確保してから行う（利用者の決定
2026-09-23 / release-02）。その取り直し用の目印も 10 分より古ければ
他の更新に回収される。ところが回収する側は、:claim_takeover を通った
後は「古さの見直し → ren → rd → md」と進むだけで、取り直し用の目印が
まだ自分のものかを見直していなかった（見直していたのは最後の
:release_takeover だけ）。

実測（v1.3.1 = 441ea02 の updater.bat、関門 4 か所、決定的。検査役
cx7d-release-inspector2 の v3 と v1.3.2 の再調査 b1_v3_lost_takeover_reclaims.py）:

  1) 置き土産の目印（40 分前）がある。A が取り直し用の目印を確保した
     直後で止まる（休止・スリープ相当）。その目印を 40 分前に見せる
  2) B が A の取り直し用の目印を回収して正当な持ち主になり、正規名の
     目印を「古い」と見た直後・ren の手前で止まる
  3) A を再開すると、A は取り直しを失ったことに気づかずに回収を終え、
     自分の目印（holder=A）を作って差し替えの直後まで進んだ
  4) B の ren が A の生きている目印を .old へ掴んだ
  5) D が空いた正規名で md を通し、差し替えの直後まで進んだ
  6) B は戻せず（名前が D で塞がっている）、rd で A の目印を消して rc=1
  7) A rc=0『更新が完了しました』、D rc=0『更新が完了しました』、
     据わった exe は EXE_FROM_D

2 本が同時にインストール先を書き、A は自分が承認した版ではない exe が
据わっても完了と表示した。

直した形: 正規名の目印を ren で掴む直前に、取り直し用の目印の holder.txt が
まだ自分の識別子（STAMP）かを見直す（:takeover_is_mine）。違えば回収を
やめて「別の更新が進行中です」で中止する（取り直し用の目印は、持ち主の
ものなので消さない）。識別子を書けなかったときは確かめようがないので、
これまでどおり進む（:release_takeover と同じ扱い）。

残る制限（直していない）: 見直しと ren の間でもう一度 10 分以上止まる
二重の停止と、インストール先の目印を持ったまま 10 分以上止まった更新が
あると 2 本が書く件（設計上の代償）は残る。
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

# :claim_takeover が通って戻った直後（取り直し用の目印を保持中）。
HELD_ANCHOR = b'set "PS_LOCK=!LOCK_DIR!"'
# 正規名の目印を .old へ掴む ren の手前（古いと見た後）。
REN_ANCHOR = b'ren "!LOCK_DIR!" "!LOCK_OLD_NAME!" 2>nul'
# 掴んだものが新しかったので戻す入口。
PUTBACK_ANCHOR = b":lock_put_back"
# exe を差し替えた直後。
SWAP_ANCHOR = b'if not exist "!APP_DIR!NetBelt.exe" ('


def _gate(var, prefix):
    return [
        b'if not defined ' + var + b' goto :' + prefix + b'_skip',
        b'echo reached>"!' + var + b'!.reached"',
        b':' + prefix + b'_wait',
        b'if not exist "!' + var + b'!.go" ( ping -n 2 127.0.0.1 >nul '
        b'& goto :' + prefix + b'_wait )',
        b':' + prefix + b'_skip',
    ]


def _index(lines, anchor, expect=1, nth=0):
    hits = [i for i, line in enumerate(lines) if line == anchor]
    assert len(hits) == expect, "関門の位置がずれている: %r %r" % (anchor, hits)
    return hits[nth]


def _gated_updater():
    """関門を 4 か所入れた updater.bat を返す。"""
    lines = io.open(UPDATER, "rb").read().split(b"\r\n")

    i = _index(lines, HELD_ANCHOR, expect=2, nth=1)
    assert lines[i - 2] == b"call :claim_takeover", lines[i - 2]
    lines[i:i] = _gate(b"NB_GATE_HELD", b"nb_held")

    i = _index(lines, REN_ANCHOR)
    lines[i:i] = _gate(b"NB_GATE_REN", b"nb_ren")

    i = _index(lines, PUTBACK_ANCHOR)
    lines[i + 1:i + 1] = _gate(b"NB_GATE_PUTBACK", b"nb_pb")

    i = _index(lines, SWAP_ANCHOR)
    lines[i:i] = _gate(b"NB_GATE_SWAP", b"nb_swap")
    return b"\r\n".join(lines)


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterTakeoverLostBeforeRenameTest(unittest.TestCase):

    GATES = ("NB_GATE_HELD", "NB_GATE_REN", "NB_GATE_PUTBACK",
             "NB_GATE_SWAP")

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_tklost_")
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
        for name in self.GATES:
            env.pop(name, None)
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

    def _reached(self, gate, tag):
        """関門に着けば True、その前に終われば False。"""
        proc = self.procs[tag][0]
        deadline = time.time() + 300
        while time.time() < deadline:
            if os.path.exists(gate + ".reached"):
                return True
            if proc.poll() is not None:
                return os.path.exists(gate + ".reached")
            time.sleep(0.02)
        self.fail("%s が関門にも終わりにも着かない:\n%s"
                  % (tag, self._output(tag)))

    def _must_reach(self, gate, tag):
        if not self._reached(gate, tag):
            self.fail("前提が崩れている（%s が関門の前に終わった）:\n%s"
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

    def _installed(self):
        with io.open(os.path.join(self.app_dir, "NetBelt.exe"), "rb") as f:
            return f.read()

    def _markers(self):
        return sorted(n for n in os.listdir(self.app_dir)
                      if n.startswith("NetBelt-update-"))

    # ------------------------------------------------------------ 本体
    def test_a_run_that_lost_the_takeover_does_not_reclaim(self):
        """取り直しを失った側が回収を続けず、完走するのは 1 本だけであること。"""
        os.makedirs(self.lock)
        self._age(self.lock)
        held_a = self._gate_path("HeldA")
        swap_a = self._gate_path("SwapA")
        ren_b = self._gate_path("RenB")
        putback_b = self._gate_path("PutBackB")
        swap_d = self._gate_path("SwapD")

        # 1) A が取り直し用の目印を持ったまま止まる -> 40 分前に見せる
        self._start("A", NB_GATE_HELD=held_a, NB_GATE_SWAP=swap_a)
        self._must_reach(held_a, "A")
        self.assertTrue(os.path.isdir(self.takeover),
                        "前提が崩れている（A が取り直し用の目印を持っていない）")
        self._age(self.takeover)

        # 2) B が A の取り直し用の目印を回収し、正規名を古いと見て ren の手前
        self._start("B", NB_GATE_REN=ren_b, NB_GATE_PUTBACK=putback_b)
        self._must_reach(ren_b, "B")
        self.assertTrue(os.path.isdir(self.lock),
                        "前提が崩れている（正規名の目印が無い）")

        # 3) A を再開する
        self._release(held_a)
        if self._reached(swap_a, "A"):
            # A が取り直しを失ったのに回収を終え、差し替えまで進んだ。
            # 実測の並びを最後まで進め、何が起きるかを出力に残す。
            self._release(ren_b)
            if self._reached(putback_b, "B"):
                self._start("D", NB_GATE_SWAP=swap_d)
                self._reached(swap_d, "D")
            self._release(putback_b)
            self._finish("B")
            self._release(swap_a)
            self._release(swap_d)
        else:
            a_code, a_text = self._finish("A")
            self.assertNotEqual(a_code, 0, "A が成功として返った:\n" + a_text)
            self.assertIn("別の更新が進行中です", a_text, a_text)
            self.assertTrue(
                os.path.isdir(self.takeover),
                "A が B の持っている取り直し用の目印を消した:\n" + a_text)
            self._release(ren_b)
            self._release(putback_b)

        results = {tag: self._finish(tag) for tag in sorted(self.procs)}
        report = "\n".join("--- %s rc=%s\n%s" % (tag, code, text)
                           for tag, (code, text) in results.items())
        done = [tag for tag, (_, text) in results.items() if DONE in text]
        self.assertEqual(
            len(done), 1,
            "「更新が完了しました」を出した更新が 1 本ではない（%s）。"
            "据わった exe = %r\n%s" % (done, self._installed(), report))
        self.assertEqual(
            self._installed(), ("EXE_FROM_%s" % done[0]).encode("ascii"),
            "完了を出した側とは別の exe が据わった:\n" + report)
        self.assertEqual(self._markers(), [],
                         "目印（改名した分を含む）が残った:\n" + report)


if __name__ == "__main__":
    unittest.main()
