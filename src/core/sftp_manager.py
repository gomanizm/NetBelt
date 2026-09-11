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
    # ワーカーから GUI スレッドへ「一覧が取れた」を運ぶ内部用。current_path の
    # 更新と file_list_ready の発火を同じスレッド・同じ順序で行うために挟む
    _listing_done = pyqtSignal(str, list)
    # 転送進捗 (転送済みバイト数, 全体バイト数)
    # int で宣言すると C++ の 32bit int に対応し、2GiB を超えるバイト数が
    # 例外も出さずに黙って丸められる（負値や桁落ちした値になる）。
    # NX-OS / IOS-XE のイメージはこの用途そのものなので object で渡す。
    transfer_progress = pyqtSignal(object, object)
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
        # SFTPClient は1本のチャンネルを共有するので、同時に叩くと壊れる。
        # 背景スレッドはこのロックで直列化する。
        self._sftp_lock = threading.Lock()
        self.sftp_client: Optional[paramiko.SFTPClient] = None
        self.ssh_client: Optional[paramiko.SSHClient] = None
        self.is_connected = False
        self.current_path = "/"
        self._listing_done.connect(self._on_listing_done)
    
    # GUI スレッドから直接呼ぶ操作が、転送の終わりを待つ最大時間。
    # 長く待つと転送中ずっと画面が固まるので、短く切って諦める。
    _GUI_WAIT_SECONDS = 0.5

    # 切断のときに、進行中の転送が手を離すのを待つ上限。GUI スレッドから
    # 呼ばれるため、長く待つとアプリが終了できなくなる。
    _DISCONNECT_WAIT_SECONDS = 3.0

    # 共有チャンネルが機器の応答を待つ上限（秒）。None のままだと、機器が
    # SFTP サブシステムだけ黙ったとき（TCP は生きている）に mkdir や
    # normalize が無期限に止まり、GUI スレッドから呼ばれるのでアプリ全体が
    # 固まる。転送中は 1 回の recv/send がこの時間ゼロのまま止まったときに
    # 限って切れる（データが流れている限り切れない）。
    CHANNEL_TIMEOUT_SECONDS = 30.0

    def _acquire_for_gui(self, what: str) -> bool:
        """GUI スレッドから使うためにロックを取る（取れなければ False）

        取れないのは転送中ということ。待たせると画面が固まるので、
        実行できないことを伝えて戻る。
        """
        if self._sftp_lock.acquire(timeout=self._GUI_WAIT_SECONDS):
            # 待っているあいだに切断されたら、取れても意味がない
            if not self.is_connected or self.sftp_client is None:
                self._sftp_lock.release()
                self.error_occurred.emit("SFTP接続がありません")
                return False
            return True
        self.error_occurred.emit(
            f"転送中のため{what}を実行できません。完了してからやり直してください。")
        return False

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
            # 応答待ちに期限を入れる。ここより後の normalize を含め、
            # このチャンネル越しの全操作が期限切れで socket.timeout を
            # 投げるようになり、各操作の except がエラー通知へ変える
            self.sftp_client.get_channel().settimeout(self.CHANNEL_TIMEOUT_SECONDS)

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
        """SFTP接続を切断

        閉じるのはロックの中で行う。転送や一覧取得の最中に閉じると、
        進行中のスレッドが閉じられたクライアントを触ることになる。
        先に is_connected を落として新しい操作を止め、いま走っている
        ものが手を離すまで待ってから閉じる。
        """
        # 新しい操作をここで止める（各メソッドが先頭で見ている）
        self.is_connected = False

        # 空くのを無期限には待たない。タブを閉じるときやアプリ終了時に
        # GUI スレッドから呼ばれるので、応答しない機器への転送中だと
        # 待った分だけアプリが固まる（終了できなくなる）。
        acquired = self._sftp_lock.acquire(timeout=self._DISCONNECT_WAIT_SECONDS)
        try:
            client, self.sftp_client = self.sftp_client, None
            self.ssh_client = None
        finally:
            if acquired:
                self._sftp_lock.release()

        if client and acquired:
            try:
                client.close()
            except Exception:
                pass
        elif client:
            # 転送が掴んだまま。ここで閉じると、その最中のスレッドが
            # 閉じたハンドルを触ることになる。参照だけ手放し、後片付けは
            # SSH 接続の終了に任せる。
            print("[SFTP] 転送中のため接続を閉じきれませんでした"
                  "（SSH の切断で解放されます）")
        self.disconnected.emit()
    
    def _on_listing_done(self, path: str, file_list: list):
        """一覧が取れたときの GUI スレッド側の処理。

        current_path の更新と file_list_ready の発火を、同じスレッドで
        この順に行う。受け手（パネル）はスロットの中で get_current_path()
        を見て表示を組み立てるので、通知より先に更新されている必要がある。
        """
        self.current_path = path
        self.file_list_ready.emit(file_list)

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
                # ディレクトリ一覧を取得（転送中なら空くまで待つ）
                with self._sftp_lock:
                    # 待っているあいだに切断されたかもしれない。取得前の
                    # 確認だけでは足りない（起きたら None を触ることになる）。
                    if not self.is_connected or self.sftp_client is None:
                        return
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
                
                # current_path はここで書かない。書いてから emit すると、
                # シグナルが GUI へ届くまでの間「場所は新しい、画面は古い一覧」
                # になり、その窓で始めた操作が見ていないディレクトリへ飛ぶ。
                # 更新と通知を GUI スレッドで同じ順序に行う
                self._listing_done.emit(path, file_list)
                
            except Exception as e:
                self.error_occurred.emit(f"ディレクトリ一覧取得エラー: {str(e)}")
        
        # バックグラウンドスレッドで実行
        threading.Thread(target=list_thread, daemon=True).start()
    
    def _remote_exists(self, remote_path: str) -> bool:
        """リモートに remote_path が存在するかを stat で確かめる（ロック内で呼ぶ）

        見つからないときの応答は機器によって異なる（NO_SUCH_FILE 以外を
        返すものもある）ので、stat の失敗はすべて「無い」と扱う。
        """
        try:
            self.sftp_client.stat(remote_path)
        except IOError:
            return False
        return True

    def upload_file(self, local_path: str, remote_path: str = None,
                    overwrite: bool = False):
        """
        ファイルをアップロード（バックグラウンド）
        
        Args:
            local_path: ローカルファイルパス
            remote_path: リモートファイルパス（Noneの場合は現在のディレクトリにファイル名のみで保存）
            overwrite: True なら既存のリモートファイルを置き換える。False の
                ときは送る直前にリモートを確かめ、既にあれば送らずにエラーで
                知らせる。パネルの上書き確認は「最後に観測した一覧」で判定
                しており、一覧が送信先と食い違っていると既存を見落とすため、
                確認を経ていない送信はここで止める
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
        
        # リモートも最終名へ直接書かない。put() は先にリモートを切り詰めるので、
        # 切断や容量不足で機器側に途中までの設定ファイルが本来の名前で残る。
        # 同じディレクトリの一時名へ送り、成功してから置き換える
        remote_dir, _, remote_name = remote_path.rpartition("/")
        # スラッシュを含まない相対名なら、一時名も相対のまま（ルート直下に
        # しない。OpenSSH 系の機器はルートに書けないことが多い）
        tmp_remote = (remote_dir + "/" if remote_dir else "") + ".%s.netbelt-part" % remote_name
        # 最終名を消したあとで置き換えに失敗した場合は、一時名が唯一の完全な
        # 写しになるので消さない
        keep_tmp = [False]

        def upload_thread():
            try:
                # ファイルサイズを取得
                file_size = os.path.getsize(local_path)
                transferred = [0]  # リスト内の値を使うことでクロージャ内で更新可能
                
                def progress_callback(transferred_bytes, total_bytes):
                    """転送進捗コールバック"""
                    transferred[0] = transferred_bytes
                    self.transfer_progress.emit(transferred_bytes, total_bytes)
                
                # アップロード実行。まとめてドロップされた分はここで
                # 順番待ちになる（同時に走らせるとチャンネルが壊れる）
                with self._sftp_lock:
                    # 待っているあいだに切断されたかもしれない。取得前の
                    # 確認だけでは足りない（起きたら None を触ることになる）。
                    if not self.is_connected or self.sftp_client is None:
                        return
                    # 確認を経ていない送信は、送る直前の実際の状態で判定する。
                    # ロック内なので、先行する転送の結果も見える
                    if not overwrite and self._remote_exists(remote_path):
                        self.error_occurred.emit(
                            f"リモートに '{remote_name}' が既にあります。上書きの確認を"
                            "経ていないので送りませんでした。一覧を更新してからやり直してください")
                        return
                    self.sftp_client.put(local_path, tmp_remote,
                                         callback=progress_callback)
                    # 全部送れてから最終名へ。posix_rename（OpenSSH 拡張）は
                    # 既存を上書きできる。無いサーバでは、まず rename を試し、
                    # 既存があって失敗したときだけ消してからもう一度 rename
                    # する（先に消すと、rename に失敗した瞬間に元が消える）
                    try:
                        self.sftp_client.posix_rename(tmp_remote, remote_path)
                    except (AttributeError, IOError):
                        try:
                            self.sftp_client.rename(tmp_remote, remote_path)
                        except IOError:
                            try:
                                self.sftp_client.remove(remote_path)
                            except IOError:
                                pass
                            try:
                                self.sftp_client.rename(tmp_remote, remote_path)
                            except IOError as e:
                                keep_tmp[0] = True
                                raise IOError(
                                    "最終名への置き換えに失敗しました。転送済みの内容は"
                                    "機器の一時名 %s に残っています: %s" % (tmp_remote, e))
                
                # 完了通知
                self.transfer_complete.emit(f"アップロード完了: {os.path.basename(local_path)}")
                
                # ディレクトリ一覧を更新
                self.list_directory(self.current_path)
                
            except Exception as e:
                # 送りかけの一時ファイルを機器に残さない（できる範囲で。切断後は
                # 消せず、機器側に .<名前>.netbelt-part が残る）。最終名のファイル
                # には触っていない。置き換えの途中で失敗した場合は、一時名が唯一の
                # 完全な写しなので消さない
                if not keep_tmp[0]:
                    try:
                        with self._sftp_lock:
                            if self.is_connected and self.sftp_client is not None:
                                self.sftp_client.remove(tmp_remote)
                    except Exception:
                        pass
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
        
        # 最終の保存先へ直接書かない。paramiko の get() はリモートを読む前に
        # ローカルを 'wb' で開くので、リモート側で消えていただけでも既存の
        # 正常なバックアップが 0 バイトになり、途中で切れれば部分ファイルが
        # 本来の名前で残る。同じディレクトリの一時名へ落として置き換える
        # 一時名はダウンロードごとに一意にする（同じ保存先へ続けて落とすと、
        # 同じ一時名の取り合いで片方が誤って失敗する）
        import tempfile
        try:
            fd, tmp_local = tempfile.mkstemp(
                prefix=os.path.basename(local_path) + ".", suffix=".netbelt-part",
                dir=os.path.dirname(os.path.abspath(local_path)))
            os.close(fd)
        except OSError as e:
            # ここは GUI スレッド。例外を上げるとスロットの外へ抜けるので、
            # 転送の失敗と同じ経路で知らせる
            self.error_occurred.emit(f"ダウンロードエラー: {str(e)}")
            return

        def download_thread():
            try:
                # ダウンロード実行
                def progress_callback(transferred_bytes, total_bytes):
                    """転送進捗コールバック"""
                    self.transfer_progress.emit(transferred_bytes, total_bytes)
                
                with self._sftp_lock:
                    # 待っているあいだに切断されたかもしれない。取得前の
                    # 確認だけでは足りない（起きたら None を触ることになる）。
                    if not self.is_connected or self.sftp_client is None:
                        return
                    self.sftp_client.get(remote_path, tmp_local,
                                         callback=progress_callback)
                
                # 全部落とせてから最終名へ（同じディレクトリなので原子的）
                os.replace(tmp_local, local_path)
                # 完了通知
                self.transfer_complete.emit(f"ダウンロード完了: {os.path.basename(remote_path)}")
                
            except Exception as e:
                # 失敗した転送の残骸を消す。既存の保存先には触っていない
                try:
                    os.remove(tmp_local)
                except OSError:
                    pass
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
        
        if not self._acquire_for_gui("ディレクトリ作成"):
            return
        try:
            self.sftp_client.mkdir(path)
        except Exception as e:
            self.error_occurred.emit(f"ディレクトリ作成エラー: {str(e)}")
            return
        finally:
            self._sftp_lock.release()
        self.transfer_complete.emit(f"ディレクトリ作成: {os.path.basename(path)}")
        # ディレクトリ一覧を更新（ロックを離してから）
        self.list_directory(self.current_path)
    
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
        
        if not self._acquire_for_gui("削除"):
            return
        try:
            if is_dir:
                self.sftp_client.rmdir(path)
            else:
                self.sftp_client.remove(path)
        except Exception as e:
            self.error_occurred.emit(f"削除エラー: {str(e)}")
            return
        finally:
            self._sftp_lock.release()
        self.transfer_complete.emit(f"削除完了: {os.path.basename(path)}")
        # ディレクトリ一覧を更新（ロックを離してから）
        self.list_directory(self.current_path)
    
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
        
        if not self._acquire_for_gui("名前変更"):
            return
        try:
            self.sftp_client.rename(old_path, new_path)
        except Exception as e:
            self.error_occurred.emit(f"名前変更エラー: {str(e)}")
            return
        finally:
            self._sftp_lock.release()
        self.transfer_complete.emit(f"名前変更完了: {os.path.basename(new_path)}")
        # ディレクトリ一覧を更新（ロックを離してから）
        self.list_directory(self.current_path)
    
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
        
        if not self._acquire_for_gui("パーミッション変更"):
            return
        try:
            self.sftp_client.chmod(path, mode)
        except Exception as e:
            self.error_occurred.emit(f"パーミッション変更エラー: {str(e)}")
            return
        finally:
            self._sftp_lock.release()
        self.transfer_complete.emit(f"パーミッション変更完了: {os.path.basename(path)}")
        # ディレクトリ一覧を更新（ロックを離してから）
        self.list_directory(self.current_path)
    
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
        
        if not self._acquire_for_gui("ディレクトリ移動"):
            return
        try:
            # パスを正規化
            normalized_path = self.sftp_client.normalize(path)
        except Exception as e:
            self.error_occurred.emit(f"ディレクトリ変更エラー: {str(e)}")
            return
        finally:
            self._sftp_lock.release()
        # ディレクトリ一覧を取得（これによりパスの存在も確認）
        self.list_directory(normalized_path)
    
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
            mode: ファイルモード。サーバが permissions を返さなければ None

        Returns:
            bool: ディレクトリの場合True。モードが不明ならファイル扱い
        """
        import stat
        # SFTP v3 の permissions は省略可能。None を S_ISDIR に渡すと
        # TypeError で一覧全体が失敗し、正常な項目まで画面から消える
        if mode is None:
            return False
        return stat.S_ISDIR(mode)
    
    @staticmethod
    def _format_permissions(mode: int) -> str:
        """
        パーミッションを文字列形式に変換（例: -rwxr-xr-x）
        
        Args:
            mode: ファイルモード。サーバが permissions を返さなければ None

        Returns:
            str: パーミッション文字列。モードが不明なら None（表示側が
                「不明」と出す。'---------' にすると権限の無いファイルと
                区別がつかない）
        """
        import stat

        if mode is None:
            return None

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