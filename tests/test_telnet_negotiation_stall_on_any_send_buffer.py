"""送信が満杯のときの交渉のテストが、OS の送信バッファの大きさによらず満杯を作れることを検証する。

対象は test_telnet_negotiation_reply_keeps_reading.py（送信バッファが埋まって
いる間に交渉を求められても受信が止まらないこと、書き出せない応答の上限、
切断後に応答を持ち越さないこと）。

何が起きていたか。CI では通っているが、1.3.1 のリリースのワークフロー
（GitHub の windows-latest）で落ちた test_telnet_send_never_waits_for_reply_writer.py
と同じ前提（NetBelt の送信バッファが埋まる）に頼っていた。送信バッファは、
ソケットに SO_SNDBUF を明示しない限り Windows が接続ごとに自動で大きさを
決める（動的な送信バッファ）ので、埋まるまでの量は環境で変わる。
  - このパソコンの localhost で、読まない相手（SO_RCVBUF 4096）へ待たずに
    書ける量: 16KB ずつで既定 65,536 バイト・SO_SNDBUF 1MB で 1,048,576 バイト、
    512 バイトずつで既定 73,728 バイト・1MB で 1,056,768 バイト。
  - 直す前の対象ファイルを、NetBelt の Telnet のソケットの SO_SNDBUF を
    接続の直後に 1MB にして流すと（送信バッファが大きい環境の代わり）、
    test_a_reply_into_a_full_send_buffer_does_not_stop_receiving が
    『前提: 送信バッファが埋まる』、
    test_piled_up_replies_pause_receiving_and_all_arrive_in_order が
    『前提: 書き出せない応答が上限まで溜まる』（溜まった応答の最大が 3 バイト）
    で落ちた。test_queued_replies_are_not_written_to_the_next_connection は
    通った。応答が送る列に載ったのを見た時点で切断するテストだが、送信
    バッファが埋まらない限り、受信スレッドは積んだ応答をその場で書き出す
    ので、列に載っているのは一瞬だけ（2ms ごとの確認で捕まえられるかは
    速さ次第）。

どう直したか（テスト側だけ。製品のコードは変えない）。_session で、接続の
直後に NetBelt のソケットの SO_SNDBUF を 4096 に明示する。Windows は
SO_SNDBUF を設定したソケットでは動的な送信バッファを使わないので、待たずに
書ける量は環境によらず 16KB ずつで 32,768 バイト、512 バイトずつで 12,288
バイトに収まる（1MB にしたあとで 4096 に戻しても同じ、実測）。貼り付けの
256KB・応答の 90KB / 300KB はどれもその数倍以上あり、埋まったあとの応答は
送る列に残り続ける。

このファイルは、送信バッファが大きい環境を作り（Telnet の接続のソケットの
SO_SNDBUF を接続の直後に 1MB にする）、その中で対象のテストがすべて通る
ことを確かめる。
"""
import importlib.util
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

TARGET = Path(__file__).with_name("test_telnet_negotiation_reply_keeps_reading.py")
TARGET_CLASS = "TelnetNegotiationReplyKeepsReadingTest"
# 送信バッファが大きい環境の代わり（貼り付けの 256KB・応答の 300KB より大きい）
LARGE_SEND_BUFFER = 1024 * 1024


def _run_with_large_send_buffer(name):
    """対象のテストを、Telnet の接続の送信バッファを大きくした状態で流す"""
    from core.telnet_connection import TelnetConnection
    spec = importlib.util.spec_from_file_location(
        "_large_sndbuf_negotiation", TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = TelnetConnection.connect

    def connect_with_large_send_buffer(self):
        ok = original(self)
        if ok and self.socket is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                   LARGE_SEND_BUFFER)
        return ok

    suite = unittest.defaultTestLoader.loadTestsFromName(name, module)
    result = unittest.TestResult()
    with mock.patch.object(TelnetConnection, "connect",
                           connect_with_large_send_buffer):
        suite.run(result)
    return result


class TelnetNegotiationStallOnAnySendBufferTest(unittest.TestCase):
    def test_negotiation_tests_pass_with_a_large_send_buffer(self):
        result = _run_with_large_send_buffer(TARGET_CLASS)

        problems = ["%s\n%s" % (test.id(), trace)
                    for test, trace in result.failures + result.errors]
        if problems:
            self.fail("送信バッファが大きい環境で、詰まりの前提が崩れた:\n\n"
                      + "\n\n".join(problems))
        self.assertEqual(4, result.testsRun)
        self.assertEqual([], result.skipped)


if __name__ == "__main__":
    unittest.main()
