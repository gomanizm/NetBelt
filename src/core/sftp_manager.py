"""SFTP接続管理"""
import os
import posixpath
import threading
import uuid
from typing import List, Dict, Optional, Callable
from PyQt6.QtCore import QObject, pyqtSignal
import paramiko
from paramiko.sftp import (CMD_HANDLE, CMD_OPEN, SFTP_FLAG_CREATE, SFTP_FLAG_EXCL,
                           SFTP_FLAG_WRITE)


class _DroppedConnection(IOError):
    """接続が切れた失敗を、案内文で包み直したあとも見分けるための印

    upload_file は改名の失敗を「どう確かめるか」を添えた IOError に包んで
    上へ送るので、包んだ時点で元の例外の型が消える。_fail が接続を畳む
    判断をできるよう、原因が切断・壊れた応答だったものだけこの型にする。
    """


# paramiko がトランスポートの死んだ読み取り・壊れた応答で上げる例外。
# OSError の仲間ではないので、IOError だけ見ていると取りこぼす
_DROPPED_CONNECTION_ERRORS = (EOFError, paramiko.SSHException,
                              paramiko.SFTPError, _DroppedConnection)


def _is_dropped_connection(e: Exception) -> bool:
    """接続が切れた・応答が壊れたと分かる失敗か"""
    return isinstance(e, _DROPPED_CONNECTION_ERRORS)


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

    def _fail(self, prefix: str, e: Exception):
        """操作の失敗を通知する。接続が使えなくなったと分かるなら接続も畳む

        期限で戻ったあとも要求と応答はずれたままなので、同じチャンネルの
        以後の操作は失敗し続ける。それでも接続中のままだと、操作のたびに
        期限ぶん画面が固まり、しかも socket.timeout は str が空なので
        理由の無いエラーだけが並ぶ。使用不能と分かる形にして再接続を促す。

        切断・壊れた応答も同じ（利用者の決定 2026-09-20）。トランスポートが
        死んだ印なのに接続中の表示のまま残ると、以後の一覧・転送がすべて
        同じ失敗を繰り返すだけになる。

        ロックを持ったまま呼ばない（disconnect がロックを取りにいく）。
        """
        if isinstance(e, TimeoutError):   # socket.timeout の別名
            self.error_occurred.emit(
                f"{prefix}: 機器が{self.CHANNEL_TIMEOUT_SECONDS:g}秒応答しません。"
                "SFTP接続を切断しました。接続し直してください")
            self.disconnect()
            return
        reason = str(e) or e.__class__.__name__
        if _is_dropped_connection(e):
            # paramiko の 'Server connection dropped: ' のように、理由が
            # コロンで終わることがある。そのまま続けると「: 。」になる
            self.error_occurred.emit(
                f"{prefix}: {reason.strip().rstrip(':').rstrip()}。"
                "SFTP接続を切断しました。接続し直してください")
            self.disconnect()
            return
        self.error_occurred.emit(f"{prefix}: {reason}")

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
            except TimeoutError:   # socket.timeout の別名
                # 期限切れは「ホームが分からない」ではなく、チャンネルが
                # 使えないという印。要求と応答はずれたままなので、'/' から
                # 始めても以後の操作は失敗し続ける。掴んだまま接続成功を
                # 返さず、ここで畳んで失敗にする（connected をまだ出して
                # いないので、disconnect ではなく直接閉じる）
                try:
                    self.sftp_client.close()
                except Exception:
                    pass
                self.sftp_client = None
                self.ssh_client = None
                self.error_occurred.emit(
                    f"SFTP接続エラー: 機器が{self.CHANNEL_TIMEOUT_SECONDS:g}秒応答しません")
                return False
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
            import stat as stat_mod
            try:
                # ディレクトリ一覧を取得（転送中なら空くまで待つ）
                with self._sftp_lock:
                    # 待っているあいだに切断されたかもしれない。取得前の
                    # 確認だけでは足りない（起きたら None を触ることになる）。
                    # 黙って戻ると、一覧が変わらない理由が利用者に届かない。
                    # 既存の except / _fail 経路へ寄せて通知する
                    if not self.is_connected or self.sftp_client is None:
                        raise IOError("SFTP接続がありません")
                    items = self.sftp_client.listdir_attr(path)
                    # listdir_attr の st_mode は lstat 相当（リンク自身）で、
                    # そのまま S_ISDIR に掛けるとディレクトリへのリンクが
                    # 常にファイルになる。パネルは is_dir を見てから移動を
                    # 決めるので、リンクの先へ入れなくなる。リンクの項目だけ
                    # 追跡先を引き直す（往復が増えるのはリンクの数だけ）。
                    # 引けない相手は、これまでどおりファイル扱いにする
                    link_modes = {}
                    for item in items:
                        mode = item.st_mode
                        if not isinstance(mode, int) or not stat_mod.S_ISLNK(mode):
                            continue
                        try:
                            target = self.sftp_client.stat(
                                posixpath.join(path, item.filename))
                        except TimeoutError:
                            # 期限切れは接続が使えなくなった印。リンクの数だけ
                            # 期限を積み上げると一覧が何分も返らないので、
                            # ここでやめて失敗として扱う（_fail が畳む）
                            raise
                        except Exception:
                            continue
                        link_modes[item.filename] = getattr(target, "st_mode", None)
                
                # ファイル情報をリストに変換
                file_list = []
                for item in items:
                    # 種別はリンクの追跡先で決める。permissions は lstat のまま
                    # 組み立てるので、リンクであることは 'l' で分かる
                    is_dir = self._is_directory(
                        link_modes.get(item.filename, item.st_mode))
                    # リンクかどうかは lstat（= listdir_attr の st_mode）で
                    # 決める。移動の可否は追跡先（is_dir）だが、削除・改名の
                    # 相手はリンク自身なので、参照先を分けて持たせる。
                    # ディレクトリへのリンクを is_dir のまま rmdir に渡すと、
                    # POSIX の rmdir は ENOTDIR で必ず失敗する
                    is_link = (isinstance(item.st_mode, int)
                               and stat_mod.S_ISLNK(item.st_mode))
                    file_list.append({
                        'name': item.filename,
                        'size': item.st_size if not is_dir else 0,
                        'mtime': item.st_mtime,
                        'mode': item.st_mode,
                        'is_dir': is_dir,
                        'is_link': is_link,
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
                self._fail("ディレクトリ一覧取得エラー", e)
        
        # バックグラウンドスレッドで実行
        threading.Thread(target=list_thread, daemon=True).start()
    
    # _remote_probe が返す、送る直前のリモートの状態
    _REMOTE_MISSING = "missing"
    _REMOTE_FILE = "file"
    _REMOTE_DIR = "dir"
    _REMOTE_UNSURE = "unsure"
    # 期限切れ。有無が分からないのは unsure と同じだが、以後この接続が
    # 使えないことまで分かるので、呼び出し側が切断へ回せるよう分ける
    _REMOTE_TIMEOUT = "timeout"

    def _remote_probe(self, remote_path: str):
        """送る直前のリモートの状態を stat で確かめる（ロック内で呼ぶ）

        stat の失敗を一律「無い」と読むと、既存ファイルの stat を権限エラーで
        返す機器で、確認を経ていない上書きが黙って通る（実測: stat が
        PermissionError のとき put が呼ばれ、既存が置き換わった）。「無い」
        ではないと分かる失敗（権限が無い・期限切れ）は "unsure" にして送らない。

        残る限界: 理由の分からない失敗は、これまでどおり「無い」と読む。
        見つからないときの応答が機器によって違い、汎用の失敗で返すものが
        あるため（実測: NetBelt 同梱の SFTP サーバも、無いファイルの stat に
        SFTP_FAILURE を返す＝クライアント側は IOError("Failure")）。ここを
        締めると、そうした機器へは新しい名前すら送れなくなる。

        Returns:
            (状態, 理由): 状態は "missing" / "file" / "dir" / "unsure" /
            "timeout"。"unsure" と "timeout" は有無を確かめられなかった
            ときで、理由にその説明が入る。"timeout" はチャンネルが以後
            使えないことまで分かっている場合
        """
        try:
            attr = self.sftp_client.stat(remote_path)
        except PermissionError as e:
            return self._REMOTE_UNSURE, str(e) or e.__class__.__name__
        except TimeoutError:   # socket.timeout の別名。str が空なので補う
            return self._REMOTE_TIMEOUT, (
                f"機器が{self.CHANNEL_TIMEOUT_SECONDS:g}秒応答しません")
        except Exception:
            return self._REMOTE_MISSING, ""
        mode = getattr(attr, "st_mode", None)
        if isinstance(mode, int) and self._is_directory(mode):
            return self._REMOTE_DIR, ""
        return self._REMOTE_FILE, ""

    def _carry_over_mode(self, remote_path: str, tmp_remote: str):
        """置き換え先の mode を一時名へ写す（ロック内で呼ぶ）

        一時名へ送ってから改名する作りでは、最終名の権限が一時名を作った
        ときのもの（サーバの umask 任せ）に変わる。0600 の設定ファイルを
        上書きすると緩くなり得るので、既存の mode が読めたときは改名の前に
        当て直す。所有者・グループ・ACL は SFTP では引き継げない。

        読めない相手（stat が失敗する）や、当てられない相手（機器の flash の
        ように mode が意味を持たない）では、これまでどおり何もしない。
        ただし期限切れは握りつぶさずに上へ送る。チャンネルはもう使えないので、
        そのまま改名へ進むと、もう一度期限まで待ったうえ接続中のまま残る。
        """
        try:
            mode = getattr(self.sftp_client.stat(remote_path), "st_mode", None)
        except TimeoutError:   # socket.timeout の別名
            raise
        except Exception:
            return
        if not isinstance(mode, int):
            return
        try:
            self.sftp_client.chmod(tmp_remote, mode & 0o7777)
        except TimeoutError:
            raise
        except Exception:
            # 権限を引き継げないことは、転送そのものの失敗にはしない
            pass

    def _create_tmp_with_mode(self, remote_path: str, tmp_remote: str):
        """中身を送る前に、置き換え先の mode を当てた空の一時名を作る（ロック内で呼ぶ）

        put() は mode を付けずに開くので、一時名はサーバの既定（OpenSSH なら
        0666 & ~umask = 0644 など）で作られる。_carry_over_mode が当て直すのは
        全部送ったあとなので、転送中や途中で切れて残った一時名は、0600 の
        設定ファイルの中身を他のユーザーが読める状態になる。先に空で作って
        mode を当てておけば、put() が O_TRUNC で開き直しても mode は残る。

        既存の mode が読めない（新しい名前など）ときは作らない（これまで
        どおりサーバの既定になる）。ハンドルへの chmod を受け付けない機器
        でも転送は止めない（最終名の mode は _carry_over_mode が当てる）。
        期限切れはチャンネルが使えない印なので、握りつぶさずに上へ送る。

        作るときの OPEN にも既存の mode を付ける。paramiko の open() は属性の
        無い OPEN を送るので、作ってから chmod するまでのあいだは一時名が
        サーバの既定（0644 など）になる。OpenSSH の sftp-server は OPEN の
        permissions を open(2) の mode に渡すので、最初から既存と同じ mode で
        作られる。umask で落ちたビットは続くハンドルへの chmod で当て直す。
        mode 付きの OPEN を断る機器では、これまでどおり属性なしで開く。
        """
        try:
            mode = getattr(self.sftp_client.stat(remote_path), "st_mode", None)
        except TimeoutError:   # socket.timeout の別名
            raise
        except Exception:
            return
        if not isinstance(mode, int):
            return
        try:
            handle = self._open_new_with_mode(tmp_remote, mode & 0o7777)
        except TimeoutError:
            raise
        except Exception:
            # mode 付きの OPEN（や EXCL）を受け付けない機器
            handle = self.sftp_client.open(tmp_remote, "wb")
        try:
            handle.chmod(mode & 0o7777)
        except TimeoutError:
            raise   # 閉じにいっても、さらに期限ぶん待つだけ
        except Exception:
            pass
        handle.close()

    def _open_new_with_mode(self, path: str, mode: int):
        """permissions 属性を付けた OPEN で新しいファイルを作って開く（ロック内で呼ぶ）

        paramiko の公開 API（open()）では OPEN に属性を付けられないので、
        open() と同じ要求を属性付きで直接送る。既にある名前は開かない（EXCL）。
        """
        client = self.sftp_client
        attr = paramiko.SFTPAttributes()
        attr.st_mode = mode
        t, msg = client._request(
            CMD_OPEN, client._adjust_cwd(path),
            SFTP_FLAG_WRITE | SFTP_FLAG_CREATE | SFTP_FLAG_EXCL, attr)
        if t != CMD_HANDLE:
            raise paramiko.SFTPError("Expected handle")
        return paramiko.SFTPFile(client, msg.get_binary(), "wb")

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

        限界: 呼ばれた順に送られる保証は無い。1 件ごとにスレッドを起こし、
        そのスレッドが _sftp_lock を取った順で転送するので、順番を決めるのは
        呼び出し順ではなく OS のスケジューリングと Python のロック
        （threading.Lock は FIFO ではない）である。同じ remote_path へ
        続けて送ると、後から呼んだ方が先に書かれ、古い方が最後に残ることが
        あり得る。しかも完了通知は両方とも「アップロード完了」なので、
        利用者は見分けられない。

        実測では自然な連続投入 30 回すべてで呼び出し順どおりに書かれ、
        逆転させるにはロック取得前のローカル側 stat を 2 件目の転送完了まで
        止める必要があった。加えて既定の confirm_overwrite=True では 2 件目に
        上書き確認ダイアログが出るため、その間に 1 件目がロックを取り、順序は
        固定される。順番を本当に保証するには、接続ごとに queue.Queue と単一の
        ワーカースレッドを置いて転送要求を FIFO で処理する作りへ変える必要が
        あり、一覧・ダウンロード・削除も同じロックを共有しているので影響が
        広い。現状はこの限界として残す。
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
        if not remote_dir and remote_path.startswith("/"):
            # '/name' の rpartition はディレクトリを '' で返す。そのまま
            # 相対の一時名にすると、サーバの開始ディレクトリ側へ書かれ、
            # ルートと別ファイルシステムなら改名も失敗する
            remote_dir = "/"
        # 一時名のディレクトリ部（ルートなら '/' 一つだけを前に付ける）
        tmp_dir = remote_dir if remote_dir in ("", "/") else remote_dir + "/"
        # スラッシュを含まない相対名なら、一時名も相対のまま（ルート直下に
        # しない。OpenSSH 系の機器はルートに書けないことが多い）
        # 一時名は送信ごとに一意にする（ダウンロード側の mkstemp と同じ理由）。
        # 固定名だと、置き換えに失敗して残した「唯一の完全な写し」を次の試行が
        # 黙って上書きし、その試行が失敗すれば後始末が消してしまう。一意なら
        # 消す相手は必ず今回作った一時名に限られる
        tmp_remote = (tmp_dir + ".%s.netbelt-part.%d-%s"
                      % (remote_name, os.getpid(), uuid.uuid4().hex[:8]))
        # 最終名を消したあとで置き換えに失敗した場合は、一時名が唯一の完全な
        # 写しになるので消さない
        keep_tmp = [False]
        # 送る直前の確認が期限切れになった印。ロックの中では接続を畳めない
        # ので、抜けてから畳むために持ち回る
        probe_timed_out = [False]
        # 期限切れで抜けるときに添える補足（転送済みの一時名など）。
        # _fail の期限切れ文面は固定なので、前置きの側へ足す
        timed_out_note = [""]
        # 元の失敗そのものが期限切れだったときの補足。通知は 1 回にまとめる
        # ので、finally ではなく元の失敗の文面へ足す
        cleanup_note = [""]

        def unknown_outcome_note(final_removed: bool = False,
                                 cause: str = "") -> str:
            """置き換わったか確かめられないときの説明を組み立てる。

            応答が期限切れになっただけで、機器側では置き換えが済んでいる
            ことがある。そこから「消してやり直す」手順へ進むと、置き換わった
            ばかりの最終名まで消し、転送した写しも一時名ごと失う。
            どちらの名前にも触れず、確かめ方だけを伝える。

            Args:
                final_removed: 復旧手順で最終名を既に remove したあとなら
                    True。利用者が最終名の無事を誤解しないよう書き添える
                cause: 確かめられない理由。期限切れのときは _fail が理由と
                    切断まで添えるので空のまま
            """
            gone = "最終名は置き換えの手順で既に消してあります。" if final_removed else ""
            return ("最終名へ置き換えられたか確かめられませんでした%s。"
                    "機器側を確認してください。" % cause + gone +
                    "一時名 %s が残っていれば置き換えは"
                    "終わっていません" % tmp_remote)

        def unknown_outcome_error(e, final_removed: bool = False) -> IOError:
            """期限切れ以外で置き換わったか確かめられない失敗を作る。

            paramiko は切断した読み取りや壊れた応答で、OSError の仲間でない
            例外（EOFError / SFTPError / SSHException）を上げる。改名の要求を
            送ったあとに落ちたのなら、置き換わったかどうかは分からない。
            確定した失敗として外側へ落とすと keep_tmp が立たず、後始末が
            転送した唯一の完全な写し（一時名）まで消す。

            原因が切断・壊れた応答だったものは _DroppedConnection にして、
            包んだあとも _fail が接続を畳む判断をできるようにする。
            """
            keep_tmp[0] = True
            wrapper = _DroppedConnection if _is_dropped_connection(e) else IOError
            return wrapper("%s: %s" % (
                unknown_outcome_note(final_removed,
                                     "（接続が切れたか応答が壊れています）"),
                str(e) or e.__class__.__name__))

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
                    # 黙って戻ると、送られたのかどうかが利用者に届かない。
                    # 既存の except / _fail 経路へ寄せて通知する（まだ何も
                    # 送っていないので、後始末が消す一時名も無い）
                    if not self.is_connected or self.sftp_client is None:
                        raise IOError("SFTP接続がありません")
                    # 確認を経ていない送信は、送る直前の実際の状態で判定する。
                    # ロック内なので、先行する転送の結果も見える
                    if not overwrite:
                        state, why = self._remote_probe(remote_path)
                        if state == self._REMOTE_DIR:
                            # 一覧を取り直しても種別は変わらない。やり直し方を
                            # 案内せず、できないことをそのまま伝える
                            self.error_occurred.emit(
                                f"リモートの '{remote_name}' はディレクトリです。"
                                "ファイルで上書きできません")
                            return
                        if state == self._REMOTE_FILE:
                            self.error_occurred.emit(
                                f"リモートに '{remote_name}' が既にあります。上書きの確認を"
                                "経ていないので送りませんでした。一覧を更新してからやり直してください")
                            return
                        if state == self._REMOTE_TIMEOUT:
                            # 期限切れのあとは要求と応答がずれたままで、この
                            # 接続はもう使えない。接続中のまま戻ると、同じ
                            # 送信を繰り返す限り毎回ここで期限ぶん待たされる。
                            # ここはロックの中なので畳めない（disconnect が
                            # 同じロックを取る）。印を付けて抜けてから畳む
                            probe_timed_out[0] = True
                            return
                        if state == self._REMOTE_UNSURE:
                            self.error_occurred.emit(
                                f"リモートに '{remote_name}' があるか確かめられませんでした"
                                f"（{why}）。上書きになる恐れがあるので送りませんでした")
                            return
                    else:
                        # 置き換えなら、転送中の一時名も既存より緩くしない
                        self._create_tmp_with_mode(remote_path, tmp_remote)
                    self.sftp_client.put(local_path, tmp_remote,
                                         callback=progress_callback)
                    if not overwrite:
                        # 送る前の確認から転送のあいだに、第三者が同じ名前を
                        # 作っているかもしれない。posix_rename は既存を上書き
                        # するので、置き換える直前にもう一度確かめる。
                        # 限界: この確認と改名のあいだは依然として塞げない
                        # （SFTP に「無ければ置き換える」原子操作が無く、
                        # _sftp_lock は同一プロセス内しか直列化しない）。
                        # ただし窓は転送の全体から stat 1 往復まで縮まる
                        state, why = self._remote_probe(remote_path)
                        if state == self._REMOTE_TIMEOUT:
                            # 有無は確かめられていない。「作られました」と
                            # 断定せず、以後使えないチャンネルも畳む。
                            # ここはロックの中なので畳めない（disconnect が
                            # 同じロックを取る）。印を付けて抜けてから畳む
                            keep_tmp[0] = True
                            probe_timed_out[0] = True
                            timed_out_note[0] = (
                                "（リモートに '%s' が現れていないか確かめられませんでした。"
                                "置き換えていません。転送した内容は一時名 %s に"
                                "残っています）" % (remote_name, tmp_remote))
                            return
                        if state == self._REMOTE_UNSURE:
                            # 権限エラーなど。有無は分からないが、チャンネル
                            # そのものは使えるので接続は畳まない
                            keep_tmp[0] = True
                            raise IOError(
                                "転送しているあいだにリモートへ '%s' が現れていないか"
                                "確かめられませんでした（%s）。上書きになる恐れがあるので"
                                "置き換えていません。転送した内容は一時名 %s に"
                                "残っています" % (remote_name, why, tmp_remote))
                        if state != self._REMOTE_MISSING:
                            # 転送した内容は捨てない。一時名に残して知らせる
                            keep_tmp[0] = True
                            raise IOError(
                                "転送しているあいだにリモートへ '%s' が作られました。"
                                "上書きの確認を経ていないので置き換えていません。"
                                "転送した内容は一時名 %s に残っています"
                                % (remote_name, tmp_remote)
                                + ("（%s）" % why if why else ""))
                    else:
                        # 置き換えなら、既存の権限を一時名へ写しておく
                        # （overwrite=False のときは上で「無い」と確かめた
                        # あとなので、引き継ぐ mode は無い）
                        try:
                            self._carry_over_mode(remote_path, tmp_remote)
                        except TimeoutError:   # socket.timeout の別名
                            # 使えないチャンネルで改名へ進まない。転送した
                            # 内容は一時名に残し、抜けてから接続を畳む
                            # （ロックの中では畳めない。畳むのは finally）
                            keep_tmp[0] = True
                            probe_timed_out[0] = True
                            timed_out_note[0] = (
                                "（置き換えていません。転送した内容は一時名 %s に"
                                "残っています）" % tmp_remote)
                            return
                    # 全部送れてから最終名へ。posix_rename（OpenSSH 拡張）は
                    # 既存を上書きできるので、上書きの確認を得ている送信だけが
                    # 使う。確認を経ていない送信は、既存があれば失敗する
                    # ふつうの rename を使う（STAT を実装しない機器では送る
                    # 直前の確認も転送後の再確認も「無い」と読むので、既存を
                    # 潰さないための最後の砦がこの失敗になる）。posix_rename
                    # の無いサーバでは、まず rename を試し、既存があって失敗
                    # したときだけ消してからもう一度 rename する（先に消すと、
                    # rename に失敗した瞬間に元が消える）
                    try:
                        if overwrite:
                            self.sftp_client.posix_rename(tmp_remote, remote_path)
                        else:
                            self.sftp_client.rename(tmp_remote, remote_path)
                    except TimeoutError:      # socket.timeout の別名
                        # 期限切れのあとは要求と応答がずれたままで、この
                        # チャンネルはもう使えない。送る前の確認や権限の
                        # 引き継ぎと同じく、抜けてから接続を畳んで知らせる
                        # （ロックの中では畳めない。畳むのは finally）
                        keep_tmp[0] = True
                        probe_timed_out[0] = True
                        timed_out_note[0] = "（%s）" % unknown_outcome_note()
                        return
                    except (AttributeError, IOError) as first_error:
                        if not overwrite:
                            # 非 posix の rename が断る理由の筆頭は
                            # 「既にある」。上書きの確認を経ていない送信で
                            # その失敗から最終名を消しにいくと、既存を
                            # 潰さないための安全網を自分で外すことになる。
                            # 最終名には触れず、一時名に残して知らせる
                            keep_tmp[0] = True
                            raise IOError(
                                "リモートの '%s' へ置き換えられませんでした"
                                "（既にある可能性があります）。上書きの確認を経て"
                                "いないので最終名には触れていません。転送した内容は"
                                "機器の一時名 %s に残っています。上書きしてよければ"
                                "一覧を更新してからやり直してください: %s"
                                # paramiko は message の無い応答でも読み進むので、
                                # str が空の IOError が届く。そのまま連結すると
                                # 理由の無い「: 」で終わる
                                % (remote_name, tmp_remote,
                                   str(first_error) or first_error.__class__.__name__))
                        # posix_rename の無いサーバ。ふつうの rename を試す
                        try:
                            self.sftp_client.rename(tmp_remote, remote_path)
                        except TimeoutError:
                            # 1 本目と同じ。畳むのは finally
                            keep_tmp[0] = True
                            probe_timed_out[0] = True
                            timed_out_note[0] = "（%s）" % unknown_outcome_note()
                            return
                        except IOError:
                            # remove が断られたら最終名は残っている。消えたと
                            # 伝えてよいのは remove が成功したときだけ
                            final_removed = False
                            try:
                                self.sftp_client.remove(remote_path)
                                final_removed = True
                            except TimeoutError:   # socket.timeout の別名
                                # IOError の仲間なので先に受ける。消せたかは
                                # 分からず、チャンネルも以後使えない。2 本目の
                                # rename へ進まず、抜けてから接続を畳む
                                keep_tmp[0] = True
                                probe_timed_out[0] = True
                                timed_out_note[0] = (
                                    "（最終名を消せたか確かめられませんでした。"
                                    "転送した内容は一時名 %s に残っています）"
                                    % tmp_remote)
                                return
                            except Exception:
                                # paramiko は OSError の仲間でない例外も上げる
                                # （EOFError / SFTPError / SSHException）。
                                # IOError だけ見ていると、外側の except へ
                                # 落ちて 2 本目の rename にも進めない
                                pass
                            try:
                                self.sftp_client.rename(tmp_remote, remote_path)
                            except TimeoutError:
                                # 1本目・2本目と同じ。機器側では置き換わって
                                # いて応答だけが返らないことがあるので、
                                # 確定した失敗として報告せず、使えなくなった
                                # チャンネルは抜けてから畳む
                                keep_tmp[0] = True
                                probe_timed_out[0] = True
                                timed_out_note[0] = "（%s）" % unknown_outcome_note(
                                    final_removed=final_removed)
                                return
                            except IOError as e:
                                # 機器が答えた失敗。改名は行われていないと
                                # 分かるので、これまでどおり断定してよい。
                                # 復旧手順で最終名を消したあとなら、利用者が
                                # 元の設定の無事を誤解しないよう書き添える
                                keep_tmp[0] = True
                                gone = ("最終名は置き換えの手順で既に消してあります。"
                                        if final_removed else "")
                                raise IOError(
                                    "最終名への置き換えに失敗しました。" + gone +
                                    "転送済みの内容は"
                                    "機器の一時名 %s に残っています: %s"
                                    # paramiko は切断した読み取りで素の
                                    # EOFError() を上げる（str が空）。
                                    # そのまま連結すると「: 」で終わる
                                    % (tmp_remote, str(e) or e.__class__.__name__))
                            except Exception as e:
                                # 切断・壊れた応答（EOFError / SFTPError /
                                # SSHException）。改名の要求は届いていて応答
                                # だけが返らなかったのなら、最終名には新しい
                                # 内容があり一時名は無い。それを「失敗した」
                                # 「写しは一時名にある」と断定すると、利用者は
                                # 両方失ったと読んで無用な復旧へ向かう。
                                # 期限切れの枝と同じく、確かめ方だけを伝える
                                raise unknown_outcome_error(
                                    e, final_removed=final_removed)
                        except Exception as e:
                            # 2 本目の rename が、機器の答えた失敗ではなく
                            # 接続の切断や壊れた応答で落ちた。置き換わったかは
                            # 分からないので、最終名を消してやり直す復旧手順へ
                            # は進まない
                            raise unknown_outcome_error(e)
                    except Exception as e:
                        # 1 本目（posix_rename / rename）が同じように落ちた。
                        # 代わりの rename へ進んでも同じチャンネルなので、
                        # 置き換わったか不明なまま一時名を残して知らせる
                        raise unknown_outcome_error(e)

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
                    except TimeoutError:   # socket.timeout の別名
                        # 後始末まで期限切れなら、以後このチャンネルは要求と
                        # 応答がずれたまま使えない。元の失敗を伝えたうえで、
                        # 期限切れの経路と同じように畳む（ロックの中では
                        # 畳めない。畳むのは finally）
                        note = ("（送りかけの一時名 %s を片づけられませんでした）"
                                % tmp_remote)
                        if isinstance(e, TimeoutError):
                            # 元の失敗そのものが期限切れ。すぐ下の _fail が
                            # 既に切断まで伝えるので、印は立てない（立てると
                            # finally がもう一度 _fail を呼び、同じ文面と
                            # disconnected が 2 回出る）。死んだチャンネルでは
                            # put が期限切れなら後始末の remove も期限切れに
                            # なるので、この重なりは珍しくない
                            cleanup_note[0] = note
                        else:
                            probe_timed_out[0] = True
                            timed_out_note[0] = note
                    except Exception:
                        pass
                self._fail("アップロードエラー" + cleanup_note[0], e)
            finally:
                # ロックの外。理由を出して接続を畳むのは _fail に任せる
                if probe_timed_out[0]:
                    self._fail("アップロードエラー" + timed_out_note[0],
                               TimeoutError())
        
        # バックグラウンドスレッドで実行
        threading.Thread(target=upload_thread, daemon=True).start()
    
    # 進行中のダウンロードの保存先（_download_target_key の鍵）。機器ごとに
    # マネージャは別なので、インスタンスをまたいでプロセス全体で共有する
    _download_targets = set()
    _download_targets_lock = threading.Lock()

    @staticmethod
    def _download_target_key(local_path: str) -> str:
        """保存先を比べるための鍵。realpath で短縮名（8.3 形式）を解き、
        normcase で大文字小文字と区切り文字の違いをならす（まだ無い
        ファイルでも、あるところまでのフォルダは解かれる）"""
        return os.path.normcase(os.path.realpath(local_path))

    @classmethod
    def _release_download_target(cls, key: str):
        """保存先の予約を外す（何度呼んでもよい）"""
        with cls._download_targets_lock:
            cls._download_targets.discard(key)

    def download_file(self, remote_path: str, local_path: str):
        """
        ファイルをダウンロード（バックグラウンド）
        
        Args:
            remote_path: リモートファイルパス
            local_path: ローカルファイルパス

        同じ保存先へのダウンロードが進行中（順番待ちを含む）なら、断って
        何もしない。保存ダイアログの上書き確認はその時点で有るものしか
        見ないので、まだ無い同じ保存先へ 2 件落とすと、後から終わった方の
        置き換えで先の方が確認なしに消える。予約は転送が終わったとき
        （成功・失敗・中断）に外す。

        予約が見るのは同一プロセスの SFTP ダウンロード同士だけなので、
        別アプリ・別プロセスの書き込みは素通りする。そこで、呼ばれた時点
        （GUI スレッド、保存ダイアログの直後）で保存先の有無も控える。
        無ければ上書きは承認されていないので、最後の確定を os.replace では
        なく os.rename で行う。Windows の os.rename は宛先があると
        FileExistsError（WinError 183）で断り、宛先の中身には触れない。
        """
        if not self.is_connected or not self.sftp_client:
            self.error_occurred.emit("SFTP接続がありません")
            return
        
        # 保存ダイアログが上書きを確認したかどうかの手掛かり。ここは
        # ダイアログの直後（GUI スレッド）なので、まだ誰も割り込んでいない
        overwrite_granted = os.path.exists(local_path)

        target_key = self._download_target_key(local_path)
        with self._download_targets_lock:
            busy = target_key in self._download_targets
            if not busy:
                self._download_targets.add(target_key)
        if busy:
            self.error_occurred.emit(
                "同じ保存先へのダウンロードが進行中です。終わってからやり直すか、"
                f"別の保存先を選んでください: {local_path}")
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
            # 始まらなかったので予約も外す
            self._release_download_target(target_key)
            # ここは GUI スレッド。例外を上げるとスロットの外へ抜けるので、
            # 転送の失敗と同じ経路で知らせる
            self.error_occurred.emit(f"ダウンロードエラー: {str(e)}")
            return

        # 置き換えを断ったときは、落とした内容が一時名にしか無いので消さない
        keep_tmp = [False]

        def download_thread():
            try:
                # ダウンロード実行
                def progress_callback(transferred_bytes, total_bytes):
                    """転送進捗コールバック"""
                    self.transfer_progress.emit(transferred_bytes, total_bytes)
                
                with self._sftp_lock:
                    # 待っているあいだに切断されたかもしれない。取得前の
                    # 確認だけでは足りない（起きたら None を触ることになる）。
                    # ここで黙って return すると、起動前に作った一時ファイルが
                    # 保存先のディレクトリに 0 バイトで残り、しかも利用者には
                    # 何も起きない。後始末と通知のある経路へ寄せる
                    if not self.is_connected or self.sftp_client is None:
                        raise IOError("SFTP接続がありません")
                    self.sftp_client.get(remote_path, tmp_local,
                                         callback=progress_callback)
                
                # 全部落とせてから最終名へ（同じディレクトリなので原子的）
                if overwrite_granted:
                    os.replace(tmp_local, local_path)
                else:
                    # 呼ばれた時点では無かった＝上書きは承認されていない。
                    # 転送しているあいだに外から作られていたら置き換えない
                    try:
                        os.rename(tmp_local, local_path)
                    except FileExistsError:
                        keep_tmp[0] = True
                        raise OSError(
                            "転送しているあいだに保存先 '%s' が作られました。"
                            "上書きの確認を経ていないので置き換えていません。"
                            "落とした内容は %s に残っています"
                            % (os.path.basename(local_path), tmp_local))
                # 予約は通知より先に外す（完了を見てすぐ落とし直しても断られない）
                self._release_download_target(target_key)
                # 完了通知
                self.transfer_complete.emit(f"ダウンロード完了: {os.path.basename(remote_path)}")
                
            except Exception as e:
                # 失敗した転送の残骸を消す。既存の保存先には触っていない。
                # 置き換えを断った場合は、落とした内容がここにしか無い
                if not keep_tmp[0]:
                    try:
                        os.remove(tmp_local)
                    except OSError:
                        pass
                self._release_download_target(target_key)
                self._fail("ダウンロードエラー", e)
        
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
        err = None
        try:
            self.sftp_client.mkdir(path)
        except Exception as e:
            err = e
        finally:
            self._sftp_lock.release()
        # 通知はロックを離してから（_fail が切断するときロックを取る）
        if err is not None:
            self._fail("ディレクトリ作成エラー", err)
            return
        self.transfer_complete.emit(f"ディレクトリ作成: {os.path.basename(path)}")
        # ディレクトリ一覧を更新（ロックを離してから）
        self.list_directory(self.current_path)
    
    def delete_item(self, path: str, is_dir: bool = False):
        """
        ファイルまたはディレクトリを削除
        
        Args:
            path: ファイル/ディレクトリパス
            is_dir: 本物のディレクトリの場合True（rmdir を使う）。ディレクトリ
                へのシンボリックリンクは False で渡すこと。rmdir はリンクに
                対して ENOTDIR で必ず失敗し、リンクを消せなくなる。一覧の
                is_dir は追跡先で決まるので、そのままは渡せない
        """
        if not self.is_connected or not self.sftp_client:
            self.error_occurred.emit("SFTP接続がありません")
            return
        
        if not self._acquire_for_gui("削除"):
            return
        err = None
        try:
            if is_dir:
                self.sftp_client.rmdir(path)
            else:
                self.sftp_client.remove(path)
        except Exception as e:
            err = e
        finally:
            self._sftp_lock.release()
        if err is not None:
            self._fail("削除エラー", err)
            return
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
        err = None
        try:
            self.sftp_client.rename(old_path, new_path)
        except Exception as e:
            err = e
        finally:
            self._sftp_lock.release()
        if err is not None:
            self._fail("名前変更エラー", err)
            return
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
        err = None
        try:
            self.sftp_client.chmod(path, mode)
        except Exception as e:
            err = e
        finally:
            self._sftp_lock.release()
        if err is not None:
            self._fail("パーミッション変更エラー", err)
            return
        self.transfer_complete.emit(f"パーミッション変更完了: {os.path.basename(path)}")
        # ディレクトリ一覧を更新（ロックを離してから）
        self.list_directory(self.current_path)

    def inspect_link_target(self, path: str):
        """シンボリックリンクの先の mode と名前を読む（GUI スレッドから呼ぶ）

        一覧の mode はリンク自身（lstat 相当、多くは 0777）だが、chmod は
        リンクをたどって先へ効く。それを権限変更の初期値にすると、何も
        変えずに OK しただけでリンク先が 0777 になる。初期値に使うために
        stat でたどり直し、ダイアログに出す名前を readlink で読む。

        Returns:
            (mode, リンク先の名前)。名前を読めない機器では名前が None。
            リンク先を読めない（壊れたリンク・権限が無い）、転送中、
            未接続のときは None で、理由は error_occurred で通知済み
        """
        if not self.is_connected or not self.sftp_client:
            self.error_occurred.emit("SFTP接続がありません")
            return None
        if not self._acquire_for_gui("パーミッション変更"):
            return None
        err = mode = target = None
        try:
            mode = getattr(self.sftp_client.stat(path), "st_mode", None)
            try:
                target = self.sftp_client.readlink(path)
            except TimeoutError:   # socket.timeout の別名
                raise
            except Exception:
                pass   # 名前が読めなくても、権限は読めている
        except Exception as e:
            err = e
        finally:
            self._sftp_lock.release()
        # 通知はロックを離してから（_fail が切断するときロックを取る）
        if isinstance(err, TimeoutError):
            self._fail("リンク先の確認エラー", err)
            return None
        if err is not None or not isinstance(mode, int):
            why = (str(err) or err.__class__.__name__) if err is not None \
                else "権限が返されませんでした"
            self.error_occurred.emit(
                f"'{posixpath.basename(path)}' のリンク先を読めないため、"
                f"パーミッションを変更できません（{why}）")
            return None
        return mode, target

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
        err = None
        try:
            # パスを正規化
            normalized_path = self.sftp_client.normalize(path)
        except Exception as e:
            err = e
        finally:
            self._sftp_lock.release()
        if err is not None:
            self._fail("ディレクトリ変更エラー", err)
            return
        # ディレクトリ一覧を取得（これによりパスの存在も確認）
        self.list_directory(normalized_path)
    
    def get_parent_directory(self) -> str:
        """
        親ディレクトリのパスを取得
        
        リモートのパスは POSIX なので posixpath で切る。os.path は
        Windows では ntpath になり、バックスラッシュを含む名前
        （Unix では合法）を区切りと読んで、'/a\\b' の親を '/a' という
        実在する別のディレクトリにしてしまう。

        Returns:
            str: 親ディレクトリパス
        """
        if self.current_path == "/":
            return "/"
        # 相対パスの親は空文字になる。そのまま change_directory へ渡すと
        # 意味のない要求になるので、ルートへ丸める
        return posixpath.dirname(self.current_path) or "/"
    
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
        パーミッションを文字列形式に変換（例: -rwxr-xr-x、drwxrwxrwt）

        setuid / setgid / sticky は、ls と同じく実行ビットの位置へ
        s / t（実行ビットが無ければ S / T）として重ねる。9 文字の
        rwx だけを組み立てると、sticky 付きのディレクトリが普通の 777 に
        見え、利用者は落としたことに気づけない。

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
        
        def exec_char(executable, special, letter):
            """実行ビットの桁を ls と同じ 1 文字にする

            特殊ビットが立っていれば letter（実行ビットが無ければ大文字）、
            立っていなければ従来どおり 'x' / '-'
            """
            if special:
                return letter if executable else letter.upper()
            return 'x' if executable else '-'

        # オーナー権限（実行の桁に setuid）
        perm_str += 'r' if mode & stat.S_IRUSR else '-'
        perm_str += 'w' if mode & stat.S_IWUSR else '-'
        perm_str += exec_char(mode & stat.S_IXUSR, mode & stat.S_ISUID, 's')

        # グループ権限（実行の桁に setgid）
        perm_str += 'r' if mode & stat.S_IRGRP else '-'
        perm_str += 'w' if mode & stat.S_IWGRP else '-'
        perm_str += exec_char(mode & stat.S_IXGRP, mode & stat.S_ISGID, 's')

        # その他権限（実行の桁に sticky）
        perm_str += 'r' if mode & stat.S_IROTH else '-'
        perm_str += 'w' if mode & stat.S_IWOTH else '-'
        perm_str += exec_char(mode & stat.S_IXOTH, mode & stat.S_ISVTX, 't')

        return perm_str