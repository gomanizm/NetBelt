"""
バージョン管理とアップデート機能
"""

import os
import sys
import json
import tempfile
import threading
import requests
from typing import Optional, Dict, Callable
from datetime import datetime
import hashlib

# バージョン情報をインポート
try:
    from __version__ import __version__, GITHUB_REPO, APP_NAME
except ImportError:
    # フォールバック
    __version__ = "1.1.1"
    GITHUB_REPO = "gomanizm/NetBelt"
    APP_NAME = "NetBelt"


# cmd が区切りや展開に使う文字。.bat は CreateProcess 経由で
# cmd.exe /c "<コマンドライン>" として起動されるため、外側の引用符が
# 剥がれた状態で cmd が読み直す。引用符で包んでも意味が変わってしまう。
# 実測: ^ は黙って消え、%VAR% は展開され、& 以降は別のコマンドとして走る。
#
# ! も入れる。updater.bat 自身も遅延展開と両立しないため中止するが、
# そのときには呼び出し側が既に QApplication.quit() を呼んでいるので、
# 「更新は絶対に当たらないのにアプリだけ先に終了する」形になる。
_CMD_UNSAFE = "&^%!"


def updater_command(updater_path: str, zip_path: str, app_path: str) -> str:
    """updater.bat を起動するコマンド行を組み立てる

    subprocess にリストで渡すと、Windows の list2cmdline は空白かタブを
    含む引数しか引用符で包まない。ところが cmd はコンマと等号も引数の
    区切りとして扱うので、パスに , や = が入っていて空白が無いと引数が
    途中で切れる。updater.bat 自身のパスが切れた場合は一度も起動せず、
    アプリだけ終了して更新が永久に当たらない。

    起動元が2箇所（起動時の未適用更新と、更新ダイアログの「適用」）に
    分かれていて、片方だけ直した状態で再発した。組み立てはここへ寄せる。

    引用符で包んでも救えない文字 (& ^ %) は、黙って失敗させずに
    ValueError にする。呼び出し側はどちらも QMessageBox で理由を出せる。
    updater.bat 自身も同じ理由で ! を検出して中止する。

    Raises:
        ValueError: cmd が意味を変えてしまう文字がパスに含まれるとき
    """
    for path in (updater_path, zip_path, app_path):
        found = [c for c in _CMD_UNSAFE if c in path]
        if found:
            raise ValueError(
                "パスに %s が含まれているため、更新を適用できません。\n"
                "フォルダ名を変えるか、新しい ZIP を手で展開してください。\n"
                "対象: %s" % (" ".join(found), path))
    return '"{}" "{}" "{}"'.format(updater_path, zip_path, app_path)


# ソース実行で更新を当てようとしたときに出す案内。
# 配布 ZIP はビルド済みの exe 一式で、展開先はリポジトリ直下になる。
SOURCE_RUN_MESSAGE = (
    "ソースから実行しているため、更新を自動で適用できません。\n\n"
    "配布物の ZIP はビルド済みの NetBelt.exe 一式で、展開先は\n"
    "このリポジトリの直下になります。追跡しているファイルが\n"
    "上書きされ、再起動も Python 本体が開くだけになります。\n\n"
    "git pull で更新するか、README の手順で ZIP を別のフォルダへ\n"
    "手で展開してください。")


def running_from_source() -> bool:
    """ソースから動いているか（凍結された exe でないか）を返す。"""
    return not getattr(sys, 'frozen', False)


def updater_env() -> dict:
    """updater.bat へ渡す環境変数を組み立てる

    onefile の exe から更新すると、updater.bat 経由で起動し直すのは
    同じパスの exe になる。PyInstaller のブートローダは _PYI_ARCHIVE_FILE が
    自分と同じなら「同一アプリの子プロセス」と見なし、親が終了時に消した
    _MEIxxxx から python DLL を読もうとする。実測では Python が一度も
    起動しないまま `Failed to load Python DLL` で落ちた。ブートローダ段階の
    失敗なので、ログにも excepthook にも何も残らない。

    PYINSTALLER_RESET_ENVIRONMENT=1 を立てると、子は _PYI_* を引き継がず
    自分用の _MEI を展開し直す。ソース実行では何の影響も無い。
    """
    env = dict(os.environ)
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    return env


def sanitized_token(token: Optional[str]) -> Optional[str]:
    """設定された GitHub トークンを、ヘッダへ載せられる形へ整える。

    前後の空白は取り除き、制御文字（CR/LF を含む）が残るものは捨てる。
    requests のヘッダ検証は、値が壊れているとヘッダ値そのもの
    （= `token <トークン>`）を例外文へ埋める。凍結ビルドの stdout は
    %LOCALAPPDATA%\\NetBelt\\logs\\ へ恒久的に退避されるので、その例外を
    そのまま記録するとトークンが平文でディスクに残る。載せなければ、
    その例外自体が起きない。

    Returns:
        使えるトークン、または None（未設定・壊れている）
    """
    if not token:
        return None
    cleaned = token.strip()
    if not cleaned:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in cleaned):
        # 値そのものは出さない。含まれていること自体を伝えるに留める。
        print("[VersionManager] GitHub トークンに使えない文字が含まれるため、"
              "認証なしで続けます")
        return None
    return cleaned


class VersionManager:
    """バージョン管理とアップデート機能を提供するクラス"""
    
    CURRENT_VERSION = __version__
    # 1回で複数世代ぶんを取る。/releases/latest だと最新1件しか返らず、
    # 何世代か飛ばしている利用者へ「その間に何が変わったか」を出せない。
    # 一覧なら要求は同じ1回のままで、下書きと事前公開は自分で除ける。
    GITHUB_API_URL = (
        f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=10")
    # 通知に載せる世代数の上限。全部並べると読まれない
    MAX_NOTES_GENERATIONS = 5
    UPDATE_DIR = os.path.join(tempfile.gettempdir(), f"{APP_NAME}Updates")
    
    def __init__(self, github_token: Optional[str] = None):
        """
        初期化
        
        Args:
            github_token: GitHub Personal Access Token（プライベートリポジトリの場合必須）
        """
        self.github_token = sanitized_token(github_token)
        # 受信中の応答。中止の要求が来たら、これを閉じて読み取りを打ち切る
        self._response = None
        # 更新用ディレクトリを作成。
        # ここで例外を外へ出さない。起動時の未適用更新チェックは同期で
        # VersionManager を作るだけなので、%TEMP%\NetBeltUpdates が通常
        # ファイルになっている／%TEMP% に作成権限が無いといった異常で、
        # 更新機能ではなくアプリ全体が起動できなくなっていた。
        # 作れなかった場合は更新が使えないだけに留め、記録して続ける。
        try:
            os.makedirs(self.UPDATE_DIR, exist_ok=True)
        except Exception as e:
            print(f"[VersionManager] 更新用フォルダを用意できません（更新は使えません）: {e}")

    def _redact(self, text) -> str:
        """外へ出す文字列から、トークンの値を伏せる。

        例外文はトークンを生のままでも、repr を通した形（改行が `\\n` の
        2文字になる）でも持ちうる。どちらの形も伏字にしてから
        print と error へ渡す。
        """
        text = str(text)
        token = self.github_token
        if not token:
            return text
        for form in (token, repr(token)[1:-1]):
            if form:
                text = text.replace(form, '***')
        return text

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
    
    @staticmethod
    def _published(payload) -> list:
        """公開済みのリリースだけを、新しい順で返す。

        一覧には下書きと事前公開も混ざる。並び順は GitHub が新しい順で
        返すが、当てにせず版で並べ直す。
        """
        if isinstance(payload, dict):       # 単一リリースを渡された場合
            payload = [payload]
        if not isinstance(payload, list):
            return []
        published = [r for r in payload
                     if isinstance(r, dict)
                     and not r.get('draft') and not r.get('prerelease')
                     and r.get('tag_name')]
        return sorted(
            published,
            key=lambda r: VersionManager._version_key(
                r.get('tag_name', '').lstrip('v')),
            reverse=True)

    @staticmethod
    def _version_key(version: str) -> tuple:
        """並べ替え用に版を数値の組へ。読めない部分は 0 として扱う。"""
        parts = []
        for chunk in str(version).split('.')[:4]:
            digits = ''.join(c for c in chunk if c.isdigit())
            parts.append(int(digits) if digits else 0)
        while len(parts) < 4:
            parts.append(0)
        return tuple(parts)

    def collect_notes(self, releases: list) -> str:
        """いま入っている版より新しいリリースの内容を、まとめて返す。

        リリースノートに README や告知しか無いと、利用者は何が変わったのか
        分からない。何世代か飛ばしている場合は、その間のぶんも並べる。
        多すぎると読まれないので上限を設ける。
        """
        newer = [r for r in releases
                 if self.compare_versions(
                     self.CURRENT_VERSION,
                     r.get('tag_name', '').lstrip('v')) < 0]
        if not newer:
            return releases[0].get('body', '') if releases else ''

        shown = newer[:self.MAX_NOTES_GENERATIONS]
        blocks = []
        for release in shown:
            version = release.get('tag_name', '').lstrip('v')
            body = (release.get('body') or '').strip()
            blocks.append("# v%s\n\n%s" % (version, body or '(内容なし)'))
        if len(newer) > len(shown):
            blocks.append("（さらに古い %d 世代ぶんは省略しました）"
                          % (len(newer) - len(shown)))
        return "\n\n---\n\n".join(blocks)

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

            payload = response.json()
            releases = self._published(payload)
            if not releases:
                return None
            data = releases[0]

            # 最新バージョンを取得
            latest_version = data.get('tag_name', '').lstrip('v')
            
            # バージョン比較
            is_newer = self.compare_versions(self.CURRENT_VERSION, latest_version) < 0
            
            # ダウンロードURLを取得（Windows 64bit用のZIPを探す）
            download_url = None
            download_name = None
            assets = data.get('assets', [])
            
            # CI が付ける配布物の名前。同じリリースに windows を名前に
            # 含む別の ZIP が並ぶと、先頭から拾う版はそちらを掴む。
            # その ZIP 専用の .sha256 まで揃っていれば照合も通ってしまい、
            # 利用者は本体でないものを「更新」として入れることになる。
            wanted_zip = f'{APP_NAME}-v{latest_version}-Windows-Portable.zip'.lower()
            for asset in assets:
                if asset.get('name', '').lower() == wanted_zip:
                    download_url = asset.get('url')  # APIのURLを使用（プライベートリポジトリ対応）
                    download_name = asset.get('name', '')
                    break

            # 正規名の資産が無いリリースを黙って切り捨てないための保険。
            # build-release.yml はこれまでの全リビジョンで上の正規名だけを
            # 作っており、「昔の命名」のリリースは存在しない。ここへ落ちるのは
            # 正規の資産が欠けたリリースだけで、そのときは windows を名前に
            # 含む最初の .zip という弱い選び方に戻る。本体でない ZIP を
            # 掴んだ場合は、実行ファイルが無いことに updater.bat が気づいて
            # exit 1 で止める。
            if not download_url:
                for asset in assets:
                    name = asset.get('name', '').lower()
                    if 'windows' in name and name.endswith('.zip'):
                        download_url = asset.get('url')
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
                'release_notes': self.collect_notes(releases),
                'download_url': download_url,
                'sha256_url': sha256_url,
                'published_at': data.get('published_at', '')
            }
        
        except requests.exceptions.RequestException as e:
            # None を返すと呼び出し側が「更新なし」と区別できず、
            # 通信が壊れていても『最新です』と表示されてしまう。
            # 例外文にはヘッダ値（＝トークン）が混ざりうるので伏せてから出す
            detail = self._redact(e)
            print(f"[VersionManager] 更新チェックエラー: {detail}")
            return {'available': False, 'error': f"更新の確認に失敗しました: {detail}"}
        except Exception as e:
            detail = self._redact(e)
            print(f"[VersionManager] 予期しないエラー: {detail}")
            return {'available': False, 'error': f"更新の確認に失敗しました: {detail}"}
    
    def abort(self) -> None:
        """受信中の応答を閉じ、読み取りを直ちに終わらせる。

        中止の判定はチャンクの区切りでしか行えないので、相手が黙り込むと
        読み取りのタイムアウト（60秒）まで戻ってこない。その間スレッドが
        残り続けるため、ソケット側から打ち切る。別のスレッドから呼ばれる。
        """
        response = self._response
        if response is None:
            return
        try:
            response.close()
        except Exception as e:
            print(f"[VersionManager] 受信の中断に失敗: {e}")

    @staticmethod
    def _safe_name_part(text) -> str:
        """ファイル名に使える文字だけを残す（版はリリースのタグ由来）。"""
        return ''.join(c for c in str(text)
                       if c.isalnum() or c in ('.', '_', '-'))

    @classmethod
    def _download_filename(cls, url: str, version: Optional[str] = None) -> str:
        """ダウンロード先のファイル名を決める。

        GitHub の asset は API 形式の URL（末尾は asset の番号）で取るため、
        basename からはファイル名が分からず、どの版も同じ
        NetBelt-update.zip を共有していた。後から来たダウンロードが、
        別の版を表示しているダイアログの ZIP を静かに置き換えられる。
        版が分かっているときは版ごとに分ける。
        """
        safe = cls._safe_name_part(version) if version else ''
        if safe:
            return f"{APP_NAME}-{safe}.zip"
        filename = os.path.basename(url)
        if not filename.endswith('.zip'):
            filename = f"{APP_NAME}-update.zip"
        return filename

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
            print(f"[VersionManager] チェックサム取得エラー: {self._redact(e)}")
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
        # 受信中に例外が出ても書きかけを残さないよう、外側でも掴んでおく
        part_path = None
        try:
            # ファイル名を生成（版ごとに分ける）
            filename = self._download_filename(url, version)
            
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
            self._response = response
            
            print(f"[VersionManager] レスポンスステータス: {response.status_code}")
            
            response.raise_for_status()
            
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            
            # 検証を通るまでは .part 名で書く。最終名(.zip)で書くと、中断した
            # 未検証ファイルが「未適用の更新」として拾われ、検証なしで適用できる。
            # 名前はダウンロードごとに変える。共有していたときは、同時に
            # 受信した2つが同じ .part を奪い合って両方とも失敗していた。
            part_path = '%s.%d-%d.part' % (zip_path, os.getpid(),
                                           threading.get_ident())
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
            # os.replace は宛先があっても置き換えるので、先に消さない。
            # 消してから改名していたときは、その隙に別の受信が失敗すると
            # 検証済みだった ZIP まで失われた。
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
            print(f"[VersionManager] ダウンロードエラー: {self._redact(e)}")
            # 受信中に切れた場合、ここまでは書きかけが残ったままだった。
            # get_pending_update_files は .zip しか拾わないので掃除にも
            # かからず、利用者が再試行しない限り temp に居座り続ける。
            if part_path:
                self._discard(part_path)
            return None
        finally:
            self._response = None
    
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
                    # 検証を通った ZIP の隣には .sha256 と .version がある。
                    # ZIP だけ消すと孤児として残り続ける（適用したときは
                    # updater.bat が3つとも消すので、適用しなかったぶんが
                    # 溜まる）。
                    for suffix in ('.sha256', '.version'):
                        self._discard(zip_path + suffix)
            except Exception as e:
                print(f"[VersionManager] ファイル削除エラー: {e}")

        # 書きかけ (.part) も片付ける。get_pending_update_files は
        # 「適用できる更新」を返す口なので、未検証の断片をそこへ混ぜる
        # わけにはいかない（検証を通っていないものを適用してしまう）。
        # 掃除だけはここで面倒を見る。まだ書いている最中かもしれないので、
        # ZIP と同じく古くなったものだけを対象にする。
        try:
            for filename in os.listdir(self.UPDATE_DIR):
                if not filename.endswith('.part'):
                    continue
                part_path = os.path.join(self.UPDATE_DIR, filename)
                try:
                    if current_time - os.path.getmtime(part_path) > max_age_seconds:
                        os.remove(part_path)
                        print(f"[VersionManager] 書きかけの更新ファイルを削除: {part_path}")
                        deleted_count += 1
                except Exception as e:
                    print(f"[VersionManager] ファイル削除エラー: {e}")
        except Exception as e:
            print(f"[VersionManager] 書きかけの確認エラー: {e}")

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