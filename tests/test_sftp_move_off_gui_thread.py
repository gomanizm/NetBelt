"""SFTP クライアントのディレクトリ移動が、機器の応答を GUI スレッドで待たないことを検証する。

実測（基準 9fee4af。Codex レビュー R05）:
  移動（ホーム／親へ／ダブルクリック／メニューの「開く」）は
  SFTPManager.change_directory を通り、normalize（REALPATH）を GUI スレッドで
  同期で呼んでいた。機器の答えが 1.0 秒遅いと、イベントループを回したまま
  「ホーム」を押したあいだ、10ms の心拍が 1.0 秒途切れた（他の SSH タブの
  描画・入力・切断・終了も同じだけ止まる）。黙る機器では期限の 30 秒止まる。
  一覧・転送・接続は、もともとワーカースレッドで動いている。

直し方:
  ロックの取得は今どおり GUI スレッドで _acquire_for_gui（0.5 秒）で行い、
  転送中なら断る。取れたロックを持ったまま normalize だけを使い捨ての
  スレッドで行い、そのスレッドから正規化後のパスで list_directory を呼ぶ。
  一覧の控えはロックを放す前に置く（移動と自動更新の順序を守る）。結果が
  届いた時点で同じ接続のままでなければ、一覧も失敗の知らせも出さない。

測り方の注意:
  スロットをイベントループの外から呼ぶと、遅れが無くても心拍は 0 回になり
  根拠にならない。QEventLoop を回し、その中で QAction.trigger() で起動する。
  CI は遅く、GC で 0.26 秒ほど止まることがあるので、遅れ 1.0 秒に対して
  途切れ 0.5 秒未満を合格にする。

後始末:
  失敗したまま残った配送待ちの知らせが、次のテストファイルの処理中に届いて
  プロセスごと落ちたことがある（R05 の検査役）。止めた通信は必ず放し、
  スレッドの終わりと知らせの処理を待ってから結び付きを外す。
"""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

_DELAY = 1.0      # 機器の答えの遅れ（秒）
_MAX_GAP = 0.5    # 許す心拍の途切れ（秒）
_BUSY = "転送中のためディレクトリ移動を実行できません。完了してからやり直してください。"


class _Attr:
    """listdir_attr の 1 件（ディレクトリ）"""

    def __init__(self, name):
        self.filename = name
        self.st_mode = 0o040755
        self.st_size = 0
        self.st_mtime = 0


class _ReleaseSpy:
    """_sftp_lock の代わり。放した時点の行き先の控えを、放したスレッドごとに残す"""

    def __init__(self, manager):
        self._inner = threading.Lock()
        self._manager = manager
        self.released = []

    def acquire(self, blocking=True, timeout=-1):
        return self._inner.acquire(blocking, timeout)

    def release(self):
        self.released.append((threading.current_thread(),
                              self._manager._listing_path))
        self._inner.release()

    def locked(self):
        return self._inner.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class SftpMoveOffGuiThreadTest(unittest.TestCase):
    # 配送待ちの知らせが残っても、送り手・受け手ごと GC されないように持つ
    _keep = []

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from ui import sftp_panel as panel_mod
        # 警告は開かない。後始末のあいだに届いても開かないよう、最後に外す
        patcher = mock.patch.object(panel_mod.QMessageBox, "warning")
        self.warnings = patcher.start()
        self.addCleanup(patcher.stop)
        self.gate = threading.Event()     # 機器の答えを止める
        self.threads = []                 # normalize が呼ばれたスレッド
        self.managers, self.panels = [], []
        self._threads_before = set(threading.enumerate())
        self.addCleanup(self._teardown)

    def _teardown(self):
        """止めた通信を放し、スレッドの終わりと知らせの処理を待ってから外す"""
        self.gate.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
                t.is_alive()
                for t in set(threading.enumerate()) - self._threads_before):
            self.app.processEvents()
            time.sleep(0.01)
        self._pump(seconds=0.2)
        for panel in self.panels:
            panel.clear()
            panel.close()
        for m in self.managers:
            for name in ("error_occurred", "disconnected", "file_list_ready"):
                try:
                    getattr(m, name).disconnect()
                except (TypeError, RuntimeError):
                    pass    # 繋がっていない / 既に破棄済み

    def _pump(self, check=lambda: False, seconds=3.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _manager(self, path="/home/sub"):
        """path にいる SFTPManager（機器は Mock）。normalize は '.' をホームにする"""
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        type(self)._keep.append(m)
        self.managers.append(m)
        m.is_connected = True
        m.current_path = path
        c = self.client = m.sftp_client = mock.Mock()
        c.get_channel.return_value.closed = False
        self.asked = []

        def listdir_attr(where):
            self.asked.append(where)
            return [_Attr("sub")]
        c.listdir_attr.side_effect = listdir_attr

        def normalize(where):
            self.threads.append(threading.current_thread())
            return "/home" if where == "." else where
        c.normalize.side_effect = normalize
        self.errors, self.gone, self.lists = [], [], []
        m.error_occurred.connect(self.errors.append)
        m.disconnected.connect(lambda: self.gone.append(True))
        m.file_list_ready.connect(
            lambda _l, m=m: self.lists.append(m.get_current_path()))
        return m

    def _panel(self, m):
        """m を表示し、最初の一覧が届いた SFTP パネル"""
        from ui.sftp_panel import SFTPPanel
        panel = SFTPPanel()
        type(self)._keep.append(panel)
        self.panels.append(panel)
        panel.set_sftp_manager(m, "rtrA")
        self.assertTrue(self._pump(lambda: self.lists), "前提: 最初の一覧が届かない")
        self.asked.clear()
        self.lists.clear()
        return panel

    @staticmethod
    def _actions(panel):
        from PyQt6.QtGui import QAction
        return {a.text(): a for a in panel.findChildren(QAction)}

    def _moved_to(self, where):
        return self._pump(lambda: where in self.lists)

    # --- normalize が GUI スレッドの外で動く ---

    def test_every_way_to_move_asks_the_device_off_the_gui_thread(self):
        """ダブルクリック・「開く」・親へ・ホームのどれでも、normalize が GUI
        スレッドの外で呼ばれ、移動先の一覧が届くこと。"""
        from PyQt6.QtCore import Qt
        m = self._manager("/home/sub")
        panel = self._panel(m)
        actions = self._actions(panel)
        info = panel.model.item(0, 0).data(Qt.ItemDataRole.UserRole)
        self.assertTrue(info and info["is_dir"], "前提: ディレクトリの行が無い")

        ways = [
            ("ダブルクリック", lambda: panel._on_item_double_clicked(
                panel.model.index(0, 0)), "/home/sub/sub"),
            ("開く", lambda: panel._open_directory(
                panel.model.item(0, 0).data(Qt.ItemDataRole.UserRole)),
             "/home/sub/sub/sub"),
            ("親へ", actions["↑ 親へ"].trigger, "/home/sub/sub"),
            ("ホーム", actions["ホーム"].trigger, "/home"),
        ]
        for label, move, where in ways:
            with self.subTest(label):
                self.threads.clear()
                self.lists.clear()
                move()
                self.assertTrue(self._moved_to(where),
                                "%s: 移動先 %s の一覧が届かない（届いた %r、"
                                "知らせ %r）" % (label, where, self.lists,
                                                self.errors))
                self.assertEqual(len(self.threads), 1,
                                 "%s: normalize の回数" % label)
                self.assertIsNot(self.threads[0], threading.main_thread(),
                                 "%s: normalize が GUI スレッドで呼ばれた" % label)
        self.assertEqual(self.errors, [])

    def test_the_heartbeat_keeps_going_while_the_device_is_slow(self):
        """機器の答えが 1.0 秒遅くても、ホームを押したあいだ GUI の心拍が
        途切れないこと（イベントループを回したまま測る）。"""
        from PyQt6.QtCore import QEventLoop, QTimer
        m = self._manager("/home/sub")
        panel = self._panel(m)
        home = self._actions(panel)["ホーム"]

        def slow_normalize(where):
            self.threads.append(threading.current_thread())
            self.gate.wait(_DELAY)          # 機器が _DELAY 秒遅れて答える
            return "/home"
        self.client.normalize.side_effect = slow_normalize

        ticks, pressed = [], {}
        loop = QEventLoop()
        heartbeat = QTimer()
        heartbeat.setInterval(10)
        heartbeat.timeout.connect(lambda: ticks.append(time.monotonic()))
        watcher = QTimer()
        watcher.setInterval(10)
        watcher.timeout.connect(
            lambda: loop.quit() if "/home" in self.lists else None)
        limit = QTimer()
        limit.setSingleShot(True)
        limit.setInterval(int((_DELAY + 3.0) * 1000))
        limit.timeout.connect(loop.quit)
        press = QTimer()
        press.setSingleShot(True)
        press.setInterval(100)

        def on_press():
            pressed["at"] = time.monotonic()
            home.trigger()                  # スロットはイベントループの中から起動する
            pressed["back"] = time.monotonic()
        press.timeout.connect(on_press)

        for timer in (heartbeat, watcher, limit, press):
            timer.start()
        loop.exec()
        for timer in (heartbeat, watcher, limit, press):
            timer.stop()

        self.assertIn("at", pressed, "前提: ホームが押されていない")
        self.assertEqual(len(self.threads), 1, "前提: normalize が 1 回呼ばれていない")
        self.assertIn("/home", self.lists,
                      "ホームの一覧が届かない（知らせ %r）" % self.errors)
        window = ([t for t in ticks if t < pressed["at"]][-1:]
                  + [t for t in ticks if t >= pressed["at"]])
        self.assertGreaterEqual(window[-1] - pressed["at"], _DELAY * 0.9,
                                "前提: 機器が遅れているあいだを測れていない")
        gaps = [b - a for a, b in zip(window, window[1:])]
        self.assertLess(max(gaps), _MAX_GAP,
                        "機器を待つあいだ GUI が %.3f 秒止まった（スロットが戻るまで "
                        "%.3f 秒）" % (max(gaps), pressed["back"] - pressed["at"]))

    # --- 守ること ---

    def test_a_move_during_a_transfer_is_still_refused_in_half_a_second(self):
        """転送がロックを持っている間の移動は、これまでどおり 0.5 秒で断ること。"""
        m = self._manager("/home/sub")
        holding, done = threading.Event(), threading.Event()
        self.addCleanup(done.set)

        def transfer():
            with m._sftp_lock:
                holding.set()
                done.wait(5)
        threading.Thread(target=transfer, daemon=True).start()
        self.assertTrue(holding.wait(5), "前提: 転送がロックを取らない")

        started = time.monotonic()
        m.change_directory("/flash")
        took = time.monotonic() - started
        self._pump(seconds=0.3)

        self.assertEqual(self.errors, [_BUSY])
        self.assertGreaterEqual(took, 0.4, "ロックを待たずに断った")
        self.assertLess(took, 1.5, "0.5 秒で諦めていない")
        self.client.normalize.assert_not_called()
        self.assertEqual(self.asked, [])

    def test_a_silent_device_still_folds_and_keeps_the_listing(self):
        """normalize の期限切れは、これまでどおり『ディレクトリ変更エラー』で
        知らせて SFTP を切断し、パネルの一覧は残すこと。"""
        m = self._manager("/home/sub")
        panel = self._panel(m)
        self.client.normalize.side_effect = TimeoutError()

        self._actions(panel)["ホーム"].trigger()
        self.assertTrue(self._pump(lambda: self.gone and self.errors),
                        "前提: 失敗が知らされない")
        self._pump(seconds=0.2)

        expected = ("ディレクトリ変更エラー: 機器が30秒応答しません。"
                    "SFTP接続を切断しました。接続し直してください")
        self.assertEqual(self.errors, [expected])
        self.assertEqual(len(self.gone), 1, "disconnected の回数")
        self.assertFalse(m.is_connected, "使えないチャンネルを掴んだまま")
        self.assertIsNone(m.sftp_client)
        self.assertEqual(panel.model.rowCount(), 1, "一覧の表示が消えた")
        self.assertTrue(panel.status_label.text().endswith(panel.DROPPED_TEXT),
                        panel.status_label.text())
        self.warnings.assert_called_once()
        self.assertEqual(self.warnings.call_args[0][2], expected)
        self.assertEqual(self.asked, [], "失敗した移動先を頼んだ")
        self.assertFalse(m._sftp_lock.locked(), "ロックが放されていない")

    def test_a_refresh_waiting_behind_the_move_follows_it(self):
        """normalize の最中に頼まれた自動更新は、移動先の控えを待って同じ場所を
        頼み、移動を取り消さないこと（控えはロックを放す前に置く）。"""
        m = self._manager("/home/sub")
        spy = m._sftp_lock = _ReleaseSpy(m)

        def normalize(where):
            self.threads.append(threading.current_thread())
            m._refresh_listing()            # 転送の完了などの自動更新
            return where
        self.client.normalize.side_effect = normalize

        m.change_directory("/flash")
        self.assertTrue(self._pump(lambda: len(self.asked) >= 2 and self.lists),
                        "一覧が揃わない（頼んだ順 %r）" % self.asked)
        self._pump(seconds=0.2)

        self.assertEqual(len(self.threads), 1, "前提: normalize の回数")
        mover = [path for thread, path in spy.released
                 if thread is self.threads[0]]
        self.assertEqual(mover[:1], ["/flash"],
                         "移動の控えがロックを放したあとに置かれた（放したとき %r）"
                         % spy.released)
        self.assertEqual(self.asked, ["/flash", "/flash"],
                         "自動更新が移動を待たずに場所を選んだ")
        self.assertEqual(m.current_path, "/flash")
        self.assertEqual(self.lists, ["/flash"])
        self.assertEqual(self.errors, [])

    def test_a_result_after_the_session_is_dropped_is_discarded(self):
        """normalize の最中に SFTP が畳まれたら、答えが届いても一覧を頼まず、
        失敗も重ねて知らせないこと（切断した側が知らせ済み）。"""
        for label, answer in (("答えが届く", lambda where: where),
                              ("期限切れ", None),
                              ("チャンネルが閉じた", OSError("Socket is closed"))):
            with self.subTest(label):
                self.gate.clear()
                m = self._manager("/home/sub")
                entered = threading.Event()

                def normalize(where, answer=answer):
                    entered.set()
                    self.gate.wait(5)
                    if answer is None:
                        raise TimeoutError()
                    if isinstance(answer, Exception):
                        raise answer
                    return answer(where)
                self.client.normalize.side_effect = normalize

                m.change_directory("/flash")
                self.assertTrue(entered.wait(5), "前提: normalize が呼ばれない")
                # MainWindow._drop_sftp_manager 相当。disconnect はロックを待つ
                # ので、機器の答えは別のスレッドから放す
                opener = threading.Timer(0.2, self.gate.set)
                opener.start()
                m.disconnect()
                opener.join(5)
                self._pump(seconds=0.5)

                self.assertEqual(self.errors, [], "畳んだあとに失敗を知らせた")
                self.assertEqual(len(self.gone), 1, "disconnected の回数")
                self.assertEqual(self.asked, [], "畳んだあとに一覧を頼んだ")
                self.assertFalse(m._sftp_lock.locked(), "ロックが放されていない")

    def test_a_manager_deleted_during_the_move_does_not_break_the_worker(self):
        """normalize が切断の待ちより長引き、そのあいだにマネージャが捨てられても、
        答えが届いたときにワーカーが例外で落ちないこと。"""
        from PyQt6 import sip
        from PyQt6.QtCore import QCoreApplication, QEvent
        m = self._manager("/home/sub")
        m._DISCONNECT_WAIT_SECONDS = 0.1
        entered, finished = threading.Event(), threading.Event()

        def normalize(where):
            entered.set()
            try:
                self.gate.wait(5)
                raise TimeoutError()
            finally:
                finished.set()
        self.client.normalize.side_effect = normalize
        crashes = []
        hook = mock.patch.object(threading, "excepthook",
                                 side_effect=lambda args: crashes.append(args))
        hook.start()
        self.addCleanup(hook.stop)

        m.change_directory("/flash")
        self.assertTrue(entered.wait(5), "前提: normalize が呼ばれない")
        # MainWindow._drop_sftp_manager 相当: ロックを待ちきれずに外して捨てる
        m.disconnect()
        m.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        self.assertTrue(sip.isdeleted(m), "前提: マネージャが破棄されていない")
        self.gate.set()
        self.assertTrue(finished.wait(5), "前提: normalize が戻らない")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and m._sftp_lock.locked():
            time.sleep(0.01)
        time.sleep(0.1)

        self.assertEqual([(a.exc_type, a.exc_value) for a in crashes], [],
                         "ワーカーが例外で落ちた")
        self.assertFalse(m._sftp_lock.locked(), "ロックが放されていない")


if __name__ == "__main__":
    unittest.main()
