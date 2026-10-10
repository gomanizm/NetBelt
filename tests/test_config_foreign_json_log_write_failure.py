"""他アプリの config.json と判定した後でログを書けなくても、そのフォルダに触らないことを検証する。

何が起きていたか（91dee14、Codex の REST-01）: 他アプリの JSON と判定したときの
ログの行（_refuse_foreign_config の「Not a NetBelt config file」）の print が
例外を出すと、_load_config の except で破損扱いへ進んでいた。配布版は stdout を
ログファイルへ向けるので（main.py の _setup_logging）、ドライブの容量不足などで
書けないと print が OSError を出す。
- 1 回だけ失敗して、次の行から書ける: except の行は書けるので load_error が立ち、
  他アプリの config.json のバックアップを作り、その整理（_cleanup_old_backups）が
  同じフォルダの config.json.backup_* を新しい 5 件だけ残して消した（実測: 他人の
  7 件のうち 3 件）。本体は save_config が断るので変わらない。起動すると、
  読み込みエラー（破損扱い）と他アプリの JSON の、食い違う 2 つの知らせが出た。
- 書けない状態が続く: except の行も書けず、ConfigManager() が OSError で終わり、
  起動が失敗した（フォルダには触らない）。
同じ結果（バックアップと整理）は 9fee4af にもあった（最初の行が Default
グループの補いの行になるだけ）。

直し方: その print は書けなくても例外を出さない。加えて、他アプリの JSON と
判定した後に _load_config の except へ来ても（ほかの行が書けないなど）、
破損扱いにしない。ログの文面・件数・順番は変えない。

ログの出力先は、配布版と同じ層（TextIOWrapper(line_buffering) → BufferedWriter →
raw）で、raw の write だけを ENOSPC にしたものへ差し替える。BufferedWriter は
書けなかった分を残し、次の行と一緒に書き直す。

テストのフォルダの中の削除は記録だけに差し替える（直しを戻して流しても、
実際には消さない）。
"""
import builtins
import contextlib
import errno
import io
import os
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

# 他アプリの dict（test_config_foreign_json_left_alone.py と同じ形）
FOREIGN_DICT = (b'{\n'
                b'  "other_application_setting": "keep-me",\n'
                b'  "huge": 1e400,\n'
                b'  "dup": 1,\n'
                b'  "dup": 2,\n'
                b'  "settings": {"ftp_server": {"password": "plain-secret"}}\n'
                b'}\n')

# 他人が起動元に置いている、NetBelt と同じ名前の形のファイル（7 件。
# 整理は新しい 5 件だけ残すので、走れば古いものから消す）
OTHERS_BACKUPS = ["config.json.backup_2020010%d_000000" % i
                  for i in range(1, 8)]

# 他アプリの JSON と判定したときのログの行
VERDICT = b"Not a NetBelt config file"


def _snapshot(directory):
    """フォルダの中身（名前 -> バイト列）"""
    result = {}
    for name in os.listdir(directory):
        with open(os.path.join(directory, name), "rb") as f:
            result[name] = f.read()
    return result


class _Raw(io.RawIOBase):
    """ログファイルのいちばん下の層。marker を含む write から ENOSPC にする。

    once なら失敗は 1 回だけで、次の write から書ける。
    """

    def __init__(self, marker, once):
        super().__init__()
        self.marker = marker
        self.once = once
        self.failing = False
        self.failures = 0
        self.attempted = bytearray()   # 書けなかったバイト列（書こうとした行）
        self.written = bytearray()

    def writable(self):
        return True

    def write(self, data):
        data = bytes(data)
        if self.marker is not None and self.marker in data:
            self.failing = True
        if self.failing and not (self.once and self.failures):
            self.failures += 1
            self.attempted += data
            raise OSError(errno.ENOSPC, "No space left on device")
        self.written += data
        return len(data)


class ForeignConfigLogWriteFailureTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-foreign-log-")
        self.path = os.path.join(self.dir, "config.json")
        home = mock.patch(
            "core.config_manager.app_data_dir",
            return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)
        with open(self.path, "wb") as f:
            f.write(FOREIGN_DICT)
        for name in OTHERS_BACKUPS:
            with open(os.path.join(self.dir, name), "wb") as f:
                f.write(b"someone else's backup " + name.encode())
        self.before = _snapshot(self.dir)
        self.removed = []   # このフォルダの中で消そうとしたファイル
        self._record_removals()

    def _inside(self, path):
        try:
            parent = os.path.dirname(os.path.abspath(os.fspath(path)))
        except TypeError:
            return False
        return os.path.normcase(parent) == os.path.normcase(
            os.path.abspath(self.dir))

    def _record_removals(self):
        """このフォルダの中の削除は記録だけにする（ほかの場所は今までどおり）"""
        real_unlink = pathlib.Path.unlink
        real_remove = os.remove
        real_os_unlink = os.unlink

        def path_unlink(path, *args, **kwargs):
            if self._inside(path):
                self.removed.append(os.path.basename(os.fspath(path)))
                return None
            return real_unlink(path, *args, **kwargs)

        def remove(path, *args, **kwargs):
            if self._inside(path):
                self.removed.append(os.path.basename(os.fspath(path)))
                return None
            return real_remove(path, *args, **kwargs)

        def os_unlink(path, *args, **kwargs):
            if self._inside(path):
                self.removed.append(os.path.basename(os.fspath(path)))
                return None
            return real_os_unlink(path, *args, **kwargs)

        for patcher in (mock.patch.object(pathlib.Path, "unlink", path_unlink),
                        mock.patch("os.remove", remove),
                        mock.patch("os.unlink", os_unlink)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _log(self, marker, once):
        """配布版の stdout と同じ層の出力（main.py の open(..., buffering=1)）"""
        raw = _Raw(marker, once)
        stream = io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8",
                                  line_buffering=True)

        def close():
            raw.marker = None
            raw.failing = False
            stream.close()
        self.addCleanup(close)
        return raw, stream

    def _construct(self, stream):
        from core.config_manager import ConfigManager
        with contextlib.redirect_stdout(stream):
            cm = ConfigManager(config_path=self.path)
            saved = cm.save_config()
        return cm, saved

    def _assert_left_alone(self, cm, saved):
        self.assertEqual(self.removed, [],
                         "他アプリのフォルダのファイルを消そうとした")
        self.assertEqual(_snapshot(self.dir), self.before,
                         "起動元のフォルダの中身が変わった（バックアップの作成など）")
        self.assertIsNone(cm.backup_path, "他アプリのファイルをバックアップした")
        self.assertIsNone(cm.load_error,
                          "破損扱い（次の保存で作り直す）にしてはいけない")
        self.assertEqual(cm.foreign_config_path, os.path.abspath(self.path))
        self.assertIn(os.path.abspath(self.path), cm.load_warning or "")
        self.assertIn("NetBelt の設定ファイルではない", cm.load_warning)
        self.assertFalse(saved, "保存できたことにしてはいけない")
        # 画面は既定の設定で動く（グループ一覧が引ける）
        self.assertIsInstance(cm.get_groups(), list)

    def test_the_verdict_line_failing_once_leaves_the_folder_alone(self):
        """判定の行の書き込みが 1 回だけ失敗しても、バックアップも整理もしない"""
        raw, stream = self._log(VERDICT, once=True)

        cm, saved = self._construct(stream)

        self.assertEqual(raw.failures, 1,
                         "前提: 判定の行の書き込みを 1 回だけ失敗させていない")
        self.assertIn(VERDICT, bytes(raw.attempted))
        self._assert_left_alone(cm, saved)

    def test_the_verdict_line_that_cannot_be_written_does_not_stop_the_start(self):
        """判定の行から書けない状態が続いても、例外で終わらず、フォルダにも触らない"""
        raw, stream = self._log(VERDICT, once=False)

        cm, saved = self._construct(stream)

        self.assertIn(VERDICT, bytes(raw.attempted),
                      "前提: 判定の行を書こうとしていない")
        self._assert_left_alone(cm, saved)

    def test_an_error_after_the_verdict_is_not_treated_as_a_broken_file(self):
        """判定の後でほかの行が書けずに例外が出ても、破損扱いにしない（except の側の防御）

        判定の後の _load_default_config は、同梱の default_config.json が読めないと
        その旨を print してから最小の設定を返す。その行が書けないと、例外が
        _load_config の except へ届く。ここでは同梱のファイルを読めなくし、
        その行の書き込みを 1 回だけ失敗させる。
        """
        real_open = builtins.open

        def open_but_not_the_default_config(file, *args, **kwargs):
            if (isinstance(file, (str, os.PathLike))
                    and os.path.basename(os.fspath(file)) == "default_config.json"):
                raise OSError(errno.EACCES, "Permission denied (test)")
            return real_open(file, *args, **kwargs)

        marker = "デフォルト設定ファイルの読み込みエラー".encode("utf-8")
        raw, stream = self._log(marker, once=True)

        with mock.patch("builtins.open", open_but_not_the_default_config):
            cm, saved = self._construct(stream)

        self.assertEqual(raw.failures, 1,
                         "前提: 既定の設定の行の書き込みを 1 回だけ失敗させていない")
        self.assertIn(VERDICT, bytes(raw.written),
                      "前提: 判定の行は書けている")
        self._assert_left_alone(cm, saved)


class MainWindowForeignConfigLogWriteFailureTest(unittest.TestCase):
    """起動（MainWindow）でも、知らせは他アプリの JSON の 1 つだけで、フォルダに触らない"""

    _windows = []   # 配送待ちシグナルの宛先を先に解放しない（他テストと同じ理由）

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        cls._windows.clear()

    def setUp(self):
        # MainWindow は CWD の config.json を読むので、一時ディレクトリへ移る
        self.dir = tempfile.mkdtemp(prefix="netbelt-foreign-log-win-")
        self.path = os.path.join(self.dir, "config.json")
        prev = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, prev)
        home = mock.patch(
            "core.config_manager.app_data_dir",
            return_value=Path(tempfile.mkdtemp(prefix="netbelt-testhome-")))
        home.start()
        self.addCleanup(home.stop)

    def test_one_notice_and_the_folder_left_alone(self):
        from ui.main_window import MainWindow
        with open(self.path, "wb") as f:
            f.write(FOREIGN_DICT)
        for name in OTHERS_BACKUPS:
            with open(os.path.join(self.dir, name), "wb") as f:
                f.write(b"someone else's backup " + name.encode())
        before = _snapshot(self.dir)
        removed = []
        real_unlink = pathlib.Path.unlink
        here = os.path.normcase(os.path.abspath(self.dir))

        def path_unlink(path, *args, **kwargs):
            if os.path.normcase(os.path.dirname(os.path.abspath(path))) == here:
                removed.append(path.name)
                return None
            return real_unlink(path, *args, **kwargs)

        raw = _Raw(VERDICT, once=True)
        stream = io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8",
                                  line_buffering=True)
        self.addCleanup(stream.close)
        with contextlib.redirect_stdout(stream), \
                mock.patch.object(pathlib.Path, "unlink", path_unlink), \
                mock.patch("PyQt6.QtWidgets.QMessageBox.warning") as warning:
            w = MainWindow()
            self._windows.append(w)
            w._save_layout()   # 閉じるときと同じ保存

        self.assertEqual(raw.failures, 1,
                         "前提: 判定の行の書き込みを 1 回だけ失敗させていない")
        self.assertEqual(removed, [], "他アプリのフォルダのファイルを消そうとした")
        self.assertEqual(_snapshot(self.dir), before)
        self.assertIsNone(w.config_manager.load_error)
        self.assertEqual(warning.call_count, 1,
                         "知らせは他アプリの JSON の 1 つだけ: %r"
                         % warning.call_args_list)
        self.assertIn(os.path.abspath("config.json"), warning.call_args.args[2])
        self.assertIn("NetBelt の設定ファイルではない", warning.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
