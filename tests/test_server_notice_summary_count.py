"""SFTP・FTP の「表示が追いつかず N 件の通知を省略しました」の N が過少になる件。

何が起きていたか（実測、基準 441ea02。ネットワークは使わず、通知の枠だけを
本物と同じく別スレッドの emit で動かした）: 上限 3 に 10 件を出すと、競合が
無ければ要約は「7 件」で正しい。ところが _on_notice_delivered は、錠の中で
省略件数を取り出して 0 に戻し、錠を放してから _emit_activity（の中の
_take_notice）で要約の枠を取り直していた。その隙間に別スレッド（転送の
ハンドラ）が空いた枠を埋めると、要約そのものが省略されて省略件数に 1 が
足されるだけになり、元の 7 件は失われた。出そうとした 15 件に対し、届いた
6 件と報告の [3] を足しても 9 件にしかならなかった。枠の状態を決め打ちした
再現でも、5 件を省いたのに「1 件の通知を省略しました」と出た。FTP の
FTPServerManager も同じ形で、同じ結果だった。

どう直したか: SFTP と FTP の _on_notice_delivered で、配送待ちが 0 になって
省略件数があるときは、取り出すのと同じ錠の区間で要約の枠
（_pending_notices += 1）を確保し、錠の外では client_activity を直に出す
（配送待ちは 0 だったので必ず取れる）。要約の 1 行もこれまでどおり配送待ちに
数え、届けば同じ受け口で戻る。

確かめ方: 省略件数を 0 に戻した錠の区間を抜けた直後に、別スレッドから枠を
埋める（_notice_lock を包む）。作りに依らず、錠の外で要約の枠を取る実装なら
この差し込みで件数を失う。件数の正確さは「届いた通知＋報告された省略件数の
合計が、出そうとした件数と一致する」で見る。
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


class _RefillingLock:
    """_notice_lock の代わり。省略件数を 0 に戻した区間を抜けた直後に 1 回だけ on_reset を呼ぶ"""

    def __init__(self, manager, on_reset):
        self._lock = threading.Lock()
        self._m = manager
        self._on_reset = on_reset
        self.fired = False
        self._had = 0

    def __enter__(self):
        self._lock.acquire()
        self._had = self._m._dropped_notices
        return self

    def __exit__(self, *exc):
        reset = self._had > 0 and self._m._dropped_notices == 0
        self._lock.release()
        if reset and not self.fired and \
                threading.current_thread() is threading.main_thread():
            self.fired = True
            self._on_reset()
        return False


class _SummaryCountCases:
    """SFTP・FTP で共通の確かめ方。_new_manager を実装した側で流す"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self, limit):
        from PyQt6.QtCore import Qt
        m = self._new_manager()
        m.max_pending_notices = limit
        log = []
        m.client_activity.connect(lambda ip, msg: log.append(msg),
                                  Qt.ConnectionType.DirectConnection)
        return m, log

    def _pump(self, rounds=60):
        for _ in range(rounds):
            self.app.processEvents()

    def test_count_without_race(self):
        m, log = self._manager(3)
        in_thread(lambda: [m._emit_activity("192.0.2.1", "refused %d" % i)
                           for i in range(10)])
        self._pump()
        self.assertEqual(reported(log), [7], log)

    def test_count_when_the_freed_slots_are_refilled(self):
        m, log = self._manager(3)
        issued_other = []

        def refill():
            # 別スレッド（転送のハンドラ）が、空いた枠を上限を超えて埋めにくる
            def work():
                for j in range(m.max_pending_notices + 2):
                    m._emit_activity("192.0.2.2", "other %d" % j)
                    issued_other.append(j)
            in_thread(work)

        m._notice_lock = _RefillingLock(m, refill)
        in_thread(lambda: [m._emit_activity("192.0.2.1", "refused %d" % i)
                           for i in range(10)])
        self._pump()

        self.assertTrue(m._notice_lock.fired,
                        "前提: 枠を埋める差し込みが動かなかった")
        delivered = [msg for msg in log if not SUMMARY.search(msg)]
        attempted = 10 + len(issued_other)
        got = reported(log)
        self.assertEqual(
            len(delivered) + sum(got), attempted,
            "報告された省略件数 %r と届いた %d 件の合計が、出そうとした %d 件と"
            "合わない（省略件数が失われた）" % (got, len(delivered), attempted))
        self.assertIn(7, got, "最初に省いた 7 件がそのまま報告されていない: %r"
                      % got)
        self.assertEqual(m._pending_notices, 0,
                         "配送待ちの数が戻っていない: %d" % m._pending_notices)


class SftpSummaryCountTest(_SummaryCountCases, unittest.TestCase):
    def _new_manager(self):
        from core.sftp_server import SFTPServerManager
        return SFTPServerManager()


class FtpSummaryCountTest(_SummaryCountCases, unittest.TestCase):
    def _new_manager(self):
        from core.ftp_server import FTPServerManager
        return FTPServerManager()


if __name__ == "__main__":
    unittest.main()
