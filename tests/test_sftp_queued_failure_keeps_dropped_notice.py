"""畳んだあとに届いた順番待ちの失敗が、SFTP パネルの切断表示を上書きしないことを検証する。

実測（基準 441ea02）:
  3 件まとめてアップロードする途中で機器が SFTP のチャンネルを閉じると
  （localhost の実 paramiko サーバ。SSH のトランスポートは生きたまま）、
  1 件目の失敗で接続が畳まれ、パネルは『SFTP は切断されました…』になる。
  ところがロック待ちだった 2・3 件目が src/core/sftp_manager.py:731-732 で
  IOError('SFTP接続がありません') を上げ、_fail が畳まない文面のまま
  error_occurred を出す。これは別スレッドからのキュー配送で、1 件目の
  disconnected との届く順は決まらない。後から届くと
  SFTPPanel._on_error（src/ui/sftp_panel.py:568）が無条件に
  『エラー: アップロードエラー: SFTP接続がありません』を書き、切断の表示が
  消えた（6 回中 4 回。警告は毎回 3 枚、disconnected は 1 回）。
  パネルは切れたまま操作を断るのに、表示からはそれが分からなくなる。

直し方:
  _on_error で、いまのマネージャが既に畳まれている（_manager_is_live() が
  False）なら、状態欄は『エラー: …』ではなく DROPPED_TEXT にする。警告
  （モーダル）はこれまでどおり理由を出す。届くのが遅れた一覧に対して
  _update_file_list が取っているのと同じ扱い。繋がっている間の失敗は
  これまでどおり『エラー: …』と出す。
"""
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _Attr:
    filename = "boot.bin"
    st_mode = 0o100644
    st_size = 1
    st_mtime = 0


class QueuedFailureKeepsDroppedNoticeTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, check, seconds=3.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _panel_with_listing(self):
        """一覧 1 行を表示した実 SFTPPanel と実 SFTPManager"""
        from core.sftp_manager import SFTPManager
        from ui.sftp_panel import SFTPPanel
        m = SFTPManager()
        m.is_connected = True
        m.current_path = "/flash"
        c = m.sftp_client = mock.Mock()
        c.get_channel.return_value.closed = False
        c.listdir_attr.return_value = [_Attr()]
        panel = SFTPPanel()
        # 後片付けで C++ 側が先に消えないよう、テストの間は参照を持ち続ける
        type(self)._keep += [m, panel]
        panel.set_sftp_manager(m, "router-A", "192.0.2.10")
        self.assertTrue(self._pump(lambda: panel.model.rowCount() == 1),
                        "前提: 一覧が出ない")
        return m, panel

    def test_a_queued_failure_after_the_fold_keeps_the_dropped_notice(self):
        """畳んだあとに届いた順番待ちの失敗で、切断の表示を上書きしないこと。"""
        from ui import sftp_panel as mod
        m, panel = self._panel_with_listing()
        with mock.patch.object(mod.QMessageBox, "warning") as warning:
            m._fail("アップロードエラー", TimeoutError())     # 1 件目で畳む
            self.assertEqual(panel.status_label.text(), mod.SFTPPanel.DROPPED_TEXT,
                             "前提: 畳んだら切断の表示になる")
            # ロック待ちだった 2 件目の失敗が、あとから届く
            m.error_occurred.emit("アップロードエラー: SFTP接続がありません")
        self.assertEqual(panel.status_label.text(), mod.SFTPPanel.DROPPED_TEXT,
                         "後から届いた失敗が切断の表示を上書きした")
        self.assertEqual(warning.call_count, 2, "警告（理由）はこれまでどおり出ること")
        self.assertEqual(warning.call_args[0][2],
                         "アップロードエラー: SFTP接続がありません")
        self.assertEqual(panel.model.rowCount(), 1, "一覧の行は残すこと")

    def test_a_failure_arriving_before_the_disconnect_notice_shows_dropped(self):
        """disconnected より先に届いた失敗でも、畳まれていれば切断の表示にすること。"""
        from ui import sftp_panel as mod
        m, panel = self._panel_with_listing()
        # 転送スレッドの disconnect() が is_connected を落とし、通知はまだ届いていない
        m.is_connected = False
        with mock.patch.object(mod.QMessageBox, "warning") as warning:
            m.error_occurred.emit("アップロードエラー: SFTP接続がありません")
        self.assertEqual(panel.status_label.text(), mod.SFTPPanel.DROPPED_TEXT)
        warning.assert_called_once()

    def test_a_failure_on_a_live_channel_is_still_shown(self):
        """対照: 繋がっている間の失敗は、これまでどおり状態欄にも出すこと。"""
        from ui import sftp_panel as mod
        m, panel = self._panel_with_listing()
        with mock.patch.object(mod.QMessageBox, "warning") as warning:
            m.error_occurred.emit("削除エラー: Permission denied")
        self.assertEqual(panel.status_label.text(),
                         "エラー: 削除エラー: Permission denied")
        warning.assert_called_once()
        self.assertTrue(m.is_connected)

    def test_no_modal_while_closing_but_dropped_notice_stays(self):
        """対照: 終了処理中はモーダルを出さず、畳まれていれば切断の表示にすること。"""
        from ui import sftp_panel as mod
        m, panel = self._panel_with_listing()
        m.is_connected = False
        with mock.patch.object(mod.SFTPPanel, "_closing", True), \
                mock.patch.object(mod.QMessageBox, "warning") as warning:
            m.error_occurred.emit("アップロードエラー: SFTP接続がありません")
        warning.assert_not_called()
        self.assertEqual(panel.status_label.text(), mod.SFTPPanel.DROPPED_TEXT)


if __name__ == "__main__":
    unittest.main()
