"""サーバーパネルのアクティビティログへ 1 行足す（届いた文字列を平文で積む）

QTextEdit.append() は渡された文字列がリッチテキストらしければ HTML として
解釈する。TFTP は認証が無く、存在しないファイルへの RRQ でもログが 1 行
増えるので、届く相手なら誰でもファイル名に <br> を入れられる。実測
（基準 f4cad23、ファイル名 'missing<br>[12:00:00] [192.0.2.9] 転送完了:
backup.cfg' の RRQ 1 件）では、

    0: '[10:33:41] [192.0.2.5] 要求されたファイルがありません: missing'
    1: '[12:00:00] [192.0.2.9] 転送完了: backup.cfg'

と画面のログが 2 行に割れ、2 行目が本物の完了記録に見えた。エクスポートは
toPlainText() をそのまま保存するため、偽の行はファイルにも残り、しかも
元の要求文字列（<br> を含む名前）は失われる。

そこで append() を使わず、QTextCursor.insertText で 1 行足す。これは
QTextEdit.append() が平文を渡されたときに行う処理と同じで、リッチテキスト
の判定だけを通らない。document().setMaximumBlockCount による行数上限は
これまでどおり効く。

FTP / SFTP のログは転送名が届くのがログイン後なので前提は重いが、同じ
実装を共有しているため 3 パネルとも同じ経路に揃える。
"""
from PyQt6.QtGui import QTextCursor


def append_line(text_edit, line):
    """ログの末尾へ 1 行足し、末尾まで送る

    表示中のカーソル・選択範囲には触らない（別のカーソルで書き足す）。

    Args:
        text_edit: 追記先の QTextEdit
        line: 足す 1 行。HTML として解釈されず、そのまま残る
    """
    document = text_edit.document()
    cursor = QTextCursor(document)
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.beginEditBlock()
    if not document.isEmpty():
        cursor.insertBlock()
    cursor.insertText(line)
    cursor.endEditBlock()
    scrollbar = text_edit.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())
