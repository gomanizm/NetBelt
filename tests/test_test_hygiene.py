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

既定の設定ファイル（tests-03）
  ConfigManager() の既定の config_path は作業ディレクトリからの相対の
  "config.json" で、MainWindow・FTP/TFTP パネル・更新ダイアログは引数なしで作る。
  ConfigManager を差し替えずに MainWindow() を作るテストは、リポジトリ直下の
  config.json（ソースから起動したときの利用者の設定）を読み書きしていた。
  実測（441ea02）: config.json の無い作業ディレクトリで test_updater_script.py の
  UpdaterLaunchTest を流すと config.json ができた。同じことをするファイルは
  ほかに 31 あった。子プロセスで窓を作る test_window_close_waits_for_mib_loader.py
  も同じ。
  直し方: conftest で、セッションの間だけ ConfigManager.__init__ の既定値を
  セッションの一時フォルダの config.json へ差し替える（クラスは差し替えない）。
  子プロセスのテストには作業ディレクトリを渡す。
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


class DefaultConfigIsNotInTheWorkingDirectoryTest(unittest.TestCase):

    def _assert_not_in_the_working_directory(self, path):
        used = os.path.normcase(os.path.abspath(str(path)))
        self.assertNotEqual(used, os.path.normcase(os.path.abspath("config.json")),
                            "作業ディレクトリの config.json を使っている")
        self.assertTrue(used.startswith(os.path.normcase(tempfile.gettempdir())),
                        "既定の設定がセッションの一時フォルダの外にある: %s" % used)

    def test_a_config_manager_without_a_path(self):
        from core.config_manager import ConfigManager
        self._assert_not_in_the_working_directory(ConfigManager().config_path)

    def test_a_window_does_not_use_the_working_directory_config(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        from ui.main_window import MainWindow

        window = MainWindow()
        self.addCleanup(window.close)

        self._assert_not_in_the_working_directory(window.config_manager.config_path)


if __name__ == "__main__":
    unittest.main()
