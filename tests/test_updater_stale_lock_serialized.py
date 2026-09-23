"""古い目印の取り直しが重なると、3 本目が空いた正規名へ滑り込む件の回帰。

tests/test_updater_stale_lock_takeover.py は 2 本の重なりしか見ていない。
その 2 本だけなら、遅れて取り直しに入った側（B）の戻しが成功して元へ戻る。
3 本目（C）が「戻すまでの間」に md を通すと、そうはならない。

実測（検査役 cx7a-verify-release の p02_lock_putback_third.py、f4cad23、
関門 3 か所 = 古さ判定の直後 / :lock_put_back の入口 / move /y の直後、
2/2 で決定的）:

  1) B: 置き土産の目印（40 分前）を『古い』と判定したところで停止
  2) A: それを回収して自分の目印を取得（holder.txt = NetBeltUpdate_1_15016）、
     差し替えの直後で停止
  3) B 再開 -> B の ren が A の『生きている』目印を .old へ移す
     正規名の目印は存在する？ -> False
     .old の持ち主 = NetBeltUpdate_1_15016（＝A）
  4) C: 空いた正規名で md に成功し目印を取得
     => A と C が同時にインストール先を書ける状態: True
  5) B 終了 rc=1『別の更新が進行中です』。戻しは名前が塞がっていて失敗し、
     続く rd が A の目印を消した
  6) A rc=0 完了表示=True / C rc=0 完了表示=True
     据わったもの: NetBelt.exe = EXE_FROM_C / zz_bundled.txt = BUNDLED_FROM_C

A は A の版を承認したのに C の版が据わり、両方が「更新が完了しました」を
出した＝目印を入れた動機そのものが破れている。関門なしで自然に重ねると
起きない（p02b_natural.py、12 回で 0 回）ので、関門で窓を開けて確かめる。

利用者の決定（2026-09-23 / release-02）: 取り直しを直列化する。古い目印の
回収に入る前に、取り直し専用の目印（NetBelt-update-takeover）を排他で確保し、
その中で古さの確認と削除を行う。取り直し用の目印が異常終了で残った場合は、
既存の目印と同じ考え方で、古ければ回収してよい。

直した形: :claim_lock の「古い」判定の後に :claim_takeover を挟み、その中で
古さを見直してから ren / rd する。見直しで『まだ新しい』と分かれば ren を
しないので、生きている目印が正規名から外れる窓そのものが開かない。
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

# A・C 用。move /y の直後、差し替えの確認へ進む手前で待たせる。
# （tests/test_updater_stale_lock_takeover.py と同じ位置）
SWAP_ANCHOR = b'if not exist "!APP_DIR!NetBelt.exe" ('
SWAP_GATE = [
    b'if not defined NB_TEST_GATE goto :nb_test_skip',
    b'echo reached>"!NB_TEST_GATE!.reached"',
    b':nb_test_wait',
    b'if not exist "!NB_TEST_GATE!.go" ( ping -n 2 127.0.0.1 >nul '
    b'& goto :nb_test_wait )',
    b':nb_test_skip',
]

# B 用その2。:lock_put_back の入口。ここへ着いたということは、B が
# 「生きている目印」を .old へ改名し、正規名を空けたということ。
PUTBACK_ANCHOR = b":lock_put_back"
PUTBACK_GATE = [
    b'if not defined NB_GATE_PUTBACK goto :nb_pb_skip',
    b'echo reached>"!NB_GATE_PUTBACK!.reached"',
    b':nb_pb_wait',
    b'if not exist "!NB_GATE_PUTBACK!.go" ( ping -n 2 127.0.0.1 >nul '
    b'& goto :nb_pb_wait )',
    b':nb_pb_skip',
]

# B 用その1。目印を「古い」と判定した直後、手を付ける手前で待たせる。
STALE_MARK = b"$d.LastWriteTime -lt (Get-Date).AddMinutes(-10)) { "
STALE_PAUSE = (
    b"Set-Content -LiteralPath ($env:NB_GATE_STALE + '.reached') -Value 'x'; "
    b"while (-not (Test-Path -LiteralPath ($env:NB_GATE_STALE + '.go'))) { "
    b"Start-Sleep -Milliseconds 20 }; ")


def _gated_updater():
    """関門を 3 か所（A・C 用 1・B 用 2）入れた updater.bat を返す。"""
    lines = io.open(UPDATER, "rb").read().split(b"\r\n")

    idx = lines.index(SWAP_ANCHOR)
    assert lines[idx - 1] == b":swapped", "A 用の関門の位置がずれている"
    lines = lines[:idx] + SWAP_GATE + lines[idx:]

    hits = [i for i, line in enumerate(lines) if line == PUTBACK_ANCHOR]
    assert len(hits) == 1, "戻しの関門の位置がずれている: %r" % (hits,)
    lines = lines[:hits[0] + 1] + PUTBACK_GATE + lines[hits[0] + 1:]

    hits = [i for i, line in enumerate(lines) if STALE_MARK in line]
    assert len(hits) == 1, "古さの関門の位置がずれている: %r" % (hits,)
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
class UpdaterStaleLockSerializedTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_stalesrl_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        with io.open(os.path.join(self.app_dir, "updater.bat"), "wb") as f:
            f.write(_gated_updater())
        self._write("NetBelt.exe", b"OLD-EXE")
        self.temp = os.path.join(self.base, "temp")
        os.makedirs(self.temp)
        self.gate_swap = os.path.join(self.base, "gateSwap")
        self.gate_stale = os.path.join(self.base, "gateStale")
        self.gate_putback = os.path.join(self.base, "gatePutBack")
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

    def _start(self, tag, swap=None, stale=None, putback=None):
        out_path = os.path.join(self.base, "%s.txt" % tag)
        out = io.open(out_path, "wb")
        self.addCleanup(out.close)
        env = dict(os.environ)
        env["TEMP"] = self.temp
        env["TMP"] = self.temp
        for name in ("NB_TEST_GATE", "NB_GATE_STALE", "NB_GATE_PUTBACK"):
            env.pop(name, None)
        if swap:
            env["NB_TEST_GATE"] = swap
        if stale:
            env["NB_GATE_STALE"] = stale
        if putback:
            env["NB_GATE_PUTBACK"] = putback
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

    def _gate_or_exit(self, gate, proc):
        """関門へ着いたら True、着かずに終わったら False。"""
        deadline = time.time() + 300
        while time.time() < deadline:
            if os.path.exists(gate + ".reached"):
                return True
            if proc.poll() is not None:
                return os.path.exists(gate + ".reached")
            time.sleep(0.02)
        return os.path.exists(gate + ".reached")

    def _release(self, gate):
        with io.open(gate + ".go", "wb") as f:
            f.write(b"go")

    def _holder(self):
        path = os.path.join(self.lock, "holder.txt")
        try:
            with io.open(path, "rb") as f:
                return f.read().strip()
        except OSError:
            return None

    def _put_a_stale_marker(self, name):
        """異常終了で残った目印（40 分前）を置く。"""
        path = os.path.join(self.app_dir, name)
        os.makedirs(path)
        old = time.time() - 40 * 60
        os.utime(path, (old, old))
        return path

    def _run_alone(self, tag="A"):
        proc, out_path = self._start(tag)
        proc.wait(timeout=300)
        return proc.returncode, self._output(out_path)

    def test_a_takeover_marker_left_by_a_crash_is_reclaimed_once_it_is_old(
            self):
        """取り直し用の目印が異常終了で残っても、古ければ回収して続けること。"""
        self._put_a_stale_marker(LOCK_NAME)
        takeover = self._put_a_stale_marker(TAKEOVER_NAME)

        code, text = self._run_alone()

        self.assertEqual(code, 0, "古い置き土産で更新が止まった:\n" + text)
        self.assertIn("目印を取り除きました", text, text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A", text)
        self.assertFalse(os.path.exists(takeover),
                         "取り直し用の目印が残った:\n" + text)
        self.assertEqual([name for name in os.listdir(self.app_dir)
                          if name.startswith("NetBelt-update-")], [],
                         "目印（改名した分を含む）が残った:\n" + text)

    def test_a_fresh_takeover_marker_stops_the_take_over(self):
        """他の更新が取り直しの最中なら、待たずに中止すること。"""
        self._put_a_stale_marker(LOCK_NAME)
        takeover = os.path.join(self.app_dir, TAKEOVER_NAME)
        os.makedirs(takeover)

        code, text = self._run_alone()

        self.assertNotEqual(code, 0, "取り直しが重なったまま進んだ:\n" + text)
        self.assertIn("別の更新が進行中です", text, text)
        self.assertEqual(self._read("NetBelt.exe"), b"OLD-EXE", text)
        self.assertTrue(os.path.isdir(takeover),
                        "他の更新の取り直し用の目印を消した:\n" + text)

    def test_a_fresh_takeover_marker_does_not_stop_a_plain_update(self):
        """取り直しの要らない更新は、その目印に妨げられないこと。"""
        takeover = os.path.join(self.app_dir, TAKEOVER_NAME)
        os.makedirs(takeover)

        code, text = self._run_alone()

        self.assertEqual(code, 0, "取り直し用の目印で更新が止まった:\n" + text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A", text)
        self.assertTrue(os.path.isdir(takeover),
                        "他の更新の取り直し用の目印を消した:\n" + text)

    def test_a_users_folder_with_the_takeover_name_is_not_deleted(self):
        """同じ名前で利用者が置いたものは、古くても消さずに中止すること。"""
        self._put_a_stale_marker(LOCK_NAME)
        folder = os.path.join(self.app_dir, TAKEOVER_NAME)
        os.makedirs(os.path.join(folder, "switch-config"))
        memo = os.path.join(folder, "memo.txt")
        with io.open(memo, "wb") as f:
            f.write(b"MEMO")
        old = time.time() - 40 * 60
        for path in (memo, os.path.join(folder, "switch-config"), folder):
            os.utime(path, (old, old))

        code, text = self._run_alone()

        self.assertNotEqual(code, 0, "そのまま更新を続けた:\n" + text)
        self.assertNotIn("取り除きました", text, text)
        self.assertEqual(self._read("NetBelt.exe"), b"OLD-EXE", text)
        self.assertTrue(os.path.isfile(memo),
                        "利用者のファイルを消した:\n" + text)
        with io.open(memo, "rb") as f:
            self.assertEqual(f.read(), b"MEMO", "中身を書き換えた:\n" + text)

    def _late_take_over(self):
        """古い目印 -> A が回収して保持 -> B が遅れて取り直しに入る、を作る。

        戻り値は (A, B, grabbed)。grabbed は B が生きている目印を .old へ
        改名した（＝正規名が空いた）かどうか。
        """
        self._put_a_stale_marker(LOCK_NAME)

        b_proc, b_path = self._start("B", stale=self.gate_stale,
                                     putback=self.gate_putback)
        self._wait_for_the_gate(self.gate_stale, b_proc, b_path, "B")
        # B は置き土産の目印を「古い」と判定したところで止まっている。

        a_proc, a_path = self._start("A", swap=self.gate_swap)
        self._wait_for_the_gate(self.gate_swap, a_proc, a_path, "A")
        # A は置き土産を回収して自分の目印を作り、差し替えの直後で止まっている。
        self.assertTrue(os.path.isdir(self.lock),
                        "前提が崩れている（A の目印が無い）")
        self.a_stamp = self._holder()
        self.assertTrue(self.a_stamp, "前提が崩れている（holder.txt が無い）")
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A",
                         "前提が崩れている（A がまだ据えていない）")

        self._release(self.gate_stale)
        grabbed = self._gate_or_exit(self.gate_putback, b_proc)
        return (a_proc, a_path), (b_proc, b_path), grabbed

    def _finish(self, a, b):
        """関門を全部開けて A と B を終わらせ、(A の出力, B の出力) を返す。"""
        (a_proc, a_path), (b_proc, b_path) = a, b
        self._release(self.gate_putback)
        b_proc.wait(timeout=300)
        self._release(self.gate_swap)
        a_proc.wait(timeout=300)
        return self._output(a_path), self._output(b_path)

    def test_a_late_take_over_does_not_grab_a_live_marker(self):
        """遅れて取り直しに入った側が、生きている目印を掴まないこと。"""
        a, b, grabbed = self._late_take_over()
        try:
            self.assertFalse(
                grabbed,
                "B が A の生きている目印を .old へ改名した（正規名が空き、"
                "3 本目が md を通せる）:\n" + self._output(b[1]))
            self.assertTrue(os.path.isdir(self.lock),
                            "正規名の目印が消えた:\n" + self._output(b[1]))
            self.assertEqual(self._holder(), self.a_stamp,
                             "目印の持ち主が A でなくなった:\n"
                             + self._output(b[1]))
        finally:
            a_text, b_text = self._finish(a, b)

        self.assertNotEqual(b[0].returncode, 0,
                            "B が成功として返った:\n" + b_text)
        self.assertIn("別の更新が進行中です", b_text, b_text)
        self.assertNotIn("更新が完了しました", b_text, b_text)
        self.assertEqual(a[0].returncode, 0, "A が失敗として返った:\n" + a_text)
        self.assertIn("更新が完了しました", a_text, a_text)
        self.assertEqual(self._read("NetBelt.exe"), b"EXE_FROM_A", a_text)

    def test_a_third_update_cannot_take_the_name_a_late_take_over_freed(self):
        """取り直しの隙に、3 本目が目印を取れてしまわないこと。"""
        a, b, _grabbed = self._late_take_over()
        try:
            c_proc, c_path = self._start("C")
            c_proc.wait(timeout=300)
            c_text = self._output(c_path)
        finally:
            a_text, b_text = self._finish(a, b)

        self.assertNotEqual(
            c_proc.returncode, 0,
            "A が目印を持っているのに 3 本目が通った:\n" + c_text)
        self.assertIn("別の更新が進行中です", c_text, c_text)
        self.assertNotIn("更新が完了しました", c_text, c_text)
        self.assertEqual(a[0].returncode, 0, "A が失敗として返った:\n" + a_text)
        self.assertEqual(
            self._read("NetBelt.exe"), b"EXE_FROM_A",
            "A が承認した版とは別の exe が据わった:\nA:\n%s\nB:\n%s\nC:\n%s"
            % (a_text, b_text, c_text))
        self.assertEqual(
            self._read("zz_bundled.txt"), b"BUNDLED_FROM_A",
            "同梱ファイルが混ざった:\nA:\n%s\nB:\n%s\nC:\n%s"
            % (a_text, b_text, c_text))


if __name__ == "__main__":
    unittest.main()
