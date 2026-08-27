"""設定の保存が途中で失敗しても、前の設定が残ることを検証する。

save_config() は config.json を直接 'w' で開いていた。open した瞬間に
ファイルは長さ 0 に切り詰められるので、そこから json.dump が終わるまでの
間に落ちると（強制終了・シャットダウン・ディスク満杯・ウイルス対策の
ロック）、残るのは途中までの壊れた JSON か空ファイルになる。

失われるのは全グループ・全機器・暗号化済みパスワードで、復旧経路
_backup_corrupted_config() は次回起動時に「既に壊れた後のファイル」を
複製するだけなので、バックアップにも残骸しか入らない。

書き込みの窓は機器を編集したときだけでなく、アプリを閉じるたびにも開く
（closeEvent -> _save_layout -> save_config）。

同じディレクトリに一時ファイルを書いてから os.replace で差し替えれば、
差し替えは不可分になり、失敗しても前の config.json がそのまま残る。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, "src")
from core.config_manager import ConfigManager


class ConfigAtomicSaveTest(unittest.TestCase):
    def _saved_config(self):
        """機器を1台入れて保存済みの ConfigManager と、そのパスを返す。"""
        d = tempfile.mkdtemp(prefix="netbelt-atomic-")
        path = os.path.join(d, "config.json")
        cm = ConfigManager(config_path=path)
        cm.add_group("拠点A")
        cm.add_device("拠点A", {
            "name": "rtr1", "host": "192.0.2.1", "port": 22,
            "protocol": "ssh", "username": "admin", "password": "secret",
        })
        cm.save_config()
        return cm, path

    @staticmethod
    def _read(path):
        with io.open(path, "rb") as f:
            return f.read()

    def test_a_failure_during_save_leaves_the_previous_config_intact(self):
        """書き込みが失敗したら、前の config.json が1バイトも変わらないこと。"""
        cm, path = self._saved_config()
        before = self._read(path)

        with mock.patch("core.config_manager.json.dump",
                        side_effect=OSError("ディスクがいっぱいです")):
            cm.save_config()

        self.assertEqual(self._read(path), before,
                         "保存に失敗したのに前の設定が壊れている")

    def test_a_partial_write_does_not_reach_the_config_file(self):
        """途中まで書けた状態でも、それが config.json にならないこと。"""
        cm, path = self._saved_config()
        before = self._read(path)

        def write_some_then_fail(obj, fp, **kwargs):
            fp.write('{"groups": [{"name": "拠')
            raise OSError("途中で落ちた")

        with mock.patch("core.config_manager.json.dump",
                        side_effect=write_some_then_fail):
            cm.save_config()

        self.assertEqual(self._read(path), before,
                         "書きかけの JSON が config.json に残っている")

    def test_the_config_is_still_loadable_after_a_failed_save(self):
        """失敗後に開き直しても、登録した機器が残っていること。"""
        cm, path = self._saved_config()

        with mock.patch("core.config_manager.json.dump",
                        side_effect=OSError("ディスクがいっぱいです")):
            cm.save_config()

        reopened = ConfigManager(config_path=path)
        self.assertIsNone(reopened.load_error,
                          "失敗した保存のせいで設定が読めなくなっている")
        names = [d["name"] for g in reopened.get_groups()
                 for d in g.get("devices", [])]
        self.assertIn("rtr1", names, "登録した機器が消えている")

    def test_a_failed_save_reports_false(self):
        """失敗を False で返すこと（呼び出し側が警告を出せなくなる）。"""
        cm, _ = self._saved_config()
        with mock.patch("core.config_manager.json.dump",
                        side_effect=OSError("ディスクがいっぱいです")):
            self.assertFalse(cm.save_config(), "失敗が成功として返っている")

    def test_a_failed_save_does_not_leave_a_temp_file_behind(self):
        """失敗しても書きかけの一時ファイルを置き去りにしないこと。"""
        cm, path = self._saved_config()
        with mock.patch("core.config_manager.json.dump",
                        side_effect=OSError("ディスクがいっぱいです")):
            cm.save_config()

        leftovers = [n for n in os.listdir(os.path.dirname(path))
                     if n != "config.json"]
        self.assertEqual(leftovers, [], "一時ファイルが残っている: %s" % leftovers)

    def test_a_normal_save_still_writes_the_config(self):
        """通常の保存はこれまでどおり書けること。"""
        cm, path = self._saved_config()
        cm.add_group("拠点B")
        self.assertTrue(cm.save_config())

        written = json.loads(io.open(path, encoding="utf-8").read())
        self.assertIn("拠点B", [g["name"] for g in written["groups"]])

    def test_a_normal_save_leaves_no_temp_file(self):
        """成功したときも一時ファイルを残さないこと。"""
        cm, path = self._saved_config()
        cm.save_config()

        leftovers = [n for n in os.listdir(os.path.dirname(path))
                     if n != "config.json"]
        self.assertEqual(leftovers, [], "一時ファイルが残っている: %s" % leftovers)


if __name__ == "__main__":
    unittest.main()
