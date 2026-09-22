r"""曖昧さの判定が、モジュールの数に比例しないことを検証する。

利用者の決定（2026-09-20）: 親がどのモジュールのものか確定できない
ときは名前を付けず OID のまま出し、諦めた件数を標準出力へ 1 行知らせる。
その「確定できない」の判定を、_resolve_definitions は
`any(parent in names for other, names in declared.items() if other != module)`
で見ていた。1 回ごとに declared の全モジュールを走る。しかもこれは
「自分のモジュールが宣言した親がまだ解決していない」たびに走り、実 MIB
では普通に起きる（抽出は _MIB_DEFINITION_PATTERNS を種類ごとに順に
回すので、モジュールの OBJECT IDENTIFIER の節が MODULE-IDENTITY の根
より先に並び、1 回目の回では親が未解決になる）。つまり
回数 x 未解決の定義数 x モジュール数。

実測（_resolve_definitions を直接呼ぶ。1 モジュール = 根 + 深さ 60 の鎖、
子を先に並べた形）:
  400 モジュール / 24400 定義 … 修正前 11.223s、修正後 0.123s
  親を先に並べた同じ大きさ（曖昧さの判定を通らない形）は 0.010s
現実の書き方で作った mibs/ の冷えた解析でも、1500 モジュール 2.225s、
3000 モジュール 8.286s と、モジュール数が倍で増分が約 4 倍になっていた。
MIB_PARSER_VERSION を上げた直後なので、全利用者が更新後の初回起動で
必ずこれを払う。

直し方: declared を組んだ直後に「名前→その名前を宣言している
モジュールの数」を 1 回だけ作り、判定を辞書引き 1 回にする。どちらの
分岐も `parent in declared[module]` が真の場所なので、件数 2 以上＝
よそのモジュールも宣言している、と同値。出力は変わらないので
MIB_PARSER_VERSION は上げ直さない。

ここでは時間だけでなく、親の名前がハッシュされた回数も数える。
set への in も dict の get も __hash__ を呼ぶので、「よそのモジュールを
全部見に行っているか」が機械の速さに関係なく数に出る（実測: 1 定義
あたり modules=20 で 38.3 回、40 で 71.7 回、80 で 138.3 回。修正後は
どの大きさでも 8.3 回）。
"""
import contextlib
import io
import sys
import time
import unittest

sys.path.insert(0, "src")


class ProbedName(str):
    """__hash__ の回数を数える、親の名前用の str。

    親の名前だけをこれにする。定義の名前（declared に入る側）は素の
    str のままなので、数えられるのは「親を探すための引き」だけになる。
    """

    count = 0

    def __hash__(self):
        ProbedName.count += 1
        return str.__hash__(self)


def build(modules, depth, child_first=True, probe=False):
    """(名前, 親, 添字, モジュール) の一覧と、宣言だけの名前を作る。

    1 モジュールにつき、根 mNRoot ::= { enterprises ... } と、その下に
    深さ depth の鎖。child_first なら子を先に並べる（親が後ろにある
    実 MIB の書き方＝1 回目の回では親が未解決になる形）。
    """
    wrap = ProbedName if probe else str
    definitions = []
    local_names = {}
    for m in range(modules):
        module = "M%d-MIB" % m
        items = [("m%dRoot" % m, wrap("enterprises"), str(1000 + m), module)]
        parent = "m%dRoot" % m
        for i in range(depth):
            name = "m%dObj%d" % (m, i)
            items.append((name, wrap(parent), str(i + 1), module))
            parent = name
        if child_first:
            items = items[:1] + items[1:][::-1]
        definitions.extend(items)
        # 抽出から落ちた宣言（複数添字の右辺など）を模す
        local_names[module] = {"m%dLocal" % m}
    return definitions, local_names


class MibAmbiguityCheckDoesNotScanEveryModuleTest(unittest.TestCase):
    def _resolver(self, local_names, known=None):
        """__init__ を通さずに _resolve_definitions だけを動かす。"""
        from core.mib_resolver import MIBResolver
        resolver = MIBResolver.__new__(MIBResolver)
        resolver.name_to_oid = dict(known or {"enterprises": "1.3.6.1.4.1"})
        resolver.oid_to_name = {}
        resolver._module_local_names = {k: set(v)
                                        for k, v in local_names.items()}
        return resolver

    def _resolve(self, definitions, local_names, known=None):
        """解決した辞書と、標準出力に出た知らせの行を返す。"""
        resolver = self._resolver(local_names, known)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resolved = resolver._resolve_definitions(definitions)
        notices = [line for line in out.getvalue().splitlines()
                   if "解決せず" in line]
        return resolved, notices

    def _probes_per_definition(self, modules):
        """定義 1 件あたり、親の名前を何回ハッシュしたか。"""
        definitions, local_names = build(modules, 5, probe=True)
        ProbedName.count = 0
        resolved, _ = self._resolve(definitions, local_names)
        self.assertEqual(len(resolved), len(definitions),
                         "この形は全部解決するはず")
        return ProbedName.count / float(len(definitions))

    def test_the_check_does_not_grow_with_the_number_of_modules(self):
        """モジュールを 4 倍にしても、1 定義あたりの引きが増えないこと。"""
        few = self._probes_per_definition(20)
        many = self._probes_per_definition(80)
        self.assertLess(
            many, few * 1.5,
            "曖昧さの判定がモジュール数に比例している: 1 定義あたり "
            "20 モジュールで %.1f 回、80 モジュールで %.1f 回"
            % (few, many))

    def test_a_big_mibs_folder_is_resolved_in_time(self):
        """大きな mibs/ でも、解決が現実的な時間で終わること。

        上限は、同じ大きさで親を先に並べた形（曖昧さの判定を通らない）の
        実測から作る。機械の速さや同時に走っている他のテストの混み具合で
        上限も一緒に動くので、絶対値で書くより落ちにくい。それでも混雑
        だけで落ちたときは、単独で流し直して判断する。
        """
        modules, depth = 400, 60
        ordered, local_names = build(modules, depth, child_first=False)
        yardstick = min(self._elapsed(ordered, local_names)
                        for _ in range(3))
        definitions, local_names = build(modules, depth)
        elapsed = self._elapsed(definitions, local_names)
        budget = max(2.0, yardstick * 100)
        self.assertLess(
            elapsed, budget,
            "%d モジュール %d 定義の解決に %.3fs かかっている"
            "（物差し %.3fs・上限 %.3fs）"
            % (modules, len(definitions), elapsed, yardstick, budget))

    def _elapsed(self, definitions, local_names):
        resolver = self._resolver(local_names)
        start = time.perf_counter()
        with contextlib.redirect_stdout(io.StringIO()):
            resolver._resolve_definitions(definitions)
        return time.perf_counter() - start

    def test_an_ambiguous_parent_is_still_given_up(self):
        """よそのモジュールも同名を宣言していたら、決定どおり諦めること。"""
        definitions = [
            # A-MIB の shared は複数添字の右辺で、抽出から落ちている
            ("aAlarm", "shared", "1", "A-MIB"),
            ("shared", "enterprises", "2222", "B-MIB"),
            ("bAlarm", "shared", "9", "B-MIB"),
        ]
        resolved, notices = self._resolve(definitions, {"A-MIB": {"shared"}})
        self.assertEqual(resolved.get("1.3.6.1.4.1.2222.9"), "bAlarm")
        self.assertNotIn("aAlarm", resolved.values(),
                         "どのモジュールの親か決めずに解決している")
        self.assertEqual(len(notices), 1,
                         "諦めたことの知らせが 1 行ではない: %r" % (notices,))
        self.assertIn("1件", notices[0])

    def test_a_name_only_this_module_declares_still_borrows_the_known_oid(
            self):
        """よそに同名が無ければ、標準表の親をこれまでどおり使うこと。"""
        # SMI 自身の internet のように、抽出から落ちた宣言しか無い親
        definitions = [("directory", "internet", "1", "SMI-MIB")]
        resolved, notices = self._resolve(
            definitions, {"SMI-MIB": {"internet"}},
            known={"enterprises": "1.3.6.1.4.1", "internet": "1.3.6.1"})
        self.assertEqual(resolved.get("1.3.6.1.1"), "directory",
                         "曖昧でないのに解決を捨てている")
        self.assertEqual(notices, [],
                         "曖昧でないのに諦めたと知らせている: %r" % (notices,))


if __name__ == "__main__":
    unittest.main()
