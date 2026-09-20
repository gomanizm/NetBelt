"""auto_commands の型が壊れた config.json から、意図しないコマンドが送られないことを検証する。

実測（16101ef）: グループの auto_commands を配列ではなく文字列「show version」に
した config.json を ConfigManager で読むと、load_error も load_warning も None で
そのまま通る。MainWindow._run_auto_commands は group.get("auto_commands", []) を
list() にして MacroManager.start_command_list へ渡すので、文字列は 1 文字ずつに
分解され、macro_manager が各要素へ CR を付けて機器へ 12 回送る（実測: 本物の
MacroManager で「s＋Enter」「h＋Enter」…が届いた）。辞書ならキーだけが送られる。
list() できない値（数値など）だと QTimer のコールバックの中で TypeError になり、
PyQt6 はスロット内の未捕捉例外で qFatal するため、接続した瞬間に NetBelt が
プロセスごと消える（実測: 終了コード 127、traceback も残らない）。

入口の GroupDialog.get_auto_commands は必ず list を返すので、入り込むのは手編集
または外部生成の config.json だけ。読み込み時に「list かつ全要素が str」を検査し、
外れていたら [] に直して、機器の隔離と同じ警告で知らせる（利用者が書いていない
操作を実機へ送るより、自動実行を止めて知らせる方が安全）。
set_group_auto_commands も同じ検査で、壊れた値の保存を断る。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

DEVICE = {
    "name": "rtr-01", "host": "192.0.2.1", "port": 22, "protocol": "ssh",
    "username": "admin", "password": "", "ssh_key": "", "macros": [],
}


class AutoCommandsTypeTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-autotype-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _manager(self, lab_group):
        """Lab グループを与えた config.json を読み込む ConfigManager を返す。"""
        from core.config_manager import ConfigManager
        path = self.dir / "config.json"
        path.write_text(json.dumps({
            "config_version": "1.0",
            "groups": [
                {"name": "Default", "auto_commands": [], "devices": []},
                lab_group,
            ],
            "global_macros": [],
            "settings": {},
        }, ensure_ascii=False), encoding="utf-8")
        return ConfigManager(config_path=str(path))

    def _lab(self, auto_commands):
        return {"name": "Lab", "auto_commands": auto_commands,
                "devices": [dict(DEVICE)]}

    def _sent_by_main_window(self, cm):
        """MainWindow._run_auto_commands が機器へ送る一覧と同じ読み方をする。"""
        group = cm.get_group("Lab")
        commands = group.get("auto_commands", [])
        if not commands:
            return []
        return list(commands)

    def test_a_string_auto_commands_is_disabled(self):
        """文字列の auto_commands は [] に直し、1 文字ずつ送らせないこと。"""
        cm = self._manager(self._lab("show version"))

        self.assertEqual(cm.get_group("Lab")["auto_commands"], [],
                         "文字列の auto_commands がそのまま残っている")
        self.assertEqual(self._sent_by_main_window(cm), [],
                         "接続しただけで機器へ 1 文字ずつ送られる")

    def test_a_string_auto_commands_is_reported(self):
        """直したことを、グループ名を挙げて知らせること。"""
        cm = self._manager(self._lab("show version"))

        self.assertIsNotNone(cm.load_warning, "案内が何も出ていない")
        self.assertIn("Lab", cm.load_warning, "どのグループかが分からない")
        self.assertIn("自動実行コマンド", cm.load_warning)

    def test_a_dict_auto_commands_is_disabled(self):
        """辞書はキーだけが送られるので、同じく [] に直すこと。"""
        cm = self._manager(self._lab({"show version": 1, "show run": 2}))

        self.assertEqual(cm.get_group("Lab")["auto_commands"], [])
        self.assertEqual(self._sent_by_main_window(cm), [])
        self.assertIn("Lab", cm.load_warning or "")

    def test_a_value_that_cannot_be_listed_is_disabled(self):
        """list() できない値（数値）も [] に直すこと（そのままだと落ちる）。"""
        cm = self._manager(self._lab(5))

        self.assertEqual(cm.get_group("Lab")["auto_commands"], [])
        self.assertEqual(self._sent_by_main_window(cm), [])

    def test_a_list_with_a_non_string_element_is_disabled(self):
        """1 つでも文字列でない要素があれば、その一覧ごと [] に直すこと。"""
        cm = self._manager(self._lab(["show version", 5]))

        self.assertEqual(cm.get_group("Lab")["auto_commands"], [])
        self.assertIn("Lab", cm.load_warning or "")

    def test_a_null_auto_commands_is_disabled(self):
        """null も list ではないので [] に直すこと。"""
        cm = self._manager(self._lab(None))

        self.assertEqual(cm.get_group("Lab")["auto_commands"], [])

    def test_a_valid_auto_commands_is_left_alone(self):
        """正しい auto_commands は触らず、警告も出さないこと。"""
        cm = self._manager(self._lab(["terminal length 0", "show clock"]))

        self.assertEqual(cm.get_group("Lab")["auto_commands"],
                         ["terminal length 0", "show clock"])
        self.assertIsNone(cm.load_warning, "正常な設定で警告が出ている")

    def test_a_group_without_auto_commands_is_left_alone(self):
        """auto_commands の無いグループは、キーを足さず警告も出さないこと。"""
        cm = self._manager({"name": "Lab", "devices": [dict(DEVICE)]})

        self.assertNotIn("auto_commands", cm.get_group("Lab"))
        self.assertIsNone(cm.load_warning)

    def test_the_devices_of_the_group_are_kept(self):
        """自動実行を止めるだけで、そのグループの機器は残すこと。"""
        cm = self._manager(self._lab("show version"))

        self.assertEqual([d["name"] for d in cm.get_group("Lab")["devices"]],
                         ["rtr-01"])

    def test_the_original_file_is_backed_up(self):
        """直したときは、元の config.json をバックアップして知らせること。"""
        cm = self._manager(self._lab("show version"))

        self.assertIsNotNone(cm.backup_path, "バックアップしていない")
        self.assertTrue(Path(cm.backup_path).exists())


class SetGroupAutoCommandsTypeTest(unittest.TestCase):
    """set_group_auto_commands も、壊れた値の保存を断ること。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-autotype-set-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        from core.config_manager import ConfigManager
        self.cm = ConfigManager(config_path=str(self.dir / "config.json"))
        self.cm.add_group("Lab", ["show clock"])

    def test_a_string_is_refused(self):
        self.assertFalse(self.cm.set_group_auto_commands("Lab", "show version"))
        self.assertEqual(self.cm.get_group("Lab")["auto_commands"], ["show clock"],
                         "断ったのに書き換わっている")

    def test_a_non_string_element_is_refused(self):
        self.assertFalse(self.cm.set_group_auto_commands("Lab", ["show version", 5]))
        self.assertEqual(self.cm.get_group("Lab")["auto_commands"], ["show clock"])

    def test_a_refusal_does_not_save(self):
        with mock.patch.object(self.cm, "save_config") as save:
            self.assertFalse(self.cm.set_group_auto_commands("Lab", "show version"))
        self.assertFalse(save.called, "断ったのに保存している")

    def test_a_valid_list_is_still_accepted(self):
        self.assertTrue(self.cm.set_group_auto_commands("Lab", ["show version"]))
        self.assertEqual(self.cm.get_group("Lab")["auto_commands"], ["show version"])


if __name__ == "__main__":
    unittest.main()
