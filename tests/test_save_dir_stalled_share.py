"""覚えた保存先フォルダが応答しない共有フォルダでも、保存ダイアログを出す前に GUI が止まらないことを検証する（termlog 近くの経路）。

何が起きていたか（ebbe593 で実測。441ea02 からある。scratchpad\\cx132b-termlog\\
insp_lastdir.py）: 応答しなくなった共有フォルダへ記録を始めると、
save_defaults.remember がそのフォルダを「前回保存した場所」として覚える。その
あと別の機器の記録をローカルへ始めると、保存ダイアログを出す前の
save_defaults.initial_path → last_dir の os.path.isdir が、選んでもいないその
共有フォルダを GUI スレッドで見に行って止まった（isdir を 2 秒止まる代役に
すると、記録の開始に 2.00 秒かかった。本物の SMB では数十秒になりうる）。
SNMP / Syslog / サーバログのエクスポートも同じ initial_path を通る。止まらずに
返ったとしても、ネイティブの保存ダイアログはその共有フォルダを初期位置に開く。

どう直したか: last_dir の実在の確認を別スレッドで行い、GUI スレッドは
DIR_CHECK_WAIT 秒（1 秒）までしか待たない。答えが出なければ覚えたフォルダを
使わず、これまでの既定の場所（端末は cwd/logs、エクスポートはダイアログの
現在地）から開く。前の問い合わせがまだ戻っていないフォルダは、待たずに既定へ
落とす（詰まっている間にダイアログを何度開いても、止まった問い合わせを
積み上げない）。応答が戻れば、次からは覚えたフォルダをまた使う。

比べた案（実測）: 「閉じ終わっていない記録のフォルダは使わない」（案 b）は
待ちが 0 秒になるが、応答する共有フォルダへ rtrA を記録中に rtrB の記録を
始めるだけで、保存ダイアログが共有フォルダではなく cwd/logs から開くように
なり、普段の使い方が変わる（別のフォルダへ保存しかねない）。上限つきの確認
（案 a）は、応答するフォルダではこれまでどおり覚えたフォルダから開く。
"""
import builtins
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

STUCK_LIMIT = 5.0      # 代役の isdir が放されずに戻るまでの秒数（直す前の作りで止まったままにしない）


class _StuckFolder:
    """指定したフォルダへの os.path.isdir だけ、放されるまで戻らない（応答しない共有フォルダの代わり）。"""

    def __init__(self, folder):
        self._folder = os.path.normcase(os.path.abspath(folder))
        self._real = os.path.isdir
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.on_gui_thread = []        # 呼ばれるたびに「GUI スレッドからか」を積む
        self.inside = 0                # いま戻らずにいる呼び出しの数

    def __call__(self, path):
        try:
            hit = os.path.normcase(os.path.abspath(path)) == self._folder
        except (TypeError, ValueError):
            hit = False
        if hit:
            with self.lock:
                self.on_gui_thread.append(
                    threading.current_thread() is threading.main_thread())
                self.inside += 1
            try:
                self.release.wait(STUCK_LIMIT)
            finally:
                with self.lock:
                    self.inside -= 1
        return self._real(path)


class _StallingFile:
    """放されるまで write / flush / close が戻らない記録ファイル。"""

    def __init__(self, f, release):
        self._f = f
        self.name = f.name
        self._release = release

    def write(self, text):
        self._release.wait(STUCK_LIMIT)
        return self._f.write(text)

    def flush(self):
        self._release.wait(STUCK_LIMIT)
        return self._f.flush()

    def close(self):
        self._release.wait(STUCK_LIMIT)
        return self._f.close()

    @property
    def closed(self):
        return self._f.closed


def _pump(ms):
    """ms ミリ秒だけイベントループを回す。"""
    from PyQt6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _wait_until(predicate, seconds=5.0):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end and not predicate():
        _pump(20)
    return predicate()


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


class SaveDirStalledShareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        root = tempfile.mkdtemp(prefix="netbelt-savedir-stuck-")
        self.addCleanup(shutil.rmtree, root, True)
        self.share = os.path.join(root, "share")
        self.local = os.path.join(root, "local")
        self.cwd = os.path.join(root, "cwd")
        for d in (self.share, self.local, self.cwd):
            os.makedirs(d)
        # cwd/logs を作られてもリポジトリを汚さないよう、作業場所を移す
        old_cwd = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, old_cwd)

        from core.config_manager import ConfigManager
        self.cm = ConfigManager(config_path=os.path.join(root, "config.json"))

        mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    def _stuck(self):
        """覚えた共有フォルダを応答しなくする。後始末で放し、戻らずにいる問い合わせを待つ"""
        stuck = _StuckFolder(self.share)
        patcher = mock.patch("os.path.isdir", stuck)
        patcher.start()
        self.addCleanup(lambda: _wait_until(lambda: stuck.inside == 0, 2 * STUCK_LIMIT))
        self.addCleanup(stuck.release.set)
        self.addCleanup(patcher.stop)
        return stuck

    def _start(self, w, name, path, release=None):
        """保存ダイアログで path を選んで記録を始める。ダイアログへ渡した初期パスを返す"""
        w.tab_widget.setCurrentWidget(w._terminals[name])
        opener = builtins.open

        def fake_open(file, mode="r", *args, **kwargs):
            f = opener(file, mode, *args, **kwargs)
            if release is not None and file == path and "w" in mode:
                return _StallingFile(f, release)
            return f

        with mock.patch("builtins.open", fake_open), \
             mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")) as dialog:
            w.start_log_recording()
        return dialog.call_args[0][2]

    def test_recording_start_does_not_wait_on_a_stalled_remembered_share(self):
        """詰まった共有フォルダを覚えていても、別の機器の記録開始で GUI が止まらないこと。"""
        from core import log_recording, save_defaults
        from ui.terminal_widget import TerminalWidget
        self.addCleanup(log_recording.stop, "rtrA")
        self.addCleanup(log_recording.stop, "rtrB")
        w = TerminalWidget(config_manager=self.cm)
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        w.create_terminal_tab("rtrB")

        # rtrA を共有フォルダへ記録し、その共有フォルダが応答しなくなる。
        # 後始末では放してから、停止した記録が閉じ終わるのを待つ
        release_a = threading.Event()
        dead = os.path.join(self.share, "rtrA.log")
        self.addCleanup(lambda: _wait_until(
            lambda: log_recording.device_using(dead) is None))
        self.addCleanup(release_a.set)
        self._start(w, "rtrA", dead, release=release_a)
        self.assertIn("rtrA", w._log_files, "前提: rtrA の記録が始まっている")
        w.queue_output("rtrA", "one\r\n")
        _pump(300)
        w.stop_log_recording("rtrA")
        self.assertEqual(_norm(self.cm.get_last_save_dir()), _norm(self.share),
                         "前提: 共有フォルダを前回保存した場所として覚えている")
        stuck = self._stuck()

        # rtrB の記録をローカルへ始める
        local = os.path.join(self.local, "rtrB.log")
        started = time.perf_counter()
        initial = self._start(w, "rtrB", local)
        took = time.perf_counter() - started
        self.addCleanup(w.stop_log_recording, "rtrB")

        self.assertNotIn(True, stuck.on_gui_thread,
                         "保存ダイアログを出す前に、GUI スレッドが応答しない共有フォルダを見に行った")
        self.assertLess(took, 3.0,
                        "応答しない共有フォルダの確認を待って、記録の開始に %.2f 秒かかった" % took)
        self.assertEqual(os.path.normcase(os.path.dirname(initial)),
                         _norm(os.path.join(self.cwd, "logs")),
                         "応答しない共有フォルダを保存ダイアログの初期位置にした"
                         "（既定の cwd/logs へ落ちていない）")
        self.assertIn("rtrB", w._log_files, "rtrB の記録が始まっていない")

        # 詰まっている間に開き直しても（エクスポートなども同じ入口）、待たずに
        # 既定へ落とし、止まった問い合わせを積み上げない。rtrB の記録開始で
        # 覚えた場所はローカルへ移ったので、共有フォルダを覚え直させる
        self.cm.set_last_save_dir(self.share)
        started = time.perf_counter()
        again = save_defaults.initial_path(self.cm, "export.txt")
        took = time.perf_counter() - started
        self.assertEqual(again, "export.txt",
                         "詰まっている共有フォルダを初期位置にした")
        self.assertLess(took, 0.5, "詰まっている間の開き直しで %.2f 秒待った" % took)
        self.assertLessEqual(stuck.inside, 1,
                             "応答しない共有フォルダへの問い合わせが積み上がった")
        self.assertNotIn(True, stuck.on_gui_thread)

        # 共有フォルダが応答を返したら、次からは覚えたフォルダをまた使う
        stuck.release.set()
        self.assertTrue(_wait_until(lambda: stuck.inside == 0),
                        "前提: 共有フォルダへの問い合わせが戻った")
        again = save_defaults.initial_path(self.cm, "export.txt")
        self.assertEqual(_norm(os.path.dirname(again)), _norm(self.share),
                         "応答が戻ったのに、覚えたフォルダを使わなくなった")

    def test_export_does_not_wait_on_a_stalled_remembered_share(self):
        """エクスポート（サーバログなど。SNMP / Syslog も同じ入口）でも GUI が止まらないこと。"""
        from PyQt6.QtWidgets import QWidget
        from ui.log_export import export_log_text
        self.cm.set_last_save_dir(self.share)
        stuck = self._stuck()
        panel = QWidget()
        self.addCleanup(panel.close)
        panel.config_manager = self.cm

        started = time.perf_counter()
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=("", "")) as dialog:
            saved = export_log_text(panel, "line\n", "tftp_log")
        took = time.perf_counter() - started
        self.assertIsNone(saved)
        self.assertNotIn(True, stuck.on_gui_thread,
                         "保存ダイアログを出す前に、GUI スレッドが応答しない共有フォルダを見に行った")
        self.assertLess(took, 3.0,
                        "応答しない共有フォルダの確認を待って、エクスポートの開始に %.2f 秒かかった" % took)
        initial = dialog.call_args[0][2]
        self.assertEqual(os.path.dirname(initial), "",
                         "応答しない共有フォルダを保存ダイアログの初期位置にした: %s" % initial)


if __name__ == "__main__":
    unittest.main()
