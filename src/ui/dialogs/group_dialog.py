"""グループ追加/編集ダイアログ"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QHBoxLayout, QMessageBox, QTextEdit, QLabel
)
from typing import Optional, List

class GroupDialog(QDialog):
    """グループ追加/編集ダイアログ"""
    
    def __init__(self, parent=None, group_name: Optional[str] = None,
                 existing_groups: list = None, auto_commands: List[str] = None):
        """
        初期化

        Args:
            parent: 親ウィジェット
            group_name: 編集時のグループ名（新規追加時はNone）
            existing_groups: 既存のグループ名リスト（重複チェック用）
            auto_commands: 編集時の自動実行コマンド（新規追加時はNone）
        """
        super().__init__(parent)
        self.group_name = group_name
        self.existing_groups = existing_groups or []
        self.auto_commands = list(auto_commands or [])
        self.is_edit_mode = group_name is not None

        self.setWindowTitle("グループ編集" if self.is_edit_mode else "グループ追加")
        self.setModal(True)
        self.resize(500, 400)

        self._create_ui()
        self._load_data()
    
    def _create_ui(self):
        """UI作成"""
        layout = QVBoxLayout(self)
        
        # フォームレイアウト
        form_layout = QFormLayout()
        
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例: 開発環境")
        form_layout.addRow("グループ名:", self.name_edit)
        
        layout.addLayout(form_layout)

        # 自動実行コマンド
        layout.addWidget(QLabel("自動実行コマンド:"))

        # 折り返しを切ると、ラベルのテキスト全長がダイアログの最小幅になり
        # resize() の指定が効かなくなる（実測で 500 指定に対し 790 になった）
        self.auto_commands_help_label = QLabel(
            "1行に1コマンド。このグループの機器へ SSH / Telnet で接続した直後に、"
            "上から順に送信されます。空欄でも構いません。")
        self.auto_commands_help_label.setWordWrap(True)
        layout.addWidget(self.auto_commands_help_label)

        self.auto_commands_edit = QTextEdit()
        self.auto_commands_edit.setPlaceholderText(
            "terminal length 0\nterminal monitor")
        layout.addWidget(self.auto_commands_edit)

        # config.json の auto_commands は暗号化されない（暗号化されるのは機器の
        # パスワードのみ）。秘密情報を書かせないよう明示する。
        self.auto_commands_warning_label = QLabel(
            "※ ここに書いた内容は config.json に平文で保存されます。"
            "パスワードなどの秘密情報は書かないでください。")
        self.auto_commands_warning_label.setWordWrap(True)
        layout.addWidget(self.auto_commands_warning_label)

        # OK/キャンセルボタン
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        self.ok_btn = QPushButton("OK")
        self.ok_btn.clicked.connect(self._on_ok)
        self.cancel_btn = QPushButton("キャンセル")
        self.cancel_btn.clicked.connect(self.reject)
        
        button_layout.addWidget(self.ok_btn)
        button_layout.addWidget(self.cancel_btn)
        
        layout.addLayout(button_layout)
    
    def _load_data(self):
        """データを読み込み（編集モード時）"""
        if self.is_edit_mode and self.group_name:
            self.name_edit.setText(self.group_name)
        self.auto_commands_edit.setPlainText("\n".join(self.auto_commands))
    
    def _on_ok(self):
        """OKボタン押下時の処理"""
        new_name = self.name_edit.text().strip()
        
        # 入力チェック
        if not new_name:
            QMessageBox.warning(self, "入力エラー", "グループ名を入力してください。")
            return
        
        # 予約語チェック
        if new_name == "コンソール接続":
            QMessageBox.warning(
                self, 
                "入力エラー", 
                "「コンソール接続」は予約語のため使用できません。"
            )
            return
        
        # 重複チェック（編集時は元の名前は除外）
        check_list = [g for g in self.existing_groups if g != self.group_name]
        if new_name in check_list:
            QMessageBox.warning(
                self, 
                "入力エラー", 
                f"グループ '{new_name}' は既に存在します。"
            )
            return
        
        self.accept()
    
    def get_group_name(self) -> str:
        """入力されたグループ名を取得"""
        return self.name_edit.text().strip()

    def get_auto_commands(self) -> List[str]:
        """入力された自動実行コマンドを取得（空行は除く。空リストもあり得る）"""
        lines = self.auto_commands_edit.toPlainText().split("\n")
        return [line.strip() for line in lines if line.strip()]
