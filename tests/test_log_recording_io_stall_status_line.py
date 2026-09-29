"""記録先の詰まりを、新しい窓ではなく記録中ダイアログの状態の行で知らせることを検証する（6 周目 term の検査役の指摘）。

何が起きていたか（31b45ee で実測。scratchpad\\cx132g-term\\logs の base_focus.txt・
base_focus_tab_other.txt・base_focus_tab_dev.txt・base_serial_focus.txt。
上限 256 KiB / 64 KiB）: 記録先の詰まりを知らせる窓（関所のある接続で受信を
止めたことの案内＝モーダルでない QMessageBox の show()、シリアルの書き込み待ちの
遅れの警告＝QMessageBox.warning のモーダル）は、出た瞬間に窓を活性化して
フォーカスを OK ボタンへ移した。'switchport trunk allowed vlan 10' と打ったところで
出ると、続けて打った ',20<Enter>' は 4 打鍵とも QPushButton へ行き、機器へ届いたのは
'switchport trunk allowed vlan 10'（CR なし）だけで、Enter は案内を閉じただけだった
（打っていたのが止めた機器のタブでも、ほかのタブでも、シリアルでも同じ）。あとで
Enter を押すと、欠けたコマンドが実行される（許可 VLAN が置き換わる）。案内が
キーを奪わない作り（WA_ShowWithoutActivating や主窓の活性化し直し）にすると、
今度は案内を閉じるつもりの Enter が CR として機器へ送られる。

どう直したか（作り手の親の決定。誤送信の余地を残さない側）: 記録先の詰まりの
知らせのために新しい窓を出さない（窓の活性化もフォーカスの移動もしない）。
記録中ずっと出ている記録中ダイアログ（LogRecordingDialog）に状態の行を足し、
関所のある接続で受信を止めている間は『受信を止めています』の趣旨を、シリアルで
書き込み待ちが上限を超えている間は『メモリに溜めています』の趣旨を出し、解けたら
行を消す（空にして隠す）。行を書き換えても、ダイアログを前に出さず活性化もしない。
止めては再開するのを繰り返しても、そのたびに行が出て消えるだけ（窓は出ない）。
記録を停止したあとは記録中ダイアログが無いので、何も出さない（受信の止め・再開は
これまでどおり）。

窓が出ないことは、QMessageBox の information / warning / critical / question /
exec / open を開かずにすぐ戻る偽物に替えて数え、show は数えてから本物を呼ぶ
（直す前の作りの案内は本物どおりに出て、キーを奪う）。加えて、表に出ている
QMessageBox を 10ms ごとに探す。キーは、本物のキーボードと同じく、そのとき
フォーカスのあるウィジェット（QApplication.focusWidget()）へ送る。上限は
インスタンスで小さくする（256 KiB / 64 KiB）。

状態の行は切れずに全部見えること（7 周目 term の検査役の指摘。5fcd235 で実測）:
記録中ダイアログは表示した時点の大きさのままで、隠していた行（折り返しあり）を
後から出しても、窓は最小の高さ（1 行分）までしか伸びず、2 行目から先が切れていた
（offscreen で行の高さ 25、要る高さ 54。Windows のフォントで 36 と 78）。行の
高さが、その幅で要る高さ（heightForWidth）以上あることを確かめる。

行を書き換えても記録中ダイアログを前に出さないこと: offscreen では raise_ で活性が
移らないので、主窓の活性を見るだけでは、set_status が raise_ を呼んでも見逃す
（7 周目 term の検査役の変異 raise_only）。本物の Windows で前に出すと、活性化を
伴ってキーの横取りへ戻りうる。LogRecordingDialog の raise_ / activateWindow /
show / showNormal / setFocus / open / exec を数える偽物に替えて、0 回を確かめる。

停止した記録だけが遅れている間も窓を出さないこと: 描き待ちが無いときに停止した
記録は、すぐ閉じ終わりの待ち（_closing_writers）へ移り、書き込み待ちとして数え
られない。描き待ちを残したまま停止すると、停止した記録は描き終わるまで
_closing_logs に残り、上限を超えた書き込み待ちを持つ（ダイアログは無い）。
その状態で見回りを回しても窓を出さないことを確かめる（検査役の変異 closingwin）。
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

FIRST = "switchport trunk allowed vlan 10"
REST = ",20\r"


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


class _SlowFile:
    """書くたびに delay 秒かかるが、必ず応答する記録ファイル（応答の遅い共有フォルダの代わり）。"""

    def __init__(self, f, delay):
        self._f = f
        self.name = f.name
        self._delay = delay

    def write(self, text):
        time.sleep(self._delay)
        return self._f.write(text)

    def flush(self):
        return self._f.flush()

    def close(self):
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


def _chunk(name, first, count):
    return "".join("%s%06d %s\r\n" % (name, i, "y" * 50)
                   for i in range(first, first + count))


def _type(text):
    """本物のキーボードと同じく、いまフォーカスのあるウィジェットへ 1 文字ずつ打つ。

    打鍵ごとに届けた先の型名を返す。
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    targets = []
    for ch in text:
        target = QApplication.focusWidget() or QApplication.activeWindow()
        targets.append(type(target).__name__)
        if ch == "\r":
            QTest.keyClick(target, Qt.Key.Key_Return)
        else:
            QTest.keyClick(target, ch)
        _pump(5)
    return targets


class LogRecordingIoStallStatusLineTest(unittest.TestCase):
    HIGH = 256 * 1024
    LOW = 64 * 1024
    LINES = 512            # 1 回に受信させる行数（約 30 KB）
    # 遅い記録先の 1 回の書き込みにかかる秒数。LogWriter.STALL_WAIT（0.1 秒）より
    # 長くする。短いと GUI が書き終わりを待つので、書き込み待ちが溜まらない
    SLOW = 0.15

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QMessageBox, QWidget
        self.dir = tempfile.mkdtemp(prefix="netbelt-logstall-status-")
        # 窓を出した呼び出し（どれで, 本文）と、表に出た QMessageBox の本文
        self.calls = []
        self.boxes = []
        self._seen = set()

        def static(kind):
            def fake(parent, title, text, *args, **kwargs):
                self.calls.append((kind, text))
                return QMessageBox.StandardButton.Ok
            return fake
        for kind in ("information", "warning", "critical", "question"):
            mock.patch.object(QMessageBox, kind, side_effect=static(kind)).start()

        def fake_exec(box, *args):
            self.calls.append(("exec", box.text()))
            return QMessageBox.StandardButton.Ok.value

        def fake_open(box, *args):
            self.calls.append(("open", box.text()))

        def recording_show(box):
            self.calls.append(("show", box.text()))
            QWidget.show(box)
        mock.patch.object(QMessageBox, "exec", fake_exec).start()
        mock.patch.object(QMessageBox, "open", fake_open).start()
        mock.patch.object(QMessageBox, "show", recording_show).start()
        self.addCleanup(mock.patch.stopall)
        self._scan_timer = QTimer()
        self._scan_timer.setInterval(10)
        self._scan_timer.timeout.connect(self._scan)
        self._scan_timer.start()
        self.addCleanup(self._close_boxes)
        self.addCleanup(self._scan_timer.stop)
        self.releases = []
        self.addCleanup(lambda: [release.set() for release in self.releases])

    def _scan(self):
        """表に出ている QMessageBox を覚える。"""
        from PyQt6.QtWidgets import QApplication, QMessageBox
        for widget in QApplication.topLevelWidgets():
            if (isinstance(widget, QMessageBox) and widget.isVisible()
                    and id(widget) not in self._seen):
                self._seen.add(id(widget))
                self.boxes.append(widget.text())

    def _close_boxes(self):
        """開いたままの QMessageBox を、後のテストへ残さない。"""
        from PyQt6.QtWidgets import QApplication, QMessageBox
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QMessageBox) and widget.isVisible():
                widget.close()

    def _windows(self):
        """出た窓（呼び出しと、表に出た箱）"""
        self._scan()
        return self.calls + [("visible", text) for text in self.boxes]

    @staticmethod
    def _stop_left_recordings(w):
        """途中で落ちたテストの記録を止める（見回りと記録中ダイアログを後のテストへ残さない）。"""
        for name in list(w._log_files):
            w._stop_log_recording_for(name)

    def _widget(self, names):
        from core import log_recording
        from ui.terminal_widget import TerminalWidget
        w = TerminalWidget()
        self.addCleanup(w.close)
        self.addCleanup(self._stop_left_recordings, w)
        w.PENDING_HIGH_WATER = self.HIGH
        w.PENDING_LOW_WATER = self.LOW
        w.resize(900, 600)
        w.show()
        for name in names:
            self.addCleanup(log_recording.stop, name)
            w.create_terminal_tab(name)
            w._terminals[name].set_input_enabled(True)
        return w

    def _stalled(self):
        release = threading.Event()
        self.releases.append(release)
        return release

    def _start(self, w, name, filename, release=None):
        """記録を始める。release を渡せば詰まる記録先、無ければ遅いが応答する記録先。"""
        path = os.path.join(self.dir, filename)
        real_open = builtins.open

        def fake_open(file, mode="r", *args, **kwargs):
            f = real_open(file, mode, *args, **kwargs)
            if isinstance(file, str) and file == path and "w" in mode:
                if release is None:
                    return _SlowFile(f, self.SLOW)
                return _StallingFile(f, release, 30.0)
            return f

        w.tab_widget.setCurrentWidget(w._terminals[name])
        with mock.patch("builtins.open", fake_open), \
             mock.patch("PyQt6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(path, "")):
            w.start_log_recording()
        self.assertIn(name, w._log_files, "前提: 記録が始まっている")
        self.assertIn(name, w._log_dialogs, "前提: 記録中ダイアログが出ている")
        return path

    @staticmethod
    def _wait_until(predicate, seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end and not predicate():
            _pump(20)
        return predicate()

    @staticmethod
    def _line(w, name):
        """その機器の記録中ダイアログの状態の行（表に出ていなければ ""。行が無ければ None）"""
        dialog = w._log_dialogs.get(name)
        label = getattr(dialog, "status_label", None)
        if label is None:
            return None
        return label.text() if label.isVisibleTo(dialog) else ""

    def _feed_until_held(self, w, gate, name, first):
        """受信スレッドの代わり: 関所が開いている間だけ受信を渡し、閉じたら止める。"""
        fed = first
        end = time.perf_counter() + 4.0
        while gate.is_set() and time.perf_counter() < end:
            w.queue_output(name, _chunk(name, fed, self.LINES))
            fed += self.LINES
            _pump(20)
        self.assertFalse(gate.is_set(), "前提: 記録待ちが上限を超えて受信を止めた")
        self.assertIn(name, w._log_throttled, "前提: この機器の描画を止めている")
        return fed

    def _feed_serial_over(self, w, name, first):
        """関所の無い接続の受信スレッドの代わり: 書き込み待ちが上限を超えるまで渡す。"""
        fed = first
        end = time.perf_counter() + 4.0
        while (w._log_backlog(name) < self.HIGH
               and time.perf_counter() < end):
            w.queue_output(name, _chunk(name, fed, self.LINES))
            fed += self.LINES
            _pump(20)
        self.assertGreaterEqual(w._log_backlog(name), self.HIGH,
                                "前提: 書き込み待ちが上限を超えた")
        return fed

    def _caught_up(self, w, gate, name):
        return (gate.is_set() and name not in w._log_throttled
                and not w._pending_output.get(name)
                and w._log_backlog(name) == 0)

    def _stop_and_read(self, w, name, path):
        """記録を止め、書き終えて閉じるのを待って中身を返す。"""
        from core import log_recording
        w.stop_log_recording(name)
        self.assertTrue(self._wait_until(
            lambda: not w._pending_output.get(name)
            and log_recording.device_using(path) is None, 20.0),
            "記録を停止しても、描き切って書き終えなかった")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _assert_held_line(self, line):
        self.assertIsNotNone(line, "記録中ダイアログに状態の行が無い")
        self.assertIn("記録先への書き込みが遅れている", line)
        self.assertIn("受信を止めて", line)
        self.assertIn("欠けは出ません", line)

    def test_a_held_device_shows_the_line_and_opens_no_window(self):
        """関所のある接続の受信を止めている間は、記録中ダイアログに状態の行を出し、窓は出さないこと。再開したら行を消すこと。"""
        from PyQt6.QtWidgets import QApplication
        w = self._widget(["dev"])
        release = self._stalled()
        path = self._start(w, "dev", "dev.log", release)
        gate = w.output_gate("dev")
        w.activateWindow()
        w._terminals["dev"].setFocus()
        _pump(50)
        fed = self._feed_until_held(w, gate, "dev", 0)

        self._wait_until(lambda: self._line(w, "dev") or self._windows(), 2.0)
        self.assertEqual(self._windows(), [],
                         "記録先の詰まりを知らせるのに窓を出した（出た瞬間にキーを奪う）")
        self._assert_held_line(self._line(w, "dev"))
        self.assertIs(QApplication.activeWindow(), w,
                      "状態の行を出すときに、主窓から活性を移した")
        self.assertIs(QApplication.focusWidget(), w._terminals["dev"],
                      "状態の行を出すときに、端末からフォーカスを移した")

        # 止めている間は出たまま。窓は出さない
        _pump(500)
        self.assertIn("dev", w._log_throttled, "前提: まだ止めている")
        self._assert_held_line(self._line(w, "dev"))

        release.set()
        self.assertTrue(self._wait_until(lambda: self._caught_up(w, gate, "dev"), 10.0),
                        "詰まりが解けても受信・描画を再開しなかった")
        self.assertTrue(self._wait_until(lambda: self._line(w, "dev") == "", 2.0),
                        "再開したのに状態の行を消さなかった: %r" % self._line(w, "dev"))
        text = self._stop_and_read(w, "dev", path)
        self.assertEqual(text, _chunk("dev", 0, fed).replace("\r\n", "\n"),
                         "止めている間の記録が欠けた・崩れた")
        self.assertIn("dev%06d" % (fed - 1), w._terminals["dev"].toPlainText())
        self.assertEqual(self._windows(), [], "窓を出した")

    def _type_through_a_hold(self, typed_on):
        """typed_on のタブへ打っている最中に dev の受信を止めても、残りのキーがそのタブへ届くこと。"""
        from PyQt6.QtWidgets import QApplication
        w = self._widget(["dev", "other"])
        release = self._stalled()
        path = self._start(w, "dev", "dev.log", release)
        gate = w.output_gate("dev")
        w.output_gate("other")
        target = w._terminals[typed_on]
        sent = []
        target.key_pressed.connect(sent.append)
        w.tab_widget.setCurrentWidget(target)
        w.activateWindow()
        target.setFocus()
        _pump(50)
        self.assertIs(QApplication.focusWidget(), target, "前提: 打つタブにフォーカスがある")

        _type(FIRST)
        fed = self._feed_until_held(w, gate, "dev", 0)
        # 直す前の作りでは、ここで案内が出る（見回りが次の刻みで出す）
        self._wait_until(lambda: self._line(w, "dev") or self._windows(), 2.0)
        _pump(100)
        went = _type(REST)
        _pump(100)

        self.assertEqual("".join(sent), FIRST + REST,
                         "受信を止めている最中に打ったキーが、打っていたタブへ届かなかった"
                         "（残りのキーの行き先: %r）" % went)
        self.assertIs(QApplication.focusWidget(), target, "端末からフォーカスを移した")
        self.assertIs(QApplication.activeWindow(), w, "主窓から活性を移した")
        self.assertEqual(self._windows(), [], "記録先の詰まりを知らせるのに窓を出した")
        self._assert_held_line(self._line(w, "dev"))

        release.set()
        self.assertTrue(self._wait_until(lambda: self._caught_up(w, gate, "dev"), 10.0),
                        "詰まりが解けても受信・描画を再開しなかった")
        text = self._stop_and_read(w, "dev", path)
        self.assertEqual(text, _chunk("dev", 0, fed).replace("\r\n", "\n"),
                         "止めている間の記録が欠けた・崩れた")

    def test_keys_typed_on_the_held_tab_reach_it(self):
        """止めた機器のタブへ打っている最中に止めても、残りのキー（Enter を含む）がそのタブへ届くこと。"""
        self._type_through_a_hold("dev")

    def test_keys_typed_on_another_tab_reach_it(self):
        """ほかのタブへ打っている最中に dev を止めても、残りのキー（Enter を含む）がそのタブへ届くこと。"""
        self._type_through_a_hold("other")

    def test_every_hold_shows_the_line_again_and_opens_no_window(self):
        """同じ記録で 2 回目に止まっても行を出し、再開のたびに消すこと。記録を始め直しても同じ。窓は出さないこと。"""
        w = self._widget(["dev"])
        release = self._stalled()
        path = self._start(w, "dev", "dev1.log", release)
        gate = w.output_gate("dev")
        fed = 0
        for round_ in range(2):
            fed = self._feed_until_held(w, gate, "dev", fed)
            self.assertTrue(self._wait_until(lambda: self._line(w, "dev"), 2.0),
                            "%d 回目に止めたのに、状態の行を出さなかった" % (round_ + 1))
            self._assert_held_line(self._line(w, "dev"))
            release.set()
            self.assertTrue(self._wait_until(
                lambda: self._caught_up(w, gate, "dev"), 10.0),
                "詰まりが解けても受信・描画を再開しなかった")
            self.assertTrue(self._wait_until(lambda: self._line(w, "dev") == "", 2.0),
                            "%d 回目に再開したのに、状態の行を消さなかった" % (round_ + 1))
            release.clear()             # 記録先がまた応答しなくなる
        self.assertEqual(self._windows(), [], "窓を出した")
        release.set()
        text1 = self._stop_and_read(w, "dev", path)
        self.assertEqual(text1, _chunk("dev", 0, fed).replace("\r\n", "\n"),
                         "1 回目の記録が欠けた・崩れた")

        # 記録を始め直しても、止めたら新しい記録中ダイアログに行を出す
        release2 = self._stalled()
        path2 = self._start(w, "dev", "dev2.log", release2)
        first2 = fed
        fed = self._feed_until_held(w, gate, "dev", fed)
        self.assertTrue(self._wait_until(lambda: self._line(w, "dev"), 2.0),
                        "始め直した記録で止めたのに、状態の行を出さなかった")
        self._assert_held_line(self._line(w, "dev"))
        release2.set()
        self.assertTrue(self._wait_until(lambda: self._caught_up(w, gate, "dev"), 10.0))
        self.assertTrue(self._wait_until(lambda: self._line(w, "dev") == "", 2.0))
        text2 = self._stop_and_read(w, "dev", path2)
        self.assertEqual(text2, _chunk("dev", first2, fed - first2).replace("\r\n", "\n"),
                         "2 回目の記録が欠けた・崩れた")
        self.assertEqual(self._windows(), [], "窓を出した")

    def test_a_slow_store_holding_again_and_again_opens_no_window(self):
        """遅いが応答する記録先で止めては再開するのを繰り返しても、窓は 1 つも出さず、追いついたら行を消すこと。"""
        w = self._widget(["dev"])
        path = self._start(w, "dev", "dev.log")
        gate = w.output_gate("dev")
        fed = 0
        holds = 0
        shown = False
        was_held = False
        end = time.perf_counter() + 20.0
        while holds < 3 and time.perf_counter() < end:
            if gate.is_set():
                w.queue_output("dev", _chunk("dev", fed, self.LINES))
                fed += self.LINES
            _pump(10)
            held = "dev" in w._log_throttled
            if held and not was_held:
                holds += 1
            was_held = held
            shown = shown or bool(self._line(w, "dev"))
        self.assertGreaterEqual(holds, 3, "前提: 止めては再開するのを繰り返した")
        _pump(300)
        self.assertEqual(self._windows(), [],
                         "止めては再開するのを繰り返す間に窓を出した（%d 回止めた）" % holds)
        self.assertTrue(shown, "止めている間に状態の行を一度も出さなかった")
        self.assertTrue(self._wait_until(lambda: self._caught_up(w, gate, "dev"), 20.0),
                        "受信・描画が追いつかなかった")
        self.assertTrue(self._wait_until(lambda: self._line(w, "dev") == "", 2.0),
                        "追いついたのに状態の行を消さなかった")
        text = self._stop_and_read(w, "dev", path)
        self.assertEqual(text, _chunk("dev", 0, fed).replace("\r\n", "\n"),
                         "記録が欠けた・崩れた")
        self.assertIn("dev%06d" % (fed - 1), w._terminals["dev"].toPlainText())
        self.assertEqual(self._windows(), [], "窓を出した")

    def test_a_serial_link_over_the_limit_shows_the_line_and_opens_no_window(self):
        """関所の無い接続（シリアル）で書き込み待ちが上限を超えても窓を出さず、状態の行を出し、キーは端末へ届くこと。追いついたら行を消すこと。"""
        from PyQt6.QtWidgets import QApplication
        w = self._widget(["ser"])
        release = self._stalled()
        path = self._start(w, "ser", "ser.log", release)
        self.assertNotIn("ser", w._output_gates, "前提: この接続には関所が無い")
        ser = w._terminals["ser"]
        sent = []
        ser.key_pressed.connect(sent.append)
        w.activateWindow()
        ser.setFocus()
        _pump(50)

        _type(FIRST)
        fed = self._feed_serial_over(w, "ser", 0)
        self._wait_until(lambda: self._line(w, "ser") or self._windows(), 2.0)
        _pump(100)
        went = _type(REST)
        _pump(100)
        self.assertEqual(self._windows(), [],
                         "書き込み待ちが上限を超えたのを知らせるのに窓を出した")
        line = self._line(w, "ser")
        self.assertIsNotNone(line, "記録中ダイアログに状態の行が無い")
        self.assertIn("記録先への書き込みが遅れています", line)
        self.assertIn("メモリに溜めて", line)
        self.assertEqual("".join(sent), FIRST + REST,
                         "上限を超えた最中に打ったキーが、端末へ届かなかった"
                         "（残りのキーの行き先: %r）" % went)
        self.assertIs(QApplication.focusWidget(), ser, "端末からフォーカスを移した")
        self.assertIs(QApplication.activeWindow(), w, "主窓から活性を移した")
        # 画面は進めたまま（関所の無い接続は描画を止めない）
        self.assertNotIn("ser", w._log_throttled)
        self.assertTrue(self._wait_until(
            lambda: "ser%06d" % (fed - 1) in ser.toPlainText(), 5.0),
            "関所の無い接続の描画を止めた")

        release.set()
        self.assertTrue(self._wait_until(
            lambda: not w._pending_output.get("ser") and w._log_backlog("ser") == 0,
            10.0), "詰まりが解けても書き込み待ちが書き終わらない")
        self.assertTrue(self._wait_until(lambda: self._line(w, "ser") == "", 2.0),
                        "追いついたのに状態の行を消さなかった: %r" % self._line(w, "ser"))
        text = self._stop_and_read(w, "ser", path)
        self.assertEqual(text, _chunk("ser", 0, fed).replace("\r\n", "\n"),
                         "詰まっている間の記録が欠けた・崩れた")
        self.assertEqual(self._windows(), [], "窓を出した")

    def test_a_stopped_recording_still_stalled_opens_no_window(self):
        """記録を停止したあと、停止した記録が詰まったままでも、窓は出さず（ダイアログも無い）、止めは解けること。"""
        from core import log_recording
        w = self._widget(["dev", "ser"])
        dev_release = self._stalled()
        ser_release = self._stalled()
        dev_path = self._start(w, "dev", "dev.log", dev_release)
        ser_path = self._start(w, "ser", "ser.log", ser_release)
        gate = w.output_gate("dev")
        dev_fed = self._feed_until_held(w, gate, "dev", 0)
        ser_fed = self._feed_serial_over(w, "ser", 0)
        self.assertTrue(self._wait_until(
            lambda: self._line(w, "dev") and self._line(w, "ser"), 2.0),
            "前提: どちらの記録中ダイアログにも状態の行が出た")

        w.stop_log_recording("dev")
        w.stop_log_recording("ser")
        self.assertNotIn("dev", w._log_dialogs)
        self.assertNotIn("ser", w._log_dialogs)
        # 停止した記録は詰まったまま。止めは解け、窓は出ない
        self.assertTrue(self._wait_until(
            lambda: gate.is_set() and "dev" not in w._log_throttled, 3.0),
            "記録を停止しても止めが解けなかった")
        _pump(500)
        self.assertFalse(dev_release.is_set() or ser_release.is_set(),
                         "前提: 停止した記録は詰まったまま")
        self.assertEqual(self._windows(), [], "停止した記録の詰まりに窓を出した")

        dev_release.set()
        ser_release.set()
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(dev_path) is None
            and log_recording.device_using(ser_path) is None, 10.0))
        with open(dev_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), _chunk("dev", 0, dev_fed).replace("\r\n", "\n"))
        with open(ser_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), _chunk("ser", 0, ser_fed).replace("\r\n", "\n"))
        self.assertEqual(self._windows(), [], "窓を出した")

    def _assert_line_fits(self, w, name, before):
        """その機器の状態の行が、折り返した 2 行目から先まで切れずに見えていること。

        before は行を出す前のダイアログの位置と大きさ。位置と幅は変えないこと。
        """
        dialog = w._log_dialogs[name]
        label = dialog.status_label
        need = label.heightForWidth(label.width())
        self.assertGreater(need, label.fontMetrics().lineSpacing() * 1.5,
                           "前提: %s の状態の行が折り返している" % name)
        self.assertGreaterEqual(
            label.height(), need,
            "%s の状態の行が切れている（行の高さ %d、その幅で要る高さ %d）"
            % (name, label.height(), need))
        self.assertTrue(dialog.rect().contains(label.geometry()),
                        "%s の状態の行がダイアログからはみ出している" % name)
        after = dialog.geometry()
        self.assertEqual((after.x(), after.y(), after.width()),
                         (before.x(), before.y(), before.width()),
                         "%s の状態の行を出すときに、ダイアログを動かした・幅を変えた"
                         % name)

    def test_the_line_fits_in_the_dialog(self):
        """止めている行とシリアルの遅れの行が、記録中ダイアログの中で切れずに全部見えること。記録と画面は欠けないこと。"""
        w = self._widget(["dev", "ser"])
        dev_release = self._stalled()
        ser_release = self._stalled()
        dev_path = self._start(w, "dev", "dev.log", dev_release)
        ser_path = self._start(w, "ser", "ser.log", ser_release)
        gate = w.output_gate("dev")
        _pump(50)
        before = {name: w._log_dialogs[name].geometry() for name in ("dev", "ser")}

        dev_fed = self._feed_until_held(w, gate, "dev", 0)
        ser_fed = self._feed_serial_over(w, "ser", 0)
        self.assertTrue(self._wait_until(
            lambda: self._line(w, "dev") and self._line(w, "ser"), 2.0),
            "前提: どちらの記録中ダイアログにも状態の行が出た")
        self._assert_held_line(self._line(w, "dev"))
        self.assertIn("メモリに溜めて", self._line(w, "ser"))
        _pump(100)
        self._assert_line_fits(w, "dev", before["dev"])
        self._assert_line_fits(w, "ser", before["ser"])

        dev_release.set()
        ser_release.set()
        self.assertTrue(self._wait_until(
            lambda: self._caught_up(w, gate, "dev")
            and not w._pending_output.get("ser") and w._log_backlog("ser") == 0,
            10.0), "詰まりが解けても追いつかなかった")
        self.assertTrue(self._wait_until(
            lambda: self._line(w, "dev") == "" and self._line(w, "ser") == "", 2.0),
            "追いついたのに状態の行を消さなかった")
        self.assertEqual(self._stop_and_read(w, "dev", dev_path),
                         _chunk("dev", 0, dev_fed).replace("\r\n", "\n"),
                         "止めている間の記録が欠けた・崩れた")
        self.assertEqual(self._stop_and_read(w, "ser", ser_path),
                         _chunk("ser", 0, ser_fed).replace("\r\n", "\n"),
                         "遅れている間の記録が欠けた・崩れた")
        self.assertIn("dev%06d" % (dev_fed - 1), w._terminals["dev"].toPlainText())
        self.assertIn("ser%06d" % (ser_fed - 1), w._terminals["ser"].toPlainText())
        self.assertEqual(self._windows(), [], "窓を出した")

    def _count_dialog_raises(self):
        """記録中ダイアログを前に出す・活性化する・見せ直す呼び出しを数える（本物は呼ばない）。"""
        from ui.dialogs.log_recording_dialog import LogRecordingDialog
        calls = []
        for method in ("raise_", "activateWindow", "show", "showNormal",
                       "setFocus", "open", "exec"):
            def counting(dialog, *args, _method=method):
                calls.append((_method, dialog.device_name))
            mock.patch.object(LogRecordingDialog, method, counting).start()
        return calls

    def test_updating_the_line_neither_raises_nor_activates_the_dialog(self):
        """状態の行を出す・書き換える・消すときに、記録中ダイアログを前に出さず、活性化もしないこと（止めている行とシリアルの行）。"""
        from PyQt6.QtWidgets import QApplication
        w = self._widget(["dev", "ser"])
        dev_release = self._stalled()
        ser_release = self._stalled()
        dev_path = self._start(w, "dev", "dev.log", dev_release)
        ser_path = self._start(w, "ser", "ser.log", ser_release)
        gate = w.output_gate("dev")
        w.tab_widget.setCurrentWidget(w._terminals["dev"])
        w.activateWindow()
        w._terminals["dev"].setFocus()
        _pump(50)
        self.assertIs(QApplication.focusWidget(), w._terminals["dev"],
                      "前提: 端末にフォーカスがある")
        # 記録を始めたとき（show）は数えない。数えるのは見回りが行を書き換える間
        raised = self._count_dialog_raises()

        dev_fed = self._feed_until_held(w, gate, "dev", 0)
        ser_fed = self._feed_serial_over(w, "ser", 0)
        self.assertTrue(self._wait_until(
            lambda: self._line(w, "dev") and self._line(w, "ser"), 2.0),
            "前提: どちらの記録中ダイアログにも状態の行が出た")
        _pump(300)
        self.assertEqual(raised, [], "状態の行を出すときに、記録中ダイアログを前に出した")
        self.assertIs(QApplication.activeWindow(), w, "主窓から活性を移した")
        self.assertIs(QApplication.focusWidget(), w._terminals["dev"],
                      "端末からフォーカスを移した")

        dev_release.set()
        ser_release.set()
        self.assertTrue(self._wait_until(
            lambda: self._caught_up(w, gate, "dev")
            and not w._pending_output.get("ser") and w._log_backlog("ser") == 0,
            10.0), "詰まりが解けても追いつかなかった")
        self.assertTrue(self._wait_until(
            lambda: self._line(w, "dev") == "" and self._line(w, "ser") == "", 2.0),
            "追いついたのに状態の行を消さなかった")
        self.assertEqual(raised, [], "状態の行を消すときに、記録中ダイアログを前に出した")
        self.assertEqual(self._stop_and_read(w, "dev", dev_path),
                         _chunk("dev", 0, dev_fed).replace("\r\n", "\n"),
                         "止めている間の記録が欠けた・崩れた")
        self.assertEqual(self._stop_and_read(w, "ser", ser_path),
                         _chunk("ser", 0, ser_fed).replace("\r\n", "\n"),
                         "遅れている間の記録が欠けた・崩れた")
        self.assertEqual(self._windows(), [], "窓を出した")

    def test_a_stopped_serial_recording_left_over_the_limit_opens_no_window(self):
        """描き待ちを残したまま停止したシリアルの記録が、上限を超えた書き込み待ちを持ったまま詰まっていても、窓は出さないこと。記録は欠けないこと。"""
        from core import log_recording
        w = self._widget(["ser"])
        release = self._stalled()
        path = self._start(w, "ser", "ser.log", release)
        fed = self._feed_serial_over(w, "ser", 0)
        self.assertTrue(self._wait_until(lambda: self._line(w, "ser"), 2.0),
                        "前提: 記録中ダイアログに状態の行が出た")

        # 受信が描画を上回っている最中に停止する（描き待ちを残す）
        w.queue_output("ser", _chunk("ser", fed, self.LINES))
        fed += self.LINES
        w.stop_log_recording("ser")
        self.assertNotIn("ser", w._log_dialogs, "前提: 記録中ダイアログは無い")
        self.assertIn("ser", w._closing_logs,
                      "前提: 停止した記録が、描き待ちの分を書くまで残っている")
        self.assertGreaterEqual(w._log_backlog("ser"), self.HIGH,
                                "前提: 停止した記録の書き込み待ちが上限を超えている")
        # この状態で見回りを回す（本来は 100ms ごと。描き終えると閉じ終わりの
        # 待ちへ移って数えなくなるので、ここで確かめる）
        w._check_log_writers()
        self.assertEqual(self._windows(), [],
                         "停止した記録の書き込み待ちの遅れに窓を出した")
        _pump(500)
        self.assertFalse(release.is_set(), "前提: 停止した記録は詰まったまま")
        self.assertEqual(self._windows(), [],
                         "停止した記録の書き込み待ちの遅れに窓を出した")

        release.set()
        self.assertTrue(self._wait_until(
            lambda: log_recording.device_using(path) is None, 10.0),
            "詰まりが解けても、停止した記録を閉じ終えなかった")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), _chunk("ser", 0, fed).replace("\r\n", "\n"),
                             "停止した記録が欠けた・崩れた")
        self.assertIn("ser%06d" % (fed - 1), w._terminals["ser"].toPlainText())
        self.assertEqual(self._windows(), [], "窓を出した")


if __name__ == "__main__":
    unittest.main()
