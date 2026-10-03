# -*- coding: utf-8 -*-
"""exe の同梱物と THIRD-PARTY-NOTICES.txt の一覧を突き合わせる判定の検証。

通知はビルド環境のパッケージから作り、exe の中身は PyInstaller が import を
辿って決める。別々に決まるので、1.3.3 までは invoke・setuptools・packaging が
exe に入っているのに通知に無いことに誰も気づかなかった。
tools/check_bundled_notices.py はビルドした exe を読んで照合する。ここでは
exe を作らず、判定の部分を合成したデータで確かめる。
"""
import importlib.util
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO_ROOT, "tools", "check_bundled_notices.py")
SEP = "=" * 78


def _load():
    spec = importlib.util.spec_from_file_location("check_bundled_notices", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _notices(sections):
    """生成スクリプトと同じ形の通知。sections は [(名前, 版, 本文)]"""
    out = ["NetBelt サードパーティ ライセンス表示", SEP, "", "-" * 78, "一覧", "-" * 78]
    out += ["  %-28s %-12s MIT" % (name, ver) for name, ver, _ in sections]
    out.append("")
    for name, ver, body in sections:
        out += [SEP, "%s %s" % (name, ver), "ライセンス: MIT", SEP, "", body, ""]
    return "\n".join(out) + "\n"


class ParseNoticesTest(unittest.TestCase):

    def setUp(self):
        self.tool = _load()

    def test_names_are_read_from_the_list_and_normalized(self):
        listed, sections = self.tool.parse_notices(_notices(
            [("PyQt6_sip", "13.10.3", "SIP-TEXT"), ("charset-normalizer", "3.5.2", "CN-TEXT")]))
        self.assertEqual(listed, {"pyqt6-sip", "charset-normalizer"})
        self.assertIn("SIP-TEXT", sections["pyqt6-sip"])
        self.assertNotIn("CN-TEXT", sections["pyqt6-sip"])
        self.assertIn("CN-TEXT", sections["charset-normalizer"])

    def test_text_without_the_list_cannot_be_checked(self):
        with self.assertRaises(ValueError):
            self.tool.parse_notices("NetBelt サードパーティ ライセンス表示\n")


class VendoredPartsTest(unittest.TestCase):

    def setUp(self):
        self.tool = _load()

    def test_parts_under_a_vendor_directory(self):
        f = self.tool.vendored_parts
        self.assertEqual(f("setuptools._vendor.jaraco.text".split(".")),
                         [("setuptools", "_vendor", "jaraco")])
        self.assertEqual(
            f(["setuptools", "_vendor", "importlib_metadata-8.0.0.dist-info", "LICENSE"]),
            [("setuptools", "_vendor", "importlib_metadata")])
        # 取り込んだ部品の中で、さらに取り込んだもの
        self.assertEqual(
            f("setuptools._vendor.wheel.vendored.packaging.tags".split(".")),
            [("setuptools", "_vendor", "wheel"),
             ("setuptools", "_vendor", "wheel", "vendored", "packaging")])
        self.assertEqual(f("setuptools._vendor".split(".")), [])
        self.assertEqual(f("paramiko.config".split(".")), [])

    def test_licence_files_that_belong_to_a_part(self):
        owns = self.tool.component_owns
        jaraco = ("setuptools", "_vendor", "jaraco")
        self.assertTrue(owns(jaraco, "setuptools/_vendor/jaraco.text-3.12.1.dist-info/LICENSE"))
        self.assertTrue(owns(jaraco, "setuptools/_vendor/jaraco/LICENSE"))
        self.assertFalse(owns(jaraco, "setuptools/_vendor/jaracoish-1.0.dist-info/LICENSE"))
        self.assertFalse(owns(jaraco, "setuptools/_vendor/zipp-3.19.2.dist-info/LICENSE"))
        self.assertFalse(owns(jaraco, "pip/_vendor/jaraco/LICENSE"))
        wheel = ("setuptools", "_vendor", "wheel")
        self.assertTrue(owns(wheel, "setuptools/_vendor/wheel/vendored/packaging/LICENSE"))
        self.assertTrue(owns(("setuptools", "_vendor", "typing_extensions"),
                             "setuptools/_vendor/typing_extensions-4.12.2.dist-info/LICENSE"))


class BundleTest(unittest.TestCase):

    def test_entries_are_sorted_into_what_the_check_needs(self):
        tool = _load()
        bundle = tool.Bundle(
            [("s", "pyiboot01_bootstrap"), ("s", "main"), ("m", "struct"),
             ("m", "pyimod01_archive"), ("b", "python311.dll"), ("b", "base_library.zip"),
             ("b", "_cffi_backend.cp311-win_amd64.pyd"), ("b", "PyQt6\\Qt6\\bin\\Qt6Core.dll"),
             ("b", "cryptography-46.0.3.dist-info\\METADATA"),
             ("b", "setuptools\\_vendor\\jaraco\\text\\Lorem ipsum.txt"), ("o", "pyi-windows")],
            ["paramiko.config", "_pyi_rth_utils", "setuptools._vendor.zipp"])
        self.assertEqual(bundle.tops, {"main", "struct", "_cffi_backend", "PyQt6",
                                       "setuptools", "paramiko"})
        self.assertEqual(bundle.pyinstaller,
                         {"pyiboot01_bootstrap", "pyimod01_archive", "_pyi_rth_utils"})
        self.assertEqual(bundle.loose_dlls, {"python311.dll"})
        self.assertEqual(bundle.dist_infos, {"cryptography"})
        self.assertIn("PyQt6/Qt6/bin/Qt6Core.dll", bundle.files)
        self.assertEqual(bundle.vendored, {("setuptools", "_vendor", "jaraco"),
                                           ("setuptools", "_vendor", "zipp")})


class CheckTest(unittest.TestCase):
    """check() の判定"""

    TOP_DISTS = {"paramiko": ["paramiko"], "invoke": ["invoke"], "setuptools": ["setuptools"],
                 "PyQt6": ["PyQt6", "PyQt6_sip"], "_cffi_backend": ["cffi"]}
    VENDORED_LICENCE = "Permission is hereby granted, free of charge, to any person"

    def setUp(self):
        self.tool = _load()

    def _check(self, entries, modules, sections, file_owner=None, licences=None):
        bundle = self.tool.Bundle(entries, modules)
        listed, parsed = self.tool.parse_notices(_notices(sections))
        return self.tool.check(
            bundle, listed, parsed, own={"main", "core", "resources"},
            stdlib={"struct", "os", "distutils"}, top_dists=self.TOP_DISTS,
            file_owner=file_owner or {}, licences=licences or (lambda dist, comp: []))

    def test_a_bundled_package_missing_from_the_notices_is_reported(self):
        missing, unknown, uncovered, natives = self._check(
            [("s", "main")], ["paramiko.config", "invoke.runners", "os.path"],
            [("paramiko", "4.0.0", "LGPL")])
        self.assertEqual(set(missing), {"invoke"})
        self.assertEqual(missing["invoke"], {"invoke"})
        self.assertEqual((unknown, uncovered), (set(), []))

    def test_everything_listed_passes(self):
        result = self._check(
            [("s", "main"), ("m", "struct"), ("s", "pyi_rth_inspect"),
             ("b", "resources\\default_config.json"), ("b", "python311.dll"),
             ("b", "_cffi_backend.cp311-win_amd64.pyd")],
            ["paramiko.client", "core.ssh_connection", "PyQt6.QtCore", "distutils.util"],
            [("cffi", "2.0.0", "MIT"), ("paramiko", "4.0.0", "LGPL"),
             ("PyQt6", "6.10.1", "GPL"), ("PyQt6_sip", "13.10.3", "BSD")])
        self.assertEqual(result, ({}, set(), [], ["python311.dll"]))

    def test_a_name_without_a_distribution_is_reported(self):
        missing, unknown, _, _ = self._check(
            [], ["mystery_pkg.mod"], [("paramiko", "4.0.0", "LGPL")])
        self.assertEqual((missing, unknown), ({}, {"mystery_pkg"}))

    def test_files_are_traced_to_their_distribution_through_record(self):
        # PyQt6-Qt6 は .py を持たず、packages_distributions では PyQt6 に結び付かない
        owner = {"pyqt6/qt6/bin/qt6core.dll": "PyQt6-Qt6", "libssl-3.dll": None}
        missing, _, _, natives = self._check(
            [("b", "PyQt6\\Qt6\\bin\\Qt6Core.dll"), ("b", "libssl-3.dll")], [],
            [("PyQt6", "6.10.1", "GPL"), ("PyQt6_sip", "13.10.3", "BSD")], file_owner=owner)
        self.assertEqual(set(missing), {"pyqt6-qt6"})
        self.assertEqual(natives, ["libssl-3.dll"])

    def test_a_top_level_extension_traced_through_record_is_not_unknown(self):
        # top_level.txt の無い wheel が最上位に置いた拡張モジュール。名前が
        # packages_distributions に無くても、RECORD でファイルの持ち主が分かる
        missing, unknown, _, _ = self._check(
            [("b", "fooext.cp311-win_amd64.pyd")], [], [("fooext", "1.0", "MIT")],
            file_owner={"fooext.cp311-win_amd64.pyd": "fooext"})
        self.assertEqual((missing, unknown), ({}, set()))

    def test_a_file_in_a_directory_traced_through_record_is_not_unknown(self):
        # top_level.txt がそのディレクトリを挙げない wheel が、ディレクトリの下に
        # 置いた DLL。名前はパスの最初の要素で、RECORD でファイルの持ち主が分かる
        missing, unknown, _, _ = self._check(
            [("b", "foodata\\x.dll")], [], [("foodata", "1.0", "MIT")],
            file_owner={"foodata/x.dll": "foodata"})
        self.assertEqual((missing, unknown), ({}, set()))

    def test_a_bundled_dist_info_names_its_distribution(self):
        missing, _, _, _ = self._check(
            [("b", "cryptography-46.0.3.dist-info\\METADATA")], [],
            [("paramiko", "4.0.0", "LGPL")])
        self.assertEqual(set(missing), {"cryptography"})

    def test_a_vendored_part_needs_its_licence_in_the_parent_section(self):
        def licences(dist, comp):
            self.assertEqual((dist, comp), ("setuptools", ("setuptools", "_vendor", "zipp")))
            return [("setuptools/_vendor/zipp-3.19.2.dist-info/LICENSE",
                     self.VENDORED_LICENCE + "\nof this software\n")]
        modules = ["setuptools.dist", "setuptools._vendor.zipp"]
        ok = self._check([], modules, [("setuptools", "80.9.0", "SETUPTOOLS-OWN\n\n"
                                        + self.VENDORED_LICENCE + " of this\nsoftware")],
                         licences=licences)
        self.assertEqual(ok[2], [])
        ng = self._check([], modules, [("setuptools", "80.9.0", "SETUPTOOLS-OWN")],
                         licences=licences)
        self.assertEqual([c for c, _ in ng[2]], ["setuptools/_vendor/zipp"])
        self.assertEqual(ng[0], {})

    def test_a_vendored_part_without_any_licence_file_is_reported(self):
        _, _, uncovered, _ = self._check(
            [], ["setuptools._vendor.zipp"], [("setuptools", "80.9.0", "OWN")])
        self.assertEqual([c for c, _ in uncovered], ["setuptools/_vendor/zipp"])

    def test_vendored_parts_of_an_unlisted_parent_are_left_to_the_missing_report(self):
        missing, _, uncovered, _ = self._check(
            [], ["invoke.vendor.yaml.loader"], [("paramiko", "4.0.0", "LGPL")])
        self.assertEqual((set(missing), uncovered), ({"invoke"}, []))


class NoticeFilesCheckTest(unittest.TestCase):
    """パッケージの中に置かれた表示（NOTICE 系）の照合。

    exe に入る setuptools.config._validate_pyproject は fastjsonschema と
    validate-pyproject に由来するコードで、表示は setuptools/config/NOTICE などにある。
    vendor の名前の下に無いので、部品の照合では見つからなかった。
    """

    NOTICE = "The following files include code from opensource projects"
    FILES = {
        "setuptools": [("setuptools/config/NOTICE", NOTICE + "\n(either as copies)\n")],
        "requests": [("requests-2.34.2.dist-info/licenses/NOTICE",
                      "Requests\nCopyright 2019 Kenneth Reitz\n")],
    }

    def _check(self, modules, sections, entries=()):
        tool = _load()
        listed, parsed = tool.parse_notices(_notices(sections))
        return tool.check(
            tool.Bundle(list(entries), modules), listed, parsed, own=set(), stdlib=set(),
            top_dists={"setuptools": ["setuptools"], "requests": ["requests"]},
            file_owner={}, licences=lambda dist, comp: [],
            notice_files=lambda dist: self.FILES.get(dist, []))

    def test_a_bundled_directory_needs_its_notice_in_the_section(self):
        modules = ["setuptools.dist", "setuptools.config._validate_pyproject.formats"]
        missing, unknown, uncovered, _ = self._check(modules, [("setuptools", "80.9.0", "OWN")])
        self.assertEqual((missing, unknown), ({}, set()))
        self.assertEqual([c for c, _ in uncovered], ["setuptools/config"])
        self.assertIn("setuptools/config/NOTICE の本文が setuptools の節にありません",
                      uncovered[0][1])
        ok = self._check(modules, [("setuptools", "80.9.0",
                                    "OWN\n\n" + self.NOTICE + "\n(either as\ncopies)")])
        self.assertEqual(ok[2], [])

    def test_a_bundled_file_in_the_directory_also_needs_it(self):
        _, _, uncovered, _ = self._check(
            ["setuptools.dist"], [("setuptools", "80.9.0", "OWN")],
            entries=[("b", "setuptools\\config\\distutils.schema.json")])
        self.assertEqual([c for c, _ in uncovered], ["setuptools/config"])

    def test_a_notice_for_a_directory_left_out_of_the_exe_is_not_needed(self):
        # setuptools.configuration は setuptools/config の下ではない
        _, _, uncovered, _ = self._check(["setuptools.dist", "setuptools.configuration"],
                                         [("setuptools", "80.9.0", "OWN")])
        self.assertEqual(uncovered, [])

    def test_a_notice_in_the_dist_info_covers_the_whole_distribution(self):
        _, _, uncovered, _ = self._check(["requests.api"], [("requests", "2.34.2", "OWN")])
        self.assertEqual([c for c, _ in uncovered], ["requests"])
        ok = self._check(["requests.api"], [("requests", "2.34.2",
                                             "OWN\nRequests\nCopyright 2019 Kenneth Reitz")])
        self.assertEqual(ok[2], [])

    def test_notices_of_a_listed_distribution_outside_the_exe_are_not_needed(self):
        # pip のように一覧にあっても exe に入らないものは、照合しない
        _, _, uncovered, _ = self._check(["setuptools.dist"], [("requests", "2.34.2", "OWN"),
                                                               ("setuptools", "80.9.0", "OWN")])
        self.assertEqual(uncovered, [])


if __name__ == "__main__":
    unittest.main()
