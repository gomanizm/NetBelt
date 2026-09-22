"""保存ダイアログへ渡す初期値（前回保存した場所）をまとめる。

保存の入口は端末（全ログ保存・ログ記録開始）、SNMP（結果・Trap の
エクスポート）、Syslog（エクスポート・選択行の保存）と画面をまたいで
散らばっているが、利用者から見ればどれも「ログファイルの保存」であり、
置き場所はたいてい 1 つに決まっている。毎回フォルダを辿り直させない
ために、保存に成功したフォルダをここへ集めて覚える。

覚えるのはフォルダのパス 1 つだけ。機器の設定や資格情報は書かない。
"""
import os

# config.json 上の置き場所（settings.<_SECTION>.<_KEY>）
_SECTION = "paths"
_KEY = "last_save_dir"


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
