"""ConfigManager のサーバー設定(root/port等)永続化を確認する回帰テスト。"""
import io, os, tempfile, unittest, sys
sys.path.insert(0, "src")
from core.config_manager import ConfigManager

class ServerSettingsTest(unittest.TestCase):
    def test_persist_roundtrip(self):
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, "config.json")
        cm = ConfigManager(config_path=cfg)
        cm.set_server_settings("tftp_server", {"root_directory": "C:/tftp", "port": 69})
        cm2 = ConfigManager(config_path=cfg)
        self.assertEqual(cm2.get_server_settings("tftp_server").get("root_directory"), "C:/tftp")
        self.assertEqual(cm2.get_server_settings("tftp_server").get("port"), 69)


class BrokenConfigSelfHealsTest(unittest.TestCase):
    """破損した config.json は「バックアップしてから作り直す」のが仕様。

    起動時のエラーダイアログが「新しい設定を保存すると config.json が
    再作成されます」と明示しているので、保存を拒否してはいけない。
    拒否すると復旧手段が無いまま読み取り専用になる。
    """

    def _broken_config(self):
        d = tempfile.mkdtemp(prefix="netbelt-broken-")
        path = os.path.join(d, "config.json")
        with io.open(path, "w", encoding="utf-8") as f:
            f.write('{"groups": [ THIS IS NOT JSON')
        return path

    def test_load_error_is_recorded_and_the_file_is_backed_up(self):
        path = self._broken_config()
        cm = ConfigManager(config_path=path)
        self.assertTrue(cm.load_error, "読み込み失敗が記録されていない")
        self.assertTrue(cm.backup_path, "バックアップが作られていない")
        self.assertTrue(os.path.exists(cm.backup_path))
        self.assertIn("THIS IS NOT JSON",
                      io.open(cm.backup_path, encoding="utf-8").read(),
                      "バックアップに元の内容が残っていない")

    def test_saving_recreates_the_config(self):
        """壊れたまま読み取り専用にせず、保存できること。"""
        path = self._broken_config()
        cm = ConfigManager(config_path=path)
        self.assertTrue(cm.save_config(), "保存できないと復旧手段が無くなる")
        self.assertTrue(cm.set_server_settings("terminal", {"font_size": 20}))

        reloaded = ConfigManager(config_path=path)
        self.assertIsNone(reloaded.load_error, "作り直した設定が読めない")
        self.assertEqual(reloaded.get_server_settings("terminal")["font_size"], 20)



class BrokenSettingTypesTest(unittest.TestCase):
    """手編集で型が壊れていても落ちないこと。

    設定を「読む」側は正規化していたが、ConfigManager の境界を素通しに
    していたため、settings.terminal が文字列だと保存で AttributeError、
    check_on_startup が "yes" だと設定ダイアログが TypeError で開けなかった。
    """

    def _manager_with(self, patch):
        d = tempfile.mkdtemp(prefix="netbelt-badtype-")
        path = os.path.join(d, "config.json")
        cm = ConfigManager(config_path=path)
        cm.config.update(patch)
        cm.save_config()
        return ConfigManager(config_path=path)

    def test_saving_survives_a_non_dict_section(self):
        for broken in ("broken", [], 5, None):
            with self.subTest(terminal=broken):
                cm = self._manager_with({"settings": {"terminal": broken}})
                self.assertEqual(cm.get_server_settings("terminal"), {})
                self.assertTrue(cm.set_server_settings("terminal", {"font_size": 12}))
                self.assertEqual(
                    cm.get_server_settings("terminal")["font_size"], 12)

    def test_saving_survives_a_non_dict_settings_root(self):
        cm = self._manager_with({"settings": "broken"})
        self.assertEqual(cm.get_server_settings("terminal"), {})
        self.assertTrue(cm.set_server_settings("terminal", {"font_size": 12}))

    def test_check_on_startup_is_always_a_bool(self):
        for broken in ("yes", None, 1, [], {}):
            with self.subTest(check_on_startup=broken):
                cm = self._manager_with(
                    {"update_settings": {"check_on_startup": broken}})
                value = cm.get_check_on_startup()
                self.assertIsInstance(value, bool)
                self.assertTrue(value, "壊れた値は既定（True）へ倒す")

    def test_real_booleans_are_kept(self):
        for kept in (True, False):
            with self.subTest(check_on_startup=kept):
                cm = self._manager_with(
                    {"update_settings": {"check_on_startup": kept}})
                self.assertIs(cm.get_check_on_startup(), kept)

    def test_skipped_version_is_a_string_or_none(self):
        for broken in (123, [], {}, ""):
            with self.subTest(skipped_version=broken):
                cm = self._manager_with(
                    {"update_settings": {"skipped_version": broken}})
                self.assertIsNone(cm.get_skipped_version())

        cm = self._manager_with({"update_settings": {"skipped_version": "1.2.3"}})
        self.assertEqual(cm.get_skipped_version(), "1.2.3")

    def test_a_non_dict_update_settings_does_not_break(self):
        cm = self._manager_with({"update_settings": "broken"})
        self.assertIs(cm.get_check_on_startup(), True)
        self.assertTrue(cm.set_check_on_startup(False))
        self.assertIs(cm.get_check_on_startup(), False)

if __name__ == "__main__":
    unittest.main()
