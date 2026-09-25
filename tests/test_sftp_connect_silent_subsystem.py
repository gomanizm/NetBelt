"""SFTP の開始に答えない機器へも、SFTPManager.connect が期限で戻ることを検証する。

実測（基準 441ea02）:
  相手は localhost の paramiko サーバで、sftp の subsystem を受け付けるが
  VERSION を返さない。CHANNEL_TIMEOUT_SECONDS=1 で SFTPManager.connect を
  別スレッドで呼び、8 秒待っても接続スレッドは生きたまま、結果=[]、
  errors=[]（SSH のトランスポートも生きている）。ssh.close() のあとで
  ようやく『SFTP接続エラー: EOF during negotiation』で戻った（計 8.4 秒）。
  原因は src/core/sftp_manager.py:227 の ssh_client.open_sftp()。期限
  （231 の settimeout）は open_sftp() が戻ってから掛かる。paramiko の
  open_sftp は subsystem 要求の返事（Channel._wait_for_event の期限の無い
  event.wait()）と VERSION（期限 None のチャンネルの読み取り）を待つので、
  SFTP だけ黙る機器では MainWindow の sftp_connect_thread が戻らず、
  SFTP 非対応の機器と見分けがつかない（知らせも出ない）。

利用者の決定（2026-09 の 1.3.2 の仕分け、案 W）:
  open_sftp() を内側のスレッドで呼び、期限で待つのをやめる（既存のテストは
  変えない）。

直し方:
  connect は open_sftp() を内側のスレッドで呼び、CHANNEL_TIMEOUT_SECONDS
  だけ待つ。期限を過ぎたら『SFTP接続エラー: 機器がN秒応答しません』を出して
  False を返す。端末の SSH は閉じない。期限のあとで内側のスレッドが開けた
  クライアントは、使われないのでそのスレッドが閉じる。期限切れのあとも
  内側のスレッドと機器側の半開きのチャンネルは、応答が来るか SSH が切れる
  まで残る（今と同じ）。open_sftp の失敗はこれまでどおり
  『SFTP接続エラー: <理由>』で返す。
"""
import socket
import sys
import threading
import time
import unittest
from unittest import mock

import paramiko

import test_sftp_upload_tmp_mode as base   # _MemoryFS / _SftpInterface / _Auth

sys.path.insert(0, "src")

TIMEOUT = 1.0


class _Server:
    """localhost の SSH サーバ。kind で SFTP の始まり方を変える

    normal:  ふつうに SFTP を出す
    silent:  subsystem 要求は受けるが、VERSION を返さない
    noreply: subsystem 要求に答えない
    """

    def __init__(self, kind):
        self.kind = kind
        self.release = threading.Event()
        self.fs = base._MemoryFS()
        self.key = paramiko.ECDSAKey.generate()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.transports = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        release = self.release

        class _Silent(paramiko.SubsystemHandler):
            def start_subsystem(self, name, transport, channel):
                release.wait(30)

        class _NoReplyAuth(base._Auth):
            def check_channel_subsystem_request(self, channel, name):
                release.wait(30)    # 返事をしない（サーバの転送スレッドごと止まる）
                return False

        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(self.key)
            if self.kind == "normal":
                t.set_subsystem_handler("sftp", paramiko.SFTPServer,
                                        base._SftpInterface, self.fs)
                t.start_server(server=base._Auth())
            elif self.kind == "silent":
                t.set_subsystem_handler("sftp", _Silent)
                t.start_server(server=base._Auth())
            else:
                t.set_subsystem_handler("sftp", _Silent)
                t.start_server(server=_NoReplyAuth())
            self.transports.append(t)

    def close(self):
        self.release.set()
        try:
            self.sock.close()
        except OSError:
            pass
        for t in list(self.transports):
            t.close()


class ConnectSilentSubsystemTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _ssh_to(self, kind):
        server = _Server(kind)
        self.addCleanup(server.close)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect("127.0.0.1", port=server.port, username=base.USER,
                    password=base.PASSWORD, look_for_keys=False,
                    allow_agent=False, timeout=10)
        # 後始末は SSH を先に閉じる（止まったままのスレッドを起こす）
        self.addCleanup(ssh.close)
        return ssh

    def _connect_in_thread(self, ssh, wait=5.0):
        """MainWindow の sftp_connect_thread と同じく別スレッドで connect を呼ぶ"""
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        type(self)._keep.append(m)
        m.CHANNEL_TIMEOUT_SECONDS = TIMEOUT
        errors, result = [], []
        m.error_occurred.connect(errors.append)
        started = time.time()
        worker = threading.Thread(target=lambda: result.append(m.connect(ssh)),
                                  daemon=True)
        worker.start()
        worker.join(wait)
        elapsed = time.time() - started
        # 別スレッドから出た通知はキュー配送なので、イベントを回して受け取る
        deadline = time.time() + 1.0
        while time.time() < deadline:
            self.app.processEvents()
            if errors or result == [True]:
                break
            time.sleep(0.01)
        return m, worker, result, errors, elapsed

    def _assert_gives_up(self, kind):
        ssh = self._ssh_to(kind)
        m, worker, result, errors, elapsed = self._connect_in_thread(ssh)
        self.assertFalse(worker.is_alive(),
                         "connect が戻らない（%.1f 秒待った）" % elapsed)
        self.assertEqual(result, [False])
        self.assertEqual(errors, ["SFTP接続エラー: 機器が1秒応答しません"])
        self.assertLess(elapsed, TIMEOUT + 2.0)
        self.assertFalse(m.is_connected)
        self.assertIsNone(m.sftp_client, "開けていないクライアントを掴んでいる")
        self.assertTrue(ssh.get_transport().is_active(), "端末の SSH まで切った")

    def test_a_device_that_never_sends_version_times_out(self):
        """subsystem は受けても VERSION を返さない機器で、期限で失敗を返すこと。"""
        self._assert_gives_up("silent")

    def test_a_device_that_never_answers_the_subsystem_request_times_out(self):
        """subsystem 要求に答えない機器でも、期限で失敗を返すこと。"""
        self._assert_gives_up("noreply")

    def test_a_normal_device_still_connects(self):
        """対照: ふつうの機器にはこれまでどおり繋がり、ホームから始まること。"""
        ssh = self._ssh_to("normal")
        m, worker, result, errors, _ = self._connect_in_thread(ssh)
        self.assertFalse(worker.is_alive(), "connect が戻らない")
        self.assertEqual(result, [True], errors)
        self.assertEqual(m.current_path, base.HOME)
        self.assertTrue(m.is_connected)
        m.disconnect()

    def test_a_client_opened_after_the_deadline_is_closed(self):
        """期限のあとで開けたクライアントは、掴んだまま残さず閉じること。"""
        from core.sftp_manager import SFTPManager
        gate = threading.Event()
        opened = mock.Mock()
        ssh = mock.Mock()

        def slow_open_sftp():
            gate.wait(5)
            return opened
        ssh.open_sftp.side_effect = slow_open_sftp
        m = SFTPManager()
        type(self)._keep.append(m)
        m.CHANNEL_TIMEOUT_SECONDS = 0.3
        errors = []
        m.error_occurred.connect(errors.append)
        self.assertFalse(m.connect(ssh))
        self.assertEqual(errors, ["SFTP接続エラー: 機器が0.3秒応答しません"])
        gate.set()
        deadline = time.time() + 3
        while not opened.close.called and time.time() < deadline:
            time.sleep(0.01)
        opened.close.assert_called_once()
        self.assertIsNone(m.sftp_client)
        ssh.close.assert_not_called()

    def test_an_open_failure_is_reported_as_before(self):
        """対照: open_sftp の失敗は、これまでどおり理由つきですぐ返すこと。"""
        from core.sftp_manager import SFTPManager
        ssh = mock.Mock()
        ssh.open_sftp.side_effect = paramiko.SSHException("Channel closed.")
        m = SFTPManager()
        type(self)._keep.append(m)
        errors = []
        m.error_occurred.connect(errors.append)
        started = time.time()
        self.assertFalse(m.connect(ssh))
        self.assertLess(time.time() - started, 2.0)
        self.assertEqual(errors, ["SFTP接続エラー: Channel closed."])


if __name__ == "__main__":
    unittest.main()
