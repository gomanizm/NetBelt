"""あとの移動に追い越された自動更新が、その移動の一覧より先に通信のロックを
取っても、同じ失敗を重ねて知らせないことを検証する（テストの穴を埋める）。

実測（157b833。本体は正しく、テストが守っていなかった）:
  自動更新（_refresh_listing）は、通信のロックを取った時点でまだ最新の要求の
  ときだけ場所を選び直す（src/core/sftp_manager.py:424 の
  if seq == self._listing_seq:）。この確かめを if True: にしても、SFTP
  クライアントに関わるテスト 86 ファイル（522 件）はすべて通った。外すと、
  /B への移動の途中で頼まれた自動更新 R1 が、続く読めない /C への移動に
  追い越されたうえで /C の一覧より先にロックを取ったとき、R1 は控えの /C を
  選び直して頼む。/C の一覧も失敗するので、「ディレクトリ一覧取得エラー:
  Permission denied」が 2 件（パネルでは警告のモーダルが 2 枚）出る。頼んだ
  順は ['/B', '/C', '/C']（157b833 は ['/B', '/B', '/C'] で 1 件）。
  CPython の Windows のロックはほぼ先着順なので、ロックの代わりを差し込んで
  順を強制する。

守り方:
  その並びを作り、失敗が 1 件で、/C を 1 回しか頼まないことを確かめる。
"""
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

_DENIED = "ディレクトリ一覧取得エラー: Permission denied"


class _Attr:
    def __init__(self, name):
        self.filename = name
        self.st_mode = 0o100644
        self.st_size = 1
        self.st_mtime = 0


class _OrderedLock:
    """_sftp_lock の代わり。armed の間、背景スレッドが初めてロックを取りに
    来たところで止め、テストが grant した順にだけ進ませる。

    同じスレッドの 2 回目からは止めない（放して取り直すのは本体の都合）。
    GUI スレッド（テストのスレッド）は止めない。
    """

    def __init__(self):
        self._inner = threading.Lock()
        self._cond = threading.Condition()
        self._gui = threading.current_thread()
        self.armed = False
        self.workers = []       # 取りに来た背景スレッド（来た順）
        self.held = []          # 止めたスレッド（来た順）
        self._granted = set()
        self._acquired = set()
        self._opened = False

    def acquire(self, blocking=True, timeout=-1):
        me = threading.current_thread()
        if me is not self._gui:
            with self._cond:
                first = me not in self.workers
                if first:
                    self.workers.append(me)
                if first and self.armed:
                    self.held.append(me)
                    self._cond.notify_all()
                    self._cond.wait_for(
                        lambda: self._opened or me in self._granted, 5)
        got = self._inner.acquire(blocking, timeout)
        if got and me is not self._gui:
            with self._cond:
                self._acquired.add(me)
                self._cond.notify_all()
        return got

    def release(self):
        self._inner.release()

    def locked(self):
        return self._inner.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def wait_held(self, count):
        """止めたスレッドが count 本になるまで待ち、count 本目を返す"""
        with self._cond:
            if not self._cond.wait_for(lambda: len(self.held) >= count, 5):
                return None
            return self.held[count - 1]

    def grant(self, thread):
        with self._cond:
            self._granted.add(thread)
            self._cond.notify_all()

    def wait_acquired(self, thread):
        with self._cond:
            return self._cond.wait_for(lambda: thread in self._acquired, 5)

    def open_all(self):
        with self._cond:
            self.armed = False
            self._opened = True
            self._cond.notify_all()


class RefreshWaitsForPendingMoveTest(unittest.TestCase):
    _keep = []

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _pump(self, check=lambda: False, seconds=1.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if check():
                return True
            time.sleep(0.01)
        return False

    def _manager(self, gates):
        """/A にいる SFTPManager。gates の場所の一覧は、その Event まで
        （通信のロックを持ったまま）機器の答えを待つ"""
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        type(self)._keep.append(m)
        lock = m._sftp_lock = _OrderedLock()
        self.addCleanup(lock.open_all)
        for gate in gates.values():
            self.addCleanup(gate.set)
        m.is_connected = True
        m.current_path = "/A"
        c = m.sftp_client = mock.Mock()
        c.get_channel.return_value.closed = False
        c.normalize.side_effect = lambda p: p
        self.asked = []
        self._asked_lock = threading.Lock()

        def listdir_attr(path):
            with self._asked_lock:
                self.asked.append(path)
            gate = gates.get(path)
            if gate is not None:
                gate.wait(5)
            if path == "/C":
                raise PermissionError("Permission denied")
            if path == "/B":
                return [_Attr("in-B.cfg")]
            return [_Attr("in-A.cfg"), _Attr("new.cfg")]
        c.listdir_attr.side_effect = listdir_attr
        self.lists, self.errors = [], []
        m.file_list_ready.connect(
            lambda l: self.lists.append([e["name"] for e in l]))
        m.error_occurred.connect(self.errors.append)
        return m, lock

    def _wait_asked(self, path):
        deadline = time.time() + 5
        while time.time() < deadline:
            with self._asked_lock:
                if path in self.asked:
                    return True
            time.sleep(0.01)
        return False

    def _settle(self, lock):
        """一覧のスレッドがすべて終わり、届いた知らせを処理し終えるまで待つ"""
        deadline = time.time() + 5
        while (time.time() < deadline
               and any(t.is_alive() for t in list(lock.workers))):
            self.app.processEvents()
            time.sleep(0.01)
        self._pump(seconds=0.3)

    def test_an_overtaken_refresh_taking_the_lock_first_waits_for_the_move(self):
        """/B への移動の途中で頼まれた自動更新が、続く読めない /C への移動に
        追い越されたうえで、/C の一覧より先にロックを取っても、失敗は 1 件で
        あること。"""
        b_gate = threading.Event()
        m, lock = self._manager({"/B": b_gate})
        m.change_directory("/B")        # 利用者の移動。一覧はロックの中で止まる
        self.assertTrue(self._wait_asked("/B"), "前提: /B の一覧が始まらない")
        lock.armed = True
        m._refresh_listing()            # R1（/B の一覧が届く前の自動更新）
        refresh = lock.wait_held(1)
        self.assertIsNotNone(refresh, "前提: R1 がロックを取りに来ない")
        b_gate.set()                    # /B の一覧が返り、ロックが空く
        m.change_directory("/C")        # 続けて読めない /C へ。R1 を追い越す
        move = lock.wait_held(2)
        self.assertIsNotNone(move, "前提: /C の一覧がロックを取りに来ない")
        lock.grant(refresh)             # 追い越された R1 が先にロックを取る
        self.assertTrue(lock.wait_acquired(refresh), "前提: R1 がロックを取らない")
        lock.grant(move)
        self._settle(lock)
        self.assertEqual(self.errors, [_DENIED],
                         "同じ失敗が重ねて知らされた（頼んだ順 %r）" % self.asked)
        self.assertEqual(self.asked.count("/C"), 1, self.asked)


if __name__ == "__main__":
    unittest.main()
