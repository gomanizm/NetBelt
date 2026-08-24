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


class SaveRefusedWhenConfigIsBrokenTest(unittest.TestCase):
    """読み込みに失敗した状態で保存しないこと。

    破損扱いになった config.json をデフォルト設定で上書きすると、
    機器リストが消えたように見える。終了時のレイアウト保存など
    ユーザーが意識しない経路からも save_config() は呼ばれる。
    """

    def _broken_config(self):
        import os, tempfile
        d = tempfile.mkdtemp(prefix="netbelt-broken-")
        path = os.path.join(d, "config.json")
        with io.open(path, "w", encoding="utf-8") as f:
            f.write('{"groups": [ THIS IS NOT JSON')
        return path

    def test_load_error_is_recorded(self):
        from core.config_manager import ConfigManager
        cm = ConfigManager(config_path=self._broken_config())
        self.assertTrue(cm.load_error, "読み込み失敗が記録されていない")

    def test_save_is_refused_and_the_file_is_untouched(self):
        from core.config_manager import ConfigManager
        path = self._broken_config()
        before = io.open(path, encoding="utf-8").read()

        cm = ConfigManager(config_path=path)
        self.assertFalse(cm.save_config(), "保存を断っていない")
        self.assertFalse(cm.set_server_settings("terminal", {"font_size": 20}),
                         "setter 経由の保存も断ること")

        self.assertEqual(io.open(path, encoding="utf-8").read(), before,
                         "破損した設定ファイルが書き換えられている")

    def test_a_healthy_config_still_saves(self):
        import os, tempfile
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-ok-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        self.assertFalse(cm.load_error)
        self.assertTrue(cm.save_config())

if __name__ == "__main__":
    unittest.main()
