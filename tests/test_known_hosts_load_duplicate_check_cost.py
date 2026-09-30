"""known_hosts を読み込むときの重複の判定が、行数の 2 乗で重くならないことを検証する。

何が起きていたか（基準 c2bb66a で実測）。ハッシュ化名の行があるファイルを読む
_HostKeysLoadedLinearly の check() と、保存・食い違い確認・BOM や壊れた行が
あるときに使う自前のローダ load_known_hosts は、1 行（1 名前）ごとに、それまでに
読んだ _entries 全体を any() でたどって「同じ名前・同じ鍵種別・同じ鍵」の
エントリを探していた。名前がすべて異なる N 行では N(N-1)/2 件をたどる:

    名前がすべて異なる 2,000 行（先頭 1 行だけハッシュ化）: 1,999,000 件
    同じ形で 10,000 行: 49,995,000 件。_HostKeysLoadedLinearly.load が 1.9 秒、
        load_known_hosts が 1.9 秒、初回接続の保存 1 回（_save_known_hosts。
        load_known_hosts を 2 回通る）が 4.2 秒

どれも known_hosts の錠を握ったまま動くので、その間ほかの接続の読み込みや
鍵の保存が待たされる。hash_host の回数は行数に比例していたので、
test_known_hosts_load_cost.py では見えなかった。

どう直したか。重複の判定を (名前, 鍵種別, 鍵) の集合で引くようにした。
_HostKeysLoadedLinearly は、check() のたびに _entries の増えた分だけを集合へ
足す（paramiko の HostKeys.load は _entries へ足すだけなので、足した分を見れば
足りる）。load_known_hosts は、渡された HostKeys に元からあるエントリと、
自分が足したエントリを集合に入れる。畳む行の決め方は前と同じなので、
残る行と順番は変わらない（下のテストで前の定義と突き合わせる）。

たどった回数は、エントリの名前の並び（hostnames）を見た回数で数える。
CPU の混雑や GC の止まりで結果は揺れない。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, "src")

import paramiko                                     # noqa: E402
from paramiko.hostkeys import HostKeyEntry, HostKeys  # noqa: E402

LINES = 2000


class _CountingNames(list):
    """名前の並び。in とたどりの回数を数える。"""

    looks = 0

    def __contains__(self, item):
        _CountingNames.looks += 1
        return list.__contains__(self, item)

    def __iter__(self):
        _CountingNames.looks += 1
        return list.__iter__(self)


def _counting_entries():
    """これ以降に作られる HostKeyEntry の hostnames を _CountingNames にする。"""
    real_init = HostKeyEntry.__init__

    def init(self, hostnames=None, key=None):
        if hostnames is not None:
            hostnames = _CountingNames(hostnames)
        real_init(self, hostnames, key)

    return mock.patch.object(HostKeyEntry, "__init__", init)


def _shape(hostkeys):
    """残ったエントリを (名前の並び, 鍵種別, 鍵) の並びにする。"""
    return [(list(e.hostnames), e.key.get_name(), e.key.asbytes())
            for e in hostkeys._entries]


def _check_by_scanning(entries, hostname, key):
    """前の定義の重複の判定（既存のエントリをすべてたどる）。突き合わせの基準。"""
    return any(hostname in e.hostnames and e.key.get_name() == key.get_name()
               and e.key.asbytes() == key.asbytes() for e in entries)


class _ScanningHostKeys(paramiko.HostKeys):
    """前の _HostKeysLoadedLinearly と同じ判定で読む HostKeys（基準）。"""

    def check(self, hostname, key):
        return _check_by_scanning(self._entries, hostname, key)


def _load_by_scanning(hostkeys, path):
    """前の load_known_hosts と同じ判定で読む（基準）。"""
    from core.ssh_connection import _iter_known_hosts_lines
    for _lineno, _text, _raw, entry in _iter_known_hosts_lines(path):
        if entry is None:
            continue
        for name in entry.hostnames:
            if not _check_by_scanning(hostkeys._entries, name, entry.key):
                hostkeys._entries.append(HostKeyEntry([name], entry.key))


class KnownHostsDuplicateCheckCostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key_a = paramiko.ECDSAKey.generate()
        cls.key_b = paramiko.ECDSAKey.generate()
        cls.key_c = paramiko.RSAKey.generate(1024)        # 別の鍵種別

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="netbelt-khdup-"))
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        self.known_hosts = self.dir / "known_hosts"
        _CountingNames.looks = 0

    def _line(self, name, key):
        return "%s %s %s\n" % (name, key.get_name(), key.get_base64())

    def _write_distinct(self):
        """名前がすべて異なる LINES 行（先頭 1 行だけハッシュ化）を置く。"""
        text = "".join(
            self._line(HostKeys.hash_host("h0.example.com") if i == 0
                       else "h%d.example.com" % i, self.key_a)
            for i in range(LINES))
        self.known_hosts.write_text(text, encoding="ascii")

    def _assert_linear(self, what, limit):
        self.assertLessEqual(
            _CountingNames.looks, limit,
            "%s: known_hosts %d 行で名前の並びを %d 回見ている"
            "（行ごとに既存のエントリをすべてたどっている）"
            % (what, LINES, _CountingNames.looks))

    def test_hashed_file_loader_does_not_rescan_entries(self):
        """ハッシュ化行のあるファイルの接続前の読み込みで、重複の判定が行数に比例すること。"""
        from core.ssh_connection import _load_known_hosts_into_client
        self._write_distinct()
        client = paramiko.SSHClient()
        self.addCleanup(client.close)
        with _counting_entries():
            _load_known_hosts_into_client(client, self.known_hosts, [])

        self.assertEqual(len(client.get_host_keys()._entries), LINES)
        self._assert_linear("_HostKeysLoadedLinearly", LINES * 4)

    def test_own_loader_does_not_rescan_entries(self):
        """自前のローダ load_known_hosts で、重複の判定が行数に比例すること。"""
        from core.ssh_connection import load_known_hosts
        self._write_distinct()
        hostkeys = paramiko.HostKeys()
        with _counting_entries():
            load_known_hosts(hostkeys, self.known_hosts)

        self.assertEqual(len(hostkeys._entries), LINES)
        self._assert_linear("load_known_hosts", LINES * 4)

    def test_first_seen_save_does_not_rescan_entries(self):
        """初回接続の鍵の保存 1 回（錠の中で 2 回読み直す）も、行数に比例すること。"""
        from core.ssh_connection import _save_known_hosts
        self._write_distinct()
        with _counting_entries():
            _save_known_hosts(self.known_hosts,
                              ("new.example.com", self.key_b),
                              ("new.example.com", 22))

        text = self.known_hosts.read_text(encoding="ascii")
        self.assertEqual(text.count("\n"), LINES + 1)
        self.assertIn(self._line("new.example.com", self.key_b), text)
        self._assert_linear("_save_known_hosts", LINES * 20)

    def _tricky_text(self):
        """畳む・畳まないが分かれる行を集めた known_hosts。"""
        a, b = self.key_a, self.key_b
        hashed = HostKeys.hash_host("h9.example.com")
        lines = [
            "# comment\n",
            "\n",
            self._line("h1.example.com", a),
            self._line("h1.example.com", a),                 # 同一行の重複
            self._line("h1.example.com", b),                 # 鍵の入れ替え期間
            self._line("h1.example.com", b),
            self._line("h1.example.com,h2.example.com", a),  # 片方だけ重複
            self._line("h2.example.com,h2.example.com", a),  # 行の中の重複
            self._line("h3.example.com,h1.example.com,h4.example.com", b),
            self._line("[h1.example.com]:2202", a),
            self._line("H1.EXAMPLE.COM", a),                 # 大文字小文字は別の名前
            self._line(hashed, a),
            self._line(hashed, a),
            self._line("h9.example.com", a),                 # ハッシュ化行と同じ鍵の平文
            self._line("h5.example.com,h5.example.com,h6.example.com", a),
            self._line("h1.example.com", self.key_c),        # 別の鍵種別
            self._line("h1.example.com", self.key_c),
        ]
        return "".join(lines)

    def test_hashed_file_loader_keeps_the_same_entries(self):
        """_HostKeysLoadedLinearly に残る行と順番が、前の判定と同じであること。"""
        from core.ssh_connection import _HostKeysLoadedLinearly
        self.known_hosts.write_text(self._tricky_text(), encoding="ascii")
        expected = _ScanningHostKeys()
        expected.load(str(self.known_hosts))
        loaded = _HostKeysLoadedLinearly()
        loaded.load(str(self.known_hosts))

        self.assertEqual(_shape(loaded), _shape(expected))

    def test_hashed_file_loader_matches_paramiko_for_plain_duplicates(self):
        """同じ名前・同じ種別の鍵が 1 つだけの形では、paramiko の HostKeys.load と同じ行・同じ順であること。

        paramiko の check は、その名前・種別の先頭の鍵とだけ比べ、ハッシュ化名とも
        照合する。前の判定（e7fe2a2 以来）は、同じ名前の文字列・同じ鍵のエントリが
        どこかにあれば畳む。鍵の入れ替え期間（同じ名前に鍵 A と鍵 B）の後ろの
        鍵 B の名前と、ハッシュ化行と同じ鍵の平文行では畳むかどうかが分かれるが、
        どちらも先頭の鍵は変わらないので照合の結果は同じ。この 2 つは上の
        前の判定との突き合わせで見る。
        """
        from core.ssh_connection import _HostKeysLoadedLinearly
        a, b = self.key_a, self.key_b
        text = "".join([
            self._line(HostKeys.hash_host("h9.example.com"), a),
            self._line("h1.example.com", a),
            self._line("h1.example.com", a),
            self._line("h1.example.com,h2.example.com", a),
            self._line("h2.example.com,h2.example.com", a),
            self._line("h3.example.com,h1.example.com,h4.example.com", a),
            self._line("h5.example.com", b),
            self._line("h1.example.com", self.key_c),
            self._line("h1.example.com", self.key_c),
        ])
        self.known_hosts.write_text(text, encoding="ascii")
        expected = paramiko.HostKeys()
        expected.load(str(self.known_hosts))
        loaded = _HostKeysLoadedLinearly()
        loaded.load(str(self.known_hosts))

        self.assertEqual(_shape(loaded), _shape(expected))

    def test_own_loader_keeps_the_same_entries(self):
        """load_known_hosts に残る行と順番が、前の判定と同じであること（読み込み済みの分も見る）。"""
        from core.ssh_connection import load_known_hosts
        self.known_hosts.write_text(self._tricky_text(), encoding="ascii")
        for title, prefill in (
                ("empty", []),
                ("prefilled", [HostKeyEntry(["h1.example.com", "h7.example.com"],
                                            self.key_b),
                               HostKeyEntry(["h5.example.com"], self.key_a)])):
            with self.subTest(title):
                expected = paramiko.HostKeys()
                expected._entries.extend(prefill)
                _load_by_scanning(expected, self.known_hosts)
                hostkeys = paramiko.HostKeys()
                hostkeys._entries.extend(prefill)
                load_known_hosts(hostkeys, self.known_hosts)

                self.assertEqual(_shape(hostkeys), _shape(expected))


if __name__ == "__main__":
    unittest.main()
