"""先頭に UTF-8 BOM が付いた config.json を、そのまま読めることを検証する。

何が起きていたか（基準 f4cad23 で実測）。_load_config は
open(..., encoding='utf-8') で読むので、BOM は文字として残ったまま
json.load へ渡り「Unexpected UTF-8 BOM (decode using utf-8-sig)」になる。
機器 1 件（sw1 / 192.0.2.10）を持つ正しい config.json の先頭に BOM を
付けただけで:

  [ERROR] 設定ファイルの読み込みエラー: Unexpected UTF-8 BOM ...
  [INFO] 破損した設定ファイルをバックアップしました: ...
  読み込まれたグループ: Default / 本番環境 / 検証環境（同梱の既定設定）

つまり利用者の機器・グループが全部消えた状態で起動し、次に save_config()
が走ると config.json が既定設定の中身（ルータA だけ）へ置き換わる。
BOM は PowerShell 5.1 の `Out-File -Encoding utf8` や古いメモ帳の
「UTF-8 (BOM)」で手編集すると付く。known_hosts 側は BOM を剥がす経路が
入っている（ssh_connection.py の _has_utf8_bom、config_manager.py の
引き継ぎ）のに、config.json だけ未対応という非対称だった。

どう直したか。読み込みの encoding を 'utf-8-sig' にした（BOM があれば
剥がすだけで、無ければ utf-8 と同じ）。書き出しは今までどおり BOM 無しの
utf-8 なので、一度読み直して保存すれば BOM は自然に落ちる。
"""
import codecs
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "src")

from core.config_manager import ConfigManager       # noqa: E402


class ConfigUtf8BomTest(unittest.TestCase):
    def _config_with(self, prefix=b""):
        """機器 1 件の正しい config.json を書いて、そのパスを返す。"""
        directory = Path(tempfile.mkdtemp(prefix="netbelt-cfgbom-"))
        path = directory / "config.json"
        data = {
            "config_version": "1.0",
            "groups": [{"name": "Default", "devices": [
                {"name": "sw1", "host": "192.0.2.10",
                 "protocol": "ssh", "port": 22,
                 "username": "admin", "password": ""}]}],
            "global_macros": [],
            "settings": {},
        }
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        path.write_bytes(prefix + raw)
        return path

    @staticmethod
    def _device_names(config):
        return [d.get("name")
                for g in config.get("groups", [])
                for d in g.get("devices", [])]

    def test_a_config_with_a_bom_is_not_treated_as_corrupted(self):
        """BOM が付いただけの config.json を、破損扱いにしないこと。"""
        path = self._config_with(codecs.BOM_UTF8)

        cm = ConfigManager(config_path=str(path))

        self.assertIsNone(
            cm.load_error,
            "BOM 付きの正しい設定を破損扱いにしている: %s" % cm.load_error)

    def test_a_config_with_a_bom_keeps_the_devices(self):
        """BOM が付いていても、登録した機器がそのまま残ること。"""
        path = self._config_with(codecs.BOM_UTF8)

        cm = ConfigManager(config_path=str(path))

        self.assertEqual(
            self._device_names(cm.config), ["sw1"],
            "BOM 付きの設定で機器が消えている（既定設定へ差し替わった）")

    def test_saving_after_a_bom_does_not_replace_the_config(self):
        """BOM 付きで読んだあとの保存が、設定を既定値へ置き換えないこと。"""
        path = self._config_with(codecs.BOM_UTF8)

        cm = ConfigManager(config_path=str(path))
        cm.save_config()

        saved = json.loads(path.read_text(encoding="utf-8-sig"))
        self.assertEqual(
            self._device_names(saved), ["sw1"],
            "保存で利用者の機器が既定設定へ置き換わっている")

    def test_saving_writes_the_config_back_without_a_bom(self):
        """書き出しは今までどおり BOM 無しであること。"""
        path = self._config_with(codecs.BOM_UTF8)

        cm = ConfigManager(config_path=str(path))
        cm.save_config()

        self.assertFalse(
            path.read_bytes().startswith(codecs.BOM_UTF8),
            "保存した config.json に BOM が残っている")

    def test_a_config_without_a_bom_still_loads(self):
        """BOM の無い今までの config.json も、これまでどおり読めること。"""
        path = self._config_with()

        cm = ConfigManager(config_path=str(path))

        self.assertIsNone(cm.load_error)
        self.assertEqual(self._device_names(cm.config), ["sw1"])

    def test_a_broken_config_is_still_reported(self):
        """壊れた JSON は、これまでどおり破損として知らせること。"""
        directory = Path(tempfile.mkdtemp(prefix="netbelt-cfgbom-"))
        path = directory / "config.json"
        path.write_bytes(codecs.BOM_UTF8 + b'{"groups": [')

        cm = ConfigManager(config_path=str(path))

        self.assertIsNotNone(
            cm.load_error, "壊れた設定を黙って読み飛ばしている")
        self.assertTrue(
            any(name.startswith("config.json.backup_")
                for name in os.listdir(str(directory))),
            "壊れた設定のバックアップが作られていない")


if __name__ == "__main__":
    unittest.main()
