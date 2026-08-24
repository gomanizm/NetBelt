"""グループの自動実行コマンド（auto_commands）の保存と編集 UI。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")


class SetGroupAutoCommandsTest(unittest.TestCase):
    """ConfigManager.set_group_auto_commands の振る舞い。"""

    def _new_manager(self):
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-autocmd-")
        return ConfigManager(config_path=os.path.join(d, "config.json")), d

    def test_roundtrip_through_file(self):
        cm, d = self._new_manager()
        self.assertTrue(cm.add_group("検証環境A"))
        self.assertTrue(
            cm.set_group_auto_commands("検証環境A", ["terminal length 0", "show clock"]))

        from core.config_manager import ConfigManager
        cm2 = ConfigManager(config_path=os.path.join(d, "config.json"))
        self.assertEqual(
            cm2.get_group("検証環境A")["auto_commands"],
            ["terminal length 0", "show clock"])

    def test_empty_list_is_allowed(self):
        cm, _ = self._new_manager()
        cm.add_group("空グループ", ["show version"])
        self.assertTrue(cm.set_group_auto_commands("空グループ", []))
        self.assertEqual(cm.get_group("空グループ")["auto_commands"], [])

    def test_unknown_group_returns_false(self):
        cm, _ = self._new_manager()
        self.assertFalse(cm.set_group_auto_commands("存在しない", ["show clock"]))

    def test_does_not_depend_on_get_group_returning_a_reference(self):
        """get_group がコピーを返すようになっても壊れないこと。"""
        import copy
        cm, _ = self._new_manager()
        cm.add_group("参照非依存")
        original_get_group = cm.get_group
        cm.get_group = lambda name: copy.deepcopy(original_get_group(name))
        self.assertTrue(cm.set_group_auto_commands("参照非依存", ["show ip int br"]))
        cm.get_group = original_get_group
        self.assertEqual(cm.get_group("参照非依存")["auto_commands"], ["show ip int br"])

    def test_rename_group_keeps_auto_commands(self):
        cm, _ = self._new_manager()
        cm.add_group("旧名", ["terminal monitor"])
        self.assertTrue(cm.rename_group("旧名", "新名"))
        self.assertEqual(cm.get_group("新名")["auto_commands"], ["terminal monitor"])


if __name__ == "__main__":
    unittest.main()
