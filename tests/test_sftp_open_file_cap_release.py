"""内蔵 SFTP サーバーの開いているファイルの上限で、使った枠が必ず戻ること。

利用者の指示（2026-10-04）: 失敗・close・切断時に枠を戻すことを確かめる。

tests/test_sftp_open_file_cap.py が見ていたのは、無いファイルの open・close
（例外を出す close を含む）・クライアントがセッション（チャネル）を閉じた場合。
ここでは次を足す。
- open の失敗の種類（無いフォルダ・O_EXCL で既にある・フォルダを開く・
  読み取り専用へ書く・ルートの外・停止の後の要求）。断った応答が届いた時点で
  もう戻っていること。
- close は 1 回だけ戻す（閉じたハンドルへの CLOSE の送り直しと、閉じた後の
  セッションの終わりで、もう一度戻さない）。
- ハンドルを閉じずに接続ごと断つ（SSH の切断・RST での abortive な切断）。
- 開いたままサーバーを止めても、数が次の起動へ残らない。
- 切断と停止は、後始末の close が例外で止まる場合も見る。close が通る普段の
  形では、残ったハンドルの close が 1 個ずつ戻すので、session_ended が戻さなく
  ても数は戻る（変異で確かめた）。session_ended が戻していることは、close が
  止まる形でだけ見える。
"""
import contextlib
import os
import socket
import stat
import struct
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
REPLY_TIMEOUT = 60.0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def open_many(sftp, count, path="/a.txt"):
    """a.txt の読み取りの OPEN を応答を待たずに count 回送る。(ハンドル, 断られた数)"""
    from paramiko.sftp import CMD_OPEN, SFTP_FLAG_READ
    from paramiko.sftp_attr import SFTPAttributes
    nums = [sftp._async_request(type(None), CMD_OPEN, path, SFTP_FLAG_READ,
                                SFTPAttributes()) for _ in range(count)]
    handles, refused = [], 0
    for num in nums:
        try:
            _t, msg = sftp._read_response(num)
        except IOError:
            refused += 1
            continue
        handles.append(msg.get_binary())
    return handles, refused


class _Counter:
    """呼ばれた回数を数える give_back"""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


class CountedHandleTest(unittest.TestCase):
    """ハンドル単体: 2 回閉じても 1 回しか戻さない。

    paramiko のサーバーは、閉じ終えたハンドルを表から外す。本物の経路で同じ
    ハンドルの close がもう一度呼ばれるのは、close が例外を出して表に残った
    ときだけ（tests/test_sftp_open_file_cap.py の
    test_a_close_that_raises_gives_back_only_once）。仕掛けそのものはここで見る
    """

    def test_a_read_handle_gives_back_once_when_closed_twice(self):
        from core.sftp_server import _CountedHandle
        counter = _Counter()
        h = _CountedHandle(os.O_RDONLY, counter)
        h.close()
        h.close()
        self.assertEqual(counter.calls, 1)

    def test_a_write_handle_gives_back_once_and_leaves_the_writers_list(self):
        from core.sftp_server import _OpenWriters, _WriteHandle
        writers = _OpenWriters()
        counter = _Counter()
        h = _WriteHandle(os.O_WRONLY, writers, counter)
        writers.add(h, os.path.join(tempfile.gettempdir(), "w1-dummy"))
        h.close()
        h.close()
        self.assertEqual(counter.calls, 1)
        self.assertEqual(writers.alive(), [])

    def test_an_open_refused_after_stop_gives_its_slot_back(self):
        """停止の後に届いた作成の open（_busy が断る）も、枠をその場で戻すこと。"""
        from paramiko import SFTP_FAILURE
        from paramiko.sftp_attr import SFTPAttributes
        from core.sftp_server import SFTPServerHandler, _OpenFiles, _OpenWriters
        root = tempfile.mkdtemp(prefix="netbelt-sftp-cap-")
        stopped = threading.Event()
        stopped.set()
        of = _OpenFiles(2, 2)
        handler = SFTPServerHandler(None, root, open_writers=_OpenWriters(),
                                    stop_event=stopped, open_files=of)
        result = handler.open("/new.bin", os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                              SFTPAttributes())
        self.assertEqual(result, SFTP_FAILURE)
        self.assertEqual((of._in_use, of._held), (0, {}))
        self.assertFalse(os.path.exists(os.path.join(root, "new.bin")))


class SftpSlotReleaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        import paramiko
        cls.app = QApplication.instance() or QApplication([])
        cls.host_key = paramiko.RSAKey.generate(2048)

    def setUp(self):
        data_dir = Path(tempfile.mkdtemp(prefix="netbelt-testhome-"))
        ap = mock.patch("core.config_manager.app_data_dir", return_value=data_dir)
        ap.start()
        self.addCleanup(ap.stop)
        self.root = tempfile.mkdtemp(prefix="netbelt-sftp-cap-")
        with open(os.path.join(self.root, "a.txt"), "wb") as f:
            f.write(b"abc")

    def _limits(self, per_session, total):
        """上限を小さくする（マネージャを作る前に呼ぶ）"""
        from core.sftp_server import SFTPServerManager
        for name, value in (("MAX_OPEN_FILES_PER_SESSION", per_session),
                            ("MAX_OPEN_FILES_TOTAL", total)):
            p = mock.patch.object(SFTPServerManager, name, value)
            p.start()
            self.addCleanup(p.stop)

    def _manager(self):
        from core.sftp_server import SFTPServerManager
        m = SFTPServerManager()
        m.STOP_TIMEOUT_SECONDS = 1.0
        m.host_key = self.host_key
        self.addCleanup(self._flush_qt)
        self.addCleanup(m.stop)
        return m

    def _start(self, m):
        for _ in range(5):
            # 調べたポートを別のプロセスが先に取ることがある（排他の待受なので
            # start が失敗する）。そのときは別のポートで起動し直す
            self.port = free_port()
            if m.start(port=self.port, root_dir=self.root,
                       username=USER, password=PASSWORD):
                break
        else:
            self.fail("サーバーが起動しない")
        self.assertTrue(self._wait(lambda: m.is_running), "サーバーが起動しない")
        return m

    def _flush_qt(self):
        """配送待ちの通知（接続・切断・断った知らせ）を処理しておく"""
        for _ in range(3):
            self.app.processEvents()

    def _connect(self):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect("127.0.0.1", port=self.port, username=USER, password=PASSWORD,
                  allow_agent=False, look_for_keys=False, timeout=10)
        self.addCleanup(c.close)
        return c

    def _session(self, client):
        s = client.open_sftp()
        s.get_channel().settimeout(REPLY_TIMEOUT)
        self.addCleanup(s.close)
        return s

    @staticmethod
    def _wait(predicate, seconds=10.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return bool(predicate())

    @staticmethod
    def _opens(sftp, path="a.txt", mode="rb"):
        """開けたらハンドル、断られたら None"""
        try:
            return sftp.open(path, mode)
        except IOError:
            return None

    def test_failed_opens_give_their_slot_back_before_the_reply(self):
        self._limits(per_session=2, total=2)
        m = self._start(self._manager())
        of = m._open_files
        os.mkdir(os.path.join(self.root, "sub"))
        ro = os.path.join(self.root, "ro.txt")
        with open(ro, "wb") as f:
            f.write(b"ro")
        os.chmod(ro, stat.S_IREAD)
        # 消さずに残すので、後で消せるよう書き込みを許し直しておく
        self.addCleanup(os.chmod, ro, stat.S_IREAD | stat.S_IWRITE)
        s = self._session(self._connect())
        failures = [
            ("missing/x.txt", "rb"),    # 無いフォルダ
            ("a.txt", "x"),             # O_EXCL（作成のみ）で既にある
            ("a.txt", "wx"),            # 書き込み＋O_EXCL（保存先の予約も取る）
            ("sub", "rb"),              # フォルダをファイルとして開く
            ("ro.txt", "r+"),           # 読み取り専用のファイルへ書く
            ("../outside.txt", "rb"),   # ルートの外
        ]
        for path, mode in failures:
            with self.subTest(path=path, mode=mode):
                with self.assertRaises(IOError):
                    s.open(path, mode)
                # 断った応答が届いた時点で戻っている
                self.assertEqual((of._in_use, of._held), (0, {}))
        # 失敗を何度重ねても、上限の分だけ開ける
        held = [self._opens(s), self._opens(s, "a.txt", "r+")]
        self.assertTrue(all(h is not None for h in held), "開けなかった open が枠を使った")
        self.assertIsNone(self._opens(s), "上限を超えて開けた")
        for h in held:
            h.close()
        self.assertEqual((of._in_use, of._held), (0, {}))

    def test_a_closed_handle_is_not_given_back_again(self):
        """閉じたハンドルへの CLOSE の送り直しと、その後のセッションの終わりで、
        もう一度戻さないこと。

        戻しすぎると全体の数が実際より小さくなり、全体の上限を超えて開ける
        """
        from paramiko.sftp import CMD_CLOSE
        self._limits(per_session=3, total=3)
        m = self._start(self._manager())
        of = m._open_files
        client = self._connect()
        keeper = self._session(client)
        kept = self._opens(keeper)
        a = client.open_sftp()
        a.get_channel().settimeout(REPLY_TIMEOUT)
        h1, h2 = self._opens(a), self._opens(a)
        self.assertEqual(of._in_use, 3)
        raw = h1.handle
        h1.close()
        self.assertEqual(of._in_use, 2)
        for _ in range(3):
            with self.assertRaises(IOError):   # Invalid handle
                a._request(CMD_CLOSE, raw)
        self.assertEqual(of._in_use, 2, "閉じたハンドルへの CLOSE で数が戻った")
        a.close()   # h2 は閉じずにセッションを終える
        self.assertTrue(self._wait(lambda: of._in_use == 1),
                        "終わったセッションの分が戻らない: %d" % of._in_use)
        time.sleep(0.2)   # 戻しすぎがあれば、ここまでに起きる
        self.assertEqual(of._in_use, 1, "閉じた分をセッションの終わりでもう一度戻した")
        # 全体の残りは 2 個
        c = self._session(client)
        held = [self._opens(c), self._opens(c)]
        self.assertTrue(all(h is not None for h in held))
        self.assertIsNone(self._opens(c), "全体の上限を超えて開けた")
        self.assertIsNotNone(kept)

    def _leftover_closes_raise(self):
        """後始末の close が例外を出すようにする（with で使う）。

        paramiko の finish_subsystem は session_ended を呼んでから残ったハンドルを
        順に close し、1 つが例外を出すと残りは閉じない。数を戻せるのは
        session_ended だけになる（close が通る普段の切断では、残りの close が
        1 個ずつ戻すので、session_ended が戻さなくても数は戻ってしまう）。
        閉じられなかったファイルは GC まで残るので、後で集めて閉じる
        """
        import gc
        import paramiko

        def broken_close(handle):
            raise RuntimeError("close failed")

        self.addCleanup(gc.collect)
        return mock.patch.object(paramiko.SFTPHandle, "close", broken_close)

    def _drop_without_closing(self, drop, closes_raise=False):
        """1 セッションで 256 個開いたまま drop(client) で接続を断ち、全部戻ること。"""
        m = self._start(self._manager())
        of = m._open_files
        keeper = self._session(self._connect())
        kept, _ = open_many(keeper, 10)
        self.assertEqual(len(kept), 10)
        per_session = m.MAX_OPEN_FILES_PER_SESSION
        client = self._connect()
        handles, refused = open_many(self._session(client), per_session)
        self.assertEqual((len(handles), refused), (per_session, 0))
        self.assertEqual(of._in_use, 10 + per_session)
        with (self._leftover_closes_raise() if closes_raise
              else contextlib.nullcontext()):
            drop(client)
            self.assertTrue(self._wait(lambda: of._in_use == 10),
                            "切れた接続の分が戻らない: %d" % of._in_use)
        self.assertEqual(sorted(of._held.values()), [10])
        # 戻った分は、新しい接続でまた上限まで開ける
        again, refused = open_many(self._session(self._connect()), per_session + 1)
        self.assertEqual((len(again), refused), (per_session, 1))
        self.assertEqual(of._in_use, 10 + per_session)

    @staticmethod
    def _close_transport(client):
        client.get_transport().close()

    @staticmethod
    def _reset(client):
        """ソケットを RST で切る（SO_LINGER 0 で閉じる）"""
        sock = client.get_transport().sock
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
        sock.close()

    def test_closing_the_ssh_connection_without_closing_files_gives_them_back(self):
        self._drop_without_closing(self._close_transport)

    def test_an_abortive_disconnect_gives_the_files_back(self):
        self._drop_without_closing(self._reset)

    def test_a_disconnect_gives_back_through_session_ended_when_closes_raise(self):
        """後始末の close が例外で止まっても、session_ended が全部戻すこと。"""
        self._drop_without_closing(self._close_transport, closes_raise=True)

    def test_an_abortive_disconnect_gives_back_when_closes_raise(self):
        self._drop_without_closing(self._reset, closes_raise=True)

    def test_stopping_with_files_open_leaves_no_count_for_the_next_start(self):
        self._stop_with_files_open(closes_raise=False)

    def test_stopping_leaves_no_count_even_when_closes_raise(self):
        self._stop_with_files_open(closes_raise=True)

    def _stop_with_files_open(self, closes_raise):
        self._limits(per_session=3, total=5)
        m = self._start(self._manager())
        of = m._open_files
        a = self._session(self._connect())
        b = self._session(self._connect())
        held = [self._opens(a) for _ in range(3)] + [self._opens(b) for _ in range(2)]
        self.assertTrue(all(h is not None for h in held))
        self.assertIsNone(self._opens(b), "前提: 全体の上限で断られていない")
        with (self._leftover_closes_raise() if closes_raise
              else contextlib.nullcontext()):
            m.stop()
            self.assertTrue(self._wait(lambda: of._in_use == 0),
                            "停止の後も数が残った: %d" % of._in_use)
        self.assertEqual(of._held, {})
        # 同じマネージャを起動し直すと、全体の上限まで開ける（0 から数える）
        self._start(m)
        c = self._session(self._connect())
        d = self._session(self._connect())
        again = [self._opens(c) for _ in range(3)] + [self._opens(d) for _ in range(2)]
        self.assertTrue(all(h is not None for h in again), "前の起動の数が残った")
        self.assertIsNone(self._opens(d), "全体の上限を超えて開けた")
        self.assertEqual(of._in_use, 5)


if __name__ == "__main__":
    unittest.main()
