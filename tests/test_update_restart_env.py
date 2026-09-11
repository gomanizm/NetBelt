"""更新後の再起動が、PyInstaller の _PYI_* を引き継がないことを確認する。

onefile の exe から updater.bat を起動すると、環境変数 _PYI_ARCHIVE_FILE
などがそのまま孫プロセス（起動し直した NetBelt.exe）まで届く。ブートローダ
は「自分と同じ exe が親」と見て、親が終了時に消した _MEIxxxx から
python DLL を読もうとし、Python が一度も起動しないまま落ちる。実測
（inspector, release #1）: 子の出力は
`[PYI-22792:ERROR] Failed to load Python DLL '...\\_MEI225882\\python312.dll'`、
`child_rc=-1`。PYINSTALLER_RESET_ENVIRONMENT=1 を立てた版は
`CHILD_STARTED_OK child_rc=0` で起動した。

起動経路は2つ（ダイアログの「更新を適用」と、起動時の未適用更新）あり、
片方だけ直すと再発するので、両方を見る。
"""
import os
import sys
import types
import unittest
import unittest.mock

sys.path.insert(0, "src")

VAR = "PYINSTALLER_RESET_ENVIRONMENT"


class UpdaterEnvTest(unittest.TestCase):
    def test_updater_env_keeps_the_rest_of_the_environment(self):
        """既存の環境変数を落とさずに、印だけを足すこと。"""
        from core.version_manager import updater_env

        with unittest.mock.patch.dict(os.environ, {"NETBELT_TEST_MARK": "1"}):
            env = updater_env()

        self.assertEqual(env.get(VAR), "1")
        self.assertEqual(env.get("NETBELT_TEST_MARK"), "1")
        for key in os.environ:
            self.assertIn(key, env, "環境変数が落ちている: %s" % key)


class _PopenSpy:
    """subprocess.Popen の差し替え。呼ばれた引数を覚えるだけ。"""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return unittest.mock.MagicMock()

    @property
    def env(self):
        return self.calls[0][1].get("env")


class RestartEnvironmentTest(unittest.TestCase):
    """2つの起動経路が、どちらも印を立てた環境で updater.bat を呼ぶこと。"""

    def setUp(self):
        # 存在確認を通すためだけに使うので、実在するパスなら何でもよい
        self.zip_path = os.path.abspath(__file__)

    def test_update_dialog_resets_the_pyinstaller_environment(self):
        from ui.dialogs.update_dialog import UpdateDialog

        dialog = UpdateDialog(None, {"version": "9.9.9"})
        self.addCleanup(dialog.deleteLater)
        dialog.downloaded_zip_path = self.zip_path
        spy = _PopenSpy()
        with unittest.mock.patch("subprocess.Popen", spy), \
                unittest.mock.patch.object(sys, "frozen", True, create=True), \
                unittest.mock.patch("os.path.exists", return_value=True):
            dialog._on_apply_clicked()

        self.assertEqual(len(spy.calls), 1, "updater.bat を起動していない")
        self.assertIsNotNone(spy.env, "環境を指定せずに起動している")
        self.assertEqual(spy.env.get(VAR), "1",
                         "再起動した exe が親の _MEI を見に行ってしまう")

    def test_pending_update_resets_the_pyinstaller_environment(self):
        from ui.main_window import MainWindow

        spy = _PopenSpy()
        with unittest.mock.patch("subprocess.Popen", spy), \
                unittest.mock.patch.object(sys, "frozen", True, create=True):
            MainWindow._apply_pending_update(types.SimpleNamespace(),
                                             self.zip_path)

        self.assertEqual(len(spy.calls), 1, "updater.bat を起動していない")
        self.assertIsNotNone(spy.env, "環境を指定せずに起動している")
        self.assertEqual(spy.env.get(VAR), "1",
                         "再起動した exe が親の _MEI を見に行ってしまう")


if __name__ == "__main__":
    unittest.main()
