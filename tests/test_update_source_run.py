"""ソースから実行しているときに、バイナリ更新を当てないことを確認する。

配布 ZIP はビルド済みの NetBelt.exe 一式で、ソース実行時の「インストール
先」はリポジトリ直下になる。実測（inspector, release #9）: 適用すると
app_dir が `['LICENSE', 'NetBelt.exe', 'README.md', 'src', 'updater.bat']`
になり、追跡している README.md が `'DIST README'`、updater.bat が
`'rem DIST updater'` で上書きされた。再起動先は引数の無い
`...\\netbelt-venv\\Scripts\\python.exe` なので、アプリは終了して素の
Python コンソールが開く。ソースは変わらないため、次の起動でも同じ更新を
また勧めてくる。

凍結された exe では従来どおり適用できること（当て過ぎでないこと）も
同じ形で確かめる。
"""
import hashlib
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
import unittest.mock

sys.path.insert(0, "src")

HERE = os.path.abspath(__file__)

def place_verified_zip(directory, version):
    """検証を通った ZIP 一式（本体・.sha256・.version）を置く。

    適用の直前に「表示した版と同じ、検証を通った ZIP か」を確かめるので、
    起動経路を試すにはこの3つが揃っている必要がある。
    """
    body = b"PK" + version.encode("ascii") * 8
    path = os.path.join(directory, "NetBelt-%s.zip" % version)
    io.open(path, "wb").write(body)
    io.open(path + ".sha256", "w").write(hashlib.sha256(body).hexdigest())
    io.open(path + ".version", "w").write(version)
    return path



class _PopenSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return unittest.mock.MagicMock()


def _frozen(value):
    return unittest.mock.patch.object(sys, "frozen", value, create=True)


class SourceRunApplyTest(unittest.TestCase):
    """「更新を適用」の2経路が、ソース実行では updater.bat を起動しないこと。"""

    def setUp(self):
        self.popen = _PopenSpy()
        self.told = []
        patches = [
            unittest.mock.patch("subprocess.Popen", self.popen),
            unittest.mock.patch(
                "PyQt6.QtWidgets.QMessageBox.information",
                lambda *a, **k: self.told.append(a[2] if len(a) > 2 else "")),
            unittest.mock.patch(
                "PyQt6.QtWidgets.QMessageBox.critical",
                lambda *a, **k: self.told.append(a[2] if len(a) > 2 else "")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _dialog(self):
        from ui.dialogs.update_dialog import UpdateDialog
        tmp = tempfile.mkdtemp(prefix="netbelt-source-")
        self.addCleanup(shutil.rmtree, tmp, True)
        dialog = UpdateDialog(None, {"version": "9.9.9"})
        self.addCleanup(dialog.deleteLater)
        dialog.downloaded_zip_path = place_verified_zip(tmp, "9.9.9")
        return dialog

    def test_the_dialog_does_not_launch_the_updater_from_source(self):
        dialog = self._dialog()
        with _frozen(False):
            dialog._on_apply_clicked()

        self.assertEqual(self.popen.calls, [],
                         "ソースツリーへ配布物を上書きしようとしている")
        self.assertTrue(self.told, "何も知らせずに黙って諦めている")

    def test_the_dialog_still_applies_from_a_frozen_build(self):
        dialog = self._dialog()
        with _frozen(True), unittest.mock.patch("os.path.exists",
                                                return_value=True):
            dialog._on_apply_clicked()

        self.assertEqual(len(self.popen.calls), 1,
                         "凍結ビルドでも適用できなくなっている")

    def test_the_pending_path_does_not_launch_the_updater_from_source(self):
        from ui.main_window import MainWindow

        with _frozen(False):
            MainWindow._apply_pending_update(types.SimpleNamespace(), HERE)

        self.assertEqual(self.popen.calls, [],
                         "ソースツリーへ配布物を上書きしようとしている")
        self.assertTrue(self.told, "何も知らせずに黙って諦めている")

    def test_the_pending_path_still_applies_from_a_frozen_build(self):
        from ui.main_window import MainWindow

        with _frozen(True):
            MainWindow._apply_pending_update(types.SimpleNamespace(), HERE)

        self.assertEqual(len(self.popen.calls), 1,
                         "凍結ビルドでも適用できなくなっている")


class SourceRunPromptTest(unittest.TestCase):
    """ソース実行では、そもそも「未適用の更新」を勧めないこと。"""

    def setUp(self):
        self.asked = []
        from PyQt6.QtWidgets import QMessageBox
        p = unittest.mock.patch.object(
            QMessageBox, "question",
            lambda *a, **k: (self.asked.append(True),
                             QMessageBox.StandardButton.No)[1])
        p.start()
        self.addCleanup(p.stop)

        from core.version_manager import VersionManager
        for name, value in (("get_pending_update_files", [HERE]),
                            ("is_verified_update", True),
                            ("pending_version", "99.9.9")):
            q = unittest.mock.patch.object(
                VersionManager, name,
                unittest.mock.Mock(return_value=value))
            q.start()
            self.addCleanup(q.stop)

    def _check(self):
        from ui.main_window import MainWindow
        MainWindow._check_pending_updates(types.SimpleNamespace())

    def test_no_prompt_when_running_from_source(self):
        with _frozen(False):
            self._check()
        self.assertEqual(self.asked, [],
                         "当てられない更新の適用を勧めている")

    def test_the_prompt_survives_in_a_frozen_build(self):
        with _frozen(True):
            self._check()
        self.assertEqual(len(self.asked), 1,
                         "凍結ビルドでも勧めなくなっている")


if __name__ == "__main__":
    unittest.main()
