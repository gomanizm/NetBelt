"""改行を含まない長大な出力でも、文書の上限が効くことを検証する。

_render_screen は折り返しで続いている履歴行を改行なしで連結する（窓を
縮めている間に流れた出力が刻まれたまま記録に残らないようにするため）。
そのため改行を一度も含まない出力では QTextBlock が 1 個のまま伸び続け、
setMaximumBlockCount(MAX_DOCUMENT_BLOCKS) の削除条件（ブロック数）に
永久に達しない。実測: 100,000 文字を流しても blockCount は 31 のままで、
一番長いブロックが 98,540 文字あった。所要時間は投入量に対してほぼ
二乗で伸びる（同じ量の改行ありの出力は一瞬で終わる）。機器が
バイナリを cat したときなどに、GUI が長時間止まりメモリも解放されない。

連結する履歴行に文字数の上限を置き、超えたら強制的にブロックを切る。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


def _block_lengths(document):
    """文書の各ブロックの長さ（ブロック区切りを含む）を返す。"""
    lengths = []
    block = document.begin()
    while block.isValid():
        lengths.append(block.length())
        block = block.next()
    return lengths


class TerminalUnwrappedOutputLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        terminal = w.create_terminal_tab("dev")
        return w, terminal

    @staticmethod
    def _feed(w, text, chunk=8192):
        """機器からの受信と同じように、まとまった塊で流し込む。"""
        for base in range(0, len(text), chunk):
            w.append_output("dev", text[base:base + chunk])

    def test_output_without_newlines_is_cut_into_blocks(self):
        """改行の無い出力でも、ブロックが際限なく伸びないこと。"""
        from ui.terminal_widget import TerminalWidget
        w, terminal = self._widget()

        self._feed(w, "X" * 100000)

        document = terminal.document()
        longest = max(_block_lengths(document))
        # 切るのは履歴へ押し出された画面行 1 本を足した後なので、
        # 上限を画面 1 行ぶん（＋ブロック区切り 1 文字）超えうる
        allowed = TerminalWidget.MAX_BLOCK_CHARS + terminal._screen.cols + 1
        self.assertLessEqual(
            longest, allowed,
            "改行の無い出力で 1 ブロックが %d 文字まで伸びている" % longest)

    def test_no_character_is_lost_when_a_block_is_cut(self):
        """強制的にブロックを切っても、文字そのものは落とさないこと。"""
        w, terminal = self._widget()

        self._feed(w, "X" * 100000)

        self.assertEqual(terminal.toPlainText().count("X"), 100000,
                         "ブロックを切るときに文字を落としている")

    def test_the_document_cap_finally_bounds_an_endless_line(self):
        """改行の無い出力でも blockCount と文書長が頭打ちになること。"""
        from ui.terminal_widget import TerminalWidget
        blocks_cap = 60
        chars_cap = 400
        for name, value in (("MAX_DOCUMENT_BLOCKS", blocks_cap),
                            ("MAX_BLOCK_CHARS", chars_cap)):
            patcher = mock.patch.object(TerminalWidget, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        w, terminal = self._widget()

        self._feed(w, "X" * 200000)

        document = terminal.document()
        self.assertLessEqual(document.blockCount(), blocks_cap,
                             "ブロック数の上限が効いていない")
        bound = blocks_cap * (chars_cap + terminal._screen.cols + 1)
        self.assertLess(document.characterCount(), bound,
                        "文書の長さが上限で抑えられていない: %d 文字"
                        % document.characterCount())

    def test_output_with_newlines_is_unchanged(self):
        """改行のある普通の出力は、これまでどおり 1 行 1 ブロックのこと（対照）。"""
        w, terminal = self._widget()

        for i in range(200):
            w.append_output("dev", "line %06d\r\n" % i)

        text = terminal.toPlainText()
        self.assertIn("line 000000", text, "前提: 上限には達していない")
        numbered = [ln for ln in text.split("\n") if ln.startswith("line ")]
        self.assertEqual(numbered,
                         ["line %06d" % i for i in range(200)],
                         "改行のある出力の行が崩れた")


if __name__ == "__main__":
    unittest.main()
