"""起動元にある他アプリの config.json を、NetBelt が書き換え・削除しないことを検証する。

何が起きていたか（基準 9fee4af で実測、R08 の検査役）: ConfigManager の既定の
保存先は作業ディレクトリからの相対の "config.json" なので、exe と違うフォルダ
から起動すると、そのフォルダにある config.json を読む。それが他アプリの JSON
でも次のようになっていた。
  - 最上位が dict で groups が無い: コンストラクタが Default グループを足して
    バックアップ無しで保存する。書式が変わるだけでなく、1e400 が Infinity に
    なり、重複キーの片方が消え、settings.ftp_server.password のような平文が
    DPAPI の暗号文に置き換わる。
  - 最上位がリスト: 破損扱いでバックアップを取り、その整理
    （_cleanup_old_backups）が起動元の config.json.backup_* を新しい 5 件だけ
    残して消す。NetBelt が作っていないファイルも消える。次の保存で本体は
    NetBelt の設定に置き換わる。
どの config.json を読んでいるかを利用者が確かめる手段も無く、読み込みエラーの
ダイアログに出るバックアップの場所も相対パスだった。

直し方: 「NetBelt の形でない」JSON（最上位が dict でない、または groups が無く
NetBelt が書かないキーを含む dict）は読み込まず、書かず、バックアップも
取らない。以後の save_config も失敗として返す。起動時の警告に絶対パスを出す。
{} や NetBelt のキーだけの手書きの設定、groups の形が崩れた設定、JSON として
読めないファイルは、今までどおり自己修復する（2026-08-24 の決定）。
設定ファイルの絶対パスはバージョン情報に 1 行出し、バックアップの場所は
絶対パスで知らせる。
"""
import html
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

# 他アプリの dict。NetBelt が書き戻すと変わってしまう要素をわざと入れる
# （LF の改行・Infinity になる数・重複キー・平文のパスワード）
FOREIGN_DICT = (b'{\n'
                b'  "other_application_setting": "keep-me",\n'
                b'  "huge": 1e400,\n'
                b'  "dup": 1,\n'
                b'  "dup": 2,\n'
                b'  "settings": {"ftp_server": {"password": "plain-secret"}}\n'
                b'}\n')
FOREIGN_LIST = b'[{"name": "not-a-netbelt-group"}, 1, 2]\n'

# 他人が起動元に置いている、NetBelt と同じ名前の形のファイル（7 件。
# 整理は新しい 5 件だけ残すので、走れば古い 2 件が消える）
OTHERS_BACKUPS = ["config.json.backup_2020010%d_000000" % i
                  for i in range(1, 8)]

NETBELT_CONFIG = {
    "config_version": "1.0",
    "groups": [{"name": "Default", "auto_commands": [], "devices": []}],
    "global_macros": [],
    "settings": {},
    "update_settings": {"check_on_startup": False},
}


def _snapshot(directory):
    """フォルダの中身（名前 -> バイト列）"""
    result = {}
    for name in os.listdir(directory):
        with open(os.path.join(directory, name), "rb") as f:
            result[name] = f.read()
    return result


class _TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-foreign-")
        self.path = os.path.join(self.dir, "config.json")
        home = mock.patch(
            "core.config_manager.app_data_dir",
            return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)

    def _write_bytes(self, data):
        with open(self.path, "wb") as f:
            f.write(data)

    def _write_json(self, config):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False)

    def _on_disk(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _backups(self):
        return [n for n in os.listdir(self.dir)
                if n.startswith("config.json.backup_")]


class ForeignJsonIsLeftAloneTest(_TempDirTestCase):
    """他アプリの JSON は、構築でも保存でもバイト単位で変わらない"""

    def _assert_left_alone(self, data):
        from core.config_manager import ConfigManager
        self._write_bytes(data)
        for name in OTHERS_BACKUPS:
            with open(os.path.join(self.dir, name), "wb") as f:
                f.write(b"someone else's backup " + name.encode())
        before = _snapshot(self.dir)

        cm = ConfigManager(config_path=self.path)

        self.assertEqual(_snapshot(self.dir), before,
                         "構築しただけで起動元のファイルが変わった")
        self.assertIsNone(cm.backup_path, "他アプリのファイルをバックアップした")
        self.assertIsNone(cm.load_error,
                          "破損扱い（次の保存で作り直す）にしてはいけない")
        self.assertTrue(cm.load_warning, "知らせていない")
        self.assertIn(os.path.abspath(self.path), cm.load_warning,
                      "警告に絶対パスが無い: %r" % cm.load_warning)
        self.assertIn("NetBelt の設定ファイルではない", cm.load_warning)
        # 画面は既定の設定で動く（グループ一覧が引ける）
        self.assertIsInstance(cm.get_groups(), list)

        self.assertFalse(cm.save_config(), "保存できたことにしてはいけない")
        self.assertFalse(cm.add_group("Lab"))
        self.assertTrue(cm.last_save_failed,
                        "保存の失敗として案内できない（同名などと区別できない）")
        cm.set_last_save_dir(self.dir)
        self.assertEqual(_snapshot(self.dir), before,
                         "保存で起動元のファイルが変わった")

    def test_a_foreign_dict_is_never_written(self):
        self._assert_left_alone(FOREIGN_DICT)

    def test_a_foreign_list_is_never_written_or_backed_up(self):
        self._assert_left_alone(FOREIGN_LIST)

    def test_a_foreign_scalar_is_never_written(self):
        self._assert_left_alone(b'"just a string"\n')

    def test_a_path_the_stdout_cannot_encode_is_still_left_alone(self):
        """ログの出力先がパスを表せなくても、破損扱い（バックアップ）にしない

        ソースから起動して標準出力をファイルやパイプへ向けると、符号化は
        ロケールのもの（cp932 など）になる。パスに表せない文字（é）があると
        ログの print が例外になり、_load_config の except で破損扱い
        （load_error と他アプリのファイルのバックアップ）へ落ちていた。
        """
        self.dir = os.path.join(self.dir, "caf" + chr(0xE9))
        os.makedirs(self.dir)
        self.path = os.path.join(self.dir, "config.json")
        narrow_stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp932",
                                         errors="strict")
        with mock.patch("sys.stdout", narrow_stdout):
            self._assert_left_alone(FOREIGN_DICT)

    def test_the_default_config_path_is_treated_the_same(self):
        """引数なしの ConfigManager()（MainWindow・パネル・更新ダイアログ）も同じ"""
        from core.config_manager import ConfigManager
        self._write_bytes(FOREIGN_DICT)
        before = _snapshot(self.dir)
        prev = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, prev)
        expected = os.path.abspath("config.json")

        cm = ConfigManager()

        self.assertIn(expected, cm.load_warning or "")
        self.assertFalse(cm.save_config())
        self.assertEqual(_snapshot(self.dir), before)


class NetBeltConfigsStillSelfRepairTest(_TempDirTestCase):
    """NetBelt 自身の設定は今までどおり（自己修復・警告なし）"""

    def test_an_empty_dict_gets_the_default_group_saved(self):
        from core.config_manager import ConfigManager
        self._write_bytes(b"{}")

        cm = ConfigManager(config_path=self.path)

        self.assertIsNone(cm.load_warning)
        self.assertIsNone(cm.load_error)
        self.assertEqual([g["name"] for g in self._on_disk()["groups"]],
                         ["Default"])
        self.assertEqual(self._backups(), [])

    def test_a_hand_written_config_with_only_netbelt_keys_is_repaired(self):
        from core.config_manager import ConfigManager
        self._write_json({"settings": {"terminal": {"font_size": 12}},
                          "update_settings": {"check_on_startup": False},
                          "global_macros": [], "config_version": "1.0"})

        cm = ConfigManager(config_path=self.path)

        self.assertIsNone(cm.load_warning)
        on_disk = self._on_disk()
        self.assertEqual([g["name"] for g in on_disk["groups"]], ["Default"])
        self.assertEqual(on_disk["settings"]["terminal"]["font_size"], 12)
        self.assertTrue(cm.save_config())

    def test_a_netbelt_config_with_broken_groups_is_backed_up_and_repaired(self):
        from core.config_manager import ConfigManager
        config = dict(NETBELT_CONFIG, groups="not-a-list", extra_key=1)
        self._write_json(config)

        cm = ConfigManager(config_path=self.path)

        self.assertTrue(cm.backup_path and os.path.exists(cm.backup_path))
        self.assertIsInstance(self._on_disk()["groups"], list)
        self.assertNotIn("NetBelt の設定ファイルではない", cm.load_warning or "")

    def test_a_broken_json_is_backed_up_and_rebuilt(self):
        from core.config_manager import ConfigManager
        self._write_bytes(b'{"groups": [')

        cm = ConfigManager(config_path=self.path)

        self.assertTrue(cm.load_error)
        self.assertTrue(cm.backup_path and os.path.exists(cm.backup_path))
        self.assertTrue(cm.save_config(), "次の保存で作り直せない")
        self.assertIsInstance(self._on_disk()["groups"], list)

    def test_a_normal_config_loads_without_a_warning(self):
        from core.config_manager import ConfigManager
        self._write_json(dict(NETBELT_CONFIG, unknown_but_has_groups=True))

        cm = ConfigManager(config_path=self.path)

        self.assertIsNone(cm.load_warning)
        self.assertIsNone(cm.load_error)
        self.assertTrue(cm.add_group("Lab"))
        self.assertIn("Lab", [g["name"] for g in self._on_disk()["groups"]])


class ConfigPathIsShownTest(_TempDirTestCase):
    """設定ファイルとバックアップの場所を、絶対パスで知らせる"""

    def _chdir(self):
        prev = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, prev)

    def test_the_config_manager_reports_the_absolute_path(self):
        from core.config_manager import ConfigManager
        self._write_json(NETBELT_CONFIG)
        self._chdir()

        cm = ConfigManager("config.json")

        self.assertEqual(cm.config_file_path(), os.path.abspath("config.json"))

    def test_the_backup_path_is_absolute_even_for_a_relative_config_path(self):
        from core.config_manager import ConfigManager
        self._write_bytes(b"{broken")
        self._chdir()

        cm = ConfigManager("config.json")

        self.assertTrue(cm.backup_path)
        self.assertTrue(os.path.isabs(cm.backup_path), cm.backup_path)
        self.assertTrue(os.path.exists(cm.backup_path))

    def test_the_quarantine_warning_carries_the_absolute_backup_path(self):
        from core.config_manager import ConfigManager
        config = dict(NETBELT_CONFIG)
        config["groups"] = [{"name": "Default", "auto_commands": [],
                             "devices": [{"host": "192.0.2.1"}]}]
        self._write_json(config)
        self._chdir()

        cm = ConfigManager("config.json")

        self.assertTrue(os.path.isabs(cm.backup_path), cm.backup_path)
        self.assertIn(cm.backup_path, cm.load_warning)


class MainWindowShowsTheConfigPathTest(unittest.TestCase):
    _windows = []   # 配送待ちシグナルの宛先を先に解放しない（他テストと同じ理由）

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        cls._windows.clear()

    def setUp(self):
        # MainWindow は CWD の config.json を読むので、一時ディレクトリへ移る。
        # HTML として書き出す場所に効くよう、名前に & を入れる
        self.dir = tempfile.mkdtemp(prefix="netbelt-a&b-")
        self.path = os.path.join(self.dir, "config.json")
        self.prev_cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, self.prev_cwd)
        home = mock.patch(
            "core.config_manager.app_data_dir",
            return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)

    def _window(self):
        from ui.main_window import MainWindow
        with mock.patch("PyQt6.QtWidgets.QMessageBox.warning") as warning:
            w = MainWindow()
        self._windows.append(w)
        return w, warning

    def test_the_version_info_shows_the_config_path(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(NETBELT_CONFIG, f)
        w, warning = self._window()
        self.assertEqual(warning.call_count, 0,
                         "普段の起動で新しいモーダルを出してはいけない: %r"
                         % warning.call_args_list)

        with mock.patch("ui.main_window.QMessageBox") as box:
            w._on_version_info()

        text = box.return_value.setText.call_args[0][0]
        self.assertIn(html.escape(os.path.abspath("config.json")), text)

    def test_the_load_error_dialog_shows_the_absolute_backup_path(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{broken")
        w, warning = self._window()

        self.assertEqual(warning.call_count, 1, warning.call_args_list)
        backup = w.config_manager.backup_path
        self.assertTrue(backup and os.path.isabs(backup), backup)
        self.assertIn(backup, warning.call_args.args[2])

    def test_a_foreign_json_is_reported_once_and_left_alone(self):
        with open(self.path, "wb") as f:
            f.write(FOREIGN_DICT)
        w, warning = self._window()

        self.assertEqual(warning.call_count, 1, warning.call_args_list)
        self.assertIn(os.path.abspath("config.json"), warning.call_args.args[2])
        w._save_layout()   # 閉じるときと同じ保存
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), FOREIGN_DICT)


if __name__ == "__main__":
    unittest.main()
