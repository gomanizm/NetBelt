"""復号できないパスワードを再暗号化してしまう不具合の回帰テスト。

DPAPI の鍵は Windows アカウント／マシンに紐づくため、config.json を別 PC へ移した、
プロファイルを作り直した、別ユーザーで起動した、といった場合に復号できなくなる。
そのとき decrypt() は暗号文をそのまま返すため、保存時にそれを平文とみなして
もう一度暗号化すると暗号文が二重に包まれ、元のパスワードは復元不能になる。

保存はユーザー操作なしでも起動時に走るため、原本が上書きされてしまう点が特に重い。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

# 他環境で作られた（＝この環境では復号できない）DPAPI 形式の値
FOREIGN = "DPAPI:" + "b2xkLW1hY2hpbmUtY2lwaGVydGV4dC1oZXJl"


def _config_with_password(password):
    return {
        "config_version": "1.0",
        "groups": [{
            "name": "Default",
            "auto_commands": [],
            "devices": [{
                "name": "ルータA", "host": "192.0.2.1", "port": 22,
                "protocol": "ssh", "username": "admin",
                "password": password, "ssh_key": "", "macros": [],
            }],
        }],
        "global_macros": [],
        "settings": {},
    }


class PasswordReencryptTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-pw-")
        self.path = os.path.join(self.dir, "config.json")

    def _saved_password(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)["groups"][0]["devices"][0]["password"]

    def test_undecryptable_password_is_not_reencrypted(self):
        """復号できない値は、そのまま書き戻されること（原本を壊さない）。"""
        from core.config_manager import ConfigManager
        cm = ConfigManager(config_path=self.path)
        cm.config = _config_with_password(FOREIGN)
        self.assertTrue(cm.save_config())
        self.assertEqual(
            self._saved_password(), FOREIGN,
            "復号できないパスワードが再暗号化され、原本が失われた")

    def test_repeated_saves_do_not_wrap_further(self):
        """起動のたびに保存が走っても、包みが増えていかないこと。"""
        from core.config_manager import ConfigManager
        cm = ConfigManager(config_path=self.path)
        cm.config = _config_with_password(FOREIGN)
        for _ in range(3):
            cm.save_config()
        self.assertEqual(self._saved_password(), FOREIGN)

    def test_plaintext_password_is_still_encrypted(self):
        """通常の平文パスワードは、これまでどおり暗号化されること。"""
        from core.config_manager import ConfigManager
        from core.crypto import PasswordCrypto
        cm = ConfigManager(config_path=self.path)
        cm.config = _config_with_password("Cisco123!")
        self.assertTrue(cm.save_config())
        saved = self._saved_password()
        self.assertNotEqual(saved, "Cisco123!", "平文のまま保存された")
        self.assertTrue(saved.startswith("DPAPI:"))
        self.assertEqual(PasswordCrypto().decrypt(saved), "Cisco123!")

    def test_roundtrip_keeps_password_usable(self):
        """保存して読み直しても、元のパスワードが取り出せること。"""
        from core.config_manager import ConfigManager
        cm = ConfigManager(config_path=self.path)
        cm.config = _config_with_password("Cisco123!")
        cm.save_config()
        cm2 = ConfigManager(config_path=self.path)
        got = cm2.config["groups"][0]["devices"][0]["password"]
        self.assertEqual(got, "Cisco123!")

    def test_empty_password_stays_empty(self):
        from core.config_manager import ConfigManager
        cm = ConfigManager(config_path=self.path)
        cm.config = _config_with_password("")
        cm.save_config()
        self.assertEqual(self._saved_password(), "")


if __name__ == "__main__":
    unittest.main()
