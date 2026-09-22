"""SNMP GET/WALK の「ポート」欄がホストの下の行にあること（G4）の回帰テスト。

何が起きていたか（実測、基準 1a206c2）: 接続設定の QGridLayout は 0 行目に
`ホスト: [____] ポート: [__]` を横一列に並べていた（ホストが 0 行 1 列、
ポートが 0 行 3 列）。ホスト欄は横に伸びるので、窓を縮めるとポートが右へ
押し出される。幅 520 で描くとポート欄の右端は x=653 で、画面の右端より
133px 外側にあり、利用者からは見えなかった。

利用者の決定（2026-09-23）: ポートはホストの下の行へ移す。バージョンの行は
さらにその下。ラベルの列は揃える。

どう直したか: 0 行目をホスト、1 行目をポート、2 行目をバージョンにした。
ラベルはすべて 0 列、入力はすべて 1 列に置いたので、3 つの入力の左端が
そろう。ポート欄は他のサーバーパネル（FTP/TFTP/SFTP の port_spin）と同じく
幅 100px までにして、1 列いっぱいに間延びしないようにした。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")


class SNMPPortBelowHostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self, width=520):
        from core.config_manager import ConfigManager
        from ui.snmp_panel import SNMPPanel
        d = tempfile.mkdtemp(prefix="netbelt-snmpport-")
        panel = SNMPPanel(config_manager=ConfigManager(
            config_path=os.path.join(d, "config.json")))
        self.addCleanup(panel.close)
        panel.main_tabs.setCurrentIndex(0)
        panel.resize(width, 900)
        panel.show()
        self.addCleanup(panel.hide)
        self.app.processEvents()
        return panel

    @staticmethod
    def _cell(widget):
        """widget が置かれている QGridLayout の (行, 列) を返す。"""
        layout = widget.parentWidget().layout()
        row, column, _, _ = layout.getItemPosition(layout.indexOf(widget))
        return row, column

    def test_port_sits_on_the_row_below_the_host(self):
        panel = self._panel()
        host_row, _ = self._cell(panel.host_edit)
        port_row, _ = self._cell(panel.port_spinbox)
        version_row, _ = self._cell(panel.version_combo)
        self.assertGreater(
            port_row, host_row,
            "ポート欄がホスト欄と同じ行のままです (host=%d port=%d)"
            % (host_row, port_row))
        self.assertGreater(
            version_row, port_row,
            "バージョンの行がポートより上にあります (port=%d version=%d)"
            % (port_row, version_row))

    def test_the_three_inputs_share_one_column(self):
        panel = self._panel()
        columns = {
            "ホスト": self._cell(panel.host_edit)[1],
            "ポート": self._cell(panel.port_spinbox)[1],
            "バージョン": self._cell(panel.version_combo)[1],
        }
        self.assertEqual(
            len(set(columns.values())), 1,
            "ラベルの列がそろっていません: %r" % (columns,))
        lefts = set()
        for name in ("host_edit", "port_spinbox", "version_combo"):
            widget = getattr(panel, name)
            lefts.add(widget.mapTo(panel, widget.rect().topLeft()).x())
        self.assertEqual(
            len(lefts), 1, "3 つの入力の左端がそろっていません: %r" % (lefts,))

    def test_port_is_not_pushed_to_the_right_of_the_host_field(self):
        """ホスト欄が伸びてもポートが右へ押し出されないこと。

        以前はホスト欄の右隣に置かれていたため、幅 520 ではポート欄の右端が
        x=491、ホスト欄の右端が x=378 と、ポートだけが 113px 右に出ていた。
        窓の幅が足りない場面ではこの右側から順に切れて見えなくなる。
        """
        panel = self._panel(width=520)
        spin = panel.port_spinbox
        host = panel.host_edit
        port_right = spin.mapTo(panel, spin.rect().bottomRight()).x()
        host_right = host.mapTo(panel, host.rect().bottomRight()).x()
        self.assertLessEqual(
            port_right, host_right,
            "ポート欄がホスト欄より右へ出ています (port_right=%d host_right=%d)"
            % (port_right, host_right))
        self.assertLessEqual(
            port_right, panel.width(),
            "幅 520 でポート欄が画面の右端からはみ出しています (right=%d width=%d)"
            % (port_right, panel.width()))


if __name__ == "__main__":
    unittest.main()
