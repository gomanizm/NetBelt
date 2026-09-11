"""ポートチェッカーの netstat 経路が、指定したポートだけを数えることを確認する。

findstr :{port} と `f':{port}' in line` は前方一致の部分文字列判定なので、
80 を調べると 8080 の待ち受けやリモート側 :80 の外向き接続まで「ポート 80
を使用している接続」として並び、別プロセスの PID が原因として表示されて
いた（実測 F10-A/E）。netstat 経路はプロトコルも見ず（UDP で調べても TCP
行が出る: F10-B）、listening 経路は状態列を見ないので ESTABLISHED だけの
ポートも「リスニング」に載っていた（F10-G）。
"""
import os
import socket
import sys
import unittest

sys.path.insert(0, "src")

WINDOWS = sys.platform == "win32"

SAMPLE = """
アクティブな接続

  プロトコル  ローカル アドレス      外部アドレス           状態            PID
  TCP         0.0.0.0:135            0.0.0.0:0              LISTENING       1880
  TCP         127.0.0.1:8080         0.0.0.0:0              LISTENING       8980
  TCP         192.0.2.25:59714       198.51.100.7:80        ESTABLISHED     23160
  TCP         127.0.0.1:80           0.0.0.0:0              LISTENING       4242
  TCP         [::]:80                [::]:0                 LISTENING       4242
  TCP         127.0.0.1:47130        127.0.0.1:64189        ESTABLISHED     8980
  TCP         127.0.0.1:64189        127.0.0.1:47130        ESTABLISHED     8980
  UDP         0.0.0.0:80             *:*                                    5150
  UDP         [::1]:1900             *:*                                    999
"""


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


class SelectNetstatLinesTest(unittest.TestCase):
    """netstat 出力の行選びを、決まった入力で確かめる。"""

    def _select(self, port, protocol, listening_only=False):
        from ui.port_checker_gui import select_netstat_lines
        return select_netstat_lines(SAMPLE, port, protocol, listening_only)

    def test_port_matches_the_local_port_exactly(self):
        lines = self._select(80, "TCP")
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(all(":80 " in l for l in lines), lines)
        self.assertFalse(any("8080" in l or "198.51.100.7:80" in l for l in lines), lines)

    def test_a_prefix_of_a_longer_port_does_not_match(self):
        self.assertEqual(self._select(4713, "TCP"), [])
        self.assertEqual(self._select(8, "TCP"), [])

    def test_protocol_is_honoured(self):
        udp = self._select(80, "UDP")
        self.assertEqual(len(udp), 1, udp)
        self.assertTrue(udp[0].strip().startswith("UDP"), udp)
        self.assertFalse(any(l.strip().startswith("UDP") for l in self._select(80, "TCP")))

    def test_listening_only_drops_established_rows(self):
        self.assertEqual(self._select(47130, "TCP", listening_only=True), [])
        self.assertEqual(len(self._select(47130, "TCP")), 1)
        self.assertEqual(len(self._select(80, "TCP", listening_only=True)), 2)

    def test_udp_rows_have_no_state_and_are_kept_with_listening_only(self):
        self.assertEqual(len(self._select(80, "UDP", listening_only=True)), 1)

    def test_headers_and_blank_lines_are_ignored(self):
        from ui.port_checker_gui import select_netstat_lines
        self.assertEqual(select_netstat_lines(SAMPLE, None, "TCP", False).__len__(), 7)
        self.assertEqual(select_netstat_lines("", 80, "TCP", False), [])


@unittest.skipUnless(WINDOWS, "Windows の netstat -ano の出力を使う")
class NetstatPathWithRealSocketsTest(unittest.TestCase):
    def _listener(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s, s.getsockname()[1]

    def test_the_listener_line_is_attributed_to_its_own_port_only(self):
        _, port = self._listener()
        mine = f"127.0.0.1:{port} "
        result = run_check(port, "netstat", "TCP")
        self.assertIn(mine, result, result)
        self.assertIn(f"PID {os.getpid()}:", result, result)

        # 前方一致で拾われていた「短いポート番号」では出ないこと（F10-E）
        prefix = int(str(port)[:-1])
        result = run_check(prefix, "netstat", "TCP")
        self.assertNotIn(mine, result, result)

    def test_udp_query_does_not_list_a_tcp_listener(self):
        _, port = self._listener()
        result = run_check(port, "netstat", "UDP")
        self.assertNotIn(f"127.0.0.1:{port} ", result, result)

    def test_listening_path_ignores_an_established_pair_without_a_listener(self):
        srv, port = self._listener()
        client = socket.create_connection(("127.0.0.1", port))
        self.addCleanup(client.close)
        accepted, _ = srv.accept()
        self.addCleanup(accepted.close)
        srv.close()   # 待ち受けは閉じ、確立済みの対だけ残す

        result = run_check(port, "listening", "TCP")
        self.assertNotIn(f"TCPポート {port} を使用している接続", result, result)

    def test_listening_path_shows_a_real_listener(self):
        _, port = self._listener()
        result = run_check(port, "listening", "TCP")
        self.assertIn(f"TCPポート {port} を使用している接続", result, result)
        self.assertIn("LISTENING", result, result)


if __name__ == "__main__":
    unittest.main()
