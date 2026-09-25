"""同じ接続先に食い違う 2 行があっても、他の機器の保存で消えないことを検証する。

何が起きていたか（基準 aa38a2b で実測。A=192.0.2.1、B=192.0.2.2）。
OpenSSH は鍵の入れ替え期間に同じホストの行を並べることを許すので、
利用者が新しい鍵の行を手で足した known_hosts は普通に起きる:

    192.0.2.1 ecdsa-sha2-nistp256 <旧>
    192.0.2.1 ecdsa-sha2-nistp256 <新>

paramiko にこれを読ませると、検証に使われるのは先頭の行
（HostKeys.load → lookup → SubDict.__getitem__ が最初に見つけた
エントリを返す）なので「旧=True / 新=False」。

ところが NetBelt 側の load_known_hosts() は行ごとに HostKeys.add() を
呼んでいた。add() は (接続先, 鍵種別) が同じ既存エントリを置き換えるので、
食い違う 2 行のうち先の行が消えて後の行だけが残る。_save_known_hosts は
保存のたびにこの関数でディスクを読み直すため、A に一切触らずに B の
初回接続を保存しただけで:

    保存前 : 旧=True  新=False（A は 2 行）
    保存後 : 旧=False 新=True （A は 1 行）

となり、利用者が書いた行が黙って 1 本消えたうえ、A が検証に使う鍵まで
入れ替わる。A は次の接続から BadHostKeyException で拒否される。基準
f4cad23 では A の行は残っていた（実際は増殖した）ので、この入力に
ついては 2d9394a が持ち込んだ回帰でもある。2026-09-20 の決定
「食い違いなら中止。上書きしない」にも反する向きになっていた。

どう直したか。load_known_hosts() を paramiko の HostKeys.load と同じ
「先に読んだ行を優先し、食い違う同じ鍵種別の行は潰さずに並べる」に
そろえた。同じ鍵がすでにある行（同一行の重複）は今までどおり畳む。
これで _refuse_conflicting_host_key() の判定も、paramiko の検証と同じ
「先頭の行」を見るようになる。
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


class KnownHostsDuplicateLinesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_old = paramiko.ECDSAKey.generate()
        cls.key_new = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp(prefix="netbelt-khdup-"))
        self.addCleanup(shutil.rmtree, str(self.data_dir), True)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.data_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.known_hosts = self.data_dir / "known_hosts"

    # --- 土台 ---

    def _line(self, hostname, key):
        return "%s %s %s" % (hostname, key.get_name(), key.get_base64())

    def _write(self, lines):
        self.known_hosts.write_text("".join(line + "\n" for line in lines),
                                    encoding="utf-8")

    def _save_b(self):
        """B の初回接続を TOFU で保存し、保存後の行を返す。"""
        from core.ssh_connection import SSHConnection
        conn = SSHConnection(HOST_B, 22, "admin", password="pw")
        self.addCleanup(conn.deleteLater)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        conn._setup_host_keys(client)
        client._policy.missing_host_key(client, HOST_B, self.key_b)
        return [line for line
                in self.known_hosts.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def _paramiko_view(self):
        """次に繋ぐ paramiko がこのファイルをどう見るか。"""
        view = paramiko.HostKeys()
        view.load(str(self.known_hosts))
        return view

    def _lines_for(self, lines, host):
        return [line for line in lines if line.split()[0] == host]

    # --- 触っていない接続先の行が消えないこと ---

    def test_saving_another_host_keeps_both_lines(self):
        """A の食い違う 2 行が、B の保存で 1 本に潰されないこと。"""
        self._write([self._line(HOST_A, self.key_old),
                     self._line(HOST_A, self.key_new)])

        lines = self._save_b()

        self.assertEqual(
            len(self._lines_for(lines, HOST_A)), 2,
            "触っていない接続先の行が消えている: %r" % (lines,))

    def test_saving_another_host_keeps_the_hand_written_line(self):
        """利用者が手で足した行が、そのまま残っていること。"""
        written = self._line(HOST_A, self.key_new)
        self._write([self._line(HOST_A, self.key_old), written])

        lines = self._save_b()

        self.assertIn(self._line(HOST_A, self.key_old), lines,
                      "先に書かれていた行が消えている: %r" % (lines,))
        self.assertIn(written, lines,
                      "手で足した行が消えている: %r" % (lines,))

    def test_saving_another_host_does_not_swap_the_verified_key(self):
        """A が検証に使う鍵（先頭の行）が入れ替わらないこと。"""
        self._write([self._line(HOST_A, self.key_old),
                     self._line(HOST_A, self.key_new)])

        self._save_b()

        view = self._paramiko_view()
        self.assertTrue(view.check(HOST_A, self.key_old),
                        "先頭の行の鍵が受け入れられなくなっている")
        self.assertFalse(view.check(HOST_A, self.key_new),
                         "後ろの行の鍵に入れ替わっている")

    def test_the_loader_agrees_with_paramiko_about_which_key_wins(self):
        """自前のローダを通る経路でも、先頭の行の鍵を採ること。"""
        from core.ssh_connection import SSHConnection
        # 読めない行を 1 本混ぜると、自前のローダを通る経路になる
        broken = "%s %s %s" % ("192.0.2.30", self.key_b.get_name(),
                               self.key_b.get_base64()[:-1])
        self._write([self._line(HOST_A, self.key_old),
                     self._line(HOST_A, self.key_new), broken])

        conn = SSHConnection(HOST_B, 22, "admin", password="pw")
        self.addCleanup(conn.deleteLater)
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        conn._setup_host_keys(client)

        keys = client.get_host_keys()
        self.assertTrue(keys.check(HOST_A, self.key_old),
                        "paramiko の検証と食い違う鍵を採っている")
        self.assertFalse(keys.check(HOST_A, self.key_new),
                         "後ろの行の鍵で検証しようとしている")

    # --- 今までどおりであること（対照） ---

    def test_the_new_host_is_still_saved(self):
        """初回接続の鍵は、これまでどおり保存されること。"""
        self._write([self._line(HOST_A, self.key_old),
                     self._line(HOST_A, self.key_new)])

        self._save_b()

        self.assertTrue(self._paramiko_view().check(HOST_B, self.key_b),
                        "保存しようとした鍵が残っていない")

    def test_identical_duplicate_lines_are_still_folded(self):
        """まったく同じ行の重複は、これまでどおり 1 本に畳むこと。"""
        same = self._line(HOST_A, self.key_old)
        self._write([same, same])

        lines = self._save_b()

        self.assertEqual(
            len(self._lines_for(lines, HOST_A)), 1,
            "同じ行が畳まれずに増えている: %r" % (lines,))


if __name__ == "__main__":
    unittest.main()
