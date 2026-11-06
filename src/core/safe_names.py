# -*- coding: utf-8 -*-
"""Windows で別の実体に化けるファイル名を、ルート拘束の前に断る。

各サーバー（TFTP・FTP・内蔵 SFTP）は realpath と接頭辞の比較でルート配下を
確かめるが、realpath は次の名前を「ルート配下のパス」として通してしまう。
その後の open で OS 側が別の実体に解決するため、拘束を謳った判定が素通りする。

- 予約デバイス名（NUL / CON / PRN / AUX / COM1〜9 / LPT1〜9 / CONIN$ / CONOUT$）。
  NUL へ書いた内容は捨てられるのに転送は成功と表示され、COM1 などは実デバイスを
  開きうる。Windows 10 は NUL.txt のように拡張子が付いていてもデバイスとして
  解決する（Windows 11 では実ファイル）ので、拡張子の手前でも判定する
- コロンを含む名前（`backup.cfg:evil` の代替データストリーム、`C:` のドライブ）。
  既存ファイルに一覧へ出ないストリームを足せる
- 末尾のドット・空白（`config. `）。OS が黙って取り除くため `config` に化け、
  同じ実ファイルへの同時書き込みを断る予約鍵（normcase(realpath)）もすり抜ける

正規のクライアントはどれも使わない名前なので、断っても通常の転送は変わらない。
"""
import re

_RESERVED_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + ["COM%d" % i for i in range(1, 10)]
    + ["LPT%d" % i for i in range(1, 10)]
)

_SEPARATORS = re.compile(r"[\\/]+")


def unsafe_name_reason(name):
    """パスの 1 要素が Windows で別の実体に化けるなら、その理由（日本語）を返す。

    安全なら None。"." と ".." は呼び出し側の拘束判定に任せるので None を返す。
    """
    if name in (".", ".."):
        return None
    if "\x00" in name:
        return "NUL 文字を含む名前"
    if ":" in name:
        return "コロンを含む名前（代替データストリームやドライブ指定）"
    if name != name.rstrip(". "):
        return "末尾がドットか空白の名前（OS が取り除いて別の名前に化ける）"
    stem = name.split(".", 1)[0].rstrip(" ").upper()
    if stem in _RESERVED_DEVICE_NAMES:
        return "Windows の予約デバイス名"
    return None


def check_relative_path(path):
    """相対パスの各要素を unsafe_name_reason で確かめ、化ける要素があれば ValueError。

    区切りは "/" と "\\" のどちらでもよい。空の要素（先頭・末尾・連続の区切り）は
    無視する。拘束そのもの（".." やリンクでルートの外へ出ないこと）は見ない。
    """
    for element in _SEPARATORS.split(path):
        if not element:
            continue
        reason = unsafe_name_reason(element)
        if reason is not None:
            raise ValueError("%s: %s" % (reason, element))
