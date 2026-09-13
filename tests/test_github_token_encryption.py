"""GitHub トークンが config.json へ平文で書かれる不具合の回帰テスト。

update_settings.github_token はプライベートリポジトリの更新取得に使う
資格情報だが、機器パスワードや FTP/SFTP のパスワードと違って暗号化の
対象から漏れていた。設定ファイルが壊れたときの backup_* にも平文のまま
複製される。

復号できない値（別 Windows アカウント／PC で保存された設定）は、機器
パスワードと同じく再暗号化せずそのまま残すこと。そしてその暗号文を
Authorization ヘッダへ載せない（載せても 401 になるだけで、原因が
利用者に分からない）。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

TOKEN = "ghp_SYNTHETIC_TOKEN_FOR_TESTS_0000"
# 他環境で作られた（＝この環境では復号できない）DPAPI 形式の値
FOREIGN = "DPAPI:" + "b2xkLW1hY2hpbmUtY2lwaGVydGV4dC1oZXJl"


def _no_env_token():
    """環境変数 GITHUB_TOKEN が設定ファイルを隠さないようにする。"""
    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    return mock.patch.dict(os.environ, env, clear=True)


class GithubTokenEncryptionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-token-")
        self.path = os.path.join(self.dir, "config.json")

    def _saved_token(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f).get("update_settings", {}).get("github_token")

    def test_the_token_is_encrypted_on_disk(self):
        """保存したトークンが平文でディスクに残らないこと。"""
        from core.config_manager import ConfigManager
        from core.crypto import PasswordCrypto
        cm = ConfigManager(config_path=self.path)
        self.assertTrue(cm.set_github_token(TOKEN))
        saved = self._saved_token()
        self.assertNotEqual(saved, TOKEN, "トークンが平文で保存された")
        self.assertEqual(PasswordCrypto().decrypt(saved), TOKEN)

    def test_the_token_is_usable_after_a_reload(self):
        """読み直しても元のトークンが取り出せること。"""
        from core.config_manager import ConfigManager
        cm = ConfigManager(config_path=self.path)
        cm.set_github_token(TOKEN)
        cm2 = ConfigManager(config_path=self.path)
        with _no_env_token():
            self.assertEqual(cm2.get_github_token(), TOKEN)

    def test_a_hand_written_plaintext_token_is_encrypted_on_the_next_save(self):
        """手で置かれた平文トークンも、次の保存で暗号化されること。"""
        from core.config_manager import ConfigManager
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"config_version": "1.0", "groups": [],
                       "global_macros": [], "settings": {},
                       "update_settings": {"github_token": TOKEN}}, f)
        cm = ConfigManager(config_path=self.path)
        cm.save_config()
        self.assertNotEqual(self._saved_token(), TOKEN,
                            "平文トークンがそのまま残っている")

    def test_an_undecryptable_token_is_not_reencrypted(self):
        """復号できない値は再暗号化しないこと（原本を壊さない）。"""
        from core.config_manager import ConfigManager
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"config_version": "1.0", "groups": [],
                       "global_macros": [], "settings": {},
                       "update_settings": {"github_token": FOREIGN}}, f)
        cm = ConfigManager(config_path=self.path)
        cm.save_config()
        self.assertEqual(self._saved_token(), FOREIGN)

    def test_an_undecryptable_token_is_not_sent_as_a_credential(self):
        """復号できない暗号文を Authorization ヘッダへ載せないこと。"""
        from core.config_manager import ConfigManager
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"config_version": "1.0", "groups": [],
                       "global_macros": [], "settings": {},
                       "update_settings": {"github_token": FOREIGN}}, f)
        cm = ConfigManager(config_path=self.path)
        with _no_env_token():
            self.assertIsNone(cm.get_github_token(),
                              "復号できない暗号文をトークンとして返している")

    def test_no_token_stays_none(self):
        """トークン未設定のときの挙動は変わらないこと。"""
        from core.config_manager import ConfigManager
        cm = ConfigManager(config_path=self.path)
        cm.save_config()
        self.assertIsNone(self._saved_token())
        with _no_env_token():
            self.assertIsNone(cm.get_github_token())


if __name__ == "__main__":
    unittest.main()
