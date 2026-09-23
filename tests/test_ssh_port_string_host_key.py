"""SSH のポートが文字列でも、保存済みのホスト鍵で相手を確かめることを検証する。

何が起きていたか（基準 470c538 で実測）。config.json の機器の "port" は
読み込みで型をそろえておらず、MainWindow._connect_ssh から SSHConnection を
経て paramiko の SSHClient.connect へそのまま渡っていた。paramiko 4.0.0 は
known_hosts を引く名前を `port == 22`（整数との比較）で決めるので、手編集の
config.json などで "port": "22" と書かれていると、接続先は同じ 22 番なのに
引く名前が "127.0.0.1" ではなく "[127.0.0.1]:22" になる。その名前の鍵は
無いので TOFU が黙って受け入れ、保存時の食い違い検査も同じ別名で引くため
素通りし、そのまま認証へ進んでいた。

    known_hosts: 127.0.0.1 <鍵 A>、127.0.0.1:22 の相手は鍵 B
    port=22   → 「ホストキーが変更されています(中間者攻撃の可能性)」で中止、
                相手に届いた認証は無し
    port="22" → 相手へパスワード認証が届く（('password', 'admin', 's3cret')）、
                known_hosts に "[127.0.0.1]:22 <鍵 B>" が増える

事前の点検（_refuse_or_warn_broken_lines が使う known_hosts_server_name）は
int() してから名前を作るので、同じ接続の中で点検と paramiko が別の名前を
見ていた。

どう直したか。接続の境界（SSHConnection.connect）でポートを整数へそろえ、
その整数を self.port に戻してから known_hosts の点検と paramiko の両方へ
渡す。整数にできない値・1〜65535 の外の値は、機器へ繋ぐ前に日本語の
エラーで止める（黙って 22 にしない）。SSH の接続はすべて SSHConnection を
通り（config.json の手編集・機器の編集ダイアログ・再接続）、SFTP は同じ
SSHClient に相乗りするので、ここ 1 か所で全部の入口に効く。
"""
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402


class _RecordingServer(paramiko.ServerInterface):
    """届いた認証を記録し、必ず拒む。"""

    def __init__(self, log):
        self.log = log

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        self.log.append((username, password))
        return paramiko.AUTH_FAILED


class _LocalSSHServer:
    """localhost のエフェメラルポートで待ち受ける paramiko サーバ。"""

    def __init__(self, key):
        self.key = key
        self.auth_log = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.transports = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                accepted, _ = self.sock.accept()
            except OSError:
                return
            transport = paramiko.Transport(accepted)
            transport.add_server_key(self.key)
            self.transports.append(transport)
            try:
                transport.start_server(server=_RecordingServer(self.auth_log))
            except Exception:
                pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for transport in self.transports:
            transport.close()


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _temp_dir(self, prefix):
        path = Path(tempfile.mkdtemp(prefix=prefix))
        self.addCleanup(shutil.rmtree, str(path), True)
        return path


class StringPortKeepsTheStoredHostKeyTest(_QtTestCase):
    """保存済みの鍵 A、相手は鍵 B。port の型だけを変えて繋ぐ。

    22 番で待ち受けるとほかのものとぶつかるので、paramiko の接続先の
    解決だけを差し替えて、このサーバのポートへ向ける。ポート番号の値と
    型は、そのまま paramiko へ渡る（known_hosts を引く名前はそこで決まる）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.stored_key = paramiko.ECDSAKey.generate()
        cls.presented_key = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.dir = self._temp_dir("netbelt-strport-")
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.known_hosts = self.dir / "known_hosts"
        self.known_hosts.write_text(
            "127.0.0.1 %s %s\n" % (self.stored_key.get_name(),
                                   self.stored_key.get_base64()),
            encoding="utf-8")
        self.original = self.known_hosts.read_text(encoding="utf-8")
        self.server = _LocalSSHServer(self.presented_key)
        self.addCleanup(self.server.close)
        self.ports_seen = []
        real_port = self.server.port

        def to_local_server(client, hostname, port):
            self.ports_seen.append(port)
            return [(socket.AF_INET, ("127.0.0.1", real_port))]

        patcher = mock.patch.object(paramiko.SSHClient,
                                    "_families_and_addresses", to_local_server)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _connect(self, port):
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("127.0.0.1", port, "admin", password="s3cret")
        self.addCleanup(conn.deleteLater)
        errors = []
        conn.error_occurred.connect(errors.append)
        returned = conn.connect()
        conn.dispose()
        return returned, errors

    def test_integer_port_22_is_refused(self):
        """整数の 22 は、鍵の食い違いで中止すること（対照）。"""
        returned, errors = self._connect(22)

        self.assertFalse(returned)
        self.assertEqual(self.server.auth_log, [],
                         "鍵が違う相手へ認証が届いている")
        self.assertTrue(any("ホストキーが変更されています" in e for e in errors),
                        "鍵の食い違いとして中止していない: %r" % (errors,))

    def test_string_port_22_does_not_send_the_password(self):
        """文字列の "22" でも、鍵が違う相手へパスワードを送らないこと。"""
        returned, errors = self._connect("22")

        self.assertFalse(returned)
        self.assertEqual(self.server.auth_log, [],
                         "保存済みの鍵と違う相手へ認証が届いている")
        self.assertTrue(any("ホストキーが変更されています" in e for e in errors),
                        "鍵の食い違いとして中止していない: %r" % (errors,))

    def test_string_port_22_does_not_register_a_second_name(self):
        """文字列の "22" で、別名（[host]:22）の鍵を増やさないこと。"""
        self._connect("22")

        self.assertEqual(self.known_hosts.read_text(encoding="utf-8"),
                         self.original,
                         "known_hosts に別名の鍵が書き足された")

    def test_paramiko_receives_an_integer_port(self):
        """paramiko へは整数の 22 が渡ること。"""
        self._connect(" 22 ")

        self.assertEqual(self.ports_seen, [22])
        self.assertIs(type(self.ports_seen[0]), int)


class _StopAtConnect(Exception):
    """paramiko の connect まで来たことを知らせて止めるための例外"""


class PortNormalizedAtTheBoundaryTest(_QtTestCase):
    """SSHClient.connect を差し替え、渡る値と止まり方だけを見る（通信しない）。"""

    def setUp(self):
        self.dir = self._temp_dir("netbelt-portnorm-")
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

        def fake_connect(client, **kwargs):
            self.calls.append(kwargs)
            raise _StopAtConnect()

        patcher = mock.patch.object(paramiko.SSHClient, "connect", fake_connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _connect(self, port):
        from core.ssh_connection import SSHConnection
        conn = SSHConnection("192.0.2.10", port, "admin", password="pw")
        self.addCleanup(conn.deleteLater)
        errors = []
        conn.error_occurred.connect(errors.append)
        returned = conn.connect()
        return conn, returned, errors

    def test_the_known_hosts_name_and_paramiko_use_the_same_integer(self):
        """点検用の名前と paramiko が、同じ整数のポートを使うこと。"""
        from core.ssh_connection import known_hosts_server_name
        for value, expected in (("22", 22), (" 2222", 2222), (2222, 2222),
                                (22, 22), (830.0, 830)):
            with self.subTest(port=value):
                self.calls.clear()
                conn, _returned, _errors = self._connect(value)

                self.assertEqual(len(self.calls), 1, "paramiko まで届いていない")
                passed = self.calls[0]["port"]
                self.assertIs(type(passed), int, "整数になっていない: %r" % (passed,))
                self.assertEqual(passed, expected)
                self.assertEqual(conn.port, passed,
                                 "点検が使うポートと paramiko のポートが違う")
                paramiko_name = ("192.0.2.10" if passed == 22
                                 else "[192.0.2.10]:%d" % passed)
                self.assertEqual(known_hosts_server_name(conn.host, conn.port),
                                 paramiko_name)

    def test_an_unusable_port_stops_before_connecting(self):
        """整数にできない・範囲外のポートでは、繋がずに日本語で止めること。"""
        for value in ("abc", "", "22abc", "0", "65536", 0, 65536, -1,
                      None, True, 22.5, [22]):
            with self.subTest(port=value):
                self.calls.clear()
                _conn, returned, errors = self._connect(value)

                self.assertFalse(returned)
                self.assertEqual(self.calls, [],
                                 "使えないポートで paramiko の接続まで進んだ")
                self.assertEqual(len(errors), 1, "エラーが出ていない: %r" % (errors,))
                self.assertIn("ポート番号", errors[0])
                self.assertIn("1〜65535", errors[0])


class ConfigStringPortThroughMainWindowTest(_QtTestCase):
    """config.json の手編集（"port": "22"）から接続・再接続まで通す。"""

    def setUp(self):
        self.dir = self._temp_dir("netbelt-portcfg-")
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        self.reached = threading.Semaphore(0)

        def fake_connect(client, **kwargs):
            self.calls.append(kwargs)
            self.reached.release()
            raise _StopAtConnect()

        patcher = mock.patch.object(paramiko.SSHClient, "connect", fake_connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _window(self, port):
        import json
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        config_path = self.dir / "config.json"
        config_path.write_text(json.dumps({
            "groups": [{"name": "Default", "devices": [{
                "name": "sw1", "host": "192.0.2.10", "port": port,
                "protocol": "ssh", "username": "admin", "password": "pw"}]}],
            "global_macros": [],
        }), encoding="utf-8")
        with mock.patch("ui.main_window.ConfigManager") as fake, \
                mock.patch.object(MainWindow, "_check_for_updates_on_startup"):
            fake.return_value = ConfigManager(config_path=str(config_path))
            window = MainWindow()
        self.addCleanup(window.close)
        return window

    def _device(self, window):
        return window.config_manager.get_groups()[0]["devices"][0]

    def _wait_for_connect(self):
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if self.reached.acquire(timeout=0.05):
                return True
            self.app.processEvents()
        return False

    def test_connect_and_reconnect_pass_an_integer_port(self):
        """接続と再接続のどちらでも、paramiko へ整数の 22 が渡ること。"""
        window = self._window("22")
        window._on_connect_requested(self._device(window))
        self.assertTrue(self._wait_for_connect(), "接続が始まらない")
        self._pump()

        window._reconnect_device("sw1")
        self.assertTrue(self._wait_for_connect(), "再接続が始まらない")
        self._pump()

        self.assertEqual([c["port"] for c in self.calls], [22, 22])
        self.assertTrue(all(type(c["port"]) is int for c in self.calls),
                        "整数になっていない: %r" % ([c["port"] for c in self.calls],))

    def test_an_unusable_port_is_reported_in_the_tab(self):
        """使えないポートは、繋がずにタブへ理由を出すこと。"""
        window = self._window("ssh")
        shown = []
        with mock.patch.object(window.terminal_widget, "show_notice",
                               side_effect=lambda name, text: shown.append(text)):
            window._on_connect_requested(self._device(window))
            self._pump(1.0)

        self.assertEqual(self.calls, [], "使えないポートで接続を始めた")
        self.assertTrue(any("ポート番号" in text for text in shown),
                        "理由がタブに出ていない: %r" % (shown,))


if __name__ == "__main__":
    unittest.main()
