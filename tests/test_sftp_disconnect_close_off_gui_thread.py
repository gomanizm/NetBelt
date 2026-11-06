# -*- coding: utf-8 -*-
"""切断の client.close() が GUI スレッドを止めないことを検証する。

disconnect() はタブを閉じる・アプリを終える操作で GUI スレッドから呼ばれる。
SFTPClient.close() はチャンネルの close を transport へ送るので、送信が
詰まった機器では戻らない。これまでは期限（_DISCONNECT_WAIT_SECONDS）が
ロック待ちにしか効かず、close そのものは GUI スレッドで無期限に待っていた。

close は別のスレッドで行い、待つのは切断の期限の残りまでにすること。
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class DisconnectCloseOffGuiThreadTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self):
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        type(self)._keep.append(m)
        m.is_connected = True
        m.sftp_client = mock.Mock()
        m.sftp_client.get_channel.return_value.closed = False
        m._DISCONNECT_WAIT_SECONDS = 0.3
        return m

    def test_a_close_that_never_returns_does_not_hold_the_gui_thread(self):
        m = self._manager()
        client = m.sftp_client
        release = threading.Event()
        self.addCleanup(release.set)
        closing_threads = []

        def stuck_close():
            closing_threads.append(threading.current_thread())
            release.wait(10)
        client.close.side_effect = stuck_close
        notices = []
        m.disconnected.connect(lambda: notices.append(True))

        started = time.monotonic()
        m.disconnect()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.0,
                        "close が戻るまで GUI スレッドが止まった: %.2f 秒" % elapsed)
        self.assertEqual(notices, [True], "切断の通知が出ない")
        self.assertIsNone(m.sftp_client)
        client.close.assert_called_once_with()
        self.assertIsNot(closing_threads[0], threading.current_thread(),
                         "close を GUI スレッドで呼んでいる")

    def test_a_quick_close_has_finished_when_disconnect_returns(self):
        """すぐ戻る close は、これまでどおり disconnect の中で終わっていること。"""
        m = self._manager()
        client = m.sftp_client
        m.disconnect()
        client.close.assert_called_once_with()

    def test_a_failing_close_still_reports_the_disconnect(self):
        m = self._manager()
        m.sftp_client.close.side_effect = OSError("Socket is closed")
        notices = []
        m.disconnected.connect(lambda: notices.append(True))
        m.disconnect()
        self.assertEqual(notices, [True])


if __name__ == "__main__":
    unittest.main()
