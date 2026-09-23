"""ハッシュ化された行の隣にある平文の行が、他の機器の保存で消えないことを検証する。

何が起きていたか（基準 097550c で実測。A=192.0.2.1、B=192.0.2.2）。
2cd5b64 は load_known_hosts() で「もう入っている行か」を
HostKeys.check() で判定するように変えた。check() は paramiko の lookup を
通るので、完全一致の名前だけでなく、ハッシュ化された名前（|1|salt|hash）
とも照合する（paramiko 4.0.0 の HostKeys._hostname_matches）。そのため
known_hosts が

    |1|...|...  ecdsa-sha2-nistp256 <A の鍵>   ← ハッシュ化された A の行
    192.0.2.1   ecdsa-sha2-nistp256 <A の鍵>   ← 利用者が書いた平文の A の行

のとき、平文の行は「同じ鍵がもう入っている」とみなされて畳まれる。
_save_known_hosts は保存のたびにこの関数でディスクを読み直すので、
A に一切触らずに B の初回接続を保存しただけで平文の行が消えた:

    2cd5b64 の src を戻した状態 : 保存後 3 行（平文の行=残る／ハッシュ行=残る）
    097550c                     : 保存後 2 行（平文の行=消える／ハッシュ行=残る）

検証に使う鍵はハッシュ行が同じ鍵を指すので変わらないが、利用者が書いた
行が別の機器の保存で黙って消える。2026-09-20 の決定（上書きしない）と
同じ向きの問題で、2cd5b64 が閉じようとした形そのものだった。docstring の
「同じ鍵がもうある行（同一行の重複）は、これまでどおり畳む」も、実際には
同一行でない行まで畳んでいて説明と合っていなかった。

どう直したか。畳むのを「同じ名前・同じ鍵種別・同じ鍵」の行に限った。
名前は文字列のまま比べ、ハッシュ化名との照合（hash_host）はしない。
まったく同じ行の重複は今までどおり 1 本に畳み、同じ接続先で食い違う
行は潰さずに並べる（test_known_hosts_duplicate_lines.py の期待のまま）。
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
from paramiko.hostkeys import HostKeys              # noqa: E402

HOST_A = "192.0.2.1"
HOST_B = "192.0.2.2"


class KnownHostsHashedPlainLinesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_other = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()

    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp(prefix="netbelt-khhash-"))
        self.addCleanup(shutil.rmtree, str(self.data_dir), True)
        patcher = mock.patch("core.config_manager.app_data_dir",
                             return_value=self.data_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.known_hosts = self.data_dir / "known_hosts"
        self.hashed_a = HostKeys.hash_host(HOST_A)

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

    def _write_hashed_then_plain(self):
        self._write([self._line(self.hashed_a, self.key_a),
                     self._line(HOST_A, self.key_a)])

    # --- 利用者が書いた平文の行が消えないこと ---

    def test_saving_another_host_keeps_the_plain_line(self):
        """ハッシュ行と同じ鍵の平文行が、B の保存で消えないこと。"""
        self._write_hashed_then_plain()

        lines = self._save_b()

        self.assertIn(self._line(HOST_A, self.key_a), lines,
                      "利用者が書いた平文の行が消えている: %r" % (lines,))
        self.assertEqual(len(lines), 3,
                         "保存で行が増減している: %r" % (lines,))

    def test_the_loader_does_not_fold_a_plain_name_into_a_hashed_one(self):
        """ローダが、ハッシュ行と平文行を別の行として持つこと。"""
        from core.ssh_connection import load_known_hosts
        self._write_hashed_then_plain()

        keys = HostKeys()
        load_known_hosts(keys, self.known_hosts)

        names = [e.hostnames for e in keys._entries]
        self.assertEqual(names, [[self.hashed_a], [HOST_A]],
                         "平文の行がハッシュ行に畳まれている: %r" % (names,))

    # --- 今までどおりであること（対照） ---

    def test_saving_another_host_keeps_the_hashed_line(self):
        """ハッシュ行は、これまでどおり残ること。"""
        self._write_hashed_then_plain()

        lines = self._save_b()

        self.assertIn(self._line(self.hashed_a, self.key_a), lines,
                      "ハッシュ行が消えている: %r" % (lines,))

    def test_the_verified_key_stays_the_same(self):
        """A の検証に使う鍵が変わらず、B の鍵も保存されること。"""
        self._write_hashed_then_plain()

        self._save_b()

        view = HostKeys()
        view.load(str(self.known_hosts))
        self.assertTrue(view.check(HOST_A, self.key_a),
                        "A の鍵が受け入れられなくなっている")
        self.assertTrue(view.check(HOST_B, self.key_b),
                        "保存しようとした B の鍵が残っていない")

    def test_identical_hashed_lines_are_still_folded(self):
        """まったく同じハッシュ行の重複は、これまでどおり 1 本に畳むこと。"""
        same = self._line(self.hashed_a, self.key_a)
        self._write([same, same])

        lines = self._save_b()

        self.assertEqual(lines.count(same), 1,
                         "同じ行が畳まれずに残っている: %r" % (lines,))

    def test_conflicting_plain_lines_are_still_kept(self):
        """同じ接続先で食い違う 2 行は、これまでどおり並べたまま残すこと。"""
        first = self._line(HOST_A, self.key_a)
        second = self._line(HOST_A, self.key_other)
        self._write([first, second])

        lines = self._save_b()

        self.assertIn(first, lines, "先の行が消えている: %r" % (lines,))
        self.assertIn(second, lines, "後の行が消えている: %r" % (lines,))


if __name__ == "__main__":
    unittest.main()
