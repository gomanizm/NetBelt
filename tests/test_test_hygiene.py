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
  %TEMP%（8.3 の短い名前があればそちら）の下に nbt-* を 1 つ作り、
  tempfile.tempdir と環境変数 TEMP / TMP / TMPDIR をそこへ向ける（子プロセスも
  同じ場所を使う）。終わりに
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
  テストが自分の一時フォルダへ移っている（chdir）間は、これまでどおり移った先の
  config.json にする（そこに置いた設定を窓に読ませるテストがある）。
  子プロセスのテストには作業ディレクトリを渡す。差し替えは子プロセスには効かない
  ので、pytest を起動した場所の config.json がテストの間に作られた・書き換わった・
  消えたら、そのテストを落とす（渡し忘れを捕まえる）。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import pytest

sys.path.insert(0, "src")


class TempIsASessionFolderTest(unittest.TestCase):

    def _assert_session_folder(self, path, what):
        """path がこのセッション用のフォルダ（%TEMP%\\nbt-*）であること"""
        self.assertTrue(os.path.basename(path).startswith("nbt-"),
                        "%sがセッション用のフォルダではない: %s" % (what, path))
        self.assertEqual(os.path.normcase(path),
                         os.path.normcase(tempfile.gettempdir()), what)

    def test_the_folder_name_is_short(self):
        # 更新のテストは TEMP の下へさらに掘り、updater.bat の xcopy は 260 文字を
        # 超えるファイルを黙って飛ばす（upd-04）。フォルダ名の分だけ、TEMP を
        # 短くしないと落ちるようになる。実測: 141 文字の TEMP で、netbelt-tests-*
        # （23 文字足す）では test_updater_old_script_new_payload の 1 件が落ちた
        self.assertLessEqual(len(os.path.basename(tempfile.gettempdir())), 12)

    def test_the_folder_is_made_under_the_short_name_of_temp(self):
        # フォルダ名を短くしても、TEMP が 13 文字長くなる。実測: 143 文字の TEMP
        # （作業フォルダの tmp）で、test_updater_temp_brackets.py の 2 件が落ちた
        # （セッションのフォルダが無ければ通る）。TEMP に 8.3 の短い名前があれば、
        # その下に作る（GitHub のランナーの TEMP も C:\Users\RUNNER~1\... の形）
        from conftest import _enter_temp_session, _leave_temp_session, _short_path
        long_dir = tempfile.mkdtemp(prefix="netbelt-hygiene-a-long-folder-name-")
        self.addCleanup(shutil.rmtree, long_dir, True)
        short_dir = _short_path(long_dir)
        if short_dir == long_dir:
            self.skipTest("このボリュームは 8.3 の短い名前を作らない")
        self.assertLess(len(short_dir), len(long_dir))
        self.assertTrue(os.path.samefile(short_dir, long_dir))

        saved = tempfile.tempdir
        tempfile.tempdir = long_dir
        try:
            state = _enter_temp_session()
            try:
                inner = tempfile.gettempdir()
                for key in ("TEMP", "TMP", "TMPDIR"):
                    self.assertEqual(os.environ.get(key), inner, key)
            finally:
                _leave_temp_session(state)
        finally:
            tempfile.tempdir = saved

        self.assertEqual(os.path.dirname(inner), short_dir)
        self.assertFalse(os.path.exists(inner), "セッション用のフォルダが残った")

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
            # 短い名前の下に作るので、綴りではなく場所で比べる
            self.assertTrue(os.path.samefile(os.path.dirname(inner), outer),
                            (inner, outer))
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
        # 両辺を realpath で長い名前へそろえてから比べる。GitHub のランナーの
        # TEMP は 8.3 の短い名前（C:\Users\RUNNER~1\...）で、tempfile は短い
        # 名前のまま、pytest の tmp_path_factory は長い名前へ直した場所を返す
        def real(p):
            return os.path.normcase(os.path.realpath(str(p)))
        used = real(path)
        self.assertNotEqual(used, real("config.json"),
                            "作業ディレクトリの config.json を使っている")
        self.assertTrue(used.startswith(real(tempfile.gettempdir()) + os.sep),
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

    def test_a_test_that_moves_to_its_own_folder_reads_the_config_there(self):
        # 自分の一時フォルダへ移り、そこに置いた config.json を窓に読ませる
        # テストがある（test_config_invalid_devices.py など）。
        # 移った先では、これまでどおり作業ディレクトリの config.json を使う
        from core.config_manager import ConfigManager
        work = tempfile.mkdtemp(prefix="netbelt-hygiene-cwd-")
        prev = os.getcwd()
        os.chdir(work)
        self.addCleanup(os.chdir, prev)

        used = ConfigManager().config_path

        self.assertEqual(os.path.normcase(os.path.realpath(str(used))),
                         os.path.normcase(os.path.realpath(
                             os.path.join(work, "config.json"))))


class WorkingDirectoryConfigIsLeftAloneTest(unittest.TestCase):
    """pytest を起動した場所の config.json に触ったテストを落とす見張り。

    既定の設定の差し替えは子プロセスには効かない。子プロセスで窓を作る
    test_window_close_waits_for_mib_loader.py から cwd を渡す 1 行を外すと、
    テストは通ったまま作業ディレクトリに config.json（2015 バイト）ができた。
    """

    def test_a_created_changed_or_removed_file_is_reported(self):
        from conftest import _what_happened
        self.assertIsNone(_what_happened(None, None))
        self.assertIsNone(_what_happened((10, 1), (10, 1)))
        self.assertEqual(_what_happened(None, (10, 1)), "作られた")
        self.assertEqual(_what_happened((10, 1), None), "消えた")
        self.assertEqual(_what_happened((10, 1), (10, 2)), "書き換わった")
        self.assertEqual(_what_happened((10, 1), (11, 1)), "書き換わった")

    def test_the_mark_tells_a_missing_file_and_a_rewrite(self):
        from conftest import _file_mark
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-hygiene-mark-"),
                            "config.json")
        self.assertIsNone(_file_mark(path))
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        before = _file_mark(path)
        self.assertIsNotNone(before)
        os.utime(path, ns=(0, before[1] + 10**9))
        self.assertNotEqual(_file_mark(path), before)


class WorkingDirectoryConfigWatchIsOnTest(unittest.TestCase):
    """見張り（conftest の working_directory_config_left_alone）が、頼んでいない
    テストにも効いていることの検証。

    何が起きていたか（ebbe593 で実測）
      上の 2 件が確かめているのは補助関数 _what_happened と _file_mark だけで、
      見張りが autouse で有効なことは、どのテストも確かめていなかった。
      conftest の autouse=True を外しても、このファイルの 11 件はすべて通った。
      1 周目の検査役の実測では、そのうえで子プロセスへ作業ディレクトリを渡す
      1 行（cwd=work）も外すと、作業ディレクトリに config.json ができても、
      どのテストも落ちなかった。
    どう直したか
      このリポジトリのテストの大半は unittest.TestCase なので、その形のテストで
      pytest の request.fixturenames に見張りが入っていることを確かめる。
    """

    @pytest.fixture(autouse=True)
    def _grab_fixturenames(self, request):
        self._fixturenames = request.fixturenames

    def test_the_watch_is_on_for_a_test_that_does_not_ask_for_it(self):
        names = getattr(self, "_fixturenames", None)
        self.assertIsNotNone(names, "pytest の fixture を受け取れていない")
        self.assertIn("working_directory_config_left_alone", names,
                      "作業ディレクトリの config.json の見張りが効いていない")


if __name__ == "__main__":
    unittest.main()
