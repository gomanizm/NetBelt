"""窓を閉じたら、機器一覧のシリアルポート定期確認も止まることを検証する。

DeviceTree は作られた時点で 1 秒ごとの QTimer を起こし、シリアルポートの
増減を見ている（_check_serial_ports）。止める者はどこにもおらず、
MainWindow.closeEvent も止めていなかったので、閉じたあとも鳴り続けていた。

実測（QT_QPA_PLATFORM=offscreen で pytest -q -p no:cacheprovider tests/）:
テスト一式は窓を作っては閉じるだけなので、閉じ終えた窓のタイマーが
積み上がる（落ちる時点で 20 個が鳴っていた）。1 プロセスで全体を流すと
進捗 30% あたり、tests/test_reconnect_wait_survives_failed_reconnect.py::
test_a_successful_reconnect_still_clears_the_wait の最中に、まとめの行を
出さないままプロセスが終わった（終了コード 0xC0000409、5 回中 5 回）。
鳴っている _check_serial_ports の最中に GC が別の窓を捨てるためで、
gc.disable() を入れた回と、このタイマーを止めた回は最後まで通った。

閉じたあとに機器一覧を組み直す意味も無いので、閉じる時点で止める。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class WindowCloseStopsSerialMonitorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        from ui.main_window import MainWindow
        from core.config_manager import ConfigManager
        d = tempfile.mkdtemp(prefix="netbelt-serial-monitor-")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(
                config_path=os.path.join(d, "config.json"))
            window = MainWindow()
        # 検証の途中で落ちても鳴らしっぱなしにしない
        self.addCleanup(window.device_tree.stop_serial_monitor)
        return window

    def test_closing_the_window_stops_the_serial_port_polling(self):
        """閉じたあとはシリアルポートの定期確認が鳴らないこと。"""
        window = self._window()
        timer = window.device_tree._serial_monitor_timer
        self.assertTrue(timer.isActive(), "前提: 開いている間は定期確認が動く")

        window.close()

        self.assertFalse(timer.isActive(),
                         "閉じたあともシリアルポートの定期確認が鳴り続けている")


if __name__ == "__main__":
    unittest.main()
