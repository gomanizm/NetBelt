"""記録の書き込みスレッドが、一度失敗したあとは書かないことを検証する（termlog 見張りの穴 1）。

何が起きていたか（ebbe593 で実測）: LogWriter の書き込みスレッドは、失敗の
あとに積まれていた書き込みを捨てる（src/core/log_writer.py の _Core.run の
`if op == "close" or self.error is None:`）。ところがこの 1 行を守るテストが
無く、`if True:` に変えても test_log_writer.py・test_log_recording_io_stall*.py・
test_log_recording_write_failure.py・test_stopped_log_failure_keeps_log_order.py・
test_log_recording_close_failure.py の 27 件が全部通った。そう変えると、記録先が
詰まっている間に積んだ書き込みのうち 1 つが一時的に失敗して後ろが成功したとき、
記録の途中に穴があいたまま続きが書かれる。利用者への警告は「記録は失敗する前の
行までです」なので、ファイルの中身と合わなくなる。

どうしたか: 本体は正しい。失敗の後ろに積まれていた書き込みがファイルへ届かない
こと（記録は失敗する前の所で終わる）と、閉じるのは行われることを確かめる
テストを足した。上の 1 行を `if True:` に変えると、このテストが落ちる。
"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, "src")


class _FailOnceFile:
    """指定した 1 回の write だけ、放されるまで戻らず、放されたら失敗するファイル。

    ほかの write はそのまま書ける（詰まった共有フォルダが一時的に失敗し、
    すぐ持ち直した場合の代わり）。放されないまま limit 秒たっても失敗して戻る。
    """

    def __init__(self, f, release, fail_on, limit=5.0):
        self._f = f
        self.name = f.name
        self._release = release
        self._fail_on = fail_on
        self._limit = limit
        self.written = []

    def write(self, text):
        if text == self._fail_on:
            self._fail_on = None
            self._release.wait(self._limit)
            raise OSError(64, "The specified network name is no longer available")
        self.written.append(text)
        return self._f.write(text)

    def flush(self):
        return self._f.flush()

    def close(self):
        return self._f.close()

    @property
    def closed(self):
        return self._f.closed


class LogWriterNoWriteAfterFailureTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(
            tempfile.mkdtemp(prefix="netbelt-logwriter-fail-"), "a.log")

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_writes_queued_behind_a_failure_are_not_written(self):
        """失敗の後ろに積まれていた書き込みを書かず、記録が失敗する前の所で終わること。"""
        from core.log_writer import LogWriter
        release = threading.Event()
        self.addCleanup(release.set)
        f = open(self.path, "w", encoding="utf-8", buffering=1)
        self.addCleanup(f.close)
        raw = _FailOnceFile(f, release, fail_on="two\n")
        w = LogWriter(raw)

        w.write("one\n")
        w.flush()                        # 普段どおり書き終わってから戻る
        self.assertEqual(self._read(), "one\n", "前提: 失敗する前の行は書けている")

        w.write("two\n")
        w.flush()                        # ここで詰まり、待たずに積む側へ回る
        self.assertTrue(w.busy, "前提: 書き込みが詰まっている")
        self.assertIsNone(w.failure, "前提: まだ失敗していない")
        for text in ("three\n", "four\n"):
            w.write(text)                # 詰まっている間に積まれる
            w.flush()

        release.set()                    # "two" が失敗し、記録先は持ち直す
        self.assertTrue(w.wait(3.0), "失敗のあと、積んだ操作が片付かない")
        self.assertIsInstance(w.failure, OSError)

        self.assertEqual(raw.written, ["one\n"],
                         "失敗したあとも、後ろに積まれていた書き込みを書いた")
        self.assertEqual(self._read(), "one\n",
                         "記録の途中に穴があいたまま続きが書かれた"
                         "（警告の『記録は失敗する前の行までです』と合わない）")
        self.assertEqual(w.written_bytes, len("one" + os.linesep),
                         "書けていない分を書けた数に入れた")
        self.assertEqual(w.backlog, 0)

        with self.assertRaises(OSError):
            w.close()
        self.assertTrue(w.wait(3.0))
        self.assertTrue(w.closed, "失敗したあと、閉じていない")


if __name__ == "__main__":
    unittest.main()
