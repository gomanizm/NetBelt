"""採取データが公開できる状態であることを見張る。

tests/fixtures/*.bin は実機・実ラボから採った受信バイトそのもの。
このリポジトリは公開されているので、実環境を指す値が 1 つでも
混ざったまま入ってはいけない。

判定は「禁止する形を並べる」ではなく「文書用に割り当てられた範囲か」で
行う。並べる方式は、次に別の形が来たときにまた抜けるうえ、実環境の値を
禁止リストとして書き残すことになる。

最初は IPv4 しか見ておらず、`ip -br addr` の出力に混じった IPv6 リンク
ローカルを見落としていた。リンクローカルは EUI-64 で作られるため、
機器の MAC アドレスがそのまま復元できてしまう。
"""
import io
import os
import re
import unittest

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures")

# RFC 5737 / RFC 6890 の文書用・ループバック・ワイルドカードのみ許す
ALLOWED_V4 = re.compile(
    r"^(?:192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|127\.|0\.0\.0\.0)")
# RFC 3849 の文書用のみ許す
ALLOWED_V6 = re.compile(r"^(?:2001:db8:|::1$)", re.I)
# RFC 2606 の予約名か、採取時に置き換えた架空名だけ許す
ALLOWED_HOST = re.compile(
    r"^[\w.-]+@(?:lab|lab-rtr|localhost|[\w.-]+\.(?:example|test|invalid))$",
    re.I)

ANY_V4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
ANY_V6 = re.compile(r"\b(?:[0-9a-f]{1,4}:){3,}[0-9a-f]{0,4}\b", re.I)
MAC_COLON = re.compile(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", re.I)
MAC_CISCO = re.compile(r"\b[0-9a-f]{4}(?:\.[0-9a-f]{4}){2}\b", re.I)
# Cisco の文書用 MAC は 0000.5e00.53xx (RFC 7042)
ALLOWED_MAC_CISCO = re.compile(r"^0000\.5e00\.53", re.I)
USER_AT_HOST = re.compile(r"\b[\w.-]+@[\w.-]+\b")


def captures():
    for name in sorted(os.listdir(FIXTURES)):
        if name.endswith(".bin"):
            raw = io.open(os.path.join(FIXTURES, name), "rb").read()
            yield name, raw.decode("utf-8", "replace")


class FixtureSanitisationTest(unittest.TestCase):
    def test_there_are_captures_to_check(self):
        """見張る対象がある (採取データの置き場所が変わっていない)。"""
        self.assertGreaterEqual(len(list(captures())), 4)

    def test_every_ipv4_is_a_documentation_address(self):
        for name, text in captures():
            with self.subTest(capture=name):
                for ip in ANY_V4.findall(text):
                    self.assertRegex(
                        ip, ALLOWED_V4,
                        "%s: 文書用に割り当てられていないアドレス" % name)

    def test_every_ipv6_is_a_documentation_address(self):
        for name, text in captures():
            with self.subTest(capture=name):
                for addr in ANY_V6.findall(text):
                    self.assertRegex(
                        addr, ALLOWED_V6,
                        "%s: 文書用に割り当てられていないアドレス" % name)

    def test_no_mac_address_survives(self):
        for name, text in captures():
            with self.subTest(capture=name):
                found = MAC_COLON.search(text)
                self.assertIsNone(
                    found, "%s: MAC アドレスが残っている: %s"
                    % (name, found.group(0) if found else ""))
                for mac in MAC_CISCO.findall(text):
                    self.assertRegex(
                        mac, ALLOWED_MAC_CISCO,
                        "%s: 文書用でない MAC が残っている" % name)

    def test_no_real_host_or_user_name_survives(self):
        for name, text in captures():
            with self.subTest(capture=name):
                for pair in USER_AT_HOST.findall(text):
                    if "@" not in pair:
                        continue
                    self.assertRegex(
                        pair, ALLOWED_HOST,
                        "%s: 実環境を指す名前が残っている" % name)


if __name__ == "__main__":
    unittest.main()
