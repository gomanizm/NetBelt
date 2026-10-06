"""内蔵 SFTP サーバーを起動したとき、前の接続の終了処理中のファイルが残っていれば、
その数をパネルのログにも 1 行出すこと。

利用者の決定（2026-10-06）: 残っているファイル数はパネルのログにも出す。
「前の接続の終了処理中で、その分の枠を使用中」と分かる文面にし、起動時に一度だけ
出す。

何が起きていたか（基準 09ecc14）: 停止したサーバーの接続が開いていたファイルは、
実際に閉じるまで開いているファイルの数（_OpenFiles）に残る（e42f451）。起動した
ときに残っていれば、件数を診断の行（print。exe ではログファイル）に出すだけで、
パネルのログには何も出なかった。その間は新しい open が上限で断られうるのに、
画面からは理由が分からない。

どう直したか: start() が数えた件数を _run_server へ渡し、started の後で
client_activity として 1 行出す（0 なら出さない）。起動のたびに 1 回だけで、
その後に枠が空いていっても出し直さない。配送待ちが上限でも省かない。

確かめ方: 本物のパネル（SFTPServerPanel）とサーバーを 127.0.0.1 で動かし、
パネルのログ欄の行を数える。前の接続のファイルは、旧セッションの close を止めて
残す（共有フォルダで close が詰まる形）。行が届き終えたことは、同じ待受スレッドが
後から出す接続の知らせ（つないですぐ切る TCP 接続）で確かめる（同じスレッドから
同じ受け手へのキュー配送は、出した順に届く）。
"""
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

USER = "tester"
PASSWORD = "example-pass"
# 開いておけるファイルの上限（1 セッションあたり・サーバー全体）を小さくする
PER_SESSION = 3
TOTAL = 5
# 状態の変化と応答を待つ上限（秒）。遅い CI を見込む
WAIT_SECONDS = 30.0
REPLY_TIMEOUT = 60.0
# 待受の accept は 1 秒ごとに抜けて停止を見る。枠が 1 個空くたびに、その周回や
# 閉じたスレッドから出し直していないかを見るために待つ秒数（1 周より長く）
TICK_WAIT_SECONDS = 1.5

STARTED = "サーバー起動: ポート"
CONNECTED = "クライアント接続: 127.0.0.1"
DISCONNECTED = "クライアント切断: 127.0.0.1"
NOTICE = "前の接続の終了処理中"
NOTICE_RE = re.compile(
    r"^\[\d\d:\d\d:\d\d\] 前の接続の終了処理中のファイルが (\d+) 個あり、"
    r"その分の枠を使用中です（開いておけるファイルはサーバー全体で (\d+) 個まで。"
    r"閉じ終われば空きます）$")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _CloseGate:
    """paramiko.SFTPHandle.close を、開いているファイルなら許しが出るまで止める。

    共有フォルダで close が詰まる形を模す。止めるのは記述子を閉じる前で、
    止めるのはこのテストのルートの下のファイルだけ（差し替えはプロセス全体に
    効くので、前のテストのサーバーが後始末で閉じるファイルはそのまま閉じる）。
    閉じ終えたファイル（paramiko の後始末の 2 回目の close）は止めずに通す
    """

    def __init__(self, root):
        import paramiko
        self._real_close = paramiko.SFTPHandle.close
        prefix = os.path.normcase(os.path.join(os.path.realpath(root), ""))
        self._mine = lambda path: os.path.normcase(
            os.fspath(path)).startswith(prefix)
        self._permits = threading.Semaphore(0)
        self._opened = threading.Event()
        self._lock = threading.Lock()
        self.waiting = 0

    def close(self, handle):
        path = getattr(handle, "_real_path", None)
        f = getattr(handle, "readfile", None) or getattr(handle, "writefile", None)
        if (path is not None and self._mine(path) and f is not None
                and not f.closed and not self._opened.is_set()):
            with self._lock:
                self.waiting += 1
            try:
                # 許し（allow）か open() でだけ通す（時間切れでは通さない）
                while not self._permits.acquire(timeout=1.0):
                    if self._opened.is_set():
                        break
            finally:
                with self._lock:
                    self.waiting -= 1
        return self._real_close(handle)

    def allow(self, count):
        """止まっている（これから止まる）close を count 個通す"""
        self._permits.release(count)

    def open(self):
        """これからの close を止めず、止まっている分もすべて通す"""
        self._opened.set()
        self._permits.release(100000)


class EarlierSessionFilesPanelNoticeTest(unittest.TestCase):
    # パネル（とマネージャ）はクラスの終わりまで持つ。キューに残った信号の
    # 配送先を先に解放すると、配送のときにプロセスごと落ちる
    # （test_server_start_failure と同じ）
    _panels = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        import paramiko
        cls.app = QApplication.instance() or QApplication([])
        cls.host_key = paramiko.RSAKey.generate(2048)

    @classmethod
    def tearDownClass(cls):
        for _ in range(3):
            cls.app.processEvents()
        cls._panels.clear()

    def setUp(self):
        import ui.sftp_server_panel as panel_mod
        from core.sftp_server import SFTPServerManager
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        patchers = [
            mock.patch("core.config_manager.app_data_dir", return_value=data_dir),
            # 上限はマネージャを作るときに読む
            mock.patch.object(SFTPServerManager, "MAX_OPEN_FILES_PER_SESSION",
                              PER_SESSION),
            mock.patch.object(SFTPServerManager, "MAX_OPEN_FILES_TOTAL", TOTAL),
        ]
        # モーダルは開かせずに数える（出してよいのは起動の失敗のエラーだけ）
        self.modals = []
        for kind in ("critical", "warning", "information"):
            patchers.append(mock.patch.object(
                panel_mod.QMessageBox, kind,
                side_effect=lambda *a, _k=kind, **kw: self.modals.append(_k)))
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-notice-")
        with open(os.path.join(self.root, "a.txt"), "wb") as f:
            f.write(b"abc")
        self.port = None

    # --- 道具 ---------------------------------------------------------------

    def _panel(self):
        from ui.sftp_server_panel import SFTPServerPanel
        panel = SFTPServerPanel()
        self._panels.append(panel)
        m = panel.sftp_server
        m.host_key = self.host_key
        m.STOP_TIMEOUT_SECONDS = 1.0
        # 後片付け（逆順に動く）: 止める → 前の接続のファイルが閉じ終わるのを
        # 待つ → 配送待ちの通知を処理する
        self.addCleanup(self._pump_for, 0.0)
        self.addCleanup(self._wait, lambda: m._open_files._in_use == 0)
        self.addCleanup(m.stop)
        panel.root_dir_edit.setText(self.root)
        panel.username_edit.setText(USER)
        panel.password_edit.setText(PASSWORD)
        return panel

    def _wait(self, predicate, seconds=WAIT_SECONDS):
        """predicate が真になるまで、Qt の配送を回しながら待つ"""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        self.app.processEvents()
        return bool(predicate())

    def _pump_for(self, seconds):
        """seconds 秒のあいだ Qt の配送を回す"""
        self._wait(lambda: False, seconds)

    @staticmethod
    def _lines(panel):
        return panel.log_text.toPlainText().splitlines()

    def _count(self, panel, text):
        return sum(text in line for line in self._lines(panel))

    def _notices(self, panel):
        return [line for line in self._lines(panel) if NOTICE in line]

    def _start(self, panel):
        """パネルの起動の処理で起動し、「サーバー起動」の行が届くまで待つ"""
        started = self._count(panel, STARTED)
        for _ in range(5):
            # 調べたポートを別のプロセスが先に取ることがある（排他の待受なので
            # 起動が失敗し、エラーのモーダルを出す）。そのときは別のポートで
            # 起動し直す
            self.port = free_port()
            panel.port_spin.setValue(self.port)
            panel._on_start_server()
            if not panel.stop_btn.isHidden():
                break
            self.modals.clear()
        else:
            self.fail("サーバーが起動しない")
        self.assertTrue(
            self._wait(lambda: self._count(panel, STARTED) == started + 1),
            "「サーバー起動」の行が届かない")

    def _settle(self, panel):
        """待受スレッドがここまでに出した知らせを、パネルが処理し終えるまで待つ。

        つないですぐ切る TCP 接続の「クライアント接続」は、待受スレッドが
        started と前の接続の行より後に出す。同じスレッドから同じ受け手への
        キュー配送は出した順に届くので、これが届けばその前の行も届いている
        """
        connected = self._count(panel, CONNECTED)
        disconnected = self._count(panel, DISCONNECTED)
        with socket.create_connection(("127.0.0.1", self.port), timeout=10):
            self.assertTrue(
                self._wait(lambda: self._count(panel, CONNECTED) == connected + 1),
                "接続の知らせが届かない")
        self.assertTrue(
            self._wait(lambda: self._count(panel, DISCONNECTED) > disconnected),
            "切断の知らせが届かない")

    def _gate(self):
        """paramiko.SFTPHandle.close を _CloseGate で止める（後片付けで全部通す）"""
        import paramiko
        gate = _CloseGate(self.root)
        p = mock.patch.object(paramiko.SFTPHandle, "close",
                              lambda handle: gate.close(handle))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(gate.open)
        return gate

    def _session(self):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("127.0.0.1", port=self.port, username=USER, password=PASSWORD,
                  allow_agent=False, look_for_keys=False, timeout=10)
        self.addCleanup(c.close)
        s = c.open_sftp()
        s.get_channel().settimeout(REPLY_TIMEOUT)
        self.addCleanup(s.close)
        return s

    def _hold_slots(self, panel, count):
        """前の接続のファイルの代わりに、開いているファイルの枠を count 個埋める"""
        of = panel.sftp_server._open_files
        slots = [of.take_slot(object())[0] for _ in range(count)]
        self.assertNotIn(None, slots, "前提: 枠を埋められない")
        self.addCleanup(lambda: [slot() for slot in slots])
        return slots

    def _assert_counts(self, line, held):
        match = NOTICE_RE.match(line)
        self.assertIsNotNone(match, "文面が違う: %r" % line)
        self.assertEqual((int(match.group(1)), int(match.group(2))),
                         (held, TOTAL), "件数か上限が合わない: %r" % line)

    # --- テスト -------------------------------------------------------------

    def test_files_still_closing_at_start_are_told_once_in_the_panel_log(self):
        """前の接続のファイルが閉じ終わらないまま起動すると、件数を 1 回だけ出す。

        その後に枠が空いていっても出し直さない。閉じ終えた後の起動では出さない
        """
        panel = self._panel()
        of = panel.sftp_server._open_files
        self._start(panel)
        self._settle(panel)
        self.assertEqual(self._notices(panel), [], "残っていないのに出した")
        a, b = self._session(), self._session()
        held = [a.open("a.txt", "rb") for _ in range(3)]
        held.append(b.open("a.txt", "rb"))
        self.assertEqual(of._in_use, 4)
        gate = self._gate()
        panel._on_stop_server()
        self.assertTrue(self._wait(lambda: gate.waiting == 2),
                        "前提: 前の接続の後始末の close で止まらない")
        # 止めた後で 1 個だけ閉じ終える（件数を、止めた時点の 4 とも上限の 5 とも
        # 違う値にして、どれを出したかを見分ける）
        gate.allow(1)
        self.assertTrue(self._wait(lambda: of._in_use == 3),
                        "数が %d" % of._in_use)

        self._start(panel)
        self._settle(panel)

        notices = self._notices(panel)
        self.assertEqual(len(notices), 1,
                         "前の接続の行が 1 行でない: %r" % self._lines(panel))
        self._assert_counts(notices[0], 3)
        lines = self._lines(panel)
        last_started = max(i for i, line in enumerate(lines) if STARTED in line)
        self.assertGreater(lines.index(notices[0]), last_started,
                           "「サーバー起動」の行より先に出した: %r" % lines)
        self.assertEqual(self.modals, [], "モーダルを出した")

        # 枠が空いていっても出し直さない。1 個空くたびに、待受の周回（1 秒ごと）
        # と閉じたスレッドから届く分を待ってから数える
        for left in (2, 1, 0):
            gate.allow(1)
            self.assertTrue(self._wait(lambda left=left: of._in_use == left),
                            "数が %d" % of._in_use)
            self._pump_for(TICK_WAIT_SECONDS)
            self._settle(panel)
            self.assertEqual(len(self._notices(panel)), 1,
                             "枠が空いていく途中で出し直した（残り %d 個）: %r"
                             % (left, self._notices(panel)))

        # 閉じ終えた後の起動では出さない
        panel._on_stop_server()
        self._start(panel)
        self._settle(panel)
        self.assertEqual(len(self._notices(panel)), 1, "残っていないのに出した")
        self.assertEqual(self.modals, [])
        self.assertEqual(len(held), 4)

    def test_no_line_when_files_were_closed_before_the_stop(self):
        """開いて閉じ終えたファイルだけなら、停止・起動しても出さない"""
        panel = self._panel()
        of = panel.sftp_server._open_files
        self._start(panel)
        self._settle(panel)
        s = self._session()
        for _ in range(3):
            s.open("a.txt", "rb").close()
        self.assertTrue(self._wait(lambda: of._in_use == 0),
                        "前提: 閉じたファイルの数が戻らない: %d" % of._in_use)
        panel._on_stop_server()
        self._start(panel)
        self._settle(panel)
        self.assertEqual(self._count(panel, STARTED), 2)
        self.assertEqual(self._notices(panel), [], "残っていないのに出した")
        self.assertEqual(self.modals, [])

    def test_no_line_when_the_start_fails(self):
        """起動に失敗したときは出さない（同じ状態で起動できたときだけ出す）。

        残りは境界の 1 個にする（1 個でも残っていれば出す）
        """
        panel = self._panel()
        self._hold_slots(panel, 1)
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(("0.0.0.0", 0))
        blocker.listen(5)
        port = blocker.getsockname()[1]
        panel.port_spin.setValue(port)
        self.assertEqual(panel.port_spin.value(), port, "前提: ポートを設定できない")
        panel._on_start_server()
        self.assertTrue(panel.stop_btn.isHidden(), "前提: 起動が失敗していない")
        self.assertEqual(self.modals, ["critical"],
                         "前提: 起動の失敗をエラーで知らせていない")
        self._pump_for(0.5)
        self.assertEqual(self._notices(panel), [], "起動に失敗したのに出した")

        self.modals.clear()
        self._start(panel)
        self._settle(panel)
        notices = self._notices(panel)
        self.assertEqual(len(notices), 1, "起動できたのに出さない: %r"
                         % self._lines(panel))
        self._assert_counts(notices[0], 1)
        self.assertEqual(self.modals, [])

    def test_line_is_kept_while_notices_are_backed_up(self):
        """配送待ちの通知が上限まで溜まっていても、この行は省かずに出す"""
        panel = self._panel()
        m = panel.sftp_server
        self._hold_slots(panel, 2)
        # GUI が他の処理で塞がり、配送待ちの通知が上限まで溜まっている状態
        with m._notice_lock:
            m._pending_notices = m.max_pending_notices
        self.addCleanup(setattr, m, "_pending_notices", 0)
        self._start(panel)
        self.assertTrue(self._wait(lambda: len(self._notices(panel)) == 1),
                        "配送待ちが上限のときに省いた: %r" % self._lines(panel))
        self._pump_for(0.5)
        notices = self._notices(panel)
        self.assertEqual(len(notices), 1)
        self._assert_counts(notices[0], 2)
        self.assertEqual(m._dropped_notices, 0, "省いた通知に数えた")
        # 出す前に配送待ちへ 1 件数え、届いた時点で 1 件戻るので元の数に戻る
        # （この試験では接続を作らないので、ほかの通知は混ざらない）
        self.assertEqual(m._pending_notices, m.max_pending_notices,
                         "配送待ちの数がずれた")
        self.assertEqual(self.modals, [])


if __name__ == "__main__":
    unittest.main()
