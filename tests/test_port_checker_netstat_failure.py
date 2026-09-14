"""netstat の取得に失敗したとき、「使用されていません」と言い切らないことを確認する。

netstat 経路の except 節は取得失敗を output = "" に潰していたため、行が
0 件のとき（＝本当に空き）と区別が付かず、どちらも「このポートは現在
使用されていません」と表示していた。同じファイルのリスニング経路は同じ
例外を「接続情報の取得に失敗しました」と出しており、netstat 経路だけの
取りこぼしだった。
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


def run_check(port, check_type, protocol):
    """PortCheckThread を同じスレッドで走らせ、結果文字列を返す。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from ui.port_checker_gui import PortCheckThread
    out = []
    t = PortCheckThread(port, check_type, protocol)
    t.result_ready.connect(out.append)
    t.run()
    return "".join(out)


class NetstatFailureIsNotReportedAsFreeTest(unittest.TestCase):
    def _run_with_failure(self, error):
        with mock.patch("subprocess.check_output", side_effect=error) as called:
            result = run_check(40404, "netstat", "TCP")
        self.assertTrue(called.called, "netstat が呼ばれていない")
        return result

    def _assert_reported_as_unknown(self, result):
        self.assertNotIn("使用されていません", result, result)
        self.assertNotIn("見つかりませんでした", result, result)
        self.assertIn("取得に失敗しました", result, result)

    def test_a_nonzero_exit_is_reported_as_a_failure(self):
        error = subprocess.CalledProcessError(1, "netstat -ano")
        self._assert_reported_as_unknown(self._run_with_failure(error))

    def test_a_missing_netstat_is_reported_as_a_failure(self):
        error = FileNotFoundError(2, "No such file or directory")
        self._assert_reported_as_unknown(self._run_with_failure(error))

    def test_an_os_error_is_reported_as_a_failure(self):
        error = OSError("netstat を起動できません")
        self._assert_reported_as_unknown(self._run_with_failure(error))

    def test_an_empty_output_still_reports_the_port_as_free(self):
        """取得に成功して 0 件なら、これまでどおり「使用されていません」。"""
        with mock.patch("subprocess.check_output", return_value=""):
            result = run_check(40404, "netstat", "TCP")
        self.assertIn("使用されていません", result, result)
        self.assertNotIn("取得に失敗しました", result, result)


if __name__ == "__main__":
    unittest.main()
