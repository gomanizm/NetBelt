"""SFTP接続管理"""
import os
import threading
from typing import List, Dict, Optional, Callable
from PyQt6.QtCore import QObject, pyqtSignal
import paramiko


class SFTPManager(QObject):
    """SFTPファイル転送を管理するクラス"""
    
    # シグナル定義
    file_list_ready = pyqtSignal(list)  # ファイル一覧取得完了 [(name, size, mtime, mode, is_dir), ...]
    transfer_progress = pyqtSignal(int, int)  # 転送進捗 (転送済みバイト数, 全体バイト数)
    transfer_complete = pyqtSignal(str)  # 転送完了 (メッセージ)
    error_occurred = pyqtSignal(str)  # エラー発生
    connected = pyqtSignal()  # 接続成功
    disconnected = pyqtSignal()  # 切断
    
    def __init__(self, parent=None):
        """
        初期化
        
        Args:
            parent: 親オブジェクト
        """
        super().__init__(parent)
        self.sftp_client: Optional[paramiko.SFTPClient] = None
        self.ssh_client: Optional[paramiko.SSHClient] = None
        self.is_connected = False
        self.current_path = "/"
    
    def connect(self, ssh_client: paramiko.SSHClient) -> bool:
        """
        SFTP接続を開始（既存のSSHクライアントを使用）
        
        Args:
            ssh_client: 既に接続済みのparamiko.SSHClient
            
        Returns:
            bool: 接続成功時True
        """
        try:
            if not ssh_client:
                self.error_occurred.emit("SSHクライアントが無効です")
                return False
            
            # SSHクライアントからSFTPセッションを取得
            self.ssh_client = ssh_client
            self.sftp_client = ssh_client.open_sftp()
            
            # ホームディレクトリを取得
            try:
                self.current_path = self.sftp_client.normalize('.')
            except Exception:
                self.current_path = "/"
            
            self.is_connected = True
            self.connected.emit()
            return True
            
        except Exception as e:
            self.error_occurred.emit(f"SFTP接続エラー: {str(e)}")
            return False
    
    def disconnect(self):
        """SFTP接続を切断"""
        self.is_connected = False
        
        if self.sftp_client:
            try:
                self.sftp_client.close()
            except Exception:
                pass
            self.sftp_client = None
        
        self.ssh_client = None
        self.disconnected.emit()
    
    def list_directory(self, path: str = None):
        """
        ディレクトリ内のファイル一覧を取得（バックグラウンド）
        
        Args:
            path: ディレクトリパス（Noneの場合は現在のパス）
        """
        if not self.is_connected or not self.sftp_client:
            self.error_occurred.emit("SFTP接続がありません")
            return
        
        if path is None:
            path = self.current_path
        
        def list_thread():
            try:
                # ディレクトリ一覧を取得
                items = self.sftp_client.listdir_attr(path)
                
                # ファイル情報をリストに変換
                file_list = []
                for item in items:
                    is_dir = self._is_directory(item.st_mode)
                    file_list.append({
                        'name': item.filename,
                        'size': item.st_size if not is_dir else 0,
                        'mtime': item.st_mtime,
                        'mode': item.st_mode,
                        'is_dir': is_dir,
                        'permissions': self._format_permissions(item.st_mode)
                    })
                
                # 名前でソート（ディレクトリが先）
                file_list.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
                
                # 現在のパスを更新
                self.current_path = path
                
                # シグナルを発火
                self.file_list_ready.emit(file_list)
                
            except Exception as e:
                self.error_occurred.emit(f"ディレクトリ一覧取得エラー: {str(e)}")
        
        # バックグラウンドスレッドで実行
        threading.Thread(target=list_thread, daemon=True).start()
    
    def upload_file(self, local_path: str, remote_path: str = None):
        """
        ファイルをアップロード（バックグラウンド）
        
        Args:
            local_path: ローカルファイルパス
            remote_path: リモートファイルパス（Noneの場合は現在のディレクトリにファイル名のみで保存）
        """
        if not self.is_connected or not self.sftp_client:
            self.error_occurred.emit("SFTP接続がありません")
            return
        
        if not os.path.exists(local_path):
            self.error_occurred.emit(f"ファイルが見つかりません: {local_path}")
            return
        
        # リモートパスが指定されていない場合はファイル名のみで保存
        if remote_path is None:
            filename = os.path.basename(local_path)
            remote_path = f"{self.current_path}/{filename}"
        
        def upload_thread():
            try:
                # ファイルサイズを取得
                file_size = os.path.getsize(local_path)
                transferred = [0]  # リスト内の値を使うことでクロージャ内で更新可能
                
                def progress_callback(transferred_bytes, total_bytes):
                    """転送進捗コールバック"""
                    transferred[0] = transferred_bytes
                    self.transfer_progress.emit(transferred_bytes, total_bytes)
                
                # アップロード実行
                self.sftp_client.put(local_path, remote_path, callback=progress_callback)
                
                # 完了通知
                self.transfer_complete.emit(f"アップロード完了: {os.path.basename(local_path)}")
                
                # ディレクトリ一覧を更新
                self.list_directory(self.current_path)
                
            except Exception as e:
                self.error_occurred.emit(f"アップロードエラー: {str(e)}")
        
        # バックグラウンドスレッドで実行
        threading.Thread(target=upload_thread, daemon=True).start()
    
    def download_file(self, remote_path: str, local_path: str):
        """
        ファイルをダウンロード（バックグラウンド）
        
        Args:
            remote_path: リモートファイルパス
            local_path: ローカルファイルパス
        """
        if not self.is_connected or not self.sftp_client:
            self.error_occurred.emit("SFTP接続がありません")
            return
        
        def download_thread():
            try:
                # ダウンロード実行
                def progress_callback(transferred_bytes, total_bytes):
                    """転送進捗コールバック"""
                    self.transfer_progress.emit(transferred_bytes, total_bytes)
                
                self.sftp_client.get(remote_path, local_path, callback=progress_callback)
                
                # 完了通知
                self.transfer_complete.emit(f"ダウンロード完了: {os.path.basename(remote_path)}")
                
            except Exception as e:
                self.error_occurred.emit(f"ダウンロードエラー: {str(e)}")
        
        # バックグラウンドスレッドで実行
        threading.Thread(target=download_thread, daemon=True).start()
    
    def create_directory(self, path: str):
        """
        ディレクトリを作成
        
        Args:
            path: ディレクトリパス
        """
        if not self.is_connected or not self.sftp_client:
            self.error_occurred.emit("SFTP接続がありません")
            return
        
        try:
            self.sftp_client.mkdir(path)
            self.transfer_complete.emit(f"ディレクトリ作成: {os.path.basename(path)}")
            # ディレクトリ一覧を更新
            self.list_directory(self.current_path)
        except Exception as e:
            self.error_occurred.emit(f"ディレクトリ作成エラー: {str(e)}")
    
    def delete_item(self, path: str, is_dir: bool = False):
        """
        ファイルまたはディレクトリを削除
        
        Args:
            path: ファイル/ディレクトリパス
            is_dir: ディレクトリの場合True
        """
        if not self.is_connected or not self.sftp_client:
            self.error_occurred.emit("SFTP接続がありません")
            return
        
        try:
            if is_dir:
                self.sftp_client.rmdir(path)
            else:
                self.sftp_client.remove(path)
            
            self.transfer_complete.emit(f"削除完了: {os.path.basename(path)}")
            # ディレクトリ一覧を更新
            self.list_directory(self.current_path)
        except Exception as e:
            self.error_occurred.emit(f"削除エラー: {str(e)}")
    
    def rename_item(self, old_path: str, new_path: str):
        """
        ファイル/ディレクトリ名を変更
        
        Args:
            old_path: 元のパス
            new_path: 新しいパス
        """
        if not self.is_connected or not self.sftp_client:
            self.error_occurred.emit("SFTP接続がありません")
            return
        
        try:
            self.sftp_client.rename(old_path, new_path)
            self.transfer_complete.emit(f"名前変更完了: {os.path.basename(new_path)}")
            # ディレクトリ一覧を更新
            self.list_directory(self.current_path)
        except Exception as e:
            self.error_occurred.emit(f"名前変更エラー: {str(e)}")
    
    def change_permissions(self, path: str, mode: int):
        """
        パーミッションを変更
        
        Args:
            path: ファイル/ディレクトリパス
            mode: パーミッション（8進数、例: 0o755）
        """
        if not self.is_connected or not self.sftp_client:
            self.error_occurred.emit("SFTP接続がありません")
            return
        
        try:
            self.sftp_client.chmod(path, mode)
            self.transfer_complete.emit(f"パーミッション変更完了: {os.path.basename(path)}")
            # ディレクトリ一覧を更新
            self.list_directory(self.current_path)
        except Exception as e:
            self.error_occurred.emit(f"パーミッション変更エラー: {str(e)}")
    
    def get_current_path(self) -> str:
        """
        現在のディレクトリパスを取得
        
        Returns:
            str: 現在のディレクトリパス
        """
        return self.current_path
    
    def change_directory(self, path: str):
        """
        ディレクトリを変更
        
        Args:
            path: 移動先ディレクトリパス
        """
        if not self.is_connected or not self.sftp_client:
            self.error_occurred.emit("SFTP接続がありません")
            return
        
        try:
            # パスを正規化
            normalized_path = self.sftp_client.normalize(path)
            # ディレクトリ一覧を取得（これによりパスの存在も確認）
            self.list_directory(normalized_path)
        except Exception as e:
            self.error_occurred.emit(f"ディレクトリ変更エラー: {str(e)}")
    
    def get_parent_directory(self) -> str:
        """
        親ディレクトリのパスを取得
        
        Returns:
            str: 親ディレクトリパス
        """
        if self.current_path == "/":
            return "/"
        return os.path.dirname(self.current_path)
    
    @staticmethod
    def _is_directory(mode: int) -> bool:
        """
        モードからディレクトリかどうかを判定
        
        Args:
            mode: ファイルモード
            
        Returns:
            bool: ディレクトリの場合True
        """
        import stat
        return stat.S_ISDIR(mode)
    
    @staticmethod
    def _format_permissions(mode: int) -> str:
        """
        パーミッションを文字列形式に変換（例: -rwxr-xr-x）
        
        Args:
            mode: ファイルモード
            
        Returns:
            str: パーミッション文字列
        """
        import stat
        
        # ファイルタイプ
        if stat.S_ISDIR(mode):
            perm_str = 'd'
        elif stat.S_ISLNK(mode):
            perm_str = 'l'
        else:
            perm_str = '-'
        
        # オーナー権限
        perm_str += 'r' if mode & stat.S_IRUSR else '-'
        perm_str += 'w' if mode & stat.S_IWUSR else '-'
        perm_str += 'x' if mode & stat.S_IXUSR else '-'
        
        # グループ権限
        perm_str += 'r' if mode & stat.S_IRGRP else '-'
        perm_str += 'w' if mode & stat.S_IWGRP else '-'
        perm_str += 'x' if mode & stat.S_IXGRP else '-'
        
        # その他権限
        perm_str += 'r' if mode & stat.S_IROTH else '-'
        perm_str += 'w' if mode & stat.S_IWOTH else '-'
        perm_str += 'x' if mode & stat.S_IXOTH else '-'
        
        return perm_str