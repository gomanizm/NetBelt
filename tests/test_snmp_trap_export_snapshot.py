"""Trap の保存も、ダイアログを開いた時点の一覧を書き出すことを検証する。

GET/WALK 側 (_on_export_clicked) は行・ホスト・理由をダイアログの前に
固定するようになったが、Trap 側 (_on_trap_export_clicked) は書き出しの
中で self.trap_data_list を読み直したままだった。モーダルダイアログは
ネストしたイベントループで queued シグナルを処理するので、保存先を
選んでいる間に届いた Trap が

  - 押した時点で画面に無かったのに保存物へ入り、
  - max_traps の切り詰めで、押した時点に見えていた最古の Trap が
    保存物から消える

（実測: 192.0.2.10 が消えて 192.0.2.99 が入った）。
「今見えているものを保存した」はずのファイルが、実際には別物になる。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

OLD_IPS = ["192.0.2.10", "192.0.2.11", "192.0.2.12"]
NEW_IPS = ["192.0.2.97", "192.0.2.98", "192.0.2.99"]


def _trap(ip, tag):
    return {
        "source_ip": ip,
        "source_port": 162,
        "trap_oid": "1.3.6.1.4.1.99999.2.0.1",
        "varbinds": [{"oid": "1.3.6.1.2.1.1.5.0", "value": tag}],
    }


class SnmpTrapExportSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        """SNMPPanel を返す（単体生成は MIB 読み込みで落ちるため MainWindow 経由）。"""
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-trapsnap-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        cm.config.setdefault("settings", {})["snmp"] = {"max_traps": 3}
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            window = MainWindow()
        self.addCleanup(window.close)
        panel = window.snmp_panel
        for i, ip in enumerate(OLD_IPS):
            panel._add_trap_to_tree(_trap(ip, "old-%d" % i))
        return panel

    def _export_while_new_traps_arrive(self, panel, kind):
        """保存先を選んでいる間に新しい Trap が届く状況で書き出す。"""
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-trapsnap-out-"),
                            "out." + kind)

        def dialog_during_which_traps_arrive(*args, **kwargs):
            for i, ip in enumerate(NEW_IPS):
                panel._add_trap_to_tree(_trap(ip, "new-%d" % i))
            return path, ""

        with mock.patch("ui.snmp_panel.QFileDialog.getSaveFileName",
                        side_effect=dialog_during_which_traps_arrive), \
                mock.patch("ui.snmp_panel.QMessageBox"):
            panel._on_trap_export_clicked()
        with io.open(path, encoding="utf-8") as f:
            return f.read()

    def test_the_json_export_holds_the_traps_that_were_on_screen(self):
        panel = self._panel()
        data = json.loads(self._export_while_new_traps_arrive(panel, "json"))
        self.assertEqual([t["source_ip"] for t in data],
                         list(reversed(OLD_IPS)),
                         "押した時点の一覧と違うものが保存された: %s" % data)

    def test_the_csv_export_holds_the_traps_that_were_on_screen(self):
        panel = self._panel()
        text = self._export_while_new_traps_arrive(panel, "csv")
        self.assertIn(OLD_IPS[0], text,
                      "押した時点で見えていた最古の Trap が保存から消えた")
        for ip in NEW_IPS:
            self.assertNotIn(ip, text,
                             "押した時点に無かった Trap が保存された: %s" % ip)

    def test_the_text_export_holds_the_traps_that_were_on_screen(self):
        panel = self._panel()
        text = self._export_while_new_traps_arrive(panel, "txt")
        self.assertIn("総Trap数: 3", text)
        self.assertIn(OLD_IPS[0], text,
                      "押した時点で見えていた最古の Trap が保存から消えた")
        for ip in NEW_IPS:
            self.assertNotIn(ip, text,
                             "押した時点に無かった Trap が保存された: %s" % ip)

    def test_the_writers_require_the_snapshot(self):
        """書き出しが self を読み直せないこと（引数落ちを顕在化させる）。"""
        panel = self._panel()
        path = os.path.join(tempfile.mkdtemp(prefix="netbelt-trapsnap-arg-"),
                            "out.json")
        for kind in ("csv", "json", "txt"):
            with self.assertRaises(TypeError,
                                   msg="%s が self から読める形のまま" % kind):
                getattr(panel, "_export_to_" + kind)(path)


if __name__ == "__main__":
    unittest.main()
