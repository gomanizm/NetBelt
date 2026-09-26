"""応答しない記録先の登録が残っていても、使用中の判定で GUI が止まらないことを検証する（termui-03）。

何が起きていたか（a3ad9b5 で実測。scratchpad\\cx132-check-termlog\\
chk_device_using.py）: 使用中の判定（core.log_recording.device_using）は、
登録済みの全パスへ GUI スレッドで os.path.samefile（= stat）をかけていた。
停止した記録の登録は、書き込みスレッドが閉じ終えるまで残る（先に外すと、
同じファイルへの次の記録が前の記録の書き込みと混ざる）。そのため、応答しない
共有フォルダへの記録を止めた（停止はすぐ戻る）あとで、別の機器の記録を
ローカルへ始める・同じ機器をローカルへ始め直す・SNMP / Syslog をローカルへ
エクスポートする、のたびに、選んでいない共有フォルダのパスを stat して
止まった（stat を 2 秒止まる代役にすると、どちらの記録開始も 2.00 秒）。
記録中のまま詰まっている記録でも同じ。

どう直したか: 記録先の実体（st_dev / st_ino）は、記録を始めたとき（開けた
直後）に覚える。判定では、新しく選ばれたパスだけを stat して、覚えた実体と
比べる。登録済みのパスはもう stat しない。実体を覚えられなかったパスは、
これまでどおり絶対化した文字列で比べる。

応答しない共有フォルダは、そのパスへの os.stat を「STAT_STALL 秒かかる」
ものへ差し替えて作る（記録ファイルも放されるまで書き込みが戻らない）。
"""
import builtins
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

# 応答しない共有フォルダのパスの stat にかかる秒数
STAT_STALL = 3.0


class _StallingFile:
    """放されるまで write / flush / close が戻らない記録ファイル（応答しない共有フォルダの代わり）。

    放されないまま limit 秒たったら戻る（直す前の作りでテストが止まったままに
    ならないように）。
    """

    def __init__(self, f, release, limit):
        self._f = f
        self.name = f.name
        self._release = release
        self._limit = limit

    def write(self, text):
        self._release.wait(self._limit)
        return self._f.write(text)

    def flush(self):
        self._release.wait(self._limit)
        return self._f.flush()

    def close(self):
        self._release.wait(self._limit)
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


class LogRecordingIoStallInUseCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "rtrA")
        self.addCleanup(log_recording.stop, "rtrB")
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-inuse-")
        self.warning = mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    def _wait_until(predicate, seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            _pump(20)
        return predicate()

    @staticmethod
    def _start(w, name, path, wrap=None):
        """name のタブで path への記録を始める。wrap(f) で記録ファイルを差し替える。"""
        real_open = builtins.open

        def fake_open(file, mode="r", *args, **kwargs):
            f = real_open(file, mode, *args, **kwargs)
            if (wrap is not None and isinstance(file, str) and file == path
                    and "w" in mode):
                return wrap(f)
            return f

        w.tab_widget.setCurrentWidget(w._terminals[name])
        with mock.patch("builtins.open", fake_open), \
             mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()

    def test_the_in_use_check_does_not_stat_a_stalled_recording(self):
        """詰まった記録を止めたあと、別の記録の開始と使用中の判定が、その記録先を stat しないこと。"""
        from core import log_recording
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        w.create_terminal_tab("rtrB")
        release = threading.Event()
        self.addCleanup(release.set)
        dead = os.path.join(self.dir, "share-rtrA.log")
        self._start(w, "rtrA", dead,
                    lambda f: _StallingFile(f, release, 20.0))
        handle = w._log_files.get("rtrA")
        self.assertIsNotNone(handle, "前提: rtrA の記録が始まっている")
        w.queue_output("rtrA", "one\r\n")
        self.assertTrue(self._wait_until(lambda: handle.busy, 2.0),
                        "前提: 記録先が詰まっている")
        w.stop_log_recording("rtrA")
        self.assertEqual(log_recording.device_using(dead), "rtrA",
                         "前提: 閉じ終わるまで使用中の登録が残っている")

        # ここから、共有フォルダ上の記録先の stat は STAT_STALL 秒戻らない
        real_stat = os.stat
        stalled_stats = []

        def slow_stat(path, *args, **kwargs):
            if isinstance(path, str) and os.path.normcase(
                    os.path.abspath(path)) == os.path.normcase(dead):
                stalled_stats.append(path)
                time.sleep(STAT_STALL)
            return real_stat(path, *args, **kwargs)
        stat_patch = mock.patch("os.stat", slow_stat)
        stat_patch.start()
        self.addCleanup(stat_patch.stop)

        local_b = os.path.join(self.dir, "local-rtrB.log")
        started = time.perf_counter()
        self._start(w, "rtrB", local_b)
        took_b = time.perf_counter() - started
        self.addCleanup(lambda: w.stop_log_recording("rtrB"))
        self.assertIn("rtrB", w._log_files, "別の機器の記録が始まらない")

        local_a = os.path.join(self.dir, "local-rtrA-2.log")
        started = time.perf_counter()
        self._start(w, "rtrA", local_a)
        took_a = time.perf_counter() - started
        self.addCleanup(lambda: w.stop_log_recording("rtrA"))
        self.assertIn("rtrA", w._log_files, "同じ機器の記録を始め直せない")

        # SNMP / Syslog のエクスポートも同じ判定を使う
        export = os.path.join(self.dir, "snmp-export.csv")
        started = time.perf_counter()
        self.assertIsNone(log_recording.device_using(export))
        took_export = time.perf_counter() - started

        self.assertEqual(
            stalled_stats, [],
            "使用中の判定が、選んでいない応答しない記録先を stat した"
            "（その間 GUI が止まる）")
        self.assertLess(took_b, 1.5, "別の機器の記録開始が %.2f 秒止まった" % took_b)
        self.assertLess(took_a, 1.5, "記録の始め直しが %.2f 秒止まった" % took_a)
        self.assertLess(took_export, 1.5,
                        "エクスポート先の判定が %.2f 秒止まった" % took_export)
        self.assertEqual(self.warning.call_count, 0)

        stat_patch.stop()
        release.set()
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(dead) is None, 3.0),
            "放したあと、閉じ終えた記録の登録が外れない")


if __name__ == "__main__":
    unittest.main()
