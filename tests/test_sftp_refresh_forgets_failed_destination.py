"""行き先の控えを追った自動更新がその場所で失敗したら、控えを外し、以後の
自動更新が今の場所を取り直すことを検証する。

実測（d77b1a0。1.3.2 の fa1bac5 から）:
  控え（_listing_path）を外すのは、失敗した一覧が「控えを置いた要求」
  （移動・利用者の更新）のときだけになった（_forget_failed_destination）。
  自動更新が控えを追ってその場所で失敗しても控えは残る。/A にいて B へ
  移動し、その一覧が GUI に届く前に、表示中の /A で B を削除すると、削除の
  自動更新は控えの /A/B を頼んで「ディレクトリ一覧取得エラー: [Errno 2]
  No such file」になる（移動の一覧は自動更新より古いので捨てられる）。
  控えが残るので、続けて /A に新規フォルダを作ると、その自動更新も /A/B を
  頼んで同じエラーを出し、作ったフォルダは表に出ない。頼んだ順は
  ['/A/B', '/A/B', '/A/B']、エラーは 2 件。以後も、利用者が自分で更新するか
  移動するまで、変更のたびに同じエラーが出る。b2858c4 では自動更新の失敗
  （最新の要求）で控えが外れ、2 回目は /A を取り直していた（エラー 1 件）。

直し方:
  自動更新が控えを追ったときは、追った控えの番号を覚え、失敗したら、
  控えがまだその番号のままなら外す（あとの移動が置き換えていれば残す）。
"""
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _Attr:
    def __init__(self, name, is_dir=False):
        self.filename = name
        self.st_mode = 0o040755 if is_dir else 0o100644
        self.st_size = 1
        self.st_mtime = 0


class RefreshForgetsFailedDestinationTest(unittest.TestCase):
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

    def test_a_refresh_failing_at_the_destination_forgets_it(self):
        """移動先を追った自動更新がそこで失敗したら、次の自動更新は今の /A を取り直すこと。"""
        from core.sftp_manager import SFTPManager
        m = SFTPManager()
        type(self)._keep.append(m)
        m.is_connected = True
        m.current_path = "/A"
        c = m.sftp_client = mock.Mock()
        c.get_channel.return_value.closed = False
        c.normalize.side_effect = lambda p: p
        state = {"B": True, "newdir": False}
        asked = []
        asked_lock = threading.Lock()

        def listdir_attr(path):
            with asked_lock:
                asked.append(path)
            if path == "/A/B":
                if not state["B"]:
                    raise FileNotFoundError(2, "No such file")
                return [_Attr("in-B.cfg")]
            names = [_Attr("x.cfg")]
            if state["B"]:
                names.append(_Attr("B", True))
            if state["newdir"]:
                names.append(_Attr("newdir", True))
            return names
        c.listdir_attr.side_effect = listdir_attr
        c.rmdir.side_effect = lambda path: state.update(B=False)
        c.mkdir.side_effect = lambda path: state.update(newdir=True)
        lists, errors = [], []
        m.file_list_ready.connect(
            lambda l: lists.append(sorted(e["name"] for e in l)))
        m.error_occurred.connect(errors.append)

        m.change_directory("/A/B")      # 利用者の移動（B のダブルクリック）
        deadline = time.time() + 5
        while time.time() < deadline:
            with asked_lock:
                if "/A/B" in asked:
                    break
            time.sleep(0.01)
        self.assertIn("/A/B", asked, "前提: /A/B の一覧が始まらない")
        # 移動の一覧が GUI に届く前（イベントを回さない）に、表示中の /A で B を削除する
        m.delete_item("/A/B", is_dir=True)
        c.rmdir.assert_called_once_with("/A/B")
        self.assertTrue(self._pump(lambda: errors, seconds=5.0),
                        "前提: 削除の自動更新が失敗しない（頼んだ順 %r）" % asked)
        self._pump(seconds=0.3)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("No such file", errors[0])

        m.create_directory("/A/newdir")
        c.mkdir.assert_called_once_with("/A/newdir")
        self._pump(lambda: lists or len(errors) >= 2, seconds=5.0)
        self._pump(seconds=0.3)         # 遅れて届くものが無いこと
        self.assertEqual(len(errors), 1,
                         "消えた移動先を頼み続けた（頼んだ順 %r）: %r"
                         % (asked, errors))
        self.assertEqual(asked, ["/A/B", "/A/B", "/A"])
        self.assertEqual(m.current_path, "/A")
        self.assertEqual(lists, [["newdir", "x.cfg"]],
                         "新規フォルダのあとの一覧が届いていない")


if __name__ == "__main__":
    unittest.main()
