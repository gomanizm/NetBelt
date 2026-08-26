"""ネットワーク機器の出力が正しく描かれることを、実機の受信バイトで検証する。

`tests/fixtures/cisco_ios_vt100.bin` は Catalyst 8000v (IOS-XE) へ SSH で
つなぎ、NetBelt と同じ `invoke_shell(term='vt100', width=80, height=24)` で
採った受信バイトそのもの。show version / show ip interface brief /
show ip route / show interfaces（ページャを 3 画面ぶん歩く）/ 打ち間違いを
backspace で消して打ち直す、を含む。実環境を指す値だけ文書用のものへ
置き換えてあり、制御文字は 1 バイトも変えていない。

**この 7058 バイトに、エスケープシーケンスは 1 つも含まれていない。**
届くのは `\\r` `\\n` `\\b` だけ。ネットワーク機器の画面はバックスペースの
上書きだけで組み立てられており、それが NetBelt の主用途である以上、
この経路を壊すことは許容できない。v1.2.0 の描画作り直しの成否も
まずここで測る。
"""
import io
import os
import re
import sys
import unittest

sys.path.insert(0, "src")

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
CAPTURE = "cisco_ios_vt100.bin"


def capture():
    raw = io.open(os.path.join(FIXTURES, CAPTURE), "rb").read()
    return raw.decode("utf-8", "replace")


class CiscoRenderingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _screen(self, *payloads):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        w.create_terminal_tab("dev")
        for payload in payloads:
            w.append_output("dev", payload)
        return w._terminals["dev"].toPlainText()

    def test_the_pager_prompt_is_erased(self):
        """--More-- が画面に残らないこと。

        機器は `\\b` と空白で消しにいく。消し方を取り違えると、
        show 出力のあちこちに --More-- が残る。
        """
        text = self._screen(capture())

        leftover = [l for l in text.split("\n") if "More" in l]
        self.assertEqual(leftover, [],
                         "ページャの表示が残っている: %r" % leftover[:3])

    def test_a_typo_corrected_with_backspace_renders_clean(self):
        """打ち間違いを消して打ち直した行が、正しく見えること。

        採取時に `show verzz` と打ち、backspace 2 回で消して
        `sion` と続けた。画面には `show version` だけが見えるべき。
        """
        text = self._screen(capture())

        self.assertIn("show version", text)
        self.assertNotIn("verzz", text, "消したはずの文字が残っている")

    def test_the_output_keeps_its_columns(self):
        """桁で読む出力が崩れないこと。

        機器の出力は桁で意味を持つ。採取時、直前のページャが次の
        コマンドの先頭文字を食べて `how ip interface brief` になり、
        機器が `^` で誤りの位置を指してきた。この `^` がずれると
        どこが悪いのか読めない。あわせて show ip route の凡例の
        字下げも見る。
        """
        lines = self._screen(capture()).split("\n")

        marker = [i for i, l in enumerate(lines) if l.strip() == "^"]
        self.assertEqual(len(marker), 1,
                         "^ の行が %d 行ある" % len(marker))
        at = marker[0]
        self.assertIn("Invalid input", lines[at + 1],
                      "^ の次に理由が続いていない: %r" % lines[at + 1])
        self.assertEqual(lines[at].index("^"), 10,
                         "^ の桁がずれている: %r" % lines[at])

        indented = [l for l in lines
                    if l.startswith("       ") and l.strip()]
        self.assertGreaterEqual(len(indented), 10,
                                "凡例の字下げが失われている")

    def test_every_split_point_renders_the_same(self):
        """受信がどこで切れても、同じ画面になること。"""
        raw = capture()
        whole = self._screen(raw)
        for cut in range(1, len(raw), 7):     # 7 バイトおきに全体を走査
            split = self._screen(raw[:cut], raw[cut:])
            if split != whole:
                self.fail(
                    "%d バイト目で分けると画面が変わる\n"
                    "  切れ目: ...%r | %r...\n"
                    "  差分: %+d 文字"
                    % (cut, raw[max(0, cut - 8):cut], raw[cut:cut + 8],
                       len(split) - len(whole)))


class CiscoCaptureIntegrityTest(unittest.TestCase):
    """採取データが、検証の材料として妥当であること。"""

    def test_the_device_sends_no_escape_sequences_at_all(self):
        """ネットワーク機器はエスケープを送ってこないこと。

        ここが崩れると、この検証の前提が変わる。逆に言えば、
        機器向けに必要なのは `\\r` `\\n` `\\b` の扱いだけということ。
        """
        text = capture()

        found = re.findall(r"\x1b\[[0-9:;<=>?]*[ -/]*[@-~]", text)
        self.assertEqual(found, [],
                         "エスケープが含まれている: %r" % found[:5])
        self.assertNotIn("\x1b", text)

    def test_the_capture_exercises_backspace_overwriting(self):
        """バックスペースでの上書きが実際に含まれていること。"""
        text = capture()

        self.assertGreater(text.count("\b"), 50,
                           "バックスペースが少なすぎる（材料として不足）")

    def test_the_capture_names_no_real_environment(self):
        """公開できる状態であること。"""
        text = capture()

        for pattern, label in (
                (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "私用 IPv4"),
                (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "私用 IPv4"),
                (r"\b5254\.[0-9a-f]{4}\.[0-9a-f]{4}\b", "実 MAC"),
                (r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", "MAC アドレス")):
            found = re.search(pattern, text, re.I)
            self.assertIsNone(
                found, "%s が残っている: %s"
                % (label, found.group(0) if found else ""))

        for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
            self.assertRegex(
                ip, r"^(?:192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|127\.|0\.0\.0\.0)",
                "文書用に割り当てられていないアドレス: %s" % ip)


if __name__ == "__main__":
    unittest.main()
