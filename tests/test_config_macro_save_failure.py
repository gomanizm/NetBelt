"""共通マクロ（プリセット）の編集が、保存に失敗したらメモリにも残らないことを検証する。

add_global_macro / update_global_macro / remove_global_macro は save_config()
の戻り値を見ずに self.config を書き換えたまま False を返す。同じファイルの
add_device / remove_device / update_device / move_device には復元があるので、
マクロ経路だけ取りこぼしている。

保存に失敗したのに書き換えが残ると、MacroDialog が「失敗しました」と出した
編集が、次の無関係な保存（機器の追加や設定変更）でディスクへ確定する。
消せなかったはずのプリセットが後で消え、保存できなかったはずのコマンド列が
後から実機へ流れる。保存が通らなかったときは、呼ぶ前の状態へ戻す。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

EXISTING = {
    "name": "基本確認",
    "commands": ["show version"],
    "description": "版数の確認",
}


def _write_config(path, macros):
    config = {
        "config_version": "1.0",
        "groups": [{"name": "Default", "auto_commands": [], "devices": []}],
        "global_macros": macros,
        "settings": {},
        "update_settings": {"check_on_startup": False},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)


class GlobalMacroRollsBackWhenSaveFailsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-macrosave-")
        self.path = os.path.join(self.dir, "config.json")
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)
        _write_config(self.path, [json.loads(json.dumps(EXISTING))])
        from core.config_manager import ConfigManager
        self.cm = ConfigManager(config_path=self.path)

    def _failing_save(self):
        return mock.patch.object(self.cm, "save_config", return_value=False)

    def test_add_is_rolled_back(self):
        with self._failing_save():
            self.assertFalse(
                self.cm.add_global_macro("追加", ["show ip interface brief"], "説明"))

        self.assertIsNone(self.cm.get_macro_by_name("追加"),
                          "保存できなかったマクロがメモリに残っている")
        self.assertEqual([m["name"] for m in self.cm.get_global_macros()],
                         [EXISTING["name"]])

    def test_update_is_rolled_back(self):
        with self._failing_save():
            self.assertFalse(
                self.cm.update_global_macro(EXISTING["name"], ["reload"], "書き換え"))

        macro = self.cm.get_macro_by_name(EXISTING["name"])
        self.assertEqual(macro["commands"], EXISTING["commands"],
                         "保存できなかったコマンド列がメモリに残っている")
        self.assertEqual(macro["description"], EXISTING["description"])

    def test_remove_is_rolled_back(self):
        with self._failing_save():
            self.assertFalse(self.cm.remove_global_macro(EXISTING["name"]))

        self.assertEqual([m["name"] for m in self.cm.get_global_macros()],
                         [EXISTING["name"]],
                         "保存できなかったのにマクロがメモリから消えている")

    def test_a_successful_edit_still_takes_effect(self):
        """復元が成功時まで巻き戻していないこと。"""
        self.assertTrue(self.cm.add_global_macro("追加", ["show clock"], "時刻"))
        self.assertTrue(self.cm.update_global_macro("追加", ["show clock detail"], "時刻"))
        self.assertEqual(self.cm.get_macro_by_name("追加")["commands"],
                         ["show clock detail"])
        self.assertTrue(self.cm.remove_global_macro("追加"))
        self.assertIsNone(self.cm.get_macro_by_name("追加"))

        with open(self.path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual([m["name"] for m in saved["global_macros"]],
                         [EXISTING["name"]])


if __name__ == "__main__":
    unittest.main()
