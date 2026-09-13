"""updater.bat が、TEMP の作業場所を他の実行と共有しないことを検証する。

cmd.exe の %RANDOM% は、プロセス開始時の時計で種付けされる。同じ瞬間に
始まった cmd.exe は同じ数列を引くので、`%TEMP%\\NetBeltUpdate_%RANDOM%`
も `%TEMP%\\NetBeltUpdater_%RANDOM%.bat` も、同時に走る更新どうしで
同じ名前になる。実測では、同時に起動した 4 プロセスが 4 つとも
`NetBeltUpdate_9141` を選んだ。

そうなると 2 通りの壊れ方をする。

  1. 展開先がぶつかる。後から来たほうの mkdir は黙って失敗し、
     `if not exist` は「ある」ので素通りし、Expand-Archive が
     「The file ... already exists.」で落ちる。
  2. TEMP へ写したスクリプト自身がぶつかる。先に終わったほうの親が
     del を撃つと、まだ走っている側の cmd は次の行を読めなくなり、
     6 手順すべてと「更新が完了しました！」を表示したあとで
     "The batch file cannot be found." を出して exit 1 になる。
     更新自体は当たっているのに失敗として返るので、利用者は
     手で戻そうとする。

どちらも「負荷が高いときだけ落ちる」に見えるが、正体は名前の共有である。
%RANDOM% を何桁重ねても種が同じなら同じ数列なので、名前を長くしても
直らない。場所を排他的に確保することでしか直らない。

さらに、失敗した実行が残した `NetBeltUpdate_*` は TEMP に溜まり続ける。
溜まるほど、同時実行でなくても古い残骸と名前がぶつかるようになる。
"""
import io
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATER = os.path.join(REPO_ROOT, "updater.bat")

# 同時に走らせる本数。実測では 2 本でもぶつかるが、種は時計なので
# 取りこぼしを避けるために少し多めにする。
CONCURRENT = 4


class _Sandbox(object):
    """更新 1 回分の隔離された一式。実インストールには一切触れない。"""

    def __init__(self, base, name):
        self.dir = os.path.join(base, name)
        self.app_dir = os.path.join(self.dir, "app")
        os.makedirs(self.app_dir)
        self.updater = os.path.join(self.app_dir, "updater.bat")
        shutil.copyfile(UPDATER, self.updater)
        self.app_path = os.path.join(self.app_dir, "dummy_app.exe")
        # start が窓を残さないよう、引数なしで即終了する exe を借りる
        shutil.copyfile(
            os.path.join(os.environ["SystemRoot"], "System32",
                         "rundll32.exe"), self.app_path)
        io.open(os.path.join(self.app_dir, "NetBelt.exe"), "w",
                encoding="ascii", newline="").write("old")
        self.zip_path = os.path.join(self.dir, "update.zip")
        with zipfile.ZipFile(self.zip_path, "w") as z:
            z.writestr("NetBelt.exe", "new")
        self.out_path = os.path.join(self.dir, "out.txt")
        self._out = None
        self.proc = None

    def start(self, env=None):
        self._out = io.open(self.out_path, "wb")
        # 本番と同じ渡し方（1 本の文字列）にする
        command = '"{}" "{}" "{}"'.format(self.updater, self.zip_path,
                                          self.app_path)
        self.proc = subprocess.Popen(
            command, stdout=self._out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env)

    def wait(self):
        code = self.proc.wait(timeout=300)
        self._out.close()
        return code

    @property
    def output(self):
        return io.open(self.out_path, encoding="utf-8",
                       errors="replace").read()

    @property
    def installed(self):
        path = os.path.join(self.app_dir, "NetBelt.exe")
        if not os.path.exists(path):
            return None
        return io.open(path, encoding="ascii", errors="replace").read()


class UpdaterTempCollisionTest(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="netbelt_collide_")
        self.addCleanup(shutil.rmtree, self.base, True)

    def test_updaters_started_together_do_not_share_their_temp_workspace(self):
        """同時に始まった更新が、互いの作業場所を壊さないこと。"""
        boxes = [_Sandbox(self.base, "run%d" % i)
                 for i in range(CONCURRENT)]
        # 起動を固めることが肝。%RANDOM% の種は時計なので、
        # 同じ刻みの中で始まったプロセスは同じ数列を引く。
        for box in boxes:
            box.start()
        codes = [box.wait() for box in boxes]

        for i, box in enumerate(boxes):
            self.assertEqual(
                codes[i], 0,
                "%d 本目が失敗として返った:\n%s" % (i, box.output))
            self.assertEqual(
                box.installed, "new",
                "%d 本目に更新が当たっていない:\n%s" % (i, box.output))
            self.assertNotIn(
                "cannot be found", box.output,
                "%d 本目が、他の実行に作業ファイルを消された:\n%s"
                % (i, box.output))
            self.assertNotIn(
                "already exists", box.output,
                "%d 本目が、他の実行と展開先を共有した:\n%s"
                % (i, box.output))

    def test_a_finished_update_leaves_nothing_behind_in_temp(self):
        """終わった更新が TEMP に残骸を置かないこと。

        残骸は次の実行と名前がぶつかる種になる。実際 %TEMP% には
        NetBelt.exe 入りの NetBeltUpdate_* が何十個も溜まっていた。
        """
        private_temp = os.path.join(self.base, "temp")
        os.makedirs(private_temp)
        env = dict(os.environ)
        env["TEMP"] = private_temp
        env["TMP"] = private_temp

        box = _Sandbox(self.base, "solo")
        box.start(env=env)
        code = box.wait()

        self.assertEqual(code, 0, box.output)
        self.assertEqual(box.installed, "new", box.output)
        self.assertEqual(
            sorted(os.listdir(private_temp)), [],
            "TEMP に残骸を置いている:\n%s" % box.output)


if __name__ == "__main__":
    unittest.main()
