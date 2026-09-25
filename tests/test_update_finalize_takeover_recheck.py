"""確定の目印の引き取りが、掴んだ後に見直すことの回帰。

_take_over_abandoned_finalize は「古いか見る → os.replace で掴む → 消す」
だけで、掴んだものを見直していなかった。見てから掴むまでの間に別の
NetBelt が同じ目印を引き取って自分の目印を作っていると、os.replace は
その『生きている』目印を掴んで消してしまう。

実測（検査役 cx7a-verify-release の p03_finalize_takeover.py +
p03_driver.py、f4cad23、FINALIZE_STALE_SEC は既定の 60 秒、2/2 で決定的）:

    置き土産の目印 NetBelt-v1.3.1.zip.finalizing（600 秒前、中身 b'9999-1-1'）
    1) B: 213 行の getmtime で『古い』と判定し、219 行の os.replace の直前で停止
    2) A: 同じ古い目印を引き取り、自分の目印を作る -> owned=True で保持中
    3) B を再開 -> B の os.replace が A の生きている目印を掴んで消し、
       続く os.open(O_CREAT|O_EXCL) が通る
    4) A がまだ確定中なのに B も owned=True -> True
       A 側: 'A marker after hold missing: [Errno 2] ...\\.finalizing'
       B 側: 'B owned=True' / marker=b'24852-30072-2267187000000'

自然な競走は狭い（p03b_natural.py、40 回で 0 回）。負けた側の
getmtime -> os.replace の隙間はマイクロ秒で、そこへ勝った側の
replace -> remove -> sleep(0.05) -> os.open（最低 50 ms）が収まる必要がある。
ただし置き土産の目印が残るのは確定の最中に落ちたときで、そのとき危険な
期間は「落ちてから 60 秒後〜cleanup_old_updates が消す 24 時間」。

既存の tests/test_update_finalize_cross_process.py は古い目印の単独回収しか
見ていなかった。

直した形: updater.bat の :claim_lock と同じ「掴んでから見直す」にする。
os.replace に成功したら、掴んだものの時刻と中身を読み直し、
FINALIZE_STALE_SEC より新しい／中身が変わっている＝別の確定が取り直した
ものだったら、消さずに os.rename で名前を戻す。戻せない（その間に誰かが
新しい目印を作った）ときは、名前から外れた以上どの排他にもならないので
掴んだ側で捨てる（updater.bat の :lock_put_back と同じ判断）。
"""
import io
import os
import sys
import tempfile
import shutil
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

from core import version_manager as vm  # noqa: E402

STALE_AGE = 600.0


class FinalizeTakeoverRecheckTest(unittest.TestCase):
    """引き取りの最中に別の確定が目印を取り直した場合。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-finalize-recheck-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.zip_path = os.path.join(self.dir, "NetBelt-v9.9.9.zip")
        self.marker = self.zip_path + ".finalizing"
        self._write_marker(b"9999-1-1", age=STALE_AGE)

    def _write_marker(self, body, age=0.0):
        with io.open(self.marker, "wb") as f:
            f.write(body)
        if age:
            when = time.time() - age
            os.utime(self.marker, (when, when))

    def _marker_body(self):
        try:
            with io.open(self.marker, "rb") as f:
                return f.read()
        except OSError:
            return None

    def _gate_that_lets_another_finalize_in(self, body=b"ALIVE-A"):
        """os.replace の直前で、別の確定に目印を取り直させる細工を返す。

        B が『古い』と見た後、掴む前に A が引き取って自分の目印を作った、
        という並びをそのまま作る。
        """
        real_replace = os.replace
        state = {"fired": False}

        def gated(src, dst):
            if not state["fired"] and src == self.marker:
                state["fired"] = True
                # A の引き取り（古い目印を消す）と、A 自身の目印の作成
                os.remove(self.marker)
                self._write_marker(body)
            return real_replace(src, dst)

        return gated, state

    def test_a_marker_retaken_while_grabbing_is_put_back(self):
        """掴んだものが生きていたら、消さずに戻すこと。"""
        gated, state = self._gate_that_lets_another_finalize_in()
        with mock.patch.object(os, "replace", gated):
            vm._take_over_abandoned_finalize(self.marker)

        self.assertTrue(state["fired"], "細工が働かなかった")
        self.assertEqual(
            self._marker_body(), b"ALIVE-A",
            "掴んだ後に見直さず、生きている確定の目印を消した")

    def test_the_other_finalize_keeps_the_marker_to_itself(self):
        """取り直された目印を掴んでも、確定へ入れてはならないこと。"""
        gated, state = self._gate_that_lets_another_finalize_in()
        with mock.patch.object(os, "replace", gated), \
                mock.patch.object(vm, "FINALIZE_WAIT_SEC", 0.5):
            with vm._cross_process_finalize(self.zip_path) as owned:
                got = owned
                body = self._marker_body()

        self.assertTrue(state["fired"], "細工が働かなかった")
        self.assertFalse(
            got, "別の確定が持っている目印を消して、自分も確定へ入った")
        self.assertEqual(body, b"ALIVE-A",
                         "生きている確定の目印が入れ替わった")

    def test_a_marker_left_by_a_crash_is_still_taken_over(self):
        """取り直しが起きていない置き土産は、これまでどおり引き取ること。"""
        vm._take_over_abandoned_finalize(self.marker)

        self.assertIsNone(self._marker_body(), "古い置き土産が残った")

    def test_a_live_marker_is_left_alone(self):
        """そもそも古くない目印には手を出さないこと。"""
        self._write_marker(b"ALIVE-A")

        vm._take_over_abandoned_finalize(self.marker)

        self.assertEqual(self._marker_body(), b"ALIVE-A")

    def test_nothing_is_left_behind_when_the_put_back_cannot_happen(self):
        """戻せないとき（既に別の目印がある）は、掴んだものを捨てること。"""
        gated, _ = self._gate_that_lets_another_finalize_in()
        real_rename = os.rename

        def rename_into_a_taken_name(src, dst):
            # 戻そうとした先を、その直前に誰かが埋めた状態にする
            if dst == self.marker and not os.path.exists(self.marker):
                with io.open(self.marker, "wb") as f:
                    f.write(b"ALIVE-C")
            return real_rename(src, dst)

        with mock.patch.object(os, "replace", gated), \
                mock.patch.object(os, "rename", rename_into_a_taken_name):
            vm._take_over_abandoned_finalize(self.marker)

        self.assertEqual(self._marker_body(), b"ALIVE-C")
        leftovers = [n for n in os.listdir(self.dir) if n.endswith(".stale")]
        self.assertEqual(leftovers, [], "掴んだ目印が残った")


if __name__ == "__main__":
    unittest.main()
