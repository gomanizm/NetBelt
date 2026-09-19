"""復号できないパスワードを、そのまま SSH のパスワードとして機器へ送らないこと。

実測（8b0c94e、localhost の paramiko サーバで確認）: 別の PC・別アカウントで
作られた config.json を読むと、crypto.decrypt() が暗号文をそのまま返し、
ConfigManager は件数を print するだけで device['password'] に暗号文を残す。
その機器へ接続すると 'DPAPI:AQAAANCMnd8...' が認証のパスワードとして送られ、
機器側は『ユーザー名またはパスワードが間違っています』を返す。Enter で
繋ぎ直すたびに同じ暗号文で試すので、機器側の認証失敗回数（AAA の
ロックアウトなど）にも数えられうる。起動時の案内も print だけで、画面には
何も出ていなかった。

直し方: 読み込み時に復号へ失敗した機器を ConfigManager が覚え、SSH は
SSHConnection を作る前に断る（機器側へは何も送らない）。判定は
is_encrypted の推測ではなく、この記録と、いま持っている値が記録した
暗号文と同じかで行うので、平文で "DPAPI:cisco123" のように見えるだけの
値は断らない。記録は、機器の編集でパスワードを入れ直して保存したら外れる。
起動時の件数は load_warning にも載せて画面に出す。Telnet は自動ログインを
しないので対象外。
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

# DPAPI の blob は必ずこの 20 バイト（version 1 + プロバイダ GUID）で始まる。
# 実測: PasswordCrypto().encrypt() の出力を base64 復号した先頭が常にこれ。
_DPAPI_HEADER = bytes.fromhex("01000000d08c9ddf0115d1118c7a00c04fc297eb")

# 他の PC / アカウントで作られた、この PC では復号できない DPAPI 値
FOREIGN = "DPAPI:" + base64.b64encode(
    _DPAPI_HEADER + bytes(range(256)) * 2).decode("ascii")

# 形は DPAPI 風だが中身は平文。これは本当のパスワードなので断らない
LOOKALIKE = "DPAPI:cisco123"


def _config(password, protocol="ssh"):
    return {
        "config_version": "1.0",
        "groups": [{"name": "Default", "auto_commands": [], "devices": [
            {"name": "rtr1", "host": "192.0.2.10", "port": 22,
             "username": "admin", "password": password,
             "protocol": protocol}]}],
        "global_macros": [],
        "settings": {},
    }


class UndecryptablePasswordNotSentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-undecryptable-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config_manager(self, password, protocol="ssh"):
        from core.config_manager import ConfigManager
        path = self.dir / "config.json"
        path.write_text(json.dumps(_config(password, protocol)),
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

    def _connect(self, window):
        """接続を要求し、(SSHConnection のモック, warning のモック) を返す"""
        device = dict(window.config_manager.get_groups()[0]["devices"][0])
        with mock.patch("ui.main_window.SSHConnection") as ssh, \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window._on_connect_requested(device)
        return ssh, warn

    def test_ssh_is_refused_before_the_connection_object_is_made(self):
        """暗号文のままの機器は、SSHConnection を作る前に断ること。"""
        window = self._window(self._config_manager(FOREIGN))
        self.assertEqual(
            window.config_manager.get_groups()[0]["devices"][0]["password"],
            FOREIGN, "前提: 復号に失敗して暗号文がメモリに残っている")

        ssh, warn = self._connect(window)

        ssh.assert_not_called()
        self.assertNotIn("rtr1", window.connections)
        warn.assert_called_once()
        message = warn.call_args[0][2]
        self.assertIn("復号できません", message)
        self.assertIn("入れ直して", message)

    def test_reconnecting_does_not_retry_with_the_ciphertext(self):
        """繋ぎ直しても同じ暗号文で認証を試さないこと（機器側の失敗回数）。"""
        window = self._window(self._config_manager(FOREIGN))
        for _ in range(3):
            ssh, _warn = self._connect(window)
            ssh.assert_not_called()

    def test_the_startup_notice_is_shown_on_screen(self):
        """復号できなかった件数を、print だけでなく画面にも出すこと。"""
        cm = self._config_manager(FOREIGN)
        self.assertIsNotNone(cm.load_warning,
                             "起動時の案内が load_warning に載っていない")
        self.assertIn("復号できません", cm.load_warning)

    def test_a_plaintext_that_only_looks_encrypted_is_still_connected(self):
        """平文の "DPAPI:cisco123" は本当のパスワードなので断らないこと。"""
        window = self._window(self._config_manager(LOOKALIKE))
        ssh, warn = self._connect(window)
        ssh.assert_called_once()
        self.assertEqual(ssh.call_args[0][3], LOOKALIKE)
        warn.assert_not_called()

    def test_the_refusal_goes_away_after_the_password_is_re_entered(self):
        """機器の編集でパスワードを入れ直して保存したら、記録を外すこと。"""
        window = self._window(self._config_manager(FOREIGN))
        device = dict(window.config_manager.get_groups()[0]["devices"][0])
        self.assertTrue(window.config_manager.update_device(
            "Default", "rtr1", "Default", dict(device, password="cisco123")))

        ssh, warn = self._connect(window)
        ssh.assert_called_once()
        self.assertEqual(ssh.call_args[0][3], "cisco123")
        warn.assert_not_called()

    def test_renaming_without_touching_the_password_stays_refused(self):
        """名前だけ変えても、暗号文のままなら断り続けること。"""
        window = self._window(self._config_manager(FOREIGN))
        device = dict(window.config_manager.get_groups()[0]["devices"][0])
        self.assertTrue(window.config_manager.update_device(
            "Default", "rtr1", "Default", dict(device, name="rtr2")))

        ssh, warn = self._connect(window)
        ssh.assert_not_called()
        warn.assert_called_once()

    def test_telnet_is_not_refused(self):
        """Telnet は自動ログインをしないので対象外（そのまま繋ぐ）。"""
        window = self._window(self._config_manager(FOREIGN, protocol="telnet"))
        device = dict(window.config_manager.get_groups()[0]["devices"][0])
        with mock.patch("ui.main_window.TelnetConnection") as telnet, \
                mock.patch("ui.main_window.QMessageBox.warning") as warn:
            window._on_connect_requested(device)
        telnet.assert_called_once()
        warn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
