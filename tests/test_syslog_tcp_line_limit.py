"""Syslog の TCP 受信が、改行の来ない相手に付き合わされないことを検証する。

_handle_tcp_client は buffer += data で受信を連結し、b"\\n" が現れた
ときだけ切り出していた。バッファ長の上限も1行の最大長のチェックも無い
ので、改行を送らない相手（バイナリを吐く誤設定の機器、CR だけで区切る
実装、あるいは繋ぎっぱなしにする1接続）がいると、送られた全バイトが
プロセスのメモリに積み上がる。

しかも recv のたびに `b"\\n" in buffer` でバッファ全体を走査するため
O(n^2) になり、メモリが尽きる前に CPU が飽和して受信スレッドが停滞する。

待受は 0.0.0.0 で、開始時にファイアウォールの受信許可も自動で足すので、
LAN 上の認証されていないホストから引き起こせる。

黙って切り捨てると障害解析に要る末尾を失うので、切ったことは記録に残す。
"""
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, "src")


class SyslogTcpLineLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import core.firewall as fw
        self._orig = fw.ensure_inbound_allow
        fw.ensure_inbound_allow = lambda *a, **k: (True, "test stub")
        from core.syslog_receiver import SyslogReceiver
        self.recv = SyslogReceiver()
        self.seen = []
        self.recv.message_received.connect(self.seen.append)
        self.assertTrue(self.recv.start_protocol("TCP", 0))
        self.port = self.recv.active_port("TCP")

    def tearDown(self):
        self.recv.stop()
        import core.firewall as fw
        fw.ensure_inbound_allow = self._orig

    def _client(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(c.close)
        c.settimeout(5)
        c.connect(("127.0.0.1", self.port))
        return c

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_there_is_a_limit_on_the_length_of_one_line(self):
        self.assertTrue(hasattr(self.recv, "max_line_bytes"),
                        "1行の最大長が決まっていない")
        self.assertGreater(self.recv.max_line_bytes, 0)

    def test_a_peer_that_never_sends_a_newline_is_cut_off(self):
        """改行を送らない相手との接続を、いつまでも保たないこと。"""
        limit = self.recv.max_line_bytes
        c = self._client()
        sent = 0
        try:
            while sent <= limit + 8192:
                c.sendall(b"A" * 4096)
                sent += 4096
        except OSError:
            pass    # 既にサーバ側から切られた

        def closed():
            try:
                c.settimeout(0.3)
                return c.recv(1) == b""
            except socket.timeout:
                return False
            except OSError:
                return True

        self.assertTrue(self._wait(closed),
                        "改行の来ない接続が切られていない")

    def test_being_cut_off_is_recorded(self):
        """黙って切らず、記録に残すこと。"""
        limit = self.recv.max_line_bytes
        c = self._client()
        try:
            sent = 0
            while sent <= limit + 8192:
                c.sendall(b"A" * 4096)
                sent += 4096
        except OSError:
            pass

        # 利用者が見るのは一覧の内容なので、生の文字列ではなく
        # 解析後の message を見る（機器からの行と同じ扱いになること）
        self.assertTrue(
            self._wait(lambda: any("切断" in m.message for m in self.seen)),
            "切ったことがどこにも残っていない: %s"
            % [m.message[:60] for m in self.seen])

    def test_an_overlong_line_that_arrives_complete_is_also_refused(self):
        """上限超過と改行が同じ受信で来ても、受理しないこと。

        判定を「改行がまだ来ていないとき」に限ると、上限を超えた行が
        終端の改行ごと 1 回の recv で届いた場合に素通りする。長さの上限を
        設けた意図（1 行にいくらでも積ませない）を満たさない。
        """
        limit = self.recv.max_line_bytes
        c = self._client()
        c.sendall(b"<134>" + b"A" * (limit + 1000) + b"\n")

        self.assertTrue(
            self._wait(lambda: any("切断" in m.message for m in self.seen)),
            "終端付きの長すぎる行がそのまま受理されている")
        self.assertFalse(
            any(len(m.message) > limit for m in self.seen),
            "上限を超えた行が一覧へ入っている")

    def test_a_normal_line_still_arrives(self):
        """通常のメッセージはこれまでどおり受け取ること。"""
        c = self._client()
        c.sendall(b"<134>Aug 27 22:00:00 rtr1 test message\n")
        self.assertTrue(
            self._wait(lambda: any("test message" in m.message
                                   for m in self.seen)),
            "通常のメッセージが受け取れていない")

    def test_a_long_line_under_the_limit_still_arrives(self):
        """上限に満たない長い行は、切らずに受け取ること。"""
        body = "X" * 4000
        c = self._client()
        c.sendall(("<134>" + body + "\n").encode())
        self.assertTrue(
            self._wait(lambda: any(body in m.message for m in self.seen)),
            "上限に満たない行が失われている")

    def test_a_limit_sized_line_split_between_cr_and_lf_is_not_cut(self):
        """CRLF が CR と LF に分かれて届いても、上限ちょうどの行は切らないこと。

        改行待ちの長さ判定は末尾の CR を数えていた。CR まで届いた時点では
        「上限 + 1 バイト」に見えるので、同じ中身でも CRLF が 1 回の recv に
        収まれば通り、TCP の切れ目が CR と LF の間に来たときだけ切られる。
        """
        limit = self.recv.max_line_bytes
        body = b"<134>" + b"B" * (limit - 5)     # 本文ちょうど上限
        c = self._client()
        c.sendall(body + b"\r")
        time.sleep(0.3)                          # CR だけで判定させる
        c.sendall(b"\n")

        self.assertTrue(
            self._wait(lambda: any(m.message.startswith("BBB")
                                   for m in self.seen)),
            "上限ちょうどの行が届いていない: %s"
            % [m.message[:40] for m in self.seen])
        self.assertFalse(any("切断" in m.message for m in self.seen),
                         "上限ちょうどの行が CR の分だけ超過と数えられて切られた")

    def test_finished_client_threads_are_not_remembered_forever(self):
        """終わった接続のスレッドを溜め込まないこと。

        tcp_clients は append するだけで、終了済みを外していなかった。
        接続を繰り返すとリストが単調に増え、stop() まで解放されない。
        """
        for _ in range(12):
            c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            c.settimeout(5)
            c.connect(("127.0.0.1", self.port))
            c.sendall(b"<134>bye\n")
            c.close()
            time.sleep(0.05)

        self._wait(lambda: len(self.recv.tcp_clients) <= 4, seconds=6.0)
        self.assertLessEqual(len(self.recv.tcp_clients), 4,
                             "終わった接続のスレッドが溜まっている: %d"
                             % len(self.recv.tcp_clients))


if __name__ == "__main__":
    unittest.main()
