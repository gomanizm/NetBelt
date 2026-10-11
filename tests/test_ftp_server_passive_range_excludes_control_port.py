"""FTP の制御ポートがパッシブの範囲に入っているとき、PASV がその番号を使わないこと。
範囲から制御ポートを除くと番号が残らない設定は、起動せずに理由を出すこと。

何が起きていたか（実測、43b2980。127.0.0.1 のみ）: PASV の待ち受けは制御接続の
自分側のアドレス（127.0.0.1 など）へ bind する。Windows では、0.0.0.0 で待ち受け中の
制御ポートと同じ番号にもその bind が通るので、制御ポート X がパッシブの範囲に
入っていると、PASV が X で待ち受けることがあった（範囲 [X, X+1] で 20 回中 7 回・
12 回）。その間に 127.0.0.1:X へ来た別の制御接続は、挨拶の代わりにデータ接続として
受け付けられた。範囲が X だけなら PASV は毎回 X で待ち受けた。範囲の下限が上限より
大きい（番号が 0 個）設定でも起動でき、PASV のたびに制御接続ごと切れた。

どう直したか: 起動の bind の後で、パッシブの範囲から制御ポートを除く。除いた結果、
番号が残らない設定（範囲が制御ポートだけ・下限が上限より大きい）では、待ち受けを
閉じて起動せず、error_occurred（パネルのログとダイアログ）に理由を出す。
"""
import ftplib
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")
sys.path.insert(0, "tests")

from test_ftp_server_concurrent_stor_refused import (  # noqa: E402
    PASSWORD, USER, _FtpServerCase)

# 制御ポートのほかに範囲へ入れる番号の数。どれかがほかに使われていても、
# PASV は残りの番号で待ち受けられる
SPARE_PORTS = 4


def _free_port(room=0):
    """OS に番号を出させて返す（すぐ閉じる）。番号 + room が 65535 を超えないもの"""
    for _ in range(1000):
        with socket.socket() as probe:
            probe.bind(("0.0.0.0", 0))
            port = probe.getsockname()[1]
        if port + room <= 65535:
            return port
    raise AssertionError("空いている番号を選べなかった")


def _bindable_like_pasv(port):
    """PASV と同じ形（127.0.0.1、オプションなし）で port へ bind できるか（すぐ閉じる）"""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _drain_qt(app):
    """配送待ちの Qt の知らせを処理する"""
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


class PassiveSkipsControlPortTest(_FtpServerCase):
    """制御ポート X を範囲 [X, X+4] の中に置いて起動する"""

    def setUp(self):
        # X+1〜X+4 は、PASV と同じ形（127.0.0.1、オプションなし）で bind できる
        # ものにする。動的ポートの範囲には、bind を断るほかのプロセスの待ち受けが
        # 並んでいることがある（手元の Windows 11 では 49664〜49668 が WinError 10013）
        for _ in range(50):
            self._x = _free_port(room=SPARE_PORTS)
            if all(_bindable_like_pasv(self._x + k)
                   for k in range(1, SPARE_PORTS + 1)):
                break
        else:
            self.skipTest("PASV が bind できる番号の並びを選べなかった")
        self.passive_ports = (self._x, self._x + SPARE_PORTS)
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        # 後始末は後から積んだ順に走るので、これは土台の停止の後に走る
        self.addCleanup(_drain_qt, app)
        super().setUp()

    def _control_port(self):
        # 土台は範囲の外から選ぶ。ここでは範囲の中の X を使う
        return self._x

    def test_pasv_and_epsv_never_listen_on_the_control_port(self):
        """PASV / EPSV を繰り返しても制御ポートで待ち受けず、範囲のほかの番号を使うこと。
        PASV の待ち受けが開いている間も、新しい制御接続へ挨拶が返ること。

        制御の待ち受けを排他にした後（_ExclusiveFTPServer）は、範囲から除かなくても
        PASV の X への bind は断られるので、PASV / EPSV の番号だけでは除いたかどうかを
        見分けられない。そのため、起動後の PASV の候補に X が無いことを先に確かめる
        （除くことだけを外した変異で、ここで落ちることを確かめた）。排他そのものは
        test_ftp_server_exclusive_control_port.py が確かめる。"""
        self.assertEqual(self.m.port, self._x, "前提: 制御ポートが範囲の中にある")
        self.assertNotIn(self._x, self.m._server.handler.passive_ports,
                         "起動で制御ポートが PASV の候補から除かれていない")
        allowed = set(range(self._x + 1, self._x + SPARE_PORTS + 1))
        ftp = self.client()
        seen = set()
        # 制御ポートを除かず排他でもない形（43b2980）は 1 回ごとに 5 分の 1 で X を
        # 選ぶので、40 回でまず見つかる（見逃すのは 0.8 の 40 乗、約 0.01%）
        for i in range(40):
            if i % 4 == 3:
                port = ftplib.parse229(ftp.sendcmd("EPSV"), ("127.0.0.1", 0))[1]
            else:
                port = ftplib.parse227(ftp.sendcmd("PASV"))[1]
            seen.add(port)
        self.assertNotIn(self._x, seen, "PASV / EPSV が制御ポートで待ち受けた")
        self.assertLessEqual(seen, allowed, "PASV / EPSV が範囲の外の番号を使った")
        # 最後の PASV の待ち受けを開いたまま、別の制御接続を張る
        other = ftplib.FTP()
        self.addCleanup(self._close, other)
        welcome = other.connect("127.0.0.1", self.m.port, timeout=10)
        self.assertTrue(welcome.startswith("220"), welcome)
        self.assertTrue(ftp.sendcmd("NOOP").startswith("200"))


class NoPassivePortLeftTest(unittest.TestCase):
    """範囲から制御ポートを除くと番号が残らない設定では、起動しないこと"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        fw = mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)
        from core.ftp_server import FTPServerManager
        self.m = FTPServerManager()
        self.errors = []
        self.m.error_occurred.connect(self.errors.append)
        self.addCleanup(_drain_qt, self.app)
        self.addCleanup(self.m.error_occurred.disconnect)
        self.addCleanup(self._stop_if_running)
        self.root = tempfile.mkdtemp(prefix="netbelt-ftp-nopasv-")

    def _stop_if_running(self):
        if self.m.is_running:
            self.m.stop()

    def _start(self, port, passive_ports):
        return self.m.start(port=port, root_dir=self.root, username=USER,
                            password=PASSWORD, passive_ports=passive_ports)

    def _assert_port_released(self, port):
        """起動しなかった後、制御ポートの待ち受けが残っていないこと"""
        with socket.socket() as again:
            try:
                again.bind(("0.0.0.0", port))
            except OSError as e:
                self.fail("起動しなかったのに %d 番の待ち受けが残っている: %r"
                          % (port, e))

    def test_a_range_of_only_the_control_port_is_refused(self):
        """範囲が制御ポートだけなら起動せず、範囲と制御ポートを挙げて理由を出すこと。"""
        x = _free_port()
        self.assertFalse(self._start(x, (x, x)),
                         "範囲が制御ポートだけなのに起動した")
        self.assertFalse(self.m.is_running)
        self.assertEqual(len(self.errors), 1, self.errors)
        self.assertIn("passiveポート範囲 %d-%d" % (x, x), self.errors[0])
        self.assertIn("制御ポート %d を除くと、使える番号がありません" % x,
                      self.errors[0])
        self._assert_port_released(x)
        # 断った後も、正しい範囲なら起動できる（状態が残らない）
        self.assertTrue(self._start(x, (x, x + 1)), self.errors)
        self.assertTrue(self.m.is_running)

    def test_a_reversed_range_is_refused(self):
        """下限が上限より大きい（番号が 0 個の）範囲なら起動せず、その理由を出すこと。"""
        x = _free_port()
        self.assertFalse(self._start(x, (50150, 50100)),
                         "下限が上限より大きい範囲なのに起動した")
        self.assertFalse(self.m.is_running)
        self.assertEqual(len(self.errors), 1, self.errors)
        self.assertIn("passiveポート範囲 50150-50100", self.errors[0])
        self.assertIn("下限が上限より大きく", self.errors[0])
        self._assert_port_released(x)

    def test_one_port_left_besides_the_control_port_starts(self):
        """制御ポートを除いて 1 つでも残れば、これまでどおり起動すること。"""
        x = _free_port(room=1)
        self.assertTrue(self._start(x, (x, x + 1)), self.errors)
        self.assertEqual(self.errors, [])


class PanelShowsWhyItDidNotStartTest(unittest.TestCase):
    """パネルから起動したとき、番号が残らない設定の理由をログとダイアログに出すこと"""

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        fw = mock.patch("core.firewall.ensure_inbound_allow",
                        return_value=(True, "test stub"))
        fw.start()
        self.addCleanup(fw.stop)

    def _panel(self):
        from core.config_manager import ConfigManager
        from ui.ftp_server_panel import FTPServerPanel
        workdir = tempfile.mkdtemp(prefix="netbelt-ftp-nopasv-panel-")
        panel = FTPServerPanel(config_manager=ConfigManager(
            os.path.join(workdir, "config.json")))
        self.addCleanup(self._dispose, panel)
        panel.root_dir_edit.setText(os.path.join(workdir, "root"))
        panel.username_edit.setText(USER)
        panel.password_edit.setText(PASSWORD)
        return panel

    def _dispose(self, panel):
        from PyQt6.QtCore import QCoreApplication, QEvent
        if panel.ftp_server.is_running:
            panel.ftp_server.stop()
        panel.close()
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        _drain_qt(self.app)

    def test_the_panel_logs_and_shows_the_reason(self):
        for name in ("control_only", "reversed"):
            with self.subTest(name):
                # 場面ごとに別の番号にする（片方が起動してしまっても、もう片方の
                # 起動の失敗の理由が「使用中」にすり替わらない）
                x = _free_port()
                lo, hi, needle = ((x, x, "制御ポート %d を除くと" % x)
                                  if name == "control_only" else
                                  (50150, 50100, "下限が上限より大きく"))
                panel = self._panel()
                panel.port_spin.setValue(x)
                panel.passive_lo_spin.setValue(lo)
                panel.passive_hi_spin.setValue(hi)
                with mock.patch("ui.ftp_server_panel.QMessageBox.critical") as critical:
                    panel._on_start_server()
                self.app.processEvents()
                self.assertFalse(panel.ftp_server.is_running)
                self.assertIn("停止中", panel.status_label.text())
                self.assertFalse(panel.start_btn.isHidden(),
                                 "起動していないのに起動ボタンが隠れた")
                log = panel.log_text.toPlainText()
                self.assertIn("エラー: passiveポート範囲 %d-%d" % (lo, hi), log)
                self.assertIn(needle, log)
                critical.assert_called_once()
                self.assertIn(needle, critical.call_args[0][2])


if __name__ == "__main__":
    unittest.main()
