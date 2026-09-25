"""接続直後のホーム取得が切断で終わったとき、接続成功を返さないことを検証する。

実測（基準 aa38a2b、src/core/sftp_manager.py:190-206）:
  connect() の normalize('.') は期限切れ（TimeoutError）だけを畳む枝へ回し、
  それ以外は `except Exception: self.current_path = "/"` で握りつぶしていた。
  そのため normalize が paramiko.SSHException('Server connection dropped: ')
  や EOFError で終わっても、
    * is_connected=True のまま connected を発火し、connect() は True を返す
    * 呼び出し側（main_window._on_sftp_session_ready）はこれを成功として
      sftp_managers へ登録し、状態バーに「…に接続しました（SFTP有効）」を出す
    * パネルは死んだチャンネルへ一覧を頼み、そこで初めて失敗する
  利用者の決定 2026-09-20 で、切断・壊れた応答は期限切れと同じく接続を畳む
  ことになっており（_fail と _DROPPED_CONNECTION_ERRORS）、全操作がそれに
  従っているのに connect() だけが外れたままだった。

直し方:
  normalize の except を期限切れと同じ枝へまとめる。文面は期限切れなら
  「機器が…秒応答しません」、それ以外は _fail と同じ整形
  （str(e) or 型名 → strip().rstrip(':').rstrip()）にする。畳み方
  （close / sftp_client=None / ssh_client=None / error_occurred / return False）
  は期限切れの枝をそのまま流用する。

畳む対象を広げすぎない（保守的な側）:
  paramiko.SFTPError まで含めると、REALPATH の応答が 1 件でない機器で
  SFTP 接続ごと断ることになる。ここは既存の挙動（'/' から始める）を変えず、
  (TimeoutError, EOFError, SSHException) に留める。SFTPError まで畳むかは
  利用者の判断が要るため、この修正の範囲に入れない。
"""
import os
import sys
import unittest
from unittest import mock

import paramiko

sys.path.insert(0, "src")


class SftpConnectDroppedHomeLookupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager_with(self, failure):
        """normalize が failure で終わるマネージャと、その sftp クライアント。"""
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        self.errors = []
        self.connected = []
        m.error_occurred.connect(self.errors.append)
        m.connected.connect(lambda: self.connected.append(True))
        sftp = mock.Mock()
        sftp.normalize.side_effect = failure
        ssh = mock.Mock()
        ssh.open_sftp.return_value = sftp
        return m, sftp, ssh

    def _assert_folded(self, m, sftp, expected):
        self.assertFalse(m.is_connected, "切れたチャンネルを接続中のまま残した")
        self.assertIsNone(m.sftp_client, "使えないチャンネルを掴んだまま")
        self.assertIsNone(m.ssh_client, "使えない接続を掴んだまま")
        sftp.close.assert_called_once()
        self.assertEqual(self.connected, [], "失敗したのに接続成功を知らせた")
        self.assertEqual(self.errors, [expected])

    def test_connect_fails_when_the_home_lookup_finds_the_connection_dropped(self):
        """切断で終わったら、接続成功を返さず畳むこと。"""
        m, sftp, ssh = self._manager_with(
            paramiko.SSHException("Server connection dropped: "))

        self.assertFalse(m.connect(ssh), "切れたチャンネルで接続成功を返した")
        self._assert_folded(m, sftp, "SFTP接続エラー: Server connection dropped")

    def test_connect_fails_when_the_home_lookup_hits_eof(self):
        """理由を持たない EOFError でも、型名を添えて畳むこと。"""
        m, sftp, ssh = self._manager_with(EOFError())

        self.assertFalse(m.connect(ssh), "切れたチャンネルで接続成功を返した")
        self._assert_folded(m, sftp, "SFTP接続エラー: EOFError")

    def test_a_timeout_is_still_reported_as_a_timeout(self):
        """対照: 期限切れの文面は変えないこと。"""
        m, sftp, ssh = self._manager_with(TimeoutError())

        self.assertFalse(m.connect(ssh))
        self.assertEqual(self.errors,
                         ["SFTP接続エラー: 機器が30秒応答しません"])

    def test_an_sftp_error_still_falls_back_to_root(self):
        """対照: SFTPError は畳まない（保守的な側。冒頭の docstring を参照）。

        ホームを 1 件で返さない機器で SFTP 接続ごと断ることになるため、
        広げるかどうかは利用者の判断に委ねる。
        """
        m, sftp, ssh = self._manager_with(
            paramiko.SFTPError("Expected handle"))

        self.assertTrue(m.connect(ssh), "SFTPError で接続ごと断った")
        self.assertTrue(m.is_connected)
        self.assertEqual(m.get_current_path(), "/")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
