# NetBelt

**ネットワーク技術者の工具ベルト。** SSH / Telnet / シリアルのターミナルに、現場で必要な受信サーバ
（Syslog・SNMP Trap・FTP・TFTP・SFTP）をひとまとめにした Windows 向けデスクトップツールです。

ターミナルソフトと、config・イメージを受け渡すためのサーバ類を別々に立ち上げる必要がありません。

![License](https://img.shields.io/badge/license-GPL--3.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.11-blue.svg)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)

> **English speakers**: see [English](#english) at the bottom.
> Note that **the user interface is in Japanese only**.

## 主な機能

### ターミナル
- **SSH / Telnet / シリアル接続**
- **デバイス管理** — グループに整理してツリーから接続
- **複数タブ** — 複数機器へ同時接続してタブで切り替え
- **マクロ** — よく使うコマンド列を登録して一括実行
- **自動実行コマンド** — 接続時に `terminal length 0` などを自動投入
- **セッションログ** — 画面の内容をファイルへ保存

### 受信サーバ / 転送
- **Syslog 受信** — UDP / TCP、レベルフィルタ付き（既定 514）
- **SNMP Trap 受信** — MIB による OID の名前解決、CSV エクスポート（既定 162/UDP）
- **TFTP サーバ** — ネットワーク機器の config / イメージ授受（既定 69/UDP）
- **FTP サーバ** — 同上。匿名・認証の両対応（既定 21/TCP）
- **SFTP サーバ / クライアント** — 双方向のファイル転送、転送履歴（既定 2222/TCP）
- **ツールエリアのタブ化・切り離し** — 各サーバのパネルを別ウィンドウへ分離可能

### その他
- パスワードは **Windows DPAPI** で暗号化して保存（OS・ユーザーアカウントに紐付け）
- SSH ホストキーの **TOFU**（Trust On First Use）検証
- GitHub Releases を見に行く**自動更新チェック** — ダウンロードした ZIP は SHA-256 で照合し、一致しなければ適用しません

## 動作環境

- **Windows 10 / 11 専用**
  - パスワードの暗号化に Windows DPAPI を使うため、他 OS では動作しません
- ポータブル版（`.exe`）は Python のインストール不要

## ダウンロード

[Releases](https://github.com/gomanizm/NetBelt/releases) から
`NetBelt-vX.Y.Z-Windows-Portable.zip` を取得し、展開して `NetBelt.exe` を実行してください。

Windows SmartScreen の警告が出る場合は「詳細情報」→「実行」を選んでください
（コード署名証明書を持たないため、署名なしの実行ファイルとして扱われます）。

## ファイアウォールについて

受信サーバを起動すると、そのポートに対する Windows Defender ファイアウォールの受信許可ルールを
`NetBelt - <サービス> (<プロトコル>/<ポート>)` という名前で追加します。
ルール作成には管理者権限が必要なため、**そのポートのルールを初めて作るときだけ UAC が表示**されます。

アンインストール時にルールを消すには、管理者権限の PowerShell で次を実行してください。

```powershell
Get-NetFirewallRule -DisplayName 'NetBelt - *' | Remove-NetFirewallRule
```

## サーバ機能を使うときの注意

内蔵の各サーバは `0.0.0.0`（すべてのネットワークインターフェース）で待ち受けます。
必要なときだけ起動し、使い終わったら停止してください。

SFTP サーバと FTP サーバのユーザー名・パスワードに既定値はありません。起動のたびに指定が必要です。
TFTP サーバはプロトコル上そもそも認証がありません。既定のルートは `./tftp_root` です。

FTP サーバの匿名アクセスを有効にすると、認証なしで読み書きできる状態になります。
ネットワーク機器から認証なしで config を受け取る用途（3CDaemon 的な使い方）を想定した挙動です。

## ソースから実行する

```bash
git clone https://github.com/gomanizm/NetBelt.git
cd NetBelt
pip install -r requirements.txt
python src/main.py
```

## ビルド

```bash
pip install -r requirements.txt
python -m PyInstaller --clean NetBelt.spec
```

`dist/NetBelt.exe` が生成されます。

## テスト

```bash
python -m pytest tests -q
```

リポジトリ直下の `test_*.py` は pytest 用のテストではなく、**対話的に実行する手動確認スクリプト**です
（実機やサーバへ実際にパケットを投げます）。`pytest.ini` の `testpaths` により自動収集からは除外しています。

## MIB ファイル

MIB は同梱していません（[理由](LICENSING.md#mib-ファイルを同梱していない理由)）。
無くても動作し、標準 MIB-II の主要な OID は内蔵の定義で名前解決されます。

ベンダー固有の OID を名前で表示したい場合は、各ベンダーの公式配布元から入手して
`mibs/` に置いてください。手順は [mibs/README.md](mibs/README.md) にあります。

> 大量に置くと、初回起動時の解析に時間がかかります（数千本で数分）。
> 解析が終わるまで SNMP Trap の受信は開始できません。

## ライセンス

**GNU General Public License v3.0 or later（GPL-3.0-or-later）**

GUI に PyQt6（GPL-3.0）を使用しているため、プロジェクト全体も GPL-3.0 に従います。
全文は [LICENSE](LICENSE) を参照してください。

配布バイナリに対応するソースコードの入手方法（GPL-3.0 第6条）、第三者パッケージの
ライセンス表示、MIB を同梱していない理由などは **[LICENSING.md](LICENSING.md)** に
まとめています。

## サポートについて

これは業務で使うために個人が開発したツールです。**サポートは行いません。**

- 使い方の質問、導入支援、動作保証には対応できません
- 不具合の報告は Issue で受け付けますが、**返信や修正をお約束するものではありません**
- 手が空いたときに、低頻度で修正を行う可能性があります
- 現状のまま（AS IS）で提供され、いかなる保証もありません（GPL-3.0 の免責条項のとおり）

フォークして自分で直していただくのが確実です。GPL-3.0 なのでご自由にどうぞ。

---

## English

NetBelt is a Windows desktop tool for network engineers. It combines an
SSH / Telnet / serial terminal with the file-transfer and log-collection servers
you normally need alongside it, so you do not have to run a separate terminal
application and a set of daemons.

> **The user interface is in Japanese only.** There is no localisation at present.
> Menus, dialogs and log messages are all in Japanese.

### What it does

- **Terminal** — SSH, Telnet and serial, with device grouping, tabs, command macros
  and session logging
- **Receivers** — Syslog (UDP/TCP 514) with level filtering; SNMP trap (UDP 162)
  with MIB name resolution, community filtering and CSV export
- **File transfer** — TFTP (UDP 69), FTP (TCP 21) and SFTP (TCP 2222) servers,
  plus an SFTP client, for moving configs and images to and from network devices
- Passwords are encrypted with **Windows DPAPI**, tied to the OS user account
- SSH host keys are verified on a **trust-on-first-use** basis
- Update checks against GitHub Releases, with **SHA-256 verification** of the
  downloaded package

### Requirements

**Windows 10 / 11 only.** The password store uses Windows DPAPI, so it does not
run on other platforms. The portable build does not require Python.

### Download

See [Releases](https://github.com/gomanizm/NetBelt/releases). Windows SmartScreen
will warn about the executable because it is not code-signed; choose
**More info** then **Run anyway**.

### A note on the built-in servers

Each server listens on `0.0.0.0`. Start them only when needed and stop them
afterwards. SFTP and FTP require a username and password to be entered every
time — there are no defaults. TFTP has no authentication at all, by protocol design.

### MIB files

None are bundled — see [LICENSING.md](LICENSING.md#why-no-mib-files-are-bundled)
for the reason. The application works without them; common MIB-II OIDs are
resolved from a built-in table. To resolve vendor-specific OIDs, obtain the MIBs
from your vendor and place them in `mibs/` (see [mibs/README.md](mibs/README.md)).

### Licence

**GPL-3.0-or-later.** The GUI uses PyQt6, which is GPL-3.0, so the project as a
whole follows GPL-3.0. See [LICENSE](LICENSE) for the full text.

Corresponding source for released binaries (GPL-3.0 section 6), third-party
notices, and the reasoning behind the bundling decisions are documented in
**[LICENSING.md](LICENSING.md#english)**.

### Support

**None.** Bug reports are welcome as issues, but replies and fixes are not
promised. This is a personal tool built for the author's own work; fixes may
happen infrequently, if at all. Forking and fixing it yourself is the reliable
option — GPL-3.0, so please do.
