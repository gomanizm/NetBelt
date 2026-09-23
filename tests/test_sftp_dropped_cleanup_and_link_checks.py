"""切断・壊れた応答が、後始末とリンク確認で握りつぶされないことを検証する。

何が起きていたか（基準 f4cad23 での実測）:
  利用者の決定 2026-09-20「切断・壊れた応答も期限切れと同じく接続を畳む」が、
  SFTPManager の 3 か所で破れていた。いずれも同じ場面を TimeoutError で
  起こせば接続が畳まれるのに、EOFError / paramiko.SSHException（＝切断・
  壊れた応答）だと接続中のまま残る。

  (a) 転送の後始末 — put が IOError('Failure: disk full')（機器が答えた
      通常の失敗）で落ちたあと、送りかけの一時名を消す remove が
      SSHException('Server connection dropped: ') や EOFError になっても
      `except Exception: pass` が飲んでいた。実測は
      errors=['アップロードエラー: Failure: disk full'] / is_connected=True /
      disconnected emits=0 で、後始末が成功した場合と出力が全く同じ。
      切断を観測したことがどこにも残らず、機器に残った一時名
      .<名前>.netbelt-part.xxxx も知らされない。

  (b) 一覧のリンク追跡 — listdir_attr は成功し、リンク項目の stat だけが
      切断系で落ちると `except Exception: continue` が飲んでいた。実測は
      file_list_ready が [('conf',True,False),('boot.bin',False,False),
      ('link-to-dir',False,True)] として配達され、errors=[] /
      is_connected=True / disconnected=0。切断後の一覧が「取得成功」として
      届き、ディレクトリへのリンクはファイル表示（＝入れない）になる。

  (c) inspect_link_target — readlink が切断系だと戻り値 (33188, None)、
      errors=[]、is_connected=True で、リンク先名なしのまま権限変更
      ダイアログが開いた。stat が切断系だと
      "'link.cfg' のリンク先を読めないため、パーミッションを変更でき
      ません（Server connection dropped: ）" と、_fail が落とすはずの
      末尾コロンまで出たうえで接続が残った。

  実害: 接続が死んでいるのに接続中の表示が残り、次の操作で 2 度目の
  エラーが出てからようやく畳まれる。壊れた応答では TCP が生きているため、
  次の GUI スレッド操作が CHANNEL_TIMEOUT_SECONDS ぶん画面を固めうる。

どう直したか:
  3 か所とも期限切れと同じ判定木に入れた。(b) はリンクの stat が
  _is_dropped_connection なら raise して外側の _fail に畳ませる。(c) は
  readlink を同じく raise し、stat の失敗が切断系なら error_occurred では
  なく _fail へ回す（末尾コロンの整形も _fail が持っている）。(a) は
  後始末の except を期限切れと同じ枝に入れ、元の失敗が通常エラーのときだけ
  『（送りかけの一時名 … を片づけられませんでした）』を添えて畳む。元の
  失敗が既に切断系なら重ねない（tests/test_sftp_dropped_cleanup_single_notice.py
  の「切断の通知は 1 回」を守る）。
"""
import io
import os
import shutil
import stat as stat_mod
import sys
import tempfile
import time
import unittest
from unittest import mock

import paramiko

sys.path.insert(0, "src")

FINAL = "/flash/running.cfg"
DROPPED = "Server connection dropped: "


class _Attr:
    """listdir_attr / stat が返す項目の代わり（必要な属性だけ持つ）。"""

    def __init__(self, filename, st_mode, st_size=0, st_mtime=1700000000):
        self.filename = filename
        self.st_mode = st_mode
        self.st_size = st_size
        self.st_mtime = st_mtime


class _DroppedBase(unittest.TestCase):
    """切断系の例外を機器の代わりに投げるマネージャを用意する土台。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _manager(self):
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        # 畳むと m.sftp_client は None になるので、呼び出しの記録は控えて見る
        self.client = m.sftp_client = mock.Mock()
        self.errors, self.gone = [], []
        m.error_occurred.connect(self.errors.append)
        m.disconnected.connect(lambda: self.gone.append(True))
        return m

    def _assert_folded(self, m, where):
        self.assertFalse(m.is_connected,
                         "切断のあとも接続中のまま残っている: %s" % self.errors)
        self.assertIsNone(m.sftp_client, "使えないクライアントを掴んだまま")
        self.assertEqual(len(self.gone), 1,
                         "disconnected が %d 回（1 回であること）" % len(self.gone))
        joined = " / ".join(self.errors)
        self.assertIn(where, joined, "どの操作で起きたかが伝わらない: %s" % self.errors)
        self.assertIn("SFTP接続を切断しました", joined,
                      "切断したことが伝わらない: %s" % self.errors)


class SftpDroppedCleanupRemoveTest(_DroppedBase):
    """(a) 送りかけの一時名を片づける remove が切断で落ちた場合。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-dropped-cleanup2-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "running.cfg")
        with io.open(self.local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

    def _upload(self, remove_effect, put_effect=None):
        """最終名が無いところへ送り、put が失敗して後始末へ進む"""
        m = self._manager()
        self.client.stat.side_effect = IOError("No such file")   # 最終名は無い
        self.client.put.side_effect = (put_effect
                                       or IOError("Failure: disk full"))
        self.client.remove.side_effect = remove_effect
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない

        m.upload_file(self.local, FINAL, overwrite=False)

        self.assertTrue(self._wait(lambda: self.errors), "失敗が通知されない")
        # 畳むのは finally なので、後から届く分まで待ってから数える
        self._wait(lambda: not m.is_connected, seconds=5.0)
        self._wait(lambda: False, seconds=0.4)
        return m

    def test_a_dropped_cleanup_remove_closes_the_session(self):
        """後始末が切断で落ちたら、使えないチャンネルを掴んだまま残さないこと。"""
        m = self._upload(paramiko.SSHException(DROPPED))

        self.assertEqual(self.errors[0], "アップロードエラー: Failure: disk full",
                         "元の失敗の文面が変わっている: %s" % self.errors)
        self._assert_folded(m, "アップロードエラー")
        joined = " / ".join(self.errors)
        self.assertIn("Server connection dropped", joined,
                      "後始末で何が起きたか伝わらない: %s" % self.errors)
        tmp = self.client.put.call_args[0][1]
        self.assertIn(tmp, joined,
                      "片づけられなかった一時名を知らせていない: %s" % self.errors)

    def test_an_eof_cleanup_remove_closes_the_session(self):
        """素の EOFError（paramiko が切断した読み取りで上げる）でも同じであること。"""
        m = self._upload(EOFError())

        self._assert_folded(m, "アップロードエラー")
        tmp = self.client.put.call_args[0][1]
        self.assertIn(tmp, " / ".join(self.errors),
                      "片づけられなかった一時名を知らせていない: %s" % self.errors)

    def test_a_cleanup_that_works_keeps_the_connection(self):
        """後始末が通ったときは、これまでどおり接続を保つこと。"""
        m = self._upload(None)

        self.assertEqual(self.errors, ["アップロードエラー: Failure: disk full"],
                         "余計な通知が増えている: %s" % self.errors)
        self.assertTrue(m.is_connected, "転送の失敗だけで接続を畳んでいる")
        self.assertEqual(self.gone, [])
        tmp = self.client.put.call_args[0][1]
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertEqual(removed, [tmp],
                         "送りかけの一時名を片づけていない: %s" % removed)

    def test_a_dropped_transfer_and_dropped_cleanup_are_told_once(self):
        """元の失敗も後始末も切断なら、通知を 1 回にまとめること。"""
        m = self._upload(EOFError(), put_effect=paramiko.SSHException(DROPPED))

        self.assertEqual(len(self.errors), 1,
                         "切断の通知が重なっている: %s" % self.errors)
        self._assert_folded(m, "アップロードエラー")
        tmp = self.client.put.call_args[0][1]
        self.assertIn(tmp, self.errors[0],
                      "片づけられなかった一時名を知らせていない: %s" % self.errors)
        self.assertIn("Server connection dropped", self.errors[0],
                      "元の失敗の理由が消えている: %s" % self.errors)


class SftpDroppedLinkListingTest(_DroppedBase):
    """(b) 一覧のリンク追跡 stat が切断で落ちた場合。"""

    def _list(self, stat_effect):
        m = self._manager()
        m.current_path = "/flash"
        self.client.listdir_attr.return_value = [
            _Attr("conf", stat_mod.S_IFDIR | 0o755),
            _Attr("link-to-dir", stat_mod.S_IFLNK | 0o777),
            _Attr("boot.bin", stat_mod.S_IFREG | 0o644, st_size=10),
        ]
        self.client.stat.side_effect = stat_effect
        self.lists = []
        m.file_list_ready.connect(self.lists.append)

        m.list_directory("/flash")

        self._wait(lambda: self.lists or self.errors, seconds=5.0)
        self._wait(lambda: False, seconds=0.3)
        return m

    def test_a_dropped_link_lookup_stops_the_listing(self):
        """切断のあとの一覧を「取得成功」として配らず、接続を畳むこと。"""
        m = self._list(paramiko.SSHException(DROPPED))

        self.assertEqual(self.lists, [],
                         "切断後の一覧が取得成功として届いた: %s" % self.lists)
        self._assert_folded(m, "ディレクトリ一覧取得エラー")

    def test_an_eof_link_lookup_stops_the_listing(self):
        """素の EOFError でも同じであること。"""
        m = self._list(EOFError())

        self.assertEqual(self.lists, [])
        self._assert_folded(m, "ディレクトリ一覧取得エラー")

    def test_a_broken_link_is_still_listed_as_a_file(self):
        """壊れたリンク（機器が答えた通常の失敗）は、これまでどおり一覧に出すこと。"""
        m = self._list(IOError("No such file"))

        self.assertEqual(len(self.lists), 1, "一覧が届かない: %s" % self.errors)
        names = [(e["name"], e["is_dir"], e["is_link"]) for e in self.lists[0]]
        self.assertIn(("link-to-dir", False, True), names,
                      "壊れたリンクの扱いが変わっている: %s" % names)
        self.assertEqual(self.errors, [])
        self.assertTrue(m.is_connected, "通常の失敗で接続を畳んでいる")


class SftpDroppedLinkTargetTest(_DroppedBase):
    """(c) inspect_link_target の stat / readlink が切断で落ちた場合。"""

    def _inspect(self, stat_effect=None, readlink_effect=None):
        m = self._manager()
        attr = _Attr("real.cfg", stat_mod.S_IFREG | 0o644)
        self.client.stat.return_value = attr
        self.client.stat.side_effect = stat_effect
        self.client.readlink.return_value = "/flash/real.cfg"
        self.client.readlink.side_effect = readlink_effect

        self.result = m.inspect_link_target("/flash/link.cfg")

        self._wait(lambda: False, seconds=0.1)
        return m

    def test_a_dropped_readlink_closes_the_session(self):
        """リンク先の名前を読む途中で切れたら、権限変更へ進まず畳むこと。"""
        m = self._inspect(readlink_effect=paramiko.SSHException(DROPPED))

        self.assertIsNone(self.result,
                          "切断したのにリンク先を返している: %s" % (self.result,))
        self._assert_folded(m, "リンク先の確認エラー")

    def test_an_eof_readlink_closes_the_session(self):
        """素の EOFError でも同じであること。"""
        m = self._inspect(readlink_effect=EOFError())

        self.assertIsNone(self.result)
        self._assert_folded(m, "リンク先の確認エラー")

    def test_a_dropped_stat_closes_the_session(self):
        """リンク先の権限を読む途中で切れたら、期限切れと同じ文面で畳むこと。"""
        m = self._inspect(stat_effect=paramiko.SSHException(DROPPED))

        self.assertIsNone(self.result)
        self._assert_folded(m, "リンク先の確認エラー")
        self.assertIn("Server connection dropped。", self.errors[0],
                      "末尾のコロンが整形されていない: %s" % self.errors)

    def test_an_eof_stat_closes_the_session(self):
        """理由の文字列が空の EOFError でも、型の名前で理由を伝えること。"""
        m = self._inspect(stat_effect=EOFError())

        self.assertIsNone(self.result)
        self._assert_folded(m, "リンク先の確認エラー")
        self.assertIn("EOFError", self.errors[0],
                      "理由が伝わらない: %s" % self.errors)

    def test_a_link_whose_name_cannot_be_read_keeps_the_session(self):
        """readlink に応じない機器（通常の失敗）は、これまでどおり権限だけ返すこと。"""
        m = self._inspect(readlink_effect=IOError("Operation unsupported"))

        self.assertEqual(self.result, (stat_mod.S_IFREG | 0o644, None))
        self.assertEqual(self.errors, [])
        self.assertTrue(m.is_connected, "通常の失敗で接続を畳んでいる")

    def test_a_broken_link_is_still_refused_with_the_reason(self):
        """壊れたリンクは、これまでどおり理由を伝えて断るだけにすること。"""
        m = self._inspect(stat_effect=IOError("No such file"))

        self.assertIsNone(self.result)
        self.assertEqual(len(self.errors), 1, "通知が増えている: %s" % self.errors)
        self.assertIn("リンク先を読めない", self.errors[0])
        self.assertIn("No such file", self.errors[0])
        self.assertTrue(m.is_connected, "通常の失敗で接続を畳んでいる")
        self.assertEqual(self.gone, [])


if __name__ == "__main__":
    unittest.main()
