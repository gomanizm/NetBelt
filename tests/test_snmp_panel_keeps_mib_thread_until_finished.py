"""作ったばかりの SNMP パネルを捨てても、プロセスが落ちないことを検証する。

SNMPPanel は生成時に MIBLoaderThread（親を持たない QThread）を起動する。
start() から作業スレッドが run() に入るまでの間、スレッドを指す参照が
パネルの mib_thread だけだと、パネルを捨てた時点で sip が実行中の QThread
を delete し、Qt が qFatal（"QThread: Destroyed while thread '' is still
running"）でプロセスごと落とす（終了コード 0xC0000409）。全件の実行では、
パネルを作って数 ms で捨てるテスト（test_log_recording_io_stall_in_use_message.py
の 1 件目など）が、CPU が混んだときだけこれで落ち、まとめの行も traceback
も残らなかった。

落ちるとテストプロセスごと消えるので、別プロセスで確かめる。run() に入る
のが遅れた状態を毎回作るため、started に数十 ms 眠るスロットを
DirectConnection でつなぐ（スロットはスレッドへの参照を持たない）。
読み込みの中身（mibs/ の量やキャッシュ）に左右されないよう、run() が呼ぶ
get_resolver は何もしない関数に差し替える。
"""
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")


class DroppingAFreshSnmpPanelTest(unittest.TestCase):
    """パネルを作ってすぐ捨てても、子プロセスが最後まで走ること。"""

    # 続けて作って捨てる枚数（走り終えたスレッドを外す処理も通す）
    PANELS = 5

    SCRIPT = textwrap.dedent('''
        import gc, os, sys, time
        sys.path.insert(0, sys.argv[1])
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication
        app = QApplication([])
        from ui import snmp_panel as mod

        panels = int(sys.argv[2])
        entered = []

        def nap():
            # スレッドへの参照を持たない（引数も sender も見ない）
            time.sleep(0.05)

        def resolver(*args, **kwargs):
            entered.append(1)

        real_start = mod.MIBLoaderThread.start

        def start(self, *args):
            self.started.connect(nap, Qt.ConnectionType.DirectConnection)
            real_start(self, *args)

        mod.MIBLoaderThread.start = start
        mod.get_resolver = resolver
        for _ in range(panels):
            panel = mod.SNMPPanel()
            del panel
            gc.collect()
        # どのスレッドも run() に入り、走り終えてから終わる（終了時の破棄も通す）
        end = time.monotonic() + 60
        while len(entered) < panels and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        end = time.monotonic() + 1
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
        print("SURVIVED %d" % len(entered))
    ''')

    def test_dropping_a_panel_right_after_creating_it_does_not_abort(self):
        work = tempfile.mkdtemp(prefix="netbelt-mibdrop-")
        script = os.path.join(work, "run.py")
        with io.open(script, "w", encoding="utf-8") as f:
            f.write(self.SCRIPT)
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        # 子の出力は下で UTF-8 として読むので、書く側もそろえる。固定しないと
        # 英語版 Windows（GitHub の CI は cp1252）では、製品の日本語の print が
        # UnicodeEncodeError になり、パネルを捨てるところまで行かない
        env["PYTHONIOENCODING"] = "utf-8"

        # cwd は一時フォルダ（作業ディレクトリに何も作らない）
        proc = subprocess.run(
            [sys.executable, script, SRC, str(self.PANELS)],
            env=env, cwd=work, capture_output=True, timeout=180)

        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        self.assertEqual(
            proc.returncode, 0,
            "作ったばかりのパネルを捨てたら落ちた (exit=0x%08X。0xC0000409 なら"
            "実行中の QThread の破棄による qFatal)\n%s"
            % (proc.returncode & 0xFFFFFFFF, err[-600:]))
        self.assertIn("SURVIVED %d" % self.PANELS, out,
                      "どれかの読み込みスレッドが run() に入っていない\n%s"
                      % out[-600:])


if __name__ == "__main__":
    unittest.main()
