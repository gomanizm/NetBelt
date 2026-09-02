"""SFTP パネルの進捗バーが、いま見ている機器の話だけを映すことを検証する。

set_sftp_manager は _detach_manager() で旧マネージャの transfer_progress /
transfer_complete を切り離し、_current_entries や案内の表示も初期化するが、
進捗バーの値・可視状態・書式だけは触っていなかった。

そのため機器 A への転送中に別タブへ切り替えると、接続先の表示は B に
変わるのに、その直下の進捗バーは A の途中経過（45% (45.0 MB / 100.0 MB)
など）を出したまま残る。A の転送が終わっても _on_transfer_complete は
もう繋がっていないので setVisible(False) されず、バーは消えない。

機器名を出して誤送信を防ぐという接続先表示の意図と正面から食い違い、
「B への転送が 45% で止まっている」と読める表示になる。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpProgressBarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self):
        m = mock.Mock()
        m.current_path = "/"
        m.get_current_path.return_value = "/"
        return m

    def _panel_transferring(self):
        """rtrA へ転送中で、進捗バーが出ている状態を作る。"""
        from ui.sftp_panel import SFTPPanel
        panel = SFTPPanel()
        panel.set_sftp_manager(self._manager(), "rtrA")
        panel._update_progress(45 * 1024 * 1024, 100 * 1024 * 1024)
        self.assertFalse(panel.progress_bar.isHidden(),
                         "前提: 転送中は進捗バーが出ている")
        return panel

    def test_switching_device_hides_the_previous_progress(self):
        """別の機器へ切り替えたら、前の機器の進捗を出したままにしないこと。"""
        panel = self._panel_transferring()
        panel.set_sftp_manager(self._manager(), "rtrB")
        self.assertTrue(panel.progress_bar.isHidden(),
                        "前の機器の進捗バーが残っている")

    def test_switching_device_clears_the_previous_percentage(self):
        """前の機器の割合が残らないこと。

        「B への転送が 45% で止まっている」と読める表示になる。
        """
        panel = self._panel_transferring()
        before = panel.progress_bar.value()
        panel.set_sftp_manager(self._manager(), "rtrB")

        self.assertNotEqual(panel.progress_bar.value(), before,
                            "前の機器の進捗の値が残っている")
        self.assertNotIn("45", panel.progress_bar.format(),
                         "前の機器の割合が書式に残っている: %r"
                         % panel.progress_bar.format())
        self.assertNotIn("45", panel.progress_bar.text(),
                         "前の機器の割合が表示に残っている: %r"
                         % panel.progress_bar.text())

    def test_disconnecting_hides_the_progress_too(self):
        """切断でも消えること（これまでどおり）。"""
        panel = self._panel_transferring()
        panel.clear()
        self.assertTrue(panel.progress_bar.isHidden())

    def test_a_transfer_on_the_new_device_still_shows(self):
        """切り替えた先で転送を始めれば、ちゃんと出ること。"""
        panel = self._panel_transferring()
        panel.set_sftp_manager(self._manager(), "rtrB")
        panel._update_progress(10, 100)
        self.assertFalse(panel.progress_bar.isHidden(),
                         "切り替え後の転送で進捗バーが出ない")
        self.assertIn("10%", panel.progress_bar.format())


GIB = 1024 ** 3


class LargeTransferProgressTest(unittest.TestCase):
    """2GiB を超える転送でも、進捗が実際の割合を映すこと。

    transfer_progress は pyqtSignal(int, int) で宣言されていた。この int は
    C++ の 32bit int に対応するため、paramiko のコールバックが渡す
    バイト数が 2**31 を超えると、emit は例外を出さずに黙って丸める。

    症状は転送サイズの帯で二分される（実測）:
      - 2GiB以上4GiB未満: 全体サイズが負になり、QProgressBar は範囲外の
        現在値を reset() するので、バーは空のまま1ミリも動かず、
        パーセント表示も出ない。
      - 4GiB以上: 全体サイズが正の小さい値に化け、真の進捗 20% ほどで
        「100%」に到達し、その先は 125%、-150% といった値まで出る。

    NX-OS / IOS-XE のイメージなど 2GiB 超の転送はこのツールの用途その
    ものなので、どちらの帯も実際に踏む。後者は完了と誤認させる。
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _wired(self):
        """本物のシグナルを本物のスロットへ繋ぐ（丸めはシグナルで起きる）。"""
        from core.sftp_manager import SFTPManager
        from ui.sftp_panel import SFTPPanel
        panel = SFTPPanel()
        manager = SFTPManager()
        manager.transfer_progress.connect(panel._update_progress)
        return panel, manager

    @staticmethod
    def _shown_fraction(panel):
        """バーが見せている割合。"""
        bar = panel.progress_bar
        span = bar.maximum() - bar.minimum()
        if span <= 0:
            return None
        return (bar.value() - bar.minimum()) / span

    def test_the_reported_sizes_are_not_truncated(self):
        """シグナルを通ってもバイト数が化けないこと。"""
        _, manager = self._wired()
        seen = []
        manager.transfer_progress.connect(lambda a, b: seen.append((a, b)))

        manager.transfer_progress.emit(3 * GIB, 5 * GIB)

        self.assertEqual(seen, [(3 * GIB, 5 * GIB)],
                         "32bit へ丸められている: %s" % seen)

    def test_a_three_gib_transfer_shows_its_real_proportion(self):
        """3GiB の半分まで進んだら、バーも半分を指すこと。"""
        panel, manager = self._wired()
        manager.transfer_progress.emit(3 * GIB // 2, 3 * GIB)

        shown = self._shown_fraction(panel)
        self.assertIsNotNone(shown, "バーの範囲が潰れている（空のまま動かない）")
        self.assertAlmostEqual(shown, 0.5, delta=0.02,
                               msg="実際の割合を映していない: %r" % shown)

    def test_a_five_gib_transfer_does_not_reach_100_percent_early(self):
        """5GiB の 20% で「100%」と出さないこと。

        完了したと誤認して、転送中にアプリを閉じたり機器を再起動しかねない。
        """
        panel, manager = self._wired()
        manager.transfer_progress.emit(5 * GIB // 5, 5 * GIB)

        shown = self._shown_fraction(panel)
        self.assertIsNotNone(shown)
        self.assertAlmostEqual(shown, 0.2, delta=0.02,
                               msg="実際の割合を映していない: %r" % shown)
        shown_format = panel.progress_bar.format()
        self.assertNotIn("100%", shown_format,
                         f"20% の時点で 100% と出ている: {shown_format!r}")

    def test_the_percentage_stays_within_range(self):
        """割合が 100% を超えたり負になったりしないこと。"""
        panel, manager = self._wired()
        for done in (0, GIB, 3 * GIB, 5 * GIB):
            with self.subTest(done=done):
                manager.transfer_progress.emit(done, 5 * GIB)
                shown = self._shown_fraction(panel)
                self.assertIsNotNone(shown)
                self.assertGreaterEqual(shown, 0.0)
                self.assertLessEqual(shown, 1.0)

    def test_the_size_in_the_label_is_the_real_size(self):
        """表示するバイト数も化けていないこと。"""
        panel, manager = self._wired()
        manager.transfer_progress.emit(3 * GIB, 5 * GIB)
        self.assertIn("5.0 GB", panel.progress_bar.format(),
                      "全体サイズの表示が化けている: %r"
                      % panel.progress_bar.format())

    def test_an_unknown_total_still_shows_how_much_moved(self):
        """全体サイズが分からない転送でも、送れた量が読めること。

        QProgressBar は minimum==maximum のとき text() を空文字にするので、
        目盛りを伏せた状態で setFormat した文字列は画面に出ない。
        「割合は出せないが転送済みは見せる」と決めた以上、出す場所が要る。
        """
        panel, manager = self._wired()
        manager.transfer_progress.emit(12345, 0)

        shown = panel.progress_bar.text() + " " + panel.status_label.text()
        self.assertIn("12.1 KB", shown,
                      "転送済みのバイト数がどこにも出ていない: %r" % shown)

    def test_a_small_transfer_still_shows_its_percentage(self):
        """これまでどおり、小さい転送も割合を出すこと。"""
        panel, manager = self._wired()
        manager.transfer_progress.emit(45, 100)
        self.assertIn("45%", panel.progress_bar.format())
        self.assertAlmostEqual(self._shown_fraction(panel), 0.45, delta=0.02)


if __name__ == "__main__":
    unittest.main()
