"""記録中のログファイルを、プロセス全体から見えるようにしておく。

端末のログ記録は TerminalWidget が持っているが、そのファイルを壊せるのは
端末だけではない。SNMP の結果・Trap のエクスポートは保存先を open('w') で
開くので、記録中のファイルを選ばれると記録済みの内容が消え、端末は開いた
ままのハンドルで自分のオフセットから書き続ける（双方のファイルが壊れる）。

保存先を決めた画面がどこであっても同じ判定ができるように、記録中のパスを
ここへ集める。
"""
import os
from typing import Dict, List, Optional, Set, Tuple

# 機器名 -> [(記録先のパス, 記録を始めたときの実体), ...]。停止したが、停止より
# 前に受信した分をまだ書いている記録も含む（その間に同じ機器で次の記録を
# 始めると、1 台で 2 つになる）。実体は os.stat の結果（掴めなければ None）
_recording: Dict[str, List[Tuple[str, Optional[os.stat_result]]]] = {}
# そのうち停止して書き終えていない記録の (機器名, 記録先のパス)。断るときの
# 案内を、記録中のものと分けるのに使う（in_use_message）
_stopped: Set[Tuple[str, Optional[str]]] = set()


def _stat(file_path: str) -> Optional[os.stat_result]:
    """file_path の実体（os.stat の結果）。掴めなければ None"""
    try:
        return os.stat(file_path)
    except OSError:
        return None


def start(device_name: str, file_path: str) -> None:
    """その機器の記録先を覚える

    実体も、ここで（開けた直後に）覚える。device_using が登録済みのパスを
    毎回 stat すると、記録先が応答しない共有フォルダのとき（停止した記録の
    登録は、書き込みスレッドが閉じ終えるまで残る）、別の機器の記録開始や
    エクスポートのたびに、選んでいないそのパスの stat で GUI が止まる。
    """
    _recording.setdefault(device_name, []).append(
        (file_path, _stat(file_path)))


def mark_stopped(device_name: str, file_path: Optional[str]) -> None:
    """その機器の記録先を、停止して書き終えていないものとして覚える

    停止した記録の登録は、停止より前に受信した分を書き終えて閉じるまで残る
    （stop で外れ、そのとき印も外れる）。そのファイルを選ばれたときに
    「先にそのログ記録を停止してください」と案内すると、すでに停止して
    いるので利用者は従いようがない。
    """
    _stopped.add((device_name, file_path))


def stop(device_name: str, file_path: Optional[str] = None) -> None:
    """その機器の記録先を忘れる（記録していなくても呼んでよい）

    file_path を渡すと、そのパスだけを忘れる。省略するとその機器の全部。
    """
    entries = _recording.get(device_name, [])
    for entry in entries:
        if entry[0] == file_path:
            entries.remove(entry)
            break
    if file_path is None or not entries:
        _recording.pop(device_name, None)
    _stopped.difference_update(
        [key for key in _stopped if key[0] == device_name
         and (file_path is None or key[1] == file_path)])


def _entry_using(file_path: str) -> Optional[Tuple[str, str]]:
    """file_path を記録先にしている登録の (機器名, 登録したパス)。無ければ None

    まず実体で比べる。8.3 短縮名・ハードリンク・ジャンクション・UNC と
    割り当てドライブなど、同じファイルを指す別表記は文字列比較では一致せず、
    そのまま素通りしていた。実体を掴めないとき（まだ無いファイル・
    アクセスできない）は絶対化した文字列で比べる。
    stat するのは選ばれた file_path だけ。登録済みのパスは、記録を始めた
    ときに覚えた実体と比べる（応答しない記録先で GUI を止めない）。
    """
    wanted = os.path.normcase(os.path.abspath(file_path))
    found = _stat(file_path)
    for device_name, entries in _recording.items():
        for path, known in entries:
            if found is not None and known is not None:
                if os.path.samestat(known, found):
                    return device_name, path
                continue
            if os.path.normcase(os.path.abspath(path)) == wanted:
                return device_name, path
    return None


def device_using(file_path: str) -> Optional[str]:
    """file_path を記録先にしている機器名を返す（無ければ None）"""
    entry = _entry_using(file_path)
    return entry[0] if entry is not None else None


def in_use_message(file_path: str) -> Optional[str]:
    """file_path が記録先なら、保存・記録を断るときの案内文を返す（無ければ None）

    端末の記録・全ログ保存と、SNMP・Syslog・サーバーパネルのエクスポートが
    同じ文で断る。判定は device_using と同じで、stat するのは 1 回だけ
    （応答しない記録先のファイルが選ばれることがあるので、2 度 stat しない）。
    停止して書き終えていない記録のファイルには「停止してください」と案内
    しない（mark_stopped）。
    """
    entry = _entry_using(file_path)
    if entry is None:
        return None
    if entry in _stopped:
        return ("このファイルは、停止した %s のログ記録が、停止より前に受信した"
                "分をまだ書き込んでいます:\n%s\n書き終えるまで（記録先が応答"
                "するまで）は使えません。別のファイルを選んでください。"
                % (entry[0], file_path))
    return ("このファイルは %s のログ記録に使用中です:\n%s\n"
            "別のファイルを選ぶか、先にそのログ記録を停止してください。"
            % (entry[0], file_path))
