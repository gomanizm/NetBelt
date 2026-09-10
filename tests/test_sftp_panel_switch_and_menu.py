"""SFTP パネルで、古い一覧や古い場所に対して操作しないことを検証する。

2 つの穴がある。

1. 機器を切り替えても（set_sftp_manager）、前の機器の行と選択が表に残る。
   新しい機器の一覧が届くまでのあいだに削除を承認すると、前の機器の
   file_info と新しい機器の _begin() が組み合わさり、新しい機器の同名
   ファイルを消す（実測で再現）。転送中で一覧が遅い機器ほど、この
   危険な表示が長く続く。切り替えた時点で表を空にする。

2. 右クリックメニューは file_info だけをメニュー作成時に捕捉し、
   manager と base_path は項目を選んでから読む。QMenu.exec() の間に
   保留中のディレクトリ移動が完了すると、古い一覧で右クリックした名前を
   新しいディレクトリで削除・改名・chmod・ダウンロードする（実測で再現）。
   メニューを開いた時点で相手と場所を固定する。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


def _entry(name, is_dir=False):
    return {"name": name, "size": 0 if is_dir else 12, "mtime": 1700000000,
            "mode": 0o040755 if is_dir else 0o100644, "is_dir": is_dir,
            "permissions": "drwxr-xr-x" if is_dir else "-rw-r--r--"}


class SftpPanelSwitchAndMenuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self, path="/"):
        m = mock.Mock()
        m.current_path = path
        m.get_current_path.return_value = path
        return m

    def _panel_with_listing(self, manager, name="rtrA", entries=None):
        from ui.sftp_panel import SFTPPanel
        panel = SFTPPanel()
        self.addCleanup(panel.close)
        panel.resize(600, 400)
        panel.show()
        panel.set_sftp_manager(manager, name)
        panel._update_file_list(entries or [_entry("boot.cfg")])
        self.app.processEvents()
        return panel

    # --- 1. 機器の切り替え ---

    def test_switching_devices_clears_the_old_listing(self):
        """新しい機器の一覧が届くまで、前の機器の行を見せないこと。"""
        a = self._manager()
        panel = self._panel_with_listing(a)
        self.assertEqual(panel.model.rowCount(), 1, "前提: A の一覧が出ている")
        panel.tree_view.setCurrentIndex(panel.model.index(0, 0))

        b = self._manager()
        panel.set_sftp_manager(b, "rtrB")   # B の一覧はまだ届かない

        self.assertEqual(panel.model.rowCount(), 0,
                         "切り替え後も前の機器の行が残っている")
        self.assertEqual(panel.tree_view.selectedIndexes(), [],
                         "前の機器の選択が残っている")

    def test_the_new_listing_still_appears_after_the_switch(self):
        """対照: 新しい機器の一覧は普通に出ること。"""
        panel = self._panel_with_listing(self._manager())
        b = self._manager()
        panel.set_sftp_manager(b, "rtrB")
        panel._update_file_list([_entry("running.cfg"), _entry("etc", is_dir=True)])
        self.assertEqual(panel.model.rowCount(), 2)

    # --- 2. 右クリックメニュー ---

    def _open_menu_and_trigger(self, panel, manager, action_text, during_menu):
        """行の上で右クリックし、メニューが開いている間に during_menu を起こしてから
        action_text の項目を選ぶ。"""
        from ui import sftp_panel as mod
        index = panel.model.index(0, 0)
        pos = panel.tree_view.visualRect(index).center()
        triggered = []

        def fake_exec(menu, *args, **kwargs):
            during_menu()
            for act in menu.actions():
                if act.text() == action_text:
                    triggered.append(act.text())
                    act.trigger()
                    return act
            self.fail("メニューに %r が無い: %s" % (action_text, [a.text() for a in menu.actions()]))

        with mock.patch.object(mod.QMenu, "exec", fake_exec):
            panel._show_context_menu(pos)
        self.assertEqual(triggered, [action_text], "前提: メニュー項目を選べている")

    def test_delete_from_the_menu_targets_the_directory_shown_when_the_menu_opened(self):
        from PyQt6.QtWidgets import QMessageBox
        from ui import sftp_panel as mod
        manager = self._manager("/A")
        panel = self._panel_with_listing(manager)

        def listing_for_b_arrives():
            manager.get_current_path.return_value = "/B"

        with mock.patch.object(mod.QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes):
            self._open_menu_and_trigger(panel, manager, "削除", listing_for_b_arrives)

        manager.delete_item.assert_called_once_with("/A/boot.cfg", False)

    def test_rename_from_the_menu_targets_the_directory_shown_when_the_menu_opened(self):
        from ui import sftp_panel as mod
        manager = self._manager("/A")
        panel = self._panel_with_listing(manager)

        def listing_for_b_arrives():
            manager.get_current_path.return_value = "/B"

        with mock.patch.object(mod.QInputDialog, "getText",
                               return_value=("boot.bak", True)):
            self._open_menu_and_trigger(panel, manager, "名前変更", listing_for_b_arrives)

        manager.rename_item.assert_called_once_with("/A/boot.cfg", "/A/boot.bak")

    def test_open_from_the_menu_uses_the_directory_shown_when_the_menu_opened(self):
        """「開く」も、メニューを開いた時点の場所を基準に移動すること。

        行番号だけを捕捉して項目選択時にモデルを読み直すと、メニューの間に
        一覧が差し替わったとき、同じ行に来た別のディレクトリへ移動する。
        """
        manager = self._manager("/A")
        panel = self._panel_with_listing(manager, entries=[_entry("etc", is_dir=True)])

        def listing_for_b_arrives():
            manager.get_current_path.return_value = "/B"
            panel._update_file_list([_entry("var", is_dir=True)])   # 同じ行に別のディレクトリ

        self._open_menu_and_trigger(panel, manager, "開く", listing_for_b_arrives)

        manager.change_directory.assert_called_once_with("/A/etc")

    def test_a_menu_action_after_a_device_switch_is_abandoned(self):
        """メニューが開いている間に機器が変わったら、どちらにも操作しないこと。"""
        from PyQt6.QtWidgets import QMessageBox
        from ui import sftp_panel as mod
        a = self._manager("/A")
        panel = self._panel_with_listing(a)
        b = self._manager("/")

        def tab_switches():
            panel.set_sftp_manager(b, "rtrB")

        with mock.patch.object(mod.QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes):
            self._open_menu_and_trigger(panel, a, "削除", tab_switches)

        a.delete_item.assert_not_called()
        b.delete_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
