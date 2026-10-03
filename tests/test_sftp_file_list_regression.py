"""SFTP クライアントのファイル一覧の、表示・並び・選択・行の削除の今の挙動を押さえる。

_update_file_list の行の置き方を、リストを渡す appendRow から、項目を 1 つずつ
置く形へ変えた（漏れの話は test_sftp_file_list_heap_steady）。見た目と操作の
相手が変わっていないことを、9fee4af で確かめた今の挙動として押さえる。
- 表示: 4 列。名前には種類の印（📁 / 📄）を前に付け、サイズはディレクトリなら
  空、分からない値は「不明」。行の元の情報（file_info）は名前の列の UserRole に
  だけ持つ。アイコン（DecorationRole）・色・字体・寄せは付けない。
- 並び: 届いた順のまま（並べるのは SFTPManager で、ディレクトリが先・名前順）。
  見出しを押しても並べ替えない（ソートは無効）。
- 選択: 1 行ずつ。どの列を押しても、ダウンロード・削除はその行の名前に対して
  行う。一覧を更新すると選択は外れる（古い行のつもりで別の物を消さない）。
  コピーの操作はこのパネルに無い。
- 行の削除: 機器で消した物・別のディレクトリへ移した物・名前を変えた物は、
  そのあとの一覧から消える。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

HOME = "/home/user"


def _entry(name, is_dir=False, size=1234, permissions="-rw-r--r--",
           mtime=1700000000):
    return {"name": name, "size": 0 if is_dir else size, "mtime": mtime,
            "mode": 0o040755 if is_dir else 0o100644, "is_dir": is_dir,
            "is_link": False, "permissions": permissions}


def _listing():
    """SFTPManager が渡す形の一覧（隠しファイルを 1 件含む）"""
    return [_entry("etc", True, permissions="drwxr-xr-x"),
            _entry("var", True, permissions="drwxr-xr-x"),
            _entry("A.txt", size=2048, permissions=None, mtime=None),
            _entry("boot.cfg", size=10),
            _entry("image.bin", size=5 * 1024 * 1024),
            _entry(".profile", size=99)]


class _Attr:
    """paramiko の SFTPAttributes の、一覧に使う分だけ"""

    def __init__(self, name, is_dir):
        self.filename = name
        self.st_mode = 0o040755 if is_dir else 0o100644
        self.st_size = 0 if is_dir else 100
        self.st_mtime = 1700000000


class _FakeSftp:
    """メモリ上のファイルの木（SFTPClient の、この試験で使う分だけ）

    paths は {パス: ディレクトリか}。
    """

    def __init__(self, paths):
        self.paths = dict(paths)

    def normalize(self, path):
        return path

    def listdir_attr(self, path):
        prefix = path.rstrip("/") + "/"
        return [_Attr(p[len(prefix):], is_dir) for p, is_dir in self.paths.items()
                if p.startswith(prefix) and "/" not in p[len(prefix):]]

    def remove(self, path):
        if self.paths.get(path) is not False:
            raise IOError(2, "No such file")
        del self.paths[path]

    def rename(self, old, new):
        if old not in self.paths or new in self.paths:
            raise IOError(2, "No such file")
        self.paths[new] = self.paths.pop(old)


class SftpFileListRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _flush(self):
        from PyQt6.QtCore import QCoreApplication, QEvent
        for _ in range(3):
            self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def _wait(self, predicate, seconds=10.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        self.app.processEvents()
        return predicate()

    def _manager(self):
        m = mock.Mock()
        m.is_connected = True
        m.current_path = HOME
        m.get_current_path.return_value = HOME
        return m

    def _panel(self, manager=None, listing=None):
        from ui.sftp_panel import SFTPPanel
        panel = SFTPPanel()
        panel.resize(800, 600)
        panel.show()
        self.addCleanup(self._dispose, panel)
        panel.set_sftp_manager(manager or self._manager(), "rtrA")
        panel._update_file_list(_listing() if listing is None else listing)
        self.app.processEvents()
        return panel

    def _dispose(self, panel):
        panel.clear()
        panel.close()
        panel.deleteLater()
        self._flush()

    @staticmethod
    def _names(panel):
        from PyQt6.QtCore import Qt
        return [panel.model.item(r, 0).data(Qt.ItemDataRole.UserRole)["name"]
                for r in range(panel.model.rowCount())]

    def _click(self, panel, row, column):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        index = panel.model.index(row, column)
        panel.tree_view.scrollTo(index)
        QTest.mouseClick(panel.tree_view.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier,
                         panel.tree_view.visualRect(index).center())
        self.app.processEvents()

    def test_each_row_shows_its_entry_and_nothing_else(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QStandardItem
        from ui.sftp_panel import SFTPPanel
        panel = self._panel()
        model = panel.model
        self.assertEqual(
            [model.headerData(c, Qt.Orientation.Horizontal)
             for c in range(model.columnCount())],
            ["名前", "サイズ", "パーミッション", "更新日時"])

        # 隠しファイルは既定（show_hidden_files=False）で出さない
        shown = [e for e in _listing() if not e["name"].startswith(".")]
        expected = [[("📁 " if e["is_dir"] else "📄 ") + e["name"],
                     "" if e["is_dir"] else SFTPPanel._format_size(e["size"]),
                     e["permissions"] or "不明",
                     SFTPPanel._format_mtime(e["mtime"])] for e in shown]
        self.assertEqual(expected[2][1:], ["2.0 KB", "不明", "不明"])
        self.assertEqual(expected[0][1], "")
        self.assertEqual(
            [[model.item(r, c).text() for c in range(4)]
             for r in range(model.rowCount())], expected)
        self.assertEqual(panel.status_label.text(), "5 項目")

        R = Qt.ItemDataRole
        nothing = (R.DecorationRole, R.ToolTipRole, R.StatusTipRole,
                   R.WhatsThisRole, R.FontRole, R.TextAlignmentRole,
                   R.BackgroundRole, R.ForegroundRole, R.CheckStateRole,
                   R.SizeHintRole)
        default_flags = QStandardItem("x").flags()
        for r, entry in enumerate(shown):
            for c in range(4):
                index = model.index(r, c)
                where = "%d 行 %d 列" % (r, c)
                self.assertEqual(model.flags(index), default_flags, where)
                self.assertFalse(model.hasChildren(index), where)
                self.assertEqual(model.data(index, R.EditRole), expected[r][c], where)
                for role in nothing:
                    self.assertIsNone(model.data(index, role),
                                      "%s に %s が付いている" % (where, role))
                self.assertEqual(model.data(index, R.UserRole),
                                 entry if c == 0 else None, where)

    def test_a_refresh_does_not_announce_a_layout_change(self):
        """一覧の更新で、モデルがレイアウトの変更を知らせないこと（9fee4af と同じ）。

        9fee4af の更新で出るのは、行の削除と追加の知らせだけ。行を足したあとに
        setItem で残りの列を埋めると、1 セルごとに layoutAboutToBeChanged /
        layoutChanged が出て、ビューと見出しがそのたびに組み直す（5 件で 15 回。
        実測で更新が 1.2〜1.5 倍遅くなった）。
        """
        panel = self._panel()
        layout, inserted = [], []
        panel.model.layoutAboutToBeChanged.connect(lambda *a: layout.append("about"))
        panel.model.layoutChanged.connect(lambda *a: layout.append("changed"))
        panel.model.rowsInserted.connect(lambda *a: inserted.append(a[1:]))
        panel._update_file_list(_listing())
        self.app.processEvents()
        self.assertEqual(self._names(panel), ["etc", "var", "A.txt", "boot.cfg", "image.bin"])
        self.assertEqual(inserted, [(r, r) for r in range(5)],
                         "前提: 5 行の追加が知らされていない")
        self.assertEqual(layout, [], "一覧の更新でレイアウトの変更が知らされた")

    def test_the_header_does_not_sort_and_rows_keep_the_order_they_came_in(self):
        from PyQt6.QtCore import QPoint, Qt
        from PyQt6.QtTest import QTest
        panel = self._panel()
        self.assertFalse(panel.tree_view.isSortingEnabled())
        order = ["etc", "var", "A.txt", "boot.cfg", "image.bin"]
        self.assertEqual(self._names(panel), order)
        header = panel.tree_view.header()
        for column in range(4):
            for _ in range(2):      # 2 回目は、並べ替えが効くなら逆順に当たる
                x = (header.sectionViewportPosition(column)
                     + header.sectionSize(column) // 2)
                QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.NoModifier,
                                 QPoint(x, header.height() // 2))
                self.app.processEvents()
                self.assertEqual(self._names(panel), order,
                                 "見出し %d を押したら並びが変わった" % column)

        # 更新したあとも、届いた順のまま（パネルは並べ替えない）
        reordered = list(reversed(_listing()))
        panel._update_file_list(reordered)
        self.assertEqual(self._names(panel),
                         [e["name"] for e in reordered if e["name"] != ".profile"])

    def test_the_row_picked_is_what_download_and_delete_act_on(self):
        from PyQt6.QtWidgets import QMessageBox
        from ui import sftp_panel as mod
        manager = self._manager()
        panel = self._panel(manager)

        # パーミッションの列を押しても、行ごと選ばれて名前の行が相手になる
        row = self._names(panel).index("boot.cfg")
        self._click(panel, row, 2)
        self.assertEqual({i.row() for i in panel.tree_view.selectedIndexes()}, {row})
        save_to = os.path.join(tempfile.mkdtemp(prefix="netbelt-sftp-pick-"),
                               "boot.cfg")
        with mock.patch.object(mod.QFileDialog, "getSaveFileName",
                               return_value=(save_to, "")):
            panel._on_download()
        manager.download_file.assert_called_once_with(
            HOME + "/boot.cfg", save_to, overwrite=False)

        # 別の行（ディレクトリ）を選び直して削除する
        row = self._names(panel).index("var")
        self._click(panel, row, 3)
        self.assertEqual({i.row() for i in panel.tree_view.selectedIndexes()}, {row})
        with mock.patch.object(mod.QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes):
            panel._on_delete()
        manager.delete_item.assert_called_once_with(HOME + "/var", True)

        # 右クリックのメニューも、押した行が相手になる
        row = self._names(panel).index("image.bin")
        index = panel.model.index(row, 1)
        clicked = []

        def fake_exec(menu, *args, **kwargs):
            for action in menu.actions():
                if action.text() == "ダウンロード":
                    clicked.append(action.text())
                    action.trigger()
                    return action
            self.fail("メニューにダウンロードが無い")

        save_to = os.path.join(os.path.dirname(save_to), "image.bin")
        with mock.patch.object(mod.QMenu, "exec", fake_exec), \
             mock.patch.object(mod.QFileDialog, "getSaveFileName",
                               return_value=(save_to, "")):
            panel._show_context_menu(panel.tree_view.visualRect(index).center())
        self.assertEqual(clicked, ["ダウンロード"])
        self.assertEqual(manager.download_file.call_args,
                         mock.call(HOME + "/image.bin", save_to, overwrite=False))

    def test_a_refresh_drops_the_selection(self):
        """更新で選択が外れ、ダウンロード・削除は「選んでください」で止まること。"""
        from ui import sftp_panel as mod
        manager = self._manager()
        panel = self._panel(manager)
        self._click(panel, self._names(panel).index("boot.cfg"), 0)
        self.assertTrue(panel.tree_view.selectedIndexes(), "前提: 選べている")

        panel._update_file_list(_listing())
        self.app.processEvents()
        self.assertEqual(panel.tree_view.selectedIndexes(), [])
        self.assertFalse(panel.tree_view.currentIndex().isValid())
        with mock.patch.object(mod.QMessageBox, "information") as info:
            panel._on_download()
            panel._on_delete()
        self.assertEqual([c.args[2] for c in info.call_args_list],
                         ["ダウンロードするファイルを選択してください。",
                          "削除するアイテムを選択してください。"])
        manager.download_file.assert_not_called()
        manager.delete_item.assert_not_called()

    def test_entries_removed_or_moved_on_the_device_leave_the_list(self):
        """削除・別のディレクトリへの移動・名前の変更のあと、元の行が消えること。

        SFTPManager は本物を使い、操作のあとの一覧の取り直し（別スレッド）から
        パネルの描き直しまでを通す。
        """
        from PyQt6.QtWidgets import QMessageBox
        from core.sftp_manager import SFTPManager
        from ui import sftp_panel as mod
        from ui.sftp_panel import SFTPPanel

        threads_before = set(threading.enumerate())
        manager = SFTPManager()
        manager.is_connected = True
        manager.current_path = HOME
        manager.sftp_client = _FakeSftp({
            HOME + "/etc": True, HOME + "/boot.cfg": False,
            HOME + "/image.bin": False, HOME + "/notes.txt": False})
        panel = SFTPPanel()
        panel.resize(800, 600)
        panel.show()

        def cleanup():
            panel.clear()
            for t in set(threading.enumerate()) - threads_before:
                t.join(10)
            self._flush()
            panel.close()
            panel.deleteLater()
            manager.deleteLater()
            self._flush()
        self.addCleanup(cleanup)

        warnings = []
        patcher = mock.patch.object(
            mod.QMessageBox, "warning",
            side_effect=lambda *a, **k: warnings.append(a[2]))
        patcher.start()
        self.addCleanup(patcher.stop)

        def info(name):
            from PyQt6.QtCore import Qt
            row = self._names(panel).index(name)
            return panel.model.item(row, 0).data(Qt.ItemDataRole.UserRole)

        panel.set_sftp_manager(manager, "rtrA")
        start = ["etc", "boot.cfg", "image.bin", "notes.txt"]
        self.assertTrue(self._wait(lambda: self._names(panel) == start),
                        "最初の一覧が出ない: %s %s" % (self._names(panel), warnings))

        with mock.patch.object(mod.QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes):
            panel._on_delete_selected(info("boot.cfg"))
        self.assertTrue(
            self._wait(lambda: self._names(panel) == ["etc", "image.bin", "notes.txt"]),
            "消したファイルの行が残っている: %s %s" % (self._names(panel), warnings))

        with mock.patch.object(mod.QInputDialog, "getText",
                               return_value=("etc/image.bin", True)):
            panel._on_rename_selected(info("image.bin"))
        self.assertTrue(
            self._wait(lambda: self._names(panel) == ["etc", "notes.txt"]),
            "別のディレクトリへ移した行が残っている: %s %s"
            % (self._names(panel), warnings))

        with mock.patch.object(mod.QInputDialog, "getText",
                               return_value=("notes.old", True)):
            panel._on_rename_selected(info("notes.txt"))
        self.assertTrue(
            self._wait(lambda: self._names(panel) == ["etc", "notes.old"]),
            "名前を変える前の行が残っている: %s %s" % (self._names(panel), warnings))

        self.assertEqual([panel.model.item(r, 0).text() for r in range(2)],
                         ["📁 etc", "📄 notes.old"])
        self.assertEqual(panel.model.columnCount(), 4)
        self.assertEqual(panel.status_label.text(), "2 項目")
        self.assertEqual(manager.sftp_client.paths,
                         {HOME + "/etc": True, HOME + "/etc/image.bin": False,
                          HOME + "/notes.old": False})
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
