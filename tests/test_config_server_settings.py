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


if __name__ == "__main__":
    unittest.main()
