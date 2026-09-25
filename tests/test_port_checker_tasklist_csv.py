"""tasklist の CSV から、カンマを含むプロセス名を正しく取り出すことを確認する。

PortCheckThread は tasklist /FO CSV の出力から引用符を全部消してから
カンマで分けていた。カンマは Windows のファイル名に使える文字で、実測
（PING.EXE を 'net,agent.exe' という名前で複製して起動）では、本物の
tasklist の出力 '"net,agent.exe","35156","Console","1","5,396 K"' に対して
「PID 35156: net」と表示された。占有しているプロセスの名前を誤って案内する。

直し方: CSV として引用符を解釈して読み（csv.reader）、最初の行の 1 列目を
プロセス名にする。行が無い・列が足りない（該当なしの「情報: …」など）ときは
これまでどおり何も出さない。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

# PID 4242 が 40404/TCP を待ち受けている netstat -ano の出力（例示用）
NETSTAT_OUTPUT = (
    "\r\n"
    "アクティブな接続\r\n"
    "\r\n"
    "  プロト  ローカル アドレス      外部アドレス           状態            PID\r\n"
    "  TCP     192.0.2.1:40404        0.0.0.0:0              LISTENING       4242\r\n"
)


class TasklistCsvTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _check(self, tasklist_output):
        """tasklist が tasklist_output を返すときの結果文字列を返す。"""
        from ui.port_checker_gui import PortCheckThread

        def fake(cmd, *args, **kwargs):
            text = cmd if isinstance(cmd, str) else " ".join(cmd)
            if text.startswith("netstat"):
                return NETSTAT_OUTPUT
            return tasklist_output

        out = []
        t = PortCheckThread(40404, "netstat", "TCP")
        t.result_ready.connect(out.append)
        with mock.patch("subprocess.check_output", side_effect=fake):
            t.run()
        self.assertEqual(len(out), 1, "result_ready が 1 回だけ emit されていない")
        return out[0]

    def test_a_process_name_with_a_comma_is_shown_whole(self):
        """カンマを含む実行ファイル名を、途中で切らずに出すこと。"""
        result = self._check('"net,agent.exe","4242","Console","1","5,396 K"\r\n')

        self.assertIn("PID 4242: net,agent.exe\n", result, result)

    def test_a_plain_process_name_is_unchanged(self):
        """普通の名前はこれまでどおり出ること。"""
        result = self._check('"svchost.exe","4242","Services","0","12,345 K"\r\n')

        self.assertIn("PID 4242: svchost.exe\n", result, result)

    def test_no_matching_task_prints_no_process_line(self):
        """該当なし（1 列だけの「情報: …」）や空の出力では、これまでどおり何も出さないこと。"""
        for output in ("情報: 指定された条件に一致するタスクは実行されていません。\r\n", ""):
            with self.subTest(output=output):
                result = self._check(output)
                self.assertNotIn("PID 4242:", result, result)
                self.assertNotIn("想定外のエラー", result, result)


if __name__ == "__main__":
    unittest.main()
