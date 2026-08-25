"""SFTP クライアントの使い方の案内が、必要なときに出ていることを検証する。

このパネルはターミナルの SSH セッションに相乗りする設計で、単独で接続する
UI が無い。作者本人が「接続方法が無い」と迷ったので、黙っていて分かるもの
ではない。加えて、SSH で入れても SFTP に対応していない機器では使えない
（その場合いまは stdout に出るだけで、画面には何も出ない）。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class SftpPanelHintTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        from ui.sftp_panel import SFTPPanel
        return SFTPPanel()

    def test_the_hint_is_shown_before_connecting(self):
        """未接続のとき、案内が見えていること。

        isVisible() は窓を show していない offscreen では常に False なので、
        setVisible の指定がそのまま出る isHidden() で見る。
        """
        panel = self._panel()
        self.assertFalse(panel.hint_label.isHidden(),
                         "未接続なのに案内が出ていない")

    def test_the_hint_says_how_to_make_it_available(self):
        """どうすれば使えるようになるかを書いてあること。"""
        panel = self._panel()
        text = panel.hint_label.text()
        self.assertIn("SSH", text, "SSH 接続が要ることを書いていない")

    def test_the_hint_warns_that_ssh_alone_may_not_be_enough(self):
        """SSH で入れても使えない機器があることを書いてあること。

        ネットワーク機器では SFTP サブシステムが無いことが珍しくない。
        書いていないと「SSH は通るのに壊れている」と受け取られる。
        """
        panel = self._panel()
        text = panel.hint_label.text()
        self.assertIn("SFTP に対応していない", text,
                      "SFTP 非対応の機器がある旨を書いていない")

    def test_the_hint_disappears_once_connected(self):
        """接続したら案内を引っ込めること（読み終えた案内は邪魔になる）。"""
        from unittest import mock
        panel = self._panel()
        panel.set_sftp_manager(mock.Mock(), "dev")
        self.assertTrue(panel.hint_label.isHidden(),
                        "接続後も案内が残っている")

    def test_the_hint_comes_back_after_disconnecting(self):
        """切断したら、また案内を出すこと。"""
        from unittest import mock
        panel = self._panel()
        panel.set_sftp_manager(mock.Mock(), "dev")
        panel.clear()
        self.assertFalse(panel.hint_label.isHidden(),
                         "切断したのに案内が戻らない")


if __name__ == "__main__":
    unittest.main()
