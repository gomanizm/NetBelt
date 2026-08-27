"""ダイアログを開いている間に接続が変わっても、操作が迷子にならないことを検証する。

SFTP パネルの各操作は「先頭で sftp_manager の有無を確かめる → ダイアログを
開く → 戻ってきて sftp_manager を使う」という順で書かれている。
QFileDialog も QMessageBox も、モーダルでも Qt のイベントループを回すので、
開いている間に次の2つが起こり得る。

1. SSH セッションが切れる（機器の exec-timeout、VPN の瞬断）。
   main_window の _on_connection_closed が sftp_panel.clear() を呼び、
   sftp_manager が None になる。戻ってきた側は AttributeError で落ちる。
   これはアクションのスロット、つまり C++ 側から呼ばれたコードの中の
   未処理例外なので、PyQt6 はプロセスごと落とす。

2. 利用者が別のターミナルタブへ切り替える。パネルは表示中のタブに追従
   するので（test_sftp_panel_follows_tab.py）、sftp_manager が別機器の
   ものへ差し替わる。この場合は落ちない代わりに、**A に送るつもりの
   ファイルが B へ送られる**。削除・改名・パーミッション変更も同じで、
   落ちるより明確に悪い。

したがって「None でないこと」の再確認では足りない。操作を始めた時点の
マネージャと同一であることまで確かめる必要がある。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpPanelStaleManagerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel_with_manager(self):
        """マネージャを1つ繋いだパネルと、そのマネージャを返す。"""
        from ui.sftp_panel import SFTPPanel
        panel = SFTPPanel()
        manager = mock.Mock()
        manager.current_path = "/"
        manager.get_current_path.return_value = "/"
        panel.set_sftp_manager(manager, "rtrA")
        manager.reset_mock()
        manager.get_current_path.return_value = "/"
        return panel, manager

    @staticmethod
    def _file_info(name="running-config"):
        return {
            'name': name,
            'size': 4096,
            'mtime': 1700000000,
            'mode': 0o100644,
            'is_dir': False,
            'permissions': '-rw-r--r--',
        }

    # --- 1. ダイアログ中に切断された ---

    def test_upload_is_abandoned_when_the_session_drops_during_the_dialog(self):
        """ファイル選択中に切れたら、アップロードを実行しないこと。"""
        from ui import sftp_panel as mod
        panel, manager = self._panel_with_manager()

        def choose_a_file_then_lose_the_session(*args, **kwargs):
            panel.clear()
            return ("C:/tmp/new.cfg", "")

        with mock.patch.object(mod.QFileDialog, "getOpenFileName",
                               side_effect=choose_a_file_then_lose_the_session):
            panel._on_upload()

        manager.upload_file.assert_not_called()

    def test_download_is_abandoned_when_the_session_drops_during_the_dialog(self):
        """保存先の選択中に切れたら、ダウンロードを実行しないこと。"""
        from ui import sftp_panel as mod
        panel, manager = self._panel_with_manager()

        def choose_a_path_then_lose_the_session(*args, **kwargs):
            panel.clear()
            return ("C:/tmp/saved.cfg", "")

        with mock.patch.object(mod.QFileDialog, "getSaveFileName",
                               side_effect=choose_a_path_then_lose_the_session):
            panel._on_download_selected(self._file_info())

        manager.download_file.assert_not_called()

    def test_delete_is_abandoned_when_the_session_drops_during_the_confirm(self):
        """削除確認の最中に切れたら、削除を実行しないこと。"""
        from PyQt6.QtWidgets import QMessageBox
        from ui import sftp_panel as mod
        panel, manager = self._panel_with_manager()

        def confirm_then_lose_the_session(*args, **kwargs):
            panel.clear()
            return QMessageBox.StandardButton.Yes

        with mock.patch.object(mod.QMessageBox, "question",
                               side_effect=confirm_then_lose_the_session):
            panel._on_delete_selected(self._file_info())

        manager.delete_item.assert_not_called()

    def test_mkdir_is_abandoned_when_the_session_drops_during_the_dialog(self):
        """フォルダ名の入力中に切れたら、作成を実行しないこと。"""
        from ui import sftp_panel as mod
        panel, manager = self._panel_with_manager()

        def type_a_name_then_lose_the_session(*args, **kwargs):
            panel.clear()
            return ("backup", True)

        with mock.patch.object(mod.QInputDialog, "getText",
                               side_effect=type_a_name_then_lose_the_session):
            panel._on_create_directory()

        manager.create_directory.assert_not_called()

    def test_rename_is_abandoned_when_the_session_drops_during_the_dialog(self):
        """名前の入力中に切れたら、改名を実行しないこと。"""
        from ui import sftp_panel as mod
        panel, manager = self._panel_with_manager()

        def type_a_name_then_lose_the_session(*args, **kwargs):
            panel.clear()
            return ("renamed.cfg", True)

        with mock.patch.object(mod.QInputDialog, "getText",
                               side_effect=type_a_name_then_lose_the_session):
            panel._on_rename_selected(self._file_info())

        manager.rename_item.assert_not_called()

    def test_chmod_is_abandoned_when_the_session_drops_during_the_dialog(self):
        """パーミッションの入力中に切れたら、変更を実行しないこと。"""
        from ui import sftp_panel as mod
        panel, manager = self._panel_with_manager()

        def type_a_mode_then_lose_the_session(*args, **kwargs):
            panel.clear()
            return ("600", True)

        with mock.patch.object(mod.QInputDialog, "getText",
                               side_effect=type_a_mode_then_lose_the_session):
            panel._on_chmod_selected(self._file_info())

        manager.change_permissions.assert_not_called()

    # --- 2. ダイアログ中に別機器へ切り替わった ---

    def test_upload_does_not_go_to_the_device_that_was_switched_to(self):
        """ファイル選択中にタブが変わったら、どちらの機器へも送らないこと。

        落ちないぶん、こちらの方が危ない。rtrA へ送るつもりの
        コンフィグが rtrB に置かれても、その場では誰も気づかない。
        """
        from ui import sftp_panel as mod
        panel, rtr_a = self._panel_with_manager()
        rtr_b = mock.Mock()
        rtr_b.current_path = "/"
        rtr_b.get_current_path.return_value = "/"

        def choose_a_file_then_switch_tabs(*args, **kwargs):
            panel.set_sftp_manager(rtr_b, "rtrB")
            return ("C:/tmp/new.cfg", "")

        with mock.patch.object(mod.QFileDialog, "getOpenFileName",
                               side_effect=choose_a_file_then_switch_tabs):
            panel._on_upload()

        rtr_a.upload_file.assert_not_called()
        rtr_b.upload_file.assert_not_called()

    def test_delete_does_not_hit_the_device_that_was_switched_to(self):
        """削除確認の最中にタブが変わったら、どちらの機器も消さないこと。"""
        from PyQt6.QtWidgets import QMessageBox
        from ui import sftp_panel as mod
        panel, rtr_a = self._panel_with_manager()
        rtr_b = mock.Mock()
        rtr_b.current_path = "/"
        rtr_b.get_current_path.return_value = "/"

        def confirm_then_switch_tabs(*args, **kwargs):
            panel.set_sftp_manager(rtr_b, "rtrB")
            return QMessageBox.StandardButton.Yes

        with mock.patch.object(mod.QMessageBox, "question",
                               side_effect=confirm_then_switch_tabs):
            panel._on_delete_selected(self._file_info())

        rtr_a.delete_item.assert_not_called()
        rtr_b.delete_item.assert_not_called()

    # --- 3. 何も起きなければ、これまでどおり動くこと ---

    def test_upload_still_happens_when_nothing_changed(self):
        """接続が変わらなければ、これまでどおりアップロードすること。"""
        from ui import sftp_panel as mod
        panel, manager = self._panel_with_manager()

        with mock.patch.object(mod.QFileDialog, "getOpenFileName",
                               return_value=("C:/tmp/new.cfg", "")):
            panel._on_upload()

        manager.upload_file.assert_called_once_with("C:/tmp/new.cfg")

    def test_delete_still_happens_when_nothing_changed(self):
        """接続が変わらなければ、これまでどおり削除すること。"""
        from PyQt6.QtWidgets import QMessageBox
        from ui import sftp_panel as mod
        panel, manager = self._panel_with_manager()

        with mock.patch.object(mod.QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes):
            panel._on_delete_selected(self._file_info("old.cfg"))

        manager.delete_item.assert_called_once_with("/old.cfg", False)


if __name__ == "__main__":
    unittest.main()
