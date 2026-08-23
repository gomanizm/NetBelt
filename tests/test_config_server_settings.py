"""ConfigManager のサーバー設定(root/port等)永続化を確認する回帰テスト。"""
import os, tempfile, unittest, sys
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

if __name__ == "__main__":
    unittest.main()
