# -*- coding: utf-8 -*-
"""GET / WALK の結果に行数の上限があることを検証する。

_perform_walk は応答の行を上限なく貯めていたので、部分木の中で OID を
厳密に増やしながら応答し続ける機器（故障・悪意）へ WALK を向けると、
ワーカーのリストと結果の表のメモリが際限なく伸びた。Trap 側の max_traps と
同じ考え方で上限を設け、超えたら打ち切って「途中まで」として理由を出す。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, "src")


class _Pretty:
    def __init__(self, text):
        self._text = text

    def prettyPrint(self):
        return self._text


class OctetString(_Pretty):
    pass


def _rows(count):
    return [(_Pretty("1.3.6.1.4.1.32473.3.%d.0" % i), OctetString("row-%d" % i))
            for i in range(1, count + 1)]


def _walk_of(count, per_response=1):
    """count 行を per_response 行ずつ返す _snmp_walk の代わり"""
    def fake(*args, **kwargs):
        rows = _rows(count)
        for start in range(0, count, per_response):
            yield (None, None, None, rows[start:start + per_response])
    return fake


class SnmpResultRowLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _run(self, operation, params, patch_target, fake, limit=5):
        from core.snmp_manager import SNMPWorker
        worker = SNMPWorker(operation, dict({
            "host": "192.0.2.1", "port": 161, "version": "v2c",
            "community": "public"}, **params))
        seen, warnings = [], []
        worker.result_ready.connect(lambda ok, r: seen.append((ok, r)))
        worker.partial_result.connect(warnings.append)
        with mock.patch.object(SNMPWorker, "MAX_RESULT_ROWS", limit), \
                mock.patch(patch_target, fake):
            worker.run()
        self.assertEqual(len(seen), 1)
        return seen[0][0], seen[0][1], warnings

    def test_the_default_limit(self):
        from core.snmp_manager import SNMPWorker
        self.assertEqual(SNMPWorker.MAX_RESULT_ROWS, 100000)

    def test_a_walk_stops_at_the_limit(self):
        ok, result, warnings = self._run(
            "walk", {"oid": "1.3.6.1.4.1.32473.3"},
            "core.snmp_manager._snmp_walk", _walk_of(20))
        self.assertTrue(ok)
        self.assertEqual(len(result), 5)
        self.assertEqual(len(warnings), 1)
        self.assertIn("上限", warnings[0])
        self.assertIn("5", warnings[0])

    def test_a_walk_stops_inside_a_multi_row_response(self):
        ok, result, warnings = self._run(
            "walk", {"oid": "1.3.6.1.4.1.32473.3"},
            "core.snmp_manager._snmp_walk", _walk_of(20, per_response=3))
        self.assertTrue(ok)
        self.assertEqual(len(result), 5)
        self.assertEqual(len(warnings), 1)

    def test_a_walk_of_exactly_the_limit_is_complete(self):
        ok, result, warnings = self._run(
            "walk", {"oid": "1.3.6.1.4.1.32473.3"},
            "core.snmp_manager._snmp_walk", _walk_of(5))
        self.assertTrue(ok)
        self.assertEqual(len(result), 5)
        self.assertEqual(warnings, [])

    def test_a_get_is_cut_at_the_limit(self):
        def fake_get(*args, **kwargs):
            return None, None, None, _rows(8)
        ok, result, warnings = self._run(
            "get", {"oids": ["1.3.6.1.4.1.32473.3.1.0"]},
            "core.snmp_manager._snmp_get", fake_get)
        self.assertTrue(ok)
        self.assertEqual(len(result), 5)
        self.assertEqual(len(warnings), 1)
        self.assertIn("上限", warnings[0])


if __name__ == "__main__":
    unittest.main()
