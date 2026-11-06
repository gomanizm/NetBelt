# -*- coding: utf-8 -*-
"""長い OID の名前解決が OID の段数について線形の時間で終わることを検証する。

resolve_oid() の部分一致は、OID を後ろから 1 段ずつ削りながら毎回
'.'.join(parts[:i]) を作り直していたので、段数 n について O(n^2) だった。
Trap の受信経路（SNMPPanel._add_trap_to_tree）は GUI スレッドで受け取った
OID をそのまま渡すので、約 65,000 段の OID を 1 つ載せた UDP 1 通で GUI が
約 10 秒固まった（実測）。

登録済みの OID の最大段数より深い前方部分は辞書に載りえないので、そこから
削り始めれば結果は変わらない。
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, "src")


class MibResolveOidLongOidTest(unittest.TestCase):
    def setUp(self):
        # mibs/ の無い凍結ビルドとして動かす（内蔵の表だけ）
        self.exe_dir = tempfile.mkdtemp(prefix="netbelt-mib-long-")
        exe = os.path.join(self.exe_dir, "NetBelt.exe")
        for patch in (mock.patch.object(sys, "frozen", True, create=True),
                      mock.patch.object(sys, "executable", exe)):
            patch.start()
            self.addCleanup(patch.stop)

    def _resolver(self):
        from core.mib_resolver import MIBResolver
        return MIBResolver()

    @staticmethod
    def _reference(table, oid):
        """直す前と同じ手順の最長一致（結果の比較用）"""
        if oid in table:
            return table[oid]
        parts = oid.split('.')
        for i in range(len(parts), 0, -1):
            partial = '.'.join(parts[:i])
            if partial in table:
                remaining = '.'.join(parts[i:])
                return f"{table[partial]}.{remaining}" if remaining else table[partial]
        return oid

    def test_a_very_long_oid_resolves_quickly(self):
        """65,000 段の OID が 1 秒台に収まること（直す前は約 10 秒）。"""
        resolver = self._resolver()
        oid = '1.3.6.1.4.1.32473' + '.1' * 64993
        started = time.perf_counter()
        name = resolver.resolve_oid(oid)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 2.0)
        self.assertEqual(name, 'enterprises.32473' + '.1' * 64993)

    def test_results_are_unchanged(self):
        """いろいろな OID で、直す前の手順と同じ名前になること。"""
        resolver = self._resolver()
        oids = ['1.3.6.1.2.1.2.2.1.2.5', '1.3.6.1.2.1.1.3.0', '1.3.6.1.2.1.1.3.0.7',
                '1.3.6.1.4.1.9.9.41', '2.5.4', '1', '', '1..3', '1.3.6.1.',
                '.1.3.6', 'abc', '1.3.6.1.6.3.1.1.5.3', '1.3.6.1.2.1.15.3.1.2.1.2.3.4']
        for oid in oids:
            with self.subTest(oid=oid):
                self.assertEqual(resolver.resolve_oid(oid),
                                 self._reference(resolver.oid_to_name, oid))

    def test_a_deep_entry_added_later_is_still_found(self):
        """解決したあとに足した深い OID も最長一致で引けること。"""
        resolver = self._resolver()
        deep = '1.3.6.1.4.1.32473' + '.2' * 40
        self.assertEqual(resolver.resolve_oid(deep + '.9'),
                         self._reference(resolver.oid_to_name, deep + '.9'))
        resolver.add_custom_mib(deep, 'deepNode')
        self.assertEqual(resolver.resolve_oid(deep + '.9'), 'deepNode.9')
        # 辞書へ直接足しても（件数が変わるので）引けること
        deeper = deep + '.3.3.3.3.3'
        resolver.oid_to_name[deeper] = 'deeperNode'
        self.assertEqual(resolver.resolve_oid(deeper + '.1'), 'deeperNode.1')

    def test_a_replaced_table_is_used(self):
        """oid_to_name を差し替えても、差し替えた表で引くこと。"""
        resolver = self._resolver()
        resolver.resolve_oid('1.3.6.1.2.1.1')
        resolver.oid_to_name = {'1.2.3.4.5.6.7.8.9.10.11.12.13.14.15.16.17.18.19.20': 'x'}
        self.assertEqual(
            resolver.resolve_oid('1.2.3.4.5.6.7.8.9.10.11.12.13.14.15.16.17.18.19.20.21'),
            'x.21')


if __name__ == "__main__":
    unittest.main()
