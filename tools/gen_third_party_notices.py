#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""配布物へ同梱する THIRD-PARTY-NOTICES.txt を生成する。

NetBelt.exe は PyInstaller で 1 ファイルに固めるため、依存パッケージの
バイナリがそのまま埋め込まれる。MIT / BSD / Apache-2.0 系は再配布時に
著作権表示とライセンス本文の添付を求めるため、それらを1つのファイルにまとめる。

使い方（ビルドに使う環境で実行すること）:
    python tools/gen_third_party_notices.py > THIRD-PARTY-NOTICES.txt

ライセンスは推測せず、インストール済みパッケージのメタデータと
同梱の LICENSE ファイルから実際に読み取る。
"""
import importlib.metadata as md
import re
import sys

# 表示（NOTICE 系）のファイル名。dist-info の外ではコードと同じ場所に置かれるので、
# 名前の形で絞る（notice.py のようなモジュールは読まない）
NOTICE_NAME = re.compile(r"NOTICES?(\.(txt|md|rst))?", re.IGNORECASE)

# メタデータの記載が実物と食い違うもの。いずれも配布物の本文を読んで確認した。
VERIFIED = {
    # dist-info は "LGPL-2.1" だが、LICENSE 本文と各ソースヘッダに
    # "either version 2.1 of the License, or (at your option) any later version"
    # とあるため -or-later。GPL-3.0 との互換性の判断に効くので明記する。
    "paramiko": "LGPL-2.1-or-later",
    # METADATA にライセンス欄が無いが、LICENSE 本文と各ソースヘッダが MIT。
    "pyftpdlib": "MIT",
    # 7.1.28 の METADATA にライセンス欄が無いが、同梱の LICENSE.rst は
    # 2 条項の BSD（BSD-2-Clause）。5.1.0 の METADATA も BSD-2-Clause だった。
    "pysnmp": "BSD-2-Clause",
    # classifier は Apache と BSD の 2 つで、最初の 1 つだけでは片方が落ちる。
    # LICENSE 本文は LICENSE.APACHE と LICENSE.BSD（2 条項）の二者択一（*either*）
    "packaging": "Apache-2.0 OR BSD-2-Clause",
}

# テストとビルドにだけ使い、exe には入らないもの。
# exe に入るものをここへ入れると、CI の tools/check_bundled_notices.py が止める
BUILD_ONLY = {
    "pytest", "pluggy", "iniconfig", "colorama", "pygments",
    "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile",
}

# 実行時の依存だが、NetBelt.spec の excludes で exe から外したもの。
# spec と揃っていることは tests/test_third_party_notices_bundle.py が確かめる
EXCLUDED_FROM_EXE = {
    "invoke",  # paramiko.config が try で import するだけで、NetBelt は使わない
}


def license_of(dist):
    meta = dist.metadata
    name = (meta.get("Name") or "").strip().lower()
    if name in VERIFIED:
        return VERIFIED[name]
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip()
    lic = meta.get("License")
    if lic and lic.strip() and "\n" not in lic.strip():
        return lic.strip()
    for c in meta.get_all("Classifier") or []:
        if c.startswith("License ::"):
            return c.split("::")[-1].strip()
    if lic and lic.strip():
        return lic.strip().splitlines()[0][:60]
    return "(メタデータに記載なし)"


def license_text(dist):
    """dist-info に同梱された LICENSE 系ファイルの本文を返す。

    RECORD 経由で拾えないことがあるため、dist-info ディレクトリを
    直接walkするフォールバックも持つ。
    """
    import os

    texts = []
    seen = set()
    for f in dist.files or []:
        if f.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
            try:
                body = dist.read_text(str(f))
            except Exception:
                body = None
            if body:
                texts.append((str(f), body))
                seen.add(f.name.upper())
    if texts:
        return texts

    # フォールバック: dist-info を直接読む
    base = getattr(dist, "_path", None)
    if base and os.path.isdir(str(base)):
        for root, _dirs, files in os.walk(str(base)):
            for fn in sorted(files):
                if not fn.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                    continue
                if fn.upper() in seen:
                    continue
                full = os.path.join(root, fn)
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        texts.append((os.path.relpath(full, os.path.dirname(str(base))),
                                      fh.read()))
                    seen.add(fn.upper())
                except Exception:
                    continue
    return texts


def vendored_license_text(dist):
    """パッケージが内部に取り込んだ部品（vendored）の LICENSE 系ファイルの本文を返す。

    setuptools は取り込んだパッケージの dist-info を setuptools/_vendor/ の下に
    そのまま持っており、exe にはその部品のコードが入る。RECORD に載った、
    自分のものではない dist-info の中のファイルを読む。
    """
    texts = []
    for f in dist.files or []:
        parts = f.parts
        if not parts or parts[0].endswith(".dist-info"):
            continue  # 自分の dist-info は license_text() が読む
        if not any(p.endswith(".dist-info") for p in parts[1:-1]):
            continue
        if not f.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
            continue
        try:
            with open(f.locate(), encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except Exception:
            continue
        if body:
            texts.append((f.as_posix(), body))
    return sorted(texts)


def notice_text(dist):
    """dist-info の外に置かれた表示（NOTICE 系のファイル）の本文を返す。

    取り込んだコードの出どころとライセンスを、そのディレクトリの NOTICE に書く
    パッケージがある（setuptools/config/_validate_pyproject は fastjsonschema と
    validate-pyproject に由来する）。RECORD に載ったものを実物から読む。
    dist-info の中のものは license_text() と vendored_license_text() が読む。
    """
    texts = []
    for f in dist.files or []:
        if any(p.endswith(".dist-info") for p in f.parts[:-1]):
            continue
        if not NOTICE_NAME.fullmatch(f.name):
            continue
        try:
            with open(f.locate(), encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except Exception:
            continue
        if body:
            texts.append((f.as_posix(), body))
    return sorted(texts)


def main():
    # 出力先のエンコーディングはロケール依存で、英語ロケールの Windows では
    # cp1252 になり日本語を書けない（GitHub Actions の windows-latest が該当）。
    # 生成物は UTF-8 と決まっているので、ロケールに関係なく明示する。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    out = []
    out.append("NetBelt サードパーティ ライセンス表示")
    out.append("=" * 78)
    out.append("")
    out.append("NetBelt の実行ファイルには、以下のパッケージが組み込まれています。")
    out.append("各パッケージの著作権は、それぞれの権利者に帰属します。")
    out.append("")
    out.append("NetBelt 本体のライセンスは GNU General Public License v3.0 です。")
    out.append("同梱の LICENSE ファイル、または https://www.gnu.org/licenses/gpl-3.0.html")
    out.append("を参照してください。")
    out.append("")

    dists = []
    for d in md.distributions():
        name = (d.metadata.get("Name") or "").strip()
        if not name or name.lower() in BUILD_ONLY | EXCLUDED_FROM_EXE:
            continue
        dists.append((name.lower(), name, d))
    dists.sort()

    out.append("-" * 78)
    out.append("一覧")
    out.append("-" * 78)
    for _, name, d in dists:
        out.append("  %-28s %-12s %s" % (name, d.version, license_of(d)))
    out.append("")

    for _, name, d in dists:
        out.append("=" * 78)
        out.append("%s %s" % (name, d.version))
        out.append("ライセンス: %s" % license_of(d))
        home = d.metadata.get("Home-page") or ""
        for key in ("Project-URL",):
            for v in d.metadata.get_all(key) or []:
                if not home and "," in v:
                    home = v.split(",", 1)[1].strip()
        if home:
            out.append("配布元: %s" % home)
        out.append("=" * 78)
        texts = license_text(d)
        if texts:
            for path, body in texts:
                out.append("")
                out.append("--- %s ---" % path)
                out.append(body.rstrip())
        else:
            out.append("")
            out.append("（このパッケージは配布物にライセンス本文を同梱していません。")
            out.append("  上記の配布元を参照してください）")
        notices = notice_text(d)
        if notices:
            out.append("")
            out.append("（以下は、このパッケージの中のディレクトリに置かれた表示（NOTICE）です）")
            for path, body in notices:
                out.append("")
                out.append("--- %s ---" % path)
                out.append(body.rstrip())
        vendored = vendored_license_text(d)
        if vendored:
            out.append("")
            out.append("（以下は、このパッケージが内部に取り込んでいる部品のライセンスです）")
            for path, body in vendored:
                out.append("")
                out.append("--- %s ---" % path)
                out.append(body.rstrip())
        out.append("")

    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
