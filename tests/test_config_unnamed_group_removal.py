"""名前の無いグループへ補う表示名が、同名グループを作らないことを検証する。

_quarantine_invalid_devices は name の無いグループすべてに同じ
UNNAMED_GROUP_NAME を与える。一方 get_group() は先頭一致で 1 件だけ返し、
remove_group() は同名を全部消す。この非対称のため、MainWindow._on_delete_group
が「グループに機器が含まれています」の確認を通した空のグループを削除すると、
同じ表示名を持つ別のグループの機器まで一緒に消える。

補う名前に通し番号を付けて同名を作らないことと、remove_group() が
（手編集で作られた本当の同名グループに対しても）1 件しか消さないことを確かめる。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

VALID = {
    "name": "ルータA", "host": "192.0.2.1", "port": 22, "protocol": "ssh",
    "username": "admin", "password": "", "ssh_key": "", "macros": [],
}


def _write_groups(path, groups):
    config = {
        "config_version": "1.0",
        "groups": groups,
        "global_macros": [],
        "settings": {},
        "update_settings": {"check_on_startup": False},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)


class ConfigManagerGroupNameTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-groupname-")
        self.path = os.path.join(self.dir, "config.json")
        home = mock.patch("core.config_manager.app_data_dir",
                          return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)

    def _manager(self, groups):
        from core.config_manager import ConfigManager
        _write_groups(self.path, groups)
        return ConfigManager(config_path=self.path)


class UnnamedGroupsGetDistinctNamesTest(ConfigManagerGroupNameTestBase):
    def test_filled_in_names_are_all_different(self):
        cm = self._manager([
            {"name": "Default", "auto_commands": [], "devices": []},
            {"auto_commands": [], "devices": []},               # name が無い
            {"auto_commands": [], "devices": [VALID]},           # name が無い
            {"name": "", "devices": []},                         # name が空
        ])

        names = [g["name"] for g in cm.get_groups()]
        self.assertEqual(len(set(names)), len(names),
                         f"同名のグループが残っている: {names}")
        from core.config_manager import ConfigManager
        self.assertEqual(names[1], ConfigManager.UNNAMED_GROUP_NAME)
        for name in names[1:]:
            self.assertIn(ConfigManager.UNNAMED_GROUP_NAME, name)

    def test_filled_in_names_avoid_a_group_that_already_uses_that_name(self):
        cm = self._manager([
            {"name": "Default", "auto_commands": [], "devices": []},
            {"name": "(名前なし)", "auto_commands": [], "devices": [VALID]},
            {"auto_commands": [], "devices": []},                # name が無い
        ])

        names = [g["name"] for g in cm.get_groups()]
        self.assertEqual(len(set(names)), len(names),
                         f"補った名前が既存のグループ名と衝突している: {names}")

    def test_deleting_an_empty_unnamed_group_keeps_the_other_ones_devices(self):
        """MainWindow._on_delete_group と同じ順番（確認してから削除）でなぞる。"""
        cm = self._manager([
            {"name": "Default", "auto_commands": [], "devices": []},
            {"auto_commands": [], "devices": []},                # 空。これを消す
            {"auto_commands": [], "devices": [VALID]},           # 機器あり。残す
        ])

        target = cm.get_groups()[1]["name"]
        keeper = cm.get_groups()[2]["name"]
        # UI はグループ名で引き直してから機器数を確かめる
        self.assertEqual(cm.get_group(target)["devices"], [])
        self.assertTrue(cm.remove_group(target))

        remaining = cm.get_groups()
        self.assertEqual([g["name"] for g in remaining], ["Default", keeper])
        self.assertEqual(remaining[1]["devices"], [VALID],
                         "別グループの機器まで消えている")


class RemoveGroupRemovesOnlyOneGroupTest(ConfigManagerGroupNameTestBase):
    def test_only_the_group_that_get_group_returns_is_removed(self):
        """手編集で作られた本当の同名グループでも、消すのは 1 件だけ。"""
        cm = self._manager([
            {"name": "Default", "auto_commands": [], "devices": []},
            {"name": "重複", "auto_commands": [], "devices": []},
            {"name": "重複", "auto_commands": [], "devices": [VALID]},
        ])

        self.assertTrue(cm.remove_group("重複"))

        remaining = cm.get_groups()
        self.assertEqual([g["name"] for g in remaining], ["Default", "重複"])
        self.assertEqual(remaining[1]["devices"], [VALID],
                         "同名の別グループまで消えている")

    def test_removing_a_group_that_does_not_exist_leaves_the_others_alone(self):
        cm = self._manager([
            {"name": "Default", "auto_commands": [], "devices": [VALID]},
        ])

        cm.remove_group("存在しない")

        self.assertEqual([g["name"] for g in cm.get_groups()], ["Default"])
        self.assertEqual(cm.get_groups()[0]["devices"], [VALID])


if __name__ == "__main__":
    unittest.main()
