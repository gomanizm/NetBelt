"""別スレッドからのシグナル中継が、受け手を解放したあとで暴発しないことを検証する。

SNMPManager は受信スレッドのシグナルを `connect(self.trap_received.emit)` の形で
中継していた。`self.trap_received` は参照のたびに作られるその場限りの
pyqtBoundSignal で、`.emit` はそれに束ねられた組み込みメソッドである。PyQt から
見ると受け手は pyqtBoundSignal であって QObject ではないため、SNMPManager が
破棄されてもこの接続は切られず、破棄時の removePostedEvents も（配送先が
PyQt のスロットプロキシなので）キューに積まれた呼び出しを消せない。

結果、受信スレッドが停止時に出したシグナルがキューに残ったまま SNMPManager が
循環 GC で回収されると、次に誰かが processEvents() を回した時点で解放済みの
C++ オブジェクトに触れ、プロセスごと落ちる。テストは順番に並べているだけで
イベントループを回さないため、爆発するのは毒を仕込んだテストではなく、
そのあとで最初にイベントを捌いたテストになる。全体テストが実行のたびに
違う場所（FTP の待受、SNMP の Trap 受信）で access violation になっていた正体。

中継はシグナル同士でつなぐ。そうすれば Qt が受け手の寿命を見て後始末する。

プロセスごと落ちる不具合なので、検証は子プロセスの終了コードで行う。
"""
import os
import subprocess
import sys
import textwrap
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = textwrap.dedent(
    """
    import gc, os, socket, sys
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, os.path.join(sys.argv[1], "src"))
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from unittest import mock
    from core.snmp_manager import SNMPManager

    def free_udp_port():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    patcher = mock.patch("core.firewall.ensure_inbound_allow",
                         return_value=(True, "test stub"))
    patcher.start()

    for _ in range(8):
        m = SNMPManager()
        m.start_trap_receiver(port=free_udp_port(), communities=["public"])
        m.stop_trap_receiver()
        del m
        # 参照を落としただけでは死なない（循環参照がある）。全体テストでは
        # 任意の時点で走る循環 GC を、ここでは自分で起こして待ち合わせる
        for _ in range(5):
            gc.collect()
            app.processEvents()
            app.sendPostedEvents(None, 0)
    print("OK")
    """
)


class SignalRelayOutlivesReceiverTest(unittest.TestCase):
    def test_a_collected_manager_does_not_take_the_process_down(self):
        proc = subprocess.run(
            [sys.executable, "-c", SCRIPT, REPO_ROOT],
            capture_output=True, text=True, timeout=180,
            cwd=REPO_ROOT)

        self.assertEqual(
            proc.returncode, 0,
            "受け手を解放したあとの配送でプロセスが落ちた "
            "(exit=%s)\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (proc.returncode, proc.stdout[-2000:], proc.stderr[-4000:]))
        self.assertIn("OK", proc.stdout)

    def test_no_signal_is_relayed_through_a_bound_emit(self):
        """中継を `.emit` でつなぎ直す再発を防ぐ。

        シグナル同士でつなげば Qt が寿命を見てくれるが、`.emit` を渡すと
        受け手が QObject だと認識されない。src/ 全体で禁じる。
        """
        import re
        pattern = re.compile(r"\.connect\(\s*[A-Za-z_][A-Za-z0-9_.]*\.emit\s*\)")
        offenders = []
        for dirpath, _dirnames, filenames in os.walk(os.path.join(REPO_ROOT, "src")):
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                with open(path, encoding="utf-8") as f:
                    for lineno, line in enumerate(f, 1):
                        if pattern.search(line):
                            offenders.append("%s:%d: %s"
                                             % (os.path.relpath(path, REPO_ROOT),
                                                lineno, line.strip()))
        self.assertEqual(
            offenders, [],
            "シグナルの中継に .emit を渡している箇所がある。"
            "受け手の寿命を Qt が追えなくなるので、シグナル同士でつなぐこと:\n"
            + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
