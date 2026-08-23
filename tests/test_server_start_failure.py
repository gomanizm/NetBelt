"""ポートが使用中のとき、起動が失敗したことを呼び出し側へ正しく返すことを確認する。

バインドはワーカースレッドの中で行われるため、start() がバインド前に True を
返してしまうと、UI は「実行中」の表示に変わったまま実際には何も待ち受けて
いない状態になり、停止も効かない。バインドを start() の中で行い、
結果を戻り値に反映させる。
"""
import os
import socket
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, "src")


def occupied_tcp_port():
    """使用中の TCP ポートを1つ用意して返す（ソケットは呼び出し側で閉じる）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    s.listen(5)
    return s, s.getsockname()[1]


def occupied_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    return s, s.getsockname()[1]


class ServerStartFailureTest(unittest.TestCase):
    # 作ったマネージャはクラス終了まで保持する。PyQt では、キューに残った
    # シグナルの配送先を Python 側で先に解放すると、配送時に解放済みの
    # C++ オブジェクトへ触れてプロセスごと落ちる（実測でセグメンテーションフォルト）。
    # 実アプリではパネルと同じ寿命を持つため起きないが、テストは作り捨てるので踏む。
    _managers = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()   # 残ったシグナルを配送しきってから手放す
        cls._managers.clear()

    def setUp(self):
        fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                 return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)
        home = unittest.mock.patch(
            "core.config_manager.app_data_dir",
            return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)

    def test_sftp_start_returns_false_when_port_busy(self):
        blocker, port = occupied_tcp_port()
        self.addCleanup(blocker.close)

        from core.sftp_server import SFTPServerManager
        m = SFTPServerManager()
        self._managers.append(m)
        self.addCleanup(m.stop)
        errors = []
        m.error_occurred.connect(errors.append)

        root = tempfile.mkdtemp(prefix="netbelt-startfail-")
        result = m.start(port=port, root_dir=root, username="u", password="p")

        self.assertFalse(result, "ポート使用中なのに start() が True を返した")
        self.assertFalse(m.is_running, "起動失敗なのに is_running が True")
        self.assertTrue(errors, "エラーが通知されていない")

    def test_sftp_start_succeeds_on_free_port(self):
        """空きポートでは従来どおり起動できること。"""
        from core.sftp_server import SFTPServerManager
        s, port = occupied_tcp_port()
        s.close()   # 直後に解放して空きポートとして使う

        m = SFTPServerManager()
        self._managers.append(m)
        self.addCleanup(m.stop)
        root = tempfile.mkdtemp(prefix="netbelt-startok-")
        self.assertTrue(m.start(port=port, root_dir=root, username="u", password="p"))
        deadline = time.time() + 15
        while not m.is_running and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(m.is_running)

    def test_snmp_trap_start_returns_false_when_port_busy(self):
        blocker, port = occupied_udp_port()
        self.addCleanup(blocker.close)

        from core.snmp_manager import SNMPManager
        m = SNMPManager()
        self._managers.append(m)
        self.addCleanup(m.stop_trap_receiver)
        errors = []
        m.error_occurred.connect(errors.append)

        result = m.start_trap_receiver(port, ["public"])
        self.assertFalse(result, "ポート使用中なのに start_trap_receiver() が偽を返さない")
        self.assertTrue(errors, "エラーが通知されていない")

    def test_snmp_trap_start_succeeds_on_free_port(self):
        from core.snmp_manager import SNMPManager
        s, port = occupied_udp_port()
        s.close()

        m = SNMPManager()
        self._managers.append(m)
        self.addCleanup(m.stop_trap_receiver)
        self.assertTrue(m.start_trap_receiver(port, ["public"]))


if __name__ == "__main__":
    unittest.main()
