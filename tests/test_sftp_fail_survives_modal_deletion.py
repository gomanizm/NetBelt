"""エラー通知のモーダル中にマネージャが捨てられても、_fail が落ちないことを検証する。

何が起きていたか（基準 f4cad23 での実測。実 MainWindow + 実 SFTPPanel でも再現）:
  GUI スレッドからの SFTP 操作（create_directory / delete_item /
  rename_item / change_permissions / inspect_link_target / change_directory）
  が期限切れや切断で失敗すると、_fail が error_occurred を同期配送する。
  受け手の sftp_panel._on_error は QMessageBox.warning を開く＝入れ子の
  イベントループが回る。その最中に SSH の切断が届くと
  MainWindow._on_connection_closed → _drop_sftp_manager が disconnect の
  あと deleteLater でマネージャを捨て、入れ子のループを抜ける時点で C++
  側が破棄される。警告を閉じて戻ってきた _fail は続けて self.disconnect()
  を呼ぶので、その中の disconnected.emit が

    RuntimeError: wrapped C/C++ object of type SFTPManager has been deleted

  を投げた。src/main.py の install_excepthook が受けるのでアプリは落ちない
  が、SFTP エラーの警告を閉じた直後に「予期しないエラーが発生しました。
  RuntimeError: ...」がもう 1 枚出る。状態は壊れていない（_fail は各呼び
  出し元の最後の処理で、パネル側もその後に何もしていない）。

  ワーカースレッドから来る通知（upload / download / list_directory）は
  キュー配送なのでこの経路には入らない。再入するのは GUI スレッドから
  呼ぶ操作だけ。

どう直したか:
  _fail は通知のあとの disconnect を _disconnect_after_notice で包み、
  破棄済みの QObject に触ったときの RuntimeError だけを飲む。畳む仕事
  （is_connected を落とす・クライアントを閉じる）は disconnected.emit より
  前に終わっているので、飲んでも状態は壊れない。捨てられていない通常の
  経路では、これまでどおり disconnected が出る。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class SftpFailSurvivesModalDeletionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _manager(self):
        """機器が黙ったまま（normalize が期限切れ）のマネージャ"""
        from PyQt6.QtCore import QObject
        from core.sftp_manager import SFTPManager

        self.owner = QObject()          # MainWindow の代わりの親
        m = SFTPManager(self.owner)
        m.is_connected = True
        self.client = m.sftp_client = mock.Mock()
        self.client.normalize.side_effect = TimeoutError()
        m.list_directory = mock.Mock()
        self.errors, self.gone = [], []
        m.error_occurred.connect(self.errors.append)
        m.disconnected.connect(lambda: self.gone.append(True))
        return m

    def _open_modal_on_error(self, m, drop):
        """sftp_panel._on_error 相当。警告のあいだ入れ子のループを回す

        Args:
            drop: True なら、その最中に SSH の切断が届いたことにする
        """
        from PyQt6.QtCore import QEventLoop, QTimer

        def on_error(_message):
            loop = QEventLoop()
            if drop:
                QTimer.singleShot(0, lambda: self._drop_like_main_window(m))
            QTimer.singleShot(30, loop.quit)
            loop.exec()

        m.error_occurred.connect(on_error)

    @staticmethod
    def _drop_like_main_window(m):
        """MainWindow._drop_sftp_manager 相当（SSH 切断の通知で走る）"""
        try:
            m.disconnect()
        except Exception:
            pass
        m.deleteLater()

    def test_a_manager_dropped_during_the_warning_does_not_raise(self):
        """警告の裏で捨てられても、GUI スレッドのスロットへ例外を抜けさせないこと。"""
        from PyQt6 import sip

        m = self._manager()
        self._open_modal_on_error(m, drop=True)

        # 例外が抜ければ、ここで RuntimeError としてテストが落ちる
        m.change_directory("/flash/config")

        self.assertTrue(sip.isdeleted(m),
                        "入れ子のループ中に破棄されていない（再現の条件が崩れた）")
        self.assertEqual(len(self.errors), 1,
                         "失敗の通知が 1 回でない: %s" % self.errors)
        self.assertIn("SFTP接続を切断しました", self.errors[0],
                      "切断したことが伝わらない: %s" % self.errors)
        self.client.close.assert_called_once_with()

    def test_a_warning_that_only_spins_the_loop_still_disconnects(self):
        """捨てられていないときは、これまでどおり接続を畳んで知らせること。"""
        m = self._manager()
        self._open_modal_on_error(m, drop=False)

        m.change_directory("/flash/config")

        self.assertFalse(m.is_connected, "使えないチャンネルを掴んだまま")
        self.assertIsNone(m.sftp_client)
        self.assertEqual(len(self.gone), 1,
                         "disconnected が %d 回（1 回であること）" % len(self.gone))
        self.assertEqual(len(self.errors), 1, "通知が増えている: %s" % self.errors)


if __name__ == "__main__":
    unittest.main()
