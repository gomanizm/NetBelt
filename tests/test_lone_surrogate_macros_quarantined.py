"""送れない文字（孤立したサロゲート）を含むマクロと自動実行コマンドを、読み込み時に隔離することを検証する。

何が起きていたか（基準 441ea02）。config.json に U+D800 のエスケープ
（壊れた UTF-16 の片割れ）を含むコマンドを書いたマクロ・自動実行
コマンドは、そのまま読み込まれ、一覧にも出た。マクロは 1 行ずつ端末の
送信列へ積まれ、その行の send_command の encode('utf-8') が
UnicodeEncodeError になる。SSH と Telnet は『送信エラー』として切断し
（それより前の行は送ったあと）、シリアルは区切り 1 つを黙って落として
次の行へ進んだ（実測: 貼り付けで前後の行が 1 行に繋がって実行された）。
GUI の編集からは入らない（config.json の保存が UTF-8 にできずに失敗し、
変更は取り消される）ので、入ってくるのは手編集の config.json だけ。

どう直したか（利用者の決定 (a)）。マクロの commands と自動実行コマンドの
各行が UTF-8 にできることを、読み込み時の点検（is_readable_macro・
_is_valid_auto_commands）の条件に足した。できない行を含むものは、既存の
仕組みで隔離され（バックアップを作り、警告を出す）、機器へは何も送らない。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

SUR = chr(0xD800)
GOOD = {"name": "show", "commands": ["show version"], "description": ""}
BROKEN = {"name": "broken", "commands": ["show clock", "descr" + SUR + "iption"],
          "description": ""}


def _device(name, **extra):
    data = {"name": name, "host": "192.0.2.1", "port": 22, "protocol": "ssh",
            "username": "u", "password": "", "ssh_key": "", "macros": []}
    data.update(extra)
    return data


class LoneSurrogateQuarantineTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-surrogate-macro-")
        patch = mock.patch("core.config_manager.app_data_dir",
                           return_value=Path(self.dir))
        patch.start()
        self.addCleanup(patch.stop)

    def _load(self, global_macros=(), device_macros=(), auto_commands=()):
        """その内容を持つ config.json（手編集と同じくエスケープで書く）を読む"""
        from core.config_manager import ConfigManager
        path = os.path.join(self.dir,
                            "config-%d.json" % len(os.listdir(self.dir)))
        with open(path, "w", encoding="utf-8") as f:
            # ensure_ascii の既定（True）でサロゲートを U+D800 のエスケープで書く
            json.dump({"config_version": "1.0",
                       "groups": [{"name": "Lab",
                                   "auto_commands": list(auto_commands),
                                   "devices": [_device(
                                       "R1", macros=list(device_macros))]}],
                       "global_macros": list(global_macros),
                       "settings": {}}, f)
        manager = ConfigManager(config_path=path)
        self.assertIsNone(manager.load_error, "前提: JSON としては読めている")
        return manager

    def test_a_global_macro_with_an_unsendable_line_is_dropped_and_reported(self):
        manager = self._load(global_macros=[BROKEN, GOOD])

        self.assertEqual([GOOD], manager.get_global_macros(),
                         "送れない行を持つマクロが残っている")
        self.assertIn("全体共通マクロ", manager.load_warning or "",
                      "外したことを知らせていない: %r" % manager.load_warning)

    def test_a_device_macro_with_an_unsendable_line_is_dropped_and_reported(self):
        manager = self._load(device_macros=[BROKEN, GOOD])

        device = manager.get_group("Lab")["devices"][0]
        self.assertEqual([GOOD], device["macros"],
                         "送れない行を持つ機器別マクロが残っている")
        self.assertIn("R1", manager.load_warning or "",
                      "どの機器か知らせていない: %r" % manager.load_warning)

    def test_auto_commands_with_an_unsendable_line_are_disabled_and_reported(self):
        manager = self._load(auto_commands=["terminal length 0",
                                            "show " + SUR])

        self.assertEqual([], manager.get_group("Lab")["auto_commands"],
                         "送れない行を持つ自動実行コマンドが残っている")
        warning = manager.load_warning or ""
        self.assertIn("Lab", warning, "どのグループか知らせていない: %r" % warning)
        self.assertIn("送れない文字", warning,
                      "理由（送れない文字）が分からない: %r" % warning)

    def test_the_original_file_is_backed_up(self):
        manager = self._load(global_macros=[BROKEN])

        self.assertTrue(manager.backup_path, "元のファイルを残していない")
        self.assertTrue(os.path.exists(manager.backup_path))

    def test_the_checks_themselves(self):
        from core.config_manager import ConfigManager, is_readable_macro
        self.assertFalse(is_readable_macro(BROKEN))
        self.assertTrue(is_readable_macro(GOOD))
        self.assertFalse(ConfigManager._is_valid_auto_commands(["a" + SUR]))
        self.assertTrue(ConfigManager._is_valid_auto_commands(
            ["show version", "日本語の説明"]))

    def test_good_entries_are_left_alone(self):
        manager = self._load(global_macros=[GOOD], device_macros=[GOOD],
                             auto_commands=["terminal length 0"])

        self.assertEqual([GOOD], manager.get_global_macros())
        self.assertEqual(["terminal length 0"],
                         manager.get_group("Lab")["auto_commands"])
        self.assertIsNone(manager.load_warning,
                          "正しい設定で警告が出た: %r" % manager.load_warning)


if __name__ == "__main__":
    unittest.main()
