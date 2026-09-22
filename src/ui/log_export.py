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
"""
import os
import tempfile
from datetime import datetime

from PyQt6.QtWidgets import QFileDialog, QMessageBox

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
        panel: ダイアログの親にするウィジェット
        text: 保存する本文（呼ぶ側が画面から取って渡す）
        stem: 既定のファイル名の頭（"tftp_log" など）
        title: 保存ダイアログと警告の見出し

    Returns:
        保存したパス。取り消したときと保存できなかったときは None。
    """
    file_path, _ = QFileDialog.getSaveFileName(
        panel, title, default_file_name(stem), FILE_FILTER)
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
    QMessageBox.information(panel, "成功",
                            "ログを %s にエクスポートしました。" % file_path)
    return file_path
