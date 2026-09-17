"""バインドテストの結果が、確かめた範囲（IPv4）に限った文面であることを確認する。

テストソケットは AF_INET 固定で bind(('', port)) しているため、IPv6 専用
（IPV6_V6ONLY）の待ち受けとは競合せず bind に成功する。それなのに
「現在このポートは使用されていません」とプロトコル全体を断定しており、
同じレポート内の netstat（IPv6 の行も拾う）と食い違っていた。
"""
import os
import socket
import sys
import unittest

sys.path.insert(0, "src")


def run_check(port, check_type, protocol):
    """PortCheckThread を同じスレッドで走らせ、結果文字列を返す。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from ui.port_checker_gui import PortCheckThread
    out = []
    t = PortCheckThread(port, check_type, protocol)
    t.result_ready.connect(out.append)
    t.run()
    return "".join(out)


@unittest.skipUnless(socket.has_ipv6, "IPv6 が使えない環境")
class BindResultIsScopedToIPv4Test(unittest.TestCase):
    def _ipv6_only_listener(self, protocol):
        """IPv6 専用で待ち受け、そのポート番号を返す。"""
        kind = socket.SOCK_DGRAM if protocol == "UDP" else socket.SOCK_STREAM
        s = socket.socket(socket.AF_INET6, kind)
        self.addCleanup(s.close)
        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind(("::", 0))
        if protocol == "TCP":
            s.listen(1)
        return s.getsockname()[1]

    def test_an_ipv6_only_listener_is_not_declared_unused_for_the_whole_protocol(self):
        for protocol in ("UDP", "TCP"):
            with self.subTest(protocol=protocol):
                port = self._ipv6_only_listener(protocol)
                result = run_check(port, "bind", protocol)
                # IPv4 側は空いているので bind 自体は通る
                self.assertIn(f"ポート {port}/{protocol} はバインド可能です", result, result)
                # 確かめていない IPv6 まで含めて「使用されていません」と断定しないこと
                self.assertNotIn("現在このポートは使用されていません", result, result)
                self.assertIn("IPv4", result, result)

    def test_a_free_port_result_also_states_the_scope(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("", 0))
        port = s.getsockname()[1]
        s.close()
        result = run_check(port, "bind", "TCP")
        self.assertIn(f"ポート {port}/TCP はバインド可能です", result, result)
        self.assertIn("IPv4", result, result)
        self.assertNotIn("現在このポートは使用されていません", result, result)


if __name__ == "__main__":
    unittest.main()
