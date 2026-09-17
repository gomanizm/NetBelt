"""受信した Trap の保持件数に上限があることを検証する。

_add_trap_to_tree は受け取った Trap を trap_data_list と trap_tree_model
へ無制限に積んでいた。破棄はクリアボタンだけで、上限が無い。

同じアプリの Syslog パネルは SyslogTableModel(max_messages=1000) で
1000 件の上限を持ち、config.json の settings.syslog.max_messages から
変えられる。Trap 側だけ対策が無かった。

実測では、1件あたりの挿入コストも描画時間も件数に比例しては伸びない
（QTreeView は可視行しか描かない）。実害はメモリで、VarBind 3件の Trap
あたり約 12KB が一定して積み上がり、100,000 件で約 1.2GB になる。
Trap 受信は張りっぱなしで使うものなので、クリアするまで解放されない。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


def _trap(n):
    return {
        "source_ip": "192.0.2.%d" % (n % 250 + 1),
        "source_port": 162,
        "trap_oid": "1.3.6.1.4.1.9.9.41.2.0.1",
        "varbinds": [
            {"oid": "1.3.6.1.2.1.1.5.0", "value": "trap-%05d" % n},
            {"oid": "1.3.6.1.2.1.1.6.0", "value": "rack-%05d" % n},
        ],
    }


class SnmpTrapLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作ったウィンドウはクラス終了まで保持する。パネルもモデルもウィンドウの
    # 子なので、ウィンドウを先に捨てると C++ 側ごと消えて触れなくなる
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _panel(self, settings=None):
        """SNMPPanel を返す（単体生成は MIB 読み込みで落ちるため MainWindow 経由）。"""
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-trap-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        if settings is not None:
            cm.config.setdefault("settings", {})["snmp"] = settings
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            window = MainWindow()
        type(self)._windows.append(window)
        return window.snmp_panel

    def _feed(self, panel, count):
        for i in range(count):
            panel._add_trap_to_tree(_trap(i))

    def test_there_is_a_limit_on_how_many_traps_are_kept(self):
        panel = self._panel()
        self.assertTrue(hasattr(panel, "max_traps"),
                        "保持件数の上限が無い")
        self.assertGreater(panel.max_traps, 0)

    def test_the_kept_traps_do_not_exceed_the_limit(self):
        """溜め込んだデータが上限を超えないこと。"""
        panel = self._panel({"max_traps": 20})
        self._feed(panel, 60)
        self.assertLessEqual(len(panel.trap_data_list), 20,
                             "上限を超えて溜め込んでいる: %d"
                             % len(panel.trap_data_list))

    def test_the_tree_does_not_grow_past_the_limit(self):
        """表示側の行も上限を超えないこと（アイテムはここが一番重い）。"""
        panel = self._panel({"max_traps": 20})
        self._feed(panel, 60)
        self.assertLessEqual(panel.trap_tree_model.rowCount(), 20,
                             "上限を超えて行が残っている: %d"
                             % panel.trap_tree_model.rowCount())

    def test_the_data_and_the_tree_stay_in_step(self):
        """溜め込んだデータと表示行の数が食い違わないこと。

        食い違うと、エクスポートの中身と画面が別物になる。
        """
        panel = self._panel({"max_traps": 20})
        self._feed(panel, 60)
        self.assertEqual(len(panel.trap_data_list),
                         panel.trap_tree_model.rowCount())

    def test_the_newest_traps_are_the_ones_kept(self):
        """捨てるのは古い方であること。"""
        panel = self._panel({"max_traps": 10})
        self._feed(panel, 30)
        newest = panel.trap_data_list[0]["varbinds"][0]["value"]
        self.assertEqual(newest, "trap-00029",
                         "最新の Trap が先頭に無い")
        kept = {t["varbinds"][0]["value"] for t in panel.trap_data_list}
        self.assertNotIn("trap-00000", kept, "古い方を残している")

    def test_traps_below_the_limit_are_all_kept(self):
        """上限に満たないうちは1件も捨てないこと。"""
        panel = self._panel({"max_traps": 50})
        self._feed(panel, 12)
        self.assertEqual(len(panel.trap_data_list), 12)
        self.assertEqual(panel.trap_tree_model.rowCount(), 12)

    def test_the_limit_can_be_configured(self):
        """Syslog と同じく設定から変えられること。"""
        panel = self._panel({"max_traps": 250})
        self.assertEqual(panel.max_traps, 250)

    def test_a_broken_setting_falls_back_to_the_default(self):
        """config.json は手で編集できるので、壊れた値が来る。"""
        for broken in ("many", 0, -5, None):
            with self.subTest(max_traps=broken):
                panel = self._panel({"max_traps": broken})
                self.assertGreater(panel.max_traps, 0,
                                   "壊れた設定で上限が消えている")


if __name__ == "__main__":
    unittest.main()
