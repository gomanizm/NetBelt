"""設定ファイル管理モジュール"""
import json
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional
from .crypto import PasswordCrypto

# 機器名として使えない名前。ターミナルはホームタブをタブ名 "ホーム" で
# 見分けているので、同名の機器はタブを閉じられず、ログ保存・記録・
# マクロ設定も「ホームタブ」扱いで断られる。
RESERVED_DEVICE_NAMES = ("ホーム",)


def is_reserved_device_name(name) -> bool:
    """その名前が機器名として予約されているか（前後の空白は無視）"""
    return isinstance(name, str) and name.strip() in RESERVED_DEVICE_NAMES


def app_data_dir():
    """アプリのデータ保存先 (~/.netbelt) を返す。無ければ作る。

    known_hosts や SFTP サーバのホストキーなど、設定ファイルとは別に
    ユーザー単位で持ち回るものを置く。
    旧名 ~/.terminal-tool に known_hosts がある場合は、初回のみ引き継ぐ。
    引き継ぎに失敗しても致命的ではない（TOFU の確認が再度出るだけ）ので握り潰す。
    """
    new_dir = Path.home() / ".netbelt"
    try:
        new_dir.mkdir(exist_ok=True)
        old_kh = Path.home() / ".terminal-tool" / "known_hosts"
        new_kh = new_dir / "known_hosts"
        if old_kh.exists() and not new_kh.exists():
            import shutil
            shutil.copy2(str(old_kh), str(new_kh))
            print("[Config] known_hosts を ~/.terminal-tool から引き継ぎました")
    except Exception:
        pass
    return new_dir


class ConfigManager:
    """設定ファイルの読み書きを管理するクラス"""
    
    def __init__(self, config_path: str = "config.json"):
        """
        初期化
        
        Args:
            config_path: 設定ファイルのパス
        """
        self.config_path = Path(config_path)
        self.crypto = PasswordCrypto()
        self.load_error = None  # 読み込みエラー情報
        self.load_warning = None  # 読み込めたが一部を除外したときの警告文
        self.backup_path = None  # バックアップファイルパス
        self.config = self._load_config()
    
    def _load_config(self) -> Dict:
        """
        設定ファイルを読み込む
        
        Returns:
            設定データ
        """
        # 設定ファイルが存在しない場合はデフォルト設定を読み込む
        if not self.config_path.exists():
            config = self._load_default_config()
            # デフォルト設定をconfig.jsonとして保存
            self.config = config
            self.save_config()
            return config
        
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # パスワードを復号化
                self._decrypt_passwords(config)
                self._notify_undecryptable()
                # name/host の無い機器は一覧から外す（UI が KeyError で落ちる）
                self._quarantine_invalid_devices(config)
                # Defaultグループが存在しない場合は追加（戻り値でフラグを受け取る）
                need_save = self._ensure_default_group(config)
                # 一時的にconfigを設定して保存
                if need_save:
                    self.config = config
                    self.save_config()
                return config
        except Exception as e:
            print(f"[ERROR] 設定ファイルの読み込みエラー: {e}")
            # エラー情報を保存
            self.load_error = str(e)
            # 破損した設定ファイルをバックアップ
            self._backup_corrupted_config()
            # デフォルト設定を返す（既存ファイルは上書きしない）
            print("[INFO] デフォルト設定を使用します。次回保存時に設定ファイルが作成されます。")
            return self._load_default_config()
    
    def _load_default_config(self) -> Dict:
        """
        デフォルト設定を読み込む
        
        Returns:
            デフォルト設定データ
        """
        default_path = Path(__file__).parent.parent / "resources" / "default_config.json"
        
        try:
            with open(default_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"デフォルト設定ファイルの読み込みエラー: {e}")
            # フォールバック: 最小限の設定を返す
            return {
                "config_version": "1.0",
                "groups": [],
                "global_macros": [],
                "settings": {
                    "terminal": {
                        "background_color": "#000000",
                        "text_color": "#FFFFFF",
                        "font_family": "Consolas",
                        "font_size": 10
                    }
                },
                "update_settings": {
                    "check_on_startup": True,
                    "skipped_version": None,
                    "last_check": None,
                    "github_token": None
                }
            }
    
    # 機器のパスワードと同じく暗号化して保存する settings のセクション。
    # 画面では伏字にしているのにディスクは平文、という状態を避ける。
    _ENCRYPTED_SETTING_SECTIONS = ("ftp_server", "sftp_server")

    def _encrypt_passwords(self, config: Dict) -> None:
        """
        設定内の全パスワードを暗号化
        
        Args:
            config: 設定データ
        """
        for group in config.get("groups", []):
            for device in group.get("devices", []):
                password = device.get("password", "")
                # 既に暗号化済みの値は再暗号化しない。DPAPI の鍵は Windows
                # アカウント/マシンに紐づくため、config.json を別環境へ移すと
                # decrypt() が暗号文をそのまま返す。それを平文とみなして再度
                # 暗号化すると二重に包まれ、元のパスワードが復元不能になる。
                # 保存は起動時にも走るので、ここで弾かないと原本が失われる。
                if password and not self.crypto.is_encrypted(password):
                    device["password"] = self.crypto.encrypt(password)

        settings = config.get("settings")
        if not isinstance(settings, dict):
            return
        for name in self._ENCRYPTED_SETTING_SECTIONS:
            section = settings.get(name)
            if not isinstance(section, dict):
                continue
            password = section.get("password", "")
            # 機器側と同じ理由で、暗号化済みは触らない
            if (isinstance(password, str) and password
                    and not self.crypto.is_encrypted(password)):
                section["password"] = self.crypto.encrypt(password)
    
    def _decrypt_passwords(self, config: Dict) -> None:
        self._undecryptable_count = 0
        """
        設定内の全パスワードを復号化
        
        Args:
            config: 設定データ
        """
        for group in config.get("groups", []):
            for device in group.get("devices", []):
                encrypted = device.get("password", "")
                if not encrypted:
                    continue
                decrypted = self.crypto.decrypt(encrypted)
                # 復号に失敗すると入力がそのまま返る。値は保持したまま件数を数える
                # （原本の保護は _encrypt_passwords 側で行う）
                if self.crypto.is_encrypted(decrypted):
                    self._undecryptable_count += 1
                device["password"] = decrypted

        settings = config.get("settings")
        if not isinstance(settings, dict):
            return
        for name in self._ENCRYPTED_SETTING_SECTIONS:
            section = settings.get(name)
            if not isinstance(section, dict):
                continue
            encrypted = section.get("password", "")
            if not isinstance(encrypted, str) or not encrypted:
                continue
            decrypted = self.crypto.decrypt(encrypted)
            if self.crypto.is_encrypted(decrypted):
                self._undecryptable_count += 1
            section["password"] = decrypted
    
    def _notify_undecryptable(self) -> None:
        """復号できなかったパスワードがあれば知らせる。"""
        n = getattr(self, "_undecryptable_count", 0)
        if n:
            print(f"[Config] {n}件のパスワードを復号できませんでした。"
                  "別の Windows アカウント/PC で保存された設定の可能性があります。"
                  "該当機器のパスワードは再入力してください。"
                  "（設定ファイル内の元の値は保護されており、上書きされません）")

    @staticmethod
    def _is_valid_device(device) -> bool:
        """UI が前提にする必須フィールド（name/host が空でない文字列）を持つか。"""
        return (isinstance(device, dict)
                and isinstance(device.get("name"), str) and bool(device["name"])
                and isinstance(device.get("host"), str) and bool(device["host"]))

    def _quarantine_invalid_devices(self, config: Dict) -> None:
        """必須フィールドの無い機器を各グループから外し、警告を記録する。

        手編集や他ツールで作られた config.json に {} や {"host": ...} の
        ような機器が混ざると、DeviceTree の構築が KeyError で落ちて
        起動できない。JSON 構文エラーとは違い load_error にもならないので、
        利用者には設定ファイルが原因だと分からなかった。
        不正な機器だけを除外し、元ファイルはバックアップして知らせる。

        予約語（ホームタブと重なる名前）の機器も同じ理由でここで外す。
        登録の入口（DeviceDialog / add_device / update_device）は別途
        断っているが、手編集された config や、その検査より前のバージョン
        で作られた config からは今でも入ってくる。
        """
        removed = 0
        reserved = 0
        for group in config.get("groups", []):
            devices = group.get("devices")
            if not isinstance(devices, list):
                continue
            kept = []
            for d in devices:
                if not self._is_valid_device(d):
                    removed += 1
                elif is_reserved_device_name(d["name"]):
                    reserved += 1
                else:
                    kept.append(d)
            group["devices"] = kept
        if not removed and not reserved:
            return
        self._backup_corrupted_config()
        reasons = []
        if removed:
            reasons.append(f"名前またはホストの無い機器が{removed}件")
        if reserved:
            reasons.append(f"ホームタブと重なる名前（「ホーム」）の機器が{reserved}件")
        message = ("設定ファイル (config.json) に" + "、".join(reasons) +
                   "あり、接続先リストから除外しました。\n"
                   "除外した機器は次回の保存時に設定ファイルから消えます。")
        if self.backup_path:
            message += f"\n\n元のファイルはバックアップしました:\n  {self.backup_path}"
        self.load_warning = message
        print(f"[Config] {removed + reserved}件の機器を除外しました "
              f"(name/host 無し={removed}, 予約語={reserved})")

    def _backup_corrupted_config(self) -> None:
        """
        破損した設定ファイルをバックアップ
        """
        try:
            if not self.config_path.exists():
                return
            
            # タイムスタンプ付きバックアップファイル名を生成
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = self.config_path.with_name(f"config.json.backup_{timestamp}")
            
            # バックアップを作成
            import shutil
            shutil.copy2(self.config_path, backup_path)
            self.backup_path = str(backup_path)  # バックアップパスを保存
            print(f"[INFO] 破損した設定ファイルをバックアップしました: {backup_path}")
            
            # 古いバックアップファイルを整理（最新5つを保持）
            self._cleanup_old_backups()
            
        except Exception as e:
            print(f"[WARNING] 設定ファイルのバックアップに失敗しました: {e}")
    
    def _cleanup_old_backups(self, keep_count: int = 5) -> None:
        """
        古いバックアップファイルを削除
        
        Args:
            keep_count: 保持するバックアップ数
        """
        try:
            # バックアップファイルを検索
            backup_pattern = "config.json.backup_*"
            backup_dir = self.config_path.parent
            backup_files = sorted(backup_dir.glob(backup_pattern), reverse=True)
            
            # 古いバックアップを削除
            for backup_file in backup_files[keep_count:]:
                backup_file.unlink()
                print(f"[INFO] 古いバックアップを削除しました: {backup_file.name}")
                
        except Exception as e:
            print(f"[WARNING] バックアップファイルの整理に失敗しました: {e}")
    
    def _ensure_default_group(self, config: Dict) -> bool:
        """
        Defaultグループが存在しない場合は追加する
        
        Args:
            config: 設定データ
            
        Returns:
            Defaultグループを追加した場合True、既に存在する場合False
        """
        groups = config.get("groups", [])
        
        # Defaultグループが存在するかチェック
        has_default = any(g.get("name") == "Default" for g in groups)
        
        if not has_default:
            # Defaultグループを先頭に追加
            default_group = {
                "name": "Default",
                "auto_commands": [],
                "devices": []
            }
            groups.insert(0, default_group)
            config["groups"] = groups
            print("[INFO] Defaultグループを追加しました")
            return True
        
        return False
    
    def save_config(self) -> bool:
        """
        設定ファイルを保存
        
        Returns:
            保存成功時True、失敗時False
        """
        try:
            # 保存用に設定をコピー（パスワードを暗号化）
            save_config = json.loads(json.dumps(self.config))
            self._encrypt_passwords(save_config)
            
            # 本体を直接開くと、その瞬間に長さ 0 へ切り詰められる。書き終える
            # までに落ちると（強制終了・シャットダウン・ディスク満杯）全機器と
            # パスワードが消え、次回起動時のバックアップにも残骸しか入らない。
            # 同じディレクトリへ書いてから os.replace で差し替える。差し替えは
            # 不可分なので、失敗しても前の config.json がそのまま残る。
            # 一時ファイルを同階層に作るのは、os.replace がドライブを跨げないため。
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(self.config_path.parent),
                    prefix=self.config_path.name + ".",
                    suffix=".tmp")
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(save_config, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, str(self.config_path))
                tmp_path = None      # 差し替え済み。後片付けの対象から外す
            finally:
                if tmp_path is not None:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

            return True
        except Exception as e:
            print(f"設定ファイルの保存エラー: {e}")
            return False
    
    def get_groups(self) -> List[Dict]:
        """グループ一覧を取得"""
        return self.config.get("groups", [])
    
    def get_group(self, group_name: str) -> Optional[Dict]:
        """指定したグループを取得"""
        for group in self.config.get("groups", []):
            if group["name"] == group_name:
                return group
        return None
    
    def add_group(self, group_name: str, auto_commands: List[str] = None) -> bool:
        """
        グループを追加
        
        Args:
            group_name: グループ名
            auto_commands: 自動実行コマンド
            
        Returns:
            追加成功時True、失敗時False
        """
        # 同名グループが存在しないかチェック
        if self.get_group(group_name):
            return False
        
        new_group = {
            "name": group_name,
            "auto_commands": auto_commands or [],
            "devices": []
        }
        
        self.config["groups"].append(new_group)
        return self.save_config()
    
    def remove_group(self, group_name: str) -> bool:
        """
        グループを削除
        
        Args:
            group_name: グループ名
            
        Returns:
            削除成功時True、失敗時False
        """
        groups = self.config.get("groups", [])
        self.config["groups"] = [g for g in groups if g["name"] != group_name]
        return self.save_config()
    
    def rename_group(self, old_name: str, new_name: str) -> bool:
        """
        グループ名を変更
        
        Args:
            old_name: 変更前のグループ名
            new_name: 変更後のグループ名
            
        Returns:
            変更成功時True、失敗時False
        """
        # 変更対象のグループを取得
        group = self.get_group(old_name)
        if not group:
            print(f"エラー: グループ '{old_name}' が見つかりません")
            return False
        
        # 新しい名前が既に存在しないかチェック
        if self.get_group(new_name):
            print(f"エラー: グループ '{new_name}' は既に存在します")
            return False
        
        # グループ名を変更
        group["name"] = new_name
        
        # 設定を保存
        result = self.save_config()
        if result:
            print(f"[INFO] グループ名を '{old_name}' から '{new_name}' に変更しました")
        
        return result
    
    def set_group_auto_commands(self, group_name: str, commands: List[str]) -> bool:
        """
        グループの自動実行コマンドを設定

        get_group() が返す参照に依存せず、groups を自分で走査して書き換える。
        将来 get_group() がコピーを返すようになっても壊れないようにするため。

        Args:
            group_name: グループ名
            commands: 自動実行コマンドのリスト（空リスト可）

        Returns:
            設定成功時True、グループが無ければFalse
        """
        for group in self.config.get("groups", []):
            if group.get("name") == group_name:
                group["auto_commands"] = list(commands)
                return self.save_config()

        print(f"エラー: グループ '{group_name}' が見つかりません")
        return False
    
    def get_update_settings(self) -> Dict:
        """
        更新設定を取得
        
        Returns:
            更新設定の辞書
        """
        if "update_settings" not in self.config:
            # 更新設定が存在しない場合はデフォルトを追加
            self.config["update_settings"] = {
                "check_on_startup": True,
                "skipped_version": None,
                "last_check": None,
                "github_token": None
            }
            self.save_config()
        
        stored = self.config.get("update_settings")
        return stored if isinstance(stored, dict) else {}
    
    def update_update_settings(self, settings: Dict) -> bool:
        """
        更新設定を更新
        
        Args:
            settings: 更新する設定
        
        Returns:
            更新成功時True、失敗時False
        """
        if not isinstance(self.config.get("update_settings"), dict):
            self.config["update_settings"] = {}
        
        self.config["update_settings"].update(settings)
        return self.save_config()
    
    def get_check_on_startup(self) -> bool:
        """起動時の更新チェック設定を取得

        手編集で bool 以外が入っていても bool を返す。生値を
        QCheckBox.setChecked() へ渡すと TypeError になるため。
        """
        value = self.get_update_settings().get("check_on_startup", True)
        return value if isinstance(value, bool) else True
    
    def set_check_on_startup(self, enabled: bool) -> bool:
        """起動時の更新チェック設定を変更"""
        return self.update_update_settings({"check_on_startup": enabled})
    
    def get_skipped_version(self) -> Optional[str]:
        """スキップ中のバージョンを取得（文字列でなければ None）"""
        value = self.get_update_settings().get("skipped_version")
        return value if isinstance(value, str) and value else None
    
    def set_skipped_version(self, version: Optional[str]) -> bool:
        """スキップするバージョンを設定"""
        return self.update_update_settings({"skipped_version": version})
    
    def get_last_check_time(self) -> Optional[str]:
        """最終更新チェック時刻を取得"""
        return self.get_update_settings().get("last_check")
    
    def set_last_check_time(self, timestamp: str) -> bool:
        """最終更新チェック時刻を設定"""
        return self.update_update_settings({"last_check": timestamp})
    
    def get_github_token(self) -> Optional[str]:
        """GitHubトークンを取得（プライベートリポジトリ用）"""
        # 環境変数を優先
        import os
        env_token = os.environ.get('GITHUB_TOKEN')
        if env_token:
            return env_token
        
        # 設定ファイルから取得
        return self.get_update_settings().get("github_token")
    
    def set_github_token(self, token: Optional[str]) -> bool:
        """GitHubトークンを設定"""
        return self.update_update_settings({"github_token": token})
    
    def add_device(self, group_name: str, device_info: Dict) -> bool:
        """
        機器を追加
        
        Args:
            group_name: 所属グループ名
            device_info: 機器情報
            
        Returns:
            追加成功時True、失敗時False
        """
        group = self.get_group(group_name)
        if not group:
            return False
        # 機器名は全グループを通して一意。接続の管理も所属グループの検索も
        # 名前だけで行うので、別グループに同名があると、先に見つかった方の
        # auto_commands が送られる
        if self.find_device_group(device_info.get("name", "")) is not None:
            return False
        if is_reserved_device_name(device_info.get("name", "")):
            return False

        before = list(group["devices"])
        group["devices"].append(device_info)
        if self.save_config():
            return True
        group["devices"][:] = before   # 保存できなかったらメモリも戻す
        return False

    def remove_device(self, group_name: str, device_name: str) -> bool:
        """機器を削除"""
        group = self.get_group(group_name)
        if not group:
            return False

        before = list(group["devices"])
        group["devices"] = [d for d in group["devices"] if d["name"] != device_name]
        if self.save_config():
            return True
        # 保存できなかったのにメモリから消したままだと、次の無関係な保存で
        # 機器がディスクから消える
        group["devices"] = before
        return False

    def find_device_group(self, device_name: str) -> Optional[str]:
        """その名前の機器が属するグループ名を返す（無ければ None）"""
        for group in self.get_groups():
            for device in group.get("devices", []):
                if device.get("name") == device_name:
                    return group["name"]
        return None

    def update_device(self, group_name: str, old_name: str,
                      new_group_name: str, device_info: Dict) -> bool:
        """機器を差し替える（改名・グループ移動を含む）。保存は 1 回。

        remove_device → add_device の 2 段階にすると、間の状態がディスクに
        残ったり、片方の保存だけ失敗して機器が消えたり新旧 2 件になったり
        する。差し替えをメモリ上で組んでから 1 回だけ保存し、失敗したら
        メモリも元に戻す。
        """
        source = self.get_group(group_name)
        target = self.get_group(new_group_name)
        if not source or not target:
            return False
        new_name = device_info.get("name", "")
        if is_reserved_device_name(new_name):
            return False
        owner = self.find_device_group(new_name)
        if owner is not None and not (owner == group_name and new_name == old_name):
            return False   # 別の機器の名前
        index = next((i for i, d in enumerate(source["devices"])
                      if d.get("name") == old_name), None)
        if index is None:
            return False

        source_before = list(source["devices"])
        target_before = list(target["devices"])
        if source is target:
            source["devices"][index] = device_info
        else:
            del source["devices"][index]
            target["devices"].append(device_info)
        if self.save_config():
            return True
        source["devices"][:] = source_before
        target["devices"][:] = target_before
        return False
    
    def get_global_macros(self) -> List[Dict]:
        """全体共通マクロ一覧を取得"""
        return self.config.get("global_macros", [])
    
    def add_global_macro(self, macro_name: str, commands: List[str], description: str = "") -> bool:
        """
        全体共通マクロを追加
        
        Args:
            macro_name: マクロ名
            commands: コマンドリスト
            description: マクロの説明
            
        Returns:
            追加成功時True、失敗時False
        """
        # 同名マクロが存在しないかチェック
        if self.get_macro_by_name(macro_name):
            return False
        
        new_macro = {
            "name": macro_name,
            "commands": commands,
            "description": description
        }
        
        if "global_macros" not in self.config:
            self.config["global_macros"] = []
        
        self.config["global_macros"].append(new_macro)
        return self.save_config()
    
    def get_macro_by_name(self, macro_name: str) -> Optional[Dict]:
        """
        指定したマクロを取得
        
        Args:
            macro_name: マクロ名
            
        Returns:
            マクロデータ、見つからない場合None
        """
        for macro in self.config.get("global_macros", []):
            if macro.get("name") == macro_name:
                return macro
        return None
    
    def update_global_macro(self, macro_name: str, commands: List[str], description: str = "") -> bool:
        """
        全体共通マクロを更新
        
        Args:
            macro_name: マクロ名
            commands: コマンドリスト
            description: マクロの説明
            
        Returns:
            更新成功時True、失敗時False
        """
        macro = self.get_macro_by_name(macro_name)
        if not macro:
            return False
        
        macro["commands"] = commands
        macro["description"] = description
        return self.save_config()
    
    def remove_global_macro(self, macro_name: str) -> bool:
        """
        全体共通マクロを削除
        
        Args:
            macro_name: マクロ名
            
        Returns:
            削除成功時True、失敗時False
        """
        if "global_macros" not in self.config:
            return False
        
        macros = self.config["global_macros"]
        self.config["global_macros"] = [m for m in macros if m.get("name") != macro_name]
        return self.save_config()
    
    def get_settings(self) -> Dict:
        """アプリケーション設定を取得"""
        return self.config.get("settings", {})
    
    def update_settings(self, settings: Dict) -> bool:
        """アプリケーション設定を更新"""
        self.config["settings"] = settings
        return self.save_config()
    
    def _settings_section(self, key, create=False):
        """
        settings.<key> を dict として返す

        config.json は手で編集できるため、settings 自体や各セクションが
        dict でなくなっていることがある。そのまま .update() すると
        AttributeError で落ちるので、ここで型を保証する。

        Args:
            key: セクション名
            create: 壊れていた場合に config を作り直すか（書き込み時は True）

        Returns:
            セクションの dict。create=False で壊れていれば空 dict
        """
        settings = self.config.get("settings")
        if not isinstance(settings, dict):
            if not create:
                return {}
            settings = {}
            self.config["settings"] = settings

        section = settings.get(key)
        if not isinstance(section, dict):
            if not create:
                return {}
            section = {}
            settings[key] = section

        return section
    
    def get_server_settings(self, key):
        """サーバー設定 dict を返す（key='tftp_server'/'ftp_server'/'sftp_server'）。"""
        return self._settings_section(key)
    
    def set_server_settings(self, key, values):
        """サーバー設定をマージして保存する。"""
        self._settings_section(key, create=True).update(values)
        return self.save_config()
    
    def move_device(self, source_group_name: str, target_group_name: str, device_name: str) -> bool:
        """
        デバイスをグループ間で移動
        
        Args:
            source_group_name: 移動元グループ名
            target_group_name: 移動先グループ名
            device_name: デバイス名
            
        Returns:
            移動成功時True、失敗時False
        """
        # 移動元グループを取得
        source_group = self.get_group(source_group_name)
        if not source_group:
            print(f"エラー: 移動元グループ '{source_group_name}' が見つかりません")
            return False
        
        # 移動先グループを取得
        target_group = self.get_group(target_group_name)
        if not target_group:
            print(f"エラー: 移動先グループ '{target_group_name}' が見つかりません")
            return False
        
        # 移動対象デバイスを検索
        device_to_move = None
        for device in source_group["devices"]:
            if device["name"] == device_name:
                device_to_move = device
                break
        
        if not device_to_move:
            print(f"エラー: デバイス '{device_name}' が移動元グループに見つかりません")
            return False
        
        # 移動先グループに同名デバイスがないかチェック
        for device in target_group["devices"]:
            if device["name"] == device_name:
                print(f"エラー: デバイス '{device_name}' は移動先グループに既に存在します")
                return False
        
        # デバイスを移動元から削除
        # 保存に失敗したら戻せるよう、両方の一覧を控える（update_device と同じ）
        source_before = list(source_group["devices"])
        target_before = list(target_group["devices"])
        source_group["devices"].remove(device_to_move)
        
        # デバイスを移動先に追加
        target_group["devices"].append(device_to_move)
        
        # 設定を保存
        result = self.save_config()
        if not result:
            # 保存できなかったのに移動したままだと、次の無関係な保存で
            # ディスク側だけが移動した状態になる
            source_group["devices"][:] = source_before
            target_group["devices"][:] = target_before
        if result:
            print(f"[INFO] デバイス '{device_name}' を '{source_group_name}' から '{target_group_name}' に移動しました")
        
        return result