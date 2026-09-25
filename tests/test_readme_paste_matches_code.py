"""README のコピー / 貼り付けの案内が、実装と食い違っていないことを検証する。

編集メニューの「ペースト」（Ctrl+Shift+V）は、改行を含む内容でも確かめずに
機器へ送っていたためになくした。貼り付けは端末の右クリックだけで、改行や
制御文字を含むときは確認ダイアログを出す（TerminalWidget.custom_paste）。
コピーは範囲を選んだ時点で行い、Ctrl+Shift+C も残っている。

実測（検証役 release-06）: README は日本語（24 行目）・英語（207 行目）とも
「Ctrl+Shift+C / Ctrl+Shift+V」のままで、右クリックでの貼り付けにも確認
ダイアログにも範囲選択でのコピーにも触れていなかった。接続中を模した端末で
クリップボードに 'show version' を入れて Ctrl+Shift+V を押すと、内容は
送られず、押したキーの文字だけが機器へ送られた（offscreen で sent=['v']）。
右クリックでは sent=['show version'] と送られた。この README はワークフローで
README.txt として配布物にも入るので、配布物の操作説明も記載どおりに
操作すると貼り付けられない状態だった。

直し方: 両言語の案内を現在の操作に合わせた。ここでは、README が
Ctrl+Shift+V を案内していないこと、右クリックでの貼り付けと確認ダイアログを
案内していること、コピーのショートカットがメニューの実装と同じであることを
確かめる。
"""
import io
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(REPO_ROOT, "README.md")
MAIN_WINDOW = os.path.join(REPO_ROOT, "src", "ui", "main_window.py")

_COPY_SHORTCUT = re.compile(r'copy_action\.setShortcut\("([^"]+)"\)')


def _readme():
    with io.open(README, encoding="utf-8") as f:
        return f.read()


def _bullet(prefix):
    """prefix で始まる箇条書きの 1 行を返す。"""
    lines = [l for l in _readme().splitlines() if l.startswith(prefix)]
    if len(lines) != 1:
        raise AssertionError("README に %r の項目が 1 つだけあるはず: %r"
                             % (prefix, lines))
    return lines[0]


class ReadmePasteTest(unittest.TestCase):

    def setUp(self):
        self.ja = _bullet("- **コピー")
        self.en = _bullet("- **Copy")

    def test_the_removed_paste_shortcut_is_not_offered(self):
        """廃止した Ctrl+Shift+V を、どこでも案内していないこと。"""
        self.assertNotIn("Ctrl+Shift+V", _readme(),
                         "廃止したショートカットを貼り付け手順として案内している")

    def test_right_click_paste_is_described(self):
        """貼り付けは右クリックだと、両言語で案内していること。"""
        self.assertIn("右クリック", self.ja,
                      "日本語の案内に右クリックでの貼り付けが無い: " + self.ja)
        self.assertIn("right-click", self.en.lower(),
                      "英語の案内に右クリックでの貼り付けが無い: " + self.en)

    def test_the_confirmation_dialog_is_described(self):
        """改行や制御文字を含むときの確認を、両言語で案内していること。"""
        self.assertIn("確認", self.ja, self.ja)
        self.assertIn("改行", self.ja, self.ja)
        self.assertIn("confirm", self.en.lower(), self.en)
        self.assertIn("line break", self.en.lower(), self.en)

    def test_copy_on_select_is_described(self):
        """範囲を選んだ時点でコピーされることを、両言語で案内していること。"""
        self.assertIn("範囲を選ぶ", self.ja, self.ja)
        self.assertIn("select", self.en.lower(), self.en)

    def test_the_copy_shortcut_matches_the_menu(self):
        """コピーのショートカットは、編集メニューの実装と同じものを案内すること。"""
        with io.open(MAIN_WINDOW, encoding="utf-8") as f:
            found = _COPY_SHORTCUT.search(f.read())
        self.assertIsNotNone(found, "編集メニューのコピーのショートカットが見つからない")
        shortcut = found.group(1)
        self.assertIn(shortcut, self.ja, self.ja)
        self.assertIn(shortcut, self.en, self.en)


if __name__ == "__main__":
    unittest.main()
