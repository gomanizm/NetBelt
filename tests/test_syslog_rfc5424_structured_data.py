"""RFC 5424 の STRUCTURED-DATA にパラメータがあっても本文が正しく取れることを確認する。

_parse_rfc5424 は message.split(None, 7) で 7 つ目を SD とみなしていたため、
SD-ID とパラメータの間の空白で SD が分断され、本文の先頭に `key="..."]` の
残骸が混ざっていた。実測: (a) message='key="a b"] hello'
(b) message='key="ab"] hello'。rsyslog の [timeQuality ...] など
パラメータ付き SD 全般で起きる。

SD は `[` から対応する `]`（`\\]` エスケープ、引用符内の `]` を考慮）まで
読み進める。nil（"-"）はこれまでどおり。
"""
import sys
import unittest

sys.path.insert(0, "src")

HDR = "<134>1 2026-09-09T14:12:02Z host app 1234 ID47 "


class SyslogRfc5424StructuredDataTest(unittest.TestCase):
    def _msg(self, raw):
        from core.syslog_receiver import SyslogMessage
        return SyslogMessage(raw, "192.0.2.1", "UDP", 514)

    def test_nil_structured_data_is_unchanged(self):
        m = self._msg(HDR + "- hello world")
        self.assertEqual(m.hostname, "host")
        self.assertEqual(m.message, "hello world")

    def test_sd_param_value_with_space(self):
        m = self._msg(HDR + '[ex@1 key="a b"] hello')
        self.assertEqual(m.hostname, "host")
        self.assertEqual(m.message, "hello")

    def test_sd_param_without_space_in_value(self):
        m = self._msg(HDR + '[ex@1 key="ab"] hello')
        self.assertEqual(m.message, "hello")

    def test_multiple_sd_elements(self):
        m = self._msg(HDR + '[timeQuality tzKnown="1" isSynced="1"][origin ip="192.0.2.9"] link down')
        self.assertEqual(m.message, "link down")

    def test_escaped_bracket_and_quote_inside_param_value(self):
        m = self._msg(HDR + r'[ex@1 k="a\]b \"q\" c"] body here')
        self.assertEqual(m.message, "body here")

    def test_sd_without_message(self):
        m = self._msg(HDR + '[ex@1 k="v"]')
        self.assertEqual(m.message, "")

    def test_unterminated_sd_falls_back_to_the_rest_as_message(self):
        # 壊れた SD は本文の一部として残す（欠落させない）
        m = self._msg(HDR + '[ex@1 k="v" hello')
        self.assertIn("hello", m.message)


if __name__ == "__main__":
    unittest.main()
