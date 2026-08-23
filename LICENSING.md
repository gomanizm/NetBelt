# ライセンスと法的事項

NetBelt のライセンスと、配布にあたっての法的な事項をまとめています。
概要は [README](README.md#ライセンス) にあります。

*English version is at the [bottom of this page](#english).*

---

## 適用しているライセンス

**GNU General Public License v3.0 or later（GPL-3.0-or-later）**

全文は [LICENSE](LICENSE) を参照してください。

```
Copyright (C) 2026 NetBelt Contributors

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
```

## GPL-3.0 を採用している理由

GUI に **PyQt6** を使用しており、PyQt6 は GPL-3.0 と商用ライセンスのデュアルライセンスで
提供されています。本プロジェクトは商用ライセンスを取得していないため、GPL-3.0 の条件に従います。
したがってプロジェクト全体も GPL-3.0 となります。

## GPL の適用範囲

GPL が適用されるのは、**このリポジトリに含まれる NetBelt 自身のコード**です。
組み込んでいる第三者パッケージは、それぞれの権利者のライセンスに従います。

## 配布した実行ファイルに対応するソースコード

> GPL-3.0 第6条（オブジェクトコードの頒布）の要件を満たすための案内です。

配布している `NetBelt.exe` は PyInstaller で 1 ファイルにまとめたもので、
**PyQt6 (GPL-3.0)、Qt (LGPL-3.0)、paramiko (LGPL-2.1-or-later)、CPython** などが
そのまま組み込まれています。

**この実行ファイルに対応するソースコード**は、次の場所で入手できます。

- 各リリースに GitHub が自動添付する **Source code アーカイブ**
- 本リポジトリの**対応するタグ**（例: `v1.0.0`）

ビルドの再現には、同梱の以下が必要です。いずれもリポジトリに含まれています。

| 必要なもの | 内容 |
|---|---|
| `requirements.txt` | 依存パッケージ。**すべてバージョン固定済み** |
| `NetBelt.spec` | PyInstaller のビルド定義 |
| Python | **3.11**（リリースビルドに使用しているバージョン） |

```bash
pip install -r requirements.txt
python -m PyInstaller --clean NetBelt.spec
```

組み込まれた第三者コンポーネント自身のソースは、各プロジェクトの配布元から入手できます。
入手先は後述の `THIRD-PARTY-NOTICES.txt` に記載しています。

## 第三者パッケージのライセンス表示

配布 ZIP には **`THIRD-PARTY-NOTICES.txt`** を同梱しています。
組み込んだパッケージの一覧・ライセンス本文・配布元をまとめたものです。

このファイルは `tools/gen_third_party_notices.py` が**ビルド環境から生成**するため、
実際に組み込まれたバージョンと必ず一致します。

```bash
python tools/gen_third_party_notices.py > THIRD-PARTY-NOTICES.txt
```

MIT・BSD・Apache-2.0 系のライセンスは、バイナリ配布時に著作権表示とライセンス本文を
添付することを要求します。このファイルがその要件を満たします。

## MIB ファイルを同梱していない理由

ベンダーが配布する MIB ファイルの多くは、**著作権表示のみで再配布の許諾を伴いません**。
第三者の著作物を GPL-3.0 のリポジトリから再配布することになるため、同梱していません。

同種の理由から、Debian もベンダー MIB を本体に含めず `snmp-mibs-downloader` として
別扱いにし、実行時にダウンロードする方式を採っています。

MIB が 1 つも無くても NetBelt は動作します。入手と配置の手順は
[mibs/README.md](mibs/README.md) を参照してください。

## 免責

本ソフトウェアは**現状のまま（AS IS）**提供され、いかなる保証もありません。
商品性、特定目的への適合性、権利非侵害を含め、明示・黙示を問わず一切の保証をしません。
詳細は GPL-3.0 の第15条・第16条を参照してください。

サポート方針は [README](README.md#サポートについて) に記載しています。

---

## English

### Licence

**GPL-3.0-or-later.** See [LICENSE](LICENSE) for the full text.

The GUI uses **PyQt6**, which is dual-licensed under GPL-3.0 and a commercial
licence. This project does not hold a commercial licence, so it follows GPL-3.0,
and the project as a whole is therefore GPL-3.0.

GPL applies to NetBelt's own code in this repository. Bundled third-party
packages remain under their respective licences.

### Corresponding source

> Provided to satisfy section 6 of GPL-3.0.

The distributed `NetBelt.exe` is a PyInstaller one-file build that embeds
**PyQt6 (GPL-3.0), Qt (LGPL-3.0), paramiko (LGPL-2.1-or-later)** and CPython.

The corresponding source for a released binary is:

- the **Source code archive** GitHub attaches to that release, and
- the **matching tag** in this repository (for example `v1.0.0`).

The build is reproducible from the bundled `requirements.txt` (all versions
pinned) and `NetBelt.spec` on **Python 3.11**:

```bash
pip install -r requirements.txt
python -m PyInstaller --clean NetBelt.spec
```

Sources for the embedded third-party components are available from their own
distributors; the URLs are listed in `THIRD-PARTY-NOTICES.txt`.

### Third-party notices

Each release ships **`THIRD-PARTY-NOTICES.txt`**, listing every bundled package
with its licence text and origin. It is generated from the build environment by
`tools/gen_third_party_notices.py`, so it always matches the versions actually
embedded. This satisfies the attribution requirements of the MIT, BSD and
Apache-2.0 licences that several dependencies use.

### Why no MIB files are bundled

Vendor MIB files usually carry a copyright notice without any redistribution
grant, so shipping them from a GPL-3.0 repository is avoided. Debian takes the
same view, keeping vendor MIBs out of the main archive via
`snmp-mibs-downloader`. NetBelt works without them; see
[mibs/README.md](mibs/README.md) for how to obtain and install them.

### Warranty

This software is provided **AS IS**, without warranty of any kind, express or
implied, including but not limited to merchantability, fitness for a particular
purpose and non-infringement. See sections 15 and 16 of GPL-3.0.
