r"""BOM の区切り判定が、ファイル全体で 1 回の前進で済むことを検証する。

連結された MIB の区切りを見分けるため、コメントの中の BOM は「その BOM
の直前の実テキストが END で終わっている」ときだけ区切りとして扱う。
その判定（_ends_with_end）は、BOM の位置から後ろ向きに「空白と BOM」を
飛ばして END を探す。docstring と commit message は「BOM の数やファイル
長に関係なく一定時間」と書いていたが、そうはならない。コメントは
_blank_comments_and_strings で空白になっているので、BOM 入りの由来
コメントの間に実テキストが無いファイルでは 1 個あたり O(n) 歩き、
全体が O(n^2) になる。基準（区間で探す形）は区間が互いに素なので線形
だった。

実測（`-- moved from <BOM>OLDn-MIB DEFINITIONS ::= BEGIN` を n 行並べた
1 モジュール。_normalize_module_boms 単体）:
  n=1000 (53KB)  基準 0.007s → 修正前 1.02s
  n=2000 (107KB) 基準 0.010s → 修正前 4.20s
  n=4000 (215KB) 基準 0.020s → 修正前 16.5s（n を倍にすると約 4 倍）
同じファイルでも BOM の間に実テキストがあると歩きが 1 文字で止まるので
基準と変わらない（n=4000 で 0.027s）。見出しコメントだけを並べた連結
MIB の配布物で当たる。

直し方: 判定はそのままに、後ろ向きの歩きをやめて前向きのカーソルに
する。BOM の位置は昇順なので、ループの中で masked を 1 回だけ前に
進めながら「空白でも BOM でもない最後の文字の次の位置」を覚え、その
位置を _ends_with_end へ渡す。渡された位置の直前は必ず実テキストなので
後ろ向きの歩きは 0 歩で終わり、全体で 1 回の前進になる。

この検査の時間の上限は、同じ大きさで BOM の間に実テキストがある形
（修正の前後どちらも線形）の実測から作る。機械の速さや同時に走って
いる他のテストの混み具合で上限も一緒に動くので、絶対値で書くより
落ちにくい。それでも混雑だけで落ちたときは、単独で流し直して判断する。
"""
import sys
import time
import unittest

sys.path.insert(0, "src")

BOM = chr(0xFEFF)
NL = "\r\n"

# 由来を書いたコメントの中に BOM がある形。連結の区切りではないので、
# ここで区切ってはいけない（区切ると生きたコードがコメントの残りになる）
COMMENT = "    -- moved from " + BOM + "OLD%d-MIB DEFINITIONS ::= BEGIN"
REAL_TEXT = "obj%d OBJECT IDENTIFIER ::= { cRoot 1 }"

# 歩きが 1 文字で止まらない大きさ。修正前は n=4000 で 16.5 秒かかった
SCAN_N = 4000


def build(n, with_real_text):
    """BOM 入りの由来コメントを n 行並べた 1 モジュールを作る。"""
    out = ["C-MIB DEFINITIONS ::= BEGIN"]
    for i in range(n):
        out.append(COMMENT % i)
        if with_real_text:
            out.append(REAL_TEXT % i)
    out.append("cRoot OBJECT IDENTIFIER ::= { enterprises 1 }")
    out.append("END")
    return NL.join(out) + NL


class MibBomSplitScanStaysLinearTest(unittest.TestCase):
    def setUp(self):
        from core.mib_resolver import MIBResolver
        self.normalize = MIBResolver._normalize_module_boms

    def _elapsed(self, text):
        t = time.perf_counter()
        self.normalize(text)
        return time.perf_counter() - t

    def test_comment_only_boms_cost_the_same_as_boms_with_real_text(self):
        """コメントだけが続く連結でも、歩きが線形で収まること。"""
        # 同じ n で、BOM の間に実テキストがある形（修正の前後どちらも
        # 線形）を物差しにする。こちらの方が文字数は多い
        baseline = min(self._elapsed(build(SCAN_N, True)) for _ in range(3))
        elapsed = self._elapsed(build(SCAN_N, False))
        budget = max(2.0, baseline * 100)
        self.assertLess(
            elapsed, budget,
            "コメントだけが続く連結で歩きが O(n^2) になっている: "
            "%.3fs（物差し %.3fs・上限 %.3fs）" % (elapsed, baseline, budget))

    def test_a_bom_inside_a_comment_is_not_a_split(self):
        """由来コメントの中の BOM で区切らないこと（振る舞いの据え置き）。"""
        text = build(8, False)
        out = self.normalize(text)
        self.assertEqual(len(out), len(text),
                         "BOM を 1 文字→1 文字で置き換えていない")
        self.assertEqual(out.count("\n"), text.count("\n"),
                         "由来コメントの中の BOM で区切っている")
        self.assertNotIn(BOM, out)

    def test_a_bom_right_after_end_is_still_a_split(self):
        """END の直後の BOM は、これまでどおり区切りとして扱うこと。"""
        text = (NL.join([
            "A-MIB DEFINITIONS ::= BEGIN",
            "    -- moved from " + BOM + "OLDA-MIB DEFINITIONS ::= BEGIN",
            "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }",
            "END",
        ]) + BOM + "B-MIB DEFINITIONS ::= BEGIN" + NL
            + "bRoot OBJECT IDENTIFIER ::= { enterprises 2222 }" + NL
            + "END" + NL)
        out = self.normalize(text)
        self.assertEqual(out.count("\n"), text.count("\n") + 1,
                         "END の直後の BOM で区切っていない")
        self.assertIn("END\nB-MIB DEFINITIONS ::= BEGIN", out,
                      "区切りの BOM が改行になっていない")

    def test_the_split_still_needs_the_end(self):
        """END で閉じていない BOM は、区切りにしないこと。"""
        text = (NL.join([
            "A-MIB DEFINITIONS ::= BEGIN",
            "aRoot OBJECT IDENTIFIER ::= { enterprises 1111 }",
            "    -- see also " + BOM + "OTHER-MIB DEFINITIONS ::= BEGIN",
            "aLeaf OBJECT IDENTIFIER ::= { aRoot 1 }",
            "END",
        ]) + NL)
        out = self.normalize(text)
        self.assertEqual(out.count("\n"), text.count("\n"),
                         "END で閉じていない BOM で区切っている")


if __name__ == "__main__":
    unittest.main()
