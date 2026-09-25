"""テスト基盤（tests/conftest.py）の後始末の検証。

一時フォルダ（tests-01）
  mkdtemp の後始末を書いていないテストが 285 ファイルあり、書き忘れはそのまま
  実物の %TEMP% に残っていた。実測（441ea02、TEMP を空のフォルダへ向けて流した）:
  test_sftp_client_settings.py と test_config_invalid_devices.py の 2 ファイル
  （27 件）だけで 41 個のフォルダ（netbelt-sftpset- 25、netbelt-testhome- 8 ほか）
  が残った。全件を 1 回流すと 1,300〜1,700 個残り、%TEMP% には約 78 万個たまって
  いた。MainWindow を作るテストは、インストール済みの NetBelt と共用の
  %TEMP%\\NetBeltUpdates にも触っていた。
  1 つずつ直すと数百行になるので、conftest でまとめて片付ける。セッションの頭で
  %TEMP% の下に netbelt-tests-* を 1 つ作り、tempfile.tempdir と環境変数
  TEMP / TMP / TMPDIR をそこへ向ける（子プロセスも同じ場所を使う）。終わりに
  元へ戻してから、そのフォルダを丸ごと消す。すでに %TEMP% にある物には触らない。
"""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, "src")


class TempIsASessionFolderTest(unittest.TestCase):

    def _assert_session_folder(self, path, what):
        """path がこのセッション用のフォルダ（%TEMP%\\netbelt-tests-*）であること"""
        self.assertTrue(os.path.basename(path).startswith("netbelt-tests-"),
                        "%sがセッション用のフォルダではない: %s" % (what, path))
        self.assertEqual(os.path.normcase(path),
                         os.path.normcase(tempfile.gettempdir()), what)

    def test_temp_is_a_folder_of_this_session(self):
        self._assert_session_folder(tempfile.gettempdir(), "一時フォルダ")
        for key in ("TEMP", "TMP", "TMPDIR"):
            self._assert_session_folder(os.environ.get(key, ""), key)

        made = tempfile.mkdtemp(prefix="netbelt-hygiene-")
        try:
            self._assert_session_folder(os.path.dirname(made), "mkdtemp の置き場")
        finally:
            os.rmdir(made)

    def test_child_processes_use_the_same_folder(self):
        proc = subprocess.run(
            [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_session_folder(proc.stdout.strip(), "子プロセスの一時フォルダ")

    def test_the_update_folder_is_not_the_shared_one(self):
        # 更新フォルダ（%TEMP%\NetBeltUpdates）はインストール済みの NetBelt と共用
        from core.version_manager import VersionManager
        self._assert_session_folder(os.path.dirname(VersionManager.UPDATE_DIR),
                                    "更新フォルダの置き場")

    def test_the_folder_and_its_contents_are_removed_at_the_end(self):
        # conftest が終わりに使う手順を、入れ子のフォルダで確かめる
        from conftest import _enter_temp_session, _leave_temp_session
        outer = tempfile.gettempdir()
        state = _enter_temp_session()
        try:
            inner = tempfile.gettempdir()
            self.assertEqual(os.path.dirname(inner), outer)
            left = tempfile.mkdtemp(prefix="netbelt-left-")
            with open(os.path.join(left, "file.txt"), "w") as f:
                f.write("x")
        finally:
            _leave_temp_session(state)

        self.assertFalse(os.path.exists(inner), "セッション用のフォルダが残った")
        self.assertEqual(tempfile.gettempdir(), outer, "tempfile.tempdir が戻らない")
        for key in ("TEMP", "TMP", "TMPDIR"):
            self.assertEqual(os.environ.get(key), outer, key)


if __name__ == "__main__":
    unittest.main()
