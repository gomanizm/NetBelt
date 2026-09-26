"""書き込みに失敗した LogWriter が、例外を処理して参照を捨てれば回収されることを検証する（Codex 11 回目 term-02）。

何が起きていたか（c2bb66a で実測。scratchpad\\cx132c-termlog\\repro_term02.py・
repro_term02_widget.py）: LogWriter はスレッドで起きた失敗を _Core.error に残し、
以後の write / flush / close でその例外そのものを raise し直していた。raise する
たびに、その例外の traceback へ呼んだ側のフレームが継ぎ足される（self の
LogWriter、呼び出し元のフレームとその変数）。_Core は weakref.finalize の登録
（core.put）と書き込みスレッドから辿れるので、「finalize の登録 → core → error →
traceback → フレーム → LogWriter」が切れない。write が OSError を出すファイルで
flush / close の例外を処理し、参照を捨てて gc.collect しても、LogWriter も呼び出し
元のフレームの変数も回収されなかった（traceback のフレームは caller, close, caller,
flush, _check, run, write と、呼ぶたびに伸びた）。閉じないまま捨てた場合は、
finalize も動かずファイルが開いたまま残った。端末に組み込んだ形では、残った
フレームが _close_log_file と _write_logs で、どちらも self に TerminalWidget を
持っていた（失敗した記録の受信片や画面モデルまで残る）。

どう直したか: 失敗は failure にそのまま残し、write / flush / close から投げるのは
毎回その写し（同じ型・同じ文言。元の失敗を __cause__ に付ける）にした。呼んだ側の
フレームは写しの traceback にだけ付き、_Core からは辿れない。

unittest の assertRaises は抜けるときに traceback のフレームを片付けるので、
このテストでは端末と同じく try / except で受ける（assertRaises だと起きない）。
"""
import gc
import os
import sys
import tempfile
import threading
import time
import unittest
import weakref

sys.path.insert(0, "src")


class _FailingFile:
    """write が OSError で失敗するファイル（ディスク満杯・共有フォルダの切断の代わり）。"""

    def __init__(self, f, error):
        self._f = f
        self.name = f.name
        self._error = error

    def write(self, text):
        raise self._error

    def flush(self):
        return self._f.flush()

    def close(self):
        return self._f.close()

    @property
    def closed(self):
        return self._f.closed


class _ScreenModel:
    """呼び出し元のフレームが持っている物（端末の画面モデル・受信の片の代わり）。"""


class LogWriterFailureReleasedTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(
            tempfile.mkdtemp(prefix="netbelt-logwriter-released-"), "a.log")

    def _failed_writer(self, error=None):
        """1 行目の書き込みで失敗した LogWriter と、その中のファイルと、書き込みスレッドを返す。"""
        from core.log_writer import LogWriter
        f = open(self.path, "w", encoding="utf-8", buffering=1)
        self.addCleanup(f.close)
        before = set(threading.enumerate())
        w = LogWriter(_FailingFile(
            f, error or OSError(28, "No space left on device")))
        started = [t for t in threading.enumerate()
                   if t not in before and t.name == "log-writer"]
        self.assertEqual(len(started), 1, "前提: 書き込みスレッドが 1 本動いた")
        w.write("one\n")
        self.assertTrue(w.wait(3.0), "前提: 書き込みスレッドが手を空けた")
        self.assertIsInstance(w.failure, OSError, "前提: 書き込みが失敗した")
        return w, f, started[0]

    @staticmethod
    def _handle_errors(w, calls):
        """端末と同じく try / except で例外を受ける。呼び出し元の変数への弱参照を返す。"""
        screen = _ScreenModel()
        for name in calls:
            try:
                getattr(w, name)()
            except OSError:
                pass
        return weakref.ref(screen)

    @staticmethod
    def _wait_until(predicate, seconds=3.0):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            gc.collect()
            time.sleep(0.01)
        return predicate()

    def test_a_failed_writer_is_collected_after_its_errors_are_handled(self):
        """flush / close の例外を処理して参照を捨てたら、LogWriter も呼び出し元の変数も回収されること。"""
        w, f, _ = self._failed_writer()
        screen = self._handle_errors(w, ("flush", "close"))
        self.assertTrue(w.wait(3.0), "前提: 閉じ終えた")
        self.assertTrue(f.closed, "前提: 失敗しても閉じた")
        writer = weakref.ref(w)
        del w
        gc.collect()
        self.assertIsNone(writer(),
                          "失敗した LogWriter が、参照を捨てても回収されない"
                          "（finalize の登録 → core → error → traceback から辿れる）")
        self.assertIsNone(screen(),
                          "例外を受けた呼び出し元の変数が、呼び出しを終えても残った")

    def test_a_failed_writer_dropped_without_close_closes_its_file(self):
        """失敗のあと閉じないまま捨てても回収され、ファイルが閉じられること。"""
        w, f, thread = self._failed_writer()
        screen = self._handle_errors(w, ("flush",))
        self.assertFalse(f.closed, "前提: まだ閉じていない")
        writer = weakref.ref(w)
        del w
        self.assertTrue(self._wait_until(lambda: writer() is None and f.closed),
                        "失敗のあと閉じないまま捨てた LogWriter が回収されず、"
                        "ファイルが開いたまま残った")
        self.assertIsNone(screen(),
                          "例外を受けた呼び出し元の変数が、呼び出しを終えても残った")
        thread.join(3.0)
        self.assertFalse(thread.is_alive(),
                         "捨てた LogWriter の書き込みスレッドが終わらない")

    def test_the_raised_error_says_the_same_as_the_failure(self):
        """write / flush / close が投げる例外は、残した失敗と同じ型・同じ文言であること。

        警告には str(例外) をそのまま出すので、写しても文言を変えない
        （OSError の写しは winerror を落とし、[WinError 64] が [Errno 22] になる）。
        """
        cases = [
            OSError(28, "No space left on device"),
            OSError(2, "No such file or directory", self.path),
            OSError(None, "The specified network name is no longer available",
                    self.path, 64),
        ]
        for error in cases:
            with self.subTest(error=str(error)):
                w, _, _ = self._failed_writer(error)
                for name in ("write", "flush", "close"):
                    raised = None
                    try:
                        if name == "write":
                            w.write("two\n")
                        else:
                            getattr(w, name)()
                    except Exception as e:
                        raised = e
                    self.assertIsNotNone(raised, "%s が失敗を返さなかった" % name)
                    self.assertIs(type(raised), type(w.failure))
                    self.assertEqual(str(raised), str(w.failure))
                    self.assertEqual(raised.errno, w.failure.errno)
                    self.assertEqual(raised.filename, w.failure.filename)
                    del raised
                self.assertTrue(w.wait(3.0))
                del w


if __name__ == "__main__":
    unittest.main()
