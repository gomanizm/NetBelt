"""起動時の未適用更新を当てたら、アプリが実際に終了することを確認する。

_check_pending_updates は MainWindow のコンストラクタから走る。そこで
QApplication.quit() を呼んでも、イベントループはまだ始まっていないので
何も起きない（Qt の quit()/exit() は走っているループにしか効かない）。
その結果 main.py の app.exec() はそのまま始まり、アプリは exe を掴んだまま
動き続け、3秒後に走る updater.bat は共有違反で上書きに失敗する。ZIP は
残るので、起動のたびに同じ失敗を繰り返す。

実測（inspector, release #6）: `Popen called during MainWindow(): True` の
あと `exec() returned rc=42 after 2.00s`（安全弁のタイマで抜けた）、
`window visible after exec: True`。

ここでは exec() をループ開始前の呼び出しと同じ順序で回し、「安全弁を
使わずに戻ってくる」ことで判定する。
"""
import hashlib
import os
import sys
import tempfile
import types
import unittest
import unittest.mock

sys.path.insert(0, "src")

SAFETY_RC = 42


def staged_update():
    """検証済みの更新ファイルを作り、そのパスを返す。

    _apply_pending_update は updater を起動する前に、ZIP の存在と
    ダウンロード時の検証記録を確かめ直す（起動時の適用経路も更新
    ダイアログと同じ確認を通すようにしたため）。ここで見たいのは
    終了の経路なので、その確認を通るファイルを用意する。
    """
    d = tempfile.mkdtemp(prefix="netbelt-quit-")
    zip_path = os.path.join(d, "NetBelt-9.9.9.zip")
    body = b"PK\x03\x04 dummy netbelt update"
    with open(zip_path, "wb") as f:
        f.write(body)
    with open(zip_path + ".sha256", "w", encoding="ascii") as f:
        f.write(hashlib.sha256(body).hexdigest())
    return zip_path


class PendingUpdateQuitTest(unittest.TestCase):
    def test_quit_takes_effect_once_the_event_loop_starts(self):
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication
        from ui.main_window import MainWindow

        app = QApplication.instance()
        popen = unittest.mock.MagicMock()

        # コンストラクタ内（＝ exec() の前）と同じ順序で呼ぶ
        with unittest.mock.patch("subprocess.Popen", popen), \
                unittest.mock.patch.object(sys, "frozen", True, create=True):
            MainWindow._apply_pending_update(types.SimpleNamespace(),
                                             staged_update())

        popen.assert_called_once()

        # 終了しなかったときに止まらないための安全弁
        safety = QTimer()
        safety.setSingleShot(True)
        safety.timeout.connect(lambda: app.exit(SAFETY_RC))
        safety.start(3000)
        rc = app.exec()
        safety.stop()

        self.assertEqual(rc, 0,
                         "updater を起動したのにアプリが終了しない"
                         "（exe を掴んだままなので上書きに失敗する）")


if __name__ == "__main__":
    unittest.main()
