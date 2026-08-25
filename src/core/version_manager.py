"""
バージョン管理とアップデート機能
"""

import os
import json
import tempfile
import requests
from typing import Optional, Dict, Callable
from datetime import datetime
import hashlib

# バージョン情報をインポート
try:
    from __version__ import __version__, GITHUB_REPO, APP_NAME
except ImportError:
    # フォールバック
    __version__ = "1.1.0"
    GITHUB_REPO = "gomanizm/NetBelt"
    APP_NAME = "NetBelt"


class VersionManager:
    """バージョン管理とアップデート機能を提供するクラス"""
    
    CURRENT_VERSION = __version__
    GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    UPDATE_DIR = os.path.join(tempfile.gettempdir(), f"{APP_NAME}Updates")
    
    def __init__(self, github_token: Optional[str] = None):
        """
        初期化
        
        Args:
            github_token: GitHub Personal Access Token（プライベートリポジトリの場合必須）
        """
        self.github_token = github_token
        # 更新用ディレクトリを作成
        os.makedirs(self.UPDATE_DIR, exist_ok=True)
    
    @staticmethod
    def compare_versions(version1: str, version2: str) -> int:
        """
        バージョンを比較
        
        Args:
            version1: 比較元バージョン (e.g., "1.0.0" or "v1.0.0")
            version2: 比較先バージョン (e.g., "1.1.0" or "v1.1.0")
        
        Returns:
            -1: version1 < version2
             0: version1 == version2
             1: version1 > version2
        """
        # "v"プレフィックスを削除
        v1 = version1.lstrip('v')
        v2 = version2.lstrip('v')
        
        # バージョン番号を分割
        def _seg(s):
            n = ''
            for ch in s:
                if ch.isdigit():
                    n += ch
                else:
                    break
            return int(n) if n else 0
        parts1 = [_seg(x) for x in v1.split('.')]
        parts2 = [_seg(x) for x in v2.split('.')]
        
        # 長さを揃える
        max_len = max(len(parts1), len(parts2))
        parts1.extend([0] * (max_len - len(parts1)))
        parts2.extend([0] * (max_len - len(parts2)))
        
        # 比較
        for p1, p2 in zip(parts1, parts2):
            if p1 < p2:
                return -1
            elif p1 > p2:
                return 1
        
        return 0
    
    def check_for_updates(self, timeout: int = 10) -> Optional[Dict]:
        """
        GitHubから最新バージョンを確認
        
        Args:
            timeout: タイムアウト秒数
        
        Returns:
            更新情報の辞書、または None（エラー時）
            {
                'available': bool,
                'version': str,
                'release_notes': str,
                'download_url': str,
                'published_at': str
            }
        """
        try:
            # ヘッダーを準備（認証トークンがあれば追加）
            headers = {}
            if self.github_token:
                headers['Authorization'] = f'token {self.github_token}'
            
            response = requests.get(self.GITHUB_API_URL, headers=headers, timeout=timeout)
            response.raise_for_status()
            
            data = response.json()
            
            # 最新バージョンを取得
            latest_version = data.get('tag_name', '').lstrip('v')
            
            # バージョン比較
            is_newer = self.compare_versions(self.CURRENT_VERSION, latest_version) < 0
            
            # ダウンロードURLを取得（Windows 64bit用のZIPを探す）
            download_url = None
            download_name = None
            assets = data.get('assets', [])
            
            for asset in assets:
                name = asset.get('name', '').lower()
                if 'windows' in name and name.endswith('.zip'):
                    download_url = asset.get('url')  # APIのURLを使用（プライベートリポジトリ対応）
                    download_name = asset.get('name', '')
                    break
            
            # Windows 向けの ZIP が無ければ何も選ばない。以前は「最初の ZIP」へ
            # 落ちていたが、実行可能物でない ZIP を掴む余地を残すだけで、
            # 見つからないことは呼び出し側が扱える。
            
            # 選んだ ZIP に対応する控えを探す。「最初の .sha256」を取ると、
            # asset が1つ増えただけで別物を掴み、照合が必ず外れて自動更新が
            # 黙って止まる。
            sha256_url = None
            if download_name:
                wanted = (download_name + '.sha256').lower()
                for asset in assets:
                    if asset.get('name', '').lower() == wanted:
                        sha256_url = asset.get('url')
                        break

            return {
                'available': is_newer,
                'version': latest_version,
                'release_notes': data.get('body', ''),
                'download_url': download_url,
                'sha256_url': sha256_url,
                'published_at': data.get('published_at', '')
            }
        
        except requests.exceptions.RequestException as e:
            # None を返すと呼び出し側が「更新なし」と区別できず、
            # 通信が壊れていても『最新です』と表示されてしまう。
            print(f"[VersionManager] 更新チェックエラー: {e}")
            return {'available': False, 'error': f"更新の確認に失敗しました: {e}"}
        except Exception as e:
            print(f"[VersionManager] 予期しないエラー: {e}")
            return {'available': False, 'error': f"更新の確認に失敗しました: {e}"}
    
    @staticmethod
    def _discard(path: str) -> None:
        """検証に失敗したダウンロードを残さない。"""
        try:
            import os as _os
            if _os.path.exists(path):
                _os.remove(path)
        except Exception as e:
            print(f"[VersionManager] 一時ファイルの削除に失敗: {e}")

    @staticmethod
    def _sha256_of(path: str) -> str:
        """ファイルの SHA-256 を16進小文字で返す。"""
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()

    def _fetch_expected_sha256(self, sha256_url: str, timeout: int = 30) -> Optional[str]:
        """チェックサムファイルを取得して期待値を返す。

        `<hash>` だけの形式と、sha256sum 形式の `<hash>  <ファイル名>` の
        どちらも受け付ける。
        """
        headers = {'Accept': 'application/octet-stream'}
        if self.github_token:
            headers['Authorization'] = f'token {self.github_token}'
        try:
            r = requests.get(sha256_url, headers=headers, timeout=timeout,
                             allow_redirects=True)
            r.raise_for_status()
            first = (r.text or '').strip().split()
            if not first:
                return None
            value = first[0].strip().lower()
            return value if len(value) == 64 else None
        except Exception as e:
            print(f"[VersionManager] チェックサム取得エラー: {e}")
            return None

    def download_update(
        self,
        url: str,
        progress_callback: Optional[Callable[[int, int, int], None]] = None,
        sha256_url: Optional[str] = None,
        version: Optional[str] = None,
        cancel_check: Optional[Callable[[], bool]] = None
    ) -> Optional[str]:
        """
        更新ファイルをダウンロード
        
        Args:
            url: ダウンロードURL
            progress_callback: プログレスコールバック関数
                               (進捗%, ダウンロード済みバイト, 総バイト)
        
        Returns:
            ダウンロードしたZIPファイルのパス、またはNone（エラー時）
        """
        try:
            # ファイル名を生成
            filename = os.path.basename(url)
            if not filename.endswith('.zip'):
                filename = f"{APP_NAME}-update.zip"
            
            zip_path = os.path.join(self.UPDATE_DIR, filename)
            
            # ヘッダーを準備（認証トークンとAcceptヘッダーを追加）
            headers = {
                'Accept': 'application/octet-stream'  # アセットのバイナリダウンロード用
            }
            if self.github_token:
                headers['Authorization'] = f'token {self.github_token}'
            # トークンの値そのものはログへ出さない。凍結ビルドでは stdout が
            # %LOCALAPPDATA%\NetBelt\logs\ へ恒久的に退避されるため、
            # 一部であってもディスクに残すと不具合報告への添付などで流出する。
            # 有無は下の行で真偽値としてのみ記録する。
            
            print(f"[VersionManager] ダウンロード URL: {url}")
            print(f"[VersionManager] ヘッダー: Accept={headers.get('Accept')}, Auth={'あり' if 'Authorization' in headers else 'なし'}")
            
            # ダウンロード（リダイレクトに従う）
            response = requests.get(url, headers=headers, stream=True, timeout=60, allow_redirects=True)
            
            print(f"[VersionManager] レスポンスステータス: {response.status_code}")
            
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            # 検証を通るまでは .part 名で書く。最終名(.zip)で書くと、中断した
            # 未検証ファイルが「未適用の更新」として拾われ、検証なしで適用できる。
            part_path = zip_path + '.part'
            self._discard(part_path)
            with open(part_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if cancel_check is not None and cancel_check():
                        print("[VersionManager] ダウンロードを中止しました")
                        f.close()
                        self._discard(part_path)
                        return None
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        # プログレス通知
                        if progress_callback and total_size > 0:
                            progress = int(downloaded / total_size * 100)
                            progress_callback(progress, downloaded, total_size)
            
            print(f"[VersionManager] ダウンロード完了: {part_path}")

            # SHA-256 を照合する。壊れたファイルを展開・上書きしないための検証で、
            # ハッシュはリリースと同じ場所から取るため、GitHub 自体が侵害された
            # 場合の改ざんまでは検知できない点に注意。
            expected = self._fetch_expected_sha256(sha256_url) if sha256_url else None
            if not expected:
                print("[VersionManager] チェックサムを取得できないため更新を中止します")
                self._discard(part_path)
                return None
            actual = self._sha256_of(part_path)
            if actual.lower() != expected:
                print("[VersionManager] チェックサムが一致しません。更新を中止します")
                print(f"[VersionManager]   期待: {expected}")
                print(f"[VersionManager]   実際: {actual}")
                self._discard(part_path)
                return None
            print("[VersionManager] チェックサム照合 OK")

            # 検証を通ったものだけを最終名にする。あわせて検証済みの証として
            # ハッシュを傍らに残し、適用時にもう一度確かめられるようにする。
            self._discard(zip_path)
            os.replace(part_path, zip_path)
            try:
                # 版も控える。控えないと、次回起動時に「これは今より新しいか」を
                # 判断できず、古い ZIP の適用を勧めてしまう。
                if version:
                    try:
                        with open(zip_path + '.version', 'w', encoding='ascii') as vf:
                            vf.write(str(version))
                    except Exception:
                        pass
                with open(zip_path + '.sha256', 'w', encoding='ascii') as f:
                    f.write(actual.lower())
            except Exception as e:
                print(f"[VersionManager] チェックサムの控えを書けませんでした: {e}")

            return zip_path
        
        except Exception as e:
            print(f"[VersionManager] ダウンロードエラー: {e}")
            return None
    
    @staticmethod
    def pending_version(zip_path: str) -> Optional[str]:
        """ダウンロード時に控えた版を返す（無ければ None）"""
        try:
            with open(zip_path + '.version', encoding='ascii') as f:
                return f.read().strip() or None
        except Exception:
            return None

    def is_verified_update(self, zip_path: str) -> bool:
        """適用前に、ダウンロード時の検証を通ったファイルかを確かめる。

        傍らの .sha256 と実ファイルのハッシュを突き合わせる。控えが無い、
        あるいは一致しないものは適用しない（未検証のZIPが適用される経路を残さない）。
        """
        side = zip_path + '.sha256'
        try:
            if not os.path.exists(side):
                print(f"[VersionManager] 検証記録がありません: {os.path.basename(zip_path)}")
                return False
            with open(side, encoding='ascii') as f:
                expected = (f.read() or '').strip().split()[0].lower()
            if len(expected) != 64:
                return False
            return self._sha256_of(zip_path).lower() == expected
        except Exception as e:
            print(f"[VersionManager] 検証記録の確認に失敗: {e}")
            return False

    def get_pending_update_files(self) -> list:
        """
        未適用の更新ファイルをリストアップ
        
        Returns:
            ZIPファイルのパスのリスト
        """
        if not os.path.exists(self.UPDATE_DIR):
            return []
        
        pending_files = []
        
        try:
            for filename in os.listdir(self.UPDATE_DIR):
                if filename.endswith('.zip'):
                    file_path = os.path.join(self.UPDATE_DIR, filename)
                    pending_files.append(file_path)
        except Exception as e:
            print(f"[VersionManager] 未適用ファイル確認エラー: {e}")
        
        return pending_files
    
    def cleanup_old_updates(self, max_age_hours: int = 24) -> int:
        """
        古い更新ファイルを削除
        
        Args:
            max_age_hours: 削除する最大経過時間（時間）
        
        Returns:
            削除したファイル数
        """
        deleted_count = 0
        current_time = datetime.now().timestamp()
        max_age_seconds = max_age_hours * 3600
        
        for zip_path in self.get_pending_update_files():
            try:
                file_age = current_time - os.path.getmtime(zip_path)
                
                if file_age > max_age_seconds:
                    os.remove(zip_path)
                    print(f"[VersionManager] 古い更新ファイルを削除: {zip_path}")
                    deleted_count += 1
            except Exception as e:
                print(f"[VersionManager] ファイル削除エラー: {e}")
        
        return deleted_count
    
    @staticmethod
    def format_file_size(size_bytes: int) -> str:
        """
        ファイルサイズを読みやすい形式に変換
        
        Args:
            size_bytes: バイト数
        
        Returns:
            フォーマットされた文字列（例: "12.5 MB"）
        """
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} TB"