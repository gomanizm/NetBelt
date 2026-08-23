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
            
            for filename in current_files.keys():
                filepath = os.path.join(mibs_dir, filename)
                try:
                    # MIBファイルを解析
                    temp_mibs = self._parse_mib_file_to_dict(filepath)
                    cached_mibs.update(temp_mibs)
                    print(f"[MIBResolver] {filename}: {len(temp_mibs)}件")
                except Exception as e:
                    print(f"[MIBResolver] {filename} エラー: {str(e)}")
            
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
    
    def _parse_mib_file_to_dict(self, filepath: str) -> dict:
        """
        MIBファイルを解析してOID→名前の辞書を返す（最適化版）
        
        Args:
            filepath: MIBファイルのパス
        
        Returns:
            OID→名前の辞書
        """
        import re
        
        result = {}
        
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            # 一時的な名前→OIDマッピング（解析中に使用）
            temp_name_to_oid = dict(self.name_to_oid)  # 既存の辞書をコピー
            
            # 正規表現を事前にコンパイル（パフォーマンス向上）
            oid_pattern = re.compile(r'(\w+)\s+OBJECT\s+IDENTIFIER\s*::=\s*\{\s*(\w+)\s+(\d+)\s*\}')
            object_pattern = re.compile(r'(\w+)\s+OBJECT-TYPE[^:]*::=\s*\{\s*(\w+)\s+(\d+)\s*\}', re.MULTILINE)
            notif_pattern = re.compile(r'(\w+)\s+NOTIFICATION-TYPE[^:]*::=\s*\{\s*(\w+)\s+(\d+)\s*\}', re.MULTILINE)
            
            # OBJECT IDENTIFIER定義を抽出
            for match in oid_pattern.finditer(content):
                name = match.group(1)
                parent = match.group(2)
                index = match.group(3)
                
                # 親のOIDを取得
                parent_oid = temp_name_to_oid.get(parent)
                if parent_oid:
                    oid = f"{parent_oid}.{index}"
                    result[oid] = name
                    temp_name_to_oid[name] = oid
                # 特殊ケース: enterprises
                elif parent == 'enterprises':
                    oid = f"1.3.6.1.4.1.{index}"
                    result[oid] = name
                    temp_name_to_oid[name] = oid
            
            # OBJECT-TYPE定義を抽出
            for match in object_pattern.finditer(content):
                name = match.group(1)
                parent = match.group(2)
                index = match.group(3)
                
                parent_oid = temp_name_to_oid.get(parent)
                if parent_oid:
                    oid = f"{parent_oid}.{index}"
                    result[oid] = name
                    temp_name_to_oid[name] = oid
            
            # NOTIFICATION-TYPE定義を抽出
            for match in notif_pattern.finditer(content):
                name = match.group(1)
                parent = match.group(2)
                index = match.group(3)
                
                parent_oid = temp_name_to_oid.get(parent)
                if parent_oid:
                    oid = f"{parent_oid}.{index}"
                    result[oid] = name
                    temp_name_to_oid[name] = oid
        
        except Exception as e:
            print(f"[MIBResolver] MIBファイル解析エラー: {str(e)}")
        
        return result
    
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