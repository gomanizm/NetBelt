"""終了で配られずに捨てた Trap の件数が、上限で捨てた件数とは別の行でログに残ることを検証する。

何が起きていたか（43b2980）: 受信スレッドが GUI へ渡したがまだ配られていない
Trap（配送待ち。既定で最大 1000 件、max_traps を大きくしていればその件数）は、
終了でどこにも数えられずに消えていた。
- 更新の適用（quit_for_update）: 窓を閉じたあと QApplication.quit() で終わり、
  キューを配らない（実測: 配送待ち 1000 件のまま 0 件配って終了）。
- ×で閉じる: 最後の窓が閉じて積まれる Quit より前に配られるが、入る先は閉じた
  窓の一覧で、誰にも見えず保存もされない（実測: 閉じた後に 1000 件を一覧へ
  入れてから終了。max_traps 20000 では 20000 件で約 0.54 秒）。
- 配っている途中に更新を適用すると、quit() の後も同じ回の残りを配った
  （実測: 300 件目の配送の中で適用して、残り 700 件も配られた）。

- 端末の記録を開いたまま閉じると（×・更新の適用とも）、記録の書き切り
  （_drain_output_before_log_finish と finish_log_recordings）が配送待ちを
  閉じかけの窓の一覧へ全部配ってから終わる。

直し方: MainWindow.closeEvent で受信を止めた後、記録の書き切りの直前に
SNMPManager.discard_undelivered_traps を呼ぶ。配送待ちの件数を
「[SNMPManager] Discarded N undelivered trap(s) at exit (not shown in the list)」
と 1 行書き、以後に届いた配送は一覧へ入れない。配り切るのは待たない。
0 件のときは書かない。上限で捨てた件数（「Trap backlog」の行）とは別の行で、
要約は二重にならない。受け取った数 ＝ 表示した数 ＋ 上限で捨てた数 ＋ 終了で
捨てた数。
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

from conftest import free_udp_port   # noqa: E402

CAP = 1000   # 既定の上限（max_traps が 1000 以下のとき）
EXIT = re.compile(r"\[SNMPManager\] Discarded (\d+) undelivered trap\(s\) at exit "
                  r"\(not shown in the list\)")
# 上限で捨てた件数の要約（捌け切ったとき・止めたときのどちらの行にも当たる）
SUMMARY = re.compile(r"Trap backlog drained: dropped (\d+) trap")
# 終了が待たされないことの目安。手元では閉じ始めてから app.exec() が戻るまで
# 0.01〜0.03 秒。遅い CI と GC の停止（約 0.26 秒）を見込んで大きく取る
EXIT_SECONDS = 3.0


class _Value:
    """pysnmp の OID・値の代わり（_build_trap_data が使う prettyPrint だけ持つ）"""

    def __init__(self, text):
        self._text = text

    def prettyPrint(self):
        return self._text


def _var_binds(n):
    return [(_Value("1.3.6.1.2.1.1.3.0"), _Value(str(n))),
            (_Value("1.3.6.1.6.3.1.1.4.1.0"), _Value("1.3.6.1.6.3.1.1.5.3"))]


def _inject(manager, start, count):
    """受信スレッドと同じく、GUI 以外のスレッドから通知コールバックを呼ぶ"""
    notify = manager.trap_receiver._on_notification

    def run():
        for n in range(start, start + count):
            notify(None, None, None, None, _var_binds(n), None)
    worker = threading.Thread(target=run)
    worker.start()
    worker.join()


def _exit_counts(text):
    return [int(n) for n in EXIT.findall(text)]


def _accounted(shown, text):
    """表示した数 ＋ 上限で捨てた数 ＋ 終了で捨てた数（ログから読む）"""
    return (shown + sum(int(n) for n in SUMMARY.findall(text))
            + sum(_exit_counts(text)))


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


class TrapsDiscardedAtExitTest(_Case):

    def test_exit_counts_the_queued_traps_and_shows_none_of_them(self):
        """配送待ちの件数を別の行で書き、そのあと届いた配送は一覧へ入れない。"""
        manager = self._manager()
        self._start(manager)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)   # GUI は止めたまま
            manager.stop_trap_receiver()
            manager.discard_undelivered_traps()
            at_exit = out.getvalue()
            self._drain()
        text = out.getvalue()
        self.assertEqual(_exit_counts(at_exit), [CAP],
                         "終了の時点で配送待ちの件数がログに無い:\n" + at_exit)
        exit_lines = [line for line in text.splitlines() if EXIT.search(line)]
        self.assertNotIn("Trap backlog", exit_lines[0],
                         "上限の行と区別できない")
        # 上限で捨てた件数は停止の行に 1 回だけ。終了の行とは別
        self.assertEqual(SUMMARY.findall(text), ["500"], text)
        self.assertEqual(self.got, [], "数えた後に一覧へ入れた")
        self.assertEqual(self.drops, [])
        self.assertEqual(_accounted(len(self.got), text), CAP + 500)

    def test_exit_while_draining_counts_only_the_rest(self):
        """配っている途中で終わると、まだ配られていない分だけを数える。"""
        manager = self._manager()
        self._start(manager)
        out = io.StringIO()
        at_exit = []
        errors = []

        def exit_at_300(_trap):
            # スロットから例外を漏らすと PyQt6 がプロセスごと止めるので、
            # 拾ってから後で確かめる
            if len(self.got) == 300 and not at_exit:
                try:
                    manager.stop_trap_receiver()
                    manager.discard_undelivered_traps()
                except Exception as e:
                    errors.append(repr(e))
                at_exit.append(out.getvalue())
        manager.trap_received.connect(exit_at_300)

        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)
            self._drain()
        self.assertEqual(errors, [])
        self.assertEqual(len(at_exit), 1)
        self.assertEqual(_exit_counts(at_exit[0]), [CAP - 300], at_exit[0])
        self.assertEqual(len(self.got), 300, "数えた後も配った")
        self.assertEqual(SUMMARY.findall(out.getvalue()), ["500"],
                         out.getvalue())
        self.assertEqual(sum(self.drops), 500)   # 300 件目までに知らせた分
        self.assertEqual(_accounted(len(self.got), out.getvalue()), CAP + 500)

    def test_exit_counts_traps_left_from_an_earlier_reception(self):
        """止めて開き直した前の回の配送待ちも、終了の件数に入る。"""
        manager = self._manager()
        self._start(manager)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)
            manager.stop_trap_receiver()
            self._start(manager)              # 前の回の 1000 件はまだ配送待ち
            _inject(manager, 10000, 200)
            manager.stop_trap_receiver()
            manager.discard_undelivered_traps()
            at_exit = out.getvalue()
            self._drain()
        self.assertEqual(_exit_counts(at_exit), [CAP + 200], at_exit)
        self.assertEqual(self.got, [])
        self.assertEqual(SUMMARY.findall(out.getvalue()), ["500"])
        self.assertEqual(_accounted(len(self.got), out.getvalue()),
                         CAP + 500 + 200)

    def test_earlier_receptions_that_drained_are_not_kept(self):
        manager = self._manager()
        self._start(manager)
        with contextlib.redirect_stdout(io.StringIO()):
            _inject(manager, 0, 10)
            self._drain()
            manager.stop_trap_receiver()
            self._start(manager)
        self.assertEqual(manager._earlier_backlogs, [])
        self.assertEqual(len(self.got), 10)

    def test_nothing_queued_writes_no_exit_line(self):
        """0 件のときは書かない（ほとんどの終了でログを変えない）。"""
        idle = self._manager()           # 受信を始めていない
        manager = self._manager()
        self._start(manager)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            idle.discard_undelivered_traps()
            _inject(manager, 0, 10)
            self._drain()
            manager.stop_trap_receiver()
            manager.discard_undelivered_traps()
        self.assertEqual(len(self.got), 10)
        self.assertNotIn("Discarded", out.getvalue(), out.getvalue())

    def test_the_exit_line_is_written_once(self):
        """終了処理が 2 度通っても、同じ配送待ちを二度数えない。"""
        manager = self._manager()
        self._start(manager)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _inject(manager, 0, 50)
            manager.stop_trap_receiver()
            manager.discard_undelivered_traps()
            manager.discard_undelivered_traps()
            self._drain()
        self.assertEqual(_exit_counts(out.getvalue()), [50], out.getvalue())
        self.assertEqual(self.got, [])

    def test_the_panel_list_does_not_take_the_discarded_traps(self):
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
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _inject(manager, 0, CAP + 500)
            panel._on_trap_stop_clicked()
            manager.discard_undelivered_traps()
            self._drain()
        self.assertEqual(_exit_counts(out.getvalue()), [CAP])
        self.assertEqual(panel.trap_data_list, [])
        self.assertEqual(panel.trap_tree_model.rowCount(), 0)


class TrapsDiscardedOnRealExitTest(unittest.TestCase):
    """実物の MainWindow で、×で閉じる経路と更新の適用の経路を通して終わらせる。

    別プロセスで app.exec() を回し、配送待ちを溜めたまま（GUI を止めたまま）
    閉じる。終了処理の後はイベントループが回らないので、同じプロセスでは
    確かめられない（test_snmp_trap_drops_logged_on_stop と同じ）。
    """

    SCRIPT = textwrap.dedent('''
        import json, os, socket, sys, threading, time
        sys.path.insert(0, %(src)r)
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from unittest import mock
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication
        app = QApplication([])
        MODE, MID, COUNT, RECORD = %(mode)r, %(mid)d, %(count)d, %(record)r

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
        # MIB の読み込みが残っていると、閉じるときにその終わりを待つ
        # （SNMPPanel.wait_for_background_work）。終了の時間に混ぜない
        deadline = time.monotonic() + 120
        while not panel.mib_loaded and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        print("MIB-LOADED %%s" %% panel.mib_loaded, flush=True)
        terminal = window.terminal_widget
        if RECORD:
            # 端末の記録を開いておく。閉じるときの記録の書き切り
            # （_drain_output_before_log_finish と finish_log_recordings）は
            # 配送待ちをその場で配る
            terminal.create_terminal_tab("trap-exit-dev")
            terminal.tab_widget.setCurrentIndex(terminal.tab_widget.count() - 1)
            log_path = os.path.join(os.getcwd(), "trap-exit-dev.log")
            with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                            return_value=(log_path, "Log (*.log)")):
                terminal.start_log_recording()
        print("RECORDING %%s" %% terminal.has_open_log_recordings(), flush=True)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        panel.trap_port_spinbox.setValue(port)
        with mock.patch("ui.snmp_panel.QMessageBox.warning"):
            panel._on_trap_start_clicked()
        print("RECEIVING %%s" %% manager.is_trap_receiver_running(), flush=True)
        got = []
        mark = {}

        def finish():
            mark["pending"] = manager._trap_backlog.pending
            mark["got_before"] = len(got)
            mark["t0"] = time.perf_counter()
            if MODE == "update":
                from ui.dialogs.update_dialog import quit_for_update
                quit_for_update(window)
            else:
                window.close()

        def on_trap(_trap):
            got.append(1)
            if MID and len(got) == MID and "t0" not in mark:
                finish()
        manager.trap_received.connect(on_trap)

        def step():
            notify = manager.trap_receiver._on_notification
            def run():
                for n in range(COUNT):
                    notify(None, None, None, None, var_binds(n), None)
            worker = threading.Thread(target=run)
            worker.start()
            worker.join()
            if not MID:
                finish()

        QTimer.singleShot(0, step)
        code = app.exec()
        mark.update(code=code, got_after=len(got),
                    exit_seconds=time.perf_counter() - mark.pop("t0"))
        print("RESULT " + json.dumps(mark), flush=True)
    ''')

    def _run(self, mode, mid=0, record=False):
        work = tempfile.mkdtemp(prefix="netbelt-trap-exit-")
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        # 子の出力は UTF-8 として読む（英語版 Windows の cp1252 で製品の
        # 日本語の print が落ちないように）
        env["PYTHONIOENCODING"] = "utf-8"
        script = self.SCRIPT % {"src": os.path.abspath("src"), "mode": mode,
                                "mid": mid, "count": CAP + 500,
                                "record": record}
        # cwd は空の一時フォルダ（MainWindow が config.json を作る）
        proc = subprocess.run([sys.executable, "-c", script], env=env,
                              cwd=work, capture_output=True, timeout=180)
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(proc.returncode, 0,
                         "exit=%s\n%s" % (proc.returncode, err[-800:]))
        self.assertIn("MIB-LOADED True", out, "前提: MIB の読み込みが終わらない")
        self.assertIn("RECORDING %s" % record, out, "前提: 記録を開けない")
        self.assertIn("RECEIVING True", out)
        found = re.search(r"^RESULT (.*)$", out, re.M)
        self.assertIsNotNone(found, out[-800:])
        return out, json.loads(found.group(1))

    def _check(self, mode, mid=0, record=False):
        out, result = self._run(mode, mid, record)
        lines = "\n".join(line for line in out.splitlines()
                          if "Trap backlog" in line or EXIT.search(line))
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["got_before"], mid)
        self.assertEqual(result["pending"], CAP - mid)
        # 終了の行は配送待ちの件数で 1 行。上限の行（500）とは別
        self.assertEqual(_exit_counts(out), [CAP - mid],
                         "終了で捨てた件数がログに無い・合わない:\n" + lines)
        self.assertEqual(SUMMARY.findall(out), ["500"], lines)
        # 閉じ始めた後は 1 件も配らない（配り切るのを待たない）
        self.assertEqual(result["got_after"], mid, lines)
        self.assertEqual(_accounted(result["got_after"], out), CAP + 500)
        self.assertLess(result["exit_seconds"], EXIT_SECONDS,
                        "終了が待たされた: %.2f 秒" % result["exit_seconds"])

    def test_closing_the_window_logs_the_queued_traps(self):
        self._check("x")

    def test_applying_an_update_logs_the_queued_traps(self):
        self._check("update")

    def test_applying_an_update_while_draining_logs_only_the_rest(self):
        """配っている途中の更新の適用。quit() の後に残りを配らない。"""
        self._check("update", mid=300)

    def test_closing_the_window_while_recording_logs_the_queued_traps(self):
        """端末の記録中に閉じる。記録の書き切りで配送待ちを一覧へ配らない。"""
        self._check("x", record=True)

    def test_applying_an_update_while_recording_logs_the_queued_traps(self):
        self._check("update", record=True)


if __name__ == "__main__":
    unittest.main()
