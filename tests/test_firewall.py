"""firewall.rule_name のポート範囲（FTP パッシブ）対応を確認する回帰テスト。

ensure_inbound_allow 等は netsh を呼び出し、無人実行では実ファイアウォールの
変更や UAC 昇格を引き起こしうるため対象外とする。ここでは純粋関数である
rule_name のみを検証する。
"""
import sys, unittest
sys.path.insert(0, "src")
from core.firewall import rule_name


class FwRangeTest(unittest.TestCase):
    def test_rule_name_accepts_range(self):
        self.assertEqual(rule_name("FTP Passive", "tcp", "50100-50150"),
                         "NetBelt - FTP Passive (TCP/50100-50150)")

    def test_self_allow_rule_name(self):
        from core.firewall import _self_rule_name
        self.assertEqual(_self_rule_name(), "NetBelt - app inbound (self)")


if __name__ == "__main__":
    unittest.main()
