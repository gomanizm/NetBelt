"""待っている側が、生きている確定の目印を開いて読んでしまう件の回帰。

tests/test_update_finalize_takeover_recheck.py の直しで
_take_over_abandoned_finalize は「掴んでから見直す」形になったが、その際に
中身の読み取りが古さの判定より「前」へ出てしまった。目印が取れるまで
待つ側は 50 ms ごとにこの関数を呼ぶので、持ち主の生きている目印を
50 ms ごとに open(marker, 'rb') で開いて読むことになる。

Windows の open() は FILE_SHARE_DELETE を含まない共有モードで開くため、
その一瞬に持ち主の _drop_finalize_marker が走ると os.replace が
[WinError 32] で失敗する。

実測（検査役 cx7a-verify-release2 の r03_regression_read_handle.py /
p03_multiproc_stress.py、aa38a2b と f4cad23 を差し替えて比較）:

    生きている（古くない）目印への open 回数 : aa38a2b 1 回 / f4cad23 0 回
    後始末の失敗（5 本 × 60 回 を 2 回）     : aa38a2b 4/297・3/298 件
                                               f4cad23 0/300・0/300 件

失敗すると『[VersionManager] 確定の目印を外せませんでした: [WinError 32]
...』が出て目印が置き去りになり、その直後の _cross_process_finalize は
owned=False ＝ 誰も保存していないのに『別の NetBelt が同じ更新を保存中
です。』を出す。製品値は FINALIZE_STALE_SEC=60 秒なので、置き去りの目印が
引き取られるまで最大 60 秒、同じ ZIP を確定しようとした NetBelt は 5 秒
待たされたうえで嘘の理由で失敗する。

直した形: 中身の読み取りを古さの判定の「後」へ戻す。掴んだ後の見直し
（_finalize_marker_is_alive）はそのままなので排他の強さは変わらず、
生きている目印を開くことだけが無くなる。
"""
import builtins
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

from core import version_manager as vm  # noqa: E402


class _DropWhileOpen:
    """読み取りの最中に持ち主の後始末を走らせる、ファイルの包み。

    open() したハンドルが開いている「その瞬間」に os.replace を走らせる
    ための足場。実機で起きる重なりを、待ち時間に頼らず決定的に作る。
    """

    def __init__(self, handle, on_read):
        self._handle = handle
        self._on_read = on_read

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, *exc):
        return self._handle.__exit__(*exc)

    def read(self, *args):
        callback, self._on_read = self._on_read, None
        if callback is not None:
            callback()
        return self._handle.read(*args)


class FinalizeLiveMarkerUntouchedTest(unittest.TestCase):
    """古くない確定の目印に、待っている側が触らないこと。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-finalize-live-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.zip_path = os.path.join(self.dir, "NetBelt-v9.9.9.zip")
        self.marker = self.zip_path + ".finalizing"
        self.token = vm._finalize_token()
        with io.open(self.marker, "wb") as f:
            f.write(self.token)

    def _hooked_open(self, on_marker_read=None):
        """目印を開いた回数を数える open。重なりを作る差し込みつき。"""
        real_open = builtins.open
        state = {"opens": 0, "armed": on_marker_read is not None}

        def hooked(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            if not isinstance(path, (str, os.PathLike)):
                return handle
            if os.fspath(path) != self.marker:
                return handle
            state["opens"] += 1
            if state["armed"]:
                state["armed"] = False
                return _DropWhileOpen(handle, on_marker_read)
            return handle

        return hooked, state

    def test_a_live_marker_is_never_opened_by_the_waiting_side(self):
        """古くない目印は、中身を読むために開きもしないこと。"""
        hooked, state = self._hooked_open()
        with mock.patch.object(builtins, "open", hooked):
            vm._take_over_abandoned_finalize(self.marker)

        self.assertEqual(
            state["opens"], 0,
            "生きている目印を開いた（持ち主の os.replace が WinError 32 で"
            "失敗する）")
        self.assertTrue(os.path.exists(self.marker),
                        "生きている目印を消した")

    def test_the_owner_can_drop_its_marker_while_a_waiter_looks(self):
        """待っている側の引き取りと重なっても、持ち主が目印を外せること。"""
        def drop():
            vm._drop_finalize_marker(self.marker, self.token)

        hooked, state = self._hooked_open(on_marker_read=drop)
        with mock.patch.object(builtins, "open", hooked):
            vm._take_over_abandoned_finalize(self.marker)
        if state["armed"]:
            # 待っている側が開かなかった＝重なりようが無い。
            # 持ち主はいつもどおり外す。
            drop()

        self.assertFalse(
            os.path.exists(self.marker),
            "持ち主が目印を外せず、置き去りになった（この後 %.0f 秒は "
            "誰も保存していないのに『別の NetBelt が同じ更新を保存中です』に"
            "なる）" % vm.FINALIZE_STALE_SEC)
        with vm._cross_process_finalize(self.zip_path) as owned:
            self.assertTrue(owned, "置き去りの目印のせいで確定へ入れない")


if __name__ == "__main__":
    unittest.main()
