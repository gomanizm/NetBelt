"""実パスワードが "DPAPI:" で始まると平文で保存される不具合の回帰テスト。

暗号化済みかの判定が接頭辞だけだったため、機器へ本当に "DPAPI:" で始まる
パスワードを設定していると、_encrypt_passwords が「もう暗号化されている」と
誤認して config.json へ平文のまま書き出す。読み直しても「復号できません
でした」と数えられ、起動のたびに警告が出る。

別環境で作られた復号不能な暗号文をそのまま保持する契約
（tests/test_password_reencrypt.py）は壊さないこと。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

# 利用者が実際に設定しうる平文。base64 として妥当でない本体を持つ
LITERAL = "DPAPI:actual-plaintext-password"
# 他環境で作られた（＝この環境では復号できない）DPAPI 形式の値
FOREIGN = "DPAPI:" + "b2xkLW1hY2hpbmUtY2lwaGVydGV4dC1oZXJl"


def _config_with_password(password):
    return {
        "config_version": "1.0",
        "groups": [{
            "name": "Default",
            "auto_commands": [],
            "devices": [{
                "name": "ルータA", "host": "192.0.2.1", "port": 23,
                "protocol": "telnet", "username": "admin",
                "password": password, "ssh_key": "", "macros": [],
            }],
        }],
        "global_macros": [],
        "settings": {},
    }


class LiteralDpapiPrefixPasswordTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-dpapi-")
        self.path = os.path.join(self.dir, "config.json")

    def _saved_password(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)["groups"][0]["devices"][0]["password"]

    def test_a_literal_prefix_password_is_not_taken_for_ciphertext(self):
        """接頭辞が一致しても、本体が base64 でなければ平文と見なすこと。"""
        from core.crypto import PasswordCrypto
        self.assertFalse(PasswordCrypto().is_encrypted(LITERAL),
                         "平文を暗号化済みと誤認している")

    def test_a_literal_prefix_password_is_encrypted_on_disk(self):
        """"DPAPI:" で始まる平文も、ディスクには暗号化して書くこと。"""
        from core.config_manager import ConfigManager
        from core.crypto import PasswordCrypto
        cm = ConfigManager(config_path=self.path)
        cm.config = _config_with_password(LITERAL)
        self.assertTrue(cm.save_config())
        saved = self._saved_password()
        self.assertNotEqual(saved, LITERAL, "平文のまま保存された")
        self.assertEqual(PasswordCrypto().decrypt(saved), LITERAL)

    def test_a_literal_prefix_password_survives_a_reload(self):
        """読み直して元の値に戻り、復号失敗として数えられないこと。"""
        from core.config_manager import ConfigManager
        cm = ConfigManager(config_path=self.path)
        cm.config = _config_with_password(LITERAL)
        cm.save_config()
        cm2 = ConfigManager(config_path=self.path)
        self.assertEqual(
            cm2.config["groups"][0]["devices"][0]["password"], LITERAL)
        self.assertEqual(getattr(cm2, "_undecryptable_count", 0), 0,
                         "平文を復号失敗として数えている")

    def test_foreign_ciphertext_is_still_preserved(self):
        """既存の契約: 復号できない暗号文は再暗号化しないこと。"""
        from core.config_manager import ConfigManager
        cm = ConfigManager(config_path=self.path)
        cm.config = _config_with_password(FOREIGN)
        cm.save_config()
        self.assertEqual(self._saved_password(), FOREIGN,
                         "復号できない暗号文を再暗号化し、原本を失った")


if __name__ == "__main__":
    unittest.main()
