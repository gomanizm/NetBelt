"""『記録したバイト数』に、まだ記録先へ送っていない分を入れないことを検証する（Codex 12 回目 term-01）。

何が起きていたか（1d9d5c5 で実測。scratchpad\\cx132i-term\\repro_term01.py と
repro_1d9d5c5.txt）: 記録ファイルは open(path, 'w', encoding='utf-8',
buffering=1) の行バッファリングのテキストモードで開く。改行の無い受信（機器の
プロンプト 'router# ' など）は、write() が戻っても Python のバッファに残り、
記録先へ送られるのは続く flush() のとき。ところが LogWriter の書き込み
スレッド（src/core/log_writer.py の _Core.run）は write() が戻った直後に
書けたバイト数へ足していたので、その flush() が記録先で詰まっている間、
記録中ダイアログは『記録したバイト数: 8 バイト』と出し、ファイルは 0 バイト
だった。flush() だけを止める差し替えでも、OS への書き込み（FileIO.write）
だけを止めて本物の BufferedWriter と行バッファリングの TextIOWrapper を
重ねた差し替えでも同じ。flush() が失敗したときも、書けていない 8 バイトを
数えたままだった。CHANGELOG の『記録先へ書けた分』と合わない（441ea02 は
flush() が戻ってから数えていた）。

どう直したか: write() で渡した分は『まだ記録先へ送ったと確かめていない分』と
して別に数え、flush() か close() が戻ったときに、書けたバイト数へ移す。
失敗して戻ったときは移さない。記録の中身・順序と、書き込みの呼び方は変えない。
"""
import builtins
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

PROMPT = "router# "     # 改行の無い受信（機器のプロンプト）


class _FlushStallFile:
    """write はそのまま通し、flush だけが放されるまで戻らない記録ファイル。

    flush に入ったら entered を立てる。放されないまま limit 秒たったら戻る
    （直す前の作りでテストが止まったままにならないように）。fail を渡すと、
    flush は止まらずにその例外で失敗する（バッファは送らない）。
    """

    def __init__(self, f, release, limit=5.0, fail=None):
        self._f = f
        self.name = f.name
        self._release = release
        self._limit = limit
        self._fail = fail
        self.entered = threading.Event()

    def write(self, text):
        return self._f.write(text)

    def flush(self):
        self.entered.set()
        if self._fail is not None:
            raise self._fail
        self._release.wait(self._limit)
        return self._f.flush()

    def close(self):
        return self._f.close()

    @property
    def closed(self):
        return self._f.closed


class _StallingRaw(io.FileIO):
    """OS への書き込み（生の write）だけが、放されるまで戻らないファイル。

    詰まった共有フォルダと同じく、Python のバッファから記録先へ送る所で止まる。
    """

    def __init__(self, path, release, limit=5.0):
        super().__init__(path, "w")
        self._release = release
        self._limit = limit
        self.entered = threading.Event()

    def write(self, b):
        self.entered.set()
        self._release.wait(self._limit)
        return super().write(b)


class _Named:
    """TextIOWrapper に name を持たせる包み（LogWriter は f.name を読む）"""

    def __init__(self, f, name):
        self._f = f
        self.name = name

    def write(self, text):
        return self._f.write(text)

    def flush(self):
        return self._f.flush()

    def close(self):
        return self._f.close()

    @property
    def closed(self):
        return self._f.closed


def _size(path):
    return os.path.getsize(path)


class LogWriterCountsFlushedBytesTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(
            tempfile.mkdtemp(prefix="netbelt-logwriter-count-"), "a.log")

    def _release(self):
        """詰まりを解く合図。後始末で必ず解く（書き込みスレッドを残さない）。"""
        release = threading.Event()
        self.addCleanup(release.set)
        return release

    def _open(self):
        # 記録を始めるときと同じ開き方（src/ui/terminal_widget.py）
        f = open(self.path, "w", encoding="utf-8", buffering=1)
        self.addCleanup(f.close)
        return f

    def test_a_prompt_waiting_in_a_stalled_flush_is_not_counted(self):
        """flush が詰まっている間、Python のバッファに残るプロンプトを数えないこと。"""
        from core.log_writer import LogWriter
        release = self._release()
        raw = _FlushStallFile(self._open(), release)
        w = LogWriter(raw)

        w.write(PROMPT)
        w.flush()                          # 詰まり、待たずに積む側へ回る
        self.assertTrue(raw.entered.wait(3.0), "前提: flush に入っていない")
        self.assertTrue(w.busy, "前提: flush が詰まっていない")
        self.assertEqual(_size(self.path), 0,
                         "前提: 改行の無い文字列が write の時点で記録先へ出た")
        self.assertEqual(w.written_bytes, 0,
                         "記録先へまだ送っていないプロンプトを、書けたバイト数に数えた")

        release.set()
        self.assertTrue(w.wait(3.0), "詰まりが解けても flush が終わらない")
        self.assertEqual(_size(self.path), len(PROMPT))
        self.assertEqual(w.written_bytes, _size(self.path),
                         "flush が戻ったのに、書けた分が数えられない")
        w.close()
        self.assertTrue(w.wait(3.0))
        self.assertEqual(w.written_bytes, len(PROMPT), "閉じたら数が変わった")

    def test_a_prompt_waiting_in_a_stalled_os_write_is_not_counted(self):
        """OS への書き込みが詰まっている間も、送れていないプロンプトを数えないこと。

        本物の BufferedWriter と行バッファリングの TextIOWrapper を重ね、止めるのは
        その下の生の write だけ（flush を差し替えない形）。
        """
        from core.log_writer import LogWriter
        release = threading.Event()
        raw = _StallingRaw(self.path, release)
        f = io.TextIOWrapper(io.BufferedWriter(raw), encoding="utf-8",
                             line_buffering=True)
        self.addCleanup(f.close)
        self.addCleanup(release.set)       # f.close より先に解く
        w = LogWriter(_Named(f, self.path))

        w.write(PROMPT)
        w.flush()
        self.assertTrue(raw.entered.wait(3.0), "前提: OS への書き込みに入っていない")
        self.assertTrue(w.busy, "前提: 書き込みが詰まっていない")
        self.assertEqual(_size(self.path), 0, "前提: 記録先へまだ出ていない")
        self.assertEqual(w.written_bytes, 0,
                         "記録先へまだ送っていないプロンプトを、書けたバイト数に数えた")

        release.set()
        self.assertTrue(w.wait(3.0), "詰まりが解けても書き終わらない")
        self.assertEqual(w.written_bytes, _size(self.path))
        self.assertEqual(w.written_bytes, len(PROMPT))
        w.close()
        self.assertTrue(w.wait(3.0))

    def test_a_prompt_whose_flush_failed_is_not_counted(self):
        """flush が失敗したら、送れなかったプロンプトを書けたバイト数に入れないこと。"""
        from core.log_writer import LogWriter
        raw = _FlushStallFile(self._open(), self._release(),
                              fail=OSError(64, "The specified network name is "
                                               "no longer available"))
        w = LogWriter(raw)
        w.write(PROMPT)
        with self.assertRaises(OSError):
            w.flush()
        self.assertEqual(_size(self.path), 0, "前提: 記録先へ出ていない")
        self.assertEqual(w.written_bytes, 0,
                         "失敗して送れなかったプロンプトを、書けたバイト数に数えた")
        with self.assertRaises(OSError):
            w.close()
        self.assertTrue(w.wait(3.0))

    def test_text_that_reaches_the_file_on_close_is_counted(self):
        """flush を挟まずに閉じても、閉じて記録先へ出た分は数えること。"""
        from core.log_writer import LogWriter
        w = LogWriter(self._open())
        w.write("line\n" + PROMPT)
        w.close()
        self.assertTrue(w.wait(3.0))
        self.assertTrue(w.closed)
        self.assertEqual(_size(self.path),
                         len("line" + os.linesep + PROMPT))
        self.assertEqual(w.written_bytes, _size(self.path),
                         "閉じて書けた分が数えられていない")


def _pump(ms):
    """ms ミリ秒だけイベントループを回す。"""
    from PyQt6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class RecordingDialogCountsFlushedBytesTest(unittest.TestCase):
    """端末の記録中ダイアログの『記録したバイト数』で確かめる。"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "rtrA")
        self.dir = tempfile.mkdtemp(prefix="netbelt-logcount-")
        mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    def _wait_until(predicate, seconds=3.0):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            _pump(20)
        return predicate()

    def test_dialog_shows_only_bytes_that_reached_the_file(self):
        """flush が詰まっている間、ダイアログがプロンプトの分を記録したと出さないこと。"""
        from core import log_recording
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        w.create_terminal_tab("rtrA")
        release = threading.Event()
        self.addCleanup(release.set)
        path = os.path.join(self.dir, "rtrA.log")
        real_open = builtins.open
        stalls = []

        def fake_open(file, mode="r", *args, **kwargs):
            f = real_open(file, mode, *args, **kwargs)
            if isinstance(file, str) and file == path and "w" in mode:
                stalls.append(_FlushStallFile(f, release))
                return stalls[-1]
            return f

        with mock.patch("builtins.open", fake_open), \
             mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        self.assertIn("rtrA", w._log_files, "前提: 記録が始まっている")
        dialog = w._log_dialogs["rtrA"]

        w.queue_output("rtrA", PROMPT)
        self.assertTrue(self._wait_until(stalls[0].entered.is_set),
                        "前提: 受信を記録へ書いていない")
        _pump(50)
        self.assertIn(PROMPT.strip(), w._terminals["rtrA"].toPlainText(),
                      "前提: 画面にプロンプトが出ていない")
        self.assertTrue(w._log_files["rtrA"].busy, "前提: flush が詰まっていない")
        self.assertEqual(_size(path), 0, "前提: 記録先へまだ出ていない")
        dialog.timer.timeout.emit()        # 1 秒ごとの表示の更新
        self.assertEqual(dialog.bytes_label.text(), "記録したバイト数: 0 バイト",
                         "記録先へまだ書けていないプロンプトを、記録したと出した")
        self.assertEqual(w.recorded_bytes("rtrA"), 0)

        release.set()
        self.assertTrue(self._wait_until(
            lambda: not w._log_files["rtrA"].busy), "詰まりが解けても書き終わらない")
        dialog.timer.timeout.emit()
        self.assertEqual(_size(path), len(PROMPT))
        self.assertEqual(w.recorded_bytes("rtrA"), len(PROMPT))
        self.assertEqual(dialog.bytes_label.text(),
                         "記録したバイト数: %d バイト" % len(PROMPT))

        w.stop_log_recording("rtrA")
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), PROMPT, "記録の中身が変わった")


if __name__ == "__main__":
    unittest.main()
