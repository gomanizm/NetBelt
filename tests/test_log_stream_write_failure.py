# -*- coding: utf-8 -*-
"""凍結ビルドのログへ書けなくても、print が例外にならないことを検証する。

exe では main.py の _setup_logging が sys.stdout / sys.stderr をログファイルへ
差し替える。ディスクが一杯・共有が切れた・権限が変わったなどで write が
OSError になると、各所の print がそのまま例外になり、受信スレッドや後始末が
途中で止まっていた（Telnet の受信、Syslog TCP の accept、SFTP / SNMP の開始、
接続の置き換えの後始末など）。差し替えるストリームを、書けなくても例外に
しない包みにする。
"""
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _BrokenStream(io.TextIOBase):
    """write / flush が必ず OSError を投げる出力先。"""

    encoding = "utf-8"
    errors = "strict"

    def __init__(self):
        self.closed_called = False

    def write(self, s):
        raise OSError(28, "No space left on device")

    def flush(self):
        raise OSError(28, "No space left on device")

    def fileno(self):
        return 99

    def close(self):
        self.closed_called = True


class _Cp1252Stream(io.TextIOBase):
    """cp1252 で encode できない文字を UnicodeEncodeError にする出力先。"""

    encoding = "cp1252"
    errors = "strict"

    def __init__(self):
        self.parts = []

    def write(self, s):
        s.encode(self.encoding)
        self.parts.append(s)
        return len(s)

    def flush(self):
        pass


class TolerantStreamTest(unittest.TestCase):
    def test_print_to_a_broken_stream_does_not_raise(self):
        import main
        inner = _BrokenStream()
        stream = main._TolerantStream(inner)
        print("[Test] ディスクが一杯", file=stream)
        print("[Test] 2 行目", file=stream, flush=True)
        stream.flush()
        self.assertGreaterEqual(stream.failures, 3)
        self.assertIsInstance(stream.last_error, OSError)
        self.assertIn("No space left", str(stream.last_error))

    def test_unencodable_text_is_written_with_replacement(self):
        import main
        inner = _Cp1252Stream()
        stream = main._TolerantStream(inner)
        print("Router 接続 ok", file=stream)
        written = "".join(inner.parts)
        self.assertIn("Router ", written)
        self.assertIn(" ok", written)
        self.assertEqual(stream.failures, 1)
        self.assertIsInstance(stream.last_error, UnicodeEncodeError)

    def test_replacement_failure_is_also_swallowed(self):
        import main

        class _AlwaysBadEncoding(_Cp1252Stream):
            def write(self, s):
                raise UnicodeEncodeError("cp1252", s, 0, 1, "boom")

        stream = main._TolerantStream(_AlwaysBadEncoding())
        print("接続", file=stream)
        self.assertGreaterEqual(stream.failures, 1)

    def test_attributes_are_delegated(self):
        import main
        inner = _BrokenStream()
        stream = main._TolerantStream(inner)
        self.assertEqual(stream.encoding, "utf-8")
        self.assertEqual(stream.errors, "strict")
        self.assertEqual(stream.fileno(), 99)
        stream.close()
        self.assertTrue(inner.closed_called)

    def test_writes_reach_a_healthy_stream(self):
        import main
        inner = io.StringIO()
        stream = main._TolerantStream(inner)
        n = stream.write("abc\n")
        stream.writelines(["d", "e\n"])
        stream.flush()
        self.assertEqual(n, 4)
        self.assertEqual(inner.getvalue(), "abc\nde\n")
        self.assertEqual(stream.failures, 0)
        self.assertIsNone(stream.last_error)

    def test_excepthook_survives_a_broken_stderr(self):
        import main
        saved_hook = sys.excepthook
        self.addCleanup(setattr, sys, "excepthook", saved_hook)
        main.install_excepthook(None)
        with mock.patch.object(sys, "stderr", main._TolerantStream(_BrokenStream())), \
                mock.patch("PyQt6.QtWidgets.QMessageBox.critical") as critical:
            try:
                raise RuntimeError("HOOK_BROKEN_STDERR")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
        self.assertTrue(critical.called)


class SetupLoggingWrapsTest(unittest.TestCase):
    def _setup(self, frozen):
        import main
        saved = sys.stdout, sys.stderr
        local = tempfile.mkdtemp(prefix="netbelt-tolerant-")
        patches = [mock.patch.dict(os.environ, {"LOCALAPPDATA": local})]
        if frozen:
            patches.append(mock.patch.object(sys, "frozen", True, create=True))
        for p in patches:
            p.start()
        try:
            log_path = main._setup_logging()
        finally:
            for p in reversed(patches):
                p.stop()
            replaced = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = saved
        return log_path, replaced, saved

    def test_frozen_build_wraps_stdout_and_stderr(self):
        import main
        log_path, (out, err), _ = self._setup(frozen=True)
        self.assertIsNotNone(log_path)
        self.addCleanup(out.close)
        self.assertIsInstance(out, main._TolerantStream)
        self.assertIs(out, err)
        print("[Test] wrapped line", file=out)
        out.flush()
        with open(log_path, encoding="utf-8") as f:
            self.assertIn("[Test] wrapped line", f.read())

    def test_not_frozen_leaves_streams_alone(self):
        log_path, replaced, saved = self._setup(frozen=False)
        self.assertIsNone(log_path)
        self.assertIs(replaced[0], saved[0])
        self.assertIs(replaced[1], saved[1])


if __name__ == "__main__":
    unittest.main()
