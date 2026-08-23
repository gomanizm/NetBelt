# MIB ファイルの配置

このディレクトリに MIB ファイルを置くと、NetBelt が起動時に読み込み、
SNMP Trap の OID を名前に変換できるようになります。

## MIB ファイルを同梱していない理由

ベンダーが配布する MIB ファイルは、その多くが著作権表示のみで再配布の許諾を伴いません。
NetBelt は GPL-3.0 で公開していますが、第三者の著作物を GPL で再ライセンスすることは
できないため、**MIB ファイルは同梱せず、利用者ご自身で入手していただく形にしています。**

同じ理由から、Debian も vendor MIB を本体に含めず `snmp-mibs-downloader` として
別扱いにしています。

MIB ファイルが 1 つも無くても NetBelt は動作します。その場合、標準 MIB-II の主要な OID
（`sysDescr`、`ifIndex`、`linkUp`/`linkDown` など約 40 件）は内蔵の定義で名前解決されます。

## MIB ファイルの入手先

- **Cisco** — [SNMP Object Navigator](https://snmp.cisco.com/) または
  [Software Download](https://software.cisco.com/download/home) の MIB Locator
- **Juniper** — [Junos MIB ダウンロード](https://www.juniper.net/documentation/)
- **IETF 標準 MIB** — 各 RFC 本文に記載

各配布元の利用条件に従ってください。

## 使い方

1. 入手した MIB ファイルをこのディレクトリ直下に置く
2. NetBelt を再起動する

対応する拡張子は `.mib` / `.txt` / `.my` です。

```
NetBelt/
├── mibs/
│   ├── CISCO-SYSLOG-MIB.my
│   ├── CISCO-CONFIG-MAN-MIB.my
│   ├── MY-CUSTOM-MIB.txt
│   └── README.md（このファイル）
```

起動時にコンソールへ読み込み結果が出ます。

```
[MIBResolver] CISCO-SYSLOG-MIB.my: 15件
[MIBResolver] MIBファイルから合計 23件のOIDを読み込みました
```

> **初回のみ時間がかかります。** ファイル数が多い場合、初回の解析に数分かかることがあります。
> 2 回目以降は `mib_cache.json` により高速化されます。MIB を入れ替えたときは
> このキャッシュが自動で更新されます。

## 自分で MIB を書く場合

標準的な SMI 形式に対応しています。簡易パーサーのため、複雑な構文は解釈できないことがあります。

```
MY-EXAMPLE-MIB DEFINITIONS ::= BEGIN

IMPORTS
    enterprises FROM SNMPv2-SMI
    NOTIFICATION-TYPE FROM SNMPv2-SMI;

myCompany OBJECT IDENTIFIER ::= { enterprises 99999 }
myProduct OBJECT IDENTIFIER ::= { myCompany 1 }
myTraps   OBJECT IDENTIFIER ::= { myCompany 2 }

myAlarmTrap NOTIFICATION-TYPE
    STATUS current
    DESCRIPTION "アラーム発生時の Trap"
    ::= { myTraps 1 }

END
```

これを置くと、次のように解決されます。

| OID | 名前 |
|---|---|
| `1.3.6.1.4.1.99999` | `myCompany` |
| `1.3.6.1.4.1.99999.1` | `myProduct` |
| `1.3.6.1.4.1.99999.2.1` | `myAlarmTrap` |

## うまく読み込めないとき

パーサーが対応できない構文の場合は、リポジトリ直下の `custom_mibs.json` に
OID と名前を直接書けます。こちらが確実です。

```json
{
  "mibs": {
    "1.3.6.1.4.1.9.9.41.2.0.1": "clogMessageGenerated"
  }
}
```

読み込み順は「内蔵の標準 MIB → `custom_mibs.json` → `mibs/` 内のファイル」で、
後から読み込まれたものが優先されます。
