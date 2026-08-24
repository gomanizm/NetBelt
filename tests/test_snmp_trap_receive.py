"""SNMP Trap 受信スレッドが実際に動作し、Trap を受け取れることを確認する。

`_parse_snmp_trap()` を直接呼ぶテストだけでは受信ループ本体を一度も通らない。
実際、バインド処理を切り出した際に `import socket` を一緒に動かしてしまい、
ループ内の `except socket.timeout` が NameError で落ちて受信スレッドが
起動直後に死ぬ状態になっていた（パネルは「受信中」と表示したまま）。
"""
import socket
import sys
import time
import unittest
import unittest.mock

sys.path.insert(0, "src")


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def trap_bytes(community="public"):
    from pyasn1.codec.ber import encoder
    from pysnmp.proto import api

    pMod = api.protoModules[api.protoVersion2c]
    pdu = pMod.TrapPDU()
    pMod.apiTrapPDU.setDefaults(pdu)
    msg = pMod.Message()
    pMod.apiMessage.setDefaults(msg)
    pMod.apiMessage.setCommunity(msg, community)
    pMod.apiMessage.setPDU(msg, pdu)
    return encoder.encode(msg)


class SnmpTrapReceiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                 return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)

    # 作った SNMPManager はクラス終了まで保持する。
    # PyQt では、キューに残ったシグナルの配送先 QObject を Python 側で先に
    # 破棄すると、配送時に解放済みの C++ オブジェクトを触りプロセスごと落ちる
    # （実測でセグメンテーションフォルト）。実アプリでは SNMPManager がパネルと
    # 同じ寿命を持つため起きないが、テストは1メソッドごとに作り捨てるため踏む。
    _managers = []

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtWidgets import QApplication
        for m in cls._managers:
            m.stop_trap_receiver()
        QApplication.processEvents()   # 残ったシグナルを配送しきってから手放す
        cls._managers.clear()

    def _start(self, communities=None):
        from core.snmp_manager import SNMPManager
        m = SNMPManager()
        self._managers.append(m)
        self.addCleanup(m.stop_trap_receiver)
        port = free_udp_port()
        self.assertTrue(m.start_trap_receiver(port, communities or ["public"]))
        # スレッドが待ち受けに入るまで待つ
        deadline = time.time() + 5
        while time.time() < deadline:
            if m.trap_receiver and m.trap_receiver.isRunning():
                break
            time.sleep(0.05)
        return m, port

    def _pump(self, box, timeout=5):
        from PyQt6.QtWidgets import QApplication
        deadline = time.time() + timeout
        while not box and time.time() < deadline:
            QApplication.processEvents()
            time.sleep(0.02)
        return box

    def test_receiver_thread_stays_alive(self):
        """起動直後に例外で死んでいないこと（本件の回帰テスト）。"""
        m, _port = self._start()
        errors = []
        m.error_occurred.connect(errors.append)

        time.sleep(1.2)   # 受信ループのタイムアウトを数回まわす
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()

        self.assertTrue(m.trap_receiver.isRunning(),
                        "受信スレッドが停止している: %s" % errors)
        self.assertEqual(
            [e for e in errors if "not defined" in e or "NameError" in e], [],
            "受信ループが例外で落ちている: %s" % errors)


    def test_stop_right_after_start_does_not_hang(self):
        """起動直後に止めてもスレッドが残らないこと。

        pysnmp の jobFinished を jobStarted より先に呼ぶと内部で KeyError に
        なり、握り潰すとジョブカウンタが合わなくなって runDispatcher() が
        永久に戻らない。生ソケット実装には無かった、エンジン化で入った危険。

        SNMPManager.stop_trap_receiver() はタイムアウト時に参照を捨てるので、
        そちら経由では気づけない。レシーバを直接止めて確かめる。
        """
        m, _port = self._start()
        receiver = m.trap_receiver
        receiver.stop()
        self.assertTrue(receiver.wait(10000),
                        "停止要求から10秒経っても受信スレッドが終わらない")
        self.assertFalse(receiver.isRunning())
    def test_trap_is_received_and_parsed(self):
        """実際に Trap を送って受け取れること。"""
        m, port = self._start(["public"])
        got = []
        m.trap_received.connect(got.append)

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        s.sendto(trap_bytes("public"), ("127.0.0.1", port))

        self._pump(got)
        self.assertTrue(got, "Trap を受信できなかった")
        self.assertEqual(got[0]["source_ip"], "127.0.0.1")
        self.assertTrue(got[0]["varbinds"], "varbinds が空")

    def test_trap_with_wrong_community_is_dropped(self):
        """許可していないコミュニティの Trap は受信ループでも弾かれること。"""
        m, port = self._start(["allowed-only"])
        got = []
        m.trap_received.connect(got.append)

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        s.sendto(trap_bytes("public"), ("127.0.0.1", port))

        self._pump(got, timeout=1.5)
        self.assertEqual(got, [], "許可していないコミュニティの Trap を受理した")

        # 正しいコミュニティなら通ること（受信ループ自体は生きている）
        s.sendto(trap_bytes("allowed-only"), ("127.0.0.1", port))
        self._pump(got)
        self.assertTrue(got, "許可したコミュニティの Trap も届かない")

    def test_stop_shuts_down_thread(self):
        m, _port = self._start()
        m.stop_trap_receiver()
        deadline = time.time() + 5
        while m.trap_receiver and m.trap_receiver.isRunning() and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(m.trap_receiver and m.trap_receiver.isRunning(),
                         "停止しても受信スレッドが動き続けている")


if __name__ == "__main__":
    unittest.main()
