"""ホスト鍵の保存が、別の機器の消された鍵を書き戻さないことを検証する。

何が起きていたか（基準 f4cad23 で実測。A=192.0.2.1、B=192.0.2.2）。
_save_known_hosts は、接続開始時に known_hosts を読み込んだ client の
HostKeys を丸ごと書き戻していた。client の在庫は読み込んだ時点のもの
なので、そのあとディスク側で行が消されても memory からは消えない。

(1) 利用者が A の行を削除した場合:
    ディスク操作後の known_hosts: （空）
    B の TOFU 保存後:
      192.0.2.1 ecdsa-sha2-nistp256 keyA1   <- 削除した行が復活
      192.0.2.2 ecdsa-sha2-nistp256 keyB
    次回 A へ接続: keyA1 を受け入れる? True

(2) 利用者が A の行を削除し、新しい鍵 keyA2 で登録し直した場合:
    ディスク操作後の known_hosts: 192.0.2.1 ... keyA2（1 行）
    B の TOFU 保存後:
      192.0.2.1 ecdsa-sha2-nistp256 keyA1
      192.0.2.1 ecdsa-sha2-nistp256 keyA1
      192.0.2.1 ecdsa-sha2-nistp256 keyA1
      192.0.2.2 ecdsa-sha2-nistp256 keyB
    次回 A へ接続: keyA1 -> True / keyA2 -> False

(2) では、利用者が入れ直した keyA2 がファイルから完全に消え、古い keyA1
が同じ行 3 本に増殖したうえで唯一の正になる。3 本になるのは、paramiko の
HostKeys.load が食い違う鍵を _entries へ append し、
SSHClient.save_host_keys が items() を回すとき lookup が先頭エントリを
返すため。verify 引数の食い違い検査は保存する接続先（B）しか見ないので、
A の巻き戻りは防げない。

窓は「別の接続が known_hosts を読んでから TOFU 保存するまで」で、機器が
遅いと banner_timeout の 30 秒まで伸びる。「鍵が食い違ったら該当行を削除
してから接続し直してください」は NetBelt 自身が案内している手順なので、
その最中に別タブが未知の機器へ繋ぎに行く順序は十分ありうる。

どう直したか。保存を「client の在庫を書き戻す」のをやめ、錠の中で
ディスクから HostKeys を作り直し、そこへ今回保存する 1 件だけを足して
書き出すようにした。副次的に、重複行の増殖も止まる。
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402

HOST_A = "192.0.2.1"
HOST_B = "192.0.2.2"


class KnownHostsSaveDoesNotResurrectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a1 = paramiko.ECDSAKey.generate()
        cls.key_a2 = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp(prefix="netbelt-khresurrect-"))
        self.addCleanup(shutil.rmtree, str(self.data_dir), True)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.data_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.known_hosts = self.data_dir / "known_hosts"

    # --- 土台 ---

    def _write(self, lines):
        self.known_hosts.write_text("".join(line + "\n" for line in lines),
                                    encoding="utf-8")

    def _line(self, hostname, key):
        return "%s %s %s" % (hostname, key.get_name(), key.get_base64())

    def _connection_that_read_the_file(self):
        """いまの known_hosts を読み終えた接続（client と TOFU ポリシー）。"""
        from core.ssh_connection import SSHConnection
        conn = SSHConnection(HOST_B, 22, "admin", password="pw")
        self.addCleanup(conn.deleteLater)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        conn._setup_host_keys(client)
        return client, client._policy

    def _save_b_after(self, disk_lines):
        """A の行を読んだ接続の裏でディスクを書き換え、B の鍵を保存する。"""
        self._write([self._line(HOST_A, self.key_a1)])
        client, policy = self._connection_that_read_the_file()
        self._write(disk_lines)
        policy.missing_host_key(client, HOST_B, self.key_b)
        return self.known_hosts.read_text(encoding="utf-8").splitlines()

    def _fresh_view(self):
        """次に繋ぐ接続が読む known_hosts の中身。"""
        hostkeys = paramiko.HostKeys()
        from core.ssh_connection import load_known_hosts
        load_known_hosts(hostkeys, self.known_hosts)
        return hostkeys

    # --- (1) 削除された行が復活しないこと ---

    def test_a_deleted_host_is_not_written_back(self):
        """利用者が消した行を、別の機器の保存が書き戻さないこと。"""
        lines = self._save_b_after([])

        self.assertNotIn(
            self._line(HOST_A, self.key_a1), lines,
            "削除された行が復活している: %r" % (lines,))

    def test_a_deleted_host_is_unknown_again(self):
        """消したあとは、その機器が「未知」に戻っていること。"""
        self._save_b_after([])

        self.assertIsNone(
            self._fresh_view().lookup(HOST_A),
            "削除した機器の鍵が残っており、消したつもりが効いていない")

    # --- (2) 入れ直した鍵が巻き戻らないこと ---

    def test_a_replaced_key_is_not_rolled_back(self):
        """案内どおり入れ直した新しい鍵を、古い鍵で置き換えないこと。"""
        self._save_b_after([self._line(HOST_A, self.key_a2)])

        view = self._fresh_view()
        self.assertTrue(
            view.check(HOST_A, self.key_a2),
            "入れ直した鍵がファイルから消えている")
        self.assertFalse(
            view.check(HOST_A, self.key_a1),
            "古い鍵が復活しており、入れ替えた機器の鍵が検証できない")

    def test_a_replaced_key_is_not_duplicated(self):
        """巻き戻しのついでに、同じ行を何本も書かないこと。"""
        lines = self._save_b_after([self._line(HOST_A, self.key_a2)])

        for_a = [line for line in lines if line.split()[0] == HOST_A]
        self.assertEqual(
            len(for_a), 1,
            "同じ接続先の行が %d 本ある: %r" % (len(for_a), lines))

    # --- 今までどおりであること（対照） ---

    def test_the_new_key_is_still_saved(self):
        """初回接続の鍵は、これまでどおり保存されること。"""
        self._save_b_after([self._line(HOST_A, self.key_a2)])

        self.assertTrue(self._fresh_view().check(HOST_B, self.key_b),
                        "保存しようとした鍵が残っていない")

    def test_an_untouched_host_is_kept(self):
        """ディスクを触らなければ、他の機器の鍵はそのまま残ること。"""
        self._save_b_after([self._line(HOST_A, self.key_a1)])

        self.assertTrue(self._fresh_view().check(HOST_A, self.key_a1),
                        "関係のない機器の鍵まで消えている")

    def test_an_unreadable_line_is_still_preserved(self):
        """paramiko が読めない行は、これまでどおり書き戻すこと。"""
        broken = "%s %s %s" % ("192.0.2.30", self.key_a1.get_name(),
                               self.key_a1.get_base64()[:-1])
        lines = self._save_b_after([self._line(HOST_A, self.key_a2), broken])

        self.assertIn(broken, lines,
                      "読めない行が消えている（利用者が直すはずの行）: %r"
                      % (lines,))

    def test_a_conflicting_key_is_still_refused(self):
        """同じ接続先に別の鍵が保存済みなら、これまでどおり中止すること。"""
        from core.ssh_connection import HostKeyMismatchError
        self._write([self._line(HOST_A, self.key_a1)])
        client, policy = self._connection_that_read_the_file()
        # 読んだあとに、別の NetBelt が B の鍵を保存した
        self._write([self._line(HOST_A, self.key_a1),
                     self._line(HOST_B, self.key_a2)])

        with self.assertRaises(HostKeyMismatchError):
            policy.missing_host_key(client, HOST_B, self.key_b)

        self.assertTrue(
            self._fresh_view().check(HOST_B, self.key_a2),
            "中止したのに、先に保存されていた鍵が書き換わっている")


if __name__ == "__main__":
    unittest.main()
