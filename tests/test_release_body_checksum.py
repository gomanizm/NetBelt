"""リリース本文が、チェックサムの在り処を正しく案内することの回帰（release-02）。

.github/release-body.md は「📦 パッケージ内容」（ZIP の中身）の一覧に
`.sha256` を載せていた。ところがワークフロー（build-release.yml）は
Compress-Archive で portable\\* を ZIP にした「後」で、ZIP の外に
"<ZIP 名>.sha256" を作り、Create Release で ZIP と並べて別に添付している。

実測（公開済みの v1.1.0 / v1.1.1 / v1.2.0 / v1.3.0 の ZIP を zipfile で列挙）:
どの ZIP にも .sha256 は入っていない（中身は NetBelt.exe・README.txt・
LICENSE.txt・THIRD-PARTY-NOTICES.txt・CHANGELOG.txt・updater.bat・
mibs/README.md のみ）。.sha256 は Release に別添えの
NetBelt-vX.Y.Z-Windows-Portable.zip.sha256 として置かれている。
ZIP だけを落とした利用者は、案内されたチェックサムを展開先で探しても
見つからない。自動更新の照合（Release の添付を取りに行く）には影響しない。

直した形: パッケージ内容の一覧から外し、一覧の後に「ZIP の中ではなく
Release に別添えした ZIP 名.sha256」と実際の添付名（{{TAG}} で版が入る）で
案内する。
"""
import io
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BODY = os.path.join(REPO_ROOT, ".github", "release-body.md")
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "build-release.yml")
TAG = "v1.3.1"


def _read(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def _package_list(text):
    """「パッケージ内容」の見出しの下の箇条書き（続きの行を含む）を返す。"""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.startswith("### ") and "パッケージ内容" in l)
    items = []
    for line in lines[start + 1:]:
        if line.startswith("- ") or (items and line.startswith("  ")):
            items.append(line)
        elif items or line.startswith("#"):
            break
    return "\n".join(items)


class ReleaseBodyChecksumTest(unittest.TestCase):

    def setUp(self):
        self.body = _read(BODY)
        self.workflow = _read(WORKFLOW)

    def test_the_premise_the_checksum_is_attached_beside_the_zip(self):
        """前提: ワークフローは ZIP と別に "<ZIP 名>.sha256" を添付している。"""
        self.assertIn('$zipName = "NetBelt-v${version}-Windows-Portable.zip"',
                      self.workflow)
        self.assertIn('"$zipName.sha256"', self.workflow)
        self.assertIn("NetBelt-v*-Windows-Portable.zip.sha256", self.workflow)

    def test_the_package_contents_do_not_list_the_checksum(self):
        """ZIP の中身の一覧に、ZIP に入っていない .sha256 を載せないこと。"""
        items = _package_list(self.body)
        self.assertIn("NetBelt.exe", items, "一覧を読み取れていない")
        self.assertNotIn(".sha256", items,
                         "ZIP に入っていない .sha256 を中身として案内している:\n"
                         + items)

    def test_the_body_names_the_checksum_attached_to_the_release(self):
        """Release に別添えの添付名で案内すること（タグを差し込んだ後の形で見る）。"""
        rendered = self.body.replace("{{TAG}}", TAG)
        self.assertIn("NetBelt-%s-Windows-Portable.zip.sha256" % TAG, rendered,
                      "別添えのチェックサムの添付名を案内していない")


if __name__ == "__main__":
    unittest.main()
