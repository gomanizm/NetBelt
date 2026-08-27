"""更新の通知に、何が変わったかを出す。

これまでリリース本文には案内文しか入っておらず、通知を見ても何が
変わったのか分からなかった。いまはリリース本文の先頭に CHANGELOG の
その版の節が入る。加えて、何世代か飛ばしている利用者には、その間の
ぶんも並べる。

GitHub API は叩かない。応答を作り物で与える。
"""
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


def release(version, body="", draft=False, prerelease=False):
    return {
        "tag_name": "v" + version,
        "body": body,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {"name": "NetBelt-v%s-Windows-Portable.zip" % version,
             "url": "https://example.com/%s.zip" % version},
            {"name": "NetBelt-v%s-Windows-Portable.zip.sha256" % version,
             "url": "https://example.com/%s.sha256" % version},
        ],
        "published_at": "2026-08-27T00:00:00Z",
    }


def check(releases, current="1.0.0"):
    from core.version_manager import VersionManager

    response = mock.Mock()
    response.json.return_value = releases
    response.raise_for_status.return_value = None
    with mock.patch("core.version_manager.requests.get",
                    return_value=response), \
            mock.patch.object(VersionManager, "CURRENT_VERSION", current):
        return VersionManager().check_for_updates()


class NotesSpanTest(unittest.TestCase):
    def test_the_notes_cover_every_version_that_was_skipped(self):
        info = check([release("1.3.0", "3 の話"),
                      release("1.2.0", "2 の話"),
                      release("1.1.0", "1 の話")], current="1.0.0")
        notes = info["release_notes"]
        for body in ("3 の話", "2 の話", "1 の話"):
            self.assertIn(body, notes)
        # 新しいものから並べる
        self.assertLess(notes.index("3 の話"), notes.index("1 の話"))

    def test_versions_already_installed_are_left_out(self):
        info = check([release("1.3.0", "3 の話"),
                      release("1.2.0", "2 の話"),
                      release("1.1.0", "1 の話")], current="1.2.0")
        notes = info["release_notes"]
        self.assertIn("3 の話", notes)
        self.assertNotIn("2 の話", notes)
        self.assertNotIn("1 の話", notes)

    def test_each_block_says_which_version_it_is(self):
        info = check([release("1.3.0", "3 の話"),
                      release("1.2.0", "2 の話")], current="1.1.0")
        self.assertIn("# v1.3.0", info["release_notes"])
        self.assertIn("# v1.2.0", info["release_notes"])

    def test_too_many_generations_are_capped(self):
        from core.version_manager import VersionManager
        many = [release("1.%d.0" % n, "%d の話" % n)
                for n in range(9, 0, -1)]
        info = check(many, current="1.0.0")
        notes = info["release_notes"]
        shown = sum(1 for n in range(1, 10) if ("# v1.%d.0" % n) in notes)
        self.assertEqual(shown, VersionManager.MAX_NOTES_GENERATIONS)
        self.assertIn("省略", notes)


class ReleaseSelectionTest(unittest.TestCase):
    def test_drafts_and_prereleases_are_skipped(self):
        info = check([release("2.0.0", "下書き", draft=True),
                      release("1.9.0", "事前公開", prerelease=True),
                      release("1.3.0", "本物")], current="1.0.0")
        self.assertEqual(info["version"], "1.3.0")
        self.assertNotIn("下書き", info["release_notes"])
        self.assertNotIn("事前公開", info["release_notes"])

    def test_the_newest_wins_even_if_the_list_is_out_of_order(self):
        info = check([release("1.1.0", "1 の話"),
                      release("1.3.0", "3 の話"),
                      release("1.2.0", "2 の話")], current="1.0.0")
        self.assertEqual(info["version"], "1.3.0")

    def test_the_download_still_comes_from_the_newest_release(self):
        info = check([release("1.3.0"), release("1.2.0")], current="1.0.0")
        self.assertIn("1.3.0", info["download_url"])
        self.assertIn("1.3.0", info["sha256_url"])

    def test_an_empty_list_is_not_an_update(self):
        self.assertIsNone(check([], current="1.0.0"))

    def test_a_single_release_object_still_works(self):
        """API が 1 件だけの辞書を返しても壊れないこと。"""
        info = check(release("1.3.0", "3 の話"), current="1.0.0")
        self.assertEqual(info["version"], "1.3.0")
        self.assertIn("3 の話", info["release_notes"])

    def test_nothing_newer_means_no_update(self):
        info = check([release("1.0.0", "同じ")], current="1.0.0")
        self.assertFalse(info["available"])


class DialogDisplayTest(unittest.TestCase):
    """通知として実際に何が見えるか。"""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def shown(self, releases, current="1.1.0"):
        from ui.dialogs.update_dialog import UpdateDialog
        info = check(releases, current=current)
        # ダイアログを変数で持つ。手放すと Qt 側が中身ごと片付けてしまう
        self.dialog = UpdateDialog(None, info)
        return self.dialog.notes_text.toPlainText()

    def test_every_skipped_version_reaches_the_dialog(self):
        text = self.shown([release("1.3.0", "### 追加\n- 機能C"),
                           release("1.2.0", "### 追加\n- 機能B"),
                           release("1.1.1", "### 修正\n- 不具合A")])
        for word in ("1.3.0", "機能C", "1.2.0", "機能B", "1.1.1", "不具合A"):
            self.assertIn(word, text)

    def test_markdown_is_rendered_not_shown_as_symbols(self):
        """見出しや箇条書きの記号が、そのまま並ばないこと。"""
        text = self.shown([release("1.3.0", "### 追加\n- 機能C")])
        self.assertNotIn("###", text)
        self.assertNotIn("# v1.3.0", text)
        self.assertIn("機能C", text)

    def test_an_empty_body_does_not_leave_the_box_blank(self):
        from ui.dialogs.update_dialog import UpdateDialog
        info = check([release("1.3.0", "")], current="1.1.0")
        info["release_notes"] = ""
        self.dialog = UpdateDialog(None, info)
        self.assertIn("ありません", self.dialog.notes_text.toPlainText())


if __name__ == "__main__":
    unittest.main()
