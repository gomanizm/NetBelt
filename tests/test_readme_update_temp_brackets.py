"""README の自動更新の注意に、TEMP の角括弧で 1.3.0 以前からの更新が止まることを書くことの回帰（release-doc-a）。

1.3.1 では updater.bat の展開の行で、Expand-Archive へ渡す展開先の
[ ] ` の前に ` を付け、TEMP のパスに角括弧があっても展開できるように
した（tests/test_updater_temp_brackets.py）。ところが更新を当てるのは
「いま入っている版の updater.bat」なので、1.3.0 以前から 1.3.1 へ上げる
ときには、この修正は効かない。

実測（cx8b-release の repro_a_v130_transition.py / repro_a_older.py）:
公開済みの v1.3.0 の ZIP に入っている updater.bat をインストール先に置き、
1.3.1 相当の ZIP（updater.bat は HEAD のもの）を 1.3.0 のアプリと同じ
3 引数で当てた。
  TEMP=...\\Temp       rc=0、NetBelt.exe と updater.bat が差し替わった
  TEMP=...\\Temp[lab]  rc=1、『エラー: An item with the specified name
                       ...\\Temp[lab]\\NetBeltUpdate_1_N\\zip already exists.』
                       『エラー: ZIPファイルの展開に失敗しました』、
                       NetBelt.exe は旧版のまま
v1.2.0・v1.1.1 の updater.bat も Temp[lab] で同じく展開に失敗した。
HEAD の updater.bat（手で 1 回入れ替えた後の、次の自動更新に当たる）は
Temp[lab] でも rc=0 で差し替わった。README の「1.2.0 以前から更新する
場合の注意」には、この件の案内が無かった。

直した形: その注意の直後に短い注意を足した。TEMP のパスに [ ] があると
1.3.0 以前からの自動更新は展開で止まること、その 1 回だけ ZIP を手で
展開して入れ替えれば、1.3.1 以降は自動更新で当たること。
"""
import io
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(REPO_ROOT, "README.md")
UPDATER = os.path.join(REPO_ROOT, "updater.bat")
# 更新の注意が置かれている場所（この見出しから次の ## 見出しまで）
ANCHOR = "以前から更新する場合の注意"
# 1.3.1 の updater.bat が展開先の [ ] ` を逃がしている行（修正の前提）
ESCAPE = "$env:PS_DEST = $env:PS_DEST -replace '([\\[\\]`])', '`$1'"


def _update_notes():
    """README の更新の注意から、次の ## 見出しまでを返す。"""
    with io.open(README, encoding="utf-8") as f:
        text = f.read()
    start = text.find(ANCHOR)
    if start < 0:
        raise AssertionError("README に「%s」が見つからない" % ANCHOR)
    end = text.find("\n## ", start)
    return text[start:end if end >= 0 else len(text)]


class ReadmeUpdateTempBracketsTest(unittest.TestCase):

    def test_the_premise_the_current_updater_escapes_the_destination(self):
        """前提: 1.3.1 以降の updater.bat は、展開先の角括弧を逃がしている。"""
        with io.open(UPDATER, encoding="utf-8") as f:
            self.assertIn(ESCAPE, f.read())

    def test_the_notes_warn_about_brackets_in_temp(self):
        """TEMP の角括弧で、1.3.0 以前からの更新が展開で止まることを書くこと。"""
        notes = _update_notes()
        for word in ("TEMP", "[", "]", "1.3.0 以前", "展開"):
            self.assertIn(word, notes,
                          "更新の注意に「%s」が無い:\n%s" % (word, notes))

    def test_the_notes_give_the_one_time_workaround(self):
        """その 1 回だけ手で展開すれば、1.3.1 以降は自動更新で当たると書くこと。"""
        notes = _update_notes()
        for word in ("手で展開", "1.3.1 以降"):
            self.assertIn(word, notes,
                          "更新の注意に「%s」が無い:\n%s" % (word, notes))


if __name__ == "__main__":
    unittest.main()
