"""Trap の取りこぼしの件数が、停止・クリア・開き直し・終了でログから消えないことを検証する。

何が起きていたか（82f4165）: 配送待ちの上限で捨てた件数の要約
（「Trap backlog drained: dropped N trap(s) …」）は、配送待ちが捌け切った
ときにだけ書いていた。捌け切るのは、受信スレッドが Qt のキューへ積んだ配送を
GUI が全部配ったとき。更新の適用（quit_for_update）は窓を閉じたあと
QApplication.quit() で終わり、その時点のキューを配らない。あふれている最中に
更新を当てると、ログには「reached its limit」（あふれ始め）だけが残り、捨てた
件数はどこにも残らなかった（実測: 実物の MainWindow で配送待ち 1000 件・捨てた
500 件のまま終了し、要約の行が出なかった）。×で閉じる経路は、最後の窓が
閉じて積まれる Quit より前に配送が配られるので残っていた。

直し方: 停止（stop_trap_receiver）と、停止を通らずに受信スレッドが終わった
後の開始（start_trap_receiver）で、まだ要約していない分を捌け切るのを待たずに
「Reception stopped before the Trap backlog drained: dropped N trap(s) …」と書く。
配送待ちはそのあとも届き、パネルの件数はこれまでどおり配送で足す。捌け切った
ときの要約はもう書かない（同じ取りこぼしを二度数えない）。

確かめたが直す必要の無かった経路（ここでは結果を守る）:
- 停止ボタン: 受信機（QThread）の参照が消えて破棄されても、キューに残った
  配送は届き、パネルの件数も足される。
- クリア: 管理側の数え役に触らないので、要約はクリアをまたいだ件数で出る。
  パネルの表示はクリアで 0 に戻り、その後に知らせた分から数え直す。
- 受信中の開始: 断られ、数え役も表示もそのまま。
- 捌け切る配送の中で停止: 要約はその配送で取り出し済みなので、1 回だけ出る。
"""
import contextlib
import gc
import io
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import weakref
from unittest import mock

sys.path.insert(0, "src")

from conftest import free_udp_port   # noqa: E402

CAP = 1000   # 既定の上限（max_traps が 1000 以下のとき）
# 捨てた件数の要約。捌け切ったとき・止めたときのどちらの行にも当たる
SUMMARY = re.compile(r"Trap backlog drained: dropped (\d+) trap")
ON_STOP = re.compile(
    r"Reception stopped before the Trap backlog drained: dropped (\d+) trap")


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


def _inject(manager, start, count):
    """受信スレッドと同じく、GUI 以外のスレッドから通知コールバックを呼ぶ

    受信機への参照は呼び終わったら手放す（停止で受信機が破棄される本番と同じ形）。
    """
    notify = manager.trap_receiver._on_notification

    def run():
        for n in range(start, start + count):
            notify(None, None, None, None, _var_binds(n), None)
    worker = threading.Thread(target=run)
    worker.start()
    worker.join()


class _Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作った SNMPManager・パネルはクラス終了まで保持する（キューに残った
    # シグナルの配送先を先に捨てると落ちる。test_snmp_trap_backlog_cap と同じ）
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
        self.got = []      # GUI が受け取った Trap
        self.drops = []    # trap_dropped で知らせた件数

    def _manager(self):
        from core.snmp_manager import SNMPManager
        manager = SNMPManager()
        self._keep.append(manager)
        self.addCleanup(manager.stop_trap_receiver)
        manager.trap_received.connect(self.got.append)
        manager.trap_dropped.connect(
            lambda count, limit, last: self.drops.append(count))
        return manager

    def _start(self, manager):
        self.assertTrue(manager.start_trap_receiver(free_udp_port(), ["public"]))

    def _drain(self, seconds=10.0):
        """配送待ちが無くなるまでイベントループを回す（GUI の再開）"""
        deadline = time.monotonic() + seconds
        idle = 0
        while time.monotonic() < deadline and idle < 3:
            before = len(self.got)
            self.app.processEvents()
            idle = idle + 1 if len(self.got) == before else 0
            time.sleep(0.01)


class TrapDropsLoggedOnStopTest(_Case):

    def test_stopping_mid_overflow_logs_the_count_at_once(self):
        """あふれている最中に止めると、その場で件数を書く（配送を待たない）。"""
        manager = self._manager()
        self._start(manager)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)   # GUI は止めたまま
            manager.stop_trap_receiver()
            at_stop = out.getvalue()
            self._drain()
        self.assertEqual(ON_STOP.findall(at_stop), ["500"],
                         "止めた時点で取りこぼしの件数がログに無い:\n" + at_stop)
        # 配送待ちはそのあとも届き、パネルへも知らせる。要約は二度書かない
        self.assertEqual(len(self.got), CAP)
        self.assertEqual(sum(self.drops), 500)
        self.assertEqual(SUMMARY.findall(out.getvalue()), ["500"],
                         out.getvalue())

    def test_traps_dropped_while_stopping_are_in_the_count(self):
        """停止を求めてから受信スレッドが終わるまでに捨てた分も、停止の件数に入る。

        要約は受信スレッドの終わり（wait）を待ってから書く。停止を求める前や
        wait の前に書くと、その間に捨てた分は新しいあふれとして残り、捌け切るか
        次の開始まで書かれない（更新の適用で終わると残らない）。
        """
        manager = self._manager()
        self._start(manager)
        receiver = manager.trap_receiver
        real_wait = receiver.wait

        def wait_while_traps_arrive(*args):
            # 停止を求めた後、受信ループが止まりきる前に届いた分
            # （配送待ちは満杯なので捨てる）
            _inject(manager, CAP + 500, 200)
            return real_wait(*args)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)   # GUI は止めたまま
            with mock.patch.object(receiver, "wait",
                                   side_effect=wait_while_traps_arrive):
                manager.stop_trap_receiver()
            at_stop = out.getvalue()
            del receiver, real_wait
            self._drain()
        self.assertEqual(ON_STOP.findall(at_stop), ["700"],
                         "停止中に捨てた分が停止の件数に入っていない:\n" + at_stop)
        self.assertEqual(len(self.got), CAP)
        self.assertEqual(sum(self.drops), 700)
        self.assertEqual(SUMMARY.findall(out.getvalue()), ["700"],
                         out.getvalue())

    def test_stop_logs_the_count_even_if_the_thread_outlives_the_wait(self):
        """受信スレッドが 5 秒で止まらなかった（wait が False）停止でも、その場で件数を書く。

        止まりきったときだけ書くと、そのあと更新の適用で終わったとき、
        それまでに捨てた件数がログに残らない。
        """
        manager = self._manager()
        self._start(manager)
        receiver = manager.trap_receiver
        real_wait = receiver.wait

        def wait_gives_up(*args):
            real_wait(*args)   # 実際には止める（スレッドを残さない）
            return False       # 5 秒で止まらなかった扱い

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)   # GUI は止めたまま
            with mock.patch.object(receiver, "wait", side_effect=wait_gives_up):
                manager.stop_trap_receiver()
            at_stop = out.getvalue()
            del receiver, real_wait
            self._drain()
        self.assertIn("5秒以内に終了しませんでした", at_stop)
        self.assertEqual(ON_STOP.findall(at_stop), ["500"],
                         "止まりきらなかった停止で件数がログに無い:\n" + at_stop)
        # 実際には終わっているので、保持リストからは外れている
        self.assertEqual(manager._retired_receivers, [])
        self.assertEqual(len(self.got), CAP)
        self.assertEqual(sum(self.drops), 500)
        self.assertEqual(SUMMARY.findall(out.getvalue()), ["500"],
                         out.getvalue())

    def test_stop_logs_the_count_while_the_thread_is_still_running(self):
        """受信スレッドが本当に動いたまま wait が諦めた停止でも、その場で件数を書く。

        上のテストは wait の中で実際に止めるので、止まりきらなかった受信機が
        後で外れるときまで要約を先送りする形と区別できない（検査役の指摘）。
        ここでは停止の要求も届けず、スレッドが動いたままの状態で確かめる。
        """
        manager = self._manager()
        self._start(manager)
        receiver = manager.trap_receiver
        real_stop, real_wait = receiver.stop, receiver.wait
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)   # GUI は止めたまま
            # 停止の要求を届けず、wait も諦めた扱い（スレッドは本当に動いたまま）
            with mock.patch.object(receiver, "stop"), \
                    mock.patch.object(receiver, "wait", return_value=False):
                manager.stop_trap_receiver()
            at_stop = out.getvalue()
            still_running = receiver.isRunning()
            retired = list(manager._retired_receivers)
            real_stop()
            self.assertTrue(real_wait(5000))
            del receiver, real_stop, real_wait
            self._drain()
        self.assertTrue(still_running)
        self.assertEqual(len(retired), 1)
        self.assertEqual(ON_STOP.findall(at_stop), ["500"],
                         "動いたままの停止で件数がログに無い:\n" + at_stop)
        self.assertEqual(manager._retired_receivers, [])
        self.assertEqual(len(self.got), CAP)
        self.assertEqual(SUMMARY.findall(out.getvalue()), ["500"],
                         out.getvalue())

    def test_stopping_part_way_through_draining_logs_the_rest_once(self):
        manager = self._manager()
        self._start(manager)
        out = io.StringIO()
        at_stop = []

        def stop_at_300(_trap):
            if len(self.got) == 300 and not at_stop:
                manager.stop_trap_receiver()
                at_stop.append(out.getvalue())
        manager.trap_received.connect(stop_at_300)

        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)
            self._drain()
        self.assertEqual(len(at_stop), 1)
        self.assertEqual(ON_STOP.findall(at_stop[0]), ["500"], at_stop[0])
        self.assertEqual(len(self.got), CAP)
        self.assertEqual(sum(self.drops), 500)
        self.assertEqual(SUMMARY.findall(out.getvalue()), ["500"],
                         out.getvalue())

    def test_stopping_as_the_backlog_drains_logs_it_once(self):
        """捌け切った配送の中（要約を書く前）で止めても、要約は 1 回だけ。"""
        manager = self._manager()
        self._start(manager)
        out = io.StringIO()
        stopped = []

        def stop_when_drained(_trap):
            if manager._trap_backlog.pending == 0 and not stopped:
                stopped.append(True)
                manager.stop_trap_receiver()
        manager.trap_received.connect(stop_when_drained)

        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)
            self._drain()
        text = out.getvalue()
        self.assertEqual(stopped, [True])
        self.assertEqual(SUMMARY.findall(text), ["500"], text)
        self.assertEqual(ON_STOP.findall(text), [], text)
        self.assertEqual(sum(self.drops), 500)

    def test_starting_after_the_receiver_died_logs_the_old_count(self):
        """停止を通らずに受信スレッドが終わった回は、開き直す前に件数を書く。

        前の回の配送待ちはそのあとも届くが、開始で 0 に戻したパネルの表示へは
        足さない（これまでどおり）。
        """
        manager = self._manager()
        self._start(manager)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)
            dead = manager.trap_receiver   # 受信スレッドだけが自分で終わった形
            dead.stop()
            self.assertTrue(dead.wait(5000))
            del dead
            self._start(manager)
            at_start = out.getvalue()
            self._drain()
        self.assertEqual(ON_STOP.findall(at_start), ["500"], at_start)
        self.assertEqual(len(self.got), CAP)
        self.assertEqual(self.drops, [])
        self.assertEqual(SUMMARY.findall(out.getvalue()), ["500"],
                         out.getvalue())

    def test_starting_while_receiving_changes_nothing(self):
        manager = self._manager()
        self._start(manager)
        backlog = manager._trap_backlog
        refused = []
        manager.error_occurred.connect(refused.append)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)
            self.assertFalse(manager.start_trap_receiver(free_udp_port(),
                                                         ["public"]))
            at_refusal = out.getvalue()
            self._drain()
        self.assertEqual(refused, ["既にTrap受信が実行中です"])
        self.assertIs(manager._trap_backlog, backlog)
        self.assertEqual(SUMMARY.findall(at_refusal), [], at_refusal)
        self.assertEqual(SUMMARY.findall(out.getvalue()), ["500"])
        self.assertEqual(sum(self.drops), 500)


class TrapDropsPanelTest(_Case):

    def _panel(self):
        from ui.snmp_panel import SNMPPanel
        with mock.patch.object(SNMPPanel, "_start_background_mib_loading"):
            panel = SNMPPanel(config_manager=None)
        self._keep.append(panel)
        panel.mib_loading = False
        panel.mib_loaded = True
        manager = self._manager()
        panel.set_snmp_manager(manager)
        panel.trap_port_spinbox.setValue(free_udp_port())
        with mock.patch("ui.snmp_panel.QMessageBox.warning") as warn:
            panel._on_trap_start_clicked()
        warn.assert_not_called()
        return panel, manager

    def test_the_stop_button_keeps_the_count_and_the_queued_traps(self):
        """停止ボタンで受信機が破棄されても、残った配送は届いてパネルにも出る。"""
        panel, manager = self._panel()
        receiver = weakref.ref(manager.trap_receiver)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)
            panel._on_trap_stop_clicked()
            gc.collect()
            self.assertIsNone(receiver(), "前提: 停止で受信機を手放している")
            self.assertEqual(ON_STOP.findall(out.getvalue()), ["500"])
            self._drain()
        self.assertEqual(len(panel.trap_data_list), CAP)
        self.assertFalse(panel.trap_drop_label.isHidden())
        self.assertIn("取りこぼし: 500 件", panel.trap_drop_label.text())
        self.assertEqual(SUMMARY.findall(out.getvalue()), ["500"])

    def test_clearing_mid_overflow_keeps_the_count_in_the_log(self):
        """クリアはパネルの表示だけを 0 に戻す。ログの要約はクリアをまたいだ件数。"""
        panel, manager = self._panel()
        out = io.StringIO()
        cleared = []

        def clear_at_300(_trap):
            if len(self.got) == 300 and not cleared:
                cleared.append(panel.trap_drop_label.text())
                panel._on_trap_clear_clicked()
                # 空いた 300 件ぶんは受け、残りの 200 件を捨てる
                _inject(manager, 10000, 500)
        manager.trap_received.connect(clear_at_300)

        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)
            self._drain()
        self.assertEqual(len(cleared), 1)
        self.assertIn("取りこぼし: 500 件", cleared[0])
        self.assertEqual(len(self.got), CAP + 300)
        self.assertEqual(SUMMARY.findall(out.getvalue()), ["700"],
                         out.getvalue())
        self.assertIn("取りこぼし: 200 件", panel.trap_drop_label.text())


class TrapBacklogSummariseLockTest(unittest.TestCase):
    """summarise も要約の区切り（件数の取り出しと 0 戻し）を錠の中で行う。"""

    def test_summarise_touches_the_counts_only_under_the_lock(self):
        from core.snmp_manager import _TrapBacklog
        outside = []

        class _Lock:
            def __init__(self):
                self._lock = threading.Lock()
                self.held = False

            def __enter__(self):
                self._lock.acquire()
                self.held = True

            def __exit__(self, *exc):
                self.held = False
                self._lock.release()

        def watched(name):
            key = "_watched_" + name

            def get(self):
                if self._watching and not self._lock.held:
                    outside.append(name)
                return self.__dict__[key]

            def put(self, value):
                if self._watching and not self._lock.held:
                    outside.append(name)
                self.__dict__[key] = value
            return property(get, put)

        names = ("pending", "dropped", "_reported", "_burst", "_burst_first",
                 "_burst_last")
        attrs = {name: watched(name) for name in names}
        attrs["_watching"] = False
        backlog = type("WatchedBacklog", (_TrapBacklog,), attrs)(1)
        backlog._lock = _Lock()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual([backlog.take() for _ in range(3)],
                             [True, False, False])
        backlog._watching = True
        summary = backlog.summarise()
        self.assertEqual(summary[0], 2)
        self.assertIsNone(backlog.summarise(), "区切った分をもう一度返した")
        # 捌け切ったときの要約は、もう出さない
        self.assertIsNone(backlog.delivered()[2])
        self.assertEqual(outside, [], "錠の外で数に触った: %s" % outside)


class TrapDropsLoggedOnQuitForUpdateTest(unittest.TestCase):
    """更新の適用（quit_for_update）で終わっても、捨てた件数がログに残る。

    別プロセスで実物の MainWindow を開いて app.exec() を回し、あふれている
    最中（GUI が止まったまま）に quit_for_update で終わらせる。終了処理の後は
    イベントループが回らないので、同じプロセスでは確かめられない。
    """

    SCRIPT = textwrap.dedent('''
        import os, socket, sys, threading, time
        sys.path.insert(0, %(src)r)
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from unittest import mock
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication
        app = QApplication([])

        class V:
            def __init__(self, text):
                self.text = text
            def prettyPrint(self):
                return self.text

        def var_binds(n):
            return [(V("1.3.6.1.2.1.1.3.0"), V(str(n))),
                    (V("1.3.6.1.6.3.1.1.4.1.0"), V("1.3.6.1.6.3.1.1.5.3"))]

        with mock.patch("ui.main_window.MainWindow._check_for_updates_on_startup"):
            from ui.main_window import MainWindow
            window = MainWindow()
        window.show()
        panel = window.snmp_panel
        manager = panel.snmp_manager
        deadline = time.monotonic() + 60
        while not panel.mib_loaded and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        panel.trap_port_spinbox.setValue(port)
        with mock.patch("ui.snmp_panel.QMessageBox.warning"):
            panel._on_trap_start_clicked()
        print("RECEIVING %%s" %% manager.is_trap_receiver_running(), flush=True)
        got = []
        manager.trap_received.connect(lambda t: got.append(1))

        def step():
            notify = manager.trap_receiver._on_notification
            def run():
                for n in range(%(count)d):
                    notify(None, None, None, None, var_binds(n), None)
            worker = threading.Thread(target=run)
            worker.start()
            worker.join()
            from ui.dialogs.update_dialog import quit_for_update
            quit_for_update(window)

        QTimer.singleShot(0, step)
        code = app.exec()
        print("EXEC-RETURNED %%d DELIVERED %%d" %% (code, len(got)), flush=True)
    ''')

    def test_quitting_mid_overflow_keeps_the_count_in_the_log(self):
        work = tempfile.mkdtemp(prefix="netbelt-trap-quit-")
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        # 子の出力は UTF-8 として読む（英語版 Windows の cp1252 で製品の
        # 日本語の print が落ちないように。test_window_close_waits_for_mib_loader と同じ）
        env["PYTHONIOENCODING"] = "utf-8"
        script = self.SCRIPT % {"src": os.path.abspath("src"),
                                "count": CAP + 500}
        # cwd は空の一時フォルダ（MainWindow が config.json を作る）
        proc = subprocess.run([sys.executable, "-c", script], env=env,
                              cwd=work, capture_output=True, timeout=180)
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0,
                         "exit=%s\n%s" % (proc.returncode, err[-800:]))
        self.assertIn("RECEIVING True", out)
        self.assertIn("EXEC-RETURNED 0", out, out[-800:])
        self.assertIn("Trap backlog reached its limit", out)
        lines = [line for line in out.splitlines() if "Trap backlog" in line]
        self.assertEqual([int(n) for n in SUMMARY.findall(out)], [500],
                         "更新の適用で終わると捨てた件数がログに残らない:\n%s"
                         % "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
