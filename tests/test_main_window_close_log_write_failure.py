"""終了の処理（MainWindow.closeEvent）の途中でログを書けなくても、後片付けが最後まで進むことを検証する。

何が起きていたか（91dee14、Codex の REST-01）: 配布版は stdout をログファイルへ
向けるので（main.py の _setup_logging）、ドライブの容量不足などで書けないと
print が OSError を出す。
- 1.3.4 で足した、終了で配られずに捨てた Trap の行
  （SNMPManager.discard_undelivered_traps の「Discarded N undelivered trap(s)」）が
  書けないと例外になり、closeEvent の except の行（「SNMP の後始末エラー」）も
  書けずに、例外が closeEvent の外へ出た。その後ろの端末の出力の書き切り・記録の
  終了・知らせの切り離し・別ウィンドウとポートチェッカーの片付け・event.accept() が
  飛んだ（配送待ちが 1 件以上あり、受信機を止めた後から書けなくなったとき。
  9fee4af はこの場合も最後まで進んでいた）。
- 9fee4af からある closeEvent の行（各サーバーの停止の行・「Stopping SNMP threads」・
  except の行）も同じ形で、閉じる前から書けないと、最初に通った行（Syslog・SFTP・
  TFTP・FTP のどれも動いていなければ SNMP の停止の行）で例外が外へ出て、Trap の
  受信も止めずに残りが飛んだ。

直し方: closeEvent が直接書く行と、discard_undelivered_traps の行は、書けなくても
例外を出さない。ログの文面・件数・順番は変えない。

窓は代役（SimpleNamespace）にして、MainWindow.closeEvent の本物を呼ぶ。部品は
呼ばれたことを記録するだけの代役で、SNMPManager は本物を使う（受信は始めない。
通信はしない）。ログの出力先は、配布版と同じ層（TextIOWrapper(line_buffering) →
BufferedWriter → raw）で、raw の write だけを ENOSPC にしたものへ差し替える。

範囲外: closeEvent から呼ぶ先（Syslog の stop()、接続の後始末、
SNMPManager.cancel_operation の 5 秒の警告など）の中の行は直していない。
"""
import contextlib
import errno
import io
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, "src")

QUEUED = 10   # 閉じるときの配送待ちの Trap の件数

DISCARDED = (b"[SNMPManager] Discarded 10 undelivered trap(s) at exit "
             b"(not shown in the list)")

# 書ける場合の closeEvent の行（この順）。サーバーはどれも動いている代役
LINES = [
    "[Main] Stopping Syslog receiver...",
    "[Main] Stopping SFTP server...",
    "[Main] Stopping TFTP server...",
    "[Main] Stopping FTP server...",
    "[Main] Stopping SNMP threads...",
    DISCARDED.decode("ascii"),
]

# 最後まで進んだときに呼ばれる部品（この順）
ALL_STEPS = [
    "tell_panels_closing", "stop_serial_monitor", "save_layout",
    "stop_syslog_receiver", "stop_sftp_server", "stop_tftp_server",
    "stop_ftp_server", "cancel_operation", "stop_trap_receiver",
    "wait_for_background_work", "discard_undelivered_traps",
    "drain_output_before_log_finish", "finish_log_recordings",
    "detach_notifications", "close_detached_tool", "close_port_checker",
    "event_accept",
]


class _Raw(io.RawIOBase):
    """ログファイルのいちばん下の層。marker を含む write から（None なら最初から）
    ENOSPC にする。fail が立っている間は書けない"""

    def __init__(self, marker=None, fail=False):
        super().__init__()
        self.marker = marker
        self.fail = fail
        self.attempted = bytearray()   # 書けなかったバイト列（書こうとした行）
        self.written = bytearray()

    def writable(self):
        return True

    def write(self, data):
        data = bytes(data)
        if self.marker is not None and self.marker in data:
            self.fail = True
        if self.fail:
            self.attempted += data
            raise OSError(errno.ENOSPC, "No space left on device")
        self.written += data
        return len(data)


class CloseLogWriteFailureTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # 作った SNMPManager はクラス終了まで保持する（他の SNMP のテストと同じ）
    _keep = []

    @classmethod
    def tearDownClass(cls):
        cls.app.processEvents()
        cls._keep.clear()

    def setUp(self):
        from core.snmp_manager import SNMPManager, _TrapBacklog
        self.calls = []
        self.manager = SNMPManager()
        self._keep.append(self.manager)
        self.backlog = _TrapBacklog(1000)
        self.manager._trap_backlog = self.backlog
        for _ in range(QUEUED):
            self.assertTrue(self.backlog.take())
        self.got = []
        self.manager.trap_received.connect(self.got.append)
        for name in ("cancel_operation", "stop_trap_receiver",
                     "discard_undelivered_traps"):
            self._record(self.manager, name)

    def _record(self, obj, name, error=None):
        """obj.name を、呼ばれたことを記録してから本物を呼ぶもの（error なら
        それを出すもの）に差し替える"""
        real = getattr(obj, name)

        def wrapper(*args, **kwargs):
            self.calls.append(name)
            if error is not None:
                raise error
            return real(*args, **kwargs)
        patcher = mock.patch.object(obj, name, wrapper)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _step(self, name):
        return lambda *args, **kwargs: self.calls.append(name)

    def _window(self):
        """closeEvent が触る部品だけを持つ窓の代役（サーバーはどれも動いている）"""
        step = self._step
        snmp_panel = SimpleNamespace(
            snmp_manager=self.manager,
            wait_for_background_work=step("wait_for_background_work"))
        return SimpleNamespace(
            _tell_panels_closing=step("tell_panels_closing"),
            device_tree=SimpleNamespace(
                stop_serial_monitor=step("stop_serial_monitor")),
            _save_layout=step("save_layout"),
            syslog_receiver=SimpleNamespace(
                is_running=True, stop=step("stop_syslog_receiver")),
            sftp_server_panel=SimpleNamespace(sftp_server=SimpleNamespace(
                server_thread=SimpleNamespace(is_alive=lambda: True),
                stop=step("stop_sftp_server"))),
            tftp_server_panel=SimpleNamespace(tftp_server=SimpleNamespace(
                is_running=True, stop=step("stop_tftp_server"))),
            ftp_server_panel=SimpleNamespace(ftp_server=SimpleNamespace(
                is_running=True, stop=step("stop_ftp_server"))),
            snmp_panel=snmp_panel,
            connections={}, sftp_managers={},
            _drain_output_before_log_finish=step(
                "drain_output_before_log_finish"),
            terminal_widget=SimpleNamespace(
                finish_log_recordings=step("finish_log_recordings")),
            _notice_sources=[object()],
            _detach_notifications=step("detach_notifications"),
            _detached={"tool": SimpleNamespace(
                _closing=False, close=step("close_detached_tool"))},
            port_checker_window=SimpleNamespace(
                close=step("close_port_checker")),
        )

    def _log(self, raw):
        """配布版の stdout と同じ層の出力（main.py の open(..., buffering=1)）"""
        stream = io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8",
                                  line_buffering=True)

        def close():
            raw.marker = None
            raw.fail = False
            stream.close()
        self.addCleanup(close)
        return stream

    def _close(self, raw):
        """代役の窓で closeEvent の本物を流す。例外は外へ出させる"""
        from ui.main_window import MainWindow
        event = SimpleNamespace(accept=self._step("event_accept"))
        with contextlib.redirect_stdout(self._log(raw)):
            MainWindow.closeEvent(self._window(), event)

    def _assert_discarded(self):
        """配送待ちは数え終え、以後に届いた配送は一覧へ入れない"""
        self.assertTrue(self.backlog.closed)
        self.assertEqual(self.backlog.pending, 0)
        self.manager._on_trap_queued(self.backlog, {"late": True})
        self.assertEqual(self.got, [])

    def test_close_writes_the_same_lines_when_the_log_can_be_written(self):
        """書けるときの行と順番、呼ぶ部品は今までどおり"""
        raw = _Raw()

        self._close(raw)

        self.assertEqual(self.calls, ALL_STEPS)
        self.assertEqual(bytes(raw.written).decode("utf-8").splitlines(), LINES)
        self._assert_discarded()

    def test_close_finishes_when_the_discarded_line_cannot_be_written(self):
        """受信を止めた後、捨てた Trap の行から書けなくなっても、最後まで進む（1.3.4 の後退）"""
        raw = _Raw(marker=b"Discarded")

        self._close(raw)

        self.assertIn(b"[Main] Stopping SNMP threads...", bytes(raw.written),
                      "前提: 受信を止める行は書けている")
        self.assertIn(DISCARDED, bytes(raw.attempted),
                      "前提: 捨てた Trap の行を書こうとしていない")
        self.assertEqual(self.calls, ALL_STEPS)
        self._assert_discarded()

    def test_close_finishes_while_the_log_cannot_be_written(self):
        """閉じる前から書けなくても、サーバーと受信を止め、最後まで進む（9fee4af から）"""
        raw = _Raw(fail=True)

        self._close(raw)

        attempted = bytes(raw.attempted).decode("utf-8")
        for line in LINES:
            self.assertIn(line, attempted, "前提: 書こうとしていない行がある")
        self.assertEqual(bytes(raw.written), b"")
        self.assertEqual(self.calls, ALL_STEPS)
        self._assert_discarded()

    def test_close_finishes_when_the_error_lines_cannot_be_written(self):
        """停止・待機・後始末の例外を書く行が書けなくても、残りが最後まで進む"""
        self._record(self.manager, "cancel_operation",
                     RuntimeError("cancel failed"))
        self._record(self.manager, "discard_undelivered_traps",
                     RuntimeError("discard failed"))
        raw = _Raw(fail=True)
        window = self._window()
        window.snmp_panel.wait_for_background_work = mock.Mock(
            side_effect=RuntimeError("wait failed"))
        from ui.main_window import MainWindow
        event = SimpleNamespace(accept=self._step("event_accept"))

        with contextlib.redirect_stdout(self._log(raw)):
            MainWindow.closeEvent(window, event)

        attempted = bytes(raw.attempted).decode("utf-8")
        for line in ("[Main] SNMP 停止エラー: cancel failed",
                     "[Main] MIB 読み込みの待機エラー: wait failed",
                     "[Main] SNMP の後始末エラー: discard failed"):
            self.assertIn(line, attempted, "前提: 書こうとしていない行がある")
        window.snmp_panel.wait_for_background_work.assert_called_once_with()
        # 停止で例外が出たので stop_trap_receiver は呼ばない（今までどおり）
        self.assertEqual(self.calls[self.calls.index("cancel_operation"):],
                         ["cancel_operation", "discard_undelivered_traps",
                          "drain_output_before_log_finish",
                          "finish_log_recordings", "detach_notifications",
                          "close_detached_tool", "close_port_checker",
                          "event_accept"])

    def test_discarding_does_not_raise_when_the_line_cannot_be_written(self):
        """discard_undelivered_traps は、行が書けなくても例外を出さずに数え終える"""
        raw = _Raw(fail=True)

        with contextlib.redirect_stdout(self._log(raw)):
            self.manager.discard_undelivered_traps()

        self.assertIn(DISCARDED, bytes(raw.attempted))
        self._assert_discarded()


if __name__ == "__main__":
    unittest.main()
