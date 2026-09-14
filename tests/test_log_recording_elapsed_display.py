"""ログ記録ダイアログの経過時間表示が 24 時間で巻き戻らないことを検証する。

_update_elapsed_time は timedelta.seconds から時・分・秒を組み立てていた。
timedelta.seconds は days を含まない 0..86399 の値なので、24 時間を超えた
時点で表示だけが 00:00:00 へ戻る（実測: 経過 26:03:04 の表示が 02:03:04）。
記録内容には影響しないが、長時間の連続記録で経過時間が読めなくなる。

経過は総秒数から組み立て、24 時間を超えたぶんは hours へ繰り上げる。
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, "src")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class LogRecordingElapsedDisplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _label_after(self, elapsed):
        """指定の経過時間が経ったことにして、経過時間ラベルの文字列を返す。"""
        from ui.dialogs.log_recording_dialog import LogRecordingDialog

        dialog = LogRecordingDialog("lab-rtr01", r"C:\logs\lab-rtr01.log")
        self.addCleanup(dialog.deleteLater)
        dialog.timer.stop()     # 実時間での自動更新を止めて、値を固定する
        dialog.start_time = datetime.now() - elapsed
        dialog._update_elapsed_time()
        return dialog.elapsed_label.text()

    def test_under_a_day_is_unchanged(self):
        """24 時間未満の表示はこれまでどおり。"""
        self.assertEqual(
            self._label_after(timedelta(hours=2, minutes=3, seconds=4)),
            "経過時間: 02:03:04")

    def test_over_a_day_does_not_wrap_around(self):
        """24 時間を超えても巻き戻らず、hours が繰り上がること。"""
        self.assertEqual(
            self._label_after(timedelta(days=1, hours=2, minutes=3, seconds=4)),
            "経過時間: 26:03:04")

    def test_several_days_keep_counting_up(self):
        """複数日でも通算の時間数で表示され続けること。"""
        self.assertEqual(
            self._label_after(timedelta(days=3, minutes=30)),
            "経過時間: 72:30:00")


if __name__ == "__main__":
    unittest.main()
