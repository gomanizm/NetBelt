"""Trap 受信機の寿命で、ソケット・スレッド・イベントループが残らないことを検証する。

pysnmp 7.1.28 への移行で、Trap の受信は asyncio のイベントループの上で動く
ようになった（5.1.0 は asyncore）。今の作り:
  - bind()（呼び出し元スレッド）が待ち受けソケットを自分で作って同期で
    バインドし、エンジン・認証情報・observer・NotificationReceiver を用意する
    （イベントループは作らない）。
  - run()（受信スレッド）がこのスレッド専用のループを作り、ディスパッチャと
    トランスポートを載せてソケットを渡し、run_dispatcher() で回す。
  - stop() は回っているループへ call_soon_threadsafe で停止を積む。回り
    始める前なら停止要求を立てるだけにし、bind() だけして start() して
    いない受信機なら、その場でソケットとエンジンを閉じる。
  - run() の finally がディスパッチャ・トランスポート・ソケット・ループを閉じる。

どこかで閉じ損ねると、停止してもポートを掴んだまま次の開始が「ポート N で
待ち受けできません」になる、受信スレッドやループが開始のたびに増える、と
いった形で表に出る。実際の UDP（127.0.0.1 から送る）で次を確かめる。
  (a) ポート使用中（別のソケットが排他で持っている）: bind() が False を返し、
      「ポート N で待ち受けできません」を出し、自分で作ったソケットを残さない。
  (b) 開始直後の停止: 固まらず、stopped が 1 度出て、すぐに同じポートへ別の
      ソケットが排他でバインドできる（＝ソケットが閉じた）。
  (c) 同じポートで停止・再開を 6 回続け、毎回 Trap が届き、終わったあと
      スレッドと開いたループが増えていない。
  (d) 初期化の途中の失敗（bind 側: SnmpEngine・NotificationReceiver の作成、
      run 側: ディスパッチャの作成・add_transport・ソケットを asyncio へ
      渡すところ）: bind() が False、または error_occurred と stopped が出て、
      ソケット・ループ・スレッドが残らない。bind() だけして start() しな
      かった受信機も、stop() でソケットを閉じる。
  (e) 同じ受信機を stop() のあとで start() し直す（run() が受信スレッドの
      中で bind() をやり直す）: 前回の停止要求を引きずらず、毎回 Trap が届く。
  (f) 受信中のポートへ、SO_REUSEADDR を立てた別のソケットが割り込めない
      （排他バインド）。
  (g) 受信中に stop() を 2 回続けて呼ぶ（2 回目がループの止まったあと、
      run() の後始末の前に来る）: 2 つ目の停止がループへ積まれて、後始末の
      run_until_complete を途中で止めない。
ループが閉じたことは受信機が持つループの is_closed() と、プロセス内の開いた
ループの数で、スレッドが残らないことは threading.enumerate() と
QThread.isFinished() で確かめる。
"""
import asyncio
import contextlib
import gc
import io
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

from conftest import free_udp_port, trap_bytes   # noqa: E402

# 待ちの上限。遅い CI のランナーでも足りるよう、実測（開始 0.05 秒・停止
# 0.02 秒ほど）より大きく取る。上限に達したら失敗として扱う
THREAD_WAIT_MS = 10000
SIGNAL_WAIT_S = 5


class _SocketSpy:
    """core.snmp_manager が作るソケットを記録する（socket モジュールの代わり）。

    作ったソケットがあとで閉じたか（fileno() が -1 か）を確かめるために使う。
    snmp_manager の名前空間の socket だけを差し替えるので、asyncio や
    テスト自身が作るソケットは記録しない。
    """

    def __init__(self):
        self.created = []

    def __getattr__(self, name):
        return getattr(socket, name)

    def socket(self, *args, **kwargs):
        sock = socket.socket(*args, **kwargs)
        self.created.append(sock)
        return sock


def _open_loops():
    """プロセス内の、閉じていないイベントループ（id の集合）"""
    return {id(obj) for obj in gc.get_objects()
            if isinstance(obj, asyncio.AbstractEventLoop) and not obj.is_closed()}


def _port_is_free(port):
    """同じポートへ別のソケットが排他でバインドできるか"""
    from core.sockets import set_exclusive_bind
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        set_exclusive_bind(probe)
        probe.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


class SnmpTrapLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作った SNMPManager と受信機はクラス終了まで保持する（test_snmp_trap_receive.py
    # と同じ理由: キューに残ったシグナルの配送先を先に捨てるとプロセスごと落ちる）
    _keep = []

    @classmethod
    def tearDownClass(cls):
        from core.snmp_manager import SNMPManager
        for obj in cls._keep:
            if isinstance(obj, SNMPManager):
                obj.stop_trap_receiver()
            else:
                obj.stop()
                obj.wait(THREAD_WAIT_MS)
        cls.app.processEvents()
        cls._keep.clear()

    def setUp(self):
        fw = mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)
        self.threads_before = set(threading.enumerate())
        self.loops_before = _open_loops()

    # ---- 補助 -------------------------------------------------------------

    def _pump(self, done, seconds=SIGNAL_WAIT_S):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if done():
                return True
            time.sleep(0.01)
        self.app.processEvents()
        return done()

    def _spy_sockets(self):
        import core.snmp_manager as snmp_manager
        spy = _SocketSpy()
        patch = mock.patch.object(snmp_manager, "socket", spy)
        patch.start()
        self.addCleanup(patch.stop)
        return spy

    def _receiver(self, port):
        """受信機を作り、error_occurred / stopped を記録するリストを付ける"""
        from core.snmp_manager import SNMPTrapReceiver
        receiver = SNMPTrapReceiver(port, ["public"])
        self._keep.append(receiver)
        receiver.errors = []
        receiver.stops = []
        receiver.error_occurred.connect(receiver.errors.append)
        receiver.stopped.connect(lambda: receiver.stops.append(True))
        return receiver

    def _assert_all_closed(self, spy):
        self.assertTrue(spy.created, "ソケットを 1 つも作っていない（検査が空振り）")
        for sock in spy.created:
            self.assertEqual(sock.fileno(), -1, "作った待ち受けソケットが閉じていない")

    def _assert_no_new_threads_or_loops(self):
        self.app.processEvents()
        gc.collect()
        new_threads = [t.name for t in threading.enumerate()
                       if t not in self.threads_before]
        self.assertEqual(new_threads, [], "スレッドが残っている")
        self.assertEqual(_open_loops() - self.loops_before, set(),
                         "閉じていないイベントループが残っている")

    def _assert_run_ended_cleanly(self, receiver, port):
        """run() が終わったあと、何も残っていないこと"""
        self.assertTrue(receiver.wait(THREAD_WAIT_MS), "受信スレッドが終わらない")
        self.assertTrue(receiver.isFinished())
        self.assertTrue(self._pump(lambda: receiver.stops), "stopped が出ない")
        self.assertEqual(receiver.stops, [True], "stopped が 1 度ではない")
        self.assertIsNotNone(receiver._loop, "受信スレッドがループを作っていない")
        self.assertTrue(receiver._loop.is_closed(), "イベントループが閉じていない")
        self.assertIsNone(receiver._socket)
        self.assertIsNone(receiver._engine)
        self.assertTrue(_port_is_free(port), "終わったのにポートを掴んだまま")

    # ---- (a) ポート使用中 -------------------------------------------------

    def test_a_port_in_use_is_refused_without_leaving_our_socket(self):
        """他のソケットが排他で持つポートでは、bind() が理由つきで False を返す。"""
        from core.sockets import set_exclusive_bind
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(blocker.close)
        set_exclusive_bind(blocker)
        blocker.bind(("0.0.0.0", 0))
        port = blocker.getsockname()[1]

        spy = self._spy_sockets()
        receiver = self._receiver(port)
        self.assertFalse(receiver.bind(), "使用中のポートで bind() が成功した")

        self.assertEqual(len(receiver.errors), 1, receiver.errors)
        self.assertTrue(
            receiver.errors[0].startswith("ポート %d で待ち受けできません: " % port),
            receiver.errors[0])
        self._assert_all_closed(spy)
        self.assertIsNone(receiver._socket)
        self.assertIsNone(receiver._engine)
        self.assertIsNone(receiver._loop, "bind() がイベントループを作った")
        self._assert_no_new_threads_or_loops()

    # ---- (b) 開始直後の停止 -----------------------------------------------

    def test_stop_right_after_start_releases_the_port(self):
        """開始直後に止めても固まらず、stopped が出て、ポートが空く。

        止めるのがループの準備中か回り始めた直後かで経路が違うので、
        start() から stop() までの間を変えて確かめる。
        """
        for delay in (0, 0.01, 0.05):
            with self.subTest(delay=delay):
                port = free_udp_port()
                spy = self._spy_sockets()
                receiver = self._receiver(port)
                self.assertTrue(receiver.bind(), receiver.errors)
                receiver.start()
                if delay:
                    time.sleep(delay)
                receiver.stop()

                self._assert_run_ended_cleanly(receiver, port)
                self._assert_all_closed(spy)
                self.assertEqual(receiver.errors, [])
        self._assert_no_new_threads_or_loops()

    # ---- (c) 停止・再開の繰り返し -----------------------------------------

    def test_repeated_restarts_on_the_same_port_keep_receiving(self):
        """同じポートで停止・再開を続けても毎回届き、何も増えない。"""
        from core.snmp_manager import SNMPManager
        port = free_udp_port()
        spy = self._spy_sockets()
        manager = SNMPManager()
        self._keep.append(manager)
        got = []
        errors = []
        manager.trap_received.connect(got.append)
        manager.error_occurred.connect(errors.append)

        for cycle in range(6):
            self.assertTrue(manager.start_trap_receiver(port, ["public"]),
                            "%d 回目に同じポートで開始できない: %s" % (cycle + 1, errors))
            receiver = manager.trap_receiver
            before = len(got)
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sender.sendto(trap_bytes("public"), ("127.0.0.1", port))
            finally:
                sender.close()
            self.assertTrue(self._pump(lambda: len(got) > before),
                            "%d 回目の Trap が届かない" % (cycle + 1))

            manager.stop_trap_receiver()
            self.assertTrue(receiver.isFinished(),
                            "%d 回目の停止でスレッドが終わっていない" % (cycle + 1))
            self.assertTrue(receiver._loop.is_closed(),
                            "%d 回目のイベントループが閉じていない" % (cycle + 1))

        self.assertEqual(errors, [])
        self.assertEqual(manager._retired_receivers, [],
                         "止まりきらなかった受信スレッドがある")
        self.assertEqual(len(spy.created), 6, "開始のたびに 1 つのソケットではない")
        self._assert_all_closed(spy)
        self._assert_no_new_threads_or_loops()

    # ---- (d) 初期化の途中の失敗 -------------------------------------------

    def test_a_failure_while_preparing_in_bind_leaves_nothing(self):
        """bind() の途中で落ちたら False を返し、作りかけを残さない。"""
        import core.snmp_manager as snmp_manager
        cases = (
            ("SnmpEngine", mock.patch.object(
                snmp_manager, "SnmpEngine", side_effect=RuntimeError("boom"))),
            ("NotificationReceiver", mock.patch.object(
                snmp_manager.ntfrcv, "NotificationReceiver",
                side_effect=RuntimeError("boom"))),
        )
        for name, patch in cases:
            with self.subTest(failing=name):
                port = free_udp_port()
                spy = self._spy_sockets()
                receiver = self._receiver(port)
                with patch:
                    self.assertFalse(receiver.bind(), "途中で落ちたのに True を返した")

                self.assertEqual(len(receiver.errors), 1, receiver.errors)
                self.assertIn("待ち受けできません", receiver.errors[0])
                self.assertIn("boom", receiver.errors[0])
                self._assert_all_closed(spy)
                self.assertIsNone(receiver._socket)
                self.assertIsNone(receiver._engine)
                self.assertIsNone(receiver._loop, "bind() がイベントループを作った")
                self.assertTrue(_port_is_free(port), "失敗したのにポートを掴んだまま")
        self._assert_no_new_threads_or_loops()

    def test_a_failure_while_starting_in_run_leaves_nothing(self):
        """run() の準備の途中で落ちたら、error_occurred と stopped を出して片付ける。"""
        import core.snmp_manager as snmp_manager
        cases = (
            ("AsyncioDispatcher", lambda receiver: mock.patch.object(
                snmp_manager, "AsyncioDispatcher", side_effect=RuntimeError("boom"))),
            ("add_transport", lambda receiver: mock.patch.object(
                snmp_manager.config, "add_transport", side_effect=RuntimeError("boom"))),
            # asyncio へ渡すソケットが使えなくなっている（create_datagram_endpoint
            # が失敗する）。pysnmp の open_server_mode はこの失敗を黙って捨てる
            ("create_datagram_endpoint", lambda receiver: _ClosedBeforeStart(receiver)),
        )
        for name, make_patch in cases:
            with self.subTest(failing=name):
                port = free_udp_port()
                spy = self._spy_sockets()
                receiver = self._receiver(port)
                self.assertTrue(receiver.bind(), receiver.errors)
                with make_patch(receiver):
                    receiver.start()
                    self.assertTrue(receiver.wait(THREAD_WAIT_MS),
                                    "準備の途中で落ちた受信スレッドが終わらない")

                self._assert_run_ended_cleanly(receiver, port)
                self.assertEqual(len(receiver.errors), 1, receiver.errors)
                self.assertTrue(receiver.errors[0].startswith("Trap受信エラー: "),
                                receiver.errors[0])
                self._assert_all_closed(spy)
        self._assert_no_new_threads_or_loops()

    def test_a_bound_but_never_started_receiver_releases_the_port_on_stop(self):
        """bind() だけして start() しなかった受信機も、stop() でソケットを閉じる。"""
        port = free_udp_port()
        spy = self._spy_sockets()
        receiver = self._receiver(port)
        self.assertTrue(receiver.bind(), receiver.errors)
        self.assertFalse(_port_is_free(port), "bind() したのにポートを掴んでいない")

        receiver.stop()

        self._assert_all_closed(spy)
        self.assertIsNone(receiver._socket)
        self.assertIsNone(receiver._engine)
        self.assertTrue(_port_is_free(port), "stop() してもポートを掴んだまま")
        self.assertFalse(receiver.isRunning())
        self._assert_no_new_threads_or_loops()

    def test_binding_twice_does_not_leave_the_first_socket(self):
        """start() の前にもう一度 bind() しても、前のソケットを残さない。"""
        port = free_udp_port()
        spy = self._spy_sockets()
        receiver = self._receiver(port)
        self.assertTrue(receiver.bind(), receiver.errors)
        self.assertTrue(receiver.bind(), "同じポートでの 2 度目の bind() が失敗した: %s"
                        % receiver.errors)
        self.assertEqual(len(spy.created), 2)
        self.assertEqual(spy.created[0].fileno(), -1, "前のソケットが閉じていない")

        receiver.stop()
        self._assert_all_closed(spy)
        self.assertTrue(_port_is_free(port))

    # ---- (e) 同じ受信機の再 start -----------------------------------------

    def test_the_same_receiver_receives_again_after_stop_and_start(self):
        """stop() のあと同じ受信機を start() し直しても、前回の停止要求で即終了しない。

        run() の finally がソケットとエンジンを閉じるので、2 回目からは run() が
        受信スレッドの中で bind() をやり直す。停止要求が残っていると、
        ループを回す前に終わって何も受け取らない。
        """
        port = free_udp_port()
        spy = self._spy_sockets()
        receiver = self._receiver(port)
        got, started = [], []
        receiver.trap_received.connect(got.append)
        receiver.started.connect(lambda: started.append(True))
        self.assertTrue(receiver.bind(), receiver.errors)

        for cycle in range(3):
            traps_before, starts_before = len(got), len(started)
            receiver.start()
            self.assertTrue(self._pump(lambda: len(started) > starts_before),
                            "%d 回目の開始が知らされない" % (cycle + 1))
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sender.sendto(trap_bytes("public"), ("127.0.0.1", port))
            finally:
                sender.close()
            self.assertTrue(self._pump(lambda: len(got) > traps_before),
                            "%d 回目の Trap が届かない" % (cycle + 1))
            receiver.stop()
            self.assertTrue(receiver.wait(THREAD_WAIT_MS),
                            "%d 回目の停止でスレッドが終わらない" % (cycle + 1))
            self.assertTrue(receiver._loop.is_closed())
            self.assertTrue(_port_is_free(port),
                            "%d 回目の停止のあとポートを掴んだまま" % (cycle + 1))

        self.assertTrue(self._pump(lambda: len(receiver.stops) == 3))
        self.assertEqual(receiver.stops, [True] * 3)
        self.assertEqual(receiver.errors, [])
        self.assertEqual(len(spy.created), 3, "開始のたびに 1 つのソケットではない")
        self._assert_all_closed(spy)
        self._assert_no_new_threads_or_loops()

    # ---- (f) 排他バインド -------------------------------------------------

    def test_the_listening_port_cannot_be_taken_over_while_receiving(self):
        """受信中のポートへ、SO_REUSEADDR を立てた別のソケットがバインドできないこと。

        Windows の SO_REUSEADDR は、排他でない待ち受けポートへの二重バインドを
        許す。割り込まれると Trap が相手のソケットへ流れる。
        """
        port = free_udp_port()
        receiver = self._receiver(port)
        started = []
        receiver.started.connect(lambda: started.append(True))
        self.assertTrue(receiver.bind(), receiver.errors)
        receiver.start()
        self.assertTrue(self._pump(lambda: started), "開始が知らされない")

        for address in ("0.0.0.0", "127.0.0.1"):
            with self.subTest(address=address):
                thief = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    thief.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    with self.assertRaises(OSError):
                        thief.bind((address, port))
                finally:
                    thief.close()

        receiver.stop()
        self._assert_run_ended_cleanly(receiver, port)
        self._assert_no_new_threads_or_loops()

    # ---- (g) 受信中の 2 回の停止 -------------------------------------------

    def test_a_second_stop_does_not_cut_the_cleanup_short(self):
        """受信中に stop() を 2 回呼んでも、run() の後始末が最後まで回る。

        1 回目の停止でループが止まってから run() の finally に入るまでの間に
        2 回目が来ると、直す前は loop.stop がもう 1 つ積まれ、後始末の
        run_until_complete がそれで止まって「イベントループの後始末に失敗
        しました」になり、残りの片付けが飛んだ（実測）。その間を確実に
        作るため、ディスパッチャの run_dispatcher が戻ったところで待たせる。
        """
        import core.snmp_manager as snmp_manager
        entered = threading.Event()
        returned = threading.Event()
        proceed = threading.Event()
        self.addCleanup(proceed.set)

        class _PausingDispatcher(snmp_manager.AsyncioDispatcher):
            def run_dispatcher(self, *args, **kwargs):
                entered.set()
                try:
                    return super().run_dispatcher(*args, **kwargs)
                finally:
                    returned.set()
                    proceed.wait(THREAD_WAIT_MS / 1000)

        patch = mock.patch.object(snmp_manager, "AsyncioDispatcher",
                                  _PausingDispatcher)
        patch.start()
        self.addCleanup(patch.stop)
        port = free_udp_port()
        spy = self._spy_sockets()
        receiver = self._receiver(port)
        started = []
        receiver.started.connect(lambda: started.append(True))
        self.assertTrue(receiver.bind(), receiver.errors)
        receiver.start()
        self.assertTrue(self._pump(lambda: started), "開始が知らされない")
        # started は受信ループに入る前に出る。その間に停止すると、run() は
        # ループを回さずに終わり、このテストの狙う経路（回っているループを
        # 2 回止める）に入らない。ループに入ったことを待ってから止める
        self.assertTrue(entered.wait(SIGNAL_WAIT_S), "受信ループに入らない")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            receiver.stop()
            self.assertTrue(returned.wait(SIGNAL_WAIT_S),
                            "1 回目の停止でループが止まらない")
            receiver.stop()     # ループは止まり、run() の後始末はまだ
            proceed.set()
            self.assertTrue(receiver.wait(THREAD_WAIT_MS),
                            "受信スレッドが終わらない")
        self.assertNotIn("後始末に失敗", output.getvalue())
        self._assert_run_ended_cleanly(receiver, port)
        self._assert_all_closed(spy)
        self.assertEqual(receiver.errors, [])
        self._assert_no_new_threads_or_loops()


class _ClosedBeforeStart:
    """start() の直前に、受信機が bind() で作ったソケットを閉じる（with で使う）"""

    def __init__(self, receiver):
        self.receiver = receiver

    def __enter__(self):
        self.receiver._socket.close()
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
