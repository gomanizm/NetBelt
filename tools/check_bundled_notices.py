#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ビルドした NetBelt.exe の中身を、THIRD-PARTY-NOTICES.txt の一覧と突き合わせる。

通知は tools/gen_third_party_notices.py がビルド環境のパッケージから作り、
exe の中身は PyInstaller が import を辿って決める。2 つは別々に決まるので、
ずれても気づけない。1.3.3 までの exe には invoke・setuptools・packaging が
入っていたのに、通知には載っていなかった。PyInstaller やフックの更新、
固定していない依存（urllib3 など）の版でも、同梱物は変わりうる。

exe の PYZ と CArchive から最上位の名前とファイルを集め、ビルド環境の
importlib.metadata（packages_distributions と RECORD）で distribution を
割り出し、通知の一覧に無いものがあれば止める。標準ライブラリと NetBelt
自身は除く。vendored の部品（setuptools/_vendor/... など）は、ビルド環境に
ある部品のライセンス本文が親の節に載っているかも確かめる。パッケージの中に
置かれた表示（NOTICE 系のファイル）も、置かれたディレクトリの中身が exe に
入っていれば、その節に載っているかを確かめる。Python パッケージ
でない DLL（python311.dll・OpenSSL・VC ランタイムなど）と PyInstaller の
起動部品は照合の対象外で、参考として表示するだけ。

使い方（exe をビルドした環境で、リポジトリの直下から）:
    python tools/check_bundled_notices.py [--exe dist/NetBelt.exe]
        [--notices THIRD-PARTY-NOTICES.txt] [--src src]

終了コード: 0 = 一致、1 = 通知に無いものがある、
    2 = 照合できない（入力を読めない、照合の途中で失敗した）
"""
import argparse
import importlib.metadata as md
import os
import re
import sys

SEP = "=" * 78
VENDOR_DIRS = {"_vendor", "vendor", "vendored"}
LICENSE_PREFIXES = ("LICENSE", "COPYING", "NOTICE")
# 表示のファイル名。tools/gen_third_party_notices.py の NOTICE_NAME と同じ
NOTICE_NAME = re.compile(r"NOTICES?(\.(txt|md|rst))?", re.IGNORECASE)
# PyInstaller のブートストラップ・ランタイムフックとその補助モジュール
PYINSTALLER_PREFIXES = ("pyimod", "pyiboot", "pyi_rth_", "_pyi_rth_")
STDLIB_ARCHIVES = {"base_library.zip"}  # PyInstaller が標準ライブラリを入れる ZIP


def normalize(name):
    """distribution 名を比べる形にする（PEP 503）"""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_notices(text):
    """通知の一覧に載った名前の集合と、名前ごとの節の本文を返す"""
    lines = text.splitlines()
    if "一覧" not in lines:
        raise ValueError("通知に「一覧」の見出しがありません")
    listed = set()
    for line in lines[lines.index("一覧") + 2:]:
        if not line.strip():
            break
        listed.add(normalize(line.split()[0]))
    if not listed:
        raise ValueError("通知の一覧が空です")
    starts = [i for i in range(len(lines) - 2)
              if lines[i] == SEP and lines[i + 2].startswith("ライセンス: ")]
    sections = {}
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        sections[normalize(lines[i + 1].split()[0])] = "\n".join(lines[i:end])
    return listed, sections


def dist_info_name(segment):
    """'name-1.0.dist-info' の name。dist-info でなければ None"""
    m = re.match(r"(.+?)-[^-]+\.dist-info$", segment)
    return m.group(1) if m else None


def vendored_parts(parts):
    """パスの中にある vendored の部品の場所（親…, vendor ディレクトリ, 部品名）"""
    found = []
    for i in range(1, len(parts) - 1):
        if parts[i] in VENDOR_DIRS:
            name = dist_info_name(parts[i + 1]) or parts[i + 1]
            found.append(tuple(parts[:i + 1]) + (name,))
    return found


def component_owns(comp, path):
    """path（/ 区切り）が、部品 comp のディレクトリか、部品の dist-info の中にあるか"""
    prefix = "/".join(comp[:-1]) + "/"
    if not path.startswith(prefix):
        return False
    head, _, rest = path[len(prefix):].partition("/")
    if not rest:
        return False
    if head == comp[-1]:
        return True
    name = dist_info_name(head)
    if not name:
        return False
    key, want = name.lower().replace("-", "_"), comp[-1].lower().replace("-", "_")
    return key == want or key.startswith(want + ".")  # jaraco.text は jaraco の部品


class Bundle:
    """exe の中身を、照合に使う形に分けたもの"""

    def __init__(self, entries, modules):
        self.tops = set()         # 最上位のモジュール・パッケージ名
        self.files = set()        # CArchive のファイル（/ 区切り）
        self.dist_infos = set()   # 最上位の *.dist-info から分かる distribution
        self.loose_dlls = set()   # 最上位に置かれた DLL
        self.pyinstaller = set()  # PyInstaller の起動部品
        self.modules = set()      # PYZ のモジュール（/ 区切り）
        self.vendored = set()
        for typecode, name in entries:
            parts = [p for p in re.split(r"[\\/]", name) if p]
            if typecode in ("o", "d", "l") or not parts:
                continue  # 実行時の設定・依存・スプラッシュ
            if typecode in ("s", "m", "M"):  # 起動スクリプトとブートストラップ
                self._top(parts[0])
                continue
            self.files.add("/".join(parts))
            if len(parts) == 1:
                lower = parts[0].lower()
                if lower.endswith(".dll"):
                    self.loose_dlls.add(parts[0])
                elif lower not in STDLIB_ARCHIVES:
                    self._top(parts[0].split(".")[0])  # 拡張モジュール（.pyd）
                continue
            if dist_info_name(parts[0]):
                self.dist_infos.add(dist_info_name(parts[0]))
            else:
                self._top(parts[0])
            self.vendored.update(vendored_parts(parts))
        for name in modules:
            parts = name.split(".")
            self.modules.add("/".join(parts))
            self._top(parts[0])
            self.vendored.update(vendored_parts(parts))

    def _top(self, name):
        if name.startswith(PYINSTALLER_PREFIXES):
            self.pyinstaller.add(name)
        else:
            self.tops.add(name)


def check(bundle, listed, sections, own, stdlib, top_dists, file_owner, licences,
          notice_files=lambda dist: []):
    """照合する。

    Args:
        own / stdlib: NetBelt 自身・標準ライブラリの最上位の名前
        top_dists: 最上位の名前 → distribution 名（packages_distributions の形）
        file_owner: ファイル（/ 区切り・小文字）→ そのファイルを持つ distribution 名
        licences: (distribution 名, 部品) → その部品のライセンス本文 [(パス, 本文)]
        notice_files: distribution 名 → その RECORD にある表示（NOTICE 系）[(パス, 本文)]

    Returns:
        (通知に無い distribution → 根拠の集合, 割り出せない名前,
         本文が親の節に無い部品 [(部品, 理由)], 照合の対象外の DLL)
    """
    required = {}
    for path in bundle.files:
        if file_owner.get(path.lower()):
            required.setdefault(normalize(file_owner[path.lower()]), set()).add(path)
    # 名前は Bundle と揃える（最上位のファイルは拡張モジュールとして '.' の前）
    owned_tops = {p.split("/")[0] if "/" in p else p.split(".")[0]
                  for p in bundle.files if file_owner.get(p.lower())}
    unknown = set()
    for top in bundle.tops - own - stdlib:
        for dist in top_dists.get(top, []):
            required.setdefault(normalize(dist), set()).add(top)
        if not top_dists.get(top) and top not in owned_tops:
            unknown.add(top)
    for name in bundle.dist_infos:
        required.setdefault(normalize(name), set()).add(name + ".dist-info")
    missing = {d: why for d, why in required.items() if d not in listed}

    uncovered = []
    for comp in sorted(bundle.vendored):
        parents = [d for d in top_dists.get(comp[0], []) if normalize(d) in listed]
        if comp[0] in own or not parents:
            continue  # 親が一覧に無ければ、missing で報告している
        section = " ".join("\n".join(sections.get(normalize(d), "") for d in parents).split())
        texts = [t for d in parents for t in licences(d, comp)]
        if not texts:
            uncovered.append(("/".join(comp), "ビルド環境に部品のライセンス本文が見つかりません"))
        for path, body in texts:
            if " ".join(body.split()) not in section:
                uncovered.append(("/".join(comp), "%s の本文が %s の節にありません"
                                  % (path, " / ".join(parents))))
    # 表示は、置かれたディレクトリの中身が exe に入っていれば要る。
    # dist-info の中のもの（licenses/ の下を含む）は distribution 全体についての表示
    places = bundle.files | bundle.modules
    for dist in sorted(set(required) & listed):
        for path, body in notice_files(dist):
            parts = path.split("/")
            scope = "" if parts[0].endswith(".dist-info") else "/".join(parts[:-1])
            if scope and not any((p + "/").startswith(scope + "/") for p in places):
                continue
            if " ".join(body.split()) not in " ".join(sections.get(dist, "").split()):
                uncovered.append((scope or dist, "%s の本文が %s の節にありません" % (path, dist)))
    natives = sorted(d for d in bundle.loose_dlls if not file_owner.get(d.lower()))
    return missing, unknown, uncovered, natives


def read_exe(path):
    """exe の CArchive の項目 [(型, 名前)] と、PYZ のモジュール名の並びを返す"""
    from PyInstaller.archive.readers import CArchiveReader
    pkg = CArchiveReader(path)
    entries, modules = [], []
    for name, entry in pkg.toc.items():
        if entry[-1] == "z":
            modules.extend(pkg.open_embedded_archive(name).toc)
        else:
            entries.append((entry[-1], name))
    return entries, modules


def own_names(src):
    """NetBelt 自身の最上位の名前（src の直下の .py とディレクトリ）"""
    return {os.path.splitext(n)[0] for n in os.listdir(src) if n != "__pycache__"
            and (n.endswith(".py") or os.path.isdir(os.path.join(src, n)))}


def installed_licences(dist_name, comp):
    """ビルド環境の dist_name が持つ、部品 comp のライセンス本文 [(パス, 本文)]"""
    out = []
    for f in md.distribution(dist_name).files or []:
        if f.name.upper().startswith(LICENSE_PREFIXES) and component_owns(comp, f.as_posix()):
            with open(f.locate(), encoding="utf-8", errors="replace") as fh:
                out.append((f.as_posix(), fh.read()))
    return out


def installed_notices(dist_name):
    """ビルド環境の dist_name の RECORD にある表示（NOTICE 系）[(パス, 本文)]"""
    out = []
    for f in md.distribution(dist_name).files or []:
        if NOTICE_NAME.fullmatch(f.name):
            with open(f.locate(), encoding="utf-8", errors="replace") as fh:
                out.append((f.as_posix(), fh.read()))
    return out


def main(argv=None):
    # ランナーは英語ロケール（cp1252）。--help の説明も日本語なので、引数の解析より前に
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description="exe の同梱物と通知の一覧を突き合わせる")
    parser.add_argument("--exe", default=os.path.join(root, "dist", "NetBelt.exe"))
    parser.add_argument("--notices", default=os.path.join(root, "THIRD-PARTY-NOTICES.txt"))
    parser.add_argument("--src", default=os.path.join(root, "src"))
    args = parser.parse_args(argv)
    try:
        with open(args.notices, encoding="utf-8") as f:
            listed, sections = parse_notices(f.read())
        own = own_names(args.src)
        bundle = Bundle(*read_exe(args.exe))
        file_owner = {}
        for dist in md.distributions():
            for f in dist.files or []:
                file_owner.setdefault(f.as_posix().lower(), dist.metadata.get("Name"))
        missing, unknown, uncovered, natives = check(
            bundle, listed, sections, own, set(sys.stdlib_module_names),
            md.packages_distributions(), file_owner, installed_licences, installed_notices)
    except Exception as e:  # 1（通知に無いものがある）と混ざらないよう、途中の失敗も 2
        print("照合できません: %s: %s" % (type(e).__name__, e))
        return 2

    print("exe: %s" % args.exe)
    print("通知: %s（一覧 %d 件）" % (args.notices, len(listed)))
    for name, why in sorted(missing.items()):
        print("NG 通知の一覧に無い: %s（%s）" % (name, ", ".join(sorted(why)[:3])))
    for name in sorted(unknown):
        print("NG distribution を割り出せない: %s" % name)
    for comp, reason in uncovered:
        print("NG 部品の本文が無い: %s: %s" % (comp, reason))
    print("参考（照合の対象外）Python パッケージでない DLL %d 件: %s"
          % (len(natives), ", ".join(natives)))
    print("参考（照合の対象外）PyInstaller の起動部品 %d 件: %s"
          % (len(bundle.pyinstaller), ", ".join(sorted(bundle.pyinstaller))))
    if missing or unknown or uncovered:
        print("結果: NG。tools/gen_third_party_notices.py と NetBelt.spec を見直してください")
        return 1
    print("結果: OK。exe に入っている distribution はすべて通知に載っています")
    return 0


if __name__ == "__main__":
    sys.exit(main())
