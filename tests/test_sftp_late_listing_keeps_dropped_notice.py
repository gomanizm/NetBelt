"""SFTP が切れたあとに届いた一覧で、切断の表示を消さないことを検証する。

実測（基準 097550c、src/core/sftp_manager.py:303-373 と
src/ui/sftp_panel.py:497）:
  list_directory のスレッドは listdir_attr（とリンクの stat）だけをロックの
  中で行い、変換・ソートはロックを離してから行う。その隙に利用者が行を
  右クリックして削除し、remove が切断（EOFError）で失敗すると、_fail が
  接続を畳み、パネルは『SFTP は切断されました（機器へ接続し直してください）』
  を出す。ところが変換を終えた一覧が遅れて届くと、_update_file_list が最後に
  status_label を『2 項目』で上書きし、切れたことが画面から消えていた。
  操作の入口は is_connected を見て断り続けるので送り先の取り違えは起きないが、
  利用者には一覧が生きているように見え、操作を断られる理由が分からない。

利用者の決定（2026-09-23、保守的な側）:
  SFTP のチャンネルだけが死んだときは、一覧の表示は残し、操作だけ止める。
  パネルには『SFTP は切断されました』と出す。

直し方:
  _update_file_list の最後で、マネージャが生きているときだけ件数を出し、
  切れていれば DROPPED_TEXT を出し直す。届いた一覧の行はそのまま並べる
  （一覧は残すという決定に沿う）。マネージャ側で切断後の一覧を捨てる手は、
  切断の時点で取れていた一覧まで失うので採らない。
"""
import os
import posixpath
import stat
import sys
import threading
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


class SftpLateListingKeepsDroppedNoticeTest(unittest.TestCase):
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

    def _pump(self, check, timeout=5.0):
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
        self.delivered = []
        return m

    def _panel_with_listing(self, m):
        """一覧を 1 行表示したパネルを返す。"""
        from ui.sftp_panel import SFTPPanel

        panel = SFTPPanel()
        type(self)._panels.append(panel)      # 親より先に捨てない
        panel.set_sftp_manager(m, "router-A", "192.0.2.10")
        self.assertTrue(self._pump(lambda: panel.model.rowCount() == 1),
                        "前提: 一覧が 1 行出ていること")
        # パネルより後に繋ぐので、ここに届いた時点でパネルは処理済み
        m.file_list_ready.connect(self.delivered.append)
        return panel

    def _hold_conversion(self):
        """次の一覧を、ロックを離したあとの変換で止める。

        止めるのは _format_permissions（listdir_attr を終えてロックを
        離してから呼ばれる）。戻り値は (変換に入った, 続けてよい)。
        """
        from core.sftp_manager import SFTPManager

        converting = threading.Event()
        may_finish = threading.Event()
        # 失敗で抜けても、止めたスレッドを待たせたままにしない
        self.addCleanup(may_finish.set)
        real_format = SFTPManager._format_permissions
        held = []

        def slow_format(mode):
            if not held:
                held.append(True)
                converting.set()
                may_finish.wait(10.0)
            return real_format(mode)

        patcher = mock.patch.object(SFTPManager, "_format_permissions",
                                    staticmethod(slow_format))
        patcher.start()
        self.addCleanup(patcher.stop)
        return converting, may_finish

    def test_a_listing_that_lands_after_the_drop_keeps_the_notice(self):
        """切断のあとに届いた一覧で『N 項目』へ戻さないこと。行は並べる。"""
        from ui import sftp_panel as mod
        m = self._manager()
        panel = self._panel_with_listing(m)
        converting, may_finish = self._hold_conversion()
        # 更新で届く一覧は 2 行（届いた一覧が並んだことを行数で見分ける）
        m.sftp_client.listdir_attr.return_value = [
            FakeAttr("boot.bin", stat.S_IFREG | 0o644),
            FakeAttr("old.cfg", stat.S_IFREG | 0o644),
        ]

        # 1) 更新を押す。一覧のスレッドは変換中（ロックは空いている）
        panel._on_refresh()
        self.assertTrue(converting.wait(5.0), "前提: 一覧の変換に入らない")

        # 2) その隙に削除すると、remove が切断で失敗して接続が畳まれる
        m.sftp_client.remove.side_effect = EOFError()
        with mock.patch.object(mod.QMessageBox, "question",
                               return_value=mod.QMessageBox.StandardButton.Yes):
            panel._on_delete_selected({"name": "boot.bin", "is_dir": False})
        self.assertFalse(m.is_connected, "前提: SFTP は畳まれている")
        self.assertEqual(panel.status_label.text(), mod.SFTPPanel.DROPPED_TEXT,
                         "前提: 切断の表示が出ている")

        # 3) 変換を終えた一覧が遅れて届く
        may_finish.set()
        self.assertTrue(self._pump(lambda: bool(self.delivered)),
                        "前提: 遅れた一覧が届かない")

        self.assertEqual(panel.model.rowCount(), 2,
                         "届いた一覧まで捨てた（一覧は残す決定）")
        self.assertEqual(panel.status_label.text(), mod.SFTPPanel.DROPPED_TEXT,
                         "切断の表示が遅れて届いた一覧で上書きされた")

    def test_a_listing_while_the_channel_is_live_still_shows_the_count(self):
        """対照: 生きているあいだの一覧は、これまでどおり件数を出すこと。"""
        m = self._manager()
        panel = self._panel_with_listing(m)
        m.sftp_client.listdir_attr.return_value = [
            FakeAttr("boot.bin", stat.S_IFREG | 0o644),
            FakeAttr("old.cfg", stat.S_IFREG | 0o644),
        ]

        panel._on_refresh()
        self.assertTrue(self._pump(lambda: bool(self.delivered)),
                        "前提: 一覧が届かない")

        self.assertEqual(panel.model.rowCount(), 2)
        self.assertEqual(panel.status_label.text(), "2 項目")


if __name__ == "__main__":
    unittest.main()
