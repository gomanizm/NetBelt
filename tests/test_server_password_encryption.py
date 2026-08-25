"""サーバ設定のパスワードも DPAPI で保存されることを検証する。

機器のパスワードは暗号化していたのに、FTP サーバの認証情報は config.json
へそのまま書かれていた。UI は入力欄を伏字にしているので、画面では隠れて
いるのにディスクには平文、という不整合だった。しかも保存はサーバ起動より
前に行われるため、起動に失敗しても平文が残る。

既存の「別 PC / 別 Windows ユーザー由来の DPAPI 値を二重暗号化しない」
という挙動を壊していないことも、ここで併せて固定する。
"""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

SECRET = "ftp-secret-1234"


class ServerPasswordEncryptionTest(unittest.TestCase):
    def _manager(self):
        from core.config_manager import ConfigManager
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-cfg-"),
                            "config.json")
        return ConfigManager(path), path

    def _raw(self, path):
        with io.open(path, encoding="utf-8") as f:
            return f.read()

    def test_the_ftp_password_is_not_written_in_the_clear(self):
        """FTP のパスワードが config.json に平文で残らないこと。"""
        cm, path = self._manager()
        cm.set_server_settings("ftp_server",
                               {"username": "ftpuser", "password": SECRET})
        raw = self._raw(path)
        self.assertNotIn(SECRET, raw, "パスワードが平文で保存されている")
        self.assertIn("DPAPI:", raw, "暗号化されていない")

    def test_the_ftp_password_round_trips(self):
        """暗号化しても、読み戻したら元の値であること。"""
        from core.config_manager import ConfigManager
        cm, path = self._manager()
        cm.set_server_settings("ftp_server",
                               {"username": "ftpuser", "password": SECRET})

        reopened = ConfigManager(path)
        section = reopened.get_server_settings("ftp_server")
        self.assertEqual(section.get("password"), SECRET)
        self.assertEqual(section.get("username"), "ftpuser",
                         "ユーザー名まで変えてしまっている")

    def test_saving_twice_does_not_double_encrypt(self):
        """二度保存しても二重に包まないこと。

        起動のたびに保存が走るので、ここを誤ると原本が復元不能になる。
        """
        from core.config_manager import ConfigManager
        cm, path = self._manager()
        cm.set_server_settings("ftp_server", {"password": SECRET})
        first = json.loads(self._raw(path))["settings"]["ftp_server"]["password"]

        again = ConfigManager(path)
        again.save_config()
        second = json.loads(self._raw(path))["settings"]["ftp_server"]["password"]

        self.assertTrue(first.startswith("DPAPI:"))
        self.assertTrue(second.startswith("DPAPI:"))
        self.assertEqual(ConfigManager(path).get_server_settings(
            "ftp_server").get("password"), SECRET,
            "二重暗号化で元のパスワードが失われた")

    def test_a_foreign_dpapi_value_is_left_alone(self):
        """別環境で作られた復号できない値を、平文とみなして包み直さないこと。

        DPAPI の鍵は Windows アカウントとマシンに紐づく。config.json を
        別の PC へ持っていくと復号できないが、それを平文と誤認して
        再暗号化すると原本が二度と戻らない。
        """
        from core.config_manager import ConfigManager
        cm, path = self._manager()
        cm.set_server_settings("ftp_server", {"password": SECRET})

        # 別環境由来を模して、復号できない DPAPI 値へ差し替える
        data = json.loads(self._raw(path))
        foreign = "DPAPI:" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
        data["settings"]["ftp_server"]["password"] = foreign
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        reopened = ConfigManager(path)
        reopened.save_config()

        stored = json.loads(self._raw(path))["settings"]["ftp_server"]["password"]
        self.assertEqual(stored, foreign,
                         "復号できない値を包み直して壊した")

    def test_an_empty_password_stays_empty(self):
        """空のパスワードを暗号化しないこと（既定値のまま使う人がいる）。"""
        cm, path = self._manager()
        cm.set_server_settings("ftp_server", {"username": "", "password": ""})
        stored = json.loads(self._raw(path))["settings"]["ftp_server"]["password"]
        self.assertEqual(stored, "")


if __name__ == "__main__":
    unittest.main()
