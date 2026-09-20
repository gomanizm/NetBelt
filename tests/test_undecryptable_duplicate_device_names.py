"""同じ名前の機器が 2 グループにあると、片方へ暗号文がそのまま送られる件の回帰テスト。

実測（ac1dee7）: ConfigManager が覚える「復号できなかった機器」は機器名だけを
キーにした辞書なので、拠点1/rtr1 と 拠点2/rtr1 のように同じ名前が 2 つあると、
後から読んだ方の暗号文しか残らない。機器名の重複を弾くのは add_device /
update_device だけで、_quarantine_invalid_devices は重複を見ないため、手編集・
他ツール由来・他 PC から持ち込んだ config.json では重複がそのまま読み込まれる。
起動時の案内は「2件」と出るのに記録は 1 件だけで、先に読んだ拠点1/rtr1 は
SSHConnection が作られ 'DPAPI:...' がパスワードとして機器へ送られていた。

直し方: 名前だけでなく値でも照合する。読み込み時に出会った本物の DPAPI 暗号文を
集合にも貯め、has_undecryptable_password は名前の記録に当たらなくても「いま持って
いる値がその集合にある」なら暗号文のままと判断する。機器の編集で入れ直した新しい
パスワードはこの集合に入らないので、「入れ直せば断られなくなる」性質は変わらない。
あわせて、同じ名前の機器が複数ある設定を読んだら、その名前を挙げて知らせる。
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


def _foreign(seed):
    """他の PC / アカウントで作られた体の、この PC では復号できない DPAPI 値"""
    return "DPAPI:" + base64.b64encode(
        _DPAPI_HEADER + bytes([seed]) * 300).decode("ascii")


CIPHER_A = _foreign(1)
CIPHER_B = _foreign(2)


def _duplicate_name_config():
    return {
        "config_version": "1.0",
        "groups": [
            {"name": "Default", "auto_commands": [], "devices": []},
            {"name": "拠点1", "auto_commands": [], "devices": [
                {"name": "rtr1", "host": "192.0.2.11", "port": 22,
                 "username": "admin", "password": CIPHER_A,
                 "protocol": "ssh"}]},
            {"name": "拠点2", "auto_commands": [], "devices": [
                {"name": "rtr1", "host": "192.0.2.12", "port": 22,
                 "username": "admin", "password": CIPHER_B,
                 "protocol": "ssh"}]},
        ],
        "global_macros": [],
        "settings": {},
    }


class DuplicateDeviceNameUndecryptableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-dupname-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config_manager(self):
        from core.config_manager import ConfigManager
        path = self.dir / "config.json"
        path.write_text(json.dumps(_duplicate_name_config()),
                        encoding="utf-8")
        return ConfigManager(config_path=str(path))

    def _window(self, config_manager):
        from ui.main_window import MainWindow
        with mock.patch("ui.main_window.ConfigManager",
                        return_value=config_manager), \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"), \
                mock.patch("ui.main_window.QMessageBox.warning"):
            window = MainWindow()
        self.addCleanup(window.close)
        return window

    def _device(self, config_manager, group_name):
        group = config_manager.get_group(group_name)
        return dict(group["devices"][0])

    def _connect(self, window, group_name):
        """その機器の接続を要求し、(SSHConnection のモック, warning のモック)"""
        device = self._device(window.config_manager, group_name)
        # 同名の機器を続けて試せるように、前回の接続登録は残さない
        window.connections.pop(device["name"], None)
        with mock.patch("ui.main_window.SSHConnection") as ssh, \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window._on_connect_requested(device)
        return ssh, warn

    def test_both_entries_of_a_duplicate_name_are_recognized(self):
        """同じ名前が 2 つあっても、両方とも暗号文のままと分かること。"""
        cm = self._config_manager()
        for group_name, cipher in (("拠点1", CIPHER_A), ("拠点2", CIPHER_B)):
            device = self._device(cm, group_name)
            self.assertEqual(device["password"], cipher,
                             "前提: 復号に失敗して暗号文がメモリに残っている")
            self.assertTrue(
                cm.has_undecryptable_password(device["name"],
                                              device["password"]),
                f"{group_name}/{device['name']} を暗号文のままと見なしていない")

    def test_the_first_duplicate_is_not_connected_with_the_ciphertext(self):
        """先に読んだ方の機器へ、暗号文をパスワードとして送らないこと。"""
        window = self._window(self._config_manager())

        ssh, warn = self._connect(window, "拠点1")

        ssh.assert_not_called()
        self.assertNotIn("rtr1", window.connections)
        warn.assert_called_once()
        self.assertIn("復号できません", warn.call_args[0][2])

    def test_the_second_duplicate_is_not_connected_with_the_ciphertext(self):
        """後から読んだ方の機器も、これまでどおり断ること。"""
        window = self._window(self._config_manager())

        ssh, warn = self._connect(window, "拠点2")

        ssh.assert_not_called()
        warn.assert_called_once()

    def test_the_duplicate_name_is_reported_at_startup(self):
        """同じ名前の機器がある設定は、その名前を挙げて知らせること。"""
        cm = self._config_manager()
        self.assertIsNotNone(cm.load_warning, "起動時の案内が何も出ていない")
        self.assertIn("rtr1", cm.load_warning,
                      "重複している機器名が案内に出ていない")
        self.assertIn("同じ名前", cm.load_warning)

    def test_re_entering_one_password_frees_only_that_device(self):
        """片方だけ入れ直したら、その機器だけ繋がり、もう片方は断ること。"""
        window = self._window(self._config_manager())
        device = self._device(window.config_manager, "拠点1")
        self.assertTrue(window.config_manager.update_device(
            "拠点1", "rtr1", "拠点1", dict(device, password="cisco123")))

        ssh, warn = self._connect(window, "拠点1")
        ssh.assert_called_once()
        self.assertEqual(ssh.call_args[0][3], "cisco123")
        warn.assert_not_called()

        ssh2, warn2 = self._connect(window, "拠点2")
        ssh2.assert_not_called()
        warn2.assert_called_once()


if __name__ == "__main__":
    unittest.main()
