"""同じインストール先へ同時に更新をかけても、嘘をつかないことを検証する。

TEMP の作業場所は `md` で排他確保しているが（tests/test_updater_temp_collision.py）、
守られているのは TEMP 側だけである。新しい exe をいったん置く一時名は
インストール先の `!APP_DIR!NetBelt.exe.new` という固定名なので、
インストール先が同じ 2 つの更新はこの 1 つのファイルを共有してしまう。

実測した壊れ方（A のコピー完了 → B のコピー完了 → A の move）:

  1. 先に move した A は、自分が展開した exe ではなく B の exe を据える。
     それでも「更新が完了しました！」と表示して exit 0 を返す。
  2. あとから move する B は対象が既に無いので失敗し、
     「NetBelt.exe は旧版のままです」と案内して exit 1 を返す。
     実際に据わっているのは B の exe なので、この案内は事実と違う。

利用者から見ると、成功と言った側と実際に入った版が食い違い、
失敗と言われた側は手で戻そうとする。嘘の失敗報告は失敗より害が大きい。

同時に動く窓を作るために、ZIP には NetBelt.exe より後ろに並ぶ詰め物を
入れてある。xcopy が一時名の exe を書いてから move へ進むまでの間を
数秒引き延ばすためで、名前の共有そのものとは関係がない。

2026-09-20 追記: 一時名を実行ごとに変えても、インストール先そのものは
共有されたままで、どちらの版のファイルが残るかは混ざっていた。利用者の
決定（release-02）により、インストール先を変える前に目印フォルダで排他を
取り、取れなかった更新は『別の更新が進行中です』と伝えて中止するように
なった（tests/test_updater_shared_appdir_lock.py）。重なったときに後から
来たほうが exit 0 で終わることは、もう無い。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")

# 詰め物。xcopy が一時名の exe を書いたあと、move までの間を広げる。
# 実測ではこの大きさで 4/4 再現した。
PAD_COUNT = 1200
PAD_SIZE = 16384
# 2 本目を遅らせる秒数。A のコピー中に B のコピーが重なる位置。
OFFSET = 0.5


class _Run(object):
    """更新 1 回分。インストール先だけは他の実行と共有する。"""

    def __init__(self, base, app_dir, tag):
        self.tag = tag
        self.app_dir = app_dir
        self.dir = os.path.join(base, tag)
        os.makedirs(self.dir)
        self.app_path = os.path.join(app_dir, "NetBelt.exe")
        self.body = "EXE_FROM_RUN_%s" % tag
        self.zip_path = os.path.join(self.dir, "update.zip")
        pad = b"\0" * PAD_SIZE
        with zipfile.ZipFile(self.zip_path, "w",
                             zipfile.ZIP_STORED) as z:
            z.writestr("NetBelt.exe", self.body)
            for i in range(PAD_COUNT):
                z.writestr("z_%s_%04d.bin" % (tag, i), pad)
        self.out_path = os.path.join(self.dir, "out.txt")
        self._out = None
        self.proc = None

    def start(self):
        self._out = io.open(self.out_path, "wb")
        updater = os.path.join(self.app_dir, "updater.bat")
        command = '"{}" "{}" "{}"'.format(updater, self.zip_path,
                                          self.app_path)
        self.proc = subprocess.Popen(
            command, stdout=self._out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL)

    def wait(self):
        code = self.proc.wait(timeout=600)
        self._out.close()
        return code

    @property
    def output(self):
        return io.open(self.out_path, encoding="utf-8",
                       errors="replace").read()


@unittest.skipUnless(sys.platform == "win32", "cmd.exe が要る")
class UpdaterSharedAppDirTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_shared_")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.app_dir = os.path.join(self.base, "app")
        os.makedirs(self.app_dir)
        shutil.copyfile(UPDATER,
                        os.path.join(self.app_dir, "updater.bat"))
        io.open(os.path.join(self.app_dir, "NetBelt.exe"), "w",
                encoding="ascii", newline="").write("old")

    def _installed(self):
        path = os.path.join(self.app_dir, "NetBelt.exe")
        if not os.path.exists(path):
            return None
        return io.open(path, encoding="ascii", errors="replace").read()

    def test_two_updates_into_one_install_folder_do_not_share_a_staging_name(self):
        """インストール先が同じでも、一時名の exe を共有しないこと。

        排他が入ってからは、重なった側は『別の更新が進行中です』で中止する
        ようになった。ここで見るのは元どおり一時名の共有だけなので、失敗が
        その中止であることと、少なくとも一方が当たりきることを確かめる。
        """
        a = _Run(self.base, self.app_dir, "A")
        b = _Run(self.base, self.app_dir, "B")

        a.start()
        time.sleep(OFFSET)
        b.start()
        codes = {run.tag: run.wait() for run in (a, b)}

        for run in (a, b):
            self.assertNotIn(
                "差し替えられませんでした", run.output,
                "%s が、他の実行に一時名の exe を持って行かれた:\n%s"
                % (run.tag, run.output))
            if codes[run.tag] != 0:
                self.assertIn(
                    "別の更新が進行中です", run.output,
                    "%s が、重なり以外の理由で失敗した:\n%s"
                    % (run.tag, run.output))

        self.assertIn(
            0, codes.values(),
            "どちらの更新も当たらなかった:\n%s\n%s" % (a.output, b.output))
        self.assertIn(
            self._installed(), (a.body, b.body),
            "据わった exe がどちらの更新のものでもない:\n%s\n%s"
            % (a.output, b.output))

    def test_neither_update_leaves_a_staging_exe_behind(self):
        """どちらの実行も、一時名の exe を置き去りにしないこと。

        置き去りにされた一時名の exe は、次の更新が拾って動いている
        exe を上書きする種になる（tests/test_updater_script.py の
        test_a_leftover_staged_exe_is_not_installed_as_the_update）。
        """
        a = _Run(self.base, self.app_dir, "A")
        b = _Run(self.base, self.app_dir, "B")

        a.start()
        time.sleep(OFFSET)
        b.start()
        for run in (a, b):
            run.wait()

        left = sorted(n for n in os.listdir(self.app_dir)
                      if n.startswith("NetBelt.exe")
                      and n != "NetBelt.exe")
        self.assertEqual(left, [],
                         "一時名の exe が残っている: %s\n%s\n%s"
                         % (left, a.output, b.output))


if __name__ == "__main__":
    unittest.main()
