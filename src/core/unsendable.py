"""機器へ送れない文字（孤立したサロゲート）を見分ける。

孤立したサロゲート（U+D800〜U+DFFF の片割れだけ。壊れた UTF-16）は UTF-8 に
できない。壊れた UTF-16 をクリップボードへ置くアプリからの貼り付けや、
config.json にエスケープで書かれたマクロから入ってくる（PyQt の変換を
通っても残る）。送る直前の encode で例外になると、SSH と Telnet は送信
エラーとして切断し、シリアルは区切り 1 つを黙って落として前後の行が繋がった
まま機器へ届いていた。送る前に丸ごと断る（途中まで送らない）ために使う。
"""
from typing import Optional


def unsendable_index(text: str) -> Optional[int]:
    """text を UTF-8 にできなければ、最初の送れない文字の位置を返す（できれば None）"""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as e:
        return e.start
    return None


def unsendable_notice(text: str, index: int) -> str:
    """送らなかったことを利用者へ知らせる文言（前後の改行は呼び出し側で付ける）"""
    return ("[NetBelt] 送れない文字（壊れた UTF-16 のサロゲート U+%04X、%d 文字目）が"
            "含まれているため、何も送信しませんでした。"
            % (ord(text[index]), index + 1))
