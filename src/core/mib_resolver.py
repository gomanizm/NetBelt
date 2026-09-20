"""
MIB解決機能

OIDを人間が読める名前に変換
"""
import os
import sys
from typing import Dict, Optional


# MIB 解析器の版。抽出・解決の規則を変えたら上げる。mib_cache.json は
# この値も鍵にするので、古い解析器が作ったキャッシュがアプリの更新後に
# そのまま使われることがなくなる。
MIB_PARSER_VERSION = '2026-09-20.5'


def app_dir() -> str:
    """アプリのディレクトリを返す。

    凍結ビルド（NetBelt.exe）なら exe のあるディレクトリ、開発実行なら
    リポジトリの直下。mibs/・custom_mibs.json・mib_cache.json は
    ここを基準に探す。作業ディレクトリ相対で開くと、ショートカットの
    「作業フォルダー」が違うだけで別の（あるいは存在しない）MIB を読み、
    同じ OID が別の名前に解決されるうえ、起動したフォルダに
    mib_cache.json を書き散らす。
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def _app_path(name: str) -> str:
    """アプリのディレクトリにある name のパスを返す。

    そこに無く、作業ディレクトリにはあるときだけ、これまでどおり作業
    ディレクトリ側を使う。どちらにも無ければアプリのディレクトリ側。
    """
    primary = os.path.join(app_dir(), name)
    if os.path.exists(primary):
        return primary
    fallback = os.path.abspath(name)
    if os.path.exists(fallback):
        return fallback
    return primary


def _custom_mibs_fingerprint() -> str:
    """custom_mibs.json の中身の指紋（無ければ空文字）。

    mibs/ の定義は custom_mibs.json の名前を親にできるので、そちらを
    直したらキャッシュも作り直す必要がある。mtime ではなく中身を見る。
    コピーで mtime が保たれたり、同じ秒に書き直したりしても取りこぼさない。
    """
    import hashlib
    path = _app_path('custom_mibs.json')
    try:
        with open(path, 'rb') as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return ''


def _mib_file_stamp(path: str) -> dict:
    """mibs/ のファイルが変わったかを見るための印（mtime・大きさ・sha1）。

    mtime だけだと、中身を差し替えても mtime が保たれたとき（固定日時で
    作られた配布アーカイブを展開し直す、mtime を保つコピー）に古い
    キャッシュが使われ続ける（実測）。中身の sha1 まで見る。費用は中身を
    読む分（合成 MIB 45MB・150 ファイルで、キャッシュが効く起動が 0.075 秒
    から 0.11 秒）で、読み込みはバックグラウンドのスレッドで動く。中身を
    読めない（排他ロックなど）ときは sha1 を None にする。
    """
    import hashlib
    stat = os.stat(path)
    try:
        with open(path, 'rb') as f:
            sha1 = hashlib.sha1(f.read()).hexdigest()
    except OSError:
        sha1 = None
    return {'mtime': stat.st_mtime, 'size': stat.st_size, 'sha1': sha1}


class MIBResolver:
    """
    MIB解決クラス
    
    OIDを名前に変換する辞書を管理
    """
    
    def __init__(self):
        self.oid_to_name: Dict[str, str] = {}
        self.name_to_oid: Dict[str, str] = {}
        # 直前の読み込みで mib_cache.json をそのまま使えたか。
        # 読み込み件数の知らせに「（キャッシュ使用）」を付けるかを決める
        self._mib_cache_used = False
        self._load_default_mibs()
        self._load_custom_mibs()
    
    def _load_default_mibs(self):
        """デフォルトMIBを読み込み"""
        
        # 標準MIB-II
        standard_mibs = {
            # 標準 OID ツリーの起点。iso は ASN.1 の暗黙の根で、どの MIB
            # ファイルにも定義が無い。ここに無いと、利用者が mibs/ へ置いた
            # 標準 MIB の `::= { mib-2 n }` が親を引けず、その MIB の配下が
            # 丸ごと解決できない（知らせるのは標準出力の「0件」だけ）。
            '1': 'iso',
            '1.3': 'org',
            '1.3.6': 'dod',
            '1.3.6.1': 'internet',
            '1.3.6.1.2': 'mgmt',
            '1.3.6.1.2.1': 'mib-2',
            '1.3.6.1.3': 'experimental',
            '1.3.6.1.4': 'private',
            '1.3.6.1.4.1': 'enterprises',
            '1.3.6.1.5': 'security',
            '1.3.6.1.6': 'snmpV2',
            # snmpModules は SNMPv2-MIB の MODULE-IDENTITY
            # （snmpMIB ::= { snmpModules 1 }）の親。標準 Trap 名を
            # 引きたい利用者が mibs/ へ SNMPv2-MIB を置いても、これが
            # 無いとその配下が丸ごと解決できない。
            '1.3.6.1.6.3': 'snmpModules',

            # System Group
            '1.3.6.1.2.1.1': 'system',
            '1.3.6.1.2.1.1.1.0': 'sysDescr',
            '1.3.6.1.2.1.1.2.0': 'sysObjectID',
            '1.3.6.1.2.1.1.3.0': 'sysUpTime',
            '1.3.6.1.2.1.1.4.0': 'sysContact',
            '1.3.6.1.2.1.1.5.0': 'sysName',
            '1.3.6.1.2.1.1.6.0': 'sysLocation',
            '1.3.6.1.2.1.1.7.0': 'sysServices',
            
            # Interfaces Group
            '1.3.6.1.2.1.2': 'interfaces',
            '1.3.6.1.2.1.2.1.0': 'ifNumber',
            '1.3.6.1.2.1.2.2': 'ifTable',
            '1.3.6.1.2.1.2.2.1': 'ifEntry',
            '1.3.6.1.2.1.2.2.1.1': 'ifIndex',
            '1.3.6.1.2.1.2.2.1.2': 'ifDescr',
            '1.3.6.1.2.1.2.2.1.3': 'ifType',
            '1.3.6.1.2.1.2.2.1.5': 'ifSpeed',
            '1.3.6.1.2.1.2.2.1.7': 'ifAdminStatus',
            '1.3.6.1.2.1.2.2.1.8': 'ifOperStatus',
            
            # BGP-4 MIB
            '1.3.6.1.2.1.15': 'bgp',
            '1.3.6.1.2.1.15.1': 'bgpVersion',
            '1.3.6.1.2.1.15.2': 'bgpLocalAs',
            '1.3.6.1.2.1.15.3': 'bgpPeerTable',
            '1.3.6.1.2.1.15.3.1': 'bgpPeerEntry',
            '1.3.6.1.2.1.15.3.1.1': 'bgpPeerIdentifier',
            '1.3.6.1.2.1.15.3.1.2': 'bgpPeerState',
            '1.3.6.1.2.1.15.3.1.3': 'bgpPeerAdminStatus',
            '1.3.6.1.2.1.15.3.1.7': 'bgpPeerRemoteAs',
            '1.3.6.1.2.1.15.7': 'bgpTraps',
            '1.3.6.1.2.1.15.7.1': 'bgpEstablished',
            '1.3.6.1.2.1.15.7.2': 'bgpBackwardTransition',
            
            # SNMP Group
            '1.3.6.1.6.3.1.1.4.1.0': 'snmpTrapOID',
            '1.3.6.1.6.3.1.1.4.3.0': 'snmpTrapEnterprise',
            
            # Standard Traps
            '1.3.6.1.6.3.1.1.5.1': 'coldStart',
            '1.3.6.1.6.3.1.1.5.2': 'warmStart',
            '1.3.6.1.6.3.1.1.5.3': 'linkDown',
            '1.3.6.1.6.3.1.1.5.4': 'linkUp',
            '1.3.6.1.6.3.1.1.5.5': 'authenticationFailure',
        }
        
        # デフォルトMIBは標準MIB-IIのみ
        # ベンダー固有MIBは mibs/ ディレクトリにMIBファイルを配置して使用
        self.oid_to_name.update(standard_mibs)
        
        # 逆引き辞書も作成
        self.name_to_oid = {v: k for k, v in self.oid_to_name.items()}
    
    @staticmethod
    def _valid_custom_entries(custom_mibs) -> dict:
        """custom_mibs.json の mibs から、使えるエントリだけを返す。

        custom_mibs.json は利用者が手で書くファイルなので、値が文字列で
        ない（list や数値）ことがある。そのまま辞書へ入れると、逆引きの
        組み立てで例外になった時点で片側だけ更新された状態が残り、
        非文字列の名前が resolve_oid() から返って受け取った側
        （Trap 表の QStandardItem）が Trap 1件ごとに落ちる。

        OID は数字とドットだけ、名前は空でない文字列。外れたものは
        捨てて警告する。
        """
        import re

        if not isinstance(custom_mibs, dict):
            print("[MIBResolver] custom_mibs.json の mibs が辞書ではありません")
            return {}
        valid = {}
        for oid, name in custom_mibs.items():
            if not isinstance(oid, str) or not re.fullmatch(r'\d+(\.\d+)*',
                                                            oid):
                print(f"[MIBResolver] custom_mibs.json の不正なOIDを無視: {oid!r}")
                continue
            if not isinstance(name, str) or not name:
                print(f"[MIBResolver] custom_mibs.json の不正な名前を無視: "
                      f"{oid} -> {name!r}")
                continue
            valid[oid] = name
        return valid

    def _load_custom_mibs(self):
        """カスタムMIBファイルを読み込み（キャッシュ対応）"""
        import json
        import os

        # 1. custom_mibs.jsonを読み込み（アプリのディレクトリ基準）
        custom_mib_file = _app_path('custom_mibs.json')

        if os.path.exists(custom_mib_file):
            try:
                with open(custom_mib_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                custom_mibs = self._valid_custom_entries(
                    data.get('mibs', {}) if isinstance(data, dict) else {})

                # カスタムMIBを登録（検証後にまとめて反映する）
                self.oid_to_name.update(custom_mibs)

                # 逆引き辞書を更新
                for oid, name in custom_mibs.items():
                    self.name_to_oid[name] = oid

                print(f"[MIBResolver] カスタムMIB {len(custom_mibs)}件を読み込みました")
            except Exception as e:
                print(f"[MIBResolver] カスタムMIB読み込みエラー: {str(e)}")
        
        # 2. mibsディレクトリからMIBファイルを読み込み（キャッシュ使用、
        #    アプリのディレクトリ基準）
        mibs_dir = _app_path('mibs')
        if os.path.exists(mibs_dir) and os.path.isdir(mibs_dir):
            # キャッシュを使用して高速化
            cached_mibs = self._load_or_update_mib_cache(mibs_dir)
            
            if cached_mibs:
                # キャッシュから登録
                self.oid_to_name.update(cached_mibs)
                for oid, name in cached_mibs.items():
                    self.name_to_oid[name] = oid
                
                # 「（キャッシュ使用）」は、本当にキャッシュをそのまま
                # 使えたときだけ付ける。常に付けていたので、直前に
                # 「キャッシュを保存できません…次の起動でも解析し直します」
                # と出した後でもこれが続き、打ち消していた（実測）。
                # 初回起動（全ファイルを解析した回）でも同じで、起動が
                # 遅い理由を探している利用者に逆のことを伝えていた
                note = '（キャッシュ使用）' if self._mib_cache_used else ''
                print(f"[MIBResolver] MIBファイルから {len(cached_mibs)}件のOIDを読み込みました{note}")

    def _load_or_update_mib_cache(self, mibs_dir: str) -> dict:
        """
        MIBキャッシュを読み込むか、必要に応じて更新

        解析し直さずにキャッシュをそのまま使えたかを self._mib_cache_used に
        残す（呼び出し側が知らせの文言に使う）。

        Args:
            mibs_dir: MIBファイルのディレクトリ

        Returns:
            OID→名前の辞書
        """
        import json
        import os
        
        # キャッシュは読んだ mibs/ の隣（通常はアプリのディレクトリ）に置く。
        # 作業ディレクトリに書くと起動したフォルダへ散らばる
        cache_file = os.path.join(os.path.dirname(mibs_dir), 'mib_cache.json')
        cached_mibs = {}
        cache_needs_update = False
        custom_fingerprint = _custom_mibs_fingerprint()
        
        # キャッシュファイルの読み込み。MIB ファイルの mtime だけを鍵に
        # すると、custom_mibs.json で親の OID を直しても子が古い親の下に
        # 残り、解析器を直しても古い結果が使われ続ける（どちらも実測）。
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    cached_files = cache_data.get('files', {})
                    cached_mibs = cache_data.get('mibs', {})
                if (cache_data.get('parser') != MIB_PARSER_VERSION
                        or cache_data.get('custom') != custom_fingerprint):
                    cache_needs_update = True
            except:
                cached_files = {}
                cache_needs_update = True
        else:
            cached_files = {}
            cache_needs_update = True
        
        # MIBファイルの変更をチェック（mtime・大きさ・中身の sha1）
        current_files = {}
        try:
            filenames = os.listdir(mibs_dir)
        except OSError as e:
            # os.path.exists / os.path.isdir を通った直後でも、一覧その
            # ものが失敗することがある（起動中の mibs/ の入れ替え、同期
            # クライアント、一覧だけを拒否する ACL）。ここで素通りさせると
            # OSError が MIBResolver.__init__ まで上がり、内蔵の標準 MIB
            # まで 1 件も読めなくなる（実測: 画面には何も出ず、標準出力に
            # 「バックグラウンドMIB読み込みエラー」が 1 行出るだけ）。
            # 読めない MIB ファイルと同じように理由を 1 行残し、前回の
            # 解析結果（読めていれば）で続ける。解析し直していないので
            # キャッシュはそのまま、上書きもしない。
            print(f"[MIBResolver] mibs フォルダの一覧を取得できません"
                  f"（{e}）。今回は MIB ファイルを読み込まず、"
                  f"キャッシュがあればその内容を使います（フォルダを"
                  f"入れ替え中か、アクセス権が無い可能性があります）")
            self._mib_cache_used = True
            return cached_mibs
        for filename in filenames:
            # 拡張子は大小を無視して判定する。Windows はファイル名の大小を
            # 保持するので、CASE.MIB のように大文字で配布された MIB が
            # 無言で解析からも監視対象からも外れていた（実測）。
            # current_files の鍵は実ファイル名のままにして mtime 比較と
            # ファイルを開く経路は変えない。
            if filename.lower().endswith(('.mib', '.txt', '.my')):
                filepath = os.path.join(mibs_dir, filename)
                # 名前が合っても通常のファイルでないもの（vendor.mib という
                # フォルダなど）は外す。開けないので「読めなかったファイル」に
                # なり、キャッシュに記録されないまま起動のたびに全ファイルを
                # 解析し直していた（実測）
                if not os.path.isfile(filepath):
                    # os.path.isfile は壊れたリンクや MAX_PATH を超える
                    # パスでも False になる。フォルダ以外の理由で外れた
                    # ものは、名前が合っているのに黙って消えて見えるので
                    # 名前を 1 行出す。フォルダは利用者にできることが
                    # 無いので、これまでどおり黙って外す
                    if not os.path.isdir(filepath):
                        print(f"[MIBResolver] {filename} はファイルとして"
                              f"開けないので読み込みから外します"
                              f"（壊れたリンク、またはパスが長すぎる"
                              f"可能性があります）")
                    continue
                try:
                    stamp = _mib_file_stamp(filepath)
                except OSError as e:
                    # 一覧に出たあと os.stat までの間に消える・読めなく
                    # なることがある（ウイルス対策の隔離、起動中の
                    # mibs/ の入れ替え、同期クライアント）。ここで
                    # 素通りさせると FileNotFoundError が
                    # MIBResolver.__init__ まで上がり、生き残っている
                    # MIB まで 1 件も読めなくなる（実測: 画面には何も
                    # 出ず、標準出力に「バックグラウンドMIB読み込み
                    # エラー」が 1 行出るだけ）。読めない MIB と同じ
                    # ように理由を 1 行残して外し、残りで続ける
                    print(f"[MIBResolver] {filename} の情報を取得でき"
                          f"ません（{e}）。このファイルは読み込みから"
                          f"外します（読み込み中に消えた、またはアクセス"
                          f"できなくなった可能性があります）")
                    continue
                cached = cached_files.get(filename)
                if (stamp['sha1'] is None and isinstance(cached, dict)
                        and cached.get('mtime') == stamp['mtime']
                        and cached.get('size') == stamp['size']):
                    # 今は中身を読めない（排他ロックなど）が、日時と大きさは
                    # 記録どおり。これまでどおり前回の解析結果を使う
                    stamp = cached
                current_files[filename] = stamp

                # キャッシュと比較
                if filename not in cached_files or cached != stamp:
                    cache_needs_update = True
        
        # ファイルが削除された場合もキャッシュ更新
        if set(cached_files.keys()) != set(current_files.keys()):
            cache_needs_update = True
        
        # キャッシュ更新が必要な場合
        if cache_needs_update:
            print(f"[MIBResolver] MIBファイルを解析中...")
            cached_mibs = {}
            
            # ファイルをまたぐ参照があるので、まず全ファイルから定義を集め、
            # そのあとでまとめて解決する。ベンダー MIB は親ノードを別ファイル
            # (Cisco なら CISCO-SMI.my の ciscoMgmt) で定義するのが普通で、
            # 1 ファイルずつ閉じて解決すると、そこにぶら下がる定義が全滅する。
            all_definitions = []
            per_file = {}
            unread = set()
            for filename in current_files.keys():
                filepath = os.path.join(mibs_dir, filename)
                try:
                    definitions = self._extract_mib_definitions(filepath)
                    per_file[filename] = definitions
                    all_definitions.extend(definitions)
                except Exception as e:
                    # 読めなかったファイルは files に記録しない。記録すると
                    # 次の起動で mtime が一致して欠けたキャッシュが使われる
                    unread.add(filename)
                    # 読めるようになったら取り戻すために毎回解析し直すので、
                    # そのことと止め方を伝える
                    print(f"[MIBResolver] {filename} を読めません（{e}）。"
                          f"このファイルの定義は使われず、読めるようになるまで"
                          f"起動のたびに MIB をすべて解析し直します。止めるには、"
                          f"このファイルを読めるようにする（アクセス権や、"
                          f"他のアプリがロックしていないかを確かめる）か、"
                          f"mibs フォルダから取り除いてください")

            cached_mibs = self._resolve_definitions(all_definitions)
            resolved_names = set(cached_mibs.values())
            for filename, definitions in per_file.items():
                got = sum(1 for d in definitions if d[0] in resolved_names)
                print(f"[MIBResolver] {filename}: {got}件")
            
            # キャッシュファイルに保存
            try:
                cache_data = {
                    'parser': MIB_PARSER_VERSION,
                    'custom': custom_fingerprint,
                    'files': {name: stamp
                              for name, stamp in current_files.items()
                              if name not in unread},
                    'mibs': cached_mibs
                }
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(cache_data, f, indent=2, ensure_ascii=False)
                print(f"[MIBResolver] キャッシュを更新しました")
            except Exception as e:
                # 読めない MIB ファイルと同じ考えで、何が起きているか・
                # 次の起動でもやり直すこと・止め方を伝える。例外だけだと、
                # 書き込めない場所へ Portable 版を置いた利用者に、起動が
                # 毎回遅い理由も直し方も分からない
                print(f"[MIBResolver] キャッシュを保存できません（{e}）。"
                      f"解析した結果は今回の起動でしか使われず、次の起動でも"
                      f"MIB をすべて解析し直します。止めるには、"
                      f"{cache_file} へ書き込めるようにする（アクセス権や、"
                      f"同じ名前のフォルダ・読み取り専用のファイルが無いかを"
                      f"確かめる）か、書き込める場所へアプリを移してください")

        # 解析し直したときは、保存できたかに関わらずキャッシュは使っていない
        self._mib_cache_used = not cache_needs_update
        return cached_mibs
    
    # MIB から拾う定義。現代の MIB はモジュールの根を MODULE-IDENTITY で
    # 定義するので、これを見ないと単一ファイルで完結していても根が解決できず、
    # その配下（Trap が実際に運ぶ通知 OID を含む）が丸ごと落ちる。
    #
    # 名前と親は mib-2 / my-root のようにハイフンを含む（標準 MIB の親は
    # ほぼ全部 mib-2）ので [\w-]+ で拾う。\w+ だと mib-2 が名前 '2' になる。
    #
    # 定義の本体（型キーワードから ::= まで）は、自分の ::= と、別の
    # 定義が始まる行を越えない。最短一致の .*? に任せると、右辺が
    # { 名前 数字 } の形でない（{ x 0 1 } のような複数添字）とき、そこで
    # 止まれずに次の定義の ::= まで伸びて、隣の OID を黙って奪ったうえ
    # 隣の定義を消す。IMPORTS の直後に並ぶ MODULE-IDENTITY も
    # 「名前 MODULE-IDENTITY」に見えるので、同じ理由で根を飲み込む。
    # 右辺が { 名前 数字 } ちょうどでない定義は、その定義だけ落とす。
    #
    # ただし「別の定義が始まる行」に節キーワードの行を混ぜてはいけない。
    # `SYNTAX OBJECT IDENTIFIER` は「名前 OBJECT IDENTIFIER」の形なので、
    # 素直に書くと OBJECT-TYPE の本体がその行で打ち切られ、snmpTrapOID /
    # sysObjectID のような標準 MIB の定義が丸ごと抽出から落ちる。節の
    # キーワード（SYNTAX / WRITE-SYNTAX など）は全部大文字なのに対し、
    # 定義の名前は ASN.1 の値定義なので大文字だけということはない。
    # そこで「先頭の語が全部大文字」の行は定義の始まりとみなさない。
    _MIB_DEFINITION_KEYWORDS = (
        r'(?:OBJECT\s+IDENTIFIER|OBJECT-TYPE|NOTIFICATION-TYPE'
        r'|MODULE-IDENTITY|OBJECT-IDENTITY|OBJECT-GROUP|NOTIFICATION-GROUP'
        r'|MODULE-COMPLIANCE|AGENT-CAPABILITIES|TRAP-TYPE|TEXTUAL-CONVENTION)'
    )
    # 名前と型キーワードの間の空白。実 MIB は名前だけを行に置くことが
    # あるので、抽出の開始（`([\w-]+)\s+<キーワード>`）と同じく改行を
    # 1 つ許す。ここだけ `[ \t]+` に狭めていたため、IMPORTS 節の直後の
    # 根が改行で割れていると境界が見えず、IMPORTS から始まった一致が
    # その根の `::=` まで伸びて偽の名前を登録していた（実測）。
    # `\s+` にせず改行 1 つに限るのは、空行をまたいで別の定義まで
    # 届かないようにするため。
    _MIB_NAME_GAP = r'(?:[ \t]+|[ \t]*\r?\n[ \t]*)'
    _MIB_DEFINITION_BODY = (
        r'(?:(?!::=)(?!\n[ \t]*(?![A-Z][A-Z0-9-]*' + _MIB_NAME_GAP
        + r')[\w-]+' + _MIB_NAME_GAP
        + _MIB_DEFINITION_KEYWORDS + r'\b).)*?'
    )
    _MIB_ASSIGNMENT = r'::=\s*\{\s*([\w-]+)\s+(\d+)\s*\}'
    _MIB_DEFINITION_PATTERNS = (
        r'([\w-]+)\s+OBJECT\s+IDENTIFIER\s*' + _MIB_ASSIGNMENT,
        # 型キーワードから ::= までは「コロンを含まない並び」ではない。
        # 実 MIB はほぼ必ず DESCRIPTION を持ち、そこへ RFC 参照や URL を
        # 書くので、[^:]* にすると本文にコロンが出た時点で定義ごと
        # 取りこぼす。
        r'([\w-]+)\s+OBJECT-TYPE\b' + _MIB_DEFINITION_BODY + _MIB_ASSIGNMENT,
        r'([\w-]+)\s+NOTIFICATION-TYPE\b' + _MIB_DEFINITION_BODY
        + _MIB_ASSIGNMENT,
        r'([\w-]+)\s+MODULE-IDENTITY\b' + _MIB_DEFINITION_BODY
        + _MIB_ASSIGNMENT,
        # ベンダー MIB は中間ノードを OBJECT-IDENTITY で置くことが多い。
        # 拾わないと、その節も配下も丸ごと解決できない。
        r'([\w-]+)\s+OBJECT-IDENTITY\b' + _MIB_DEFINITION_BODY
        + _MIB_ASSIGNMENT,
    )

    @staticmethod
    def _blank_comments_and_strings(text: str) -> str:
        """コメントと文字列の中身を空白にして返す。

        定義は生のテキストに正規表現を掛けて拾う。DESCRIPTION の中や
        `--` コメントの中に `::= { x n }` と書いてあると、そこで一致が
        止まって偽の親を記録する。コメントアウトされた
        `-- old OBJECT-TYPE` が起点になって次の本物の定義を飲むこともある
        （ベンダー MIB は廃止したオブジェクトをこの形で残す）。
        先に中身を消しておけば、どちらも起こらない。

        文字列は閉じる `"` まで。コメントは `--` から行末まで、または
        同じ行の次の `--` まで（ASN.1 の規則）。文字列の中の `--` は
        コメントではない。改行は残す。
        """
        out = []
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == '"':
                j = text.find('"', i + 1)
                if j == -1:
                    j = n
                out.append('"')
                out.append(''.join('\n' if c == '\n' else ' '
                                   for c in text[i + 1:j]))
                out.append('"')
                i = j + 1
            elif ch == '-' and text.startswith('--', i):
                j = i + 2
                while j < n and text[j] != '\n':
                    if text.startswith('--', j):
                        j += 2
                        break
                    j += 1
                out.append(' ' * (j - i))
                i = j
            else:
                out.append(ch)
                i += 1
        return ''.join(out)

    @classmethod
    def _normalize_module_boms(cls, raw: str) -> str:
        """本文に残る BOM（U+FEFF）を、1 文字ずつ改行か空白へ置き換えて返す。

        BOM 付きの MIB を `copy /b A.my+B.my` のように生のまま連結すると、
        A.my に末尾の改行が無ければ `END` の直後に次の BOM が来る。見出しを
        探す正規表現は行頭の BOM しか読み飛ばせないので、そのままでは
        モジュールを区切れず、同じ名前が後勝ちで混ざる（実測: A の Trap が
        B の enterprise の下に付いた）。そこで、直後にモジュールの見出しが
        続く BOM を改行へ置き換える。

        ただし置き換えを本文の見た目だけで決めると、`--` コメントの中の
        `<BOM>NAME DEFINITIONS` でコメントが切れ、その残りが生きたコードに
        なる（実測: 由来を書いたコメントの中の見出しで、直後の定義が区間
        ごと打ち切られて解決できなくなった）。コメント・文字列の中の BOM は、
        その行の残りがモジュールの見出しちょうど（連結したファイルの先頭が
        そのまま続く形）で、かつ直前のモジュールが END で閉じているときだけ
        区切りとして扱う。連結の境目は必ず直前のファイルの END の後ろに
        来るのに対し、由来を書いたコメントはモジュールの途中にあるので、
        この 1 点で分けられる。

        見出しにならない BOM は空白にする。どちらも 1 文字→1 文字なので、
        あとで位置を使う処理がずれない。
        """
        import re

        bom = chr(0xFEFF)
        positions = [m.start() for m in re.finditer(bom, raw)]
        if not positions:
            return raw
        # コメント・文字列の中身は空白になるので、ここに BOM が残っていれば
        # 「コメントの外の BOM」。先頭の 1 つだけなら調べるまでもない
        masked = (raw if positions == [0]
                  else cls._blank_comments_and_strings(raw))
        # コメントの外: 見出しを探す _MIB_MODULE_HEADER と同じ広さで見る
        outside = re.compile(r'[ \t' + bom + r']*[\w-]+[\s' + bom
                             + r']+DEFINITIONS\b')
        # コメント・文字列の中: 行の残りが見出しちょうどのときだけ。
        # 広さはコメントの外と揃える。ここだけ 1 行に収まる見出ししか
        # 認めていなかったので、見出しを折り返した MIB が末尾コメントの
        # 後ろに連結されると区切れず、全部が 1 つのモジュールに混ざった
        # （実測: B-MIB の Trap が C-MIB の配下に付いた）。行末ちょうどの
        # 縛りだけは残すので、`::= { bogus 9 }` が続く幽霊の見出しは弾ける
        inside = re.compile(
            r'[ \t]*[\w-]+[\s' + bom + r']+DEFINITIONS\s*::=\s*BEGIN'
            r'[ \t]*(?:--[^\r\n]*)?\r?$', re.MULTILINE)
        # 直前のモジュールが閉じているか。詳しくは docstring を見ること
        closed = re.compile(r'\bEND\b')
        out = []
        prev = 0
        for i in positions:
            if masked[i] == bom:
                split = outside.match(raw, i + 1) is not None
            else:
                split = (inside.match(raw, i + 1) is not None
                         and closed.search(masked, prev, i) is not None)
            out.append(raw[prev:i])
            out.append('\n' if split else ' ')
            prev = i + 1
        out.append(raw[prev:])
        return ''.join(out)

    # モジュール名。`FOO-MIB DEFINITIONS ::= BEGIN` の FOO-MIB。
    # 行頭の BOM（U+FEFF）も空白と同じく読み飛ばす。BOM 付きの MIB を
    # 連結すると各モジュールの先頭に BOM が残り、見出しを見落として
    # 複数のモジュールが 1 つに混ざる（実測）。utf-8-sig で開いても
    # 消えるのはファイル先頭の BOM だけなので、ここで許す。
    # 読み込み時に BOM は改行へ正規化してあるので、ここに残る BOM は
    # もう無いはずだが、この式だけを使う経路が増えても壊れないよう残す。
    _MIB_MODULE_HEADER = r'^[ \t\ufeff]*([\w-]+)\s+DEFINITIONS\b'

    def _extract_mib_definitions(self, filepath: str) -> list:
        """
        MIBファイルから (名前, 親の名前, 添字, モジュール名) を抜き出す

        ここでは OID へ解決しない。親が別のファイルで定義されていることが
        普通にあるため、解決は全ファイルを読み終えてからまとめて行う。

        モジュール名は `X DEFINITIONS ::= BEGIN` の X（見出しが複数ある
        ファイルは、その定義が書かれた区間の X）。無いファイルは
        ファイル名をモジュール名の代わりにする（ファイル単位の名前空間）。

        Args:
            filepath: MIBファイルのパス

        Returns:
            (名前, 親の名前, 添字, モジュール名) のリスト

        Raises:
            OSError: ファイルを読めなかったとき
        """
        import re

        definitions = []
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                raw = f.read()
            # 本文に残る BOM（U+FEFF）を、連結の区切り（改行）か無害な
            # 空白へ振り分ける。詳しくは _normalize_module_boms を見ること
            raw = self._normalize_module_boms(raw)
            content = self._blank_comments_and_strings(raw)
            # 1 ファイルに複数のモジュールを連結して配る MIB があるので、
            # 見出しの位置ごとに本文を区切り、区間ごとにそのモジュール名を
            # 付ける。ファイル全体に最初の見出しの名前を付けると、2 つの
            # モジュールが同じ名前を定義したとき同じモジュールの同名として
            # 後勝ちになり、片方の子がもう一方の親に付く（実測）。最初の
            # 見出しより前は、これまでどおり最初のモジュールに含める。
            headers = list(re.finditer(self._MIB_MODULE_HEADER, content,
                                       re.MULTILINE))
            if headers:
                bounds = ([0] + [h.start() for h in headers[1:]]
                          + [len(content)])
                sections = [(h.group(1), content[bounds[i]:bounds[i + 1]])
                            for i, h in enumerate(headers)]
            else:
                sections = [(os.path.basename(filepath), content)]
            for module, text in sections:
                for pattern in self._MIB_DEFINITION_PATTERNS:
                    # DOTALL が要る。定義は複数行にまたがるので、`.` が改行を
                    # 拾わないと型キーワードから ::= まで届かない
                    for match in re.finditer(pattern, text,
                                             re.MULTILINE | re.DOTALL):
                        definitions.append(
                            (match.group(1), match.group(2), match.group(3),
                             module))
        except OSError:
            # 読めなかった（排他ロック・ACL など）ことは空の結果にせず、
            # 呼び出し側へ返す。空で返すと解析済みとして mtime ごと
            # キャッシュに記録され、読めるようになっても再解析されない。
            raise
        except Exception as e:
            print(f"[MIBResolver] MIBファイル解析エラー: {str(e)}")

        return definitions

    def _resolve_definitions(self, definitions: list) -> dict:
        """
        (名前, 親の名前, 添字, モジュール名) の並びを OID へ解決する

        親が別のファイルで定義されていることがあるので、解決が進まなく
        なるまで繰り返す。一度で終える（＝読んだ順に解決する）と、親が
        後ろのファイルにある定義が os.listdir() の順しだいで落ちる。

        親は同じモジュールの定義を優先する。名前→OID を全モジュール共通の
        1 枚にすると、2 つの MIB が同じ名前（例: 両方に shared）を自分の
        根の下に定義したとき後に書いた方だけが残り、もう一方の子がよその
        親に付く（実測: 2 つの Trap が互いの enterprise に入れ替わった）。
        同じモジュールに親の定義があるのにまだ解決していなければ、よその
        同名を使わずに次の回を待つ。同じモジュールに無い親（IMPORTS）は
        これまでどおりモジュールをまたいで探す。

        残る制限: 2 つのモジュールが同じ名前を定義し、第三のモジュールが
        その一方を IMPORTS しているとき、IMPORTS を見ていないのでどちらを
        指すか決められず、後に解決した方になる。「後に解決した方」は
        os.listdir() が返すファイルの順と、その定義が何回目の回で解決した
        かで決まるので、同じ mibs/ でも環境によって結果が変わる。しかも
        結果は mib_cache.json に残るので、一度ずれるとキャッシュを
        作り直すまでそのまま使われる。IMPORTS 節（`IMPORTS ... FROM
        <MODULE>;`）を解析して名前ごとに参照先モジュールを持たない限り、
        ここは直らない。曖昧になったこと自体も知らせていない。

        Args:
            definitions: (名前, 親の名前, 添字, モジュール名) のリスト

        Returns:
            解決できた OID→名前 の辞書（同名でも別 OID なら両方残る）
        """
        known = dict(self.name_to_oid)
        declared = {}
        for name, _, _, module in definitions:
            declared.setdefault(module, set()).add(name)
        in_module = {}
        resolved = {}
        pending = list(definitions)

        while pending:
            still_pending = []
            progressed = False
            for name, parent, index, module in pending:
                if parent == 'enterprises':
                    parent_oid = '1.3.6.1.4.1'
                elif parent in declared[module]:
                    parent_oid = in_module.get(module, {}).get(parent)
                else:
                    parent_oid = known.get(parent)
                if parent_oid is None:
                    still_pending.append((name, parent, index, module))
                    continue
                oid = f"{parent_oid}.{index}"
                known[name] = oid
                in_module.setdefault(module, {})[name] = oid
                resolved[oid] = name
                progressed = True
            if not progressed:
                # これ以上どれも解決できない（親がどこにも無い）
                break
            pending = still_pending

        return resolved


    def resolve_oid(self, oid: str) -> str:
        """
        OIDを名前に変換
        
        完全一致がない場合は、部分一致で名前を構築
        例: 1.3.6.1.4.1.9.9.41.1.2.3.1.5.1 -> clogHistMsgText.1
        
        Args:
            oid: OID文字列
        
        Returns:
            解決された名前、または元のOID
        """
        # 完全一致チェック
        if oid in self.oid_to_name:
            return self.oid_to_name[oid]
        
        # 部分一致で最長マッチを探す
        parts = oid.split('.')
        for i in range(len(parts), 0, -1):
            partial_oid = '.'.join(parts[:i])
            if partial_oid in self.oid_to_name:
                # 残りの部分をインデックスとして追加
                remaining = '.'.join(parts[i:])
                if remaining:
                    return f"{self.oid_to_name[partial_oid]}.{remaining}"
                else:
                    return self.oid_to_name[partial_oid]
        
        # 解決できない場合は元のOIDを返す
        return oid
    
    def resolve_name(self, name: str) -> Optional[str]:
        """
        名前をOIDに変換
        
        Args:
            name: MIB名
        
        Returns:
            OID文字列、または None
        """
        return self.name_to_oid.get(name)
    
    def add_custom_mib(self, oid: str, name: str):
        """
        カスタムMIBを追加
        
        Args:
            oid: OID文字列
            name: MIB名
        """
        self.oid_to_name[oid] = name
        self.name_to_oid[name] = oid
    
    def load_mib_file(self, filepath: str):
        """
        MIBファイルを読み込み（将来の拡張用）
        
        Args:
            filepath: MIBファイルのパス
        """
        # TODO: MIBファイルのパース実装
        pass
    
    def get_all_names(self):
        """登録されているすべてのMIB名を取得"""
        return sorted(self.name_to_oid.keys())


# グローバルインスタンス
_resolver = None

def get_resolver() -> MIBResolver:
    """MIBResolverのシングルトンインスタンスを取得"""
    global _resolver
    if _resolver is None:
        _resolver = MIBResolver()
    return _resolver