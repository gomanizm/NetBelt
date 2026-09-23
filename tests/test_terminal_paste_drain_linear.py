"""大きな貼り付けを送り切るまでの手間が、長さの 2 乗で増えないことを検証する。

何が起きていたか（実測、基準 470c538、QT_QPA_PLATFORM=offscreen）:
InteractiveTerminal._drain_send_queue は 512 文字（SEND_CHUNK）送るたびに、
残り全体を payload[512:] で切り出して列へ戻していた。N 文字の貼り付けを
送り切るまでに、残りのコピーだけで約 N²/1024 文字になる。右クリックの
貼り付け（確認ダイアログは mock で通し、送信先は key_pressed につないだ
偽の send）を送り切るまでの時間は、1MiB 0.16 秒、4MiB 2.60 秒、
8MiB 9.75 秒、16MiB 37.5 秒だった。長さが 2 倍になると約 4 倍かかる。
区切りごとにイベントループへ譲るので画面は固まらないが、その間 CPU を
使い続け、送り終わりが遅れる（受信側の _PendingOutput で解消したのと同じ形）。

どう直したか: 列の各件は、元の文字列と送り終えた位置で「まだ送っていない
文字列」を持ち（_Unsent）、位置を進めながら 512 文字ずつ取り出す。送る
順序・区切りの大きさ・停止での取り消し（送り始めた行は送り切る）・
イベントループへ譲る間隔は変えない。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, "src")


class PasteDrainIsLinearTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _terminal(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("dev")
        terminal = w._terminals["dev"]
        terminal.set_input_enabled(True)
        sent = []
        terminal.key_pressed.connect(sent.append)
        return terminal, sent

    def _drain_cpu_seconds(self, size):
        """size 文字の貼り付けを送り切るまでに使った CPU 時間と、送った区切りを返す。

        他の検証と同時に流れて CPU が混んでも振れにくいよう、経過時間では
        なくこのプロセスの CPU 時間で測る。
        """
        terminal, sent = self._terminal()
        body = ("x" * 63 + "\n") * (size // 64)
        started = time.process_time()
        terminal.send_text(body)
        while terminal._sending:
            self.app.processEvents()
        spent = time.process_time() - started
        return spent, sent, body

    def test_four_times_the_paste_costs_about_four_times_the_work(self):
        """4 倍の長さの貼り付けが、4 倍程度の手間で送り切れること（2 乗なら 16 倍）。"""
        small, _, _ = self._drain_cpu_seconds(2 * 1024 * 1024)
        large, sent, body = self._drain_cpu_seconds(8 * 1024 * 1024)

        # 長さに比例なら 4 倍、2 乗なら 16 倍（直す前の実測は 18 倍）。間の
        # 10 倍で分ける。Windows の CPU 時間は 15.6ms 刻みなので、短い方に
        # 1 刻みの余裕を持たせる
        self.assertLess(large, 10 * (small + 0.016),
                        "2MiB で %.2f 秒、8MiB で %.2f 秒: 長さの 2 乗で重くなっている"
                        % (small, large))
        # 速くしても、中身・順序・区切りの大きさは変わらないこと
        self.assertEqual("".join(sent), body.replace("\n", "\r"),
                         "送った中身か順序が変わった")
        self.assertEqual({len(chunk) for chunk in sent[:-1]}, {512},
                         "区切りの大きさが変わった")


if __name__ == "__main__":
    unittest.main()
