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
import sys

# メタデータの記載が実物と食い違うもの。いずれも配布物の本文を読んで確認した。
VERIFIED = {
    # dist-info は "LGPL-2.1" だが、LICENSE 本文と各ソースヘッダに
    # "either version 2.1 of the License, or (at your option) any later version"
    # とあるため -or-later。GPL-3.0 との互換性の判断に効くので明記する。
    "paramiko": "LGPL-2.1-or-later",
    # METADATA にライセンス欄が無いが、LICENSE 本文と各ソースヘッダが MIT。
    "pyftpdlib": "MIT",
}

# 実行時には同梱されない、開発・ビルド時のみ使うもの
BUILD_ONLY = {
    "pytest", "pluggy", "iniconfig", "colorama", "pygments",
    "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile",
    "setuptools", "invoke", "packaging",
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


def main():
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
        if not name or name.lower() in BUILD_ONLY:
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
        out.append("")

    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
