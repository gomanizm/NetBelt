"""始まりを省いた相手の終わり（切断・完了）を省いたとき、省略件数の要約が遅れずに出ること。

何が起きていたか（実測、ebbe593。ネットワークは使わず、通知の枠だけを本物と
同じく GUI の外のスレッドから動かした）: 配送待ちの上限で接続（FTP は転送の
開始）を省いた相手は、その切断（完了・中断）も出さずに省略件数へ足すだけに
している（SFTP の _take_closing_notice(shown=False)、FTP の row=='hidden'）。
ところが要約を出すきっかけは「配送待ちが 0 に戻った通知の配送」だけなので、
そのとき配送待ちがすでに 0 だと、足した件数は次に何か 1 件届くまで（来なければ
サーバーを止めるまで）パネルに出なかった。上限 1 で A を届け B を省いた後、
GUI が追いついて「1 件の通知を省略しました」が出た状態から B が切断すると、
pending=0 dropped=1 のまま要約が増えなかった。

どう直したか: 省略件数に足すのと同じ錠の区間で、配送待ちが 0 なら要約の枠を
取り（_on_notice_delivered と同じ形。取り出すのと同じ錠の中で取るので件数を
失わない）、錠の外で要約を出す。要約も配送待ちに数えるので、GUI が塞がって
いる間に省いた終わりが続いても、要約は配送待ちの枠の分しか積まれない。
"""
import os
import re
import sys
import threading
import unittest

sys.path.insert(0, "src")

SUMMARY = re.compile(r"表示が追いつかず (\d+) 件の通知を省略しました")


def reported(log):
    return [int(m.group(1)) for msg in log for m in [SUMMARY.search(msg)] if m]


def in_thread(fn):
    """本物と同じく、GUI 以外のスレッドから出す（配送待ちに積まれる）"""
    t = threading.Thread(target=fn)
    t.start()
    t.join()


class _HiddenCloseCases:
    """_new_manager / _open / _close を実装した側で流す。

    _open は始まり（接続・転送の開始）を 1 件、_close はその終わりを 1 件出す。
    どちらも待受・ハンドラのスレッドと同じ手順で、配送待ちの枠を取ってから出す
    """

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtCore import Qt
        self.m = self._new_manager()
        self.m.max_pending_notices = 1
        self.log = []
        # 出た時点で数える（DirectConnection は出したスレッドで呼ばれる）
        self.m.client_activity.connect(lambda ip, msg: self.log.append(msg),
                                       Qt.ConnectionType.DirectConnection)

    def _pump(self, rounds=60):
        for _ in range(rounds):
            self.app.processEvents()

    def test_summary_for_a_hidden_close_is_not_delayed(self):
        m = self.m
        # A は届き、B は上限で省かれる
        in_thread(lambda: (self._open("192.0.2.1"), self._open("192.0.2.2")))
        self._pump()
        # 前提: B の始まりを省いた分の要約が出て、配送待ちは 0 に戻っている
        self.assertEqual(reported(self.log), [1], self.log)
        self.assertEqual(m._pending_notices, 0)
        self.assertEqual(m._dropped_notices, 0)

        # B の終わりは省く。この後に届く通知は無いので、出すならここしかない
        in_thread(lambda: self._close("192.0.2.2"))
        self._pump()
        self.assertEqual(
            reported(self.log), [1, 1],
            "省いた B の終わりの 1 件が報告されない（pending=%d dropped=%d）"
            % (m._pending_notices, m._dropped_notices))
        self.assertEqual(m._dropped_notices, 0)
        self.assertEqual(m._pending_notices, 0)

    def test_hidden_closes_while_the_gui_is_busy_stay_counted_and_bounded(self):
        m = self.m
        hidden = ["192.0.2.%d" % i for i in range(2, 7)]
        in_thread(lambda: [self._open(ip) for ip in ["192.0.2.1"] + hidden])
        self._pump()
        self.assertEqual(reported(self.log), [len(hidden)], self.log)

        # GUI を回さないまま、省いた相手が続けて終わる
        in_thread(lambda: [self._close(ip) for ip in hidden])
        emitted = reported(self.log)[1:]
        # 要約も配送待ちに数えるので、GUI が塞がっている間は枠（1）の分しか出ない
        self.assertLessEqual(len(emitted), 1,
                             "GUI が塞がっている間に要約が積み上がった: %r" % emitted)
        self._pump()
        got = reported(self.log)
        self.assertEqual(sum(got), 2 * len(hidden),
                         "省いた始まりと終わりの件数が報告と合わない: %r" % got)
        self.assertEqual(m._dropped_notices, 0)
        self.assertEqual(m._pending_notices, 0)


class SftpHiddenCloseTest(_HiddenCloseCases, unittest.TestCase):
    """接続を省いた相手の切断（待受の _take_notice と、ハンドラの後始末と同じ手順）"""

    def _new_manager(self):
        from core.sftp_server import SFTPServerManager
        self.shown = {}
        return SFTPServerManager()

    def _open(self, ip):
        shown = self.m._take_notice()
        self.shown[ip] = shown
        if shown:
            self.m.client_connected.emit(ip)

    def _close(self, ip):
        if self.m._take_closing_notice(self.shown.pop(ip, None)):
            self.m.client_disconnected.emit(ip)


class FtpHiddenCloseTest(_HiddenCloseCases, unittest.TestCase):
    """開始を省いた転送の完了"""

    def _new_manager(self):
        from core.ftp_server import FTPServerManager
        return FTPServerManager()

    def _open(self, ip):
        self.m._emit_started(ip, "get.cfg", 10, "download")

    def _close(self, ip):
        self.m._emit_complete(ip, "get.cfg", 10, 10, "download")


class TftpHiddenCloseTest(_HiddenCloseCases, unittest.TestCase):
    """開始を省いた転送の完了（TFTP の row == 'hidden' も同じ形だった）。

    TFTP は要約を配送待ちに数えずに出していた（client_activity を配送待ちの
    受け口へつないでいなかった）。その形のまま終わりの側でも要約を出すと、
    GUI が塞がっている間に省いた終わりの数だけ要約が積み上がる（実測: 下の
    2 つ目のテストで 5 件）。SFTP・FTP と同じく要約も数える形にそろえた
    """

    def _new_manager(self):
        from core.tftp_server import TFTPServerManager
        return TFTPServerManager()

    def _open(self, ip):
        self.m._on_event("transfer_started", ip, ("get.cfg", 10, "download"))

    def _close(self, ip):
        self.m._on_event("transfer_complete", ip, ("get.cfg", 10, 10, "download"))


if __name__ == "__main__":
    unittest.main()
