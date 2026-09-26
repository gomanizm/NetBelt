"""内蔵 SFTP サーバーの診断行（print）の、1 行の長さに上限を掛ける件。

何が起きていたか（検査役の実測、11191e7。127.0.0.1 のみ）: 要求ごとの行は
種類ごとに 1 分 100 行へ間引いた（_LogLimiter）。ところが数えているのは行数
だけで、認証済みの相手は 1 行の長さを決められる。例外の文言には要求のパスが
入るので、名前を約 32K 文字（Windows のパスの上限）にすると 1 行が約 32.8KB
（rename は 2 本入って約 65KB）。stat・lstat・open・remove・rename・mkdir・
rmdir・chmod を各 110 回（2.8 秒）送るだけで、1 分の窓に 29,529,100 バイト
出た。毎分くり返すと約 1.77GB/時で、行数を抑える前の実測（1.2〜1.7GB/時）と
同じ桁。exe では標準出力が上限も回転も無いログファイルなので、上限は効いて
いるのにディスクを埋められる点が残っていた。

どう直したか: _LogLimiter.log で、出す行を一定の長さ（MAX_LINE_CHARS）で切り、
切った文字数を「...(+N chars)」で添える。短い行はこれまでどおり変えない。
決まった「種類ごとに 1 分 100 行＋省略件数の 1 行」はそのまま。
"""
import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

LIMIT_PER_MINUTE = 100
# 1 行の長さの目安（文字）。切る長さそのものではなく、元の件（約 32K 文字）
# より十分に短いことを確かめるための上限
LINE_BOUND = 1000
LONG_NAME = "/" + "n" * 30000
CLIPPED = re.compile(r"\.\.\.\(\+(\d+) chars\)$")


class SftpServerPrintLineLengthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import core.sftp_server as sftp_server
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=data_dir)
        home.start()
        self.addCleanup(home.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-line-length-")
        # 前のテストが同じ 1 分の枠を使い切っていても影響しないよう、
        # 間引きの状態をこのテストだけの新しいものにする
        patcher = mock.patch.object(sftp_server, "_diag_log",
                                    sftp_server._LogLimiter())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _handler(self):
        from core.sftp_server import SFTPServerHandler
        return SFTPServerHandler(None, self.root)

    @staticmethod
    def _printed(action):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            action()
        return out.getvalue()

    # --- 本題 ------------------------------------------------------------

    def test_long_names_do_not_make_long_lines(self):
        handler = self._handler()
        attempts = LIMIT_PER_MINUTE + 20

        def flood():
            for _ in range(attempts):
                handler.stat(LONG_NAME)

        text = self._printed(flood)
        lines = [line for line in text.splitlines() if "stat error" in line]
        self.assertGreater(len(lines), 0, "最初の行まで消えている")
        longest = max(len(line) for line in lines)
        self.assertLessEqual(
            longest, LINE_BOUND,
            "長い名前の stat で 1 行が %d 文字になっている" % longest)
        self.assertLessEqual(
            len(text.encode("utf-8")), LIMIT_PER_MINUTE * LINE_BOUND,
            "1 分の窓の出力が行数の上限×1 行の上限を超えている")

    def test_a_rename_with_two_long_names_is_clipped_too(self):
        handler = self._handler()
        text = self._printed(
            lambda: handler.rename(LONG_NAME, LONG_NAME + "x"))
        lines = [line for line in text.splitlines() if "rename error" in line]
        self.assertEqual(len(lines), 1, text[:2000])
        self.assertLessEqual(len(lines[0]), LINE_BOUND,
                             "rename の 1 行が %d 文字になっている"
                             % len(lines[0]))

    def test_a_clipped_line_keeps_its_head_and_says_how_much_was_cut(self):
        import core.sftp_server as sftp_server
        original = "[SFTP Server] stat error: " + "p" * 5000
        text = self._printed(
            lambda: sftp_server._log_limited("stat error", original))
        line = text.rstrip("\n")
        self.assertNotIn("\n", line)
        match = CLIPPED.search(line)
        self.assertIsNotNone(match, "切ったことが行に書かれていない: %r"
                             % line[-80:])
        head = line[:match.start()]
        self.assertTrue(original.startswith(head),
                        "行の頭が元の文言のままになっていない")
        self.assertTrue(head.startswith("[SFTP Server] stat error: "))
        self.assertEqual(len(head) + int(match.group(1)), len(original),
                         "切った文字数が合わない")

    def test_a_short_line_is_printed_unchanged(self):
        handler = self._handler()
        text = self._printed(lambda: handler.stat("/no-such-file.cfg"))
        lines = text.splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith("[SFTP Server] stat error: "),
                        lines[0])
        self.assertIsNone(CLIPPED.search(lines[0]),
                          "短い行まで切っている: %r" % lines[0])
        # 例外の文言の末尾（パス）まで残っている
        self.assertTrue(lines[0].endswith("no-such-file.cfg'"), lines[0])

    def test_the_line_cap_still_counts_lines_per_kind(self):
        """長い行を切っても、1 分 100 行と省略件数の数え方は変わらない"""
        import core.sftp_server as sftp_server
        handler = self._handler()
        attempts = LIMIT_PER_MINUTE + 30

        def flood():
            for _ in range(attempts):
                handler.stat(LONG_NAME)
            sftp_server._diag_log.flush()

        lines = self._printed(flood).splitlines()
        self.assertEqual(
            sum(1 for line in lines if "stat error: " in line
                and "suppressed" not in line), LIMIT_PER_MINUTE)
        summary = [line for line in lines if "suppressed" in line]
        self.assertEqual(len(summary), 1, summary)
        self.assertIn("stat error: 30 more line(s) suppressed", summary[0])


if __name__ == "__main__":
    unittest.main()
