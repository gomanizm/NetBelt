"""ポートチェッカーのバインドテストが、使用中のポートを見逃さないことを確認する。

テスト用ソケットに SO_REUSEADDR を立てていたため、Windows では占有側も
SO_REUSEADDR を持っていると同じポートへの bind が通り、占有中なのに
「バインド可能です／使用されていません」と表示していた（実測: UDP/TCP
とも）。占有側が SO_REUSEADDR 無しの場合は WinError 10013 になり、10048
だけを見る分岐から外れて汎用の「✗ エラー:」表示になっていた。
"""
import os
import socket
import sys
import unittest

sys.path.insert(0, "src")

WINDOWS = sys.platform == "win32"


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


class BindTestDetectsHolderTest(unittest.TestCase):
    def _holder(self, protocol, reuse, addr):
        kind = socket.SOCK_DGRAM if protocol == "UDP" else socket.SOCK_STREAM
        s = socket.socket(socket.AF_INET, kind)
        self.addCleanup(s.close)
        if reuse:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((addr, 0))
        if protocol == "TCP":
            s.listen(1)
        return s.getsockname()[1]

    def _assert_in_use(self, result, port, protocol):
        self.assertNotIn("バインド可能", result, result)
        self.assertNotIn("使用されていません", result, result)
        self.assertIn(f"ポート {port}/{protocol} は既に使用されています", result, result)
        self.assertNotIn("✗ エラー:", result, "汎用エラー扱いになっている: " + result)

    @unittest.skipUnless(WINDOWS, "Windows の SO_REUSEADDR の挙動を見る")
    def test_a_holder_with_reuseaddr_is_reported_as_in_use(self):
        for protocol in ("UDP", "TCP"):
            with self.subTest(protocol=protocol):
                port = self._holder(protocol, reuse=True, addr="")
                self._assert_in_use(run_check(port, "bind", protocol), port, protocol)

    @unittest.skipUnless(WINDOWS, "Windows の SO_REUSEADDR の挙動を見る")
    def test_a_holder_without_reuseaddr_is_reported_as_in_use_not_as_a_generic_error(self):
        for protocol in ("UDP", "TCP"):
            with self.subTest(protocol=protocol):
                port = self._holder(protocol, reuse=False, addr="")
                self._assert_in_use(run_check(port, "bind", protocol), port, protocol)

    @unittest.skipUnless(WINDOWS, "Windows の SO_EXCLUSIVEADDRUSE の挙動を見る")
    def test_a_holder_bound_to_a_specific_address_is_reported_as_in_use(self):
        """127.0.0.1 固定で占有されていても見逃さないこと。"""
        for protocol in ("UDP", "TCP"):
            with self.subTest(protocol=protocol):
                port = self._holder(protocol, reuse=False, addr="127.0.0.1")
                self._assert_in_use(run_check(port, "bind", protocol), port, protocol)

    def test_a_free_port_is_reported_as_bindable(self):
        for protocol in ("UDP", "TCP"):
            with self.subTest(protocol=protocol):
                port = self._holder(protocol, reuse=False, addr="")
                # 占有側を閉じてから調べる
                self.doCleanups()
                result = run_check(port, "bind", protocol)
                self.assertIn(f"ポート {port}/{protocol} はバインド可能です", result, result)

    def test_the_test_socket_does_not_leave_the_port_reusable(self):
        """テスト用ソケットが SO_REUSEADDR を立てないこと（立てると占有側に割り込める）。"""
        from ui import port_checker_gui as mod
        seen = []
        real_socket = socket.socket

        class Spy(real_socket):
            def setsockopt(self, level, opt, value, *rest):
                seen.append((level, opt, value))
                return super().setsockopt(level, opt, value, *rest)

        original = mod.socket.socket
        mod.socket.socket = Spy
        try:
            free = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            free.bind(("", 0))
            port = free.getsockname()[1]
            free.close()
            run_check(port, "bind", "UDP")
        finally:
            mod.socket.socket = original
        self.assertNotIn((socket.SOL_SOCKET, socket.SO_REUSEADDR, 1), seen, seen)


if __name__ == "__main__":
    unittest.main()
