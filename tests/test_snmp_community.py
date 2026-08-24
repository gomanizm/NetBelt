"""SNMP Trap の「許可コミュニティ」が実際にフィルタとして働くことを確認する。

コミュニティ文字列は SNMPv1/v2c における合言葉。UI に「許可コミュニティ」の
入力欄があるのに照合していないと、0.0.0.0:162 に届いた任意の送信元・任意の
コミュニティの Trap がすべて受理され、入力欄が何の意味も持たなくなる。

（v2c のコミュニティは平文で流れるため強固な認証ではないが、誤送信や
別システムの Trap を弾く実用的な効果はある。）

以前は private な _parse_snmp_trap() を直接呼んでいたが、Trap 受信を
pysnmp のエンジンへ載せ替えた際に、実際に UDP で送って受信ループを通す
end-to-end 形式へ移した。照合が pysnmp 側の community 登録に移ったため、
自前の関数を呼ぶだけでは経路を一度も通らなくなったため。
"""
import socket
import sys
import time
import unittest
import unittest.mock

sys.path.insert(0, "src")

from conftest import free_udp_port, trap_bytes, v1_trap_bytes   # noqa: E402


class SnmpCommunityFilterTest(unittest.TestCase):
    """許可コミュニティのフィルタが受信ループで実際に効くこと。"""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作った SNMPManager はクラス終了まで保持する（tests/test_snmp_trap_receive.py
    # と同じ理由。破棄済み QObject へのシグナル配送でプロセスごと落ちるため）。
    _managers = []

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtWidgets import QApplication
        for m in cls._managers:
            m.stop_trap_receiver()
        QApplication.processEvents()
        cls._managers.clear()

    def setUp(self):
        fw = unittest.mock.patch("core.firewall.ensure_inbound_allow",
                                 return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)

    def _start(self, communities):
        from core.snmp_manager import SNMPManager
        m = SNMPManager()
        self._managers.append(m)
        self.addCleanup(m.stop_trap_receiver)
        port = free_udp_port()
        self.assertTrue(m.start_trap_receiver(port, communities))
        deadline = time.time() + 5
        while time.time() < deadline:
            if m.trap_receiver and m.trap_receiver.isRunning():
                break
            time.sleep(0.05)
        got = []
        m.trap_received.connect(got.append)
        return m, port, got

    def _send_and_wait(self, port, community, got, expect_more, timeout=3):
        """Trap を送り、受信件数が増えるか（増えないか）を判定する。"""
        from PyQt6.QtWidgets import QApplication
        before = len(got)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(trap_bytes(community), ("127.0.0.1", port))
        s.close()
        deadline = time.time() + timeout
        while time.time() < deadline:
            QApplication.processEvents()
            if expect_more and len(got) > before:
                return True
            time.sleep(0.02)
        QApplication.processEvents()
        return len(got) > before

    def test_matching_community_is_accepted(self):
        _m, port, got = self._start(["public"])
        self.assertTrue(self._send_and_wait(port, "public", got, True),
                        "許可したコミュニティの Trap が受理されない")

    def test_mismatched_community_is_dropped(self):
        _m, port, got = self._start(["secret-community"])
        self.assertFalse(self._send_and_wait(port, "public", got, False),
                         "許可していないコミュニティの Trap が受理された")

    def test_multiple_allowed_communities(self):
        _m, port, got = self._start(["public", "netbelt", "ops"])
        for c in ("public", "netbelt", "ops"):
            with self.subTest(community=c):
                self.assertTrue(self._send_and_wait(port, c, got, True))
        self.assertFalse(self._send_and_wait(port, "other", got, False))

    def test_community_match_is_case_sensitive(self):
        """コミュニティ名は大文字小文字を区別する（SNMP の仕様どおり）。"""
        _m, port, got = self._start(["Public"])
        self.assertFalse(self._send_and_wait(port, "public", got, False))

    def test_default_is_public(self):
        """未指定なら public のみを許可する（従来の既定を維持）。"""
        _m, port, got = self._start(None)
        self.assertTrue(self._send_and_wait(port, "public", got, True))
        self.assertFalse(self._send_and_wait(port, "private", got, False))


    def test_an_empty_list_allows_no_community(self):
        """空リストは「v1/v2c を受けない」であって、既定の public ではない。

        Trap のバージョンに v3 を選ぶとパネルは [] を渡す。ここが既定値へ
        落ちると、認証も暗号も要求しない public の v2c Trap が黙って通る。
        v3 を選んだ利用者からはコミュニティ欄が使われていないように見えるので、
        public が有効なことに気づく手掛かりが画面上に無い。
        """
        m, port, got = self._start([])
        self.assertEqual(m.trap_receiver.communities, [],
                         "空リストが既定値へ落ちている")
        self.assertFalse(self._send_and_wait(port, "public", got, False),
                         "v1/v2c を受けない設定なのに public の Trap を受理した")

    def test_a_v1_trap_does_not_expose_its_community(self):
        """v1 Trap のコミュニティを画面にもエクスポートにも載せないこと。

        pysnmp は v1 Trap を v2c へ変換する際に snmpTrapCommunity
        （1.3.6.1.6.3.18.1.4.0）を合成し、コミュニティ文字列そのものを
        varbind として足す。v1/v2c ではこれが唯一の認証情報であり、
        varbinds はそのまま CSV / JSON / TXT へ書き出される。
        """
        from PyQt6.QtWidgets import QApplication
        _m, port, got = self._start(["s3cret-community"])

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(v1_trap_bytes("s3cret-community"), ("127.0.0.1", port))
        s.close()
        deadline = time.time() + 3
        while time.time() < deadline and not got:
            QApplication.processEvents()
            time.sleep(0.02)
        self.assertTrue(got, "v1 Trap が届かない")

        oids = [vb["oid"] for vb in got[-1]["varbinds"]]
        values = [vb["value"] for vb in got[-1]["varbinds"]]
        self.assertNotIn("1.3.6.1.6.3.18.1.4.0", oids,
                         "snmpTrapCommunity が varbind に残っている")
        self.assertNotIn("s3cret-community", values,
                         "コミュニティ文字列が値として残っている")

    def test_a_v1_trap_still_reports_the_agent_address(self):
        """送信元アドレスは残す。プロキシ経由だと送信元 IP と別物になる。"""
        from PyQt6.QtWidgets import QApplication
        _m, port, got = self._start(["s3cret-community"])

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(v1_trap_bytes("s3cret-community"), ("127.0.0.1", port))
        s.close()
        deadline = time.time() + 3
        while time.time() < deadline and not got:
            QApplication.processEvents()
            time.sleep(0.02)
        self.assertTrue(got)

        oids = [vb["oid"] for vb in got[-1]["varbinds"]]
        self.assertIn("1.3.6.1.6.3.18.1.3.0", oids,
                      "snmpTrapAddress まで落としている")
    def test_parsed_trap_keeps_source_and_varbinds(self):
        """受理した Trap の中身がこれまでどおり取り出せること。"""
        _m, port, got = self._start(["public"])
        self.assertTrue(self._send_and_wait(port, "public", got, True))
        trap = got[-1]
        self.assertEqual(trap["source_ip"], "127.0.0.1")
        self.assertIn("source_port", trap)
        self.assertIn("varbinds", trap)


if __name__ == "__main__":
    unittest.main()
