"""畳んだあとに届いた完了の通知が、SFTP パネルの切断表示を消さないことを検証する。

実測（e34c1ac。症状は基準 441ea02 から在る）:
  ダウンロードの転送スレッドは get() のあとロックを離し、保存先を os.replace で
  確定してから transfer_complete を出す（src/core/sftp_manager.py の
  download_thread）。get() のあとで機器が SFTP のチャンネルを閉じ、ロック待ち
  だった一覧がその間に閉じたチャンネルで失敗して畳むと、disconnected が先に
  届く。あとから届いた『ダウンロード完了: startup.cfg』を
  SFTPPanel._on_transfer_complete（src/ui/sftp_panel.py:550-558）が無条件に
  書くので、切断の表示が消えた（人工の遅延なしで 10 回中 5 回。os.replace を
  0.2 秒遅らせると毎回。状態欄の順は [DROPPED_TEXT, DROPPED_TEXT,
  'ダウンロード完了: startup.cfg']、is_connected は False）。
  パネルは切れたまま操作を断るのに、表示からはそれが分からなくなる。

直し方:
  _on_transfer_complete で、いまのマネージャが既に畳まれている
  （_manager_is_live() が False）なら、完了の知らせと切断の表示を両方出す
  （『<完了メッセージ>。<DROPPED_TEXT>』）。完了は本当のことで、完了には
  モーダルが無いので、DROPPED_TEXT だけに置き換えると知らせが消える。
  繋がっている間の完了はこれまでどおり完了メッセージだけを出す。
  畳んだあとの通知（disconnected・順番待ちの失敗・遅れた一覧）は、状態欄が
  既に切断の表示で終わっていれば書き直さない（_show_dropped）。完了だけ直すと、
  その完了のあとに disconnected が届いた回で知らせが消えた（10 回中 4 回）。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _Attr:
    filename = "startup.cfg"
    st_mode = 0o100644
    st_size = 6
    st_mtime = 0


class LateCompletionKeepsDroppedNoticeTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, check=lambda: False, seconds=3.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _panel_with_listing(self):
        """一覧 1 行を表示した実 SFTPPanel と実 SFTPManager（クライアントは mock）"""
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

    def test_a_download_completion_after_the_fold_keeps_the_dropped_notice(self):
        """畳んだあとに届いたダウンロード完了で、切断の表示を消さないこと。"""
        from PyQt6.QtCore import Qt
        from core.sftp_manager import SFTPManager
        from ui import sftp_panel as mod
        m, panel = self._panel_with_listing()
        chan = m.sftp_client.get_channel.return_value
        work = tempfile.mkdtemp(prefix="netbelt-late-complete-")
        self.addCleanup(shutil.rmtree, work, True)
        local = os.path.join(work, "startup.cfg")

        def listdir_attr(path):
            if chan.closed is True:
                raise OSError("Socket is closed")
            return [_Attr()]
        m.sftp_client.listdir_attr.side_effect = listdir_attr
        got = threading.Event()

        def fake_get(remote, local_tmp, callback=None):
            with open(local_tmp, "wb") as f:
                f.write(b"REMOTE")
            chan.closed = True          # 転送のあと、機器が SFTP のチャンネルを閉じた
            got.set()
        m.sftp_client.get.side_effect = fake_get

        # 転送スレッドが完了を出す直前（保存先の確定のあと）で、畳まれるのを待つ。
        # これで disconnected が完了より先に GUI へ届く順を決定的に作る
        folded = threading.Event()
        m.disconnected.connect(folded.set, Qt.ConnectionType.DirectConnection)

        def release_after_fold(key):
            folded.wait(5)
            return SFTPManager._release_download_target(key)
        m._release_download_target = release_after_fold

        with mock.patch.object(mod.QMessageBox, "warning") as warning:
            m.download_file("/flash/startup.cfg", local, overwrite=True)
            self.assertTrue(got.wait(5), "前提: 転送が始まらない")
            m.list_directory()          # ロック待ちの一覧が、閉じたチャンネルで失敗して畳む
            self.assertTrue(folded.wait(5), "前提: 一覧の失敗で畳まれない")
            done = "ダウンロード完了: startup.cfg"
            self.assertTrue(self._pump(lambda: done in panel.status_label.text()),
                            "前提: 完了が届かない（表示 %r）" % panel.status_label.text())
            self._pump(seconds=0.3)
        shown = panel.status_label.text()
        self.assertFalse(m.is_connected)
        self.assertTrue(shown.endswith(mod.SFTPPanel.DROPPED_TEXT),
                        "後から届いた完了が切断の表示を消した: %r" % shown)
        self.assertTrue(shown.startswith(done), "完了の知らせが消えた: %r" % shown)
        with open(local, "rb") as f:
            self.assertEqual(f.read(), b"REMOTE", "前提: 保存先が確定していない")
        warning.assert_called_once()
        self.assertEqual(panel.model.rowCount(), 1, "一覧の行は残すこと")

    def test_a_completion_arriving_before_the_disconnect_notice_keeps_both(self):
        """disconnected より先に届いた完了でも、畳まれていれば両方を出すこと。"""
        from ui import sftp_panel as mod
        m, panel = self._panel_with_listing()
        # 別スレッドの disconnect() が is_connected を落とし、通知はまだ届いていない
        m.is_connected = False
        m.transfer_complete.emit("アップロード完了: new.cfg")
        self.assertEqual(panel.status_label.text(),
                         "アップロード完了: new.cfg。" + mod.SFTPPanel.DROPPED_TEXT)

    def test_later_dropped_notices_keep_the_late_completion(self):
        """畳んだあとの完了のあとに disconnected・順番待ちの失敗・遅れた一覧が
        届いても、完了の知らせを消さないこと（届く順は決まらない）。"""
        from ui import sftp_panel as mod
        m, panel = self._panel_with_listing()
        both = "ダウンロード完了: startup.cfg。" + mod.SFTPPanel.DROPPED_TEXT
        m.is_connected = False
        m.transfer_complete.emit("ダウンロード完了: startup.cfg")
        self.assertEqual(panel.status_label.text(), both, "前提: 両方出ていない")
        m.disconnected.emit()
        self.assertEqual(panel.status_label.text(), both,
                         "後から届いた disconnected が完了の知らせを消した")
        with mock.patch.object(mod.QMessageBox, "warning") as warning:
            m.error_occurred.emit("ダウンロードエラー: SFTP接続がありません")
        self.assertEqual(panel.status_label.text(), both,
                         "後から届いた失敗が完了の知らせを消した")
        warning.assert_called_once()     # 失敗の理由はこれまでどおり警告で出す
        m.file_list_ready.emit([{"name": "startup.cfg", "size": 6, "mtime": 0,
                                 "mode": 0o100644, "is_dir": False,
                                 "is_link": False, "permissions": "-rw-r--r--"}])
        self.assertEqual(panel.status_label.text(), both,
                         "後から届いた一覧が完了の知らせを消した")

    def test_a_disconnect_after_a_live_completion_shows_the_dropped_notice(self):
        """対照: 繋がっている間に完了を出したあとで切れたら、切断の表示にすること。"""
        from ui import sftp_panel as mod
        m, panel = self._panel_with_listing()
        m.transfer_complete.emit("アップロード完了: new.cfg")
        with mock.patch.object(mod.QMessageBox, "warning"):
            m._fail("ディレクトリ一覧取得エラー", TimeoutError())
        self.assertEqual(panel.status_label.text(), mod.SFTPPanel.DROPPED_TEXT)

    def test_a_completion_on_a_live_channel_is_shown_alone(self):
        """対照: 繋がっている間の完了は、これまでどおり完了メッセージだけを出すこと。"""
        m, panel = self._panel_with_listing()
        m.transfer_complete.emit("ダウンロード完了: startup.cfg")
        self.assertEqual(panel.status_label.text(), "ダウンロード完了: startup.cfg")
        self.assertTrue(m.is_connected)


if __name__ == "__main__":
    unittest.main()
