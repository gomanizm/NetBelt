"""ビルドスクリプトが、動く一式を作ることを検証する。

build.bat は PyInstaller を呼んで dist\\NetBelt.exe を作るところまでで
終わっており、updater.bat を dist へ入れていなかった。exe は自動更新の
最後に updater.bat を自分の隣から探す（凍結時は sys.executable の
ディレクトリ。src/ui/dialogs/update_dialog.py と src/ui/main_window.py の
両方）ので、ローカルでビルドした exe は更新をダウンロードできても
適用の直前で必ず失敗する。

ソースから実行している間は __file__ を辿ってリポジトリ直下の
updater.bat が見つかるため、この壊れ方は exe でしか出ない。
GitHub Releases の ZIP は CI（.github/workflows/build-release.yml）が
別途コピーしているので影響を受けず、ローカルビルドだけが壊れる。

さらに build.bat は「このファイルは単独で動作します」と表示しており、
exe だけを配れば済むと読める。updater.bat を置き去りにする案内なので、
実態に合わせる必要がある。
"""
import io
import os
import unittest


def _build_bat():
    # build.bat は BOM なしの UTF-8。cp932 で読むと日本語が化けて、
    # 文言の検査が素通りする
    with io.open("build.bat", encoding="utf-8") as f:
        return f.read()


class BuildScriptTest(unittest.TestCase):
    def test_updater_is_placed_next_to_the_exe(self):
        """updater.bat を dist へ入れること。"""
        text = _build_bat()
        lines = [l for l in text.splitlines()
                 if "updater.bat" in l and l.strip().lower().startswith("copy")]
        self.assertTrue(lines,
                        "build.bat が updater.bat を dist へコピーしていない")
        self.assertTrue(any("dist" in l for l in lines),
                        "コピー先が dist になっていない: %s" % lines)

    def test_the_distribution_notes_do_not_claim_the_exe_stands_alone(self):
        """exe 単独で配れると案内しないこと。

        updater.bat を置き去りにすると自動更新が死ぬので、
        「単独で動作します」は嘘になる。
        """
        text = _build_bat()
        self.assertNotIn("このファイルは単独で動作します", text,
                         "exe 単独で配れる、と案内したままになっている")

    def test_the_distribution_notes_name_both_files(self):
        """配布に要る2つを、どちらも挙げること。"""
        text = _build_bat()
        start = text.find("配布方法")
        self.assertNotEqual(start, -1, "配布方法の案内が消えている")
        notes = text[start:]
        self.assertIn("NetBelt.exe", notes)
        self.assertIn("updater.bat", notes)

    def test_the_source_of_the_copy_exists(self):
        """コピー元の updater.bat が実在すること。"""
        self.assertTrue(os.path.exists("updater.bat"),
                        "リポジトリ直下に updater.bat が無い")


if __name__ == "__main__":
    unittest.main()
