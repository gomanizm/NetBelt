"""SFTP で断った書き込みの通知にも、配送待ちの上限が効くことを検証する。

何が起きていたか（実測、基準 87514a3。127.0.0.1 のみ）: 同じ保存先への 2 本目の
書き込みの open や SETSTAT size を断ったときの通知（パネルのログの「他の転送が
書き込み中のため断りました」）は、SFTPServerHandler の notify から
client_activity.emit() を直接呼んでいて、接続・切断の通知が使っている
max_pending_notices（配送待ちの件数の上限）を通っていなかった。GUI の
イベント処理を止めたまま、認証済みの A が保存先を 'w' で開いた状態で、B が
同じ名前への open 'w' と truncate を交互に送り続けると、上限 6 に対して
5 秒で 10642 件の通知が発行された（数えられた配送待ちは接続の 2 件だけ）。
パネルの行数上限が効くのは配送の後なので、GUI が塞がっている間は Qt の
配送キューへ際限なく積み上がる。

どう直したか: FTP の _emit_activity と同じく、client_activity は取得
（_take_notice）を通してから出し、配送されたら _on_notice_delivered で数を
戻す（client_activity も自分で受ける）。上限を超えた分は出さずに省略件数へ
足し、GUI が追いついた時点で「表示が追いつかず N 件の通知を省略しました」を
1 行だけ出す（接続・切断の通知と同じ扱い）。その 1 行も同じ数え方で出す。
"""
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"
REFUSED = "書き込み中のため断りました"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SftpRefusalNoticeBacklogTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = mock.patch("core.config_manager.app_data_dir", return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-refusal-")

    def _manager(self, max_pending):
        """起動済みのマネージャ、ポート、発行された通知の記録を返す。

        GUI のイベント処理は流さない（止まっている GUI を再現する）ので、
        発行そのものを数えるために DirectConnection で受ける
        """
        import paramiko
        from PyQt6.QtCore import Qt
        from core.sftp_server import SFTPServerManager

        m = SFTPServerManager()
        self.addCleanup(m.stop)
        m.STOP_TIMEOUT_SECONDS = 1.0
        m.host_key = paramiko.RSAKey.generate(2048)
        m.max_pending_notices = max_pending
        log = {"activity": []}
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

    def _sftp(self, port):
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect("127.0.0.1", port=port, username=USER,
                       password=PASSWORD, look_for_keys=False,
                       allow_agent=False, timeout=10)
        self.addCleanup(client.close)
        return client.open_sftp()

    def _hold(self, port, name):
        """A が name を 'w' で開いて書いた状態にする（テストの終わりまで閉じない）"""
        writer = self._sftp(port).open(name, "w", bufsize=0)
        writer.write(b"AAAA")
        self.addCleanup(writer.close)

    def _refuse(self, port, name, times):
        """B が name への書き込みの open と truncate を交互に送り、全部断られること"""
        other = self._sftp(port)
        for i in range(times):
            with self.assertRaises(IOError, msg="書き込み中の保存先への要求が通った"):
                if i % 2 == 0:
                    other.open(name, "w").close()
                else:
                    other.truncate(name, 0)

    @staticmethod
    def _refusals(log):
        return [msg for msg in log["activity"] if REFUSED in msg]

    # --- 本題 ------------------------------------------------------------

    def test_refused_requests_cannot_pile_up_notices(self):
        m, port, log = self._manager(max_pending=6)
        self._hold(port, "held.cfg")
        self._refuse(port, "held.cfg", 40)

        issued = len(self._refusals(log))
        self.assertLessEqual(
            issued, m.max_pending_notices,
            "断った通知が配送待ちの上限を通らずに積み上がっている（発行 %d 件）"
            % issued)
        self.assertGreater(issued, 0, "断った通知が 1 件も出ていない")

    def test_the_omitted_refusals_are_reported_once_the_gui_catches_up(self):
        m, port, log = self._manager(max_pending=6)
        self._hold(port, "held.cfg")
        self._refuse(port, "held.cfg", 40)

        # GUI が追いついた時点で 1 行だけ出る
        deadline = time.time() + 5
        while time.time() < deadline and \
                not any("省略しました" in msg for msg in log["activity"]):
            self.app.processEvents()
            time.sleep(0.02)
        omitted = [msg for msg in log["activity"] if "省略しました" in msg]
        self.assertEqual(len(omitted), 1,
                         "省略件数の知らせが 1 行で出ていない: %s" % log["activity"])
        # 追いついたあとは、断った通知がまた届く
        before = len(self._refusals(log))
        self._refuse(port, "held.cfg", 1)
        self.assertEqual(len(self._refusals(log)), before + 1,
                         "追いついたあとの断った通知が出ていない")

    def test_refusals_flow_normally_under_the_default_limit(self):
        """対照: 上限に余裕があればこれまでどおり全部届く"""
        m, port, log = self._manager(max_pending=1000)
        self._hold(port, "held.cfg")
        self._refuse(port, "held.cfg", 5)

        self.assertEqual(len(self._refusals(log)), 5)
        self.app.processEvents()
        self.assertEqual([msg for msg in log["activity"] if "省略しました" in msg], [])


if __name__ == "__main__":
    unittest.main()
