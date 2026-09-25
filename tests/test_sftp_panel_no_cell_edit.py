"""SFTP パネルの一覧を、セルの直接編集で書き換えられないことを検証する。

一覧のビューは既定の編集トリガ（DoubleClicked | EditKeyPressed）のままで、
項目も QStandardItem の既定どおり編集可能だった。実測（QTest で実際に
キーとマウスを操作）: 名前セルで F2 を押すとエディタが開き、
'renamed.cfg' と打って Enter で確定すると表示だけが 'renamed.cfg' になった。
UserRole の name は元のままで、機器へは要求が 1 件も出ず、エラーも出ない。
権限セルも F2 で '-rwxrwxrwx' に書き換わったが、機器の mode は変わらない。
ファイル名のダブルクリックでもエディタが開いた。改名・権限変更が効いた
ように見える誤表示で、更新すると元に戻る。ほかのパネル（ftp / tftp /
snmp）は既に NoEditTriggers にしており、SFTP パネルだけ漏れていた。

直し方: 一覧のビューを NoEditTriggers にする（ほかのパネルと揃える）。
改名・権限変更は、これまでどおり右クリックメニューから行う。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")


class SftpPanelNoCellEditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui.sftp_panel import SFTPPanel

        self.panel = SFTPPanel()
        self.addCleanup(self.panel.deleteLater)
        self.panel.resize(800, 500)
        self.panel.show()
        self.panel._update_file_list([{
            'name': 'startup.cfg',
            'size': 12,
            'mtime': 1700000000,
            'mode': 0o100644,
            'is_dir': False,
            'is_link': False,
            'permissions': '-rw-r--r--',
        }])
        self.view = self.panel.tree_view
        self.view.setFocus()
        self.app.processEvents()

    def _editor_opened(self):
        from PyQt6.QtWidgets import QAbstractItemView, QLineEdit

        editors = [w for w in self.view.findChildren(QLineEdit) if w.isVisible()]
        return (self.view.state() == QAbstractItemView.State.EditingState
                or bool(editors))

    def _press_f2_and_type(self, column, text):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        index = self.panel.model.index(0, column)
        self.view.setCurrentIndex(index)
        self.app.processEvents()
        QTest.keyClick(self.view, Qt.Key.Key_F2)
        self.app.processEvents()
        opened = self._editor_opened()
        focus = self.app.focusWidget() or self.view
        QTest.keyClicks(focus, text)
        QTest.keyClick(focus, Qt.Key.Key_Return)
        self.app.processEvents()
        return opened

    def _texts(self):
        return [self.panel.model.item(0, c).text() for c in range(4)]

    def test_f2_on_the_name_cell_opens_no_editor(self):
        """名前セルで F2 を押しても編集に入らず、表示が変わらないこと。"""
        before = self._texts()

        opened = self._press_f2_and_type(0, "renamed.cfg")

        self.assertFalse(opened, "名前セルのエディタが開いた（機器は改名されない）")
        self.assertEqual(self._texts(), before, "表示だけが書き換わった")

    def test_f2_on_the_permission_cell_opens_no_editor(self):
        """権限セルで F2 を押しても編集に入らず、表示が変わらないこと。"""
        before = self._texts()

        opened = self._press_f2_and_type(2, "-rwxrwxrwx")

        self.assertFalse(opened, "権限セルのエディタが開いた（機器の mode は変わらない）")
        self.assertEqual(self._texts(), before, "表示だけが書き換わった")

    def test_a_double_click_on_the_name_opens_no_editor(self):
        """ファイル名のダブルクリックでも編集に入らないこと。"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        before = self._texts()
        index = self.panel.model.index(0, 0)
        center = self.view.visualRect(index).center()
        # 実際のダブルクリックは 押す→離す→ダブルクリック→離す の順に届く
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=center)
        QTest.mouseDClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=center)
        self.app.processEvents()

        self.assertFalse(self._editor_opened(), "ダブルクリックでエディタが開いた")
        self.assertEqual(self._texts(), before)
        info = self.panel.model.item(0, 0).data(Qt.ItemDataRole.UserRole)
        self.assertEqual(info['name'], 'startup.cfg')

    def test_a_double_click_on_a_directory_still_opens_it(self):
        """ディレクトリのダブルクリックでの移動は、これまでどおり動くこと。"""
        from unittest import mock
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest

        manager = mock.Mock()
        manager.get_current_path.return_value = "/flash"
        self.panel.sftp_manager = manager
        self.panel._update_file_list([{
            'name': 'archive', 'size': 0, 'mtime': 1700000000,
            'mode': 0o040755, 'is_dir': True, 'is_link': False,
            'permissions': 'drwxr-xr-x',
        }])
        center = self.view.visualRect(self.panel.model.index(0, 0)).center()
        QTest.mouseClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=center)
        QTest.mouseDClick(self.view.viewport(), Qt.MouseButton.LeftButton, pos=center)
        self.app.processEvents()

        manager.change_directory.assert_called_once_with("/flash/archive")


if __name__ == "__main__":
    unittest.main()
