"""
MIB解決機能

OIDを人間が読める名前に変換
"""
from typing import Dict, Optional


class MIBResolver:
    """
    MIB解決クラス
    
    OIDを名前に変換する辞書を管理
    """
    
    def __init__(self):
        self.oid_to_name: Dict[str, str] = {}
        self.name_to_oid: Dict[str, str] = {}
        self._load_default_mibs()
        self._load_custom_mibs()
    
    def _load_default_mibs(self):
        """デフォルトMIBを読み込み"""
        
        # 標準MIB-II
        standard_mibs = {
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
    
    def _load_custom_mibs(self):
        """カスタムMIBファイルを読み込み（キャッシュ対応）"""
        import json
        import os
        
        # 1. custom_mibs.jsonを読み込み
        custom_mib_file = 'custom_mibs.json'
        
        if os.path.exists(custom_mib_file):
            try:
                with open(custom_mib_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    custom_mibs = data.get('mibs', {})
                    
                    # カスタムMIBを登録
                    self.oid_to_name.update(custom_mibs)
                    
                    # 逆引き辞書を更新
                    for oid, name in custom_mibs.items():
                        self.name_to_oid[name] = oid
                    
                    print(f"[MIBResolver] カスタムMIB {len(custom_mibs)}件を読み込みました")
            except Exception as e:
                print(f"[MIBResolver] カスタムMIB読み込みエラー: {str(e)}")
        
        # 2. mibsディレクトリからMIBファイルを読み込み（キャッシュ使用）
        mibs_dir = 'mibs'
        if os.path.exists(mibs_dir) and os.path.isdir(mibs_dir):
            # キャッシュを使用して高速化
            cached_mibs = self._load_or_update_mib_cache(mibs_dir)
            
            if cached_mibs:
                # キャッシュから登録
                self.oid_to_name.update(cached_mibs)
                for oid, name in cached_mibs.items():
                    self.name_to_oid[name] = oid
                
                print(f"[MIBResolver] MIBファイルから {len(cached_mibs)}件のOIDを読み込みました（キャッシュ使用）")
    
    def _load_or_update_mib_cache(self, mibs_dir: str) -> dict:
        """
        MIBキャッシュを読み込むか、必要に応じて更新
        
        Args:
            mibs_dir: MIBファイルのディレクトリ
        
        Returns:
            OID→名前の辞書
        """
        import json
        import os
        
        cache_file = 'mib_cache.json'
        cached_mibs = {}
        cache_needs_update = False
        
        # キャッシュファイルの読み込み
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)
                    cached_files = cache_data.get('files', {})
                    cached_mibs = cache_data.get('mibs', {})
            except:
                cached_files = {}
                cache_needs_update = True
        else:
            cached_files = {}
            cache_needs_update = True
        
        # MIBファイルのタイムスタンプをチェック
        current_files = {}
        for filename in os.listdir(mibs_dir):
            if filename.endswith(('.mib', '.txt', '.my')):
                filepath = os.path.join(mibs_dir, filename)
                mtime = os.path.getmtime(filepath)
                current_files[filename] = mtime
                
                # キャッシュと比較
                if filename not in cached_files or cached_files[filename] != mtime:
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
            for filename in current_files.keys():
                filepath = os.path.join(mibs_dir, filename)
                try:
                    definitions = self._extract_mib_definitions(filepath)
                    per_file[filename] = definitions
                    all_definitions.extend(definitions)
                except Exception as e:
                    print(f"[MIBResolver] {filename} エラー: {str(e)}")

            resolved = self._resolve_definitions(all_definitions)
            cached_mibs = {oid: name for name, oid in resolved.items()}
            for filename, definitions in per_file.items():
                got = sum(1 for name, _, _ in definitions if name in resolved)
                print(f"[MIBResolver] {filename}: {got}件")
            
            # キャッシュファイルに保存
            try:
                cache_data = {
                    'files': current_files,
                    'mibs': cached_mibs
                }
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(cache_data, f, indent=2, ensure_ascii=False)
                print(f"[MIBResolver] キャッシュを更新しました")
            except Exception as e:
                print(f"[MIBResolver] キャッシュ保存エラー: {str(e)}")
        
        return cached_mibs
    
    # MIB から拾う定義。現代の MIB はモジュールの根を MODULE-IDENTITY で
    # 定義するので、これを見ないと単一ファイルで完結していても根が解決できず、
    # その配下（Trap が実際に運ぶ通知 OID を含む）が丸ごと落ちる。
    _MIB_DEFINITION_PATTERNS = (
        r'(\w+)\s+OBJECT\s+IDENTIFIER\s*::=\s*\{\s*(\w+)\s+(\d+)\s*\}',
        r'(\w+)\s+OBJECT-TYPE[^:]*::=\s*\{\s*(\w+)\s+(\d+)\s*\}',
        r'(\w+)\s+NOTIFICATION-TYPE[^:]*::=\s*\{\s*(\w+)\s+(\d+)\s*\}',
        r'(\w+)\s+MODULE-IDENTITY[^:]*::=\s*\{\s*(\w+)\s+(\d+)\s*\}',
    )

    def _extract_mib_definitions(self, filepath: str) -> list:
        """
        MIBファイルから (名前, 親の名前, 添字) を抜き出す

        ここでは OID へ解決しない。親が別のファイルで定義されていることが
        普通にあるため、解決は全ファイルを読み終えてからまとめて行う。

        Args:
            filepath: MIBファイルのパス

        Returns:
            (名前, 親の名前, 添字) のリスト
        """
        import re

        definitions = []
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            for pattern in self._MIB_DEFINITION_PATTERNS:
                for match in re.finditer(pattern, content, re.MULTILINE):
                    definitions.append(
                        (match.group(1), match.group(2), match.group(3)))
        except Exception as e:
            print(f"[MIBResolver] MIBファイル解析エラー: {str(e)}")

        return definitions

    def _resolve_definitions(self, definitions: list) -> dict:
        """
        (名前, 親の名前, 添字) の並びを OID へ解決する

        親が別のファイルで定義されていることがあるので、解決が進まなく
        なるまで繰り返す。一度で終える（＝読んだ順に解決する）と、親が
        後ろのファイルにある定義が os.listdir() の順しだいで落ちる。

        Args:
            definitions: (名前, 親の名前, 添字) のリスト

        Returns:
            解決できた 名前→OID の辞書
        """
        known = dict(self.name_to_oid)
        resolved = {}
        pending = list(definitions)

        while pending:
            still_pending = []
            progressed = False
            for name, parent, index in pending:
                if parent == 'enterprises':
                    parent_oid = '1.3.6.1.4.1'
                else:
                    parent_oid = known.get(parent)
                if parent_oid is None:
                    still_pending.append((name, parent, index))
                    continue
                oid = f"{parent_oid}.{index}"
                known[name] = oid
                resolved[name] = oid
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