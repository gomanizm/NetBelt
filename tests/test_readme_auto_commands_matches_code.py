"""README の自動実行コマンドの対象が、実際の送信先と食い違っていないことを検証する。

何が起きていたか（実測、基準 9fee4af）: README は日本語（22 行目）
「SSH / Telnet が対象です。」、英語（225 行目）"Applies to SSH and Telnet."
だった。main_window._run_auto_commands が除外するのは自動検出の COM ポート
だけで、プロトコルは見ていない。登録済みの機器なら SSH / Telnet /
コンソール（シリアル）のどれで繋いでも送られる。グループ編集ダイアログの
説明（group_dialog.py）は 2026-09-20 の利用者決定で実際に合わせてあり
（tests/test_group_auto_commands_help_matches_behaviour.py）、README だけが
取り残されていた。README は配布物の README.txt にもなる。

直し方: 両言語の箇条を、登録済みの機器ならコンソールでも送ること、ツリーの
「コンソール接続」に自動で並ぶ COM ポートはどのグループにも属さないので
送らないことに合わせた。グループ名はツリーの実装から取り出して突き合わせる。
"""
import io
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(REPO_ROOT, "README.md")
DEVICE_TREE = os.path.join(REPO_ROOT, "src", "ui", "device_tree.py")

# 自動検出した COM ポートを並べるツリーのグループ
_CONSOLE_GROUP = re.compile(r'console_group\s*=\s*QTreeWidgetItem\(self\.tree,\s*\["([^"]+)"\]\)')


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


def _console_group_label():
    with io.open(DEVICE_TREE, encoding="utf-8") as f:
        found = _CONSOLE_GROUP.search(f.read())
    if found is None:
        raise AssertionError("device_tree.py にコンソール接続のグループが見つからない")
    return found.group(1)


class ReadmeAutoCommandsTest(unittest.TestCase):

    def setUp(self):
        self.ja = _bullet("- **自動実行コマンド**")
        self.en = _bullet("- **Auto commands**")

    def test_japanese_names_console_connections_too(self):
        """登録済みのコンソール（シリアル）機器へも送ると書いてあること。"""
        self.assertNotIn("SSH / Telnet が対象です", self.ja,
                         "SSH / Telnet だけのように読めるが、実際はコンソールへも送る")
        self.assertIn("コンソール（シリアル）", self.ja)

    def test_japanese_says_autodetected_ports_are_excluded(self):
        """唯一の除外（ツリーに自動で並ぶ COM ポート）を書いてあること。"""
        self.assertIn("「%s」" % _console_group_label(), self.ja)
        self.assertIn("送られません", self.ja)

    def test_english_no_longer_limits_it_to_ssh_and_telnet(self):
        """英語でも SSH / Telnet だけとは言わず、同じ除外を書いてあること。"""
        self.assertNotIn("Applies to SSH and Telnet", self.en)
        self.assertIn("console", self.en)
        self.assertIn("「%s」" % _console_group_label(), self.en)


if __name__ == "__main__":
    unittest.main()
