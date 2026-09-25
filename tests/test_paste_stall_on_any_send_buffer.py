"""貼り付けが詰まったときのテストが、OS の送信バッファの大きさによらず詰まりを作れることを検証する。

対象は test_paste_to_slow_device_keeps_session.py（読まない相手へ Telnet で
大きく貼り付け、詰まっている間にタブを閉じる・切断して繋ぎ直す・マクロを
止める・相手が閉じる）。

何が起きていたか。1.3.1 のタグで走ったリリースのワークフロー（GitHub の
windows-latest、英語の Windows・Python 3.11）で、
test_closing_the_tab_while_stalled_stops_the_watcher と
test_reconnecting_after_a_stalled_disconnect_sends_nothing_old が
『前提: 見張りが動いていた』（thread が None）で落ちた。このパソコン
（日本語の Windows・Python 3.12）では通る。製品の不具合ではない。
_stalled_paste は、読まない相手へ 192KB を貼り付けて 0.6 秒回すだけで、
送信が本当に詰まったか（見張りのスレッドが動き出したか）を確かめて
いなかった。詰まるのは、NetBelt のソケットの送信バッファと相手の受信
バッファが埋まったとき。送信バッファは、ソケットに SO_SNDBUF を明示
しない限り Windows が接続ごとに自動で大きさを決める（動的な送信バッファ）
ので、埋まるまでの量も、端末が 0.6 秒で渡せる量も環境で変わる。
  - このパソコンの localhost で、読まない相手（SO_RCVBUF 4096）へ 512 バイト
    ずつ待たずに書ける量: 既定のままで 73,728 バイト、SO_SNDBUF 4096 で
    12,288 バイト、SO_SNDBUF 1MB で 1,056,768 バイト。
  - 直す前の対象ファイルを、NetBelt の Telnet のソケットの SO_SNDBUF を
    接続の直後に 1MB にして流すと（送信バッファが大きい環境の代わり）、
    同じ 2 件が CI と同じ『前提: 見張りが動いていた』で落ちた。

どう直したか（テスト側だけ。製品のコードは変えない）。_stalled_paste で、
接続の直後に NetBelt のソケットの SO_SNDBUF を 4096 に明示する。Windows は
SO_SNDBUF を設定したソケットでは動的な送信バッファを使わないので、送信側に
溜まる量は環境によらず数 KB に収まる（1MB にしたあとで 4096 に戻しても
12,288 バイトで止まる、実測）。相手の受信バッファは、これまでどおり listen の
前に SO_RCVBUF 4096 を設定している。そのうえで、0.6 秒の決め打ちをやめ、
見張りのスレッドが動き出すまで（最大 10 秒）回して、動き出したことを前提と
して確かめてから操作する。前提が崩れたら、黙って通らずにそこで落ちる。

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

TARGET = Path(__file__).with_name("test_paste_to_slow_device_keeps_session.py")
TARGET_CLASS = "PasteToSlowDeviceKeepsSessionTest"
STALLED_TESTS = [
    "test_closing_the_tab_while_stalled_stops_the_watcher",
    "test_reconnecting_after_a_stalled_disconnect_sends_nothing_old",
    "test_stopping_a_macro_queued_behind_a_stalled_paste_sends_none_of_it",
    "test_a_peer_that_closes_during_a_stalled_paste_ends_the_session",
]
# 送信バッファが大きい環境の代わり（貼り付けの 192KB より十分大きい）
LARGE_SEND_BUFFER = 1024 * 1024


def _run_with_large_send_buffer(names):
    """対象のテストを、Telnet の接続の送信バッファを大きくした状態で流す"""
    from core.telnet_connection import TelnetConnection
    spec = importlib.util.spec_from_file_location("_large_sndbuf_target", TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = TelnetConnection.connect

    def connect_with_large_send_buffer(self):
        ok = original(self)
        if ok and self.socket is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF,
                                   LARGE_SEND_BUFFER)
        return ok

    suite = unittest.defaultTestLoader.loadTestsFromNames(
        ["%s.%s" % (TARGET_CLASS, name) for name in names], module)
    result = unittest.TestResult()
    with mock.patch.object(TelnetConnection, "connect",
                           connect_with_large_send_buffer):
        suite.run(result)
    return result


class PasteStallOnAnySendBufferTest(unittest.TestCase):
    def test_stalled_paste_tests_pass_with_a_large_send_buffer(self):
        result = _run_with_large_send_buffer(STALLED_TESTS)

        problems = ["%s\n%s" % (test.id(), trace)
                    for test, trace in result.failures + result.errors]
        if problems:
            self.fail("送信バッファが大きい環境で、詰まりの前提が崩れた:\n\n"
                      + "\n\n".join(problems))
        self.assertEqual(len(STALLED_TESTS), result.testsRun)
        self.assertEqual([], result.skipped)


if __name__ == "__main__":
    unittest.main()
