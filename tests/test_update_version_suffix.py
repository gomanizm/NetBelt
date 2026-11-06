# -*- coding: utf-8 -*-
"""接尾辞付きのタグが、本物の新版を隠さないことを検証する。

何が起きていたか。リリースの並べ替え（_version_key）は区切りの中の数字を
全部つないで読み（'4-rc1' → 41）、新しいかどうかの判定（compare_versions）は
先頭の数字だけを読んでいた（'4-rc1' → 4）。事前公開の印を付け忘れた
v1.3.4-rc1 が 1 つあるだけで、それが v1.3.5 より上に並んで「最新」になり、
1.3.4 の利用者には「最新です」と出て v1.3.5 が通知されなかった。

両方を同じ読み方（数字の並びだけで比べ、接尾辞付きは同じ数字の正式版より前）
にそろえる。GitHub API は叩かない。
"""
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


def release(tag):
    return {
        "tag_name": "v" + tag,
        "body": "",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": "NetBelt-v%s-Windows-Portable.zip" % tag,
             "url": "https://example.com/%s.zip" % tag},
            {"name": "NetBelt-v%s-Windows-Portable.zip.sha256" % tag,
             "url": "https://example.com/%s.sha256" % tag},
        ],
        "published_at": "2026-08-27T00:00:00Z",
    }


def check(releases, current):
    from core.version_manager import VersionManager

    response = mock.Mock()
    response.json.return_value = releases
    response.raise_for_status.return_value = None
    with mock.patch("core.version_manager.requests.get",
                    return_value=response), \
            mock.patch.object(VersionManager, "CURRENT_VERSION", current):
        return VersionManager().check_for_updates()


class SuffixedTagTest(unittest.TestCase):

    def test_suffixed_tag_does_not_hide_newer_release(self):
        for suffixed in ("1.3.4-rc1", "1.3.4-hotfix2"):
            with self.subTest(tag=suffixed):
                for releases in ([release("1.3.5"), release(suffixed)],
                                 [release(suffixed), release("1.3.5")]):
                    info = check(releases, current="1.3.4")
                    self.assertTrue(info["available"])
                    self.assertEqual(info["version"], "1.3.5")
                    info = check(releases, current="1.3.3")
                    self.assertEqual(info["version"], "1.3.5")

    def test_suffixed_sorts_before_same_numbers(self):
        from core.version_manager import VersionManager
        cmp = VersionManager.compare_versions
        self.assertEqual(cmp("1.3.4-rc1", "1.3.4"), -1)
        self.assertEqual(cmp("1.3.4", "1.3.4-rc1"), 1)
        self.assertEqual(cmp("1.3.4-rc1", "1.3.3"), 1)
        self.assertEqual(cmp("1.3.4-rc1", "1.3.5"), -1)
        # 今までどおりの読み方
        self.assertEqual(cmp("1.3.4", "v1.3.4"), 0)
        self.assertEqual(cmp("1.3", "1.3.0"), 0)
        self.assertEqual(cmp("1.10.0", "1.9.9"), 1)

    def test_sort_order_matches_compare_versions(self):
        from core.version_manager import VersionManager
        tags = ["1.3.4-rc1", "1.3.5", "1.3.4", "1.3.4-hotfix2", "1.2.99",
                "1.3", "1.3.0.1"]
        ordered = [r["tag_name"].lstrip("v") for r in
                   VersionManager._published([release(t) for t in tags])]
        for newer, older in zip(ordered, ordered[1:]):
            self.assertGreaterEqual(
                VersionManager.compare_versions(newer, older), 0,
                (newer, older, ordered))
        self.assertEqual(ordered[0], "1.3.5")


if __name__ == "__main__":
    unittest.main()
