"""パスワード欄が文字列でない機器が 1 台あっても、設定全体を捨てないこと。

実測（8b0c94e）: 正常な機器と password が 1234 / true / {"x": 1} の機器を
同じ config.json に入れて読むと、crypto.is_encrypted() の startswith() が
AttributeError になり、_load_config の全体の例外処理がそれを掴んで既定設定へ
置き換える。接続先リストから全機器と全マクロが消え、そのまま終了すると
_save_layout の保存で既定設定が config.json へ書かれた（password が null / 0
のときは偽値なので復号を飛ばし、この経路には入らない）。

直し方: 機器を捨てずに、復号より前（_quarantine_invalid_devices）で
パスワードの型をそろえる。数値（int / float。bool は数値として扱わない）は
str() で文字列にし、それ以外（bool・dict・list など）は空にして、機器名を
添えて警告する。null は「パスワードなし」の意味が空文字と同じなので、
黙って空文字にする（画面の入力欄は None を受け取れない）。
正常な機器は必ず残る。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "src")

GOOD = {"name": "good", "host": "192.0.2.10", "port": 22,
        "protocol": "ssh", "username": "admin", "password": "secret"}


class NonStringPasswordKeepsDevicesTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-badpassword-"))
        self.path = self.dir / "config.json"

    def _load(self, password):
        from core.config_manager import ConfigManager
        config = {
            "config_version": "1.0",
            "groups": [{"name": "Default", "auto_commands": [], "devices": [
                dict(GOOD),
                {"name": "bad", "host": "192.0.2.11", "port": 22,
                 "protocol": "ssh", "username": "admin",
                 "password": password}]}],
            "global_macros": [{"name": "m1", "commands": ["show version"],
                               "description": ""}],
            "settings": {},
        }
        self.path.write_text(json.dumps(config), encoding="utf-8")
        return ConfigManager(config_path=str(self.path))

    @staticmethod
    def _devices(cm):
        return {d["name"]: d for g in cm.get_groups()
                for d in g.get("devices", [])}

    def test_a_numeric_password_does_not_reset_the_whole_config(self):
        """数値 1 件で、正常な機器もマクロも消えないこと。"""
        cm = self._load(1234)
        self.assertIsNone(cm.load_error,
                          "読み込みが失敗して既定設定へ退避している")
        devices = self._devices(cm)
        self.assertEqual(sorted(devices), ["bad", "good"])
        self.assertEqual(devices["good"]["password"], "secret")
        self.assertEqual([m["name"] for m in cm.get_global_macros()], ["m1"])

    def test_an_integer_password_becomes_the_same_text(self):
        """int は str() で文字列にすること。"""
        self.assertEqual(self._devices(self._load(1234))["bad"]["password"],
                         "1234")

    def test_a_float_password_becomes_the_same_text(self):
        """float も同じ。"""
        self.assertEqual(self._devices(self._load(12.5))["bad"]["password"],
                         "12.5")

    def test_a_zero_password_becomes_the_same_text(self):
        """0 も数値なので "0"。復号を飛ばす偽値のまま残さない。"""
        self.assertEqual(self._devices(self._load(0))["bad"]["password"], "0")

    def test_a_boolean_password_is_emptied(self):
        """bool は数値として扱わない（"True" はパスワードではない）。"""
        for value in (True, False):
            with self.subTest(value=value):
                self.assertEqual(
                    self._devices(self._load(value))["bad"]["password"], "")

    def test_a_dict_or_list_password_is_emptied(self):
        for value in ({"x": 1}, ["a"], [], {}):
            with self.subTest(value=value):
                self.assertEqual(
                    self._devices(self._load(value))["bad"]["password"], "")

    def test_a_null_password_becomes_empty_without_a_warning(self):
        """null は空文字と同じ意味。黙ってそろえるだけにする。"""
        cm = self._load(None)
        self.assertEqual(self._devices(cm)["bad"]["password"], "")
        self.assertIsNone(cm.load_warning)

    def test_the_warning_names_the_device(self):
        """どの機器のパスワードを直したのか分かること。"""
        for value in (1234, True, {"x": 1}):
            with self.subTest(value=value):
                cm = self._load(value)
                self.assertIsNotNone(cm.load_warning)
                self.assertIn("bad", cm.load_warning)
                self.assertIn("パスワード", cm.load_warning)

    def test_the_good_device_survives_on_disk(self):
        """終了時の保存で、正常な機器が既定設定に置き換わらないこと。"""
        cm = self._load({"x": 1})
        self.assertTrue(cm.save_config())
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        names = [d["name"] for g in on_disk["groups"]
                 for d in g.get("devices", [])]
        self.assertEqual(sorted(names), ["bad", "good"])

    def test_a_numeric_password_is_encrypted_when_saved(self):
        """文字列にした数値も、他と同じように暗号化して保存すること。"""
        from core.crypto import PasswordCrypto
        cm = self._load(1234)
        self.assertTrue(cm.save_config())
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        stored = {d["name"]: d["password"] for g in on_disk["groups"]
                  for d in g.get("devices", [])}
        crypto = PasswordCrypto()
        self.assertTrue(crypto.is_encrypted(stored["bad"]))
        self.assertEqual(crypto.decrypt(stored["bad"]), "1234")


if __name__ == "__main__":
    unittest.main()
