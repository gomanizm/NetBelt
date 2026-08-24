"""設定ファイル管理モジュール"""
import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from .crypto import PasswordCrypto

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
    
    def _notify_undecryptable(self) -> None:
        """復号できなかったパスワードがあれば知らせる。"""
        n = getattr(self, "_undecryptable_count", 0)
        if n:
            print(f"[Config] {n}件のパスワードを復号できませんでした。"
                  "別の Windows アカウント/PC で保存された設定の可能性があります。"
                  "該当機器のパスワードは再入力してください。"
                  "（設定ファイル内の元の値は保護されており、上書きされません）")

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
            
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(save_config, f, indent=2, ensure_ascii=False)
            
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
        
        return self.config.get("update_settings", {})
    
    def update_update_settings(self, settings: Dict) -> bool:
        """
        更新設定を更新
        
        Args:
            settings: 更新する設定
        
        Returns:
            更新成功時True、失敗時False
        """
        if "update_settings" not in self.config:
            self.config["update_settings"] = {}
        
        self.config["update_settings"].update(settings)
        return self.save_config()
    
    def get_check_on_startup(self) -> bool:
        """起動時の更新チェック設定を取得"""
        return self.get_update_settings().get("check_on_startup", True)
    
    def set_check_on_startup(self, enabled: bool) -> bool:
        """起動時の更新チェック設定を変更"""
        return self.update_update_settings({"check_on_startup": enabled})
    
    def get_skipped_version(self) -> Optional[str]:
        """スキップされたバージョンを取得"""
        return self.get_update_settings().get("skipped_version")
    
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
        
        group["devices"].append(device_info)
        return self.save_config()
    
    def remove_device(self, group_name: str, device_name: str) -> bool:
        """機器を削除"""
        group = self.get_group(group_name)
        if not group:
            return False
        
        group["devices"] = [d for d in group["devices"] if d["name"] != device_name]
        return self.save_config()
    
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
    
    def get_server_settings(self, key):
        """サーバー設定 dict を返す（key='tftp_server'/'ftp_server'/'sftp_server'）。"""
        return self.config.get("settings", {}).get(key, {})
    
    def set_server_settings(self, key, values):
        """サーバー設定をマージして保存する。"""
        settings = self.config.setdefault("settings", {})
        settings.setdefault(key, {}).update(values)
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
        source_group["devices"].remove(device_to_move)
        
        # デバイスを移動先に追加
        target_group["devices"].append(device_to_move)
        
        # 設定を保存
        result = self.save_config()
        if result:
            print(f"[INFO] デバイス '{device_name}' を '{source_group_name}' から '{target_group_name}' に移動しました")
        
        return result