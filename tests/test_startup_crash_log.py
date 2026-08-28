"""未捕捉例外が痕跡を残すことを検証する。

exe は console=False でビルドしている。標準出力の行き先が無いので、
main.py の _setup_logging() が sys.stdout/sys.stderr をログファイルへ
差し替えている。しかしこれではスロット内の例外を拾えない。

PyQt6 は、シグナルから呼ばれたスロットの中で未捕捉例外が起きると
sys.excepthook を呼んだうえでプロセスを abort する。既定の
sys.__excepthook__ は C レベルの実ファイルディスクリプタ側へ書くため、
Python オブジェクトとして差し替えた sys.stderr は経由しない。
コンソールの無い exe ではその出力先が存在せず、結果として

  - ウィンドウが消えるだけで、ダイアログも何も出ない
  - _setup_logging() のログファイルは 0 バイトのまま
  - app.exec() は戻らないので、後片付けも走らない

という、痕跡がまったく残らない壊れ方になる。ソースから起動している
間はコンソールに traceback が出るので、exe でだけ起こる。

実測（PyQt6 6.10.1 / Qt 6.10.0、offscreen）:
  excepthook 無し … 異常終了、ログ 0 バイト
  excepthook 有り … exit 0、traceback 全文がログに残る

さらに、_setup_logging() は main() の中で呼ばれるのに MainWindow は
モジュール先頭で import している。import 中に落ちるとログの差し替えが
まだ済んでおらず、起動できない理由も残らない。
"""
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

sys.path.insert(0, "src")


def _a_traceback(message="BOOM"):
    """本物の traceback を持つ例外情報を作って返す。"""
    try:
        raise RuntimeError(message)
    except RuntimeError:
        return sys.exc_info()


class CrashLogTest(unittest.TestCase):
    def setUp(self):
        self._hook = sys.excepthook
        self.addCleanup(lambda: setattr(sys, "excepthook", self._hook))
        self._stderr = sys.stderr
        self.addCleanup(lambda: setattr(sys, "stderr", self._stderr))
        self.log_path = os.path.join(
            tempfile.mkdtemp(prefix="netbelt-crash-"), "app.log")

    def _capture(self):
        """本番と同じく sys.stderr をファイルへ向ける。"""
        f = io.open(self.log_path, "a", encoding="utf-8", buffering=1)
        self.addCleanup(f.close)
        sys.stderr = f
        return f

    def _logged(self):
        if not os.path.exists(self.log_path):
            return ""
        with io.open(self.log_path, encoding="utf-8") as f:
            return f.read()

    def test_an_unhandled_exception_is_written_where_the_log_goes(self):
        """例外の内容が、退避先の stderr へ残ること。"""
        import main
        self._capture()
        main.install_excepthook(self.log_path)

        with mock.patch.object(main.QMessageBox, "critical"):
            sys.excepthook(*_a_traceback("SLOT_BOOM"))

        logged = self._logged()
        self.assertIn("RuntimeError", logged)
        self.assertIn("SLOT_BOOM", logged)
        self.assertIn("Traceback", logged,
                      "traceback 本体が残っていない: %r" % logged)

    def test_the_user_is_told_that_it_happened_and_where_to_look(self):
        """黙って消えないこと。ログの場所まで伝えること。"""
        import main
        self._capture()
        main.install_excepthook(self.log_path)

        with mock.patch.object(main.QMessageBox, "critical") as critical:
            sys.excepthook(*_a_traceback("SLOT_BOOM"))

        self.assertTrue(critical.called, "利用者に何も知らせていない")
        shown = " ".join(str(a) for a in critical.call_args[0])
        self.assertIn(self.log_path, shown,
                      "ログの場所を伝えていない: %r" % shown)

    def test_the_log_is_written_even_if_the_dialog_cannot_be_shown(self):
        """知らせに失敗しても、記録だけは残すこと。

        QApplication がまだ無い、あるいは既に落ちている状況でも
        呼ばれ得る。ダイアログの失敗で記録まで道連れにしない。
        """
        import main
        self._capture()
        main.install_excepthook(self.log_path)

        with mock.patch.object(main.QMessageBox, "critical",
                               side_effect=RuntimeError("no QApplication")):
            sys.excepthook(*_a_traceback("SLOT_BOOM"))

        self.assertIn("SLOT_BOOM", self._logged(),
                      "ダイアログの失敗で記録まで消えている")

    def test_ctrl_c_is_left_to_the_default_handler(self):
        """Ctrl+C は障害ではないので、ダイアログを出さないこと。"""
        import main
        self._capture()
        main.install_excepthook(self.log_path)

        try:
            raise KeyboardInterrupt()
        except KeyboardInterrupt:
            info = sys.exc_info()

        with mock.patch.object(main.QMessageBox, "critical") as critical:
            sys.excepthook(*info)

        self.assertFalse(critical.called,
                         "Ctrl+C でエラーダイアログを出している")

    def test_the_window_is_not_imported_before_the_log_is_redirected(self):
        """import 中に落ちても理由が残るよう、UI の import を遅らせること。

        モジュール先頭で MainWindow を import すると、その import が
        失敗したときにはまだ stdout/stderr の差し替えが済んでいない。
        exe では「起動しない、ログも空」になり、原因が何も残らない。
        """
        import ast
        with io.open("src/main.py", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        top_level = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                top_level.append(node.module)
            elif isinstance(node, ast.Import):
                top_level.extend(a.name for a in node.names)

        offenders = [m for m in top_level if m.startswith(("ui", "core"))]
        self.assertEqual(offenders, [],
                         "ログの差し替えより先に import している: %s" % offenders)


class CrashLogEndToEndTest(unittest.TestCase):
    """実際に Qt を動かして、スロット例外が記録されることを確かめる。

    単体で excepthook を呼ぶだけでは、PyQt が本当にこの経路を通るのかを
    確かめたことにならない。Qt のイベントループを回して、そこで起きた
    例外がログに落ちるところまでを別プロセスで見る。
    """

    SCRIPT = textwrap.dedent('''
        import os, sys
        sys.path.insert(0, %(src)r)
        log = %(log)r
        f = open(log, "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import QTimer
        import main
        app = QApplication([])
        # offscreen ではモーダルを閉じる手が無く、出すと戻ってこない。
        # 呼ばれたことだけ印を残して差し替える
        def _stub(parent, title, text):
            f.write("dialog-shown\\n")
        main.QMessageBox.critical = _stub
        main.install_excepthook(log)
        def boom():
            raise RuntimeError("SLOT_BOOM_E2E")
        QTimer.singleShot(50, boom)
        QTimer.singleShot(1500, app.quit)
        code = app.exec()
        f.write("exec-returned=%%d\\n" %% code)
        f.flush()
    ''')

    def test_an_exception_inside_a_slot_reaches_the_log(self):
        work = tempfile.mkdtemp(prefix="netbelt-e2e-")
        log = os.path.join(work, "app.log")
        script = os.path.join(work, "run.py")
        src = os.path.abspath("src")
        with io.open(script, "w", encoding="utf-8") as f:
            f.write(self.SCRIPT % {"src": src, "log": log})

        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        proc = subprocess.run([sys.executable, script], env=env,
                              capture_output=True, timeout=120)

        logged = ""
        if os.path.exists(log):
            with io.open(log, encoding="utf-8") as f:
                logged = f.read()

        self.assertIn("SLOT_BOOM_E2E", logged,
                      "スロットの例外がログに残っていない (exit=%s stderr=%r)"
                      % (proc.returncode, proc.stderr[-400:]))
        self.assertIn("dialog-shown", logged,
                      "利用者へ知らせていない")
        self.assertIn("exec-returned=", logged,
                      "app.exec() が戻っていない＝プロセスが abort している")
        self.assertEqual(proc.returncode, 0,
                         "異常終了している: %s" % proc.returncode)


if __name__ == "__main__":
    unittest.main()
