"""交渉の応答で送信が詰まったときのテストが、OS の送信バッファの大きさによらず詰まりを作れることを検証する。

対象は test_telnet_send_never_waits_for_reply_writer.py（読まない相手が交渉を
連発し、NetBelt の応答で送信バッファが埋まったところで send_command を呼び、
GUI スレッドが受信スレッドの書き込みを待たずに戻ることを確かめる）。

何が起きていたか。1.3.1 のタグで走ったリリースのワークフロー（GitHub の
windows-latest、英語の Windows・Python 3.11）で、
test_send_returns_while_the_reader_cannot_write_its_replies が
『前提: 応答で送信バッファが埋まらない』で落ちた（15 秒待っても、NetBelt の
ソケットが書き込み不可のまま 0.3 秒続かなかった）。このパソコン（日本語の
Windows・Python 3.12）では通る。製品の不具合ではない。応答は 90KB
（IAC DO ECHO 30,000 個への IAC WONT ECHO）で、NetBelt のソケットの送信
バッファと相手の受信バッファ（SO_RCVBUF 4096）の合計より大きいことを
当てにしていた。送信バッファは、ソケットに SO_SNDBUF を明示しない限り
Windows が接続ごとに自動で大きさを決める（動的な送信バッファ）ので、
環境によっては 90KB を丸ごと受け取ってしまう。
  - このパソコンの localhost で、読まない相手（SO_RCVBUF 4096）へ 16KB ずつ
    待たずに書ける量: 既定のままで 65,536 バイト、SO_SNDBUF 4096 で 32,768
    バイト、SO_SNDBUF 1MB で 1,048,576 バイト。既定のままでも 90KB との差は
    1.4 倍しかなかった。
  - 直す前の対象ファイルを、NetBelt の Telnet のソケットの SO_SNDBUF を
    接続の直後に 1MB にして流すと（送信バッファが大きい環境の代わり）、
    CI と同じ『前提: 応答で送信バッファが埋まらない』で落ちた。

どう直したか（テスト側だけ。製品のコードは変えない）。接続の直後に
NetBelt のソケットの SO_SNDBUF を 4096 に明示する。Windows は SO_SNDBUF を
設定したソケットでは動的な送信バッファを使わないので、送信側に溜まる量は
環境によらず数十 KB に収まる（1MB にしたあとで 4096 に戻しても 32,768
バイトで止まる、実測）。あわせて交渉を 100,000 個（応答 300KB）に増やし、
埋まるまでの量との差を 9 倍にした。

このファイルは、送信バッファが大きい環境を作り（Telnet の接続のソケットの
SO_SNDBUF を接続の直後に 1MB にする）、その中で対象のテストが通ることを
確かめる。
"""
import importlib.util
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

TARGET = Path(__file__).with_name(
    "test_telnet_send_never_waits_for_reply_writer.py")
TARGET_TEST = ("TelnetSendNeverWaitsForReplyWriterTest."
               "test_send_returns_while_the_reader_cannot_write_its_replies")
# 送信バッファが大きい環境の代わり（直す前の応答 90KB より十分大きい）
LARGE_SEND_BUFFER = 1024 * 1024


def _run_with_large_send_buffer(name):
    """対象のテストを、Telnet の接続の送信バッファを大きくした状態で流す"""
    from core.telnet_connection import TelnetConnection
    spec = importlib.util.spec_from_file_location("_large_sndbuf_reply", TARGET)
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


class TelnetReplyStallOnAnySendBufferTest(unittest.TestCase):
    def test_reply_stall_test_passes_with_a_large_send_buffer(self):
        result = _run_with_large_send_buffer(TARGET_TEST)

        problems = ["%s\n%s" % (test.id(), trace)
                    for test, trace in result.failures + result.errors]
        if problems:
            self.fail("送信バッファが大きい環境で、詰まりの前提が崩れた:\n\n"
                      + "\n\n".join(problems))
        self.assertEqual(1, result.testsRun)
        self.assertEqual([], result.skipped)


if __name__ == "__main__":
    unittest.main()
