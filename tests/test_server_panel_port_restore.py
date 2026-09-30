"""config.json のサーバー設定のポートが手書きで壊れていても、FTP / TFTP のパネルが作れること。

何が起きていたか（実測、ebbe593。ネットワークは使わない）: config.json の
settings.ftp_server の port / passive_low / passive_high と settings.tftp_server の
port に 2121.0 や 1e309 を手で書くと、json はそれを float（1e309 は inf）に読む。
パネルの _restore_settings はその値をそのまま QSpinBox.setValue へ渡しており、
'TypeError: setValue(self, val: int): argument 1 has unexpected type 'float''
でパネルの構築が失敗した。パネルは MainWindow.__init__ から作られるので、値が
残っている限りアプリが起動しない（1.3.2 で直した Syslog の max_messages の
無限大と同じ種類で、別の経路）。文字列の "2121" も同じ TypeError になった。
範囲外の整数（70000、passive の 80 など）は QSpinBox が黙って端（65535 / 1024）へ
寄せ、設定していない番号で待ち受ける状態になっていた。

どう直したか: ポートの値は SSH・Telnet の接続と同じ読み方
（core.sockets.tcp_port_number: 前後の空白を許す整数の文字列と端数の無い数は
整数へそろえ、bool・端数のある数・無限大・非数・1〜65535 の外は読まない）で
整数にし、さらにその欄の範囲（passive は 1024〜65535）に入るときだけ戻す。
使えない値は欄の既定値（FTP 21、passive 50100 / 50150、TFTP 69）のまま起動する。
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

FTP_DEFAULTS = {"port": 21, "passive_low": 50100, "passive_high": 50150}
TFTP_DEFAULTS = {"port": 69}


def _config_text(section, field, literal, extra=None):
    """settings.<section>.<field> に literal（JSON の字面そのまま）を書いた config.json の中身。

    extra（欄の名前 → JSON の字面）があれば、同じセクションに並べて書く
    """
    fields = '"%s": %s' % (field, literal)
    for name, value in (extra or {}).items():
        fields += ', "%s": %s' % (name, value)
    return ('{"config_version": "1.0", "groups": [{"name": "Default", '
            '"auto_commands": [], "devices": []}], "global_macros": [], '
            '"settings": {"%s": {"root_directory": "./example-root", %s}}, '
            '"update_settings": {"check_on_startup": false}}'
            % (section, fields))


class ServerPanelPortRestoreTest(unittest.TestCase):
    _keep = []   # 配送待ちシグナルの宛先を先に解放しない（他テストと同じ理由）

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.app.processEvents()
        cls._keep.clear()

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-panel-port-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        home = os.path.join(self.dir, "home")
        os.mkdir(home)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=Path(home))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _panel(self, section, field, literal, extra=None):
        from core.config_manager import ConfigManager
        from ui.ftp_server_panel import FTPServerPanel
        from ui.tftp_server_panel import TFTPServerPanel
        if field == "passive_high" and extra is None:
            # 前提: passive_high だけを書くと passive_low は既定値 50100 のままで、
            # それより下の番号（2121 など）は範囲の逆転として組ごと既定値へ戻る
            # （test_inverted_passive_range_falls_back_to_the_default_pair）。
            # 欄ごとの読み方を確かめるため、passive_low には欄の下端 1024 を置く
            extra = {"passive_low": "1024"}
        path = os.path.join(self.dir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(_config_text(section, field, literal, extra))
        cm = ConfigManager(config_path=path)
        # 前提: 読み込みの経路（_load_config）を通っても値が残っている
        self.assertIsNone(cm.load_error)
        for name in [field] + list(extra or {}):
            self.assertIn(name, cm.get_server_settings(section))
        cls = FTPServerPanel if section == "ftp_server" else TFTPServerPanel
        panel = cls(config_manager=cm)
        self._keep.append(panel)
        return panel

    @staticmethod
    def _spin(panel, field):
        return {"port": panel.port_spin,
                "passive_low": getattr(panel, "passive_lo_spin", None),
                "passive_high": getattr(panel, "passive_hi_spin", None)}[field]

    def _cases(self):
        for field in FTP_DEFAULTS:
            yield "ftp_server", field, FTP_DEFAULTS[field]
        yield "tftp_server", "port", TFTP_DEFAULTS["port"]

    def test_unusable_port_falls_back_to_the_default(self):
        for section, field, default in self._cases():
            for literal in ("1e309", "-1e309", "2121.5", "true", '"abc"',
                            "70000", "-5", "[2121]", "{}"):
                with self.subTest(section=section, field=field, literal=literal):
                    panel = self._panel(section, field, literal)
                    self.assertEqual(self._spin(panel, field).value(), default)

    def test_whole_number_float_and_numeric_string_are_read_as_the_port(self):
        for section, field, _default in self._cases():
            for literal in ("2121.0", '"2121"', '" 2121 "', "2121"):
                with self.subTest(section=section, field=field, literal=literal):
                    panel = self._panel(section, field, literal)
                    self.assertEqual(self._spin(panel, field).value(), 2121)

    def test_passive_port_below_the_field_range_falls_back_to_the_default(self):
        # 1〜65535 の番号でも、passive の欄（1024〜65535）の外なら既定値へ戻す。
        # 黙って 1024 へ寄せると、設定していない番号で待ち受ける
        for field in ("passive_low", "passive_high"):
            with self.subTest(field=field):
                panel = self._panel("ftp_server", field, "80")
                self.assertEqual(self._spin(panel, field).value(),
                                 FTP_DEFAULTS[field])

    def _passive_pair(self, low, high):
        """passive_low / passive_high に low / high（JSON の字面）を書いて FTP パネルを作り、欄の組を返す"""
        panel = self._panel("ftp_server", "passive_low", low,
                            {"passive_high": high})
        return panel.passive_lo_spin.value(), panel.passive_hi_spin.value()

    def test_inverted_passive_range_falls_back_to_the_default_pair(self):
        # 片方だけを既定値へ戻すと範囲が逆転しうる（60000 と 70000 は 60000-50150）。
        # 逆転した範囲で起動すると、PASV のときに制御接続ごと切られ、理由は
        # パネルにもコンソールにも出ない（転送が黙って失敗する）。組として
        # 使えないので、両方を既定値へ戻す。両方とも読める値で逆転しているとき
        # （60000 と 55000）も同じ
        default = (FTP_DEFAULTS["passive_low"], FTP_DEFAULTS["passive_high"])
        for low, high in (("60000", "70000"), ("60000", "65535.5"),
                          ("60000", "1e309"), ("1000", "40000"),
                          ("60000", "55000")):
            with self.subTest(low=low, high=high):
                self.assertEqual(self._passive_pair(low, high), default)

    def test_usable_passive_range_is_kept(self):
        # 逆転していなければ、これまでどおり欄ごとに戻す（片方だけ既定値でもよい）
        for low, high, expected in (("60000", "65535", (60000, 65535)),
                                    ("40000", "45000", (40000, 45000)),
                                    ("50200", "50200", (50200, 50200)),
                                    ("1000", "60000", (50100, 60000)),
                                    ("40000", "70000", (40000, 50150))):
            with self.subTest(low=low, high=high):
                self.assertEqual(self._passive_pair(low, high), expected)

    def test_other_settings_are_still_restored(self):
        """ポートが使えなくても、同じセクションのほかの設定は読み込まれる"""
        panel = self._panel("ftp_server", "port", "1e309")
        self.assertEqual(panel.root_dir_edit.text(), "./example-root")
        panel = self._panel("tftp_server", "port", "2121.0")
        self.assertEqual(panel.root_dir_edit.text(), "./example-root")


if __name__ == "__main__":
    unittest.main()
