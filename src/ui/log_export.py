"""サーバーパネルの「エクスポート」（画面に出ているログをファイルへ保存する）

FTP / TFTP / SFTP の 3 パネルが同じ作法で保存できるように、ここへまとめた。
作法は SNMP・Syslog の書き出し（syslog_panel._write_text_file_atomically と
SNMPPanel._refuse_if_recording）に合わせる:

  - 一時ファイルへ書き切ってから os.replace で差し替える。保存先を直接
    open('w') すると、その時点で元の内容が消え、書き込みの途中で失敗
    （満杯・共有断・USB 取り外し）すると新旧どちらでもない部分ファイルが
    残る。
  - 端末のログ記録に使われているファイルを選ばれたら断る。差し替えが通れば
    記録済みの内容は失われ、端末は開いたままのハンドルで自分のオフセットから
    書き続けるので、双方のファイルが壊れる。
  - 保存先のフォルダは core/save_defaults.py へ覚える。利用者から見れば
    端末・SNMP・Syslog の保存と同じ「ログファイルの置き場所」なので、
    ここだけ毎回フォルダを辿り直させない（覚える先も 1 つを共有する）。
"""
import os
import tempfile
from datetime import datetime

from PyQt6.QtWidgets import QFileDialog, QMessageBox

from core import save_defaults

# 保存ダイアログの選択肢。中身は素のテキストなので txt と log を出す
FILE_FILTER = ("テキストファイル (*.txt);;ログファイル (*.log);;"
               "すべてのファイル (*.*)")


def default_file_name(stem: str) -> str:
    """保存ダイアログに出す既定のファイル名（SNMP・Syslog と同じ形）"""
    return "%s_%s.txt" % (stem, datetime.now().strftime("%Y%m%d_%H%M%S"))


def write_text_atomically(file_path: str, text: str) -> None:
    """書き切れたときだけ保存先を置き換える

    Args:
        file_path: 保存先のパス
        text: 書き出す本文
    """
    target = os.path.abspath(file_path)
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(target) + ".", suffix=".tmp",
        dir=os.path.dirname(target))
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(text)
        # 閉じてから差し替える（Windows では開いたままだと置き換えられない）
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def export_log_text(panel, text: str, stem: str,
                    title: str = "ログをエクスポート"):
    """画面に出ているログを 1 つのテキストファイルへ保存する

    Args:
        panel: ダイアログの親にするウィジェット。config_manager を持って
            いれば、前回保存したフォルダの出し入れにも使う
        text: 保存する本文（呼ぶ側が画面から取って渡す）
        stem: 既定のファイル名の頭（"tftp_log" など）
        title: 保存ダイアログと警告の見出し

    Returns:
        保存したパス。取り消したときと保存できなかったときは None。
    """
    # config_manager を持たない相手（素のウィジェットを渡すテスト等）でも
    # 保存自体は通す。save_defaults は None を「覚えていない」として扱う
    config_manager = getattr(panel, "config_manager", None)
    # 既定名は 1 回だけ作る。2 回作ると秒がずれ、利用者が名前に触れていない
    # のに「触った」と判定されて拡張子の付け替えが効かない
    default_name = default_file_name(stem)
    file_path, selected_filter = QFileDialog.getSaveFileName(
        panel, title,
        save_defaults.initial_path(config_manager, default_name),
        FILE_FILTER)
    if not file_path:
        return None
    # 選んだ種類に合わせて拡張子を付け替える（SNMP・Syslog・端末と同じ作法）。
    # 捨てていたため、既定名のまま種類だけ「ログファイル (*.log)」へ変えても
    # .txt で保存されていた。付け替えた先が既にあれば、ダイアログが訊いて
    # いない上書きなのでここで確認する（断られたら空が返る）
    file_path = save_defaults.apply_filter_suffix_confirmed(
        panel, title, file_path, selected_filter, default_name)
    if not file_path:
        return None

    from core import log_recording
    device_name = log_recording.device_using(file_path)
    if device_name is not None:
        QMessageBox.warning(
            panel, title,
            "このファイルは %s のログ記録に使用中です:\n%s\n"
            "別のファイルを選ぶか、先にそのログ記録を停止してください。"
            % (device_name, file_path))
        return None

    # 末尾に改行を足す。無いと最後の1行が次に追記した内容とつながる
    if text and not text.endswith("\n"):
        text += "\n"
    try:
        write_text_atomically(file_path, text)
    except Exception as error:
        QMessageBox.critical(panel, "エラー",
                             "エクスポートに失敗しました: %s" % error)
        return None
    # 書き終えてから覚える（取り消し・断り・失敗では変えない）
    save_defaults.remember(config_manager, file_path)
    QMessageBox.information(panel, "成功",
                            "ログを %s にエクスポートしました。" % file_path)
    return file_path
