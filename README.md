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
- **自動実行コマンド** — グループごとに登録したコマンドを、接続直後に自動投入（`terminal length 0` など）。ツリーでグループを右クリック →「グループを編集」から設定します。SSH / Telnet が対象です。**config.json に平文で保存されるため、パスワードは書かないでください**
- **セッションログ** — 画面の内容をファイルへ保存
- **コピー / ペースト** — マウスで範囲を選ぶと、その時点でクリップボードへコピーされます（`Ctrl+Shift+C` でもコピーできます。ターミナルの `Ctrl+C` は機器への中断送信（0x03）に使うため、端末ソフトの慣習に合わせています）。貼り付けはターミナルの右クリックで、接続中のタブでのみ動きます。改行を含む内容（行ごとにコマンドとして実行されます）や、`Ctrl+Z` などの制御文字（タブを除く）を含む内容は、送る前に確認ダイアログで中身を表示します（既定のボタンは「キャンセル」）。貼り付けにキーボードのショートカットはありません
- **フォントサイズ変更** — 表示メニューから 6〜32pt。開いているタブすべてに即反映され、以降に開くタブにも引き継ぎます

### 受信サーバ / 転送
- **Syslog 受信** — UDP / TCP、レベルフィルタ付き（既定 514）
- **SNMP Trap 受信** — v1 / v2c / v3（USM）に対応。MIB による OID の名前解決、CSV エクスポート（既定 162/UDP）
- **SNMP GET / WALK** — v1 / v2c / v3（USM）に対応。任意の OID の取得と巡回、結果の CSV / JSON / テキスト出力
- **TFTP サーバ** — ネットワーク機器の config / イメージ授受（既定 69/UDP）
- **FTP サーバ** — 同上。匿名・認証の両対応（既定 21/TCP）
- **SFTP サーバ / クライアント** — 双方向のファイル転送、転送履歴（既定 2222/TCP）。クライアントはターミナルの SSH セッションを使うため、機器へ SSH 接続すると利用できます（機器が SFTP に対応していない場合は使えません）
- **ツールエリアのタブ化・切り離し** — 各サーバのパネルを別ウィンドウへ分離可能

#### SNMPv3 について

GET / WALK は認証に MD5 / SHA-1 / SHA-224 / SHA-256 / SHA-384 / SHA-512、
暗号化に DES / 3DES / AES-128 / AES-192 / AES-256 が使えます
（AES-192 / AES-256 はベンダー実装で広く使われている Reeder 方式です）。
実際に使える方式は接続先機器の実装に依存します。
SNMP パネルでバージョンに v3 を選ぶと「v3認証」タブが有効になり、
v1/v2c 認証タブは無効になります。ユーザ名は必須で、認証なしでの暗号化は
選べません（実行前に警告して止まります）。

Trap 受信のバージョン選択は「両方」「v1/v2c」「v3」から選べ、既定は
「両方」です。「両方」で v3 のユーザを設定すると、v1/v2c と v3 を同一
ポートで同時に受信します。v3 のユーザを設定しなければ v1/v2c だけを
受信します。

**Trap を v3 で受信する場合は、送信元機器の EngineID の登録が必要です。**
SNMPv3 の Trap では送信側が authoritative engine となるため、受信側が
あらかじめ機器の EngineID を知っていないと復号・認証ができません。
Cisco IOS なら `show snmp engineID` で確認できます。EngineID は偶数桁の
16進で（例: `8000000001020304`）、複数台から受ける場合は1行に1つずつ
入力してください。v3 のユーザ名を入力したまま EngineID を登録せずに
受信を開始しようとすると、警告が出て止まります。v3 の受信を有効に
するには、まずユーザ名を設定し、その上で EngineID を登録してから
開始してください。

v3 の認証情報は保存されません。アプリを起動するたびに入力が必要です。

受信した Trap の一覧には、どの版・どの保護レベルで届いたかを示す
「セキュリティ」列があり、v3 の場合はユーザ名も出ます（例:
`v3 authPriv / netbelt`）。この列はエクスポートにも含まれます。
パスワードと、v1/v2c のコミュニティ文字列は出ません。

### その他
- **ポートチェッカー** — この PC のポートが空いているかを調べます（ツール → ポートチェッカー）。指定ポートへ実際にバインドを試し、使用中なら `netstat` と `tasklist` で占有しているプロセスを特定します。Syslog・TFTP・SNMP Trap などの受信サーバが起動できないときの切り分け用です。リモート機器へのポートスキャンではありません
- **設定** — ターミナルの配色とフォント、SFTP クライアントの動作、起動時の更新確認を変更できます（ツール → 設定）
- パスワードは **Windows DPAPI** で暗号化して保存（OS・ユーザーアカウントに紐付け）
- SSH ホストキーの **TOFU**（Trust On First Use）検証
- GitHub Releases を見に行く**自動更新チェック** — ダウンロードした ZIP は SHA-256 で照合し、一致しなければ適用しません

> **1.2.0 以前から更新する場合の注意**
> 更新を当てるのは、いま入っている版に同梱された `updater.bat` です。そのため
> 1.2.0 以前から上げるときは、そちらの古い不具合がそのまま出ます（実物の
> 配布物で確認済み）。
> - **NetBelt を終了してから更新してください。** 起動したままだと実行ファイルを
>   置き換えられず、1.1.x では失敗を繰り返します。
> - 1.2.0 からの更新では、途中で英語のエラー（`ERROR: could not create a work
>   folder in TEMP` など）が出ることがありますが、最後に「更新が完了しました！」が
>   出ていれば**当たっています**。
> - 1.1.x からの更新では、最後まで英語のエラーで終わることがあります。この場合も
>   実行ファイルは置き換わっているので、`NetBelt.exe` を手で起動して版を確かめて
>   ください（ヘルプ → バージョン情報）。
> - 1.3.0 以降どうしの更新では、これらは起きません。

> **TEMP のパスに `[` や `]` がある場合**
> 1.3.0 以前からの自動更新は、ZIP の展開で止まります（「ZIPファイルの展開に失敗しました」。
> 元の版はそのまま残ります）。その 1 回だけ、Releases の ZIP を手で展開して NetBelt の
> フォルダへ上書きすれば、1.3.1 以降は自動更新で当たります。

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

受信サーバ（FTP / TFTP / Syslog / SFTP / SNMP Trap）は、起動しても Windows Defender
ファイアウォールの受信許可ルールを追加しません。起動しただけで管理者権限（UAC）を求めないためです。

そのポートで受信できないときは、各パネルの**「ファイアウォールで許可（管理者）」**を一度押してください。
押したときだけ UAC が表示され、`NetBelt - <サービス> (<プロトコル>/<ポート>)` という名前で
受信許可ルールを追加します。

v1.3.0 より前の版が自動で作ったルールは、そのまま残っています。同じポートを使い続けるなら
追加の操作は要りません。

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

`dist/NetBelt.exe` が生成されます。`updater.bat` を `dist/` へコピーして、
exe と同じフォルダに置いたまま配布してください（`build.bat` を使う場合は
自動でコピーされます）。自動更新は最後にこのスクリプトを exe の隣から
探すため、無いと更新を適用できません。

## テスト

```bash
python -m pytest tests -q
```

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

**個人が自分の業務効率化のために作ったツール**を、そのまま公開しているものです。
他人が使うことを主眼に設計してはいないため、UI は日本語のみ、対象は Windows のみで、
既定値も作者の使い方に合わせてあります。**サポートは行いません。**

- 使い方の質問、導入支援、動作保証には対応できません
- 不具合の報告は Issue で受け付けますが、**返信や修正をお約束するものではありません**
- 手が空いたときに、低頻度で修正を行う可能性があります
- 現状のまま（AS IS）で提供され、いかなる保証もありません（GPL-3.0 の免責条項のとおり）

以上は**作りとサポートの話**で、利用の許諾とは別です。ライセンス上は
**業務・商用を問わず自由に利用できます**（GPL-3.0 は利用目的を制限しません）。
社内で使う分に追加のライセンスは不要です（[詳しくは LICENSING.md](LICENSING.md)）。

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
- **Receivers** — Syslog (UDP/TCP 514) with level filtering; SNMP trap (UDP 162,
  v1/v2c/v3 with USM) with MIB name resolution, community filtering and CSV export
- **File transfer** — TFTP (UDP 69), FTP (TCP 21) and SFTP (TCP 2222) servers,
  plus an SFTP client, for moving configs and images to and from network devices.
  The client rides the terminal's SSH session, so it becomes available once you
  connect to a device — provided that device supports SFTP.
- **Auto commands** — commands registered per group are sent right after connecting (e.g. `terminal length 0`). Configure them by right-clicking a group in the tree and choosing 「グループを編集」 ("Edit group"). Applies to SSH and Telnet. They are stored in plain text in `config.json`, so do not put passwords there.
- **Copy / paste** — selecting text with the mouse copies it to the clipboard right away (`Ctrl+Shift+C` also copies, following terminal-emulator convention: `Ctrl+C` in a terminal tab is left free to send an interrupt (0x03) to the device). Right-click in the terminal to paste; this only works on a connected tab. If the text contains a line break (each line would run as a command) or a control character such as `Ctrl+Z` (tabs excepted), a confirmation dialog shows it before anything is sent, with Cancel as the default. There is no keyboard shortcut for paste.
- **SNMP GET / WALK** — v1/v2c/v3 with USM; fetch or walk arbitrary OIDs, export results as CSV / JSON / text
- **Port checker** — checks whether a port on this PC is free (Tools → ポートチェッカー). It attempts a real bind, and when the port is taken it identifies the owning process via `netstat` and `tasklist`. Meant for troubleshooting why a receiving server (Syslog, TFTP, SNMP Trap, …) will not start. It is not a port scanner for remote devices.
- **Settings** — terminal colors and font, SFTP client behavior, and the startup update check (Tools → 設定)
- Passwords are encrypted with **Windows DPAPI**, tied to the OS user account
- SSH host keys are verified on a **trust-on-first-use** basis
- Update checks against GitHub Releases, with **SHA-256 verification** of the
  downloaded package

#### SNMPv3

GET/WALK support authentication with MD5, SHA-1, SHA-224, SHA-256, SHA-384
or SHA-512, and encryption with DES, 3DES, AES-128, AES-192 or AES-256
(AES-192 and AES-256 use the Reeder variant, the one most vendor
implementations use). Which of these are actually usable depends on
the device you connect to. Selecting v3 in the SNMP panel enables the "v3"
auth tab and disables the v1/v2c one. A username is required, and
encryption cannot be selected without authentication — the app blocks
the request and warns instead.

Trap version selection is "Both" / "v1/v2c" / "v3", defaulting to **Both**.
With a v3 user configured, "Both" receives v1/v2c and v3 traps on the same
port at the same time; without one it receives v1/v2c only.

**Receiving v3 traps requires registering the sending device's EngineID.**
In SNMPv3 traps the sender is the authoritative engine, so the receiver
must already know the device's EngineID to authenticate and decrypt the
message. On Cisco IOS, `show snmp engineID` shows it. EngineIDs are
entered as even-length hex (e.g. `8000000001020304`), one per line for
multiple devices. Starting the receiver with a v3 username but no
EngineID registered will warn and refuse to start. To enable v3
reception, set the username first, then register the EngineID,
then start the receiver.

v3 credentials are never saved to disk — they must be re-entered every
time the app starts.

The trap list has a security column showing which version and protection
level each trap arrived with, including the v3 username (e.g.
`v3 authPriv / netbelt`). It is carried into the exports too. Passwords
and v1/v2c community strings are not shown anywhere.

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

**None.** This is a personal tool the author built to speed up their own work,
published as is. It is not designed with other users in mind: the interface is
Japanese only, it runs on Windows only, and the defaults suit the author's
workflow. Bug reports are welcome as issues, but replies and fixes are not
promised; fixes may happen infrequently, if at all.

That is a statement about design and support, not about permission. **You are
free to use it commercially and at work** — GPL-3.0 places no restriction on
purpose. Using it inside your organisation needs no additional licence; see
[LICENSING.md](LICENSING.md#english) for the details. Forking and fixing it
yourself is the reliable option — GPL-3.0, so please do.
