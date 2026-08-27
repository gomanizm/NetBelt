## NetBelt ポータブル版

### 📄 ライセンスと対応するソースコード

NetBelt は GNU General Public License v3.0 で配布しています。

この実行ファイルには PyQt6 (GPL-3.0)、Qt (LGPL-3.0)、paramiko (LGPL-2.1-or-later)
などが組み込まれています。**この実行ファイルに対応するソースコード**は、
本リリースに自動添付される Source code アーカイブ、および
https://github.com/gomanizm/NetBelt のタグ `{{TAG}}` で入手できます。
ビルドは同梱の `requirements.txt`（バージョン固定済み）と `NetBelt.spec`、
Python 3.11 で再現できます。

組み込まれた第三者パッケージの一覧・ライセンス本文・配布元は、
ZIP 内の `THIRD-PARTY-NOTICES.txt` を参照してください。

### 📦 パッケージ内容
- `NetBelt.exe` - アプリケーション本体
- `README.txt` - 使い方ガイド
- `LICENSE.txt` - NetBelt のライセンス（GPL-3.0）
- `THIRD-PARTY-NOTICES.txt` - 同梱した第三者パッケージのライセンス表示
- `CHANGELOG.txt` - この版で変わったこと
- `.sha256` - ZIP のチェックサム。自動更新がダウンロード後に照合し、
  一致しなければ更新を中止します（破損の検知が目的で、GitHub 自体が
  侵害された場合の改ざんは検知できません）
- `mibs/` - MIB ファイルの配置先（既定では空。README を参照）

### 🚀 インストール方法
1. ZIPファイルをダウンロード
2. 任意のフォルダに展開
3. `NetBelt.exe` をダブルクリックして起動

### 💻 動作環境
- Windows 10 / Windows 11 (64bit)
- Pythonのインストールは不要
- すべての依存ファイルが含まれています

### ✨ 主な機能
- SSH、Telnet、シリアル接続に対応
- デバイス管理とグループ化
- マクロ機能
- SNMP Trap / Syslog 受信（カスタムMIB対応）
- SFTP ファイル転送

### 📝 初回起動
- 初回起動時に `config.json` が自動生成されます
- MIB ファイルは同梱していません。`mibs/README.md` の手順で配置してください

詳しい使い方は [README.md](https://github.com/gomanizm/NetBelt/blob/main/README.md) をご覧ください。
