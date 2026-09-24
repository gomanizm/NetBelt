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

ただしそれだけでは、<br> の代わりに生の改行を入れれば同じ被害が出る
（実測。chr(10) / chr(13) / CRLF / chr(11) / chr(12) / U+0085 / U+2028 /
U+2029 のどれでも 2 行に割れた。Qt が枠の区切りに使う U+FDD0 / U+FDD1 も
同じで、toPlainText() では chr(10) になる）。届いた文字列は insertText へ渡す前に
fold_to_one_line() で 1 行へ畳む。値は捨てずに見える表記へ置き換えるので、
要求名はログにもエクスポートにも残る。

FTP / SFTP のログは転送名が届くのがログイン後なので前提は重いが、同じ
実装を共有しているため 3 パネルとも同じ経路に揃える。
"""
from PyQt6.QtGui import QTextCursor

# 置き換え表。ソースへ生の制御文字を書かないよう chr() で組み立てる
# （見えない文字はレビューでも差分でも追えない）。バックスラッシュも
# エスケープの読み違いを避けるため同じ作り方にする。
_BACKSLASH = chr(92)
_LINE_BREAKS = (
    (chr(0x0A), _BACKSLASH + "n"),
    (chr(0x0D), _BACKSLASH + "r"),
    (chr(0x0B), _BACKSLASH + "v"),
    (chr(0x0C), _BACKSLASH + "f"),
    # Qt の画面では割れないが、保存したログを str.splitlines() などで読むと
    # 行が割れる（Syslog のテキスト保存と同じ基準・同じ表記にそろえる）
    (chr(0x1C), _BACKSLASH + "x1c"),
    (chr(0x1D), _BACKSLASH + "x1d"),
    (chr(0x1E), _BACKSLASH + "x1e"),
    (chr(0x85), _BACKSLASH + "u0085"),
    (chr(0x2028), _BACKSLASH + "u2028"),
    (chr(0x2029), _BACKSLASH + "u2029"),
    # insertText は QTextBeginningOfFrame / QTextEndOfFrame でもブロックを
    # 作り、toPlainText() はそこを chr(10) にして返す（実測）
    (chr(0xFDD0), _BACKSLASH + "ufdd0"),
    (chr(0xFDD1), _BACKSLASH + "ufdd1"),
)


def fold_to_one_line(line):
    """行を割れる文字を見える表記へ置き換え、1 行に畳んで返す。

    値そのものは捨てない。機器から届いた名前をあとから読み解けるように
    しておく（捨てると、何を要求されたのか分からなくなる）。

    Args:
        line: 畳む前の 1 行

    Returns:
        改行を含まない 1 行
    """
    for char, shown in _LINE_BREAKS:
        if char in line:
            line = line.replace(char, shown)
    return line


def append_line(text_edit, line):
    """ログの末尾へ 1 行足し、末尾まで送る

    表示中のカーソル・選択範囲には触らない（別のカーソルで書き足す）。

    Args:
        text_edit: 追記先の QTextEdit
        line: 足す 1 行。HTML として解釈されず、改行で割れることもない
    """
    document = text_edit.document()
    cursor = QTextCursor(document)
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.beginEditBlock()
    if not document.isEmpty():
        cursor.insertBlock()
    cursor.insertText(fold_to_one_line(line))
    cursor.endEditBlock()
    scrollbar = text_edit.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())
