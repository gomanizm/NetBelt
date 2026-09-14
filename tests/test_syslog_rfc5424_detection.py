"""RFC 5424 と決める条件が緩すぎて、日時の無い本文が食われないことを確認する。

_parse は PRI 以降が `^(\\d+)\\s+` に当たるだけで RFC 5424 と決めていた。
本文が「数字＋空白」で始まる（日時を付けない機器の "3 interfaces are
down ..." など）と RFC 5424 側へ回され、そちらは日時を見ないので
parts[2] がホスト名として拾われ、本文の大半が捨てられる。
tests/test_syslog_rfc3164_no_timestamp.py で塞いだ穴へ、隣の経路から
同じように到達できる。

実測: '<14>3 interfaces are down on rtr01 now'
      → hostname='are' message='now'

VERSION の次が RFC 5424 の TIMESTAMP（RFC 3339 か NILVALUE '-'）で
あるときだけ RFC 5424 として消費し、そうでなければ RFC 3164 経路へ落とす。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

SOURCE_IP = "192.0.2.10"


class SyslogRfc5424DetectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _parse(self, raw):
        from core.syslog_receiver import SyslogMessage
        return SyslogMessage(raw, SOURCE_IP)

    # --- 誤って RFC 5424 に振り分けられていた本文 -------------------------

    def test_body_starting_with_a_digit_is_not_rfc5424(self):
        m = self._parse("<14>3 interfaces are down on rtr01 now")
        self.assertEqual(m.message, "3 interfaces are down on rtr01 now",
                         "本文が RFC 5424 のヘッダとして食われた: %r" % m.message)
        self.assertEqual(m.hostname, SOURCE_IP,
                         "ホスト名が本文から拾われた: %r" % m.hostname)

    def test_body_starting_with_a_number_and_counting_words_is_kept(self):
        m = self._parse("<14>2 of 3 links are down on rtr01")
        self.assertEqual(m.message, "2 of 3 links are down on rtr01")
        self.assertEqual(m.hostname, SOURCE_IP)

    def test_body_starting_with_a_long_number_is_kept(self):
        m = self._parse("<14>2026 packets dropped by acl 101 on Gi0/1")
        self.assertEqual(m.message, "2026 packets dropped by acl 101 on Gi0/1")
        self.assertEqual(m.hostname, SOURCE_IP)

    def test_body_starting_with_a_digit_and_a_real_rfc3164_timestamp(self):
        """数字始まりでも、その先が RFC 3164 の日時なら従来どおり消費する"""
        m = self._parse("<134>Sep  9 10:00:00 rtr01 5 interfaces are down")
        self.assertEqual(m.hostname, "rtr01")
        self.assertEqual(m.message, "5 interfaces are down")

    # --- 本物の RFC 5424 はこれまでどおり ---------------------------------

    def test_real_rfc5424_is_still_parsed(self):
        m = self._parse("<134>1 2026-09-09T14:12:02Z rtr1 app 123 ID47 - link down")
        self.assertEqual(m.hostname, "rtr1")
        self.assertEqual(m.message, "link down")

    def test_real_rfc5424_with_offset_and_fraction_is_still_parsed(self):
        m = self._parse(
            "<134>1 2026-09-09T14:12:02.123456+09:00 rtr1 app - - - link up")
        self.assertEqual(m.hostname, "rtr1")
        self.assertEqual(m.message, "link up")

    def test_real_rfc5424_with_nil_timestamp_is_still_parsed(self):
        m = self._parse("<134>1 - rtr1 app - - - link flap")
        self.assertEqual(m.hostname, "rtr1")
        self.assertEqual(m.message, "link flap")

    def test_real_rfc5424_with_nil_hostname_falls_back_to_source_ip(self):
        m = self._parse("<134>1 2026-09-09T14:12:02Z - app - - - link down")
        self.assertEqual(m.hostname, SOURCE_IP)
        self.assertEqual(m.message, "link down")


if __name__ == "__main__":
    unittest.main()
