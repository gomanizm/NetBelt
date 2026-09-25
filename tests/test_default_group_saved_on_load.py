"""起動時に補う Default グループが、Windows でもディスクへ保存されることを検証する。

実測（81664d2）: ConfigManager._load_config() は
`with open(self.config_path, 'r', ...) as f:` のブロックの中で
_ensure_default_group() → save_config() を呼んでいた。save_config() は一時
ファイルへ書いてから os.replace で本体へ差し替えるが、Windows では読み込みの
ために開いたままのファイルへ os.replace すると PermissionError [WinError 5]
になる。検査役の実測では 5 回中 5 回、「[INFO] Defaultグループを追加しました」
の直後に「設定ファイルの保存エラー: [WinError 5] アクセスが拒否されました。」が
出て、ディスクには Default グループが入らなかった。メモリ上には入るので画面は
正常に見えるが、保存は毎回の起動で失敗し続ける。

直し方: 読み込みのブロック（with open）を抜けてから補いと保存を行う。
JSON の読み込みが済めばファイルを開いたままにする理由は無いので、
with の内側に残す必要は無い。保存に失敗したときの扱い（save_config() の
戻り値を見ずに読み込みを続ける）は今までどおり変えていない。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


class DefaultGroupSavedOnLoadTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-defgrp-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_path = self.dir / "config.json"

    def _write(self, config):
        self.config_path.write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def _on_disk(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_the_added_default_group_reaches_the_file(self):
        """Default が無い config.json を読むと、補った Default がディスクにも残る"""
        from core.config_manager import ConfigManager

        self._write({
            "config_version": "1.0",
            "groups": [{"name": "Lab", "auto_commands": [], "devices": []}],
            "global_macros": [],
            "settings": {},
        })

        cm = ConfigManager(str(self.config_path))

        self.assertEqual([g["name"] for g in cm.get_groups()],
                         ["Default", "Lab"])
        names = [g["name"] for g in self._on_disk().get("groups", [])]
        self.assertIn("Default", names,
                      "補った Default グループがディスクに保存されていない")
        self.assertEqual(names, ["Default", "Lab"])

    def test_no_save_error_is_printed_while_loading(self):
        """読み込み中の保存が WinError 5 で失敗しない（保存エラーを出さない）"""
        from core.config_manager import ConfigManager

        self._write({
            "config_version": "1.0",
            "groups": [{"name": "Lab", "auto_commands": [], "devices": []}],
            "global_macros": [],
            "settings": {},
        })

        printed = []
        with mock.patch("builtins.print",
                        side_effect=lambda *a, **k: printed.append(
                            " ".join(str(x) for x in a))):
            ConfigManager(str(self.config_path))

        errors = [line for line in printed if "設定ファイルの保存エラー" in line]
        self.assertEqual(errors, [],
                         "読み込み中の保存が失敗している: %r" % (printed,))

    def test_the_replace_happens_after_the_file_is_closed(self):
        """os.replace の時点で、読み込み用に開いたハンドルが残っていない"""
        from core import config_manager as cm_module
        from core.config_manager import ConfigManager

        self._write({
            "config_version": "1.0",
            "groups": [{"name": "Lab", "auto_commands": [], "devices": []}],
            "global_macros": [],
            "settings": {},
        })

        real_open = open
        opened = []

        def tracking_open(file, *args, **kwargs):
            handle = real_open(file, *args, **kwargs)
            if Path(str(file)) == self.config_path:
                opened.append(handle)
            return handle

        still_open = []
        real_replace = os.replace

        def tracking_replace(src, dst, *args, **kwargs):
            if Path(str(dst)) == self.config_path:
                still_open.append([h for h in opened if not h.closed])
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=tracking_open):
            with mock.patch.object(cm_module.os, "replace",
                                   side_effect=tracking_replace):
                ConfigManager(str(self.config_path))

        self.assertTrue(still_open, "保存（os.replace）が一度も走っていない")
        self.assertEqual(
            [len(handles) for handles in still_open], [0] * len(still_open),
            "config.json を開いたまま os.replace している（Windows では "
            "PermissionError [WinError 5] になる）")

    def test_an_existing_default_group_is_not_saved_again(self):
        """Default が既にあるときは、読み込みで保存しない（今までどおり）"""
        from core import config_manager as cm_module
        from core.config_manager import ConfigManager

        self._write({
            "config_version": "1.0",
            "groups": [{"name": "Default", "auto_commands": [], "devices": []}],
            "global_macros": [],
            "settings": {},
        })

        replaced = []
        real_replace = os.replace
        with mock.patch.object(
                cm_module.os, "replace",
                side_effect=lambda src, dst, *a, **k: (
                    replaced.append(str(dst)), real_replace(src, dst))[1]):
            ConfigManager(str(self.config_path))

        self.assertEqual(replaced, [])


if __name__ == "__main__":
    unittest.main()
