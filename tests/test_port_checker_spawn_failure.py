"""netstat / tasklist を起動できない環境でも、結果が利用者へ届くことを確認する。

netstat 経路の except だけが OSError を拾うよう直され、同じ run() の
リスニング経路と tasklist 経路は subprocess.CalledProcessError しか
拾っていなかった。netstat 自体を起動できない環境では、既定の
「すべてチェック」（check_type='all'）でリスニング経路が OSError を
送出して run() ごと死に、result_ready が一度も emit されない。
直したはずの「取得に失敗しました」という文面すら利用者へ届かない。
"""
import os
import subprocess
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
    return out


class SpawnFailureStillReachesTheUserTest(unittest.TestCase):
    def test_a_listening_check_survives_a_netstat_spawn_failure(self):
        """リスニング経路: netstat を起動できなくても結果が emit される。"""
        error = OSError("netstat を起動できません")
        with mock.patch("subprocess.check_output", side_effect=error):
            out = run_check(40404, "listening", "TCP")
        self.assertEqual(len(out), 1, "result_ready が emit されていない")
        self.assertIn("取得に失敗しました", out[0], out[0])
        self.assertNotIn("使用されていません", out[0], out[0])

    def test_an_all_check_survives_a_netstat_spawn_failure(self):
        """既定の「すべてチェック」でも run() が死なない。"""
        error = OSError("netstat を起動できません")
        with mock.patch("subprocess.check_output", side_effect=error):
            out = run_check(40404, "all", "TCP")
        self.assertEqual(len(out), 1, "result_ready が emit されていない")
        self.assertIn("取得に失敗しました", out[0], out[0])

    def test_a_netstat_check_survives_a_tasklist_spawn_failure(self):
        """tasklist を起動できなくても、netstat の結果ごと捨てない。"""
        calls = []

        def fake_check_output(cmd, *args, **kwargs):
            calls.append(cmd)
            if cmd.startswith("netstat"):
                return NETSTAT_OUTPUT
            raise OSError("tasklist を起動できません")

        with mock.patch("subprocess.check_output", side_effect=fake_check_output):
            out = run_check(40404, "netstat", "TCP")
        self.assertEqual(len(out), 1, "result_ready が emit されていない")
        self.assertTrue(any(c.startswith("tasklist") for c in calls), calls)
        self.assertIn("4242", out[0], out[0])
        self.assertIn("プロセス情報取得失敗", out[0], out[0])

    def test_an_unexpected_error_still_reaches_the_user(self):
        """想定外の例外でも、黙って消えずに「確認できていません」と伝える。"""
        with mock.patch("ui.port_checker_gui.select_netstat_lines",
                        side_effect=RuntimeError("boom")):
            with mock.patch("subprocess.check_output", return_value=NETSTAT_OUTPUT):
                out = run_check(40404, "netstat", "TCP")
        self.assertEqual(len(out), 1, "result_ready が emit されていない")
        self.assertIn("確認できていません", out[0], out[0])

    def test_a_successful_check_is_unchanged(self):
        """成功時の文面は変えない。"""
        with mock.patch("subprocess.check_output", return_value=NETSTAT_OUTPUT):
            out = run_check(40404, "listening", "TCP")
        self.assertEqual(len(out), 1)
        self.assertIn("4242", out[0], out[0])
        self.assertNotIn("取得に失敗しました", out[0], out[0])


if __name__ == "__main__":
    unittest.main()
