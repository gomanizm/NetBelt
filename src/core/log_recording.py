"""記録中のログファイルを、プロセス全体から見えるようにしておく。

端末のログ記録は TerminalWidget が持っているが、そのファイルを壊せるのは
端末だけではない。SNMP の結果・Trap のエクスポートは保存先を open('w') で
開くので、記録中のファイルを選ばれると記録済みの内容が消え、端末は開いた
ままのハンドルで自分のオフセットから書き続ける（双方のファイルが壊れる）。

保存先を決めた画面がどこであっても同じ判定ができるように、記録中のパスを
ここへ集める。
"""
import os
from typing import Dict, Optional

_recording: Dict[str, str] = {}   # 機器名 -> 記録先のパス


def start(device_name: str, file_path: str) -> None:
    """その機器の記録先を覚える"""
    _recording[device_name] = file_path


def stop(device_name: str) -> None:
    """その機器の記録先を忘れる（記録していなくても呼んでよい）"""
    _recording.pop(device_name, None)


def device_using(file_path: str) -> Optional[str]:
    """file_path を記録先にしている機器名を返す（無ければ None）

    まず実体で比べる。8.3 短縮名・ハードリンク・ジャンクション・UNC と
    割り当てドライブなど、同じファイルを指す別表記は文字列比較では一致せず、
    そのまま素通りしていた。実体を掴めないとき（まだ無いファイル・
    アクセスできない）は絶対化した文字列で比べる。
    """
    wanted = os.path.normcase(os.path.abspath(file_path))
    for device_name, path in _recording.items():
        try:
            if os.path.samefile(path, file_path):
                return device_name
            continue
        except OSError:
            pass
        if os.path.normcase(os.path.abspath(path)) == wanted:
            return device_name
    return None
