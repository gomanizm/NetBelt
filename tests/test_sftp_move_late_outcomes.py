"""SFTP のディレクトリ移動の答えが遅れて届くときの扱いを、実際の Qt のイベントループで検証する。

1.3.4 で change_directory の normalize（REALPATH）を使い捨てのスレッドへ移した
（Codex レビュー R05。tests/test_sftp_move_off_gui_thread.py）。答えは GUI の
操作のあとから届くようになったので、利用者の指示（2026-10-03）どおり、
次の 5 つの場合を確かめる。

  1. 成功の遅れ: 答えが届くまで場所は変わらない。届いたら正規化したパスの
     一覧が届き、パネルの場所が変わる。知らせは GUI スレッドで処理される。
  2. 失敗の遅れ:「ディレクトリ変更エラー」が 1 回だけ出る。期限切れなら
     SFTP を切断し、一覧の表示は残す（利用者の決定）。期限切れ以外の失敗の
     扱いは、GUI スレッドで同期に呼んでいた 9fee4af と同じ。
  3. 処理中に画面を片付ける（機器のタブを閉じる・窓を閉じて破棄する・パネルを
     破棄する・切り離した SFTP の窓を閉じる）: 落ちず、破棄した相手へ知らせが
     届かず、ロックが放され、スレッドが終わる。切り離した窓を閉じたときは、
     タブへ戻ったパネルに答えが 1 回だけ届く。
  4. 処理中の切断（SSH の切断・SFTP だけ畳む）: 答えは捨てられ、切断の
     知らせは重ならない。ロックが放され、スレッドが終わる。
  5. 繋ぎ直したあとに古い答えが届く: 新しい接続の場所・一覧を変えず、
     一覧の取得も起こさず、古い答えのエラーも出さない。

機器は 127.0.0.1 の本物の SSH／SFTP（paramiko のサーバ）で、REALPATH の答えを
止める・遅らせる・失敗させることができる。後始末で SSH を閉じると、待って
いた normalize はその場で終わる。閉じても答えが遅れて届く場合（送信の詰まり
などで、閉じるのが答えより後になる）は、クライアントの normalize を、放す
まで答えない代役に差し替えて作る。

2 の 2 件と、失敗の種類ごとの表（test_each_kind_of_failure_is_handled_as_before）
は、9fee4af（同期で呼んでいた版）でもそのまま通る（扱いが同じことの確かめ）。
ほかの多くは、答えを待つあいだに操作できることが前提なので 9fee4af では落ちる。

後始末: 止めた答えは必ず放し、移動のスレッドの終わりを待ち、配送待ちの
知らせを処理してから、接続と機器を閉じる。後続のテストを巻き込まない
ため（配送待ちの知らせが次のファイルで届いて落ちたことがある）。
"""
import json
import os
import posixpath
import socket
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import paramiko

sys.path.insert(0, "src")

HOME = "/home/admin"
SUB = HOME + "/sub"          # 一覧の 1 行目のディレクトリ
SUB_REAL = "/data/sub"       # 機器が SUB を正規化した先（リンクの先）
ROWS = ["sub", "a.txt"]      # どの場所の一覧もこの 2 行
_WAIT = 5.0                  # 止めた答えを放すまで・スレッドを待つ上限（秒）
_LIMIT = 0.8                 # 後始末で進行中の操作を待つ上限（本物は 3 秒）

# 配送待ちの知らせが残っても、送り手・受け手ごと GC されないように持つ
_KEEP = []


class _SFTP(paramiko.SFTPServerInterface):
    """機器の SFTP。REALPATH は _Device.realpath に任せる"""

    def __init__(self, server, device):
        super().__init__(server)
        self.device = device

    def canonicalize(self, path):
        return self.device.realpath(path)

    @staticmethod
    def _attr(name):
        attr = paramiko.SFTPAttributes()
        attr.filename = name
        attr.st_mode = ((stat.S_IFREG | 0o644) if name.endswith(".txt")
                        else (stat.S_IFDIR | 0o755))
        attr.st_size = 0
        attr.st_uid = attr.st_gid = 0
        attr.st_atime = attr.st_mtime = 1700000000
        return attr

    def list_folder(self, path):
        self.device.listed.append(path)
        return [self._attr(name) for name in ROWS]

    def stat(self, path):
        return self._attr(posixpath.basename(path))

    lstat = stat


class _Shell(paramiko.ServerInterface):
    def __init__(self, shells):
        self.shells = shells

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        self.shells.append(channel)
        return True

    def check_channel_window_change_request(self, *args):
        return True


class _Device:
    """シェルと SFTP を持つ 127.0.0.1 の機器（何本でも受ける）

    hold(path) で、その場所の REALPATH の答えを止める。返した gate を立てると
    答え、error を渡していればそこで失敗する（paramiko のサーバは SFTP_FAILURE
    を返し、クライアントでは IOError('Failure') になる）。立てなくても _WAIT 秒で
    答える。
    """

    def __init__(self):
        self.shells = []
        self.transports = []
        self.listed = []                 # list_folder で頼まれた場所
        self.entered = threading.Event()  # 止めた REALPATH が届いた
        self._held = {}
        self._gates = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.host_key = paramiko.ECDSAKey.generate()
        threading.Thread(target=self._accept, daemon=True).start()

    def hold(self, path, error=None):
        gate = threading.Event()
        self._gates.append(gate)
        self.entered.clear()
        self._held[path] = (gate, error)
        return gate

    def release_all(self):
        for gate in self._gates:
            gate.set()

    def realpath(self, path):
        held = self._held.get(path)
        if held is not None:
            gate, error = held
            self.entered.set()
            gate.wait(_WAIT)
            if error is not None:
                raise error
        if path in ("", "."):
            return HOME
        full = posixpath.normpath(
            path if path.startswith("/") else posixpath.join(HOME, path))
        return SUB_REAL if full == SUB else full

    def close_last_shell(self):
        """機器がいちばん新しいセッションのシェルを閉じる（SSH の切断）"""
        self.shells[-1].close()

    def _accept(self):
        while True:
            try:
                accepted, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(accepted,),
                             daemon=True).start()

    def _serve(self, accepted):
        t = paramiko.Transport(accepted)
        self.transports.append(t)
        t.add_server_key(self.host_key)
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, _SFTP, self)
        try:
            t.start_server(server=_Shell(self.shells))
        except Exception:
            return
        ch = t.accept(10)
        if ch is None:
            return
        # 先に開いたのが SFTP のチャンネルなら触らない（プロンプトを書くと壊れる）
        deadline = time.monotonic() + _WAIT
        while (ch not in self.shells and not ch.closed
               and time.monotonic() < deadline):
            time.sleep(0.01)
        if ch not in self.shells:
            return
        ch.sendall(b"sw# ")
        ch.settimeout(0.2)
        while True:
            try:
                if not ch.recv(65536):
                    break
            except socket.timeout:
                continue
            except Exception:
                break

    def close(self):
        self.release_all()
        try:
            self.sock.close()
        except OSError:
            pass
        for t in list(self.transports):
            t.close()


class _Harness(unittest.TestCase):
    """機器・例外の記録・移動のスレッドの後始末をまとめる"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core.sftp_manager import SFTPManager
        self._threads_before = set(threading.enumerate())
        self.slot_errors, self.worker_errors = [], []
        for patcher in (
                mock.patch.object(SFTPManager, "_DISCONNECT_WAIT_SECONDS", _LIMIT),
                # スロットの例外（既定のままだと PyQt がプロセスごと落とす）
                mock.patch.object(sys, "excepthook",
                                  lambda *a: self.slot_errors.append(a[:2])),
                mock.patch.object(threading, "excepthook",
                                  lambda args: self.worker_errors.append(
                                      (args.exc_type, args.exc_value,
                                       getattr(args.thread, "name", "?"))))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.device = _Device()
        self.clients = []    # テストが張った SSHClient
        self.movers = []     # normalize を呼んだスレッド（移動のワーカー）
        self.gates = []      # 代役の normalize を止めている gate

    def _pump(self, check=lambda: False, seconds=5.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        self.app.processEvents()
        return bool(check())

    @staticmethod
    def _notes(manager):
        """マネージャの知らせを控える（パネルを外したあとも残る結び付き）"""
        notes = types.SimpleNamespace(errors=[], gone=[])
        manager.error_occurred.connect(notes.errors.append)
        manager.disconnected.connect(lambda n=notes: n.gone.append(True))
        return notes

    def _record_moves(self, client):
        """client.normalize を、呼んだスレッドを控える素通しにする"""
        real = client.normalize

        def normalize(path):
            self.movers.append(threading.current_thread())
            return real(path)
        client.normalize = normalize

    def _stick(self, client, answer):
        """client.normalize を、gate が立つまで答えない代役にする

        SSH を閉じても戻らない。answer が例外ならそれを投げ、そうでなければ
        answer を正規化したパスとして返す。
        """
        gate, entered = threading.Event(), threading.Event()
        self.gates.append(gate)

        def normalize(path):
            self.movers.append(threading.current_thread())
            entered.set()
            gate.wait(_WAIT)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        client.normalize = normalize
        return gate, entered

    def _finish_moves(self):
        """止めた答えを放し、移動のスレッドが終わるのを待つ（終わったかを返す）"""
        self.device.release_all()
        for gate in self.gates:
            gate.set()
        # GUI スレッド（同期で呼んでいた場合）は待てない・待たない
        workers = [t for t in self.movers if t is not threading.current_thread()]
        for t in workers:
            t.join(_WAIT)
        return not any(t.is_alive() for t in workers)

    def _wait_for_new_threads(self):
        deadline = time.monotonic() + _WAIT
        while time.monotonic() < deadline and any(
                t.is_alive()
                for t in set(threading.enumerate()) - self._threads_before):
            self.app.processEvents()
            time.sleep(0.01)
        self._pump(seconds=0.2)

    def _ssh(self):
        """機器へ繋いだ paramiko の SSHClient"""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect("127.0.0.1", port=self.device.port, username="admin",
                    password="pw", look_for_keys=False, allow_agent=False,
                    timeout=10, banner_timeout=10, auth_timeout=10)
        self.clients.append(ssh)
        return ssh

    @staticmethod
    def _rows(panel):
        from PyQt6.QtCore import Qt
        return [panel.model.item(row, 0).data(Qt.ItemDataRole.UserRole)["name"]
                for row in range(panel.model.rowCount())]

    @staticmethod
    def _open_sub(panel):
        """一覧の 1 行目（sub）をダブルクリックする"""
        panel._on_item_double_clicked(panel.model.index(0, 0))

    def _assert_nothing_crashed(self):
        self.assertEqual([(t.__name__, str(v)) for t, v in self.slot_errors], [],
                         "スロットで例外が出た")
        self.assertEqual([(t.__name__ if t else None, str(v), name)
                          for t, v, name in self.worker_errors], [],
                         "スレッドが例外で落ちた")


class MoveLateOutcomeOnPanelTest(_Harness):
    """本物の SFTP の機器・SFTPManager・SFTPPanel で確かめる"""

    def setUp(self):
        super().setUp()
        from ui import sftp_panel as panel_mod
        self.warning_threads = []
        patcher = mock.patch.object(
            panel_mod.QMessageBox, "warning",
            side_effect=lambda *a, **k: self.warning_threads.append(
                threading.current_thread()))
        self.warnings = patcher.start()
        self.addCleanup(patcher.stop)
        self.managers, self.panels = [], []
        self.addCleanup(self._teardown)

    def _teardown(self):
        from PyQt6 import sip
        self._finish_moves()
        self._pump(seconds=0.2)
        for panel in self.panels:
            if not sip.isdeleted(panel):
                panel.clear()
        for m in self.managers:
            if not sip.isdeleted(m):
                m.disconnect()
        for ssh in self.clients:
            ssh.close()
        self.device.close()
        self._wait_for_new_threads()
        for m in self.managers:
            for name in ("error_occurred", "disconnected", "file_list_ready"):
                try:
                    getattr(m, name).disconnect()
                except (TypeError, RuntimeError):
                    pass    # 繋がっていない / 既に破棄済み

    def _connect(self):
        """機器へ SFTP で繋いだマネージャと、その知らせの控え"""
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        _KEEP.append(m)
        self.managers.append(m)
        self.assertTrue(m.connect(self._ssh()), "前提: SFTP が開かない")
        self._record_moves(m.sftp_client)
        return m, self._notes(m)

    def _panel(self, m):
        """m を表示し、最初の一覧（ホーム）が届いた SFTP パネル"""
        from ui.sftp_panel import SFTPPanel
        panel = SFTPPanel()
        _KEEP.append(panel)
        self.panels.append(panel)
        panel.set_sftp_manager(m, "rtrA")
        self.assertTrue(self._pump(lambda: self._rows(panel) == ROWS),
                        "前提: 最初の一覧が届かない")
        self.assertEqual(panel.path_label.text(), "rtrA: " + HOME)
        self.device.listed.clear()
        return panel

    def _spy(self, cls, names, record):
        """cls のメソッドを、呼ばれたことを record に控える素通しにする

        シグナルへ繋ぐ前（マネージャ・パネルを作る前）に差し替えること。
        """
        for name in names:
            real = getattr(cls, name)

            def spy(obj, *args, _real=real, _name=name):
                record(_name, obj)
                return _real(obj, *args)
            patcher = mock.patch.object(cls, name, spy)
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- 1. 成功の遅れ ---

    def test_a_late_answer_lists_the_normalized_path_on_the_gui_thread(self):
        """答えが届くまで場所は変わらず、届いたら正規化したパスの一覧が届いて
        パネルの場所が変わること。一覧の知らせが GUI スレッドで処理されること。"""
        from core.sftp_manager import SFTPManager
        from ui.sftp_panel import SFTPPanel
        handled = []

        def record(name, _obj):
            handled.append((name, threading.current_thread()))
        self._spy(SFTPManager, ["_on_listing_done"], record)
        self._spy(SFTPPanel, ["_update_file_list"], record)
        m, notes = self._connect()
        panel = self._panel(m)
        handled.clear()
        gate = self.device.hold(SUB)

        started = time.monotonic()
        self._open_sub(panel)
        took = time.monotonic() - started
        self.assertTrue(self.device.entered.wait(_WAIT), "前提: REALPATH が機器に届かない")
        self.assertLess(took, 2.0, "移動の呼び出しが機器の答えを %.2f 秒待った" % took)
        self._pump(seconds=0.3)
        self.assertEqual(m.current_path, HOME, "答えより先に場所が変わった")
        self.assertEqual(panel.path_label.text(), "rtrA: " + HOME)
        self.assertEqual(self.device.listed, [], "答えより先に一覧を頼んだ")

        gate.set()
        self.assertTrue(
            self._pump(lambda: panel.path_label.text() == "rtrA: " + SUB_REAL),
            "移動先の一覧が届かない（知らせ %r）" % notes.errors)
        self._pump(seconds=0.2)

        self.assertEqual(self.device.listed, [SUB_REAL], "正規化したパスで一覧を頼んでいない")
        self.assertEqual(m.current_path, SUB_REAL)
        self.assertEqual(self._rows(panel), ROWS)
        self.assertEqual(panel.status_label.text(), "2 項目")
        self.assertEqual(len(self.movers), 1, "前提: 移動の normalize の回数")
        self.assertIsNot(self.movers[0], threading.main_thread(),
                         "normalize が GUI スレッドで呼ばれた")
        gui = threading.main_thread()
        self.assertEqual(handled, [("_on_listing_done", gui), ("_update_file_list", gui)],
                         "一覧の知らせが GUI スレッドで処理されていない")
        self.assertEqual(notes.errors, [])
        self.warnings.assert_not_called()
        self.assertTrue(self._finish_moves(), "移動のスレッドが終わらない")
        self.assertFalse(m._sftp_lock.locked(), "ロックが放されていない")
        self._assert_nothing_crashed()

    # --- 2. 失敗の遅れ ---

    def test_a_late_timeout_is_reported_once_folds_and_keeps_the_listing(self):
        """期限切れは『ディレクトリ変更エラー』を 1 回だけ知らせ、SFTP を切断し、
        一覧の表示を残すこと（利用者の決定。9fee4af と同じ結果）。"""
        from core.sftp_manager import SFTPManager
        from ui.sftp_panel import SFTPPanel
        patcher = mock.patch.object(SFTPManager, "CHANNEL_TIMEOUT_SECONDS", 2.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        handled = []
        self._spy(SFTPPanel, ["_on_error"],
                  lambda name, _obj: handled.append(threading.current_thread()))
        m, notes = self._connect()
        panel = self._panel(m)
        self.device.hold(SUB)                      # 期限まで答えない

        self._open_sub(panel)
        self.assertTrue(self.device.entered.wait(_WAIT), "前提: REALPATH が機器に届かない")
        self.assertTrue(self._pump(lambda: notes.errors and notes.gone, seconds=10),
                        "期限切れが知らされない")
        self._pump(seconds=0.5)                    # 重ねて届かないことを見る

        expected = ("ディレクトリ変更エラー: 機器が2秒応答しません。"
                    "SFTP接続を切断しました。接続し直してください")
        self.assertEqual(notes.errors, [expected])
        self.assertEqual(len(notes.gone), 1, "disconnected の回数")
        self.warnings.assert_called_once()
        self.assertEqual(self.warnings.call_args[0][2], expected)
        gui = threading.main_thread()
        self.assertEqual(handled, [gui], "失敗の知らせが GUI スレッドで処理されていない")
        self.assertEqual(self.warning_threads, [gui])
        self.assertFalse(m.is_connected, "使えないチャンネルを掴んだまま")
        self.assertIsNone(m.sftp_client)
        self.assertEqual(self._rows(panel), ROWS, "一覧の表示が消えた")
        self.assertEqual(panel.path_label.text(), "rtrA: " + HOME)
        self.assertEqual(panel.status_label.text(), panel.DROPPED_TEXT)
        self.assertEqual(self.device.listed, [], "失敗した移動先を頼んだ")
        self.assertTrue(self._finish_moves(), "移動のスレッドが終わらない")
        self.assertFalse(m._sftp_lock.locked(), "ロックが放されていない")
        self._assert_nothing_crashed()

    def test_a_late_refusal_is_reported_once_and_keeps_the_session(self):
        """期限切れ以外の失敗は、9fee4af と同じく『ディレクトリ変更エラー: 理由』を
        1 回だけ知らせ、接続も場所も変えないこと。次の移動もできること。"""
        from PyQt6.QtGui import QAction
        m, notes = self._connect()
        panel = self._panel(m)
        gate = self.device.hold(SUB, error=OSError("device refused"))
        opener = threading.Timer(0.3, gate.set)    # 0.3 秒遅れて断る
        self.addCleanup(opener.join, _WAIT)
        opener.start()

        self._open_sub(panel)
        self.assertTrue(self._pump(lambda: notes.errors, seconds=10), "失敗が知らされない")
        self._pump(seconds=0.5)

        expected = "ディレクトリ変更エラー: Failure"
        self.assertEqual(notes.errors, [expected])
        self.assertEqual(notes.gone, [], "断られただけで切断した")
        self.warnings.assert_called_once()
        self.assertEqual(self.warnings.call_args[0][2], expected)
        self.assertEqual(self.warning_threads, [threading.main_thread()])
        self.assertTrue(m.is_connected)
        self.assertEqual(m.current_path, HOME)
        self.assertEqual(panel.path_label.text(), "rtrA: " + HOME)
        self.assertEqual(self._rows(panel), ROWS)
        self.assertEqual(panel.status_label.text(), "エラー: " + expected)
        self.assertEqual(self.device.listed, [])
        self.assertFalse(m._sftp_lock.locked(), "ロックが放されていない")

        actions = {a.text(): a for a in panel.findChildren(QAction)}
        actions["ホーム"].trigger()
        self.assertTrue(self._pump(lambda: self.device.listed == [HOME]),
                        "失敗のあとで移動できない（知らせ %r）" % notes.errors)
        self.assertEqual(notes.errors, [expected])
        self.assertTrue(self._finish_moves(), "移動のスレッドが終わらない")
        self._assert_nothing_crashed()

    # --- 3. 処理中にパネルを破棄する ---

    def test_destroying_the_panel_during_the_move_reaches_nothing_deleted(self):
        """normalize の最中にパネルが破棄されても、答え・失敗のどちらでも落ちず、
        破棄したパネルへ知らせが届かず、ロックが放され、スレッドが終わること。"""
        from PyQt6 import sip
        from PyQt6.QtCore import QCoreApplication, QEvent
        from ui.sftp_panel import SFTPPanel
        reached = []
        self._spy(SFTPPanel, ["_update_file_list", "_on_error", "_on_sftp_disconnected"],
                  lambda name, obj: reached.append((name, sip.isdeleted(obj))))
        for label, refusal in (("答えが届く", None),
                               ("機器が断る", OSError("device refused"))):
            with self.subTest(label):
                m, notes = self._connect()
                panel = self._panel(m)
                reached.clear()
                start = len(self.movers)
                gate = self.device.hold(SUB, refusal)

                self._open_sub(panel)
                self.assertTrue(self.device.entered.wait(_WAIT),
                                "前提: REALPATH が機器に届かない")
                panel.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
                self.assertTrue(sip.isdeleted(panel), "前提: パネルが破棄されていない")
                gate.set()
                if refusal is None:
                    self.assertTrue(self._pump(lambda: m.current_path == SUB_REAL),
                                    "マネージャに答えが届かない（知らせ %r）" % notes.errors)
                else:
                    self.assertTrue(self._pump(lambda: notes.errors), "失敗が知らされない")
                self._pump(seconds=0.3)

                self.assertEqual(reached, [], "破棄したパネルへ知らせが届いた")
                if refusal is None:
                    self.assertEqual(self.device.listed, [SUB_REAL])
                    self.assertEqual(notes.errors, [])
                else:
                    self.assertEqual(notes.errors, ["ディレクトリ変更エラー: Failure"])
                    self.assertEqual(m.current_path, HOME)
                self.assertTrue(m.is_connected)
                self.assertEqual(len(self.movers) - start, 1, "前提: normalize の回数")
                self.assertTrue(self._finish_moves(), "移動のスレッドが終わらない")
                self.assertFalse(m._sftp_lock.locked(), "ロックが放されていない")
                self.warnings.assert_not_called()
                self._assert_nothing_crashed()

    # --- 4. 処理中に SFTP だけ畳まれる ---

    def test_folding_sftp_during_the_move_drops_the_answer(self):
        """normalize の最中に SFTP が畳まれたら（別の操作の失敗など。SSH は生きて
        いる）、答え・断り・期限切れのどれでも一覧を頼まず、知らせも重ねないこと。
        パネルは一覧と切断の表示を残すこと。"""
        for label, refusal, stuck in (("答えが届く", None, None),
                                      ("機器が断る", OSError("device refused"), None),
                                      ("期限切れ", None, TimeoutError())):
            with self.subTest(label):
                m, notes = self._connect()
                panel = self._panel(m)
                start = len(self.movers)
                if stuck is None:
                    gate = self.device.hold(SUB, refusal)
                    entered = self.device.entered
                else:
                    gate, entered = self._stick(m.sftp_client, stuck)

                self._open_sub(panel)
                self.assertTrue(entered.wait(_WAIT), "前提: normalize が始まらない")
                # ロックは移動が持っているので、_LIMIT で諦めて参照だけ手放す
                m.disconnect()
                self.assertTrue(self._pump(
                    lambda: panel.status_label.text() == panel.DROPPED_TEXT),
                    "前提: 切断の表示が出ない")
                gate.set()
                self.assertTrue(self._finish_moves(), "移動のスレッドが終わらない")
                self._pump(seconds=0.5)

                self.assertEqual(notes.errors, [], "畳んだあとに知らせた")
                self.assertEqual(len(notes.gone), 1, "disconnected の回数")
                self.assertEqual(self.device.listed, [], "畳んだあとに一覧を頼んだ")
                self.assertEqual(self._rows(panel), ROWS, "一覧の表示が消えた")
                self.assertEqual(panel.path_label.text(), "rtrA: " + HOME)
                self.assertEqual(panel.status_label.text(), panel.DROPPED_TEXT)
                self.assertEqual(len(self.movers) - start, 1, "前提: normalize の回数")
                self.assertFalse(m._sftp_lock.locked(), "ロックが放されていない")
                self.warnings.assert_not_called()
                self._assert_nothing_crashed()

    # --- 5. 同じマネージャで繋ぎ直したあとに古い答えが届く ---

    def test_a_stale_answer_after_the_same_manager_reconnects_changes_nothing(self):
        """畳んだマネージャを新しい SSH・新しいクライアントで繋ぎ直したあとに、
        前のクライアントの答え・期限切れ・断りが届いても、場所・一覧を変えず、
        一覧を頼まず、知らせも出さないこと（届いた答えは前のクライアントのもの）。"""
        for label, answer in (("答え", SUB_REAL), ("期限切れ", TimeoutError()),
                              ("機器が断る", IOError("Failure"))):
            with self.subTest(label):
                m, notes = self._connect()
                panel = self._panel(m)
                old = m.sftp_client
                start = len(self.movers)
                gate, entered = self._stick(old, answer)

                self._open_sub(panel)
                self.assertTrue(entered.wait(_WAIT), "前提: normalize が始まらない")
                m.disconnect()     # ロックは移動が持つので old は閉じずに手放す
                self.assertTrue(m.connect(self._ssh()), "前提: 繋ぎ直せない")
                self.assertIsNot(m.sftp_client, old, "前提: クライアントが同じ")
                # 新しい接続の一覧は、移動がロックを放すまで待つ
                panel.set_sftp_manager(m, "rtrA")
                gate.set()
                self.assertTrue(self._finish_moves(), "移動のスレッドが終わらない")
                self.assertTrue(self._pump(lambda: self._rows(panel) == ROWS),
                                "新しい接続の一覧が届かない（知らせ %r）" % notes.errors)
                self._pump(seconds=0.5)

                self.assertEqual(self.device.listed, [HOME], "古い答えで一覧を頼んだ")
                self.assertEqual(m.current_path, HOME)
                self.assertEqual(panel.path_label.text(), "rtrA: " + HOME)
                self.assertEqual(panel.status_label.text(), "2 項目")
                self.assertEqual(notes.errors, [], "古い答えの知らせを出した")
                self.assertEqual(len(notes.gone), 1, "disconnected の回数（繋ぎ直す前の 1 回）")
                self.assertTrue(m.is_connected, "新しい接続が畳まれた")
                self.assertEqual(len(self.movers) - start, 1, "前提: normalize の回数")
                self.assertFalse(m._sftp_lock.locked(), "ロックが放されていない")
                self.warnings.assert_not_called()
                self._assert_nothing_crashed()


class MoveLateOutcomeInWindowTest(_Harness):
    """本物の MainWindow の後始末・繋ぎ直しの流れで確かめる"""

    def setUp(self):
        super().setUp()
        from core.config_manager import ConfigManager
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-move-late-"))
        config_path = self.dir / "config.json"
        config_path.write_text(json.dumps({
            "groups": [{"name": "Default", "devices": [{
                "name": "sw1", "host": "127.0.0.1", "port": self.device.port,
                "protocol": "ssh", "username": "admin", "password": "pw"}]}],
            "global_macros": []}), encoding="utf-8")
        for patcher in (
                mock.patch("core.config_manager.app_data_dir", return_value=self.dir),
                mock.patch("ui.main_window.ConfigManager",
                           lambda *a, **k: ConfigManager(str(config_path))),
                mock.patch("ui.main_window.MainWindow._check_for_updates_on_startup",
                           lambda self: None),
                mock.patch("ui.main_window.QMessageBox")):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("ui.sftp_panel.QMessageBox")
        self.panel_box = patcher.start()
        self.addCleanup(patcher.stop)
        self.windows, self._closed = [], []
        self.addCleanup(self._teardown)

    def _teardown(self):
        self._finish_moves()
        self._pump(seconds=0.2)
        for window in self.windows:
            self._close_window(window)
        self.device.close()
        self._wait_for_new_threads()
        self.windows.clear()

    def _open_window(self):
        """sw1 へ繋ぎ、SFTP パネルにホームの一覧が出た MainWindow"""
        from ui.main_window import MainWindow
        window = MainWindow()
        self.windows.append(window)
        window._on_connect_requested(window.config_manager.get_groups()[0]["devices"][0])
        self.assertTrue(self._pump(
            lambda: "sw1" in window.sftp_managers
            and self._rows(window.sftp_panel) == ROWS, seconds=20),
            "前提: SFTP パネルに一覧が出ない")
        # 終了処理の MIB 読み込み待ちで、閉じるのが遅れないようにしておく
        window.snmp_panel.wait_for_background_work()
        self._pump(seconds=0.3)
        self.device.listed.clear()
        return window

    def _close_window(self, window):
        if window in self._closed:
            return
        self._closed.append(window)
        for name in list(window.connections):
            window._dispose_connection(name)
        with mock.patch("sys.excepthook", lambda *args: None):
            window.close()
        self._pump(seconds=0.2)

    def _start_move(self, window, answer):
        """sw1 で sub へ移る。answer が None なら機器が答えを止め、SSH を閉じると
        normalize も終わる。それ以外は、閉じても戻らない代役が answer を返す"""
        mgr = window.sftp_managers["sw1"]
        if answer is None:
            self._record_moves(mgr.sftp_client)
            self.device.hold(SUB)
            entered = self.device.entered
        else:
            _gate, entered = self._stick(mgr.sftp_client, answer)
        self._open_sub(window.sftp_panel)
        self.assertTrue(entered.wait(_WAIT), "前提: normalize が始まらない")

    def _drop_and_check(self, window, mgr, notes, start):
        """後始末のあとに答えを放し、何も起きないことを確かめる"""
        from PyQt6 import sip
        from PyQt6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self._pump(seconds=0.2)
        self.assertTrue(sip.isdeleted(mgr), "前提: マネージャが破棄されていない")
        self.assertTrue(self._finish_moves(), "移動のスレッドが終わらない")
        self._pump(seconds=0.5)

        self.assertEqual(notes.errors, [], "後始末のあとに知らせた")
        self.assertEqual(len(notes.gone), 1, "disconnected の回数")
        self.assertEqual(self.device.listed, [], "後始末のあとに一覧を頼んだ")
        panel = window.sftp_panel
        self.assertEqual(panel.model.rowCount(), 0)
        self.assertEqual(panel.path_label.text(), "接続されていません")
        self.assertEqual(len(self.movers) - start, 1, "前提: normalize の回数")
        self.assertFalse(mgr._sftp_lock.locked(), "ロックが放されていない")
        self.panel_box.warning.assert_not_called()
        self._assert_nothing_crashed()

    # --- 3. 処理中に機器のタブを閉じる ---

    def test_closing_the_device_tab_during_the_move(self):
        """normalize の最中に機器のタブを閉じても、落ちず、片付けたパネルと破棄した
        マネージャへ知らせが届かず、ロックが放され、スレッドが終わること。"""
        for label, answer in (("SSH を閉じると normalize も終わる", None),
                              ("答えが破棄のあとに届く", SUB_REAL),
                              ("期限切れが破棄のあとに届く", TimeoutError())):
            with self.subTest(label):
                window = self._open_window()
                mgr = window.sftp_managers["sw1"]
                notes = self._notes(mgr)
                start = len(self.movers)
                self._start_move(window, answer)

                window._on_tab_closed("sw1")

                self.assertNotIn("sw1", window.sftp_managers)
                self._drop_and_check(window, mgr, notes, start)
                self._close_window(window)

    def test_closing_the_window_during_the_move(self):
        """normalize の最中に窓を閉じて破棄しても（アプリの終了）、落ちず、破棄した
        マネージャから知らせが出ず、ロックが放され、スレッドが終わること。"""
        from PyQt6 import sip
        from PyQt6.QtCore import QCoreApplication, QEvent
        for label, answer in (("SSH を閉じると normalize も終わる", None),
                              ("期限切れが破棄のあとに届く", TimeoutError())):
            with self.subTest(label):
                window = self._open_window()
                mgr = window.sftp_managers["sw1"]
                panel = window.sftp_panel
                start = len(self.movers)
                self._start_move(window, answer)

                self._closed.append(window)
                window.close()
                window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
                self.assertTrue(sip.isdeleted(mgr) and sip.isdeleted(panel),
                                "前提: 窓が破棄されていない")
                self.assertTrue(self._finish_moves(), "移動のスレッドが終わらない")
                self._pump(seconds=0.5)

                self.assertEqual(self.device.listed, [], "閉じたあとに一覧を頼んだ")
                self.assertEqual(len(self.movers) - start, 1, "前提: normalize の回数")
                self.assertFalse(mgr._sftp_lock.locked(), "ロックが放されていない")
                self.panel_box.warning.assert_not_called()
                self._assert_nothing_crashed()

    def test_closing_the_detached_sftp_window_during_the_move(self):
        """SFTP クライアントを別ウィンドウへ切り離し、normalize の最中にその窓を
        閉じても（窓は破棄され、パネルはタブへ戻る）、答え・断り・期限切れが
        戻ったパネルへ 1 回だけ届き、落ちず、ロックが放され、スレッドが終わること。"""
        from PyQt6 import sip
        from PyQt6.QtCore import QCoreApplication, QEvent
        timeout = ("ディレクトリ変更エラー: 機器が30秒応答しません。"
                   "SFTP接続を切断しました。接続し直してください")
        for label, refusal, stuck, expected in (
                ("答えが届く", None, None, None),
                ("機器が断る", OSError("device refused"), None,
                 "ディレクトリ変更エラー: Failure"),
                ("期限切れ", None, TimeoutError(), timeout)):
            with self.subTest(label):
                self.panel_box.reset_mock()
                window = self._open_window()
                mgr = window.sftp_managers["sw1"]
                notes = self._notes(mgr)
                start = len(self.movers)
                window._detach_tool("sftp")
                win = window._detached["sftp"]
                self._pump(seconds=0.2)
                panel = window.sftp_panel
                if stuck is None:
                    self._record_moves(mgr.sftp_client)
                    gate = self.device.hold(SUB, refusal)
                    entered = self.device.entered
                else:
                    gate, entered = self._stick(mgr.sftp_client, stuck)
                self._open_sub(panel)
                self.assertTrue(entered.wait(_WAIT), "前提: normalize が始まらない")

                win.close()    # タブへ戻す処理は次のループで走り、窓を deleteLater する
                self.assertTrue(self._pump(lambda: "sftp" not in window._detached,
                                           seconds=2.0), "前提: 窓を閉じてもタブへ戻らない")
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
                self.assertTrue(sip.isdeleted(win), "前提: 切り離した窓が破棄されていない")
                self.assertFalse(sip.isdeleted(panel), "パネルが窓と一緒に破棄された")
                scroll = window.tool_tabs.widget(window._tab_index["sftp"])
                self.assertIs(scroll.widget(), panel, "前提: パネルがタブへ戻っていない")
                self.assertEqual(mgr.current_path, HOME, "答えより先に場所が変わった")
                self.assertEqual(notes.errors, [], "答えより先に知らせた")
                self.assertEqual(self.device.listed, [], "答えより先に一覧を頼んだ")

                gate.set()
                if expected is None:
                    self.assertTrue(
                        self._pump(lambda: panel.path_label.text() == "sw1: " + SUB_REAL),
                        "戻したパネルに移動先の一覧が届かない（知らせ %r）" % notes.errors)
                else:
                    self.assertTrue(self._pump(lambda: notes.errors), "失敗が知らされない")
                self.assertTrue(self._finish_moves(), "移動のスレッドが終わらない")
                self._pump(seconds=0.5)    # 重ねて届かないことを見る

                self.assertEqual(self._rows(panel), ROWS, "一覧の表示が消えた")
                self.assertEqual(len(self.movers) - start, 1, "前提: normalize の回数")
                self.assertFalse(mgr._sftp_lock.locked(), "ロックが放されていない")
                self.assertEqual(len(notes.gone), 1 if stuck else 0, "disconnected の回数")
                self.assertEqual(mgr.is_connected, not stuck)
                if expected is None:
                    self.assertEqual(notes.errors, [])
                    self.assertEqual(self.device.listed, [SUB_REAL])
                    self.assertEqual(mgr.current_path, SUB_REAL)
                    self.assertEqual(panel.status_label.text(), "2 項目")
                    self.panel_box.warning.assert_not_called()
                else:
                    self.assertEqual(notes.errors, [expected])
                    self.assertEqual(self.device.listed, [], "失敗した移動先を頼んだ")
                    self.assertEqual(mgr.current_path, HOME)
                    self.assertEqual(panel.path_label.text(), "sw1: " + HOME)
                    self.assertEqual(panel.status_label.text(),
                                     panel.DROPPED_TEXT if stuck else "エラー: " + expected)
                    self.panel_box.warning.assert_called_once()
                    self.assertEqual(self.panel_box.warning.call_args[0][2], expected)
                self._assert_nothing_crashed()
                self._close_window(window)

    # --- 4. 処理中の SSH の切断 ---

    def test_an_ssh_drop_during_the_move_drops_the_answer(self):
        """normalize の最中に機器が SSH を切っても、答えは捨てられ、切断の知らせは
        重ならず、ロックが放され、スレッドが終わること。"""
        for label, answer in (("SSH が切れると normalize も終わる", None),
                              ("答えが切断のあとに届く", SUB_REAL),
                              ("期限切れが切断のあとに届く", TimeoutError())):
            with self.subTest(label):
                window = self._open_window()
                mgr = window.sftp_managers["sw1"]
                notes = self._notes(mgr)
                start = len(self.movers)
                self._start_move(window, answer)

                self.device.close_last_shell()
                self.assertTrue(self._pump(lambda: "sw1" not in window.sftp_managers,
                                           seconds=10), "前提: 切断の後始末が走らない")

                self._drop_and_check(window, mgr, notes, start)
                self._close_window(window)

    # --- 5. 繋ぎ直したあとに古い答えが届く ---

    def test_a_stale_answer_after_reconnecting_changes_nothing(self):
        """接続 A で移動を始めて切断し、新しい接続 B（新しいマネージャ）に繋ぎ直した
        あとで A の答え・期限切れ・断りが届いても、B の場所・一覧を変えず、B で一覧を
        頼まず、どちらのマネージャからも知らせを出さないこと。"""
        from PyQt6.QtCore import QCoreApplication, QEvent
        for label, answer in (("答え", SUB_REAL), ("期限切れ", TimeoutError()),
                              ("機器が断る", IOError("Failure"))):
            with self.subTest(label):
                window = self._open_window()
                old = window.sftp_managers["sw1"]
                old_notes = self._notes(old)
                start = len(self.movers)
                gate, entered = self._stick(old.sftp_client, answer)
                self._open_sub(window.sftp_panel)
                self.assertTrue(entered.wait(_WAIT), "前提: normalize が始まらない")

                self.device.close_last_shell()
                self.assertTrue(self._pump(lambda: "sw1" not in window.sftp_managers,
                                           seconds=10), "前提: 切断の後始末が走らない")
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
                window._reconnect_device("sw1")
                panel = window.sftp_panel
                self.assertTrue(self._pump(
                    lambda: window.sftp_managers.get("sw1") not in (None, old)
                    and self._rows(panel) == ROWS
                    and panel.path_label.text() == "sw1: " + HOME, seconds=20),
                    "前提: 繋ぎ直した B の一覧が出ない")
                new = window.sftp_managers["sw1"]
                new_notes = self._notes(new)
                self._pump(seconds=0.2)

                def state():
                    return (panel.path_label.text(), self._rows(panel),
                            panel.status_label.text(), new.current_path,
                            new._listing_seq, list(self.device.listed))
                before = state()
                gate.set()
                self.assertTrue(self._finish_moves(), "移動のスレッドが終わらない")
                self._pump(seconds=0.5)

                self.assertEqual(state(), before, "古い答えで B の画面・一覧が変わった")
                self.assertEqual(old_notes.errors, [], "古い答えの知らせを出した")
                self.assertEqual(new_notes.errors, [], "B に知らせが出た")
                self.assertEqual(new_notes.gone, [], "B が畳まれた")
                self.assertTrue(new.is_connected)
                self.assertEqual(len(self.movers) - start, 1, "前提: normalize の回数")
                self.assertFalse(old._sftp_lock.locked(), "A のロックが放されていない")
                self.panel_box.warning.assert_not_called()
                self._assert_nothing_crashed()
                self._close_window(window)


class MoveFailureKindsTest(unittest.TestCase):
    """失敗の種類ごとの扱いが 9fee4af（GUI スレッドで同期に呼んでいた版）と同じこと"""

    # (名前, normalize の失敗, チャンネルが閉じているか, 知らせ, 畳むか)
    CASES = [
        ("期限切れ", TimeoutError(), False,
         "ディレクトリ変更エラー: 機器が30秒応答しません。SFTP接続を切断しました。"
         "接続し直してください", True),
        ("見つからない", IOError(2, "No such file"), False,
         "ディレクトリ変更エラー: [Errno 2] No such file", False),
        ("切断", EOFError(), False,
         "ディレクトリ変更エラー: EOFError。SFTP接続を切断しました。接続し直してください",
         True),
        ("チャンネルが閉じた", OSError("Socket is closed"), True,
         "ディレクトリ変更エラー: Socket is closed。SFTP接続を切断しました。"
         "接続し直してください", True),
    ]

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._threads_before = set(threading.enumerate())
        self.managers = []
        self.addCleanup(self._teardown)

    def _teardown(self):
        deadline = time.monotonic() + _WAIT
        while time.monotonic() < deadline and any(
                t.is_alive()
                for t in set(threading.enumerate()) - self._threads_before):
            self.app.processEvents()
            time.sleep(0.01)
        self._pump(seconds=0.2)
        for m in self.managers:
            for name in ("error_occurred", "disconnected"):
                try:
                    getattr(m, name).disconnect()
                except (TypeError, RuntimeError):
                    pass

    def _pump(self, check=lambda: False, seconds=3.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _manager(self, error, channel_closed):
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        _KEEP.append(m)
        self.managers.append(m)
        m.is_connected = True
        m.current_path = HOME
        client = m.sftp_client = mock.Mock()
        client.get_channel.return_value.closed = channel_closed
        client.normalize.side_effect = error
        return m, client, _Harness._notes(m)

    def test_each_kind_of_failure_is_handled_as_before(self):
        """期限切れ・見つからない・切断・チャンネルの閉鎖で、知らせの文面・回数と
        切断するかどうかが 9fee4af と同じこと。"""
        for label, error, closed, expected, folds in self.CASES:
            with self.subTest(label):
                m, client, notes = self._manager(error, closed)

                m.change_directory(SUB)
                self.assertTrue(self._pump(lambda: notes.errors), "失敗が知らされない")
                self._pump(seconds=0.3)

                self.assertEqual(notes.errors, [expected])
                self.assertEqual(len(notes.gone), 1 if folds else 0, "disconnected の回数")
                self.assertEqual(m.is_connected, not folds)
                self.assertEqual(m.current_path, HOME)
                client.listdir_attr.assert_not_called()
                self.assertFalse(m._sftp_lock.locked(), "ロックが放されていない")

    def test_a_worker_that_cannot_start_releases_the_lock(self):
        """移動のスレッドを作れないときは、ロックを放して『ディレクトリ変更エラー』を
        知らせること（持ったままだと以後の操作がすべて『転送中』で断られる）。"""
        import core.sftp_manager as sftp_module

        class _NoThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        class _ThreadingWithoutThreads:
            """sftp_manager から見た threading（ほかのモジュールには触らない）"""
            Thread = _NoThread

            def __getattr__(self, name):
                return getattr(threading, name)

        m, client, notes = self._manager(None, False)
        with mock.patch.object(sftp_module, "threading", _ThreadingWithoutThreads()):
            m.change_directory(SUB)
        self._pump(lambda: notes.errors, seconds=1.0)

        self.assertEqual(notes.errors, ["ディレクトリ変更エラー: can't start new thread"])
        self.assertEqual(notes.gone, [])
        self.assertTrue(m.is_connected)
        self.assertFalse(m._sftp_lock.locked(), "ロックが放されていない")
        client.normalize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
