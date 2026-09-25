"""SFTP のチャンネルだけが切れたとき、一覧は残し操作は断ることを検証する。

実測（基準 aa38a2b）:
  実 SFTPPanel + 実 SFTPManager で一覧 1 行を表示したあと、
  m._fail('ディレクトリ一覧取得エラー', TimeoutError()) で接続が畳まれても、
  パネルは何も知らないままだった。src/ 全体で SFTPManager.disconnected を
  購読しているところはゼロで、sftp_panel.py には 'disconnected' の文字列
  自体が無い。結果、
    * 表示は「エラー: ...」のままで、SFTP が切れたことがどこにも出ない
    * 行を選んで削除すると「'boot.bin' を削除しますか？」の確認まで開き、
      Yes のあとでようやく「SFTP接続がありません」になる
    * 別のターミナルタブへ移って戻ると、_on_terminal_tab_changed が
      切断済みのマネージャで set_sftp_manager を呼び直すので、
      list_directory がもう一度走り、表示が「一覧を取得しています...」に
      戻ったうえで「SFTP接続がありません」の警告がタブを戻すたびに出る
  SFTP だけが切れても SSH セッションは生きているため、_on_connection_closed
  も _drop_sftp_manager も走らない。つまり機器へ繋ぎ直す以外に戻す道が無い。

利用者の決定（2026-09-23、保守的な側）:
  一覧は残し、操作だけ止める。SFTP のチャンネルだけが死んで SSH セッションが
  生きている場合、パネルに『SFTP は切断されました』と出し、転送・削除・改名・
  権限変更などの操作はすべて断る（送り先の取り違えを防ぐ）。一覧の表示は
  残す。切断直後のモーダルが出ている最中に画面を空にしない。

直し方:
  SFTPPanel が disconnected を購読し（set_sftp_manager で繋ぎ、
  _detach_manager で外す）、届いたら表示を DROPPED_TEXT にするだけで
  行はそのまま残す。操作の入口は _refuse_if_dropped で断り、ダイアログの
  裏で切れた場合は _still_on が弾く。set_sftp_manager は繋ぐ相手が既に
  切れていれば一覧を取りに行かない（タブを戻すたびの警告を止める）。
"""
import os
import posixpath
import stat
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class FakeAttr:
    """listdir_attr が返す 1 項目分"""

    def __init__(self, filename, st_mode, st_size=1024, st_mtime=1700000000):
        self.filename = filename
        self.st_mode = st_mode
        self.st_size = st_size
        self.st_mtime = st_mtime


class SftpDroppedPanelTest(unittest.TestCase):
    _panels = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # offscreen では誰もモーダルを閉じられない
        for name in ("critical", "warning", "information"):
            patcher = mock.patch("ui.sftp_panel.QMessageBox." + name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _pump(self, check, timeout=3.0):
        from PyQt6.QtWidgets import QApplication
        deadline = time.time() + timeout
        while time.time() < deadline:
            QApplication.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _manager(self):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        m.current_path = "/flash"
        m.sftp_client = mock.Mock()
        m.sftp_client.listdir_attr.return_value = [
            FakeAttr("boot.bin", stat.S_IFREG | 0o644),
        ]
        m.sftp_client.normalize.side_effect = lambda p: posixpath.normpath(p)
        return m

    def _panel_with_listing(self, m):
        """一覧を 1 行表示したパネルを返す。"""
        from ui.sftp_panel import SFTPPanel

        panel = SFTPPanel()
        type(self)._panels.append(panel)      # 親より先に捨てない
        panel.set_sftp_manager(m, "router-A", "192.0.2.10")
        self.assertTrue(self._pump(lambda: panel.model.rowCount() == 1),
                        "前提: 一覧が 1 行出ていること")
        return panel

    @staticmethod
    def _drop(m):
        """SFTP のチャンネルだけが死ぬ（SSH セッションは生きている）。"""
        m._fail("ディレクトリ一覧取得エラー", TimeoutError())

    def _file_info(self, panel):
        from PyQt6.QtCore import Qt
        item = panel.model.item(0, 0)
        return item.data(Qt.ItemDataRole.UserRole)

    # --- 一覧は残す ---

    def test_the_listing_stays_and_the_panel_says_the_channel_dropped(self):
        """切れても行は消さず、切れたことを出すこと。"""
        m = self._manager()
        panel = self._panel_with_listing(m)

        self._drop(m)

        self.assertFalse(m.is_connected, "前提: SFTP は畳まれている")
        self.assertEqual(panel.model.rowCount(), 1, "切断で一覧まで消えた")
        self.assertEqual(panel.target_label.text(),
                         "接続先: router-A (192.0.2.10)",
                         "接続先の表示まで消えた")
        self.assertIn("SFTP は切断されました", panel.status_label.text(),
                      "SFTP が切れたことがパネルに出ていない")

    def test_the_listing_is_still_there_while_the_notice_is_open(self):
        """対照: 切断の警告が開いている最中に画面を空にしないこと。"""
        from ui import sftp_panel as mod
        m = self._manager()
        panel = self._panel_with_listing(m)
        seen = []

        with mock.patch.object(mod.QMessageBox, "warning",
                               side_effect=lambda *a, **k: seen.append(
                                   panel.model.rowCount())):
            self._drop(m)

        self.assertEqual(seen, [1], "警告が出ている最中に一覧が消えた")

    # --- 操作は断る ---

    def test_delete_is_refused_without_opening_the_confirmation(self):
        """削除の確認ダイアログを開かずに断ること。"""
        from ui import sftp_panel as mod
        m = self._manager()
        panel = self._panel_with_listing(m)
        self._drop(m)
        panel.tree_view.setCurrentIndex(panel.model.index(0, 0))

        with mock.patch.object(mod.QMessageBox, "question") as question, \
                mock.patch.object(m, "delete_item") as delete_item:
            panel._on_delete()

        question.assert_not_called()
        delete_item.assert_not_called()
        self.assertIn("SFTP は切断されました", panel.status_label.text())

    def test_upload_is_refused_without_opening_the_file_dialog(self):
        """アップロードのファイル選択も開かずに断ること。"""
        from ui import sftp_panel as mod
        m = self._manager()
        panel = self._panel_with_listing(m)
        self._drop(m)

        with mock.patch.object(mod.QFileDialog, "getOpenFileName",
                               return_value=("", "")) as chooser, \
                mock.patch.object(m, "upload_file") as upload_file:
            panel._on_upload()

        chooser.assert_not_called()
        upload_file.assert_not_called()

    def test_download_is_refused(self):
        """ダウンロードの保存先ダイアログも開かずに断ること。"""
        from ui import sftp_panel as mod
        m = self._manager()
        panel = self._panel_with_listing(m)
        info = self._file_info(panel)
        self._drop(m)

        with mock.patch.object(mod.QFileDialog, "getSaveFileName",
                               return_value=("", "")) as chooser, \
                mock.patch.object(m, "download_file") as download_file:
            panel._on_download_selected(info)

        chooser.assert_not_called()
        download_file.assert_not_called()

    def test_rename_is_refused(self):
        """名前変更の入力も開かずに断ること。"""
        from ui import sftp_panel as mod
        m = self._manager()
        panel = self._panel_with_listing(m)
        info = self._file_info(panel)
        self._drop(m)

        with mock.patch.object(mod.QInputDialog, "getText",
                               return_value=("", False)) as ask, \
                mock.patch.object(m, "rename_item") as rename_item:
            panel._on_rename_selected(info)

        ask.assert_not_called()
        rename_item.assert_not_called()

    def test_chmod_does_not_touch_the_dead_channel(self):
        """権限変更は、リンク先の問い合わせも入力も出さずに断ること。"""
        from ui import sftp_panel as mod
        m = self._manager()
        panel = self._panel_with_listing(m)
        info = self._file_info(panel)
        self._drop(m)

        with mock.patch.object(mod.QInputDialog, "getText",
                               return_value=("", False)) as ask, \
                mock.patch.object(m, "inspect_link_target") as inspect, \
                mock.patch.object(m, "change_permissions") as chmod:
            panel._on_chmod_selected(info)

        ask.assert_not_called()
        inspect.assert_not_called()
        chmod.assert_not_called()

    def test_creating_a_directory_is_refused(self):
        """新規フォルダの入力も開かずに断ること。"""
        from ui import sftp_panel as mod
        m = self._manager()
        panel = self._panel_with_listing(m)
        self._drop(m)

        with mock.patch.object(mod.QInputDialog, "getText",
                               return_value=("", False)) as ask, \
                mock.patch.object(m, "create_directory") as mkdir:
            panel._on_create_directory()

        ask.assert_not_called()
        mkdir.assert_not_called()

    def test_navigation_and_refresh_are_refused(self):
        """移動と更新も、死んだチャンネルへ投げないこと。"""
        m = self._manager()
        panel = self._panel_with_listing(m)
        self._drop(m)

        with mock.patch.object(m, "list_directory") as listing, \
                mock.patch.object(m, "change_directory") as chdir:
            panel._on_refresh()
            panel._on_go_up()
            panel._on_go_home()
            panel._on_item_double_clicked(panel.model.index(0, 0))

        listing.assert_not_called()
        chdir.assert_not_called()

    # --- タブを戻したときに問い合わせ直さない ---

    def test_reattaching_a_dropped_manager_does_not_ask_again(self):
        """切れたマネージャを繋ぎ直しても、一覧を取りに行かないこと。

        タブを別の機器へ移して戻すと _on_terminal_tab_changed が
        set_sftp_manager を呼び直す。切れたままなので、戻すたびに
        「SFTP接続がありません」の警告が出ていた。
        """
        m = self._manager()
        panel = self._panel_with_listing(m)
        self._drop(m)

        with mock.patch.object(m, "list_directory") as listing:
            panel.set_sftp_manager(m, "router-A", "192.0.2.10")

        listing.assert_not_called()
        self.assertIn("SFTP は切断されました", panel.status_label.text(),
                      "繋ぎ直したあとも切れたことが出ていない")

    # --- 対照: 生きている間はこれまでどおり ---

    def test_a_live_channel_still_lists_and_deletes(self):
        """対照: 繋がっている間は一覧も削除もこれまでどおり通ること。"""
        from ui import sftp_panel as mod
        m = self._manager()
        panel = self._panel_with_listing(m)
        panel.tree_view.setCurrentIndex(panel.model.index(0, 0))

        with mock.patch.object(mod.QMessageBox, "question",
                               return_value=mod.QMessageBox.StandardButton.Yes), \
                mock.patch.object(m, "delete_item") as delete_item:
            panel._on_delete()

        delete_item.assert_called_once_with("/flash/boot.bin", False)

    def test_a_live_manager_is_still_asked_for_a_listing(self):
        """対照: 繋がっているマネージャを繋いだら一覧を取りに行くこと。"""
        m = self._manager()
        panel = self._panel_with_listing(m)

        with mock.patch.object(m, "list_directory") as listing:
            panel.set_sftp_manager(m, "router-A", "192.0.2.10")

        listing.assert_called_once_with()
        self.assertEqual(panel.status_label.text(), "一覧を取得しています...")


if __name__ == "__main__":
    unittest.main()
