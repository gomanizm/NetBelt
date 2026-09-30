"""移動の一覧が失敗する途中で頼まれた自動更新が、失敗した先を頼み直さず、
今の場所を取り直すことを検証する。

実測（b2858c4）:
  自動更新（_refresh_listing）は、最後に頼んだ一覧の場所（_listing_path）を、
  頼んだ時点で選んでいた。読めない /C への移動の一覧が機器の答えを待って
  いる（通信のロックの中）あいだにアップロードが終わると、自動更新も /C を
  選ぶ。/C の一覧が失敗しても、それはもう最新の要求ではないので控えは /C
  のまま残り、自動更新の /C も同じ理由で失敗する。頼んだ順は ['/C', '/C']、
  エラーは「ディレクトリ一覧取得エラー: Permission denied」が 2 件（パネルでは
  警告のモーダルが 2 枚）で、今いる /A の一覧は取り直されず、アップロード
  したファイルは表に出なかった。441ea02 では自動更新が current_path を頼むので、
  頼んだ順は ['/C', '/A']、エラーは 1 件で、/A の一覧が届いていた。

直し方（fa1bac5 で始め、d77b1a0・ca3217d で今の形になった）:
  行き先の控えは、自動更新ではない一覧の要求（移動・利用者の更新）だけが置き、
  置いた要求の番号も一緒に控える。一覧が失敗したら、その一覧が置いた控え
  （自動更新なら追った控え）がまだ控えのままのときだけ外す（あとに自動更新が
  続いていても外す。あとの移動が控えを置き換えていれば残す）。外すのは通信の
  ロックを放す前にする。自動更新の場所は、一覧のスレッドが通信のロックを
  取った時点でだけ選ぶ（控えがあればそこ、無ければ今の場所。
  _pick_refresh_path）。控えを置いた要求の一覧がまだ機器に頼まれていなければ、
  ロックを一度放してそれが済むのを待ってから選ぶので、先に頼んだ移動の成否は、
  選ぶときには分かっている。まだ届いていない移動を自動更新が取り消さない
  こと（1.3.2 の直し）はそのまま保つ。
  自動更新の場所を頼んだ時点で選ぶ形、番号より先に別のロック区間で選ぶ形、
  控えをロックを放したあとで外す形のどれに戻しても、このテストは落ちる
  （頼んだ順 ['/C', '/C']、エラー 2 件）。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _Attr:
    def __init__(self, name):
        self.filename = name
        self.st_mode = 0o100644
        self.st_size = 1
        self.st_mtime = 0


class _HandOffLock:
    """_sftp_lock の代わり。/C の一覧のスレッドが手を放したら、待っていた
    自動更新がロックを取るまで、そのスレッドを先へ進ませない。

    控えを外すのがロックを放したあとだと、自動更新は外れる前の /C を選ぶ。
    放す前に外していることを、スレッドの切り替わりの運に任せず確かめる。
    """

    def __init__(self):
        self._inner = threading.Lock()
        self.releaser = None
        self.taken_by_other = threading.Event()

    def acquire(self, blocking=True, timeout=-1):
        got = self._inner.acquire(blocking, timeout)
        if (got and self.releaser is not None
                and threading.current_thread() is not self.releaser):
            self.taken_by_other.set()
        return got

    def release(self):
        self._inner.release()
        if threading.current_thread() is self.releaser:
            self.taken_by_other.wait(2)

    def locked(self):
        return self._inner.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class RefreshAfterFailedPendingMoveTest(unittest.TestCase):
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

    def test_a_refresh_during_a_failing_move_lists_the_current_directory(self):
        """読めない /C への移動の途中で頼まれた自動更新が、/C ではなく /A を取り直すこと。"""
        from PyQt6.QtCore import Qt
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        type(self)._keep.append(m)
        lock = m._sftp_lock = _HandOffLock()
        m.is_connected = True
        m.current_path = "/A"
        c = m.sftp_client = mock.Mock()
        c.get_channel.return_value.closed = False
        c.normalize.side_effect = lambda p: p
        c.stat.side_effect = IOError("no such file")
        asked = []
        asked_lock = threading.Lock()
        c_gate = threading.Event()
        self.addCleanup(c_gate.set)
        self.addCleanup(lock.taken_by_other.set)

        def listdir_attr(path):
            with asked_lock:
                asked.append(path)
            if path == "/C":
                if lock.releaser is None:
                    lock.releaser = threading.current_thread()
                # 機器の答えを待つあいだ、通信のロックを持ったまま止まる
                c_gate.wait(5)
                raise PermissionError("Permission denied")
            return [_Attr("in-A.cfg"), _Attr("new.cfg")]
        c.listdir_attr.side_effect = listdir_attr
        lists, errors = [], []
        m.file_list_ready.connect(lambda l: lists.append([e["name"] for e in l]))
        m.error_occurred.connect(errors.append)

        # 転送スレッドの自動更新を頼み終えたところを知る
        refresh_asked = threading.Event()
        original_refresh = m._refresh_listing

        def spy_refresh():
            original_refresh()
            refresh_asked.set()
        m._refresh_listing = spy_refresh

        work = tempfile.mkdtemp(prefix="netbelt-refresh-failed-move-")
        self.addCleanup(shutil.rmtree, work, True)
        local = os.path.join(work, "new.cfg")
        with open(local, "w") as f:
            f.write("x")
        upload_done = threading.Event()
        moved = threading.Event()
        self.addCleanup(moved.set)

        def hold_before_refresh(message):
            # 転送スレッドの中（DirectConnection）。ロックは離れている
            upload_done.set()
            moved.wait(5)
        m.transfer_complete.connect(hold_before_refresh,
                                    Qt.ConnectionType.DirectConnection)
        m.upload_file(local, "/A/new.cfg", overwrite=True)
        self.assertTrue(upload_done.wait(5), "前提: 転送が終わらない")
        m.change_directory("/C")        # 利用者の移動。/C の一覧はロックの中で止まる
        deadline = time.time() + 5
        while time.time() < deadline:
            with asked_lock:
                if "/C" in asked:
                    break
            time.sleep(0.01)
        self.assertIn("/C", asked, "前提: /C の一覧が始まらない")
        moved.set()                     # 転送スレッドが自動更新を頼む
        self.assertTrue(refresh_asked.wait(5), "前提: 自動更新が頼まれない")
        c_gate.set()                    # /C の一覧が失敗する
        self._pump(lambda: lists or len(errors) >= 2, seconds=5.0)
        self._pump(seconds=0.5)         # 遅れて届くものが無いこと
        self.assertEqual(len(errors), 1,
                         "同じ失敗が重ねて知らされた（頼んだ順 %r）: %r"
                         % (asked, errors))
        self.assertIn("Permission denied", errors[0])
        self.assertEqual(asked, ["/C", "/A"],
                         "自動更新が失敗した移動先を頼み直した")
        self.assertEqual(m.current_path, "/A")
        self.assertEqual(lists, [["in-A.cfg", "new.cfg"]],
                         "今の場所の一覧が取り直されていない")


if __name__ == "__main__":
    unittest.main()
