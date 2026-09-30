"""記録ファイルの書き込みスレッド（core/log_writer.py の LogWriter）の振る舞いを検証する（termui-03）。

何が起きていたか（441ea02 で実測。scratchpad\\cx132-termlog\\repro\\
repro_termui03_io_stall.py）: 端末のログ記録は write / flush / close を GUI
スレッドで呼んでいたので、記録先が応答しない間は GUI 全体が止まった（書き込みが
1 秒ずつ止まる代役で、受信 1 片ごとに 2.004 秒、停止で 1.002 秒）。

どう直したか: 記録ごとに書き込みスレッドを置く LogWriter を足した。ここでは
その部品の約束を確かめる（端末に組み込んだあとの振る舞いは
test_log_recording_io_stall*.py）。
  - 普段は flush / close が書き終わってから戻る（これまでの「呼んだら書けている」）
  - ファイルの呼び出しが戻らなければ短い待ちで諦め、以後は積むだけにする。
    積んだ分は順序どおりに後から書かれる
  - スレッドで起きた失敗は failure に残り、以後の呼び出しで例外になる。閉じるのは必ず行う
  - 閉じないまま捨てられたら、ファイルを閉じる
"""
import gc
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, "src")


class _StallingFile:
    """放されるまで write が戻らないファイル（応答しない共有フォルダの代わり）。"""

    def __init__(self, f, release, fail=None):
        self._f = f
        self.name = f.name
        self._release = release
        self._fail = fail

    def write(self, text):
        self._release.wait(5.0)
        if self._fail is not None:
            raise self._fail
        return self._f.write(text)

    def flush(self):
        return self._f.flush()

    def close(self):
        return self._f.close()

    @property
    def closed(self):
        return self._f.closed


class LogWriterTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="netbelt-logwriter-"),
                                 "a.log")

    def _open(self):
        f = open(self.path, "w", encoding="utf-8", buffering=1)
        self.addCleanup(f.close)
        return f

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def _stalling(self, fail=None):
        from core.log_writer import LogWriter
        release = threading.Event()
        self.addCleanup(release.set)
        stalls = []
        w = LogWriter(_StallingFile(self._open(), release, fail),
                      on_stall=lambda: stalls.append(1))
        return w, release, stalls

    def test_flush_returns_after_the_text_is_in_the_file(self):
        """普段は、flush が戻った時点でファイルに書けていること。"""
        from core.log_writer import LogWriter
        w = LogWriter(self._open())
        w.write("show version\n")
        w.flush()
        self.assertEqual(self._read(), "show version\n")
        self.assertEqual(w.written_bytes, len("show version" + os.linesep))
        self.assertFalse(w.busy)
        w.close()
        self.assertTrue(w.closed)
        self.assertEqual(w.name, self.path)

    def test_a_stalled_write_is_given_up_quickly_and_caught_up_in_order(self):
        """戻らない書き込みは短い待ちで諦め、後から順序どおりに書かれること。"""
        w, release, stalls = self._stalling()
        started = time.perf_counter()
        w.write("one\n")
        w.flush()
        first = time.perf_counter() - started
        self.assertLess(first, 0.5, "詰まった書き込みを %.2f 秒待った" % first)
        self.assertEqual(stalls, [1], "詰まったことを知らせていない")
        self.assertTrue(w.busy)

        started = time.perf_counter()
        for text in ("two\n", "three\n"):
            w.write(text)
            w.flush()
        w.close()
        again = time.perf_counter() - started
        self.assertLess(again, 0.05, "詰まっている間も書き終わりを待った: %.2f 秒" % again)
        self.assertEqual(w.backlog, len("one\ntwo\nthree\n"))
        self.assertEqual(w.written_bytes, 0, "まだ書けていない分を数えた")

        release.set()
        self.assertTrue(w.wait(3.0), "詰まりが解けても書き終わらない")
        self.assertEqual(self._read(), "one\ntwo\nthree\n")
        self.assertTrue(w.closed)
        self.assertEqual(w.backlog, 0)

    def test_slow_calls_that_each_return_in_time_are_not_a_stall(self):
        """1 回ずつは STALL_WAIT 内に戻る遅い記録先では、諦めずに書き終わりまで待つこと。

        合計で STALL_WAIT を超えても、戻らない呼び出しが無ければ詰まりではない。
        待ちの始めから数えると、CPU が混んで呼び出しの途中で待たされただけでも
        書き終える前に戻ってしまう（呼んだら書けている、が崩れる）。
        """
        from core.log_writer import LogWriter

        class _Slow:
            def __init__(self, f):
                self._f = f
                self.name = f.name

            def write(self, text):
                time.sleep(0.045)
                return self._f.write(text)

            def flush(self):
                time.sleep(0.045)
                return self._f.flush()

            def close(self):
                return self._f.close()

        stalls = []
        w = LogWriter(_Slow(self._open()), on_stall=lambda: stalls.append(1))
        w.write("a\n")
        w.write("b\n")
        w.flush()                       # 合計 0.135 秒、1 回ずつは 0.045 秒
        self.assertEqual(stalls, [], "戻ってくる呼び出しを詰まりとみなした")
        self.assertFalse(w.busy, "書き終える前に戻った")
        self.assertEqual(w.written_bytes, 2 * len("a" + os.linesep))
        w.close()

    def test_a_failure_in_the_thread_is_kept_and_raised(self):
        """スレッドで起きた失敗が残り、以後の呼び出しが例外になり、ファイルは閉じること。"""
        w, release, stalls = self._stalling(fail=OSError(28, "No space left on device"))
        w.write("one\n")
        w.flush()                                   # 詰まる
        release.set()
        self.assertTrue(w.wait(3.0))
        self.assertIsInstance(w.failure, OSError)
        with self.assertRaises(OSError):
            w.write("two\n")
        with self.assertRaises(OSError):
            w.close()
        self.assertTrue(w.wait(3.0))
        self.assertTrue(w.closed, "失敗しても閉じていない")

    def test_close_twice_is_harmless_and_writes_after_close_fail(self):
        """二度閉じても平気で、閉じたあとの書き込みはファイルと同じく失敗すること。"""
        from core.log_writer import LogWriter
        w = LogWriter(self._open())
        w.close()
        started = time.perf_counter()
        w.close()
        self.assertLess(time.perf_counter() - started, 0.05)
        self.assertFalse(w.busy)
        with self.assertRaises(ValueError):
            w.write("late\n")

    def test_a_dropped_writer_closes_its_file(self):
        """閉じないまま捨てられたら、ファイルを閉じること（ファイルオブジェクトと同じ）。"""
        from core.log_writer import LogWriter
        f = self._open()
        w = LogWriter(f)
        w.write("kept\n")
        w.flush()
        del w
        gc.collect()
        end = time.perf_counter() + 3.0
        while time.perf_counter() < end and not f.closed:
            time.sleep(0.01)
        self.assertTrue(f.closed, "捨てた記録のファイルが開いたまま残った")
        self.assertEqual(self._read(), "kept\n")


if __name__ == "__main__":
    unittest.main()
