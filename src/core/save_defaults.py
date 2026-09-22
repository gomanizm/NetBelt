"""保存ダイアログへ渡す初期値（前回保存した場所）をまとめる。

保存の入口は端末（全ログ保存・ログ記録開始）、SNMP（結果・Trap の
エクスポート）、Syslog（エクスポート・選択行の保存）と画面をまたいで
散らばっているが、利用者から見ればどれも「ログファイルの保存」であり、
置き場所はたいてい 1 つに決まっている。毎回フォルダを辿り直させない
ために、保存に成功したフォルダをここへ集めて覚える。

覚えるのはフォルダのパス 1 つだけ。機器の設定や資格情報は書かない。
"""
import os
import re

# config.json 上の置き場所（settings.<_SECTION>.<_KEY>）
_SECTION = "paths"
_KEY = "last_save_dir"

# 表や一覧のある画面（SNMP の結果・Trap、Syslog）で使う絞り込み。
# 並びと表記をここへ一本化しておかないと、画面ごとに順番も言い回しも
# ばらけ、どの画面でどの形式を選べるのかが見た目から分からなくなる
TABLE_FILTERS = "テキスト (*.txt);;CSV (*.csv);;JSON (*.json)"

# 絞り込み 1 件から拡張子を拾う（"CSV (*.csv)" -> ".csv"）
_FILTER_EXTENSION = re.compile(r"\*(\.[A-Za-z0-9_]+)")


def _filter_extensions(selected_filter):
    """選ばれた絞り込みが許す拡張子（小文字）。何でも可なら空リスト"""
    if not selected_filter:
        return []
    if "*.*" in selected_filter:
        # 「すべてのファイル (*.*)」。利用者が書いた名前をそのまま使う
        return []
    return [ext.lower() for ext in _FILTER_EXTENSION.findall(selected_filter)]


def apply_filter_suffix(file_path, selected_filter, default_name):
    """保存先の拡張子を、ダイアログで選んだ種類に合わせる

    書き出す形式は拡張子で決まる（_export_format）。ところがダイアログの
    既定の名前は "....txt" 固定で、種類を CSV へ変えても名前は .txt の
    ままなので、CSV を選んだつもりでテキストが書かれていた。

    付け替えるのは「こちらが用意した既定の拡張子のまま」だったときだけ。
    利用者が既定とは違う拡張子を自分で書いたなら、そちらを尊重する
    （種類は既定のまま名前だけ .json と打つ使い方を潰さないため）。
    拡張子が無ければ、選んだ種類のものを足す。

    Args:
        file_path: ダイアログが返した保存先
        selected_filter: ダイアログが返した選択中の絞り込み
        default_name: こちらがダイアログへ渡した既定のファイル名
    """
    if not file_path:
        return file_path
    allowed = _filter_extensions(selected_filter)
    if not allowed:
        return file_path

    root, ext = os.path.splitext(file_path)
    if ext.lower() in allowed:
        return file_path
    if not ext:
        return file_path + allowed[0]

    default_ext = os.path.splitext(default_name or "")[1]
    if default_ext and ext.lower() == default_ext.lower():
        # 既定の名前のまま種類だけ変えた＝選んだ種類が利用者の意思
        return root + allowed[0]
    return file_path


def last_dir(config_manager):
    """前回保存したフォルダを返す（覚えていない・消えていれば None）

    覚えたフォルダは利用者が後から消せる（一時フォルダ・外付け・共有）。
    消えたパスを初期値にすると、ダイアログがどこを開くかは環境任せに
    なるので、実在を確かめてから使う。
    """
    if config_manager is None:
        return None
    try:
        directory = config_manager.get_last_save_dir()
    except Exception:
        # 設定の読み出しで保存の入口を塞がない（初期値が既定へ戻るだけ）
        return None
    if not directory:
        return None
    try:
        if not os.path.isdir(directory):
            return None
    except (OSError, ValueError):
        return None
    return directory


def initial_path(config_manager, default_name, fallback_dir=None):
    """保存ダイアログの初期パスを作る

    Args:
        config_manager: ConfigManager（無ければ None）
        default_name: 既定のファイル名（フォルダを含まない）
        fallback_dir: 覚えていないときのフォルダ。None なら名前だけを返し、
            これまでどおりダイアログの現在地から始まる
    """
    directory = last_dir(config_manager)
    if directory is None:
        directory = fallback_dir
    if not directory:
        return default_name
    return os.path.join(directory, default_name)


def remember(config_manager, file_path):
    """保存に成功したファイルのフォルダを覚える

    呼ぶのは書き終えた後だけ。取り消した・失敗したときに呼ぶと、書けて
    いない場所が次の初期値になる。
    """
    if config_manager is None or not file_path:
        return
    try:
        directory = os.path.dirname(os.path.abspath(file_path))
    except (OSError, ValueError):
        return
    if not directory:
        return
    try:
        config_manager.set_last_save_dir(directory)
    except Exception:
        # 設定を保存できなくても、利用者のファイルは既に書けている。
        # ここで例外にすると成功した保存が失敗に見える
        pass
