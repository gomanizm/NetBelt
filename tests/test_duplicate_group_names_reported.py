"""同じ名前のグループが複数ある設定を読んだら知らせることを確かめる回帰テスト。

実測（f83be17）: get_group() は先頭の 1 件しか返さないので、groups に同じ name の
グループが 2 つあると、2 つ目のグループの機器はどの操作からも届かない。kyoten を
2 つ置き、2 つ目にだけ居る rtr2 に対して update_device("kyoten", "rtr2", ...) も
remove_device("kyoten", "rtr2") も False を返す（どちらも get_group() が返した
1 つ目のグループしか見ないため、機器が見つからない）。move_device も同じ。
それでいて読み込み時の警告は何も出ず（load_warning is None）、画面には機器が
並んでいるので、利用者には「編集を押しても何も起きない」としか見えなかった。

機器名の重複はすでに _notify_duplicate_device_names() が名指しで知らせていたので、
グループ名の重複も同じ形で知らせる。設定は勝手に書き換えない（黙って片方を消すと
利用者の機器が消える）。グループ名の変更 rename_group() も get_group() 経由で
1 つ目しか掴まないため、案内は config.json を直接直すよう書く。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")


def _device(name, host):
    return {"name": name, "host": host, "port": 22, "protocol": "ssh",
            "username": "admin", "password": "", "ssh_key": "", "macros": []}


def _group(name, devices):
    return {"name": name, "auto_commands": [], "devices": list(devices)}


class DuplicateGroupNamesReportedTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-dupgroup-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config_manager(self, groups):
        from core.config_manager import ConfigManager
        path = self.dir / "config.json"
        path.write_text(json.dumps({
            "config_version": "1.0",
            "groups": groups,
            "global_macros": [],
            "settings": {},
        }), encoding="utf-8")
        return ConfigManager(config_path=str(path))

    def _duplicated(self):
        return self._config_manager([
            _group("Default", []),
            _group("kyoten", [_device("rtr1", "192.0.2.11")]),
            _group("kyoten", [_device("rtr2", "192.0.2.12")]),
        ])

    def test_the_duplicate_group_name_is_reported_at_startup(self):
        """重複しているグループ名を名指しで知らせること。"""
        cm = self._duplicated()

        self.assertIsNotNone(cm.load_warning, "起動時の案内が何も出ていない")
        self.assertIn("kyoten", cm.load_warning,
                      "重複しているグループ名が案内に出ていない")
        self.assertIn("同じ名前のグループ", cm.load_warning)

    def test_the_second_group_is_still_unreachable_so_the_notice_matters(self):
        """前提の確認: 2 つ目のグループの機器へは、どの操作も届かないこと。"""
        cm = self._duplicated()

        self.assertEqual([d["name"] for d in cm.get_group("kyoten")["devices"]],
                         ["rtr1"], "get_group が先頭以外も返すようになった")
        self.assertFalse(cm.update_device("kyoten", "rtr2", "kyoten",
                                          _device("rtr2", "192.0.2.99")))
        self.assertFalse(cm.remove_device("kyoten", "rtr2"))

    def test_the_config_file_is_not_rewritten(self):
        """知らせるだけで、重複を勝手に消したり改名したりしないこと。"""
        cm = self._duplicated()

        names = [g["name"] for g in cm.get_groups()]
        self.assertEqual(names, ["Default", "kyoten", "kyoten"],
                         "メモリ上のグループが書き換えられている")
        on_disk = json.loads(Path(cm.config_path).read_text(encoding="utf-8"))
        self.assertEqual([g["name"] for g in on_disk["groups"]],
                         ["Default", "kyoten", "kyoten"],
                         "設定ファイルが書き換えられている")

    def test_every_duplicated_name_is_listed_once(self):
        """3 つ以上あっても、重複した名前を 1 回ずつ挙げること。"""
        cm = self._config_manager([
            _group("Default", []),
            _group("A", []), _group("A", []), _group("A", []),
            _group("B", []), _group("B", []),
        ])

        line = next(ln for ln in cm.load_warning.splitlines()
                    if "同じ名前のグループ" in ln)
        self.assertEqual(line.count("A"), 1, f"A が 1 回でない: {line}")
        self.assertEqual(line.count("B"), 1, f"B が 1 回でない: {line}")

    def test_nothing_is_said_when_group_names_are_unique(self):
        """重複が無ければ、グループの話はしないこと。"""
        cm = self._config_manager([
            _group("Default", []),
            _group("kyoten1", [_device("rtr1", "192.0.2.11")]),
            _group("kyoten2", [_device("rtr2", "192.0.2.12")]),
        ])

        self.assertIsNone(cm.load_warning)

    def test_the_device_name_notice_is_not_disturbed(self):
        """機器名の重複の案内は、これまでどおり出ること。"""
        cm = self._config_manager([
            _group("Default", []),
            _group("kyoten1", [_device("rtr1", "192.0.2.11")]),
            _group("kyoten2", [_device("rtr1", "192.0.2.12")]),
        ])

        self.assertIn("同じ名前の機器", cm.load_warning)
        self.assertNotIn("同じ名前のグループ", cm.load_warning)


if __name__ == "__main__":
    unittest.main()
