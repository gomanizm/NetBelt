"""復号できなかった SFTP サーバーのパスワードの案内が、直せる場所を指しているか。

実測（f83be17）: settings.sftp_server.password に他 PC の DPAPI 値を置いた
config.json で起動すると、load_warning に『SFTPサーバーのパスワードを復号できません
でした。その画面で入れ直してください（表示メニューから開けます）。』と出る。
ところが SFTPServerPanel は config_manager を受け取らず
（src/ui/sftp_server_panel.py の __init__(self, parent=None)、呼び出しも
src/ui/main_window.py の SFTPServerPanel() で引数なし）、settings.sftp_server を
読みも書きもしない。案内どおり表示メニューからパネルを開いても、設定ファイルの
その値を直す場所が無い。FTP サーバーパネルだけが config_manager を受け取り、
_restore_settings / set_server_settings で settings.ftp_server を読み書きしている。

直し方: 「その画面で入れ直してください」と言ってよいのは、settings のパスワードを
実際に読み書きしている画面があるセクションだけなので、案内の対象を
_SETTING_SECTIONS_WITH_EDITOR として分けた。暗号化の対象
（_ENCRYPTED_SETTING_SECTIONS）は sftp_server を含んだままにして、平文のまま
ディスクへ残さない性質は変えない。復号できなかったこと自体は引き続き知らせるが、
文面は「この版では読まないので動作に影響しない」に書き分けた。

見張りの書き換え（2026-09-23）: 最後のテストは当初
SFTPServerPanel.__init__ の signature に config_manager が無いことで
「settings を読んでいない」を代用していた。サーバーのログのエクスポートも
保存先のフォルダを覚える（利用者の決定）ことになり、SFTP サーバーパネルも
他の 2 つと同じ形で config_manager を受け取るようになったため、この代用は
成り立たない。受け取っても使うのは保存先の記憶だけで、settings.sftp_server
は読みも書きもしないままなので、見張りを本来の条件
（get_server_settings / set_server_settings を呼んでいないこと）へ直した。

なお検査役の案は「案内の対象から外す（何も出さない）」だったが、それは既存の
tests/test_undecryptable_notice_scope.py の
test_a_settings_only_failure_does_not_send_the_user_to_the_device_editor が
期待する『load_warning が None でない・SFTPサーバー と出る』と食い違う。
既存テストの期待を変えてよい項目ではないため、警告は残したまま、直せない場所を
指す文言だけを取り除いた。
"""
import base64
import inspect
import json
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


def _config(settings):
    return {
        "config_version": "1.0",
        "groups": [{"name": "Default", "auto_commands": [], "devices": [
            {"name": "rtr1", "host": "192.0.2.10", "port": 22,
             "username": "admin", "password": "cisco123",
             "protocol": "ssh"}]}],
        "global_macros": [],
        "settings": settings,
    }


class SFTPServerPasswordNoticeTargetTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-sftp-notice-"))
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _config_manager(self, settings):
        from core.config_manager import ConfigManager
        path = self.dir / "config.json"
        path.write_text(json.dumps(_config(settings)), encoding="utf-8")
        return ConfigManager(config_path=str(path))

    def test_the_sftp_notice_does_not_send_the_user_to_a_panel_that_cannot_fix_it(self):
        """SFTP は入れ直す場所が無いので、入れ直せと言わないこと。"""
        cm = self._config_manager(
            {"sftp_server": {"username": "u", "password": FOREIGN}})

        self.assertIsNotNone(cm.load_warning, "設定側の失敗が知らされていない")
        self.assertIn("SFTPサーバー", cm.load_warning,
                      "どの設定の話か分からない")
        self.assertNotIn("入れ直してください", cm.load_warning,
                         "直す場所が無いのに入れ直せと案内している")
        self.assertNotIn("表示メニュー", cm.load_warning,
                         "開いても直せないパネルへ誘導している")
        self.assertIn("影響しません", cm.load_warning,
                      "放っておいてよいことが伝わらない")

    def test_the_ftp_notice_still_points_at_its_panel(self):
        """FTP サーバーパネルは settings を読み書きするので、案内は変えないこと。"""
        cm = self._config_manager(
            {"ftp_server": {"username": "u", "password": FOREIGN}})

        self.assertIn("FTPサーバー", cm.load_warning)
        self.assertIn("入れ直してください", cm.load_warning)
        self.assertIn("表示メニュー", cm.load_warning)

    def test_both_sections_are_written_separately(self):
        """両方失敗したら、直せる方だけに入れ直しを案内すること。"""
        cm = self._config_manager({
            "ftp_server": {"username": "u", "password": FOREIGN},
            "sftp_server": {"username": "u", "password": FOREIGN},
        })

        lines = cm.load_warning.splitlines()
        editable = [ln for ln in lines if "入れ直してください" in ln]
        self.assertEqual(len(editable), 1,
                         f"入れ直しの案内が 1 行でない: {lines}")
        self.assertIn("FTPサーバー", editable[0])
        self.assertNotIn("SFTPサーバー", editable[0],
                         "SFTP まで入れ直し先として案内している")

    def test_the_sftp_password_is_still_stored_encrypted(self):
        """案内から外しても、ディスクには平文で残さないこと。"""
        from core.config_manager import ConfigManager

        self.assertIn("sftp_server", ConfigManager._ENCRYPTED_SETTING_SECTIONS)

        cm = self._config_manager({})
        cm.set_server_settings("sftp_server", {"username": "u",
                                               "password": "secret"})
        raw = json.loads(Path(cm.config_path).read_text(encoding="utf-8"))
        stored = raw["settings"]["sftp_server"]["password"]
        self.assertNotEqual(stored, "secret",
                            "SFTP サーバーのパスワードが平文で保存されている")

    def test_the_notice_target_matches_which_panel_reads_settings(self):
        """どの画面が settings を読むかと、案内の対象を一致させておくこと。

        見張るのは「そのパネルが settings のセクションを読み書きするか」。
        config_manager を受け取っているかどうかでは代われない: SFTP
        サーバーパネルは、ログのエクスポート先（前回保存したフォルダ）を
        覚えるためだけに config_manager を受け取るようになったが、
        settings.sftp_server は読みも書きもしないままで、案内の対象として
        正しいのは変わらず FTP だけである。
        """
        import ui.ftp_server_panel
        import ui.sftp_server_panel
        from core.config_manager import ConfigManager

        ftp_source = inspect.getsource(ui.ftp_server_panel)
        self.assertIn('get_server_settings("ftp_server")', ftp_source,
                      "前提: FTP サーバーパネルは settings を読み書きする")
        self.assertIn("ftp_server",
                      ConfigManager._SETTING_SECTIONS_WITH_EDITOR)

        sftp_source = inspect.getsource(ui.sftp_server_panel)
        for call in ("get_server_settings", "set_server_settings"):
            self.assertNotIn(
                call, sftp_source,
                "SFTP サーバーパネルが settings を読み書きするようになった。"
                "_SETTING_SECTIONS_WITH_EDITOR へ sftp_server を戻すこと")
        self.assertNotIn("sftp_server",
                         ConfigManager._SETTING_SECTIONS_WITH_EDITOR)


if __name__ == "__main__":
    unittest.main()
