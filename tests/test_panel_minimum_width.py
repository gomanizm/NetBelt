"""画面を縮めるとボタンが隠れる件（G2）の回帰テスト。

何が起きていたか（実測、基準 1a206c2）: SNMP パネルは窓をどれだけ縮めても
一定の幅より細くならず、はみ出した右側（エクスポート・クリア）が窓の外へ
出て押せなくなっていた。内訳は Trap 受信タブが幅を決めており、その中の
「v3 Trap は送信元機器の EngineID を登録しないと…」の説明ラベルが
折り返し無効のまま 1 行ぶんの幅を最小幅として要求していた。

実測の minimumSizeHint().width()（offscreen / 日本語フォント有り）:
  FTP 329 / TFTP 407 / SFTP 257 / Syslog 243 / SNMP 852
  （SNMP の内訳: Trap 受信タブ 838・GET/WALK タブ 362。
    838 のうち 796 が上記の折り返し無効ラベル）
日本語フォントを拾えない環境（QT_QPA_FONTDIR 未設定）では同じ原因で
SNMP 1160 / Trap 1146 になる。どちらの環境でも 520 を超える。

どう直したか: 折り返しの無い説明用ラベルに setWordWrap(True) を付けた。
併せて、長い文面が入りうる状態表示のラベル（SNMP の実行状況・Trap の受信
状態、Syslog の状況）も折り返す。これらは「途中まで: 123件（… のため中断。
全部ではありません）」やファイアウォール許可の結果など、その場で長い文字列
を流し込むので、折り返さないままだと利用者が操作した瞬間に最小幅が跳ね上がり、
同じ「ボタンが窓の外へ出る」状態へ戻る。

この 520 という上限は、縮めた画面でもボタン行が 1 行で収まる幅として
利用者の決定（2026-09-23）で決められた確認幅。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 縮めたときに収まっていてほしい幅（利用者の決定）
MAX_MIN_WIDTH = 520


class PanelMinimumWidthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        """一時フォルダの config.json を使う MainWindow を組む。"""
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-minwidth-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            window = MainWindow()
        self.addCleanup(window.close)
        return window

    def _assert_fits(self, name, widget):
        width = widget.minimumSizeHint().width()
        self.assertLessEqual(
            width, MAX_MIN_WIDTH,
            "%s の最小幅が %d px で、%d px まで縮められない"
            % (name, width, MAX_MIN_WIDTH))

    def test_every_panel_can_shrink_to_520(self):
        window = self._window()
        for name, attr in (("FTP サーバー", "ftp_server_panel"),
                           ("TFTP サーバー", "tftp_server_panel"),
                           ("SFTP サーバー", "sftp_server_panel"),
                           ("Syslog", "syslog_panel"),
                           ("SNMP", "snmp_panel")):
            with self.subTest(panel=name):
                self._assert_fits(name, getattr(window, attr))

    def test_both_snmp_tabs_can_shrink_to_520(self):
        window = self._window()
        tabs = window.snmp_panel.main_tabs
        for index in range(tabs.count()):
            with self.subTest(tab=tabs.tabText(index)):
                self._assert_fits("SNMP の %s タブ" % tabs.tabText(index),
                                  tabs.widget(index))

    def test_long_status_text_does_not_widen_the_panels(self):
        """長い状況表示を入れても最小幅が跳ね上がらないこと。"""
        window = self._window()
        long_text = ("途中まで: 128件（機器からの応答がありません のため中断。"
                     "全部ではありません）")
        window.snmp_panel.status_label.setText(long_text)
        window.snmp_panel.trap_status_label.setText(long_text)
        window.syslog_panel.status_label.setText(long_text)
        self._assert_fits("SNMP", window.snmp_panel)
        self._assert_fits("Syslog", window.syslog_panel)


if __name__ == "__main__":
    unittest.main()
