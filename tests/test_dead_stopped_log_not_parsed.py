"""書き込みに失敗した「死んだ」停止記録の区間を、もうパーサへ通さないことを検証する。

何が起きていたか: 停止して書き終えていない記録（TerminalWidget._closing_logs）への
書き込みが失敗すると、_write_logs はその項目の handle を None にして以後そこへ
書かない。ところが項目自体は _closing_logs に残るので、タブを閉じるときの
_log_pending_on_close は sum(entry[2]) にその区間を数え、誰も受け取らない文字を
描き待ちから take してパーサへ通していた。書かれるバイトは 0 なので、まるごと無駄。
実測（検査役 cx5c-termui-verify\\probe_dead_entry.py）: 16MiB を溜めて停止 →
1 片の書き込みを失敗させてからタブを閉じると、16,762,154 文字を通して GUI が
2.38 秒止まる。終了時（finish_log_recordings）でも同じく 1.91 秒。

どう直したか: 末尾の死んだ区間だけを take の対象から外す。単純に
entry[0] is not None で絞ると、_split_for_logs が先頭から順に配るため、死んだ区間が
前にあると後ろの生きた区間の文字を食ってしまう（2 つめのテストがその形）。
生きている最後の区間までの合計を take する。

ここでは時間ではなく「パーサへ通した文字数」を見る。混雑した CPU でも揺れず、
無駄がゼロになったことをそのまま確かめられるため。大きさは実測の 16MiB ではなく
数万文字に縮めてある（通す/通さないの差は大きさに比例するだけ）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")

LINE_LEN = 50       # _line() 1 行の文字数（"\r\n" を含む）


def _line(i):
    return "line %05d  abcdefghijklmnopqrstuvwxyz  0123456789\r\n" % i


def _logged(first, end):
    """その範囲がログファイルへ書かれたときの中身（CR は落ち、LF だけ残る）"""
    return "".join("line %05d  abcdefghijklmnopqrstuvwxyz  0123456789\n" % i
                   for i in range(first, end))


class _FailingHandle:
    """書き込むと必ず失敗する記録ファイル（ディスク満杯・共有フォルダの切断の代わり）"""

    def __init__(self, name):
        self.name = name
        self.closed = False

    def write(self, text):
        raise OSError(28, "No space left on device")

    def flush(self):
        pass

    def close(self):
        self.closed = True


class DeadStoppedLogNotParsedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from core import log_recording
        self.addCleanup(log_recording.stop, "dev")
        self.dir = tempfile.mkdtemp(prefix="netbelt-dead-entry-")
        mock.patch("PyQt6.QtWidgets.QMessageBox.warning").start()
        mock.patch("PyQt6.QtWidgets.QMessageBox.information").start()
        self.addCleanup(mock.patch.stopall)

    def _widget(self):
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(self._discard, w)
        w.create_terminal_tab("dev")
        return w

    @staticmethod
    def _discard(w):
        w._output_timer.stop()
        w._pending_output.clear()
        w._closing_logs.clear()
        w.close()

    def _start(self, w, name):
        path = os.path.join(self.dir, name)
        with mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        self.assertIn("dev", w._log_files, "前提: 記録が始まっている")
        return path

    @staticmethod
    def _kill_first_entry(w):
        """先頭の停止記録への書き込みを 1 片ぶん失敗させ、「死んだ」状態にする"""
        entry = w._closing_logs["dev"][0]
        real, entry[0] = entry[0], _FailingHandle(entry[1])
        real.close()
        w._flush_pending_output()
        assert entry[0] is None, "前提: 失敗した区間の handle が外れている"
        return entry

    @staticmethod
    def _count_parser_feeds(w):
        """以後パーサへ通した文字数を数える入れ物を返す"""
        terminal = w._terminals["dev"]
        parser = terminal._parser
        real_feed = parser.feed
        fed = []

        def counting_feed(text):
            fed.append(len(text))
            return real_feed(text)

        parser.feed = counting_feed
        return fed

    @staticmethod
    def _close_tab(w):
        index = w.tab_widget.indexOf(w._terminals["dev"])
        w._close_tab(index)

    @staticmethod
    def _read(path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_a_dead_trailing_entry_is_not_fed_to_the_parser(self):
        """末尾の区間が死んでいたら、その分をパーサへ通さないこと。"""
        w = self._widget()
        self._start(w, "old.log")
        w.queue_output("dev", "".join(_line(i) for i in range(400)))
        self.assertGreater(len(w._pending_output["dev"]), w.OUTPUT_SLICE,
                           "前提: 描き待ちが 2 片以上ある")
        w.stop_log_recording("dev")
        entry = self._kill_first_entry(w)
        self.assertGreater(entry[2], 0, "前提: 死んだ区間がまだ残っている")

        fed = self._count_parser_feeds(w)
        self._close_tab(w)

        self.assertEqual(sum(fed), 0,
                         "誰も受け取らない %d 文字をパーサへ通した" % sum(fed))

    def test_a_live_entry_behind_a_dead_one_still_gets_its_text(self):
        """死んだ区間の後ろに生きた区間があるなら、その分は今までどおり書くこと。

        死んだ区間を単に取り除くと、_split_for_logs は先頭から順に配るので、
        後ろの生きた区間ぶんの文字を死んだ区間が食ってしまう。
        """
        w = self._widget()
        self._start(w, "old.log")
        w.queue_output("dev", "".join(_line(i) for i in range(400)))
        w.stop_log_recording("dev")
        second = self._start(w, "new.log")
        w.queue_output("dev", "".join(_line(i) for i in range(400, 500)))
        w.stop_log_recording("dev")
        self.assertEqual(len(w._closing_logs["dev"]), 2,
                         "前提: 停止した記録が 2 件ある")
        self._kill_first_entry(w)

        self._close_tab(w)

        self.assertEqual(self._read(second), _logged(400, 500),
                         "生きた区間ぶんの受信が別の区間に食われた")


if __name__ == "__main__":
    unittest.main()
