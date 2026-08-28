"""内蔵サーバがファイアウォールを勝手に触らないことを検証する。

TFTP と FTP は「自動設定しない（3CDaemon 方式）」を明示的に選んでいる。
起動しただけで管理者昇格(UAC)を要求しないためで、受信許可は Windows の
初回プロンプトか既存ルールに委ね、通らない環境のために手動ボタン
（fix_firewall）を残してある。

Syslog・SFTP サーバ・SNMP Trap 受信だけがこの方針から取り残されていた。
待受のたびに ensure_inbound_allow を無条件に呼ぶため、

  - 「受信開始」を押しただけで UAC が出る
  - 作られるルールはポート番号入りの名前で恒久登録され、停止処理にも
    どこにも削除が無いので、ポートを変えて使うたび残骸が増える
  - 同じ操作でも TFTP/FTP ではルールが作られず、挙動が一貫しない

待ち受ける5つ（FTP / TFTP / Syslog / SFTP / SNMP Trap）で揃える。
"""
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FirewallPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        patcher = mock.patch("core.firewall.ensure_inbound_allow",
                             return_value=(True, "test stub"))
        self.allow = patcher.start()
        self.addCleanup(patcher.stop)
        program = mock.patch("core.firewall.ensure_self_program_allow",
                             return_value=(True, "test stub"))
        self.allow_program = program.start()
        self.addCleanup(program.stop)

    # --- 起動しただけでは触らないこと ---

    def test_starting_syslog_does_not_touch_the_firewall(self):
        from core.syslog_receiver import SyslogReceiver
        receiver = SyslogReceiver()
        self.addCleanup(receiver.stop)

        self.assertTrue(receiver.start_protocol("UDP", 0))

        self.allow.assert_not_called()
        self.allow_program.assert_not_called()

    def test_starting_the_sftp_server_does_not_touch_the_firewall(self):
        from core.sftp_server import SFTPServerManager
        server = SFTPServerManager()
        self.addCleanup(server.stop)
        root = tempfile.mkdtemp(prefix="netbelt-fw-sftp-")

        self.assertTrue(server.start(port=free_port(), root_dir=root,
                                     username="netbelt", password="pw"))

        self.allow.assert_not_called()
        self.allow_program.assert_not_called()

    def test_starting_tftp_still_does_not_touch_the_firewall(self):
        """既に方針どおりのものは、そのままであること。"""
        from core.tftp_server import TFTPServerManager
        server = TFTPServerManager()
        self.addCleanup(server.stop)
        root = tempfile.mkdtemp(prefix="netbelt-fw-tftp-")

        self.assertTrue(server.start(port=0, root_dir=root))

        self.allow.assert_not_called()
        self.allow_program.assert_not_called()

    def test_starting_the_trap_receiver_does_not_touch_the_firewall(self):
        """Trap 受信も待ち受ける側なので、同じ扱いにすること。"""
        from core.snmp_manager import SNMPManager
        manager = SNMPManager()
        self.addCleanup(manager.stop_trap_receiver)

        manager.start_trap_receiver(port=free_udp_port(), communities=["public"])

        self.allow.assert_not_called()
        self.allow_program.assert_not_called()

    # --- 手動で直す口があること ---

    def test_syslog_can_fix_the_firewall_by_hand(self):
        """通らない環境のために、押したときだけ許可を足せること。"""
        from core.syslog_receiver import SyslogReceiver
        receiver = SyslogReceiver()
        self.addCleanup(receiver.stop)
        receiver.start_protocol("UDP", 0)
        self.allow.reset_mock()

        receiver.fix_firewall()

        self.assertTrue(self.allow.called,
                        "手動でも受信許可を足せない")
        self.assertTrue(self.allow_program.called,
                        "自exe の許可を足していない")

    def test_the_sftp_server_can_fix_the_firewall_by_hand(self):
        from core.sftp_server import SFTPServerManager
        server = SFTPServerManager()
        self.addCleanup(server.stop)
        root = tempfile.mkdtemp(prefix="netbelt-fw-sftp2-")
        port = free_port()
        server.start(port=port, root_dir=root, username="netbelt", password="pw")
        self.allow.reset_mock()

        server.fix_firewall(port=port)

        self.assertTrue(self.allow.called, "手動でも受信許可を足せない")
        self.assertTrue(self.allow_program.called, "自exe の許可を足していない")

    def test_the_trap_receiver_can_fix_the_firewall_by_hand(self):
        from core.snmp_manager import SNMPManager
        manager = SNMPManager()
        self.addCleanup(manager.stop_trap_receiver)
        port = free_udp_port()
        manager.start_trap_receiver(port=port, communities=["public"])
        self.allow.reset_mock()

        manager.fix_firewall(port=port)

        self.assertTrue(self.allow.called, "手動でも受信許可を足せない")
        self.assertTrue(self.allow_program.called, "自exe の許可を足していない")

    # --- 4つのパネルで揃っていること ---

    def test_every_server_panel_offers_the_manual_fix(self):
        """どのサーバでも同じ手順で直せること。

        揃っていないと、「TFTP では押すボタンがあるのに Syslog には無い」
        という覚え方をしなければならなくなる。
        """
        from ui.main_window import MainWindow
        window = MainWindow()
        # 開いたままにするとサーバや QThread が残り、終了時の後片付けで
        # 解放済みオブジェクトへ触れてプロセスごと落ちる（conftest の注意書き）
        self.addCleanup(window.close)

        missing = [name for name in ("ftp_server_panel", "tftp_server_panel",
                                     "syslog_panel", "sftp_server_panel",
                                     "snmp_panel")
                   if not hasattr(getattr(window, name), "fw_allow_btn")]
        self.assertEqual(missing, [],
                         "手動でファイアウォールを直すボタンが無いパネル: %s"
                         % missing)


if __name__ == "__main__":
    unittest.main()
