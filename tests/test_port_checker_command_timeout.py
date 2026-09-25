"""netstat / tasklist が応答しないとき、ポートチェックが終わらなくならないことを確認する。

PortCheckThread は netstat と tasklist を shell=True・期限なしで起動し、
終わるのを待っていた。子プロセスが応答しないと _build_result が戻らず、
result_ready も finished も来ないので、確認ボタンは無効のまま残る。
実測: tasklist の代わりに 40 秒居座るバッチを PATH の先頭に置き、ポート
135/TCP の「使用状況確認」を押すと、10 秒後もボタンは無効・結果は 0 文字で、
取れているはずの netstat の結果も表示されなかった（子が終わった 55 秒後に
ようやく出た）。netstat で固まる場合も同じ。キャンセルの経路も無い。

直し方: 期限（COMMAND_TIMEOUT_SECONDS）を付け、shell を通さずに直接起動する。
shell=True のまま期限を付けても効かない。期限切れで止まるのは cmd.exe だけで、
孫プロセスがパイプを持ち続けるため、戻るのは孫が終わってから（実測: timeout=2
で 14 秒）。shell=True が付けていた「コンソール窓を隠す」は CREATE_NO_WINDOW で
代える。期限切れは起動・終了コードの失敗と同じく「取得に失敗しました／確認
できていません」と伝え、tasklist の期限切れでは取れている netstat の行を残す。
"""
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

# PID 4242 が 40404/TCP を待ち受けている netstat -ano の出力（例示用）
LISTEN_LINE = "  TCP     192.0.2.1:40404        0.0.0.0:0              LISTENING       4242"
NETSTAT_OUTPUT = (
    "\r\n"
    "アクティブな接続\r\n"
    "\r\n"
    "  プロト  ローカル アドレス      外部アドレス           状態            PID\r\n"
    + LISTEN_LINE + "\r\n"
)


def _command_text(cmd):
    return cmd if isinstance(cmd, str) else " ".join(cmd)


class PortCheckerCommandTimeoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _run(self, check_type, fake):
        """PortCheckThread を同じスレッドで走らせ、emit された結果の一覧を返す。"""
        from ui.port_checker_gui import PortCheckThread
        out = []
        t = PortCheckThread(40404, check_type, "TCP")
        t.result_ready.connect(out.append)
        with mock.patch("subprocess.check_output", side_effect=fake):
            t.run()
        return out

    @staticmethod
    def _timeout(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 15)

    def test_a_timed_out_netstat_is_reported_as_unknown(self):
        """netstat の期限切れは「取得に失敗しました／確認できていません」になること。"""
        out = self._run("netstat", self._timeout)

        self.assertEqual(len(out), 1, "result_ready が 1 回だけ emit されていない")
        self.assertIn("取得に失敗しました", out[0], out[0])
        self.assertIn("確認できていません", out[0], out[0])
        self.assertNotIn("使用されていません", out[0], out[0])

    def test_a_timed_out_listening_check_is_reported_as_unknown(self):
        """リスニング経路の netstat の期限切れも同じく失敗として伝えること。"""
        out = self._run("listening", self._timeout)

        self.assertEqual(len(out), 1, "result_ready が 1 回だけ emit されていない")
        self.assertIn("取得に失敗しました", out[0], out[0])
        self.assertIn("確認できていません", out[0], out[0])

    def test_a_timed_out_tasklist_keeps_the_netstat_lines(self):
        """tasklist が期限切れでも、取れている netstat の行を捨てないこと。"""
        def fake(cmd, *args, **kwargs):
            if _command_text(cmd).startswith("netstat"):
                return NETSTAT_OUTPUT
            return self._timeout(cmd, *args, **kwargs)

        out = self._run("netstat", fake)

        self.assertEqual(len(out), 1, "result_ready が 1 回だけ emit されていない")
        self.assertIn(LISTEN_LINE, out[0], "netstat の結果が消えた: %s" % out[0])
        self.assertIn("PID 4242: プロセス情報取得失敗", out[0], out[0])
        self.assertNotIn("想定外のエラー", out[0], out[0])

    def test_every_command_has_a_deadline_and_no_shell(self):
        """すべての起動に期限が付き、shell を通さず、窓を出さないこと。"""
        calls = []

        def fake(cmd, *args, **kwargs):
            calls.append((_command_text(cmd), kwargs))
            if _command_text(cmd).startswith("netstat"):
                return NETSTAT_OUTPUT
            return '"svc.exe","4242","Services","0","1,024 K"\r\n'

        self._run("all", fake)

        self.assertTrue(any(c.startswith("tasklist") for c, _ in calls), calls)
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for cmd, kwargs in calls:
            self.assertFalse(kwargs.get("shell"),
                             "%s を shell 経由で起動している（期限が効かない）" % cmd)
            self.assertIsNotNone(kwargs.get("timeout"), "%s に期限が無い" % cmd)
            self.assertEqual(kwargs.get("creationflags", 0) & no_window, no_window,
                             "%s がコンソール窓を出す" % cmd)

    def test_a_tasklist_that_never_returns_does_not_hang_the_check(self):
        """本物の子プロセスが応答しなくても、期限で結果が届くこと。

        tasklist の代わりに 20 秒居座る子プロセスを起動する（渡された引数は
        そのまま使う）。期限を 1 秒に縮め、8 秒以内に結果が届くかを見る。
        """
        from ui.port_checker_gui import PortCheckThread

        real = subprocess.check_output
        hang = [sys.executable, "-c", "import time; time.sleep(20)"]

        def fake(cmd, *args, **kwargs):
            if _command_text(cmd).startswith("netstat"):
                return NETSTAT_OUTPUT
            return real(hang, *args, **kwargs)

        out = []
        t = PortCheckThread(40404, "netstat", "TCP")
        t.result_ready.connect(out.append)
        # 期限切れにならない（修正前の）場合も、子が終わるまで待ってから片付ける
        self.addCleanup(t.wait, 30000)
        with mock.patch("subprocess.check_output", side_effect=fake), \
                mock.patch.object(PortCheckThread, "COMMAND_TIMEOUT_SECONDS", 1,
                                  create=True):
            started = time.time()
            t.start()
            deadline = started + 8.0
            while not out and time.time() < deadline:
                self.app.processEvents()
                time.sleep(0.02)
            elapsed = time.time() - started
            self.assertTrue(out, "応答しない tasklist で %.1f 秒たっても結果が来ない" % elapsed)
            self.assertTrue(t.wait(5000), "結果のあともスレッドが終わらない")

        self.assertIn(LISTEN_LINE, out[0], out[0])
        self.assertIn("PID 4242: プロセス情報取得失敗", out[0], out[0])


if __name__ == "__main__":
    unittest.main()
