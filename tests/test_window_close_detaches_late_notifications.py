"""窓を閉じたら、その窓の接続の知らせが窓の受け口へ届かなくなることを検証する。

何が起きていたか（実測、一時ブランチの e143a5f）。tests/ 全体を 1 プロセスで
流すと、31% 付近で tests/test_reconnect_wait_survives_enter_reconnect.py に
入ったところでプロセスが黙って消えた（exit 127。Qt のメッセージは
「QtFatalMsg TypeError: () missing 1 required positional argument: 'c'」）。

接続の知らせ（受信・接続・切断・エラー）は、main_window.py で接続オブジェクトを
既定値に束縛した lambda（`lambda error, c=conn: ...` など）で受けている。

  1) 窓を閉じたあとに、まだ走っている接続スレッドが知らせを出す（Enter で
     始めた再接続の失敗など）。キュー接続なので配送待ちに残る。closeEvent の
     配送待ちの配り切り（_drain_output_before_log_finish）より後なので、
     誰も配らない。
  2) 窓・接続・lambda がまとめて GC に回収される（テストでは、偽の接続の
     一覧を次のテストの setUp が空にした時点）。CPython は回収するとき、
     関数の既定値と名前を空にする。
  3) 次にイベントを処理したとき、PyQt の中継が配送待ちの知らせで空の lambda
     を呼び、TypeError になる。sys.excepthook が既定のままだと、PyQt は qFatal
     でプロセスを落とす（アプリ本体は excepthook を差し替えているので、落ちずに
     記録に残るだけ）。

仕組みは scratchpad の probe_window_garbage.py で再現した（親付きの接続・
キューに残った知らせ・窓ごとの GC で、同じ TypeError が 2 回）。deleteLater
では防げず（削除より先に配送待ちの知らせが届く）、知らせの結び付きを外すと
防げた。

どう直したか: closeEvent で、接続と SFTP マネージャを片付け、配送待ちの受信を
記録し切ったあとに、それらの知らせ（受信・接続・切断・エラー）の結び付きを
外す。外しておけば、あとから届く知らせは誰も呼ばない。

このテストは、閉じたあとに接続の知らせの受け手が残っていないことを、Qt の
receivers() で確かめる（GC の時機に左右されない）。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QCoreApplication, QEvent, QObject, pyqtSignal

sys.path.insert(0, "src")

NOTIFICATIONS = ("output_received", "connected", "disconnected", "error_occurred")

from ui.main_window import MainWindow as _MainWindow    # noqa: E402

# setUp で差し替える前の本物（SFTP マネージャの登録を確かめるテストで使う）
_REAL_START_SFTP = _MainWindow._start_sftp_session


class _FakeSSH(QObject):
    """接続の見た目だけを持つ偽物。connect() は成功を知らせるだけ。"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)
    instances = []

    def __init__(self, host, port, username, password, ssh_key, parent=None):
        super().__init__(parent)
        self.client = None
        type(self).instances.append(self)

    def set_terminal_size(self, cols, rows):
        pass

    def connect(self):
        self.connected.emit()
        return True

    def send_command(self, command):
        pass

    def dispose(self):
        pass

    def disconnect(self):
        pass


class _FakeTelnet(_FakeSSH):
    """Telnet 接続の見た目だけを持つ偽物（引数の形だけ Telnet に合わせる）。"""
    instances = []

    def __init__(self, host, port, username, password, parent=None):
        QObject.__init__(self, parent)
        self.client = None
        type(self).instances.append(self)


class _FakeSerial(QObject):
    """シリアル接続の見た目だけを持つ偽物（送信の背圧の口も持つ）。"""
    output_received = pyqtSignal(str)
    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error_occurred = pyqtSignal(str)
    send_drained = pyqtSignal()
    instances = []

    def __init__(self, port, baudrate, parent=None):
        super().__init__(parent)
        type(self).instances.append(self)

    def connect(self):
        self.connected.emit()
        return True

    def has_pending_sends(self):
        return False

    def send_command(self, command):
        pass

    def dispose(self):
        pass

    def disconnect(self):
        pass


class _PendingSFTP(QObject):
    """確立を合図まで待たせる SFTP マネージャの偽物（辞書へ入る前の状態を作る）。"""
    error_occurred = pyqtSignal(str)
    release = None
    instances = []

    def __init__(self, parent=None):
        super().__init__(parent)
        type(self).instances.append(self)

    def connect(self, client):
        type(self).release.wait(5)
        return False

    def disconnect(self):
        pass


class _FakeSFTP(QObject):
    """SFTP マネージャの見た目だけを持つ偽物。"""
    error_occurred = pyqtSignal(str)

    def disconnect(self):
        pass


class WindowCloseDetachesLateNotificationsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.config_manager import ConfigManager
        from ui.main_window import MainWindow
        d = Path(tempfile.mkdtemp(prefix="netbelt-latenotice-"))
        for patcher in (
                mock.patch("core.config_manager.app_data_dir", return_value=d),
                mock.patch("ui.main_window.ConfigManager",
                           lambda *a, **k: ConfigManager(str(d / "config.json"))),
                mock.patch("ui.main_window.SSHConnection", _FakeSSH),
                mock.patch("ui.main_window.TelnetConnection", _FakeTelnet),
                mock.patch("ui.main_window.SerialConnection", _FakeSerial),
                mock.patch.object(MainWindow, "_start_sftp_session")):
            patcher.start()
            self.addCleanup(patcher.stop)
        _FakeSSH.instances = []
        _FakeTelnet.instances = []
        _FakeSerial.instances = []
        self.window = MainWindow()
        self.addCleanup(self._close_quietly)

    def _close_quietly(self):
        """後始末の close。回帰で closeEvent が例外を出しても、テストの失敗で
        止まるようにする（既定の excepthook のままだと PyQt が qFatal で
        プロセスごと落とし、全体の結果が失われる）"""
        with mock.patch("sys.excepthook", lambda *args: None):
            self.window.close()

    def _pump(self, seconds=0.3):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.01)

    def _connect(self):
        self.window._on_connect_requested({
            "name": "dev", "host": "192.0.2.10", "port": 22,
            "username": "u", "password": "", "protocol": "ssh"})
        self._pump()
        return _FakeSSH.instances[-1]

    @staticmethod
    def _receivers(obj, names):
        return {name: obj.receivers(getattr(obj, name)) for name in names}

    def test_closing_detaches_the_connection_notifications(self):
        """閉じたあと、接続の知らせの受け手が残っていないこと。"""
        conn = self._connect()
        before = self._receivers(conn, NOTIFICATIONS)
        self.assertTrue(all(count > 0 for count in before.values()),
                        "前提: 接続の知らせが窓へ結ばれていない: %r" % (before,))

        self.window.close()

        after = self._receivers(conn, NOTIFICATIONS)
        self.assertEqual(after, {name: 0 for name in NOTIFICATIONS},
                         "閉じた窓へ、接続の知らせがまだ結ばれている")

    def test_closing_detaches_a_connection_already_taken_off_the_list(self):
        """閉じる前にタブを閉じて辞書から外れた接続も、閉じたあと受け手が残らないこと。

        外した接続は deleteLater に渡すが、イベントループへ戻るまで破棄されず
        窓の子として残る（テストのように外から直接呼ぶと processEvents では
        処理されない）。その接続の遅れた知らせも、閉じた窓へ届けない。
        """
        conn = self._connect()
        self.window._on_tab_closed("dev")
        self.assertNotIn("dev", self.window.connections, "前提: 辞書から外れていない")

        self.window.close()

        after = self._receivers(conn, NOTIFICATIONS)
        self.assertEqual(after, {name: 0 for name in NOTIFICATIONS},
                         "辞書から外れた接続の知らせが、閉じた窓へまだ結ばれている")

    def test_closing_detaches_a_telnet_connection_taken_off_the_list(self):
        """Telnet でも、辞書から外れた接続の知らせを閉じたあと残さないこと。"""
        self.window._on_connect_requested({
            "name": "tel", "host": "192.0.2.11", "port": 23,
            "username": "u", "password": "", "protocol": "telnet"})
        self._pump()
        conn = _FakeTelnet.instances[-1]
        self.window._on_tab_closed("tel")

        self.window.close()

        after = self._receivers(conn, NOTIFICATIONS)
        self.assertEqual(after, {name: 0 for name in NOTIFICATIONS},
                         "辞書から外れた Telnet 接続の知らせが、閉じた窓へまだ結ばれている")

    def test_closing_detaches_a_serial_connection_taken_off_the_list(self):
        """シリアルでも、辞書から外れた接続の知らせを閉じたあと残さないこと。"""
        self.window._connect_serial({
            "name": "con", "protocol": "console", "host": "COM9",
            "baudrate": 9600})
        self._pump()
        conn = _FakeSerial.instances[-1]
        self.window._on_tab_closed("con")

        self.window.close()

        after = self._receivers(conn, NOTIFICATIONS)
        self.assertEqual(after, {name: 0 for name in NOTIFICATIONS},
                         "辞書から外れたシリアル接続の知らせが、閉じた窓へまだ結ばれている")

    def test_closing_detaches_an_sftp_manager_still_being_established(self):
        """確立の途中（辞書へ入る前）の SFTP マネージャも、閉じたあと受け手を残さないこと。"""
        import threading
        conn = self._connect()
        _PendingSFTP.release = threading.Event()
        _PendingSFTP.instances = []
        self.addCleanup(_PendingSFTP.release.set)
        with mock.patch("ui.main_window.SFTPManager", _PendingSFTP):
            _REAL_START_SFTP(self.window, "dev", conn)
        manager = _PendingSFTP.instances[-1]
        self.assertNotIn(manager, self.window.sftp_managers.values(),
                         "前提: 確立の途中なのに辞書へ入っている")
        self.assertGreater(manager.receivers(manager.error_occurred), 0,
                           "前提: SFTP のエラーが窓へ結ばれていない")

        self.window.close()

        self.assertEqual(manager.receivers(manager.error_occurred), 0,
                         "確立途中の SFTP マネージャのエラーが、閉じた窓へまだ結ばれている")

    def _close_after_the_connection_was_deleted(self, notice):
        """接続が失敗・切断して破棄されたあと（ラッパーだけが残る）に窓を閉じる。

        再接続待ちの端末は接続の has_pending_sends（bound method）を握り続ける
        ので、C++ 側が破棄されてもラッパーは登録簿に残る。そのラッパーの
        シグナルを引くと RuntimeError になる（検査役の実測: closeEvent が途中で
        止まり、アプリでは「予期しないエラー」のダイアログが出て、別ウィンドウに
        したツールが閉じられずに残った。テストでは qFatal でプロセスが落ちた）。
        """
        from PyQt6 import sip
        conn = self._connect()
        notice(conn)
        self.assertNotIn("dev", self.window.connections, "前提: 辞書から外れていない")
        # イベントループへ戻ったのと同じく、deleteLater を処理させる
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self.assertTrue(sip.isdeleted(conn), "前提: C++ 側が破棄されていない")
        self.window._detach_tool("sftp")
        detached = self.window._detached["sftp"]
        errors = []
        with mock.patch("sys.excepthook",
                        lambda t, v, tb: errors.append("%s: %s" % (t.__name__, v))):
            self.window.close()
        self.assertEqual(errors, [], "closeEvent が例外で止まった")
        self.assertFalse(detached.isVisible(), "別ウィンドウのツールが閉じられていない")

    def test_close_runs_to_the_end_after_a_failed_connection_was_deleted(self):
        """接続失敗で破棄された接続が残っていても、閉じる処理が最後まで走ること。"""
        self._close_after_the_connection_was_deleted(
            lambda conn: conn.error_occurred.emit("connection refused"))

    def test_close_runs_to_the_end_after_a_dropped_connection_was_deleted(self):
        """機器側の切断で破棄された接続が残っていても、閉じる処理が最後まで走ること。"""
        self._close_after_the_connection_was_deleted(
            lambda conn: conn.disconnected.emit())

    def test_closing_detaches_the_sftp_error_notice(self):
        """閉じたあと、SFTP マネージャのエラーの受け手が残っていないこと。"""
        sftp = _FakeSFTP()
        self.addCleanup(sftp.deleteLater)
        sftp.error_occurred.connect(lambda err, c=sftp: None)
        self.window.sftp_managers["dev"] = sftp

        self.window.close()

        self.assertEqual(sftp.receivers(sftp.error_occurred), 0,
                         "閉じた窓へ、SFTP のエラーがまだ結ばれている")

    def test_closing_keeps_other_panels_error_notices(self):
        """接続・SFTP 以外（SNMP・Syslog の管理役）のエラーの受け手は外さないこと（対照）。

        これらは終了処理の最中に届いたエラーもパネルが記録へ残す（SNMPPanel の
        _on_error_occurred は「黙って捨てずに記録だけ残す」）。窓の子を名前で
        一律に拾って外すと、それが消える（69af9ba を検査役が実測）。
        """
        snmp = self.window.snmp_panel.snmp_manager
        syslog = self.window.syslog_receiver
        before = (snmp.receivers(snmp.error_occurred),
                  syslog.receivers(syslog.error_occurred))
        self.assertTrue(all(before), "前提: エラーの受け手が無い: %r" % (before,))

        self.window.close()

        after = (snmp.receivers(snmp.error_occurred),
                 syslog.receivers(syslog.error_occurred))
        self.assertEqual(after, before,
                         "SNMP・Syslog のエラーの受け手まで外した")

    def test_output_received_before_close_is_still_delivered(self):
        """記録中なら、閉じる直前に届いた受信を配り切ってから外すこと（対照）。

        closeEvent は、ログを記録している間だけ配送待ちの受信を配り切って
        記録へ回す（_drain_output_before_log_finish）。外すのを先にすると、
        記録へ書くはずの受信が誰にも届かず、記録から欠ける。記録していない
        ときは、閉じた窓へ届く受信に行き先は無い。
        """
        conn = self._connect()
        self.window.terminal_widget.has_open_log_recordings = lambda: True
        delivered = []
        original = self.window._on_connection_output

        def spy(device_name, text, c=None):
            delivered.append(text)
            return original(device_name, text, c)

        self.window._on_connection_output = spy
        thread = threading.Thread(
            target=lambda: conn.output_received.emit("tail-before-close"))
        thread.start()
        thread.join()

        self.window.close()

        self.assertIn("tail-before-close", delivered,
                      "閉じる直前に届いた受信が、配り切られる前に外された")


if __name__ == "__main__":
    unittest.main()
