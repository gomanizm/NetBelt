"""README のファイアウォール案内が、実装と食い違っていないことを検証する。

v1.3.0 で「受信サーバは起動しても受信許可ルールを作らない。通らない
ときは各パネルのボタンを押す」へ変えた（tests/test_firewall_policy.py
が保証している）。README はそのとき更新されず、v1.0.0 初版の「起動
すると自動でルールを追加し、初回だけ UAC が出る」のまま残っていた。

新しいポートで受信できない利用者が README だけを読むと、押すべき
ボタンの存在に辿り着けない。文面が実装と同じ言葉を使っているかを、
コード側から取り出した値と突き合わせて確かめる。
"""
import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(REPO_ROOT, "README.md")
HEADING = "## ファイアウォールについて"

sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

# 受信して待つ5つのパネル。ボタン名はここから取り出す。
PANELS = (
    "ftp_server_panel.py",
    "tftp_server_panel.py",
    "syslog_panel.py",
    "sftp_server_panel.py",
    "snmp_panel.py",
)
_BUTTON = re.compile(r'fw_allow_btn\s*=\s*QPushButton\("([^"]+)"\)')


def _firewall_section():
    """README の「ファイアウォールについて」節だけを返す。"""
    text = io.open(README, encoding="utf-8").read()
    start = text.find(HEADING)
    if start < 0:
        raise AssertionError("README に %s が見つからない" % HEADING)
    end = text.find("\n## ", start + len(HEADING))
    return text[start:] if end < 0 else text[start:end]


class ReadmeFirewallSectionTest(unittest.TestCase):

    def setUp(self):
        self.section = _firewall_section()

    def test_the_readme_names_the_button_the_panels_actually_show(self):
        """許可ボタンの名前を、パネルと同じ文字列で案内していること。

        受信できないときに押すものが README に書かれていなければ、
        利用者はそこへ辿り着けない。パネル側の QPushButton の文言を
        読み出して突き合わせるので、片方だけ変えるとここで落ちる。
        """
        labels = set()
        for name in PANELS:
            src = io.open(os.path.join(REPO_ROOT, "src", "ui", name),
                          encoding="utf-8").read()
            found = _BUTTON.search(src)
            self.assertIsNotNone(found, "%s に許可ボタンが無い" % name)
            labels.add(found.group(1))
        self.assertEqual(len(labels), 1,
                         "パネルごとにボタン名が違う: %r" % sorted(labels))

        label = labels.pop()
        self.assertIn(label, self.section,
                      "README が許可ボタン %r を案内していない" % label)

    def test_the_readme_shows_the_rule_name_format_the_code_builds(self):
        """ルール名の形式が、rule_name() の組み立て方と一致すること。"""
        from core.firewall import rule_name

        template = rule_name("<サービス>", "<プロトコル>", "<ポート>")
        self.assertIn(template, self.section,
                      "README のルール名 %r が実装と違う" % template)


if __name__ == "__main__":
    unittest.main()
