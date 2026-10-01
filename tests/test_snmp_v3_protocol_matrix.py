"""SNMPv3 の認証・暗号方式の全組み合わせで、GET と Trap 受信が実際に通ることを確かめる。

なぜ要るか: pyasn1 の脆弱性（GHSA-8ppf-4f7h-5ppj / CVE-2026-59885）対応で、
pysnmp を 5.1.0 から 7.1.28 へ移した。v3 の方式は UI で認証 6 種
（MD5・SHA・SHA-224・SHA-256・SHA-384・SHA-512）× 暗号 6 種（なし・DES・
3DES・AES-128・AES-192・AES-256）を選べるが、それまでのテストは方式名と
定数の対応（test_snmp_v3.py）と、実通信は SHA-256 / AES-128 などの一部しか
通していなかった。移行で方式が黙って使えなくなっても気づけない（実際、
最初に選んだ pysnmp 7.1.30 の AES は、cryptography 47 以上にしか無い
decrepit.ciphers.modes が無いと例外を出さずに無効になった。実測。そのため
7.1.28 にした）。方式を削ったり弱い方式に置き換えたりしていないことを、
全組み合わせの実通信で確かめる。

何を確かめるか:
  - GET: tests/snmp_loopback_agent.py の応答側に同じ方式の v3 ユーザを
    登録し、NetBelt の SNMPManager の GET で値が取れること（組み合わせごとに
    subTest）。応答側の USM は、登録と違う securityLevel の要求を断る
    （弱めると GET が「Unsupported SNMP security level」で失敗する。実測）。
    それに加えて、応答側に届いた要求の securityLevel が方式どおりである
    こともじかに確かめる
  - Trap: pysnmp 7 の send_notification（専用のスレッドとループ）で同じ方式の
    v3 Trap を送り、NetBelt の SNMPTrapReceiver（v3_users に同じ名前・方式・
    パスワード・送信側の engineID）が受け取り、security_level が方式どおり
    であること
  - AES-192 / AES-256 は Reeder 版（Cisco 等と相互接続する方）であること:
    相手が Blumenthal 版だと GET は失敗し Trap は届かない。Reeder 版同士は
    通る

AES-192/256 の取り違えの確認は、認証を SHA（SHA-1）にして行う。2 つの版は
鍵の延ばし方だけが違い、認証のハッシュの出力が鍵長以上だと延ばさないので
同じ鍵になる。AES-192 は SHA-224 以上、AES-256 は SHA-256 以上で区別が
付かない（pysnmp 7.1.30 と 7.1.28 で実測）。SHA-1（20 バイト）なら
どちらも延ばす。

実行時間: 相手役は 1 つに全ユーザを登録し（鍵の導出は起動時に 1 回ずつ）、
Trap の受信側は暗号方式ごとに 1 つだけ立てる。組み合わせは間引かない。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")
# 補助モジュール（tests/snmp_loopback_agent.py）を、起動の仕方によらず読めるように
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import snmp_loopback_agent as loopback   # noqa: E402

HOST = loopback.HOST
ENTERPRISE = loopback.oid_text(loopback.ENTERPRISE)
GET_OID = ENTERPRISE + ".1.1.0"
GET_ROWS = [("SNMPv2-SMI::enterprises.32473.1.1.0", "OctetString",
             "netbelt-agent")]

AUTH_NAMES = ("MD5", "SHA", "SHA-224", "SHA-256", "SHA-384", "SHA-512")
PRIV_NAMES = ("none", "DES", "3DES", "AES-128", "AES-192", "AES-256")
# noAuthNoPriv と、認証 6 種 × 暗号 6 種（認証なしで暗号化は選べない）
COMBINATIONS = (("none", "none"),) + tuple(
    (auth, priv) for auth in AUTH_NAMES for priv in PRIV_NAMES)


def matrix_user(auth, priv):
    """組み合わせごとのユーザ（名前もパスワードも組み合わせごとに違う）"""
    return loopback.v3_user("nb-%s-%s" % (auth.lower(), priv.lower()),
                            auth, priv)


class _MatrixTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        # 作った SNMPManager はクラス終了まで保持する（キューに残った
        # シグナルの配送先を先に捨てると落ちる。既存のテストと同じ理由）
        cls._managers = []

    @classmethod
    def tearDownClass(cls):
        for manager in cls._managers:
            manager.stop_trap_receiver()
        for _ in range(3):
            cls.app.processEvents()
        cls._managers.clear()

    def _manager(self):
        from core.snmp_manager import SNMPManager
        manager = SNMPManager()
        type(self)._managers.append(manager)
        return manager


class V3MatrixCoversTheUiTest(unittest.TestCase):

    def test_the_matrix_covers_every_choice_in_the_ui(self):
        """UI の選択肢が増減したら、この表も合わせること。"""
        from core.snmp_manager import V3_AUTH_PROTOCOL_NAMES, V3_PRIV_PROTOCOL_NAMES
        self.assertEqual(set(AUTH_NAMES) | {"none"}, set(V3_AUTH_PROTOCOL_NAMES))
        self.assertEqual(set(PRIV_NAMES), set(V3_PRIV_PROTOCOL_NAMES))
        self.assertEqual(len(COMBINATIONS), 37)


class V3GetMatrixTest(_MatrixTestCase):

    def _get(self, recorder, port, user):
        recorder.run(lambda: recorder.manager.snmp_get(
            HOST, [GET_OID], port=port, **loopback.netbelt_v3_params(user)))
        return recorder.result()

    def test_every_combination_can_get_a_value(self):
        users = {combination: matrix_user(*combination)
                 for combination in COMBINATIONS}
        agent = loopback.LoopbackAgent(communities=(),
                                       v3_users=list(users.values()))
        agent.start()
        self.addCleanup(agent.stop)
        recorder = loopback.ManagerRecorder(self._manager())

        for (auth, priv), user in users.items():
            with self.subTest(auth=auth, priv=priv):
                ok, payload = self._get(recorder, agent.port, user)
                self.assertTrue(ok, "%s / %s で GET できない: %r"
                                % (auth, priv, payload))
                self.assertEqual(payload, GET_ROWS)
                self.assertEqual(recorder.errors, [])
                # 弱めた方式で送っていないこと（応答側に届いた要求で見る）
                self.assertEqual(
                    agent.security_levels(user["username"]),
                    {int(loopback.security_level(auth, priv))},
                    "%s / %s の要求の securityLevel が方式と違う" % (auth, priv))

    def test_aes_192_and_256_are_the_reeder_variants(self):
        """相手が Blumenthal 版だと失敗し、Reeder 版なら通る。"""
        reeder, blumenthal = {}, {}
        for size in loopback.BLUMENTHAL_PRIV_PROTOCOLS:
            reeder[size] = loopback.v3_user(
                "nb-reeder-%s" % size.lower(), "SHA", size)
            blumenthal[size] = loopback.v3_user(
                "nb-blumenthal-%s" % size.lower(), "SHA", size,
                priv_protocol=loopback.BLUMENTHAL_PRIV_PROTOCOLS[size])
        agent = loopback.LoopbackAgent(
            communities=(),
            v3_users=list(reeder.values()) + list(blumenthal.values()))
        agent.start()
        self.addCleanup(agent.stop)
        recorder = loopback.ManagerRecorder(self._manager())

        for size in loopback.BLUMENTHAL_PRIV_PROTOCOLS:
            with self.subTest(priv=size, agent="Reeder"):
                ok, payload = self._get(recorder, agent.port, reeder[size])
                self.assertTrue(ok, "Reeder 版の相手に GET できない: %r"
                                % (payload,))
                self.assertEqual(payload, GET_ROWS)
            with self.subTest(priv=size, agent="Blumenthal"):
                # NetBelt 側は同じ名前・同じパスワードで「AES-192/256」を選ぶ
                ok, payload = self._get(recorder, agent.port, blumenthal[size])
                self.assertFalse(ok, "Blumenthal 版の相手に通ってしまった"
                                 "（Reeder 版を使っていない）: %r" % (payload,))

    def test_sha1_derives_different_keys_for_the_two_aes_variants(self):
        """上の取り違えの確認が意味を持つこと（SHA では 2 つの版の鍵が違う）。"""
        from pysnmp.proto.rfc1902 import OctetString
        from pysnmp.proto.secmod.eso.priv import aes192, aes256
        auth = loopback.AUTH_PROTOCOLS["SHA"]
        engine_id = OctetString(hexValue=loopback.new_engine_id("keys"))
        for size, services in (("AES-192", (aes192.Aes192(),
                                            aes192.AesBlumenthal192())),
                               ("AES-256", (aes256.Aes256(),
                                            aes256.AesBlumenthal256()))):
            with self.subTest(priv=size):
                keys = [bytes(service.localize_key(
                            auth, service.hash_passphrase(auth, "priv-pass-keys"),
                            engine_id))
                        for service in services]
                self.assertNotEqual(keys[0], keys[1])


class V3TrapMatrixTest(_MatrixTestCase):

    def _start_receiver(self, users, sender_engine_id):
        """NetBelt の受信側を立てる。(manager, 受け取った Trap, エラー) を返す"""
        manager = self._manager()
        traps, errors, started = [], [], []
        manager.trap_received.connect(traps.append)
        manager.error_occurred.connect(errors.append)
        manager.trap_receiver_started.connect(lambda: started.append(True))
        port = loopback.free_udp_port()
        self.assertTrue(
            manager.start_trap_receiver(
                port, [],
                [loopback.netbelt_trap_user(user, [sender_engine_id])
                 for user in users]),
            "受信を始められない: %r" % (errors,))
        self.addCleanup(manager.stop_trap_receiver)
        self.assertTrue(loopback.pump_until(lambda: started, 15),
                        "受信が始まらない: %r" % (errors,))
        return manager, port, traps, errors

    @staticmethod
    def _received(traps, label):
        return [trap for trap in traps
                if any(varbind.get("value") == label
                       for varbind in trap.get("varbinds", []))]

    def _send_and_collect(self, users_and_labels, receiver_users):
        """受信側を立てて Trap を送り、届いた分を集める。

        Returns:
            (送信の結果 {目印: エラー or None}, 届いた Trap, 受信側のエラー)
        """
        sender_engine_id = loopback.new_engine_id("sender")
        manager, port, traps, errors = self._start_receiver(
            receiver_users, sender_engine_id)
        results = dict(loopback.send_v3_traps(
            port, sender_engine_id, users_and_labels))
        # 送れたものが全部届くまで待つ（送れなかったものは待たない）
        sent = [label for label, error in results.items() if error is None]
        loopback.pump_until(
            lambda: all(self._received(traps, label) for label in sent), 15)
        # 同じ Trap が 2 回届かないことも見るので、少しだけ余分に待つ
        loopback.pump_until(lambda: False, 0.3)
        manager.stop_trap_receiver()
        loopback.pump_until(lambda: False, 0.1)
        return results, list(traps), list(errors)

    def test_every_combination_is_received_with_its_security_level(self):
        # 受信側は暗号方式ごとに 1 つ（1 つの方式が壊れても、ほかの方式の
        # 受信を巻き込まない）
        for priv in PRIV_NAMES:
            combinations = [(auth, p) for auth, p in COMBINATIONS if p == priv]
            users = [matrix_user(*combination) for combination in combinations]
            results, traps, errors = self._send_and_collect(
                [(user, "label-" + user["username"]) for user in users], users)
            with self.subTest(priv=priv, check="receiver errors"):
                self.assertEqual(errors, [])
            for (auth, p), user in zip(combinations, users):
                label = "label-" + user["username"]
                with self.subTest(auth=auth, priv=p):
                    self.assertIsNone(
                        results[label],
                        "送信側が %s / %s の Trap を送れない" % (auth, p))
                    received = self._received(traps, label)
                    self.assertEqual(len(received), 1,
                                     "%s / %s の Trap が %d 件届いた"
                                     % (auth, p, len(received)))
                    trap = received[0]
                    self.assertEqual(trap.get("security_name"), user["username"])
                    self.assertEqual(trap.get("security_model"), "3")
                    self.assertEqual(trap.get("security_level"),
                                     loopback.security_level(auth, p))

    def test_aes_192_and_256_traps_are_the_reeder_variants(self):
        """Blumenthal 版で送った Trap は届かず、あとから Reeder 版で送った Trap は届く。

        同じソケットから順に送るので、あとの Trap が届いた時点で前の Trap は
        処理済み（届かなかったことが確定する）。
        """
        receiver_users, notifications = [], []
        for size in loopback.BLUMENTHAL_PRIV_PROTOCOLS:
            user = loopback.v3_user("nb-trap-%s" % size.lower(), "SHA", size)
            receiver_users.append(user)
            blumenthal = dict(
                user, priv_protocol=loopback.BLUMENTHAL_PRIV_PROTOCOLS[size])
            notifications.append((blumenthal, "blumenthal-" + size))
            notifications.append((user, "reeder-" + size))
        results, traps, errors = self._send_and_collect(
            notifications, receiver_users)

        for size in loopback.BLUMENTHAL_PRIV_PROTOCOLS:
            with self.subTest(priv=size):
                self.assertIsNone(results["blumenthal-" + size],
                                  "送信側が Blumenthal 版で送れない")
                self.assertIsNone(results["reeder-" + size],
                                  "送信側が Reeder 版で送れない")
                self.assertEqual(len(self._received(traps, "reeder-" + size)), 1,
                                 "Reeder 版の Trap が届かない")
                self.assertEqual(self._received(traps, "blumenthal-" + size), [],
                                 "Blumenthal 版の Trap を受け取った"
                                 "（Reeder 版を使っていない）")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
