"""SFTP の接続・切断通知に配送待ちの上限があることを検証する。

実測（基準 16101ef）: SFTPServerPanel を作り、GUI のイベント処理を止めたまま
localhost から TCP 接続→即切断を繰り返すと、認証を一度も通さない接続だけで
client_connected と client_disconnected の両方が出た。5 秒で TCP 4516 回を投げて
受理 1816 回 → 通知 3632 件が Qt の配送キューに滞留（GUI の処理は 0 件）。
20 秒版では 13130 件、ワーキングセット 59.8MB → 71.0MB（1 件あたり約 856 バイト）、
発行速度 656〜939 件/秒。32 接続の上限は切断ごとに枠が戻るので累積を止めない。
パネルの 1000 行上限（src/ui/sftp_server_panel.py:141）が効くのは配送の後なので、
キューの増大は止められなかった（復帰時の停滞は 13130 件で 0.12〜0.13 秒なので、
実害の中心は停滞ではなく滞留メモリ・最大で毎分約 34MB）。
FTP(src/core/ftp_server.py:66) と TFTP(src/core/tftp_server.py:743) は
max_pending_notices を持っており、src/core/sftp_server.py だけに
_take_notice 系が 1 つも無かった。

直し方: FTP/TFTP と同じ _take_notice / _on_notice_delivered / max_pending_notices
を SFTPServerManager にも置き、client_connected と client_disconnected をそれで包む。
接続を届けた相手の切断は上限を超えても必ず届ける（届けないとパネルの
「接続クライアント: N」が戻らない）。はけた時点で「表示が追いつかず N 件の通知を
省略しました」を 1 行出す、既存の作法に揃える。
"""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

USER = "netbelt"
PASSWORD = "pw"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SftpNoticeBacklogTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = mock.patch("core.config_manager.app_data_dir", return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-backlog-")

    def _manager(self, max_pending):
        """起動済みのマネージャと、発行された通知の記録を返す。

        GUI のイベント処理は流さない（止まっている GUI を再現する）ので、
        発行そのものを数えるために DirectConnection で受ける
        """
        from PyQt6.QtCore import Qt
        from core.sftp_server import SFTPServerManager

        m = SFTPServerManager()
        self.addCleanup(m.stop)
        m.STOP_TIMEOUT_SECONDS = 1.0
        m.max_pending_notices = max_pending
        log = {"connected": [], "disconnected": [], "activity": []}
        m.client_connected.connect(log["connected"].append,
                                   Qt.ConnectionType.DirectConnection)
        m.client_disconnected.connect(log["disconnected"].append,
                                      Qt.ConnectionType.DirectConnection)
        m.client_activity.connect(
            lambda ip, msg: log["activity"].append(msg),
            Qt.ConnectionType.DirectConnection)
        port = free_port()
        self.assertTrue(m.start(port=port, root_dir=self.root,
                                username=USER, password=PASSWORD))
        deadline = time.time() + 5
        while not m.is_running and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(m.is_running, "サーバーが起動しない")
        return m, port, log

    def _knock(self, m, port, times):
        """認証を通さない接続→即切断を繰り返す（1 本ずつ終わらせる）"""
        for _ in range(times):
            c = socket.socket()
            c.settimeout(3)
            try:
                c.connect(("127.0.0.1", port))
            finally:
                c.close()
            deadline = time.time() + 5
            while time.time() < deadline:
                with m._client_lock:
                    busy = len(m._client_sockets)
                if not busy:
                    break
                time.sleep(0.01)

    @staticmethod
    def _settled(log, quiet=0.3, timeout=5.0):
        """通知が出そろうまで待つ（増えなくなってから quiet 秒）。

        _knock は「マネージャが掴んでいるソケットが無くなったか」で待つが、
        通知を出すのはその後片付けと同じスレッドの別の場所なので、戻った
        時点ではまだ 1 件出ていないことがある（実測: 5 回に 1 回ほど
        connected が 4 件のまま）。数を確かめる前にここで落ち着かせる。
        """
        deadline = time.time() + timeout
        last = None
        stable_since = time.time()
        while time.time() < deadline:
            now = (len(log["connected"]), len(log["disconnected"]))
            if now != last:
                last, stable_since = now, time.time()
            elif time.time() - stable_since >= quiet:
                return
            time.sleep(0.02)

    # --- 本題 ------------------------------------------------------------

    def test_unauthenticated_knocking_cannot_pile_up_notices(self):
        m, port, log = self._manager(max_pending=6)
        self._knock(m, port, 40)
        self._settled(log)

        issued = len(log["connected"]) + len(log["disconnected"])
        # 上限 + 「届けた接続の切断は必ず届ける」ぶんで頭打ちになる
        self.assertLessEqual(issued, 2 * m.max_pending_notices,
                             "配送待ちの上限が効いていない（発行 %d 件）" % issued)
        self.assertLess(issued, 80,
                        "接続 40 回ぶんの通知がそのまま積み上がっている（%d 件）" % issued)
        self.assertGreater(issued, 0, "通知が 1 件も出ていない")

    def test_a_delivered_connect_always_gets_its_disconnect(self):
        """パネルの「接続クライアント: N」が戻らなくならないこと"""
        m, port, log = self._manager(max_pending=6)
        self._knock(m, port, 40)
        self._settled(log)

        self.assertEqual(len(log["connected"]), len(log["disconnected"]),
                         "接続と切断の数が合わない: %d / %d"
                         % (len(log["connected"]), len(log["disconnected"])))

    def test_the_omitted_count_is_reported_once_the_gui_catches_up(self):
        m, port, log = self._manager(max_pending=6)
        self._knock(m, port, 40)

        # GUI が追いついた時点で 1 行だけ出る
        deadline = time.time() + 5
        while time.time() < deadline and not log["activity"]:
            self.app.processEvents()
            time.sleep(0.02)
        omitted = [msg for msg in log["activity"] if "省略しました" in msg]
        self.assertEqual(len(omitted), 1,
                         "省略件数の知らせが 1 行で出ていない: %s" % log["activity"])

    def test_notices_flow_normally_under_the_default_limit(self):
        """対照: 上限に余裕があればこれまでどおり全部届く"""
        m, port, log = self._manager(max_pending=1000)
        self._knock(m, port, 5)
        self._settled(log)

        self.assertEqual(len(log["connected"]), 5)
        self.assertEqual(len(log["disconnected"]), 5)
        self.app.processEvents()
        self.assertEqual([msg for msg in log["activity"] if "省略しました" in msg], [])


if __name__ == "__main__":
    unittest.main()
