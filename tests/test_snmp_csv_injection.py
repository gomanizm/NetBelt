"""エクスポートした CSV が、開いた側で数式として走らないことを検証する。

Trap の CSV 出力も GET/WALK の CSV 出力も、機器由来の文字列を無加工で
書き出していた。csv モジュールは区切り文字と引用符しかエスケープしない
ため、先頭が = + - @ の値はそのまま残り、Excel / LibreOffice で開いた
瞬間に数式（DDE を含む）として解釈される。

値は攻撃者が選べる。v1/v2c ならコミュニティ名を知っている者が UDP
パケット1発で任意の VarBind を入れられるし、GET/WALK 側も sysName /
sysLocation / sysContact など機器側で自由に書ける文字列が入る。
NetBelt のエクスポートは障害チケットや報告書へ回る前提なので、
受け取った側の端末で発火する。

負の数まで文字列にすると表として読みにくくなるので、数として読める値は
そのまま通す。
"""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

DANGEROUS = "=cmd|'/c calc.exe'!A1"


class SnmpCsvInjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _panel(self):
        # SNMPPanel 単体生成は MIB 読み込みでヘッドレスが落ちるため MainWindow 経由
        from ui.main_window import MainWindow
        return MainWindow().snmp_panel

    def _read(self, path):
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.reader(f))

    def _path(self, name):
        return os.path.join(tempfile.mkdtemp(prefix="netbelt-csv-"), name)

    @staticmethod
    def _trap(value, oid="1.3.6.1.4.1.9.9.41.2.0.1"):
        return {
            "timestamp": "2026-08-27 22:00:00",
            "source_ip": "192.0.2.1",
            "source_port": 162,
            "security": "public",
            "trap_oid": oid,
            "varbinds": [{"oid": "1.3.6.1.2.1.1.5.0", "value": value}],
        }

    # --- 危ないものを危ないまま書かない ---

    def test_a_formula_in_a_trap_value_is_not_left_executable(self):
        panel = self._panel()
        panel.trap_data_list = [self._trap(DANGEROUS)]
        path = self._path("traps.csv")

        panel._export_to_csv(path)

        cells = [c for row in self._read(path) for c in row]
        self.assertNotIn(DANGEROUS, cells,
                         "機器が入れた数式がそのまま書かれている")

    def test_a_formula_in_a_trap_oid_is_not_left_executable(self):
        panel = self._panel()
        panel.trap_data_list = [self._trap("ok", oid=DANGEROUS)]
        path = self._path("traps_oid.csv")

        panel._export_to_csv(path)

        cells = [c for row in self._read(path) for c in row]
        self.assertNotIn(DANGEROUS, cells,
                         "Trap OID 列の数式がそのまま書かれている")

    def test_a_formula_in_a_walk_result_is_not_left_executable(self):
        panel = self._panel()
        path = self._path("walk.csv")

        panel._export_results_to_csv(
            path, [["1.3.6.1.2.1.1.5.0", "OctetString", DANGEROUS]])

        cells = [c for row in self._read(path) for c in row]
        self.assertNotIn(DANGEROUS, cells,
                         "sysName に入れられた数式がそのまま書かれている")

    def test_the_original_text_is_still_readable(self):
        """無害化しても、元の文字列が読めること（消してはいけない）。"""
        panel = self._panel()
        path = self._path("walk_readable.csv")

        panel._export_results_to_csv(
            path, [["1.3.6.1.2.1.1.5.0", "OctetString", DANGEROUS]])

        text = open(path, encoding="utf-8").read()
        self.assertIn("calc.exe", text, "元の値が失われている")

    # --- 普通の値は変えない ---

    def test_an_ordinary_value_is_unchanged(self):
        panel = self._panel()
        path = self._path("plain.csv")

        panel._export_results_to_csv(
            path, [["1.3.6.1.2.1.1.5.0", "OctetString", "router01"]])

        self.assertEqual(self._read(path)[1],
                         ["1.3.6.1.2.1.1.5.0", "OctetString", "router01"])

    def test_a_negative_number_stays_a_number(self):
        """負の数を文字列に変えないこと（表として読めなくなる）。"""
        panel = self._panel()
        path = self._path("negative.csv")

        panel._export_results_to_csv(
            path, [["1.3.6.1.2.1.2.2.1.1", "Integer", "-1"]])

        self.assertEqual(self._read(path)[1][2], "-1",
                         "負の数まで文字列にしている")

    def test_values_that_python_reads_as_numbers_but_excel_does_not(self):
        """Python が数として読める値でも、表計算が数式にするものは通さないこと。

        float() は -inf / +nan / -1_000 を受け付けるが、Excel は先頭が
        - や + のセルを数式として解釈する（-inf なら #NAME? になる）。
        「数として読めるか」の判定を float() に任せると、この隙間が残る。
        """
        panel = self._panel()
        for value in ("-inf", "+nan", "-1_000", "-Infinity"):
            with self.subTest(value=value):
                path = self._path("num_%s.csv" % abs(hash(value)))
                panel._export_results_to_csv(
                    path, [["1.3.6.1.2.1.1.5.0", "Integer", value]])
                self.assertNotEqual(
                    self._read(path)[1][2], value,
                    "表計算が数式として読む値をそのまま書いている")

    def test_a_leading_space_does_not_smuggle_a_formula_through(self):
        """先頭に空白を置いて判定をすり抜けさせないこと。

        表計算ソフトは前置きの空白を落として解釈することがある。
        """
        panel = self._panel()
        smuggled = " " + DANGEROUS
        path = self._path("space.csv")

        panel._export_results_to_csv(
            path, [["1.3.6.1.2.1.1.5.0", "OctetString", smuggled]])

        self.assertNotEqual(self._read(path)[1][2], smuggled,
                            "空白を前置しただけで素通りしている")

    def test_plain_numbers_are_still_left_alone(self):
        """普通の数値は、これまでどおり数値のまま書くこと。"""
        panel = self._panel()
        for value in ("-1", "0", "42", "-3.5", "+7", "1.25e3"):
            with self.subTest(value=value):
                path = self._path("plain_%s.csv" % abs(hash(value)))
                panel._export_results_to_csv(
                    path, [["1.3.6.1.2.1.2.2.1.1", "Integer", value]])
                self.assertEqual(self._read(path)[1][2], value,
                                 "普通の数値まで文字列にしている")

    def test_the_header_row_is_unchanged(self):
        panel = self._panel()
        path = self._path("header.csv")
        panel._export_results_to_csv(path, [])
        self.assertEqual(self._read(path)[0], ["OID", "Type", "Value"])


if __name__ == "__main__":
    unittest.main()
