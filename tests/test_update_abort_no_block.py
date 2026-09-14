"""中止の要求が、受信の後片付けを待って GUI を止めないことを確認する。

実測（release #5）: VersionManager.abort() は GUI スレッドから呼ばれる。
docstring には「読み取りのタイムアウト（60秒）まで戻ってこない」ので
「ソケット側から打ち切る」と書いてあるが、実装は requests の
Response.close() を呼び出し元のスレッドでそのまま待っていた。
close() は下の読み取りが片付くまで戻らないため、停滞した接続を
中止すると最大60秒ぶん GUI が固まる（＝意図どおりに機能していない）。

既存の tests/test_update_dialog_cancel.py の偽応答は close() が即座に
戻るため、この待ちを検出できない。ここでは close() が戻らない応答を使う。
"""
import sys
import threading
import time
import unittest

sys.path.insert(0, "src")

# 「待たずに戻ること」の境目。止まった close() より十分短く取る
PROMPT = 1.0
STUCK_SECONDS = 30


class _StuckClosingResponse:
    """close() が読み取りの完了を待って戻らない応答。"""

    def __init__(self):
        self.close_started = threading.Event()
        self.release = threading.Event()

    def close(self):
        self.close_started.set()
        self.release.wait(STUCK_SECONDS)


class AbortDoesNotBlockTest(unittest.TestCase):
    def _manager(self):
        from core.version_manager import VersionManager
        return VersionManager()

    def test_abort_returns_without_waiting_for_close(self):
        """abort() が close() の完了を待たずに戻ること。"""
        mgr = self._manager()
        response = _StuckClosingResponse()
        self.addCleanup(response.release.set)
        mgr._response = response

        start = time.monotonic()
        mgr.abort()
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, PROMPT,
                        "中止に %.1f 秒かかった（その間 GUI が固まる）" % elapsed)
        self.assertTrue(response.close_started.wait(5),
                        "応答を閉じにいっていない")

    def test_abort_still_closes_the_response(self):
        """待たないだけで、閉じること自体はやめないこと。"""
        mgr = self._manager()
        closed = threading.Event()

        class _Response:
            def close(self):
                closed.set()

        mgr._response = _Response()
        mgr.abort()

        self.assertTrue(closed.wait(5), "応答を閉じていない")

    def test_abort_without_a_response_is_a_no_op(self):
        """受信していないときに呼ばれても、何も起こさないこと。"""
        mgr = self._manager()
        mgr._response = None
        mgr.abort()

    def test_close_failure_is_swallowed(self):
        """閉じるのに失敗しても、呼び出し元へ例外を出さないこと。"""
        mgr = self._manager()
        failed = threading.Event()

        class _Response:
            def close(self):
                failed.set()
                raise IOError("すでに閉じています")

        mgr._response = _Response()
        mgr.abort()

        self.assertTrue(failed.wait(5), "close() を呼んでいない")
        # 別スレッドで潰した例外が、後続の呼び出しを壊していないこと
        time.sleep(0.1)
        mgr._response = None
        mgr.abort()


if __name__ == "__main__":
    unittest.main()
