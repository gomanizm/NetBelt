"""
更新ダイアログ
"""

import os
import sys
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QTextEdit, QWidget, QMessageBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from core.version_manager import (
    VersionManager, running_from_source, SOURCE_RUN_MESSAGE)


# 中止したダウンロードは、終わるまでここで生かしておく。実行中の QThread が
# 破棄されると Qt はその場でプロセスを落とす（実測 0xC0000409）。ダイアログ
# とその親の破棄に巻き込ませないよう、所有者をこちらへ移す。
_RUNNING_DOWNLOADS = set()


def quit_for_update(window=None) -> None:
    """更新を当てるためにアプリを終わらせる（後始末を通してから）。

    QApplication.quit() はウィンドウへ closeEvent を送らない。そのまま呼ぶと
    MainWindow.closeEvent だけが通る後始末 — 接続の切断、配送待ちの受信の
    取り込み、TerminalWidget.finish_log_recordings による記録の書き切りと
    停止 — を飛ばして終わる。記録中のログはファイルを閉じられず、描き待ちの
    受信はそのまま捨てられていた（実測: 記録を始めてから描き待ちを 19 文字
    作って quit すると、その分が記録に残らない）。

    先に閉じて通常の終了と同じ道を通す。window を渡さなければ、表示中の
    トップレベルの窓をまとめて閉じる（更新ダイアログからの適用。呼ぶ側は
    まだ exec() の中で、親を辿るより Qt に任せる方が確実）。起動時の適用は
    メインウィンドウがまだ表示されていないことがあるので、その窓を渡す。

    閉じる方に失敗しても updater.bat は既に起動していて、数秒後に実行中の
    ファイルを置き換えに来るので、終わらせる方は必ず通す。
    """
    from PyQt6.QtWidgets import QApplication

    try:
        if window is not None:
            window.close()
        else:
            QApplication.closeAllWindows()
    except Exception as e:
        print(f"[Update] 終了前の後始末に失敗: {e}")
    QApplication.quit()


def _release_when_finished(thread):
    """切り離したスレッドを、終わるまで見届ける。"""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()

    def wait_briefly():
        # アプリ終了時だけは短く待つ。ここで待たないと、後始末の最中に
        # 実行中のまま破棄されて同じ落ち方をする
        try:
            thread.wait(5000)
        except RuntimeError:
            pass

    def finished():
        _RUNNING_DOWNLOADS.discard(thread)
        if app is not None:
            try:
                app.aboutToQuit.disconnect(wait_briefly)
            except TypeError:
                pass
        thread.deleteLater()

    _RUNNING_DOWNLOADS.add(thread)
    thread.finished.connect(finished)
    if app is not None:
        app.aboutToQuit.connect(wait_briefly)


class DownloadThread(QThread):
    """ダウンロードを別スレッドで実行"""
    
    progress_updated = pyqtSignal(int, int, int)  # 進捗%, ダウンロード済み, 総サイズ
    download_completed = pyqtSignal(str)  # ZIPファイルパス
    download_failed = pyqtSignal(str)  # エラーメッセージ
    
    def __init__(self, url: str, github_token: str = None, parent=None,
                 sha256_url: str = None, version: str = None):
        super().__init__(parent)
        self.url = url
        self.sha256_url = sha256_url
        # ダウンロードした ZIP の傍らへ控える版。次回起動時に
        # 「これは今より新しいか」を判断するのに要る。
        self.version = version
        self._cancelled = False
        self.version_mgr = VersionManager(github_token=github_token)
    
    def run(self):
        """ダウンロード実行"""
        try:
            zip_path = self.version_mgr.download_update(
                self.url,
                progress_callback=self._on_progress,
                sha256_url=self.sha256_url,
                version=self.version,
                cancel_check=lambda: self._cancelled
            )
            
            if zip_path:
                self.download_completed.emit(zip_path)
            else:
                # 理由が分かっているときは、それを伝える。一律に
                # 「チェックサムが一致しない場合も含みます」と出していたため、
                # 別の NetBelt が同じ更新を保存中でも、原因と違うことを
                # 名指ししていた（凍結ビルドでは print はログファイル行きで、
                # 画面には何も出ない）。
                reason = getattr(self.version_mgr, "last_failure", None)
                self.download_failed.emit(
                    reason or
                    "ダウンロードに失敗しました"
                    "（チェックサムが一致しない場合も含みます）")
        
        except Exception as e:
            self.download_failed.emit(f"エラー: {str(e)}")
    
    def cancel(self):
        """ダウンロードの中止を要求する。

        terminate() は任意の位置でスレッドを殺すため、書きかけのファイルを
        残してしまう。ここではフラグを立て、ダウンロードループ側に
        後始末をさせてから抜けてもらう。
        """
        self._cancelled = True
        # フラグはチャンクの区切りでしか見られない。相手が黙り込んで
        # いると読み取りのタイムアウトまで戻ってこないので、応答も閉じる
        self.version_mgr.abort()

    def _on_progress(self, progress: int, downloaded: int, total: int):
        """プログレス更新"""
        self.progress_updated.emit(progress, downloaded, total)


class UpdateDialog(QDialog):
    """更新ダイアログ"""
    
    # 戻り値の定数
    UPDATE_NOW = 1
    UPDATE_LATER = 2
    SKIP_VERSION = 3
    
    def __init__(self, parent, update_info: dict):
        """
        初期化
        
        Args:
            parent: 親ウィジェット
            update_info: 更新情報
                {
                    'version': str,
                    'release_notes': str,
                    'download_url': str,
                    'published_at': str
                }
        """
        super().__init__(parent)
        
        self.update_info = update_info
        self.downloaded_zip_path = None
        self.download_thread = None
        
        self.setWindowTitle("更新プログラム利用可能")
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)
        
        # UI構築
        self._setup_ui()
        
        # 初期状態は通知フェーズ
        self._show_notification_phase()
    
    def _setup_ui(self):
        """UI構築"""
        layout = QVBoxLayout(self)
        
        # タイトルラベル
        self.title_label = QLabel()
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self.title_label.font()
        font.setPointSize(12)
        font.setBold(True)
        self.title_label.setFont(font)
        layout.addWidget(self.title_label)
        
        # バージョン情報ラベル
        try:
            from __version__ import __version__
            current_version = __version__
        except ImportError:
            current_version = "1.1.1"
        new_version = self.update_info.get('version', '不明')
        
        self.version_label = QLabel(f"現在: v{current_version}  →  新規: v{new_version}")
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.version_label)
        
        layout.addSpacing(10)
        
        # リリースノートラベル
        notes_label = QLabel("リリースノート:")
        layout.addWidget(notes_label)
        
        # リリースノート表示エリア
        self.notes_text = QTextEdit()
        self.notes_text.setReadOnly(True)
        # リリースノートは Markdown で書かれている。そのまま出すと
        # 見出しの # や箇条書きの - が記号のまま並んで読みにくい
        notes = self.update_info.get('release_notes') or 'リリースノートがありません'
        try:
            self.notes_text.setMarkdown(notes)
        except Exception:
            self.notes_text.setPlainText(notes)
        self.notes_text.setMaximumHeight(150)
        layout.addWidget(self.notes_text)
        
        layout.addSpacing(10)
        
        # プログレスバー（初期は非表示）
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        # ダウンロード情報ラベル（初期は非表示）
        self.download_info_label = QLabel()
        self.download_info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.download_info_label.setVisible(False)
        layout.addWidget(self.download_info_label)
        
        # ステータスラベル
        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)
        
        layout.addSpacing(10)
        
        # ボタンコンテナ
        self.button_container = QWidget()
        self.button_layout = QHBoxLayout(self.button_container)
        self.button_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.button_container)
        
        # ボタン作成
        self.download_button = QPushButton("今すぐダウンロード")
        self.download_button.clicked.connect(self._on_download_clicked)
        
        self.later_button = QPushButton("後で")
        self.later_button.clicked.connect(self._on_later_clicked)
        
        self.skip_button = QPushButton("スキップ")
        self.skip_button.clicked.connect(self._on_skip_clicked)
        
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        
        self.apply_button = QPushButton("更新を適用")
        self.apply_button.clicked.connect(self._on_apply_clicked)
    
    def _show_notification_phase(self):
        """フェーズ1: 更新通知を表示"""
        self.title_label.setText("更新プログラム利用可能")
        self.status_label.setText("")
        
        # ボタンを配置
        self._clear_buttons()
        self.button_layout.addWidget(self.download_button)
        self.button_layout.addWidget(self.later_button)
        self.button_layout.addWidget(self.skip_button)
        
        self.download_button.setVisible(True)
        self.later_button.setVisible(True)
        self.skip_button.setVisible(True)
    
    def _show_downloading_phase(self):
        """フェーズ2: ダウンロード中を表示"""
        self.title_label.setText("更新プログラムをダウンロード中...")
        
        # プログレスバーを表示
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        
        self.download_info_label.setVisible(True)
        self.download_info_label.setText("0 B / 0 B")
        
        self.status_label.setText("ダウンロード中...")
        
        # ボタンを配置
        self._clear_buttons()
        self.button_layout.addStretch()
        self.button_layout.addWidget(self.cancel_button)
        self.button_layout.addStretch()
        
        self.cancel_button.setVisible(True)
    
    def _show_completed_phase(self):
        """フェーズ3: ダウンロード完了を表示"""
        self.title_label.setText("ダウンロード完了！")
        
        # プログレスバーを100%に
        self.progress_bar.setValue(100)
        
        self.status_label.setText(
            "✅ ダウンロード完了\n\n"
            "更新を適用するとアプリケーションが再起動されます。"
        )
        
        # ボタンを配置
        self._clear_buttons()
        self.button_layout.addStretch()
        self.button_layout.addWidget(self.apply_button)
        self.button_layout.addStretch()
        
        self.apply_button.setVisible(True)
    
    def _clear_buttons(self):
        """ボタンをすべて非表示にしてレイアウトからクリア"""
        while self.button_layout.count():
            item = self.button_layout.takeAt(0)
            if item.widget():
                item.widget().setVisible(False)
    
    def _on_download_clicked(self):
        """ダウンロードボタンがクリックされた"""
        download_url = self.update_info.get('download_url')
        
        if not download_url:
            QMessageBox.warning(
                self,
                "エラー",
                "ダウンロードURLが見つかりません。"
            )
            return
        
        # ダウンロードフェーズに移行
        self._show_downloading_phase()
        
        # ダウンロードスレッドを開始（トークンを渡す）
        from core.config_manager import ConfigManager
        config_mgr = ConfigManager()
        github_token = config_mgr.get_github_token()
        
        self.download_thread = DownloadThread(
            download_url, github_token, self,
            sha256_url=self.update_info.get('sha256_url'),
            version=self.update_info.get('version'))
        self.download_thread.progress_updated.connect(self._on_progress_updated)
        self.download_thread.download_completed.connect(self._on_download_completed)
        self.download_thread.download_failed.connect(self._on_download_failed)
        self.download_thread.start()
    
    def _on_progress_updated(self, progress: int, downloaded: int, total: int):
        """ダウンロード進捗更新"""
        self.progress_bar.setValue(progress)
        
        downloaded_str = VersionManager.format_file_size(downloaded)
        total_str = VersionManager.format_file_size(total)
        
        self.download_info_label.setText(f"{downloaded_str} / {total_str}")
    
    def _on_download_completed(self, zip_path: str):
        """ダウンロード完了"""
        self.downloaded_zip_path = zip_path
        self._release_download_thread()
        self._show_completed_phase()
    
    def _on_download_failed(self, error_message: str):
        """ダウンロード失敗"""
        # 先に手放す。持ったままだと、閉じるときに「ダウンロード中です」と
        # 聞き直すことになる
        self._release_download_thread()
        QMessageBox.critical(
            self,
            "ダウンロードエラー",
            f"ダウンロードに失敗しました。\n\n{error_message}"
        )
        self.reject()
    
    def _on_apply_clicked(self):
        """更新適用ボタンがクリックされた"""
        # ソース実行では当てない。配布物はビルド済みの exe 一式で、
        # 展開先はリポジトリ直下、再起動先は Python 本体になる。
        if running_from_source():
            QMessageBox.information(self, "更新", SOURCE_RUN_MESSAGE)
            return

        # updater.batのパスを取得
        if getattr(sys, 'frozen', False):
            # PyInstallerでビルドされている場合
            app_dir = os.path.dirname(sys.executable)
            app_path = sys.executable
        else:
            # 開発環境の場合
            # __file__ = <アプリのルート>\src\ui\dialogs\update_dialog.py
            # 3回上がって src/ に、さらに1回上がってアプリのルートに
            app_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            app_path = sys.executable
        
        updater_path = os.path.join(app_dir, "updater.bat")
        
        # デバッグ情報を表示
        print(f"[UpdateDialog] app_dir: {app_dir}")
        print(f"[UpdateDialog] updater_path: {updater_path}")
        print(f"[UpdateDialog] zip_path: {self.downloaded_zip_path}")
        print(f"[UpdateDialog] app_path: {app_path}")
        print(f"[UpdateDialog] updater.bat exists: {os.path.exists(updater_path)}")
        
        # updater.batの存在確認
        if not os.path.exists(updater_path):
            QMessageBox.critical(
                self,
                "エラー",
                f"updater.batが見つかりません。\n\nパス: {updater_path}"
            )
            return

        # 表示した版と同じものを渡す。ダウンロード先が版ごとに分かれる前は、
        # 後から来た受信が、先に表示したダイアログの ZIP を置き換えられた。
        # 適用時は存在確認しかしていなかったので、そのまま別の版が当たる。
        # 同じ確認は起動時の適用経路（MainWindow._apply_pending_update）も
        # 通る。片方だけ直る形にしないため VersionManager へまとめてある。
        # 確認を通ってから updater.bat が ZIP を開き直すまでにも間があるので、
        # 確かめた写しを作り、updater.bat にはそのパスを渡す
        # （VersionManager.stage_for_apply の説明を参照）。
        # 写しを作るのは updater.bat の存在を確かめた後。先に作っていたときは、
        # 見つからずに戻るたびに写しが更新フォルダへ溜まっていた。
        version_mgr = VersionManager()
        staged_path, problem = version_mgr.stage_for_apply(
            self.downloaded_zip_path, self.update_info.get('version'))
        if problem:
            QMessageBox.warning(self, "エラー", problem)
            return

        # updater.batを起動
        try:
            # updater.bat <ZIPパス> <実行ファイルパス>
            # 起動できなければ、写しを片付けてから例外が戻ってくる
            version_mgr.launch_updater(updater_path, staged_path, app_path)

            # ダイアログを閉じる
            self.done(self.UPDATE_NOW)

            # アプリケーションを終了する。記録中のログを閉じずに終わらない
            # よう、窓の closeEvent を通してから終わらせる（quit_for_update）。
            # ここはまだ exec() の中なので、入れ子のループを抜けてから
            # 後始末が走るように予約する
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, quit_for_update)

        except Exception as e:
            QMessageBox.critical(
                self,
                "エラー",
                f"更新プロセスの起動に失敗しました。\n\n{str(e)}"
            )
    
    def _on_later_clicked(self):
        """後でボタンがクリックされた"""
        self.done(self.UPDATE_LATER)
    
    def _on_skip_clicked(self):
        """スキップボタンがクリックされた"""
        reply = QMessageBox.question(
            self,
            "確認",
            f"バージョン v{self.update_info.get('version')} をスキップしますか？\n\n"
            "次のバージョンがリリースされるまで、この更新は表示されません。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self.done(self.SKIP_VERSION)
    
    def _on_cancel_clicked(self):
        """キャンセルボタンがクリックされた（ダウンロード中）"""
        self.reject()

    def _release_download_thread(self):
        """ダウンロードスレッドをダイアログの寿命から切り離す。

        走ったままなら中止を頼むだけで、終わりは待たない。待つと GUI が
        固まる（実測で 10 秒）。ダイアログの子のまま残すと、親が壊れる
        ときに実行中の QThread ごと破棄されてプロセスが落ちるので、
        親子関係も外して、終わるまで別に抱えておく。
        """
        thread = self.download_thread
        self.download_thread = None
        if thread is None:
            return
        # 片付けの後に完了・失敗が届いても、閉じたダイアログに触らせない
        for signal in (thread.progress_updated, thread.download_completed,
                       thread.download_failed):
            try:
                signal.disconnect()
            except TypeError:
                pass
        thread.setParent(None)
        if thread.isRunning():
            thread.cancel()
            _release_when_finished(thread)
        else:
            thread.deleteLater()

    def _confirm_close(self) -> bool:
        """閉じてよいか確かめ、よければスレッドを手放す。"""
        thread = self.download_thread
        if thread is not None and thread.isRunning():
            reply = QMessageBox.question(
                self,
                "確認",
                "ダウンロード中です。中止して閉じますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return False
        self._release_download_thread()
        return True

    def reject(self):
        """Esc・キャンセル・閉じるを1つの経路へ通す。

        QDialog.reject() は closeEvent を呼ばない。そのため Esc だけが
        確認も中止もされずに閉じ、ダウンロードスレッドが走ったまま
        ダイアログの子として残っていた。
        """
        if not self._confirm_close():
            return
        super().reject()

    def closeEvent(self, event):
        """ダイアログが閉じられる前の処理"""
        if not self._confirm_close():
            event.ignore()
            return
        event.accept()
