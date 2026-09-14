"""Trap の時刻は受信した時刻であり、GUI が処理した時刻でないことを検証する。

snmp_manager._build_trap_data は受信スレッドで received_at（ISO 8601）を
入れているのに、_add_trap_to_tree は datetime.now() で付け直していた。
trap_received は queued 配送なので、受信から GUI の処理までには間がある。

定常状態のずれはミリ秒で害は無い。意味のある差になるのは GUI が滞留する
とき —— 大量 Trap の queued 配送、終了待ち、モーダルダイアログを開いている
間 —— で、その滞留分だけ後ろにずれた時刻が表示され、そのままエクスポート
にも入る。Trap の前後関係を時刻で追う用途で読み違える。

received_at があればそれを表示・保存用の時刻に使い、無い／読めないときだけ
now() に落とす。
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, "src")

# 受信スレッドが付けた時刻（GUI が処理するのはこれより後）
RECEIVED_AT = "2026-01-02T03:04:05.678901"
RECEIVED_AT_DISPLAY = "2026-01-02 03:04:05"


def _trap(received_at=RECEIVED_AT):
    trap = {
        "source_ip": "192.0.2.10",
        "source_port": 162,
        "trap_oid": "1.3.6.1.4.1.9.9.41.2.0.1",
        "varbinds": [
            {"oid": "1.3.6.1.2.1.1.5.0", "value": "device-01"},
        ],
    }
    if received_at is not None:
        trap["received_at"] = received_at
    return trap


class SnmpTrapReceivedAtTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作ったウィンドウはクラス終了まで保持する（test_snmp_trap_limit と同じ）
    _windows = []

    @classmethod
    def tearDownClass(cls):
        cls._windows.clear()

    def _panel(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-trap-at-")
        cm = ConfigManager(config_path=os.path.join(d, "config.json"))
        with mock.patch("ui.main_window.ConfigManager") as fake, \
             mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = cm
            window = MainWindow()
        type(self)._windows.append(window)
        return window.snmp_panel

    def test_the_kept_trap_carries_the_time_it_was_received(self):
        """保存される時刻が受信時刻であること（エクスポートはこれを書く）。"""
        panel = self._panel()
        panel._add_trap_to_tree(_trap())
        self.assertEqual(panel.trap_data_list[0]["timestamp"],
                         RECEIVED_AT_DISPLAY,
                         "GUI が処理した時刻で上書きしている")

    def test_the_tree_shows_the_time_it_was_received(self):
        """画面の時刻列も受信時刻であること。"""
        panel = self._panel()
        panel._add_trap_to_tree(_trap())
        self.assertEqual(panel.trap_tree_model.item(0, 0).text(),
                         RECEIVED_AT_DISPLAY,
                         "画面の時刻が受信時刻と違う")

    def test_a_delay_before_the_gui_handles_it_does_not_shift_the_time(self):
        """GUI が滞留しても時刻がずれないこと（この指摘の本体）。"""
        panel = self._panel()
        much_later = datetime(2026, 1, 2, 3, 9, 30)
        with mock.patch("ui.snmp_panel.datetime") as fake_datetime:
            fake_datetime.now.return_value = much_later
            fake_datetime.fromisoformat = datetime.fromisoformat
            panel._add_trap_to_tree(_trap())
        self.assertEqual(panel.trap_data_list[0]["timestamp"],
                         RECEIVED_AT_DISPLAY,
                         "滞留した分だけ時刻が後ろへずれている")

    def test_a_trap_without_a_received_time_still_gets_one(self):
        """受信時刻が無いときは処理時刻に落ちること（時刻列を空にしない）。"""
        panel = self._panel()
        now = datetime(2026, 1, 2, 3, 9, 30)
        with mock.patch("ui.snmp_panel.datetime") as fake_datetime:
            fake_datetime.now.return_value = now
            fake_datetime.fromisoformat = datetime.fromisoformat
            panel._add_trap_to_tree(_trap(received_at=None))
        self.assertEqual(panel.trap_data_list[0]["timestamp"],
                         "2026-01-02 03:09:30")

    def test_an_unreadable_received_time_falls_back_instead_of_raising(self):
        """読めない受信時刻でも落ちないこと。"""
        now = datetime(2026, 1, 2, 3, 9, 30)
        for broken in ("", "not-a-time", 12345, None):
            with self.subTest(received_at=broken):
                panel = self._panel()
                with mock.patch("ui.snmp_panel.datetime") as fake_datetime:
                    fake_datetime.now.return_value = now
                    fake_datetime.fromisoformat = datetime.fromisoformat
                    panel._add_trap_to_tree(_trap(received_at=broken))
                self.assertEqual(panel.trap_data_list[0]["timestamp"],
                                 "2026-01-02 03:09:30")


if __name__ == "__main__":
    unittest.main()
