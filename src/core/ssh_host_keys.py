"""SSH ホストキー管理モジュール"""
import os
from pathlib import Path
import paramiko
from typing import Optional, Tuple
from PyQt6.QtWidgets import QMessageBox


from .config_manager import app_data_dir as _app_data_dir


class SSHHostKeyManager:
    """SSHホストキーの管理クラス"""

    def __init__(self):
        """初期化"""
        self.known_hosts_path = self._get_known_hosts_path()
        self.host_keys = paramiko.HostKeys()
        self._load_known_hosts()

    def _get_known_hosts_path(self) -> Path:
        """
        known_hostsファイルのパスを取得

        Returns:
            Path: known_hostsファイルのパス
        """
        # アプリケーション用のknown_hostsファイルを使用
        return _app_data_dir() / "known_hosts"

    def _load_known_hosts(self):
        """known_hostsファイルを読み込み"""
        if self.known_hosts_path.exists():
            try:
                self.host_keys.load(str(self.known_hosts_path))
                print(f"[SSH] known_hosts読み込み完了: {len(self.host_keys)} keys")
            except Exception as e:
                print(f"[SSH] known_hosts読み込みエラー: {e}")

    def verify_host_key(self, hostname: str, port: int, key: paramiko.PKey,
                       parent_widget=None) -> Tuple[bool, Optional[str]]:
        """
        ホストキーを検証

        Args:
            hostname: ホスト名
            port: ポート番号
            key: ホストキー
            parent_widget: 親ウィジェット（ダイアログ表示用）

        Returns:
            Tuple[bool, Optional[str]]: (検証結果, エラーメッセージ)
        """
        # ホスト名とポートの組み合わせでキーを検索
        host_id = f"[{hostname}]:{port}" if port != 22 else hostname

        if host_id in self.host_keys:
            # 既知のホスト - キーを比較
            stored_key = self.host_keys[host_id].get(key.get_name())

            if stored_key is None:
                # キータイプが異なる
                return self._handle_key_type_changed(
                    hostname, port, key, parent_widget
                )

            if stored_key.get_fingerprint() == key.get_fingerprint():
                # キーが一致 - OK
                return True, None
            else:
                # キーが変更されている - 警告
                return self._handle_key_changed(
                    hostname, port, key, stored_key, parent_widget
                )
        else:
            # 初回接続 - ユーザーに確認
            return self._handle_new_host(
                hostname, port, key, parent_widget
            )

    def _handle_new_host(self, hostname: str, port: int, key: paramiko.PKey,
                        parent_widget) -> Tuple[bool, Optional[str]]:
        """
        初回接続時の処理

        Args:
            hostname: ホスト名
            port: ポート番号
            key: ホストキー
            parent_widget: 親ウィジェット

        Returns:
            Tuple[bool, Optional[str]]: (検証結果, エラーメッセージ)
        """
        fingerprint = self._format_fingerprint(key)

        message = f"""初めて接続するホストです。ホストキーの指紋を確認してください。

ホスト: {hostname}:{port}
キータイプ: {key.get_name()}
フィンガープリント (SHA256):
{fingerprint}

このホストを信頼して接続を続けますか？

※「はい」を選択すると、このキーが保存され、次回以降は自動的に検証されます。
※「いいえ」を選択すると、接続がキャンセルされます。"""

        if parent_widget:
            reply = QMessageBox.question(
                parent_widget,
                "SSH ホストキーの確認",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No  # デフォルトは No
            )

            if reply == QMessageBox.StandardButton.Yes:
                # ホストキーを保存
                self._save_host_key(hostname, port, key)
                return True, None
            else:
                return False, "ユーザーによりホストキーが拒否されました"
        else:
            # UIがない場合は自動的に拒否
            return False, "ホストキーを検証できません（UIなし）"

    def _handle_key_changed(self, hostname: str, port: int, new_key: paramiko.PKey,
                           old_key: paramiko.PKey, parent_widget) -> Tuple[bool, Optional[str]]:
        """
        ホストキーが変更された場合の処理

        Args:
            hostname: ホスト名
            port: ポート番号
            new_key: 新しいホストキー
            old_key: 古いホストキー
            parent_widget: 親ウィジェット

        Returns:
            Tuple[bool, Optional[str]]: (検証結果, エラーメッセージ)
        """
        old_fingerprint = self._format_fingerprint(old_key)
        new_fingerprint = self._format_fingerprint(new_key)

        message = f"""⚠️ 警告: ホストキーが変更されています！

ホスト: {hostname}:{port}

これは以下のいずれかを意味する可能性があります：
1. サーバーが再インストールされた
2. 中間者攻撃（MITM）を受けている ⚠️
3. ネットワーク設定が変更された

保存されているキー (SHA256):
{old_fingerprint}

新しいキー (SHA256):
{new_fingerprint}

信頼できる理由がある場合のみ「はい」を選択してください。
不明な場合は「いいえ」を選択し、管理者に確認してください。

このキーを信頼しますか？"""

        if parent_widget:
            reply = QMessageBox.critical(
                parent_widget,
                "⚠️ SSH ホストキー変更の警告",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.Yes:
                # 新しいキーを保存（古いキーを上書き）
                self._save_host_key(hostname, port, new_key)
                return True, None
            else:
                return False, "ホストキーの変更が拒否されました"
        else:
            return False, "ホストキーが変更されています（自動検証失敗）"

    def _handle_key_type_changed(self, hostname: str, port: int,
                                 key: paramiko.PKey, parent_widget) -> Tuple[bool, Optional[str]]:
        """
        キータイプが変更された場合の処理

        Args:
            hostname: ホスト名
            port: ポート番号
            key: 新しいホストキー
            parent_widget: 親ウィジェット

        Returns:
            Tuple[bool, Optional[str]]: (検証結果, エラーメッセージ)
        """
        fingerprint = self._format_fingerprint(key)

        message = f"""ホストキーのタイプが変更されています。

ホスト: {hostname}:{port}
新しいキータイプ: {key.get_name()}
フィンガープリント (SHA256):
{fingerprint}

サーバー設定が変更された可能性があります。
このキーを信頼しますか？"""

        if parent_widget:
            reply = QMessageBox.warning(
                parent_widget,
                "SSH ホストキータイプの変更",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.Yes:
                self._save_host_key(hostname, port, key)
                return True, None
            else:
                return False, "キータイプの変更が拒否されました"
        else:
            return False, "キータイプが変更されています（自動検証失敗）"

    def _save_host_key(self, hostname: str, port: int, key: paramiko.PKey):
        """
        ホストキーを保存

        Args:
            hostname: ホスト名
            port: ポート番号
            key: ホストキー
        """
        host_id = f"[{hostname}]:{port}" if port != 22 else hostname
        self.host_keys.add(host_id, key.get_name(), key)

        try:
            self.host_keys.save(str(self.known_hosts_path))
            print(f"[SSH] ホストキー保存: {host_id}")
        except Exception as e:
            print(f"[SSH] ホストキー保存エラー: {e}")

    def _format_fingerprint(self, key: paramiko.PKey) -> str:
        """
        ホストキーのフィンガープリントをフォーマット

        Args:
            key: ホストキー

        Returns:
            str: フォーマットされたフィンガープリント
        """
        import hashlib
        import base64

        fingerprint_bytes = hashlib.sha256(key.asbytes()).digest()
        fingerprint = base64.b64encode(fingerprint_bytes).decode('ascii').rstrip('=')

        return f"SHA256:{fingerprint}"

    def remove_host_key(self, hostname: str, port: int = 22):
        """
        ホストキーを削除

        Args:
            hostname: ホスト名
            port: ポート番号
        """
        host_id = f"[{hostname}]:{port}" if port != 22 else hostname

        if host_id in self.host_keys:
            del self.host_keys[host_id]
            try:
                self.host_keys.save(str(self.known_hosts_path))
                print(f"[SSH] ホストキー削除: {host_id}")
            except Exception as e:
                print(f"[SSH] ホストキー削除エラー: {e}")
