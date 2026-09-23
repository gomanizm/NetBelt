import sys
import os
import traceback
# PyQt6 はここで import しない。exe で PyQt6 自体の読み込みに失敗する
# （DLL が読めない、展開が壊れた）と、ログの差し替えより前なので
# 痕跡が残らない。必要になった場所で読み込む


def _setup_logging():
    """凍結(exe)ビルド時は stdout/stderr をログファイルへ退避する。
    windowedビルドでは print 出力先が無いため、診断用にファイルへ残す。

    Returns:
        ログファイルのパス。退避しなかった場合は None
    """
    if not getattr(sys, "frozen", False):
        return None
    try:
        from datetime import datetime
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        log_dir = os.path.join(base, "NetBelt", "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"app_{datetime.now():%Y%m%d}.log")
        f = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = f
        sys.stderr = f
        return log_path
    except Exception:
        return None


def install_excepthook(log_path=None):
    """未捕捉例外を記録し、利用者へ知らせる。

    PyQt6 は、シグナルから呼ばれたスロットの中で未捕捉例外が起きると
    sys.excepthook を呼んだうえでプロセスを abort する。既定の
    sys.__excepthook__ は C レベルの実ファイルディスクリプタへ書くため、
    _setup_logging() が Python オブジェクトとして差し替えた sys.stderr を
    経由しない。console=False の exe にはその出力先が無いので、
    ウィンドウが消えるだけで記録もダイアログも何も残らなくなる。

    自前の excepthook を入れると abort されなくなり、差し替え済みの
    sys.stderr へ書けるようになる。1つのスロットが失敗しても、
    開いている接続まで道連れにはしない。

    Args:
        log_path: 記録先として利用者へ伝えるパス。None なら伝えない
    """
    def hook(exc_type, exc_value, exc_tb):
        # Ctrl+C は障害ではない。既定の扱いに任せる
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return

        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        # 記録が先。知らせる方は QApplication の状態に左右されるので、
        # そちらの失敗で記録まで落とさない
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except Exception:
            pass

        summary = "%s: %s" % (exc_type.__name__, exc_value)
        message = "予期しないエラーが発生しました。\n\n%s" % summary
        if log_path:
            message += "\n\n詳細の記録先:\n%s" % log_path
        try:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(None, "NetBelt", message)
        except Exception:
            pass

    sys.excepthook = hook


def main():
    log_path = _setup_logging()
    install_excepthook(log_path)

    # PyQt6 と UI の import はログの差し替えより後に行う。import 中に
    # 落ちたとき、モジュール先頭で読み込んでいると退避が間に合わず、
    # 起動しない理由がどこにも残らない
    from PyQt6.QtWidgets import QApplication
    from core.single_instance import SingleInstanceGuard
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("NetBelt")

    # 2 つ起動すると、あとから保存した方が相手の追加した機器・パスワード・
    # グループ・マクロを警告なしに消す（config.json は読み込み以降の更新を
    # 見ずに丸ごと書き戻すため）。2 つ目は起動せず、動いている方の窓を
    # 前へ出して終わる。
    # another_instance_is_running() は確認と同時に「起動してよい権利」を
    # 不可分に取る。確認と取得を 2 操作に分けると、ほぼ同時に起動した
    # 2 つがどちらも相手より先に確認を終えて、両方とも開いてしまう
    guard = SingleInstanceGuard()
    if guard.another_instance_is_running():
        return
    guard.listen()

    window = MainWindow()
    guard.set_window(window)
    window.show()

    try:
        code = app.exec()
    finally:
        guard.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
