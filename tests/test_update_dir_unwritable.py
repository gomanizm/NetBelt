"""更新キャッシュ用ディレクトリを作れなくても、アプリが起動できること。

実測（release #4）: VersionManager.__init__ の
`os.makedirs(self.UPDATE_DIR, exist_ok=True)` は例外をそのまま外へ出す。
起動時の未適用更新チェック（MainWindow._check_pending_updates）は同期で
VersionManager() を作るだけで try/except が無いため、%TEMP%\\NetBeltUpdates が
通常ファイルになっている、あるいは %TEMP% に作成権限が無いだけで、
更新機能ではなくアプリ全体が起動できなくなる。

更新が使えなくなるのは避けられないが、それは更新機能の中だけで完結させる。
"""
import io
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, "src")


class UpdateDirUnusableTest(unittest.TestCase):
    """UPDATE_DIR を作れない状態でも、例外を外へ出さないこと。"""

    def setUp(self):
        # ディレクトリと同じ名前の通常ファイルを置く。makedirs は
        # exist_ok=True でも FileExistsError になる。
        base = tempfile.mkdtemp(prefix="netbelt-updatedir-")
        self.blocked = os.path.join(base, "NetBeltUpdates")
        io.open(self.blocked, "w", encoding="ascii").write("not a directory")

        from core.version_manager import VersionManager
        patch = unittest.mock.patch.object(
            VersionManager, "UPDATE_DIR", self.blocked)
        patch.start()
        self.addCleanup(patch.stop)

    def _manager(self):
        from core.version_manager import VersionManager
        return VersionManager()

    def test_constructing_the_manager_does_not_raise(self):
        """起動経路が握っていない例外を、初期化から出さないこと。"""
        try:
            self._manager()
        except Exception as e:
            self.fail("更新ディレクトリを作れないだけで %s が起動を止めた: %s"
                      % (type(e).__name__, e))

    def test_pending_updates_is_empty(self):
        """未適用の更新は「無し」として扱うこと。"""
        self.assertEqual(self._manager().get_pending_update_files(), [])

    def test_cleanup_does_not_raise(self):
        """古い更新の掃除も、例外を外へ出さないこと。"""
        self.assertEqual(self._manager().cleanup_old_updates(24), 0)

    def test_download_returns_none(self):
        """ダウンロードは失敗として戻ること（保存先が作れないので当然）。"""
        class _Resp:
            status_code = 200
            headers = {"content-length": "3"}

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size=8192):
                yield b"abc"

            def close(self):
                pass

        with unittest.mock.patch("core.version_manager.requests.get",
                                 return_value=_Resp()):
            self.assertIsNone(
                self._manager().download_update("https://example.com/a.zip"))


class UpdateDirNormalCaseTest(unittest.TestCase):
    """作れる場合は、これまでどおり作ること。"""

    def test_directory_is_created(self):
        from core.version_manager import VersionManager

        target = os.path.join(tempfile.mkdtemp(prefix="netbelt-updatedir-"),
                              "NetBeltUpdates")
        with unittest.mock.patch.object(VersionManager, "UPDATE_DIR", target):
            VersionManager()
        self.assertTrue(os.path.isdir(target), "更新用ディレクトリを作っていない")


if __name__ == "__main__":
    unittest.main()
