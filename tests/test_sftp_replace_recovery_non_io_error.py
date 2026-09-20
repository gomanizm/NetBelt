"""置き換えの復旧手順が IOError 以外で失敗しても、唯一の完全な写しを消さないことを検証する。

posix_rename の無い機器では、1 本目の rename が「既にある」で断られたとき
だけ最終名を remove し、もう一度 rename する。その 2 本目の rename と、
最終名の remove は except TimeoutError / except IOError しか見ていなかった。
paramiko は OSError の仲間でない例外も上げる（EOFError、SFTPError、
SSHException("Server connection dropped: ")）ので、それらは外側の
except Exception へ落ち、(1) keep_tmp が立たないまま後始末が一時名
（最終名を消したあとは唯一の完全な写し）まで消し、(2) 文面も最終名を
消してあることを伝えない、という二重の被害になっていた。

実測（mock の機器: stat が 0o100600、posix_rename が IOError、rename が
[IOError("Failure"), EOFError("")]、remove が成功、overwrite=True）:
remove の呼び出しは ['/flash/running.cfg', '/flash/.running.cfg.netbelt-part.…']
で、通知は 'アップロードエラー: EOFError' だけだった。最終名を消す復旧手順が
効く機器（＝非 posix の rename しか無い機器）でそのまま起きる。

直し方: 2 本目の rename の `except IOError as e:` と、最終名の remove の
`except IOError: pass` を `except Exception` に広げる。どちらも
except TimeoutError の後ろなので、期限切れはこれまでどおり先に取られる。
"""
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")

FINAL = "/flash/running.cfg"


class SftpReplaceRecoveryNonIoErrorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="netbelt-sftp-nonio-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.local = os.path.join(self.dir, "running.cfg")
        with io.open(self.local, "w", encoding="utf-8") as f:
            f.write("hostname R1")

    def _wait(self, predicate, seconds=5.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def _replace(self, rename_effects, remove_effect=None):
        """posix_rename の無い機器へ、承認済みの置き換えを送る"""
        from core.sftp_manager import SFTPManager

        m = SFTPManager()
        m.is_connected = True
        self.client = m.sftp_client = mock.Mock()
        attr = mock.Mock()
        attr.st_mode = 0o100600          # 置き換える最終名がある
        self.client.stat.return_value = attr
        self.client.posix_rename.side_effect = IOError("Operation unsupported")
        self.client.rename.side_effect = rename_effects
        self.client.remove.side_effect = remove_effect
        m.list_directory = mock.Mock()      # 転送後の一覧更新は動かさない
        self.errors, self.done = [], []
        m.error_occurred.connect(self.errors.append)
        m.transfer_complete.connect(self.done.append)

        m.upload_file(self.local, FINAL, overwrite=True)

        self.assertTrue(self._wait(lambda: self.errors or self.done),
                        "完了もエラーも届かない")
        return m

    def test_a_non_io_error_from_the_second_rename_keeps_the_temporary_copy(self):
        """2 本目の rename が IOError 以外で落ちても、一時名を消さないこと。"""
        self._replace([IOError("Failure"), EOFError("")])

        tmp = self.client.put.call_args[0][1]
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertIn(FINAL, removed, "復旧手順を通っていない: %s" % removed)
        self.assertNotIn(tmp, removed,
                         "唯一の完全な写し（一時名）を消している: %s" % removed)
        self.assertEqual(self.done, [],
                         "置き換えていないのに完了を通知している: %s" % self.done)

    def test_a_non_io_error_from_the_second_rename_says_the_final_name_is_gone(self):
        """そのとき、最終名を消してあることと一時名の在処を伝えること。"""
        self._replace([IOError("Failure"), EOFError("")])

        tmp = self.client.put.call_args[0][1]
        self.assertTrue(self.errors, "失敗が通知されない")
        joined = " / ".join(self.errors)
        self.assertIn(tmp, joined,
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        self.assertIn("最終名は置き換えの手順で既に消してあります", joined,
                      "元の設定が消えていることが伝わらない: %s" % self.errors)

    def test_a_non_io_error_from_the_remove_still_retries_the_rename(self):
        """最終名の remove が IOError 以外で断られても、そこで投げ出さないこと。

        remove が失敗していれば最終名は残っている。2 本目の rename まで
        進めば、失敗しても一時名の在処が伝わり、後始末に消されない。
        """
        self._replace([IOError("Failure"), IOError("Failure")],
                      remove_effect=EOFError(""))

        tmp = self.client.put.call_args[0][1]
        self.assertEqual(self.client.rename.call_count, 2,
                         "remove の失敗で 2 本目の rename へ進んでいない")
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertNotIn(tmp, removed,
                         "転送した内容（一時名）を消している: %s" % removed)
        joined = " / ".join(self.errors)
        self.assertIn(tmp, joined,
                      "機器に残った一時名を知らせていない: %s" % self.errors)
        self.assertNotIn("最終名は置き換えの手順で既に消してあります", joined,
                         "消せていない最終名を消したと伝えている: %s" % self.errors)

    def test_a_working_replace_is_unchanged(self):
        """これまでどおり、復旧手順が通れば置き換えが完了すること。"""
        self._replace([IOError("Failure"), None])

        self.assertEqual(self.done, ["アップロード完了: running.cfg"],
                         "承認済みの置き換えが通らなくなっている: %s" % self.errors)
        removed = [c[0][0] for c in self.client.remove.call_args_list]
        self.assertEqual(removed, [FINAL], "remove の対象が変わっている: %s" % removed)


if __name__ == "__main__":
    unittest.main()
