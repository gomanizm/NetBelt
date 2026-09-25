"""読まない相手への Telnet の貼り付けのテストが、OS の送信バッファの大きさによらず本当に詰まることを検証する。

対象は test_telnet_paste_to_slow_reader.py の、相手が読まなくなったときの 2 件
（test_a_stalled_reader_keeps_the_session_and_the_rest_stays_queued と
test_a_peer_that_closes_while_backlogged_is_reported）。どちらも、詰まっている
間の後始末・相手の切断で、送信の見張りのスレッドが止まることを確かめる。

何が起きていたか。CI では通っているが、1.3.1 のリリースのワークフロー
（GitHub の windows-latest）で落ちた test_paste_to_slow_device_keeps_session.py
と同じ前提（192KB を渡すうちに NetBelt の送信バッファが埋まる）に頼って
いた。しかも見張りの確かめは『if thread is not None:』の中にあり、詰まら
なければ黙って飛ばしていた。送信バッファは、ソケットに SO_SNDBUF を明示
しない限り Windows が接続ごとに自動で大きさを決める（動的な送信バッファ）
ので、詰まるかどうかは環境で変わる。
  - 直す前の対象の 2 件を、NetBelt の Telnet のソケットの SO_SNDBUF を接続の
    直後に 1MB にして流すと（送信バッファが大きい環境の代わり）、どちらも
    通ったが、見張りのスレッドは 1 回も動かなかった（このパソコンの既定の
    ままでは 1 回ずつ動く）。見張りが止まることは確かめられていなかった。

どう直したか（テスト側だけ。製品のコードは変えない）。2 件とも、接続の直後に
NetBelt のソケットの SO_SNDBUF を 4096 に明示し（Windows は SO_SNDBUF を
設定したソケットでは動的な送信バッファを使わない。512 バイトずつなら
12,288 バイトで止まる、実測）、見張りのスレッドが動き出すまで（最大 10 秒）
回す。動き出したことを前提として確かめ、見張りが止まることを条件なしに
確かめる。

このファイルは、送信バッファが大きい環境を作り（Telnet の接続のソケットの
SO_SNDBUF を接続の直後に 1MB にする）、その中で対象の 2 件が通り、しかも
それぞれで見張りのスレッドが動いた（詰まった）ことを確かめる。
"""
import importlib.util
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

TARGET = Path(__file__).with_name("test_telnet_paste_to_slow_reader.py")
TARGET_CLASS = "TelnetPasteToSlowReaderTest"
STALLED_TESTS = [
    "test_a_stalled_reader_keeps_the_session_and_the_rest_stays_queued",
    "test_a_peer_that_closes_while_backlogged_is_reported",
]
# 送信バッファが大きい環境の代わり（貼り付けの 192KB より十分大きい）
LARGE_SEND_BUFFER = 1024 * 1024


class TelnetSlowReaderStallOnAnySendBufferTest(unittest.TestCase):
    def test_stalled_tests_really_stall_with_a_large_send_buffer(self):
        from core.send_backpressure import DrainWatcher
        from core.telnet_connection import TelnetConnection
        spec = importlib.util.spec_from_file_location(
            "_large_sndbuf_slow_reader", TARGET)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_connect = TelnetConnection.connect
        original_run = DrainWatcher._run
        watcher_runs = []

        def connect_with_large_send_buffer(conn):
            ok = original_connect(conn)
            if ok and conn.socket is not None:
                conn.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                       LARGE_SEND_BUFFER)
            return ok

        def counted_run(watcher):
            watcher_runs.append(watcher)
            return original_run(watcher)

        problems, stalls = [], {}
        with mock.patch.object(TelnetConnection, "connect",
                               connect_with_large_send_buffer), \
                mock.patch.object(DrainWatcher, "_run", counted_run):
            for name in STALLED_TESTS:
                del watcher_runs[:]
                result = unittest.TestResult()
                unittest.defaultTestLoader.loadTestsFromName(
                    "%s.%s" % (TARGET_CLASS, name), module).run(result)
                problems += ["%s\n%s" % (test.id(), trace)
                             for test, trace in result.failures + result.errors]
                self.assertEqual(1, result.testsRun)
                self.assertEqual([], result.skipped)
                stalls[name] = len(watcher_runs)

        if problems:
            self.fail("送信バッファが大きい環境で、対象のテストが通らない:\n\n"
                      + "\n\n".join(problems))
        self.assertEqual(
            [], [name for name, runs in stalls.items() if runs == 0],
            "送信バッファが大きい環境で、送信が詰まらないまま"
            "（見張りが動かないまま）通った: %r" % (stalls,))


if __name__ == "__main__":
    unittest.main()
