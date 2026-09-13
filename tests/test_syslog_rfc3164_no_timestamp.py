"""日時を付けない機器の syslog で、本文の先頭が食われないことを確認する。

RFC 3164 のパーサは「空白で 4 つに割れたら parts[0:3] は日時」と決め打ちし、
parts[3] の先頭語をホスト名として捨てていた。日時を付けない送信側
（service timestamps log datetime を切った機器など）では、
メッセージ ID とインターフェース名という一番肝心な部分が黙って消える。

実測: '<14>%LINK-3-UPDOWN: Interface Gi0/1, changed state to down'
      → hostname='changed' message='state to down'

parts[0:3] が RFC 3164 の日時に見えるときだけ日時+ホスト名として消費し、
そうでなければ hostname=source_ip・message=全文へフォールバックする。
"""
import os
import sys
import unittest

sys.path.insert(0, "src")

SOURCE_IP = "192.0.2.10"


class SyslogRfc3164NoTimestampTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _parse(self, raw):
        from core.syslog_receiver import SyslogMessage
        return SyslogMessage(raw, SOURCE_IP)

    def test_message_without_timestamp_keeps_its_whole_body(self):
        m = self._parse(
            "<14>%LINK-3-UPDOWN: Interface Gi0/1, changed state to down")
        self.assertEqual(
            m.message,
            "%LINK-3-UPDOWN: Interface Gi0/1, changed state to down",
            "本文の先頭が日時+ホスト名として食われた: %r" % m.message)
        self.assertEqual(m.hostname, SOURCE_IP,
                         "ホスト名が本文から拾われた: %r" % m.hostname)

    def test_message_without_pri_and_without_timestamp_is_kept(self):
        m = self._parse("kernel: eth0 link is down now")
        self.assertEqual(m.message, "kernel: eth0 link is down now")
        self.assertEqual(m.hostname, SOURCE_IP)

    def test_a_body_that_starts_with_three_words_is_not_a_timestamp(self):
        """日時に似ているだけの語（月名でない・時刻でない）を食わないこと"""
        m = self._parse("<14>Interface Gi0/1 changed state to down")
        self.assertEqual(m.message, "Interface Gi0/1 changed state to down")
        self.assertEqual(m.hostname, SOURCE_IP)

    def test_a_real_rfc3164_timestamp_is_still_consumed(self):
        m = self._parse("<134>Sep  9 10:00:00 rtr01 %SYS-5-CONFIG_I: done")
        self.assertEqual(m.hostname, "rtr01")
        self.assertEqual(m.message, "%SYS-5-CONFIG_I: done")

    def test_a_single_digit_day_without_padding_is_still_a_timestamp(self):
        m = self._parse("<134>Jan 1 00:00:00 rtr01 boot complete")
        self.assertEqual(m.hostname, "rtr01")
        self.assertEqual(m.message, "boot complete")

    def test_a_timestamp_only_message_keeps_the_hostname_behaviour(self):
        """日時+ホスト名だけで本文が無い場合は、これまでどおり本文は空"""
        m = self._parse("<134>Sep  9 10:00:00 rtr01")
        self.assertEqual(m.hostname, "rtr01")
        self.assertEqual(m.message, "")


if __name__ == "__main__":
    unittest.main()
