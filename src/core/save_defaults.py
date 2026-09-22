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

from PyQt6.QtWidgets import QMessageBox

# 覚えたフォルダの置き場所は ConfigManager が持つ
# （settings.paths.last_save_dir。get_last_save_dir / set_last_save_dir）

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


def confirm_suffix_overwrite(parent, title, file_path, new_path):
    """付け替えた先が既にあるとき、上書きしてよいか訊く

    保存ダイアログの上書き確認は、利用者がその場で選んだ名前についてしか
    行われない。こちらが絞り込みに合わせて拡張子を付け替えると、付け替えた
    後の名前は誰も確かめていないので、out.csv があるフォルダで out.txt と
    入れて CSV を選ぶだけで、確認なしに out.csv が置き換わっていた（実測）。

    訊くのは付け替えでパスが変わったときだけ。利用者がダイアログで既存の
    ファイルを直に選んだときは、ダイアログ側で既に訊かれている。

    Args:
        parent: 確認ダイアログの親にするウィジェット
        title: 見出し（保存の入口の名前）
        file_path: ダイアログが返した保存先
        new_path: 拡張子を付け替えた後の保存先

    Returns:
        保存してよければ True。断られたら False（保存も記憶もしない）
    """
    if not new_path or new_path == file_path:
        return True
    try:
        if not os.path.exists(new_path):
            return True
    except (OSError, ValueError):
        # 確かめられない置き場所（長すぎるパス等）。ここで止めると保存
        # 自体ができなくなるので、これまでどおり進める
        return True
    reply = QMessageBox.question(
        parent, title,
        "'%s' が既にあります。上書きしますか？\n%s"
        % (os.path.basename(new_path), new_path),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No
    )
    return reply == QMessageBox.StandardButton.Yes


def apply_filter_suffix_confirmed(parent, title, file_path, selected_filter,
                                  default_name):
    """拡張子を付け替え、付け替え先が既にあれば上書きの確認を取る

    保存の入口から呼ぶのはこちら。apply_filter_suffix だけを呼ぶと、
    付け替えた先の上書きが誰にも確認されないまま通る。

    Returns:
        保存してよいパス。取り消し・断られたときは ""（呼ぶ側の
        `if file_path:` がそのまま使えるよう、None ではなく空文字）
    """
    new_path = apply_filter_suffix(file_path, selected_filter, default_name)
    if not confirm_suffix_overwrite(parent, title, file_path, new_path):
        return ""
    return new_path


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
