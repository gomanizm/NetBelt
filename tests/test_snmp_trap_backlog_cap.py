"""Trap の配送待ちに上限があり、あふれた分を数えて知らせることを検証する。

何が起きていたか（9fee4af、R06）: 受信スレッドは Trap を 1 件受けるたびに
dict を作り、queued シグナルで GUI へ送っていた。保持件数の上限
（max_traps、既定 1000）が効くのは GUI が一覧へ入れた後だけで、その手前の
Qt のキューには上限が無い。GUI が止まっている間は配送 0 のまま溜まり続け
（実測: 15,999 件、普通の Trap で毎秒 5〜7 MB、細工した 60 KB の値で最大
約 50 MB/秒）、再開すると全件が流れ込む。Syslog・FTP・TFTP・SFTP には
同じ形の上限がすでにある。

直し方: Syslog と同じく、配送待ちの件数を数えて上限 max(1000, max_traps)
を超えたら新しく届いた方を捨てて数える。数え役は受信の開始ごとに
SNMPManager が作って受信機へ渡す（受信機は開始のたびに作り直す QThread
なので、数えるのは長生きする側）。捨てた件数は SNMPManager.trap_dropped で
パネルへ知らせ、あふれ始めと捌けたあとの要約をログ（print）に書く。

ここでは GUI を止めた状態を「イベントループを回さない」で作り、受信
スレッドと同じく GUI 以外のスレッドから通知コールバックを呼んで投入する。
最後に実際の UDP（127.0.0.1）での往復も確かめる。
"""
import contextlib
import io
import os
import re
import socket
import sys
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, "src")

from conftest import free_udp_port, trap_bytes   # noqa: E402

CAP = 1000   # 既定の上限（max_traps が 1000 以下のとき）


class _Value:
    """pysnmp の OID・値の代わり（_build_trap_data が使う prettyPrint だけ持つ）"""

    def __init__(self, text):
        self._text = text

    def prettyPrint(self):
        return self._text


def _var_binds(n):
    return [(_Value("1.3.6.1.2.1.1.3.0"), _Value(str(n))),
            (_Value("1.3.6.1.6.3.1.1.4.1.0"), _Value("1.3.6.1.6.3.1.1.5.3")),
            (_Value("1.3.6.1.2.1.2.2.1.1.%d" % n), _Value("trap-%05d" % n))]


def _inject(receiver, start, count):
    """受信スレッドと同じく、GUI 以外のスレッドから通知コールバックを呼ぶ"""
    def run():
        for n in range(start, start + count):
            receiver._on_notification(None, None, None, None, _var_binds(n),
                                      None)
    worker = threading.Thread(target=run)
    worker.start()
    worker.join()


def _value(trap):
    return trap["varbinds"][-1]["value"]


class _Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作った SNMPManager・パネルはクラス終了まで保持する（test_snmp_trap_receive
    # と同じ理由: キューに残ったシグナルの配送先を先に捨てると落ちる）
    _keep = []

    @classmethod
    def tearDownClass(cls):
        from core.snmp_manager import SNMPManager
        for obj in cls._keep:
            if isinstance(obj, SNMPManager):
                obj.stop_trap_receiver()
        cls.app.processEvents()
        cls._keep.clear()

    def setUp(self):
        fw = mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)
        # SNMPManager.trap_received で届いた Trap（GUI が受け取った分）
        self.got = []

    def _manager(self):
        from core.snmp_manager import SNMPManager
        manager = SNMPManager()
        self._keep.append(manager)
        self.addCleanup(manager.stop_trap_receiver)
        manager.trap_received.connect(self.got.append)
        return manager

    def _start(self, manager, **kwargs):
        self.assertTrue(manager.start_trap_receiver(free_udp_port(), ["public"],
                                                    **kwargs))
        return manager.trap_receiver

    def _drain(self, seconds=10.0):
        """配送待ちが無くなるまでイベントループを回す（GUI の再開）"""
        deadline = time.monotonic() + seconds
        idle = 0
        while time.monotonic() < deadline and idle < 3:
            before = len(self.got)
            self.app.processEvents()
            idle = idle + 1 if len(self.got) == before else 0
            time.sleep(0.01)

    def _spy_drops(self, manager):
        drops = []
        manager.trap_dropped.connect(
            lambda count, limit, last: drops.append((count, limit, last)))
        return drops


class TrapBacklogCapTest(_Case):

    def test_a_stalled_gui_receives_at_most_the_cap(self):
        """GUI が止まっている間に上限を超えて届いても、配送待ちは上限で止まる。

        捨てるのは新しく届いた方（Syslog と同じ）。嵐の始まりの Trap が残る。
        """
        manager = self._manager()
        receiver = self._start(manager)

        _inject(receiver, 0, CAP + 500)
        self.assertEqual(self.got, [], "イベントループを回していないのに届いた")
        self._drain()
        self.assertEqual(len(self.got), CAP,
                         "配送待ちが上限を超えて積み上がった: %d 件"
                         % len(self.got))
        self.assertEqual(_value(self.got[0]), "trap-00000")
        self.assertEqual(_value(self.got[-1]), "trap-%05d" % (CAP - 1))

    def test_the_dropped_count_adds_up(self):
        manager = self._manager()
        receiver = self._start(manager)
        drops = self._spy_drops(manager)

        _inject(receiver, 0, CAP + 500)
        self._drain()
        self.assertEqual(sum(d[0] for d in drops), 500)
        self.assertEqual(len(self.got) + sum(d[0] for d in drops), CAP + 500)
        self.assertTrue(all(d[1] == CAP for d in drops), drops)
        self.assertTrue(all(isinstance(d[2], datetime) for d in drops), drops)

    def test_overflow_is_logged_when_it_starts_and_summarised_once_drained(self):
        manager = self._manager()
        receiver = self._start(manager)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _inject(receiver, 0, CAP + 500)
            self._drain()
            _inject(receiver, 10000, CAP + 200)
            self._drain()
        lines = out.getvalue().splitlines()
        started = [line for line in lines if "reached its limit" in line]
        drained = [line for line in lines if "backlog drained" in line]
        self.assertEqual(len(started), 2, lines)
        self.assertEqual(len(drained), 2, lines)
        self.assertIn("dropped 500 trap(s)", drained[0])
        self.assertIn("dropped 200 trap(s)", drained[1])
        self.assertRegex(drained[0],
                         r"between \d\d:\d\d:\d\d and \d\d:\d\d:\d\d")
        self.assertIn("limit %d" % CAP, drained[0])

    def test_an_overflow_while_draining_is_summarised_once_at_the_end(self):
        """捌いている途中にもう一度あふれても、要約は捌け切ったときに 1 回だけ。

        最初の配送で要約すると、上限付近を行き来する嵐で「reached its limit」と
        要約の行が配送のたびに出て、ログがあふれる。
        """
        manager = self._manager()
        receiver = self._start(manager)
        got = self.got
        summary_at = []   # 要約の行を書いた時点で GUI に届いていた件数

        class _Tee(io.StringIO):
            def write(self, s):
                if "backlog drained" in s:
                    summary_at.append(len(got))
                return super().write(s)

        def overflow_again(_trap):
            if len(got) == 10:   # 10 件捌いたところで、もう一度あふれさせる
                _inject(receiver, 50000, 500)
        manager.trap_received.connect(overflow_again)

        out = _Tee()
        with contextlib.redirect_stdout(out):
            _inject(receiver, 0, CAP + 500)
            self._drain()
        lines = out.getvalue().splitlines()
        started = [line for line in lines if "reached its limit" in line]
        drained = [line for line in lines if "backlog drained" in line]
        # 2 回目は空いた 10 件ぶんだけ受けて、残りの 490 件を捨てる
        self.assertEqual(len(got), CAP + 10)
        self.assertEqual(len(started), 1, lines)
        self.assertEqual(len(drained), 1, lines)
        self.assertIn("dropped 990 trap(s)", drained[0])
        self.assertEqual(summary_at, [CAP + 10], "捌け切る前に要約した")

    def test_traffic_that_keeps_up_is_never_dropped(self):
        """配送待ちが上限に届かなければ、合計が上限を超えても 1 件も捨てない。"""
        manager = self._manager()
        receiver = self._start(manager)
        drops = self._spy_drops(manager)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for burst in range(3):
                _inject(receiver, burst * 1000, 600)
                self._drain()
        self.assertEqual(len(self.got), 1800)
        self.assertEqual(drops, [])
        self.assertNotIn("backlog", out.getvalue())

    def test_the_cap_is_never_below_max_traps(self):
        """max_traps を大きくしている利用者に、新しく取りこぼしを出さない。"""
        for keep, cap in ((1500, 1500), (20, CAP)):
            with self.subTest(keep_traps=keep):
                del self.got[:]
                manager = self._manager()
                receiver = self._start(manager, keep_traps=keep)
                drops = self._spy_drops(manager)
                _inject(receiver, 0, cap + 100)
                self._drain()
                self.assertEqual(len(self.got), cap)
                self.assertEqual(sum(d[0] for d in drops), 100)
                self.assertTrue(all(d[1] == cap for d in drops), drops)

    def test_a_restart_does_not_mix_the_counts(self):
        """止めて開き直しても、前の回の配送待ちが新しい回の上限と件数に入らない。"""
        manager = self._manager()
        first = self._start(manager)
        drops = self._spy_drops(manager)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _inject(first, 0, CAP + 500)       # 前の回: 1000 件が配送待ち
            manager.stop_trap_receiver()       # GUI は止まったまま
            second = self._start(manager)
            _inject(second, 20000, CAP + 100)  # 新しい回も 1000 件まで受ける
            self._drain()
        self.assertEqual(len(self.got), 2 * CAP)
        self.assertEqual(
            sum(1 for t in self.got if _value(t) >= "trap-20000"), CAP,
            "前の回の配送待ちが、新しい回の上限を食った")
        # パネルへ知らせるのは今の回のぶんだけ（開始で 0 に戻した表示が狂わない）
        self.assertEqual(sum(d[0] for d in drops), 100)
        # 前の回のぶんもログには残る
        drained = re.findall(r"backlog drained: dropped (\d+) trap",
                             out.getvalue())
        self.assertEqual(sorted(drained), ["100", "500"])


class TrapBacklogUdpTest(_Case):
    """実際の UDP（127.0.0.1）で数件送る往復。"""

    def _send(self, port, count):
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _ in range(count):
                sender.sendto(trap_bytes("public"), ("127.0.0.1", port))
        finally:
            sender.close()

    def _start_udp(self, manager):
        # 待ち受けは bind() の中で同期に済むので、すぐ送ってよい
        port = free_udp_port()
        self.assertTrue(manager.start_trap_receiver(port, ["public"]))
        return port

    def test_a_few_real_traps_all_arrive(self):
        manager = self._manager()
        port = self._start_udp(manager)
        drops = self._spy_drops(manager)
        self._send(port, 5)
        deadline = time.monotonic() + 5
        while len(self.got) < 5 and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(len(self.got), 5)
        self.assertEqual(drops, [])
        self.assertEqual(self.got[0]["source_ip"], "127.0.0.1")

    def test_real_traps_past_a_small_cap_are_counted(self):
        manager = self._manager()
        manager.DEFAULT_MAX_PENDING_TRAPS = 3   # この受信機だけ小さくする
        port = self._start_udp(manager)
        drops = self._spy_drops(manager)
        self._send(port, 10)
        backlog = manager._trap_backlog
        deadline = time.monotonic() + 5
        while (backlog.pending + backlog.dropped < 10
               and time.monotonic() < deadline):
            time.sleep(0.01)    # GUI は止めたまま、受信スレッドの処理を待つ
        self.assertEqual(self.got, [])
        self._drain()
        self.assertEqual(len(self.got), 3)
        self.assertEqual(sum(d[0] for d in drops), 7)


class _WatchedLock:
    """持っている間だけ held が真になる錠（数に触るのが錠の中かを見る）"""

    def __init__(self):
        self._lock = threading.Lock()
        self.held = False

    def __enter__(self):
        self._lock.acquire()
        self.held = True
        return self

    def __exit__(self, *exc):
        self.held = False
        self._lock.release()


class TrapBacklogLockTest(unittest.TestCase):
    """数え役の数は、受信スレッド（take）と GUI スレッド（delivered）の両方が
    触るので、錠の中だけで読み書きする。要約の区切り（件数の取り出しと 0 戻し）
    を錠の外へ出すと、その隙に捨てた分が要約から漏れる。競合を待たずに、
    錠の外で触ったかどうかを直接見る。
    """

    COUNTS = ("pending", "dropped", "_reported", "_burst", "_burst_first",
              "_burst_last")

    def test_the_counts_are_only_touched_under_the_lock(self):
        from core.snmp_manager import _TrapBacklog
        outside = []

        def watched(name):
            key = "_watched_" + name

            def check(self):
                if self._watching and not self._lock.held:
                    outside.append(name)

            def get(self):
                check(self)
                return self.__dict__[key]

            def put(self, value):
                check(self)
                self.__dict__[key] = value
            return property(get, put)

        attrs = {name: watched(name) for name in self.COUNTS}
        attrs["_watching"] = False   # 作る間（__init__）は見ない
        backlog = type("WatchedBacklog", (_TrapBacklog,), attrs)(2)
        backlog._lock = _WatchedLock()
        backlog._watching = True

        with contextlib.redirect_stdout(io.StringIO()):
            taken = [backlog.take() for _ in range(4)]   # 2 件受けて 2 件捨てる
            results = [backlog.delivered() for _ in range(2)]
        self.assertEqual(taken, [True, True, False, False])
        self.assertEqual(results[0][0], 2)            # パネルへ知らせる件数
        self.assertIsNone(results[0][2])              # まだ 1 件配送待ち
        self.assertEqual(results[-1][2][0], 2)        # 捌け切ったときの要約
        self.assertEqual(outside, [], "錠の外で数に触った: %s" % outside)


if __name__ == "__main__":
    unittest.main()
