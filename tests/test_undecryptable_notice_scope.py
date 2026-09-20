"""復号できなかったパスワードの案内が、機器の話をしてよい場面か確かめる回帰テスト。

実測（ac1dee7）: 復号失敗の件数（_undecryptable_count）は、機器のパスワードと
settings の ftp_server / sftp_server のパスワードをひとまとめに数えていた。
機器が 1 台も絡まず、settings.sftp_server.password にだけ他 PC の DPAPI 値が
入っている config.json でも、起動時に『1件のパスワードを復号できませんでした…
該当機器のパスワードは、機器の編集で入れ直してください』と画面に出る。
記録（undecryptable_devices）は空のままなので、言われたとおり機器の編集を開いても
直すところが無い。あわせて、平文の合言葉 "DPAPI:cisco123" のように復号を要らない
値も件数に入るのに接続は断られないため、案内の件数と実際に断られる機器の数も
食い違っていた。

直し方: 件数を機器由来と settings 由来で分け、文面を書き分ける。機器は「機器の
編集で入れ直してください」、内蔵サーバの設定は、その設定のタブで入れ直す案内にする。
機器側の件数は、接続を断る条件（本物の DPAPI 暗号文）と同じ数え方にそろえる。
"""
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

# DPAPI の blob は必ずこの 20 バイト（version 1 + プロバイダ GUID）で始まる
_DPAPI_HEADER = bytes.fromhex("01000000d08c9ddf0115d1118c7a00c04fc297eb")

# 他の PC / アカウントで作られた、この PC では復号できない DPAPI 値
FOREIGN = "DPAPI:" + base64.b64encode(
    _DPAPI_HEADER + bytes(range(256))).decode("ascii")

# 形は DPAPI 風だが中身は平文。本当のパスワードなので接続は断られない
LOOKALIKE = "DPAPI:cisco123"


def _device(name, password):
    return {"name": name, "host": "192.0.2.10", "port": 22,
            "username": "admin", "password": password, "protocol": "ssh"}


def _config(devices, settings=None):
    return {
        "config_version": "1.0",
        "groups": [{"name": "Default", "auto_commands": [],
                    "devices": list(devices)}],
        "global_macros": [],
        "settings": settings or {},
    }


class UndecryptableNoticeScopeTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-notice-scope-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config_manager(self, devices, settings=None):
        from core.config_manager import ConfigManager
        path = self.dir / "config.json"
        path.write_text(json.dumps(_config(devices, settings)),
                        encoding="utf-8")
        return ConfigManager(config_path=str(path))

    def test_a_settings_only_failure_does_not_send_the_user_to_the_device_editor(self):
        """機器が絡まないなら、機器の編集を案内しないこと。"""
        cm = self._config_manager(
            [_device("rtr1", "cisco123")],
            {"sftp_server": {"username": "u", "password": FOREIGN}})

        self.assertEqual(cm.undecryptable_devices, {},
                         "前提: 復号できなかった機器は 1 台も無い")
        self.assertIsNotNone(cm.load_warning, "設定側の失敗が知らされていない")
        self.assertNotIn("機器の編集", cm.load_warning,
                         "機器が絡まないのに機器の編集を案内している")
        self.assertIn("SFTPサーバー", cm.load_warning,
                      "どの設定を直せばよいか分からない")

    def test_a_device_failure_still_points_at_the_device_editor(self):
        """機器側の失敗は、これまでどおり機器の編集を案内すること。"""
        cm = self._config_manager([_device("rtr1", FOREIGN)])

        self.assertIsNotNone(cm.load_warning)
        self.assertIn("機器の編集", cm.load_warning)
        self.assertNotIn("SFTPサーバー", cm.load_warning)

    def test_both_kinds_are_reported_separately(self):
        """機器と設定の両方が失敗したら、両方を書き分けること。"""
        cm = self._config_manager(
            [_device("rtr1", FOREIGN)],
            {"ftp_server": {"username": "u", "password": FOREIGN}})

        self.assertIn("機器の編集", cm.load_warning)
        self.assertIn("FTPサーバー", cm.load_warning)

    def test_the_device_count_matches_the_number_that_will_be_refused(self):
        """接続を断られない平文の合言葉を、件数に入れないこと。"""
        cm = self._config_manager([_device("rtr-bad", FOREIGN),
                                   _device("rtr-ok", LOOKALIKE)])

        self.assertEqual(list(cm.undecryptable_devices), ["rtr-bad"],
                         "前提: 断られるのは 1 台だけ")
        self.assertIn("1件", cm.load_warning)
        self.assertNotIn("2件", cm.load_warning)

    def test_nothing_is_said_when_every_password_decrypts(self):
        """復号できなかったものが無ければ、何も出さないこと。"""
        cm = self._config_manager(
            [_device("rtr1", "cisco123")],
            {"ftp_server": {"username": "u", "password": "secret"}})

        self.assertIsNone(cm.load_warning)


if __name__ == "__main__":
    unittest.main()
