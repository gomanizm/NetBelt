from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QProgressBar, QPushButton, QHBoxLayout
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from ui import theme
import os
import tempfile


class LogSaveWorker(QThread):
    """ログ保存を別スレッドで実行するワーカー"""
    
    progress = pyqtSignal(int)  # 進行状況（0-100）
    finished = pyqtSignal(bool, str)  # 完了（成功/失敗、エラーメッセージ）
    
    def __init__(self, log_text: str, file_path: str):
        super().__init__()
        self.log_text = log_text
        self.file_path = file_path
        self._is_cancelled = False
    
    def cancel(self):
        """保存をキャンセル"""
        self._is_cancelled = True
    
    def run(self):
        """ログ保存を実行

        保存先を直接 open('w') すると、キャンセル済みでも 0 バイトに切り詰め、
        途中キャンセル・途中失敗では新旧どちらでもない部分ファイルが残る
        （利用者が既存ファイルを保存先に選んだ場合、旧内容が黙って消える）。
        同じディレクトリの一時ファイルへ書き切ってから os.replace で差し替え、
        書き切れなかったときは一時ファイルを消して保存先には触れない。
        """
        tmp_path = None
        try:
            if self._is_cancelled:
                self.finished.emit(False, "キャンセルされました")
                return

            total_size = len(self.log_text)
            chunk_size = max(1024, total_size // 100)  # 最低1KB、最大100チャンク

            target = os.path.abspath(self.file_path)
            fd, tmp_path = tempfile.mkstemp(
                prefix=os.path.basename(target) + ".", suffix=".tmp",
                dir=os.path.dirname(target))
            with open(fd, 'w', encoding='utf-8') as f:
                for i in range(0, total_size, chunk_size):
                    if self._is_cancelled:
                        self.finished.emit(False, "キャンセルされました")
                        return

                    chunk = self.log_text[i:i + chunk_size]
                    f.write(chunk)

                    # 進行状況を更新
                    progress_percent = min(100, int((i + chunk_size) / total_size * 100))
                    self.progress.emit(progress_percent)

            # ループの判定はチャンクの切れ目だけなので、最後の write と
            # close のあいだに立ったキャンセルはここまで届かない。
            # 保存先へ触る直前にもう一度見て、立っていれば置き換えない
            # （一時ファイルは finally で消える）。
            if self._is_cancelled:
                self.finished.emit(False, "キャンセルされました")
                return

            # 閉じてから差し替える（Windows では開いたままだと置き換えられない）
            os.replace(tmp_path, target)
            tmp_path = None
            self.finished.emit(True, "")

        except Exception as e:
            self.finished.emit(False, str(e))
        finally:
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass   # 消せなくても保存先は無傷。残骸は .tmp なので見分けがつく


_abandoned_workers = set()


def _abandon_worker(worker):
    """待ち切れなかったワーカーを、終わるまで保持する

    実行中の QThread への参照が全部消えると C++ 側が破棄され、プロセスごと
    落ちる。ダイアログを閉じても消えない場所で持っておき、終わったものは
    次に呼ばれたときに手放す。
    """
    for finished in [w for w in _abandoned_workers if w.isFinished()]:
        _abandoned_workers.discard(finished)
    _abandoned_workers.add(worker)


class LogSaveProgressDialog(QDialog):
    """ログ保存プログレスダイアログ"""

    # ワーカーの停止を待つ上限（ミリ秒）
    WAIT_TIMEOUT_MS = 2000

    def __init__(self, log_text: str, file_path: str, parent=None):
        super().__init__(parent)
        self.log_text = log_text
        self.file_path = file_path
        self.worker = None
        self._success = False
        
        self.setWindowTitle("ログ保存中")
        self.setModal(True)
        self.setMinimumWidth(400)
        
        self._create_ui()
        self._start_save()
    
    def _create_ui(self):
        """UI作成"""
        layout = QVBoxLayout(self)
        
        # メッセージラベル
        self.message_label = QLabel("ログファイルを保存しています...")
        layout.addWidget(self.message_label)
        
        # ファイルパスラベル
        self.path_label = QLabel(self.file_path)
        self.path_label.setWordWrap(True)
        self.path_label.setStyleSheet(
            theme.dim_style(self, font_size="9pt"))
        layout.addWidget(self.path_label)
        
        # プログレスバー
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        
        # ボタンレイアウト
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        # キャンセルボタン
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.clicked.connect(self._on_cancel)
        button_layout.addWidget(self.cancel_button)
        
        layout.addLayout(button_layout)
    
    def _start_save(self):
        """保存処理を開始"""
        self.worker = LogSaveWorker(self.log_text, self.file_path)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()
    
    def _on_progress(self, percent: int):
        """進行状況を更新"""
        self.progress_bar.setValue(percent)
    
    def _on_finished(self, success: bool, error_message: str):
        """保存完了時の処理"""
        if success:
            self._success = True
            self.message_label.setText("保存完了!")
            self.cancel_button.setText("閉じる")
            self.accept()
        else:
            self.message_label.setText(f"保存失敗: {error_message}")
            self.cancel_button.setText("閉じる")
    
    def _on_cancel(self):
        """キャンセルボタンクリック時の処理"""
        self.reject()

    def reject(self):
        """閉じる前にワーカーを止めて待つ

        キャンセルボタンだけでなく Esc やタイトルバーの × もここへ来る
        （QDialog は Esc と closeEvent で reject() を呼ぶ）。ここで止めないと
        進捗表示だけが消えて裏で書き込みが続き、直後にアプリを終了すると
        途中で切れたファイルが黙って残る。

        ただし無期限には待たない。キャンセルの判定はチャンクの切れ目だけ
        なので、1 回の write が返ってこない保存先（応答しない共有フォルダ
        など）では wait() も返らず、GUI スレッドごと固まる。待ち切れないときは
        待つのをやめる。書き込み先は一時ファイルで、保存先へ移すのは書き切った
        あとの os.replace 1 回だけなので、保存先のファイルは無傷のまま残る。
        """
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            if not self.worker.wait(self.WAIT_TIMEOUT_MS):
                # 閉じたダイアログへ通知が届かないよう切り離してから手放す
                try:
                    self.worker.progress.disconnect(self._on_progress)
                    self.worker.finished.disconnect(self._on_finished)
                except TypeError:
                    pass    # すでに切れている
                _abandon_worker(self.worker)
        super().reject()
    
    def exec(self) -> bool:
        """
        ダイアログを表示して保存処理を実行

        Returns:
            bool: 保存が成功した場合True
        """
        result = super().exec()
        return self._success

    def release(self) -> None:
        """使い終わったダイアログを手放す

        親（端末）はアプリと同じ寿命なので、閉じただけでは保存のたびに
        子として積み上がる。ただしワーカーを持ったまま捨てると、実行中の
        QThread への参照が消えてプロセスごと落ちる。成功で閉じた直後も
        run() は戻り切っていない（完了の通知は run() の中から出る）ので、
        終わっていなければ reject() と同じく預けてから捨てる。
        """
        if self.worker is not None and not self.worker.isFinished():
            # 捨てたダイアログへ通知が届かないよう切り離してから手放す
            try:
                self.worker.progress.disconnect(self._on_progress)
                self.worker.finished.disconnect(self._on_finished)
            except TypeError:
                pass    # すでに切れている
            _abandon_worker(self.worker)
        self.setParent(None)
        self.deleteLater()
