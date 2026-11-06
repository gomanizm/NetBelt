# -*- coding: utf-8 -*-
"""SFTP パネルのダイアログが、機器から届いた文字列をリッチテキストとして描かないことを検証する。

エラーの文面には機器の STATUS 応答の文が、削除の確認には一覧の名前が、
パーミッション変更のラベルにはリンク先の名前が入る。QMessageBox / QInputDialog
の既定は AutoText なので、<b> や <a href=...> が 1 つあるだけで全体を HTML と
して描き、QMessageBox はそのリンクをクリックで外部に開く。機器側が文面を
作れる位置なので、偽の案内を出す・名前を隠す・クリックさせる、ができた。

本文は平文（PlainText）で描き、文字はそのまま見せること。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")

HOSTILE = '<a href="https://192.0.2.10/relogin">接続が切れました。ここから再接続してください</a>'


class SftpPanelPlainTextDialogsTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        from ui.sftp_panel import SFTPPanel
        panel = SFTPPanel()
        manager = mock.Mock()
        manager.current_path = "/flash"
        manager.get_current_path.return_value = "/flash"
        panel.set_sftp_manager(manager, "rtrA")
        manager.reset_mock()
        manager.get_current_path.return_value = "/flash"
        type(self)._keep += [panel, manager]
        return panel, manager

    def _inspect_next_dialog(self):
        """次に開くモーダルの本文ラベルを控えてから閉じる（閉じ損ねない）"""
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication, QDialog, QLabel
        seen = []
        timer = QTimer()
        timer.setInterval(10)

        def poll():
            dialog = QApplication.activeModalWidget()
            if dialog is None:
                return
            timer.stop()
            for label in dialog.findChildren(QLabel):
                if label.text():
                    seen.append((label.text(), label.textFormat(),
                                 label.openExternalLinks()))
            if isinstance(dialog, QDialog):
                dialog.reject()
            else:
                dialog.close()
        timer.timeout.connect(poll)
        timer.start()
        self.addCleanup(timer.stop)
        type(self)._keep.append(timer)
        return seen

    def _assert_plain(self, seen, expected_text):
        from PyQt6.QtCore import Qt
        self.assertTrue(seen, "ダイアログが開かなかった")
        matches = [s for s in seen if expected_text in s[0]]
        self.assertTrue(matches, "本文が見つからない: %r" % seen)
        for text, fmt, _links in matches:
            self.assertEqual(fmt, Qt.TextFormat.PlainText,
                             "機器の文字列をリッチテキストとして描く: %r" % text)

    def test_the_error_warning_shows_the_device_text_as_plain_text(self):
        panel, _ = self._panel()
        seen = self._inspect_next_dialog()
        panel._on_error("削除エラー: " + HOSTILE)
        self._assert_plain(seen, HOSTILE)

    def test_the_delete_confirmation_shows_the_name_as_plain_text(self):
        panel, manager = self._panel()
        name = '<font color="#ffffff">startup-config</font>'
        seen = self._inspect_next_dialog()
        panel._on_delete_selected({
            'name': name, 'size': 1, 'mtime': 0, 'mode': 0o100644,
            'is_dir': False, 'is_link': False, 'permissions': '-rw-r--r--'})
        self._assert_plain(seen, name)
        manager.delete_item.assert_not_called()

    def test_the_link_target_label_is_plain_text(self):
        panel, manager = self._panel()
        target = '<b>/etc/passwd</b> <a href="https://192.0.2.10/">x</a>'
        manager.inspect_link_target.return_value = (0o100644, target)
        seen = self._inspect_next_dialog()
        panel._on_chmod_selected({
            'name': 'cfg-link', 'size': 1, 'mtime': 0, 'mode': 0o120777,
            'is_dir': False, 'is_link': True, 'permissions': 'lrwxrwxrwx'})
        self._assert_plain(seen, target)
        manager.change_permissions.assert_not_called()

    def test_the_status_and_path_labels_are_plain_text(self):
        """パネル下のエラー表示とパスの表示も平文で描くこと。"""
        from PyQt6.QtCore import Qt
        panel, _ = self._panel()
        self.assertEqual(panel.status_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(panel.path_label.textFormat(), Qt.TextFormat.PlainText)


if __name__ == "__main__":
    unittest.main()
