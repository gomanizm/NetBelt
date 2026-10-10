"""Trap の取りこぼしのログが書けなくても、件数の知らせと受信の停止が飛ばないことを検証する。

何が起きていたか（8ad283e）: 1.3.4 で足した取りこぼしのログ（「Trap backlog」の行）の
print が例外を出すと、その後ろの処理が飛んでいた。配布版は stdout をログファイルへ
向けるので（main.py の _setup_logging）、ドライブの容量不足などで書けないと
print が OSError を出す。
- 停止（stop_trap_receiver）: 受信スレッドを止めた後の要約で例外が出て、
  trap_receiver を手放さず、「SNMP Trap受信停止」の知らせも出ないまま、例外が
  呼び出し元（停止ボタン）へ出た。
- 配送待ちが捌け切ったときの要約（_on_trap_queued）: 例外がスロットの外へ出て
  excepthook（「予期しないエラー」のダイアログ）になった。要約と同じ配送で
  知らせる件数があれば、trap_dropped も出ず、その件数は以後も知らせなかった。
  ただし上限は max(1000, max_traps) で、上限が 2 以上なら、捌け切る配送で新しく
  捨てた件数は必ず 0 になる（捨てるのは配送待ちが上限ちょうどのときで、その
  次の配送ではまだ捌け切らない）。製品で飛んでいたのは知らせではなく、例外が
  外へ出ることのほう（実測: 上限 2・3・5・1000 で delivered() を約 54 万回呼んで、
  重なったのは 0 回。上限 1 では重なる）。
- あふれ始めの行（_TrapBacklog.take。受信スレッド）: 例外が通知コールバックの
  外（pysnmp）へ出て、その Trap の pysnmp の後始末が飛んだ（実測: 127.0.0.1 へ
  UDP で Trap を送り、あふれ始め 3 回で pysnmp の送信元の控えが 3 件残った）。

同じ形で 9fee4af からあったもの: 受信機の stop() の「停止要求」の行が例外になると、
停止を求める前に抜け、受信スレッドが動き続けた（書けない状態が続いていると、
停止の要約より先にそこで失敗する）。そこだけ直すと、次は受信スレッドの run() の
「正常終了」の行が例外になり、except の行も書けずに例外がスレッドの外（excepthook）
へ出た。main.py の excepthook は受信スレッドのままダイアログを開くので、停止の
wait（5 秒）が時間切れになり、停止は終わらなかった。その時間切れの警告の行
（stop_trap_receiver）も、書けないと例外になり、停止の残りが飛んだ。停止の
知らせ（stopped）を受けるパネルのスロットも、先頭の行が例外になると表示を戻す
処理が飛び、表示は「停止しています...」のまま、開始ボタンも戻らなかった。

直し方: この 6 つの print（_log_dropped_traps・take・受信機の stop()・run() の
「正常終了」・stop_trap_receiver の警告・パネルの stopped のスロット）だけ、
書けなくても例外を出さない。ログの文面・件数・順番は変えない。

ログの出力先は、配布版と同じ層（TextIOWrapper(line_buffering) → BufferedWriter →
raw）で、raw の write だけを ENOSPC にしたものへ差し替える。

範囲外: 受信スレッドの run() のほかの行（開始の行・except の「エラー:」の行など）と、
パネルのほかの行（受信開始の知らせのスロットの行など）は直していない。
"""
import contextlib
import errno
import io
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

from conftest import free_udp_port   # noqa: E402

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


def _inject(manager, start, count):
    """受信スレッドと同じく、GUI 以外のスレッドから通知コールバックを呼ぶ"""
    notify = manager.trap_receiver._on_notification

    def run():
        for n in range(start, start + count):
            notify(None, None, None, None, _var_binds(n), None)
    worker = threading.Thread(target=run)
    worker.start()
    worker.join()


class _Raw(io.RawIOBase):
    """ログファイルのいちばん下の層。fail を立てると write が ENOSPC になる"""

    def __init__(self):
        super().__init__()
        self.fail = False
        self.attempted = bytearray()   # 書けなかったバイト列（書こうとした行）

    def writable(self):
        return True

    def write(self, data):
        if self.fail:
            self.attempted += bytes(data)
            raise OSError(errno.ENOSPC, "No space left on device")
        return len(data)


class _Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作った SNMPManager はクラス終了まで保持する（キューに残ったシグナルの
    # 配送先を先に捨てると落ちる。test_snmp_trap_backlog_cap と同じ）
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
        self.drops = []    # trap_dropped で知らせた (件数, 上限)

    def _manager(self):
        from core.snmp_manager import SNMPManager
        manager = SNMPManager()
        self._keep.append(manager)
        self.addCleanup(manager.stop_trap_receiver)
        manager.trap_received.connect(self.got.append)
        manager.trap_dropped.connect(
            lambda count, limit, last: self.drops.append((count, limit)))
        return manager

    def _start(self, manager):
        """受信を始め、受信スレッドが開始の行を書き終えるまで待つ。

        受信スレッドの run() は、開始の行（「開始: ポート…」など 4 行）を書いてから
        started を出す。ログを書けない状態にする前にこれを待たないと、受信スレッドの
        起動が遅い環境では開始の行が書けない側へ出て、run() の例外がスレッドの外
        （excepthook）へ出る（このテストで確かめたいこととは別の失敗）。
        """
        started = []
        manager.trap_receiver_started.connect(lambda: started.append(1))
        self.assertTrue(manager.start_trap_receiver(free_udp_port(), ["public"]))
        deadline = time.monotonic() + 10
        while not started and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(started)

    def _log(self):
        """配布版の stdout と同じ層の出力（main.py の open(..., buffering=1)）"""
        raw = _Raw()
        stream = io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8",
                                  line_buffering=True)

        def close():
            raw.fail = False
            stream.close()
        self.addCleanup(close)
        return raw, stream

    def _drain(self, seconds=10.0):
        """配送待ちが無くなるまでイベントループを回す（GUI の再開）"""
        deadline = time.monotonic() + seconds
        idle = 0
        while time.monotonic() < deadline and idle < 3:
            before = len(self.got)
            self.app.processEvents()
            idle = idle + 1 if len(self.got) == before else 0
            time.sleep(0.01)


class TrapDropLogWriteFailureTest(_Case):

    def test_stop_finishes_when_the_summary_cannot_be_written(self):
        """受信スレッドが止まった後に書けなくなっても、停止を最後まで行い、例外を出さない。

        元は例外を呼び出し元（停止ボタンのスロット）へ出していた（excepthook の
        「予期しないエラー」のダイアログ）。書けないのはログの 1 行だけで停止
        そのものは済んでいるので、例外は出さない（受信ループの例外ハンドラや
        main.py の excepthook と同じく、記録の失敗で本来の処理を止めない）。
        """
        manager = self._manager()
        notices = []
        manager.operation_started.connect(notices.append)
        self._start(manager)
        receiver = manager.trap_receiver
        real_wait = receiver.wait
        raw, log = self._log()

        def wait_then_cannot_write(*args):
            finished = real_wait(*args)
            raw.fail = True   # 受信スレッドが止まった後から書けない
            return finished

        with contextlib.redirect_stdout(log):
            _inject(manager, 0, CAP + 500)   # GUI は止めたまま
            with mock.patch.object(receiver, "wait",
                                   side_effect=wait_then_cannot_write):
                manager.stop_trap_receiver()
        del receiver, real_wait
        self.assertIsNone(manager.trap_receiver)
        self.assertEqual(len(notices), 2, notices)
        self.assertEqual(notices[1], "SNMP Trap受信停止")
        # 要約の行は書こうとした（件数はこれまでどおり）
        self.assertIn(b"Reception stopped before the Trap backlog drained: "
                      b"dropped 500 trap(s)", bytes(raw.attempted))
        # 配送待ちはこのあとも届き、パネルへも知らせる
        self._drain()
        self.assertEqual(len(self.got), CAP)
        self.assertEqual(self.drops, [(500, CAP)])

    def test_drained_summary_that_cannot_be_written_stays_in_the_slot(self):
        """捌け切ったときの要約が書けなくても、例外はスロットの外へ出ず、件数は届く。"""
        manager = self._manager()
        self._start(manager)
        raw, log = self._log()
        escaped = []
        with contextlib.redirect_stdout(log), \
                mock.patch.object(sys, "excepthook",
                                  lambda *exc: escaped.append(exc[1])):
            _inject(manager, 0, CAP + 500)   # GUI は止めたまま
            raw.fail = True                  # ここから書けない
            self._drain()
        self.assertEqual(escaped, [])
        self.assertEqual(len(self.got), CAP)
        self.assertEqual(self.drops, [(500, CAP)])
        self.assertIn(b"Trap backlog drained: dropped 500 trap(s)",
                      bytes(raw.attempted))

    def test_drops_found_with_the_summary_are_still_reported(self):
        """要約と同じ配送で知らせる件数があれば、要約が書けなくてもその件数を知らせる。

        製品の上限（max(1000, max_traps)）では、捌け切る配送で新しく捨てた件数は
        0 になる（モジュールの docstring）。ここでは数え役を上限 1 で作り、
        要約の行の成否と知らせの順に依らないことを確かめる。
        """
        from core.snmp_manager import _TrapBacklog
        manager = self._manager()
        backlog = _TrapBacklog(1)
        manager._trap_backlog = backlog
        raw, log = self._log()
        with contextlib.redirect_stdout(log):
            self.assertTrue(backlog.take())
            self.assertFalse(backlog.take())   # 上限で 1 件捨てる
            raw.fail = True
            manager._on_trap_queued(backlog, {"n": 0})   # 捌け切る配送
        self.assertEqual(self.got, [{"n": 0}])
        self.assertEqual(self.drops, [(1, 1)])
        self.assertIn(b"Trap backlog drained: dropped 1 trap(s)",
                      bytes(raw.attempted))

    def test_overflow_line_that_cannot_be_written_stays_out_of_pysnmp(self):
        """あふれ始めの行が書けなくても、通知コールバックは例外を出さずに数える。

        受信スレッドで pysnmp から呼ばれる。例外を出すと、pysnmp はその Trap の
        後始末（pysnmp/proto/rfc3412.py の receive_message の、送信元の控えの
        削除）を飛ばす。ここでは pysnmp と同じく通知コールバックを直接呼ぶ。
        """
        manager = self._manager()
        self._start(manager)
        raw, log = self._log()
        with contextlib.redirect_stdout(log):
            _inject(manager, 0, CAP)          # 配送待ちが上限ちょうど
            raw.fail = True
            manager.trap_receiver._on_notification(
                None, None, None, None, _var_binds(CAP), None)
        self.assertEqual(manager._trap_backlog.dropped, 1)
        self.assertIn(b"Trap backlog reached its limit (1000)",
                      bytes(raw.attempted))
        self._drain()
        self.assertEqual(len(self.got), CAP)
        self.assertEqual(self.drops, [(1, CAP)])

    def test_stop_finishes_while_the_log_cannot_be_written(self):
        """書けない状態が続いている間に停止しても、受信スレッドを止めて停止を最後まで行う。

        元は受信機の stop() の「停止要求」の行で例外が出て、停止を求める前に抜けて
        いた。受信スレッドは動き続け、trap_receiver も残り、「SNMP Trap受信停止」も
        出ず、例外は呼び出し元（停止ボタン）へ出た（9fee4af から）。

        受信スレッドの run() の「正常終了」の行も書けないと、例外がスレッドの外
        （excepthook）へ出る。main.py の excepthook は受信スレッドのままダイアログを
        開いて止まり、停止の wait が時間切れになる。ここでは excepthook を記録だけに
        差し替え、1 度も呼ばれないことを確かめる（PyQt6 の既定の excepthook のまま
        だと、漏れたときにプロセスごと止まる）。
        """
        manager = self._manager()
        notices = []
        manager.operation_started.connect(notices.append)
        self._start(manager)
        receiver = manager.trap_receiver
        raw, log = self._log()
        escaped = []
        with contextlib.redirect_stdout(log), \
                mock.patch.object(sys, "excepthook",
                                  lambda *exc: escaped.append(exc[1])):
            raw.fail = True   # 停止を求める前から書けない
            manager.stop_trap_receiver()
        self.assertIn("停止要求".encode("utf-8"), bytes(raw.attempted),
                      "前提: 停止要求の行を書こうとしていない")
        self.assertIn("正常終了".encode("utf-8"), bytes(raw.attempted),
                      "前提: 正常終了の行を書こうとしていない")
        self.assertEqual(escaped, [])
        self.assertTrue(receiver.isFinished(), "受信スレッドが止まらない")
        self.assertIsNone(manager.trap_receiver)
        self.assertEqual(len(notices), 2, notices)
        self.assertEqual(notices[1], "SNMP Trap受信停止")

    def test_stop_finishes_when_the_wait_gives_up_and_the_log_cannot_be_written(self):
        """受信スレッドが 5 秒で止まらなかった停止でも、警告の行が書けないまま最後まで行う。

        元は警告の行で例外が出て、参照の保持（_retire）・取りこぼしの要約・
        trap_receiver の片付け・「SNMP Trap受信停止」の知らせが飛び、例外は
        呼び出し元（停止ボタン）へ出た（9fee4af から）。
        """
        manager = self._manager()
        notices = []
        manager.operation_started.connect(notices.append)
        self._start(manager)
        receiver = manager.trap_receiver
        real_wait = receiver.wait

        def wait_gives_up(*args):
            real_wait(*args)   # 実際には止める（スレッドを残さない）
            return False       # 5 秒で止まらなかった扱い

        raw, log = self._log()
        escaped = []
        with contextlib.redirect_stdout(log), \
                mock.patch.object(sys, "excepthook",
                                  lambda *exc: escaped.append(exc[1])), \
                mock.patch.object(receiver, "wait", side_effect=wait_gives_up), \
                mock.patch.object(manager, "_retire",
                                  wraps=manager._retire) as retire:
            raw.fail = True   # 停止を求める前から書けない
            manager.stop_trap_receiver()
        self.assertIn("5秒以内に終了しませんでした".encode("utf-8"),
                      bytes(raw.attempted), "前提: 警告の行を書こうとしていない")
        self.assertEqual(escaped, [])
        retire.assert_called_once_with(receiver)
        self.assertTrue(receiver.isFinished(), "受信スレッドが止まらない")
        del receiver, real_wait, retire
        self.assertIsNone(manager.trap_receiver)
        self.assertEqual(len(notices), 2, notices)
        self.assertEqual(notices[1], "SNMP Trap受信停止")


class TrapPanelStopLogWriteFailureTest(_Case):

    def test_panel_shows_stopped_while_the_log_cannot_be_written(self):
        """書けない状態が続いている間にパネルの停止ボタンで止めても、表示が「停止中」へ戻る。

        元はパネルの stopped のスロット（_on_trap_receiver_stopped）の行で例外が出て、
        表示を戻す処理（_show_trap_stopped）が飛んでいた。表示は「停止しています...」の
        まま、開始ボタンは隠れたまま、停止ボタンは押せないままだった。stopped は 1 回
        しか出ないので、ログが書けるようになっても受信を開始し直せない（9fee4af から）。
        """
        from ui.snmp_panel import SNMPPanel
        with mock.patch.object(SNMPPanel, "_start_background_mib_loading"):
            panel = SNMPPanel()
        self._keep.append(panel)
        panel.mib_loading = False
        panel.mib_loaded = True
        manager = self._manager()
        panel.set_snmp_manager(manager)
        # パネルのスロットより後につなぐ（パネルの開始・停止の行の後に立つ）
        started, stopped = [], []
        manager.trap_receiver_started.connect(lambda: started.append(1))
        manager.trap_receiver_stopped.connect(lambda: stopped.append(1))
        panel.trap_port_spinbox.setValue(free_udp_port())
        with mock.patch("ui.snmp_panel.QMessageBox.warning") as warn:
            panel._on_trap_start_clicked()
        warn.assert_not_called()
        deadline = time.monotonic() + 10
        while not started and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(started)
        self.assertTrue(panel.trap_start_button.isHidden(), "前提: 受信中の表示でない")

        raw, log = self._log()
        escaped = []
        with contextlib.redirect_stdout(log), \
                mock.patch.object(sys, "excepthook",
                                  lambda *exc: escaped.append(exc[1])):
            raw.fail = True   # 停止を求める前から書けない
            panel._on_trap_stop_clicked()
            deadline = time.monotonic() + 10
            while not stopped and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        self.assertTrue(stopped)
        self.assertIn("Trap受信が停止しました".encode("utf-8"), bytes(raw.attempted),
                      "前提: パネルの停止の行を書こうとしていない")
        self.assertEqual(escaped, [])
        self.assertIsNone(manager.trap_receiver)
        self.assertEqual(panel.trap_status_label.text(), "🔴 停止中")
        self.assertFalse(panel.trap_start_button.isHidden(), "開始ボタンが戻らない")
        self.assertTrue(panel.trap_stop_button.isHidden())
        self.assertTrue(panel.trap_stop_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
