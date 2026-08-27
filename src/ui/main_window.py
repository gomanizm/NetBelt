from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QSplitter, QDockWidget,
    QTabWidget, QScrollArea, QLabel, QTabBar,
    QMenuBar, QMenu, QStatusBar, QMessageBox, QDialog, QToolBar
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QPoint, QTimer
from .device_tree import DeviceTree
from .terminal_widget import TerminalWidget
from .sftp_panel import SFTPPanel
from .sftp_server_panel import SFTPServerPanel
from .tftp_server_panel import TFTPServerPanel
from .ftp_server_panel import FTPServerPanel
from .syslog_panel import SyslogPanel
from .snmp_panel import SNMPPanel
from .dialogs.device_dialog import DeviceDialog
from .dialogs.group_dialog import GroupDialog
from .dialogs.macro_dialog import MacroDialog
from .dialogs.settings_dialog import SettingsDialog
from core.config_manager import ConfigManager
from core.ssh_connection import SSHConnection
from core.serial_connection import SerialConnection
from core.telnet_connection import TelnetConnection
from core.macro_manager import MacroManager
from core.sftp_manager import SFTPManager
from core.syslog_receiver import SyslogReceiver
from core.snmp_manager import SNMPManager
from core.version_manager import VersionManager
from ui import theme
from typing import Dict, Union, Optional
from datetime import datetime
import os

class DetachableTabBar(QTabBar):
    """タブを下方向へ十分ドラッグすると、そのタブを別ウィンドウへ切り離すタブバー。"""

    def __init__(self, on_detach, parent=None):
        super().__init__(parent)
        self._on_detach = on_detach       # 切り離しを実行するコールバック（index を渡す）
        self._press_pos = None
        self._press_index = -1

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            self._press_index = self.tabAt(self._press_pos)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # タブ列から縦方向に離れたら「引き剥がした」とみなす（誤爆を避けるため閾値は大きめ）
        if self._press_pos is not None and self._press_index >= 0:
            delta = event.position().toPoint() - self._press_pos
            if abs(delta.y()) > max(40, self.height() + 20):
                index = self._press_index
                self._press_pos = None
                self._press_index = -1
                self._on_detach(index)
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_pos = None
        self._press_index = -1
        super().mouseReleaseEvent(event)


class DetachedToolWindow(QWidget):
    """ツールを切り離して表示する独立ウィンドウ。

    閉じられたら親へ通知してタブに戻す。closeEvent の最中に reparent すると
    ウィンドウ破棄と競合してクラッシュするため、実際の戻し処理は次の
    イベントループへ回す（singleShot(0)）。
    """

    def __init__(self, key, on_close):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.Window)
        self._key = key
        self._on_close = on_close
        self._closing = False

    def closeEvent(self, event):
        if not self._closing:
            self._closing = True
            QTimer.singleShot(0, lambda k=self._key: self._on_close(k))
        event.accept()


class MainWindow(QMainWindow):
    # 更新通知用のシグナルを追加
    update_available = pyqtSignal(dict)
    update_check_error = pyqtSignal(str)
    no_update_available = pyqtSignal()
    run_auto_commands_requested = pyqtSignal(str)  # 接続後の自動実行コマンド要求

    # ターミナルのフォントサイズの上下限（settings.terminal.font_size）。
    # 数値の実体は TerminalWidget 側にあり、ここでは参照するだけにして
    # 二重定義を避ける。
    FONT_SIZE_MIN = TerminalWidget.FONT_SIZE_MIN
    FONT_SIZE_MAX = TerminalWidget.FONT_SIZE_MAX
    
    def __init__(self):
        super().__init__()
        
        # 更新通知シグナルを接続
        self.update_available.connect(self._show_update_dialog)
        self.update_check_error.connect(self._show_update_check_error)
        self.no_update_available.connect(self._show_no_update_message)
        self.run_auto_commands_requested.connect(self._run_auto_commands)
        
        # ConfigManager初期化
        self.config_manager = ConfigManager()
        
        # 設定ファイル読み込みエラーをチェック
        if self.config_manager.load_error:
            self._show_config_load_error()
        
        # MacroManager初期化
        self.macro_manager = MacroManager()
        
        # 接続管理用辞書（SSH/Telnet/シリアル）
        self.connections: Dict[str, Union[SSHConnection, TelnetConnection, SerialConnection]] = {}  # 機器名 -> 接続
        self.device_info: Dict[str, dict] = {}  # 機器名 -> 機器データ（再接続用）
        self.keepalive_intervals: Dict[str, int] = {}  # 機器名 -> キープアライブ間隔（秒）
        self.macro_signal_connected: Dict[str, bool] = {}  # マクロシグナル接続済みフラグ
        
        # SFTP管理用辞書
        self.sftp_managers: Dict[str, SFTPManager] = {}  # 機器名 -> SFTPManager
        
        # Syslogレシーバー初期化
        self.syslog_receiver = SyslogReceiver(self)
        
        # SNMPマネージャー初期化
        self.snmp_manager = SNMPManager(self)
        
        self.setWindowTitle("NetBelt")
        self.setGeometry(100, 100, 1200, 700)
        
        # ツールバー作成
        self._create_toolbar()
        
        # メニューバー作成
        self._create_menu_bar()
        
        # メインウィジェット作成
        self._create_main_widget()
        
        # ステータスバー作成
        self._create_status_bar()
        
        # 設定から接続先リストを読み込み
        self._load_devices()
        
        # ターミナルの外観設定（config の settings.terminal）を反映
        self._apply_terminal_settings_from_config()

        # 起動時の更新チェック（非同期）
        self._check_for_updates_on_startup()

        # 保存済みレイアウト（スプリッター幅・選択タブ）を復元
        self._restore_layout()
    
    def _show_config_load_error(self):
        """設定ファイル読み込みエラーダイアログを表示"""
        error_msg = f"""設定ファイル (config.json) の読み込みに失敗しました。

エラー内容:
{self.config_manager.load_error}

対処方法:
"""
        if self.config_manager.backup_path:
            error_msg += f"""
• 破損した設定ファイルはバックアップされました:
  {self.config_manager.backup_path}

• 現在はデフォルト設定で起動しています
• バックアップファイルから手動で設定を復元できます
• 新しい設定を保存すると、config.jsonが再作成されます
"""
        else:
            error_msg += """
• 現在はデフォルト設定で起動しています
• 新しい設定を保存すると、config.jsonが再作成されます
"""
        
        QMessageBox.warning(
            self,
            "設定ファイル読み込みエラー",
            error_msg
        )
    
    def _create_toolbar(self):
        """ツールバー作成"""
        toolbar = QToolBar("メインツールバー")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        
        # グループ追加アクション
        add_group_action = toolbar.addAction("グループ追加")
        add_group_action.triggered.connect(self._on_add_group)
        add_group_action.setToolTip("新しいグループを追加")
    
    def _create_menu_bar(self):
        """メニューバー作成"""
        menubar = self.menuBar()
        
        # ファイルメニュー
        file_menu = menubar.addMenu("ファイル(&F)")
        file_menu.addAction("新規接続先(&N)", self._on_add_device)  # 修正
        file_menu.addSeparator()
        file_menu.addAction("終了(&X)", self.close)
        
        # 編集メニュー
        # 端末ソフトの慣習に合わせて Ctrl+Shift+C / Ctrl+Shift+V を使う。
        # Ctrl+C はターミナルから機器へ 0x03（中断）として送られるので奪わない。
        edit_menu = menubar.addMenu("編集(&E)")
        self.copy_action = edit_menu.addAction("コピー(&C)")
        self.copy_action.setShortcut("Ctrl+Shift+C")
        self.copy_action.triggered.connect(self._on_copy)

        self.paste_action = edit_menu.addAction("ペースト(&P)")
        self.paste_action.setShortcut("Ctrl+Shift+V")
        self.paste_action.triggered.connect(self._on_paste)
        
        # 表示メニュー
        view_menu = menubar.addMenu("表示(&V)")
        self.sftp_panel_action = view_menu.addAction("SFTPクライアント(&C)")
        self.sftp_panel_action.triggered.connect(self._toggle_sftp_panel)
        
        self.sftp_server_panel_action = view_menu.addAction("SFTPサーバー(&S)")
        self.sftp_server_panel_action.triggered.connect(self._toggle_sftp_server_panel)
        
        self.tftp_server_panel_action = view_menu.addAction("TFTPサーバー(&T)")
        self.tftp_server_panel_action.triggered.connect(self._toggle_tftp_server_panel)
        
        self.ftp_server_panel_action = view_menu.addAction("FTPサーバー(&F)")
        self.ftp_server_panel_action.triggered.connect(self._toggle_ftp_server_panel)
        
        self.syslog_panel_action = view_menu.addAction("Syslogパネル(&L)")
        self.syslog_panel_action.setCheckable(True)
        self.syslog_panel_action.setChecked(False)
        self.syslog_panel_action.triggered.connect(self._toggle_syslog_panel)
        
        self.snmp_panel_action = view_menu.addAction("SNMPパネル(&N)")
        self.snmp_panel_action.setCheckable(True)
        self.snmp_panel_action.setChecked(False)
        self.snmp_panel_action.triggered.connect(self._toggle_snmp_panel)

        view_menu.addSeparator()
        self.toggle_device_list_action = view_menu.addAction("接続先リスト表示/非表示")
        self.toggle_device_list_action.setCheckable(True)
        self.toggle_device_list_action.setChecked(True)
        self.toggle_device_list_action.triggered.connect(
            self._toggle_device_list)
        self.toggle_tool_area_action = view_menu.addAction("ツールエリア表示/非表示")
        self.toggle_tool_area_action.setCheckable(True)
        self.toggle_tool_area_action.setChecked(True)
        self.toggle_tool_area_action.triggered.connect(self._toggle_tool_area)
        
        view_menu.addSeparator()
        self.font_increase_action = view_menu.addAction("フォントサイズ拡大")
        self.font_increase_action.triggered.connect(self._on_font_size_increase)
        self.font_decrease_action = view_menu.addAction("フォントサイズ縮小")
        self.font_decrease_action.triggered.connect(self._on_font_size_decrease)
        
        # ツールメニュー
        tools_menu = menubar.addMenu("ツール(&T)")
        tools_menu.addAction("マクロ設定(&M)", self._on_macro_settings)
        tools_menu.addAction("ポートチェッカー(&P)", self._on_port_checker)
        tools_menu.addSeparator()
        self.settings_action = tools_menu.addAction("設定")
        self.settings_action.triggered.connect(self._on_settings)
        
        # ログメニュー
        log_menu = menubar.addMenu("ログ(&L)")
        log_menu.addAction("ログ保存(&S)", self._on_save_log)
        log_menu.addAction("ログ記録開始(&R)", self._on_start_log_recording)
        log_menu.addAction("ログ記録停止(&T)", self._on_stop_log_recording)
        
        # ヘルプメニュー
        help_menu = menubar.addMenu("ヘルプ(&H)")
        help_menu.addAction("バージョン情報(&A)", self._on_version_info)
        help_menu.addAction("更新を確認(&U)", self._on_check_for_updates)
    
    def _create_main_widget(self):
        """メインウィジェット作成（左右分割）"""
        # 中央ウィジェット
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # レイアウト
        layout = QHBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # スプリッター（左右分割）
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # 左側: 接続先リスト
        self.device_tree = DeviceTree()
        self.device_tree.btn_add.clicked.connect(self._on_add_device)
        self.device_tree.btn_connect.clicked.connect(self._on_connect_button_clicked)
        # シグナル接続
        self.device_tree.device_connect.connect(self._on_device_connect)
        self.device_tree.device_edit.connect(self._on_device_edit)
        self.device_tree.device_delete.connect(self._on_device_delete)
        self.device_tree.device_duplicate.connect(self._on_device_duplicate)
        self.device_tree.connect_requested.connect(self._on_connect_requested)
        self.device_tree.device_moved.connect(self._on_device_moved)
        self.device_tree.group_add_requested.connect(self._on_add_group)
        self.device_tree.group_edit_requested.connect(self._on_edit_group)
        self.device_tree.group_delete_requested.connect(self._on_delete_group)
        self.device_tree.hide_requested.connect(self._toggle_device_list)
        splitter.addWidget(self.device_tree)
        
        # 右側: ターミナル
        self.terminal_widget = TerminalWidget()
        self.terminal_widget.tab_closed.connect(self._on_tab_closed)
        self.terminal_widget.current_tab_changed.connect(
            self._on_terminal_tab_changed)
        self.terminal_widget.font_size_change_requested.connect(
            self._on_font_size_wheel)
        self.terminal_widget.macro_execute_requested.connect(self._on_macro_execute_requested)
        self.terminal_widget.macro_settings_requested.connect(self._on_macro_settings_from_context)
        self.terminal_widget.keepalive_start_requested.connect(self._on_keepalive_start_requested)
        self.terminal_widget.keepalive_stop_requested.connect(self._on_keepalive_stop_requested)
        self.terminal_widget.terminal_resized.connect(self._on_terminal_resized)
        splitter.addWidget(self.terminal_widget)
        
        # デフォルトの分割比率を設定（30% : 70%）
        self.main_splitter = splitter
        self.tool_tabs = QTabWidget()
        self.tool_tabs.setTabPosition(QTabWidget.TabPosition.North)
        self.tool_tabs.setTabBar(DetachableTabBar(self._detach_tool_by_index, self.tool_tabs))
        self.tool_tabs.setMovable(True)   # 横ドラッグでタブの並べ替え（縦に引くとデタッチ）
        self.tool_tabs.tabBar().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tool_tabs.tabBar().customContextMenuRequested.connect(self._tool_area_context_menu)
        splitter.addWidget(self.tool_tabs)
        splitter.setSizes([250, 650, 300])
        # 接続先リストとツールエリアはハンドルを引いて畳める。
        # ターミナルは本体なので畳ませない
        splitter.setCollapsible(0, True)
        splitter.setCollapsible(1, False)
        splitter.setCollapsible(2, True)
        
        layout.addWidget(splitter)
        
        # 各ツールをスクロール内包でタブに収める（QDockWidget は廃止）
        self.sftp_panel = SFTPPanel(config_manager=self.config_manager)
        self.sftp_server_panel = SFTPServerPanel()
        self.tftp_server_panel = TFTPServerPanel(config_manager=self.config_manager)
        self.ftp_server_panel = FTPServerPanel(config_manager=self.config_manager)
        self.syslog_panel = SyslogPanel(config_manager=self.config_manager)
        self.syslog_receiver.message_received.connect(self.syslog_panel.add_message)
        self.syslog_panel.set_syslog_receiver(self.syslog_receiver)
        self.snmp_panel = SNMPPanel(config_manager=self.config_manager)
        self.snmp_panel.set_snmp_manager(self.snmp_manager)

        # 既定のタブ並び（ユーザーは横ドラッグで自由に並べ替えられる）
        self._tool_panels = [
            ("syslog", "Syslog", self.syslog_panel),
            ("snmp", "SNMP", self.snmp_panel),
            ("ftp_server", "FTP", self.ftp_server_panel),
            ("tftp_server", "TFTP", self.tftp_server_panel),
            ("sftp_server", "SFTPサーバー", self.sftp_server_panel),
            ("sftp", "SFTPクライアント", self.sftp_panel),
        ]
        # キー -> スクロール widget。タブ位置は都度 indexOf で解決する（並べ替えに追従）
        self._tool_scrolls = {}
        for _key, _title, _panel in self._tool_panels:
            _scroll = QScrollArea()
            _scroll.setWidgetResizable(True)
            _scroll.setWidget(_panel)
            self.tool_tabs.addTab(_scroll, _title)
            self._tool_scrolls[_key] = _scroll
        # 保存済みのタブ並び順を復元する
        self._restore_tab_order()
    
    def _create_status_bar(self):
        """ステータスバー作成"""
        from PyQt6.QtWidgets import QLabel

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        # 端末の大きさ。右端の常設欄に置くので、showMessage の
        # 一時メッセージとは場所を取り合わない
        self.terminal_size_label = QLabel("")
        self.terminal_size_label.setToolTip(
            "いま機器へ伝えている端末の大きさ（桁×行）")
        self.status_bar.addPermanentWidget(self.terminal_size_label)
        self.status_bar.showMessage("準備完了")

    def _show_terminal_size(self, device_name: str) -> None:
        """表示中のターミナルの大きさを、ステータスバーへ出す。"""
        if not device_name:
            self.terminal_size_label.setText("")
            return
        cols, rows = self.terminal_widget.grid_size_for(device_name)
        self.terminal_size_label.setText("%d x %d" % (cols, rows))
    
    def _load_devices(self):
        """設定ファイルから接続先リストを読み込み"""
        groups = self.config_manager.get_groups()
        self.device_tree.load_from_config(groups)
        self.status_bar.showMessage(f"接続先リスト読み込み完了（{len(groups)}グループ）")
    
    def _on_add_device(self):  # 追加
        """機器追加ダイアログを表示"""
        # グループ名リストを取得
        group_names = [g["name"] for g in self.config_manager.get_groups()]
        
        if not group_names:
            QMessageBox.warning(
                self,
                "グループなし",
                "機器を追加するには、まずグループを作成してください。"
            )
            return
        
        # ダイアログ表示
        dialog = DeviceDialog(self, groups=group_names)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # 機器データ取得
            device_data = dialog.get_device_data()
            group_name = dialog.get_selected_group()
            
            # 設定に追加
            if self.config_manager.add_device(group_name, device_data):
                # ツリーを再読み込み
                self._load_devices()
                self.status_bar.showMessage(f"機器 '{device_data['name']}' を追加しました")
            else:
                QMessageBox.warning(self, "エラー", "機器の追加に失敗しました。")
    
    def _on_device_connect(self, group_name: str, device_data: dict):
        """
        機器接続（右クリックメニューから）
        
        Args:
            group_name: グループ名
            device_data: 機器データ
        """
        # 実際の接続処理を実行（ダブルクリックと同じ）
        self._on_connect_requested(device_data)
    
    def _on_device_edit(self, group_name: str, device_data: dict):
        """
        機器編集
        
        Args:
            group_name: グループ名
            device_data: 機器データ
        """
        # グループ名リストを取得
        group_names = [g["name"] for g in self.config_manager.get_groups()]
        
        # 編集ダイアログを表示
        dialog = DeviceDialog(self, groups=group_names, device_data=device_data)
        
        # デフォルトでグループを選択
        index = dialog.group_combo.findText(group_name)
        if index >= 0:
            dialog.group_combo.setCurrentIndex(index)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # 新しい機器データを取得
            new_device_data = dialog.get_device_data()
            new_group_name = dialog.get_selected_group()
            old_device_name = device_data["name"]
            
            # 古い機器を削除
            if not self.config_manager.remove_device(group_name, old_device_name):
                QMessageBox.warning(self, "エラー", "機器の削除に失敗しました。")
                return
            
            # 新しい機器を追加
            if self.config_manager.add_device(new_group_name, new_device_data):
                # 開いているタブの再接続は device_info の写しを見る。
                # ここを更新しないと編集内容が届かず、古い接続情報のまま
                # 繋がり続ける（存在しない鍵を指定しても、以前の鍵で
                # 繋がってしまう）。名前を変えたときは古い写しを残さない。
                if old_device_name in self.device_info:
                    del self.device_info[old_device_name]
                    self.device_info[new_device_data["name"]] = new_device_data
                # ツリーを再読み込み
                self._load_devices()
                self.status_bar.showMessage(f"機器 '{new_device_data['name']}' を更新しました")
            else:
                # 失敗した場合は古い機器を復元
                self.config_manager.add_device(group_name, device_data)
                QMessageBox.warning(self, "エラー", "機器の更新に失敗しました。")
    
    def _on_device_delete(self, group_name: str, device_name: str):
        """
        機器削除
        
        Args:
            group_name: グループ名
            device_name: 機器名
        """
        # 確認ダイアログ
        reply = QMessageBox.question(
            self,
            "削除確認",
            f"機器 '{device_name}' を削除しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            if self.config_manager.remove_device(group_name, device_name):
                # 消した機器の接続情報を残さない（残すと、開いたままの
                # タブで Enter を押したときに消したはずの機器へ繋がる）
                self.device_info.pop(device_name, None)
                # ツリーを再読み込み
                self._load_devices()
                self.status_bar.showMessage(f"機器 '{device_name}' を削除しました")
            else:
                QMessageBox.warning(self, "エラー", "機器の削除に失敗しました。")
    
    def _on_device_duplicate(self, group_name: str, device_data: dict):
        """
        機器複製
        
        Args:
            group_name: グループ名
            device_data: 機器データ
        """
        # グループ名リストを取得
        group_names = [g["name"] for g in self.config_manager.get_groups()]
        
        # 複製データを作成（名前に「のコピー」を追加）
        duplicate_data = device_data.copy()
        duplicate_data["name"] = f"{device_data['name']}のコピー"
        
        # 追加ダイアログを表示（複製データで初期化）
        dialog = DeviceDialog(self, groups=group_names, device_data=duplicate_data)
        
        # デフォルトでグループを選択
        index = dialog.group_combo.findText(group_name)
        if index >= 0:
            dialog.group_combo.setCurrentIndex(index)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # 機器データを取得
            new_device_data = dialog.get_device_data()
            new_group_name = dialog.get_selected_group()
            
            # 設定に追加
            if self.config_manager.add_device(new_group_name, new_device_data):
                # ツリーを再読み込み
                self._load_devices()
                self.status_bar.showMessage(f"機器 '{new_device_data['name']}' を追加しました")
            else:
                QMessageBox.warning(self, "エラー", "機器の追加に失敗しました。")
    
    def _on_device_moved(self, source_group_name: str, target_group_name: str, device_name: str):
        """
        機器移動（ドラッグアンドドロップ）
        
        Args:
            source_group_name: 移動元グループ名
            target_group_name: 移動先グループ名
            device_name: デバイス名
        """
        # ConfigManagerで移動処理を実行
        if self.config_manager.move_device(source_group_name, target_group_name, device_name):
            # ツリーを再読み込み
            self._load_devices()
            self.status_bar.showMessage(
                f"機器 '{device_name}' を '{source_group_name}' から '{target_group_name}' に移動しました"
            )
        else:
            QMessageBox.warning(self, "エラー", "機器の移動に失敗しました。")
    
    def _on_connect_requested(self, device_data: dict):
        """
        機器へのダブルクリック接続要求を処理
        
        Args:
            device_data: 機器データ
        """
        device_name = device_data['name']
        
        # 既に接続中かチェック
        if device_name in self.connections:
            self.status_bar.showMessage(f"{device_name} は既に接続されています")
            return
        
        # プロトコルで分岐
        protocol = device_data.get('protocol', 'ssh')  # デフォルトはSSH
        
        if protocol == 'serial' or protocol == 'console':
            # シリアル接続
            self._connect_serial(device_data)
        elif protocol == 'telnet':
            # Telnet接続
            self._connect_telnet(device_data)
        else:
            # SSH接続
            self._connect_ssh(device_data)
    
    def _connect_ssh(self, device_data: dict):
        """
        SSH接続を確立
        
        Args:
            device_data: 機器データ
        """
        device_name = device_data['name']
        
        # ステータスバーに接続メッセージを表示
        self.status_bar.showMessage(f"{device_name} に接続します...")
        
        # 新しいターミナルタブを作成
        terminal = self.terminal_widget.create_terminal_tab(device_name)
        
        # 接続情報を取得
        host = device_data.get('host', 'unknown')
        port = device_data.get('port', 22)
        username = device_data.get('username', '')
        password = device_data.get('password', '')
        ssh_key = device_data.get('ssh_key', '')
        
        # SSH接続を作成
        ssh = SSHConnection(host, port, username, password, ssh_key, self)
        # pty 要求 (RFC 4254 6.2) に、いまの表示領域の行数・桁数を使う
        cols, rows = self.terminal_widget.grid_size_for(device_name)
        ssh.set_terminal_size(cols, rows)
        
        # シグナル接続
        ssh.output_received.connect(lambda text: self.terminal_widget.append_output(device_name, text))
        ssh.connected.connect(lambda: self._on_connection_success(device_name, terminal))
        ssh.disconnected.connect(lambda: self._on_connection_closed(device_name))
        ssh.error_occurred.connect(lambda error: self._on_connection_error(device_name, error))
        
        # ターミナルのキー入力をSSHに送信（再接続時の蓄積を防ぐため既存接続を切断）
        try:
            terminal.key_pressed.disconnect()
        except TypeError:
            pass
        terminal.key_pressed.connect(ssh.send_command)
        
        # 接続と機器情報を保存
        self.connections[device_name] = ssh
        self.device_info[device_name] = device_data  # 再接続用
        
        # マクロマネージャーにコールバックを登録
        self.macro_manager.register_send_callback(device_name, ssh.send_command)
        
        # マクロマネージャーのシグナルをターミナルに接続（初回のみ）
        if device_name not in self.macro_signal_connected:
            self.macro_manager.command_output.connect(
                lambda dev, text, dn=device_name: self.terminal_widget.append_output(dev, text) if dev == dn else None
            )
            self.macro_signal_connected[device_name] = True
        
        # マクロリストをターミナルに設定
        macros = self.config_manager.get_global_macros()
        self.terminal_widget.set_macro_list(device_name, macros)
        
        # バックグラウンドで接続を開始
        import threading
        def connect_thread():
            try:
                success = ssh.connect()
                if not success:
                    # GUIスレッド外からウィジェットを直接触らない。
                    # 既に append_output へ接続済みのシグナルへ流す。
                    ssh.output_received.emit("\r\n接続失敗\r\n")
            except Exception as e:
                ssh.error_occurred.emit(f"接続スレッドエラー: {str(e)}")
        
        threading.Thread(target=connect_thread, daemon=True).start()
    
    def _connect_serial(self, device_data: dict):
        """
        シリアル接続を確立
        
        Args:
            device_data: 機器データ
        """
        device_name = device_data['name']
        
        # ステータスバーに接続メッセージを表示
        self.status_bar.showMessage(f"{device_name} に接続します...")
        
        # 新しいターミナルタブを作成
        terminal = self.terminal_widget.create_terminal_tab(device_name)
        
        # 接続情報を取得
        # コンソール接続ではhostフィールドにCOMポート名が入っている
        if device_data.get('protocol') == 'console':
            port = device_data.get('host', '')
        else:
            port = str(device_data.get('port', ''))
        baudrate = device_data.get('baudrate', 9600)

        # シリアル接続を作成
        serial_conn = SerialConnection(port, baudrate, self)
        
        # シグナル接続
        serial_conn.output_received.connect(lambda text: self.terminal_widget.append_output(device_name, text))
        serial_conn.connected.connect(lambda: self._on_connection_success(device_name, terminal))
        serial_conn.disconnected.connect(lambda: self._on_connection_closed(device_name))
        serial_conn.error_occurred.connect(lambda error: self._on_connection_error(device_name, error))
        
        # ターミナルのキー入力をシリアルに送信（再接続時の蓄積を防ぐため既存接続を切断）
        try:
            terminal.key_pressed.disconnect()
        except TypeError:
            pass
        terminal.key_pressed.connect(serial_conn.send_command)
        
        # 接続と機器情報を保存
        self.connections[device_name] = serial_conn
        self.device_info[device_name] = device_data  # 再接続用
        
        # マクロマネージャーにコールバックを登録
        self.macro_manager.register_send_callback(device_name, serial_conn.send_command)
        
        # マクロマネージャーのシグナルをターミナルに接続（初回のみ）
        if device_name not in self.macro_signal_connected:
            self.macro_manager.command_output.connect(
                lambda dev, text, dn=device_name: self.terminal_widget.append_output(dev, text) if dev == dn else None
            )
            self.macro_signal_connected[device_name] = True
        
        # マクロリストをターミナルに設定
        macros = self.config_manager.get_global_macros()
        self.terminal_widget.set_macro_list(device_name, macros)
        
        # バックグラウンドで接続を開始
        import threading
        def connect_thread():
            try:
                success = serial_conn.connect()
                if not success:
                    # GUIスレッド外からウィジェットを直接触らない。
                    # 既に append_output へ接続済みのシグナルへ流す。
                    serial_conn.output_received.emit("\r\n接続失敗\r\n")
            except Exception as e:
                serial_conn.error_occurred.emit(f"接続スレッドエラー: {str(e)}")
        
        threading.Thread(target=connect_thread, daemon=True).start()
    
    def _connect_telnet(self, device_data: dict):
        """
        Telnet接続を確立
        
        Args:
            device_data: 機器データ
        """
        device_name = device_data['name']
        
        # ステータスバーに接続メッセージを表示
        self.status_bar.showMessage(f"{device_name} に接続します...")
        
        # 新しいターミナルタブを作成
        terminal = self.terminal_widget.create_terminal_tab(device_name)
        
        # 接続情報を取得
        host = device_data.get('host', 'unknown')
        port = device_data.get('port', 23)
        username = device_data.get('username', '')
        password = device_data.get('password', '')
        
        # Telnet接続を作成
        telnet = TelnetConnection(host, port, username, password, self)
        
        # シグナル接続
        telnet.output_received.connect(lambda text: self.terminal_widget.append_output(device_name, text))
        telnet.connected.connect(lambda: self._on_connection_success(device_name, terminal))
        telnet.disconnected.connect(lambda: self._on_connection_closed(device_name))
        telnet.error_occurred.connect(lambda error: self._on_connection_error(device_name, error))
        
        # ターミナルのキー入力をTelnetに送信（再接続時の蓄積を防ぐため既存接続を切断）
        try:
            terminal.key_pressed.disconnect()
        except TypeError:
            pass
        terminal.key_pressed.connect(telnet.send_command)
        
        # 接続と機器情報を保存
        self.connections[device_name] = telnet
        self.device_info[device_name] = device_data  # 再接続用
        
        # マクロマネージャーにコールバックを登録
        self.macro_manager.register_send_callback(device_name, telnet.send_command)
        
        # マクロマネージャーのシグナルをターミナルに接続（初回のみ）
        if device_name not in self.macro_signal_connected:
            self.macro_manager.command_output.connect(
                lambda dev, text, dn=device_name: self.terminal_widget.append_output(dev, text) if dev == dn else None
            )
            self.macro_signal_connected[device_name] = True
        
        # マクロリストをターミナルに設定
        macros = self.config_manager.get_global_macros()
        self.terminal_widget.set_macro_list(device_name, macros)
        
        # バックグラウンドで接続を開始
        import threading
        def connect_thread():
            try:
                success = telnet.connect()
                if not success:
                    # GUIスレッド外からウィジェットを直接触らない。
                    # 既に append_output へ接続済みのシグナルへ流す。
                    telnet.output_received.emit("\r\n接続失敗\r\n")
            except Exception as e:
                telnet.error_occurred.emit(f"接続スレッドエラー: {str(e)}")
        
        threading.Thread(target=connect_thread, daemon=True).start()
    
    def _on_connection_success(self, device_name: str, terminal):
        """接続成功時の処理（SSH/Telnet/シリアル共通）"""
        self.status_bar.showMessage(f"{device_name} に接続しました")
        terminal.set_input_enabled(True)  # キー入力を有効化
        # グループの自動実行コマンドをGUIスレッドで送信する
        self.run_auto_commands_requested.emit(device_name)
        
        # SSH接続の場合はSFTPセッションも確立
        if device_name in self.connections:
            conn = self.connections[device_name]
            if isinstance(conn, SSHConnection) and conn.client:
                try:
                    # SFTPマネージャーを作成して接続
                    sftp_manager = SFTPManager(self)
                    
                    # エラーシグナルを接続してデバッグ
                    sftp_manager.error_occurred.connect(
                        lambda err: self._on_sftp_error(device_name, err)
                    )
                    
                    if sftp_manager.connect(conn.client):
                        self.sftp_managers[device_name] = sftp_manager
                        # 現在アクティブなタブの場合はSFTPパネルに表示
                        if self.terminal_widget.get_current_tab_name() == device_name:
                            self.sftp_panel.set_sftp_manager(
                                sftp_manager, device_name,
                                self._describe_target(device_name))
                            # SFTPパネルを表示
                            self._select_tool_tab("sftp")
                        self.status_bar.showMessage(f"{device_name} に接続しました（SFTP有効）")
                    else:
                        # SFTP接続失敗 - エラーダイアログは表示せず、ログのみ
                        print(f"[INFO] SFTP接続失敗: {device_name} - 機器がSFTPをサポートしていない可能性があります")
                        # ステータスバーは通常の接続メッセージのまま（ユーザーを混乱させない）
                except Exception as e:
                    # SFTP接続エラー - エラーダイアログは表示せず、ログのみ
                    import traceback
                    print(f"[ERROR] SFTP接続エラー: {device_name}")
                    print(f"  エラー: {str(e)}")
                    print(f"  詳細:\n{traceback.format_exc()}")
                    # ステータスバーは通常の接続メッセージのまま
    
    def _describe_target(self, device_name: str) -> str:
        """機器の接続先（host:port）を返す。分からなければ空文字。

        SFTP パネルに出して、送り先の取り違えに気づけるようにする。
        """
        conn = self.connections.get(device_name)
        host = getattr(conn, "host", "")
        if not host:
            return ""
        port = getattr(conn, "port", 22)
        return host if port == 22 else "%s:%s" % (host, port)

    def _on_terminal_tab_changed(self, device_name: str) -> None:
        """表示中のターミナルに合わせて SFTP パネルを切り替える

        追従しないと、別の機器のターミナルを見ながら、その1つ前に
        繋いだ機器へファイルを送ることになる。パネルに機器名が出ない
        ため、送り先が違うことに気づけない。
        """
        manager = self.sftp_managers.get(device_name)
        if manager is not None:
            if self.sftp_panel.current_device != device_name:
                self.sftp_panel.set_sftp_manager(
                    manager, device_name, self._describe_target(device_name))
        elif self.sftp_panel.current_device:
            # いま見ている機器に SFTP が無いなら、前の機器のものを残さない
            self.sftp_panel.clear()

        self._show_terminal_size(device_name)

    def _find_group_of_device(self, device_name: str):
        """機器名から所属グループを返す(見つからなければNone)"""
        for group in self.config_manager.get_groups():
            for device in group.get("devices", []):
                if device.get("name") == device_name:
                    return group
        return None

    def _run_auto_commands(self, device_name: str):
        """接続先グループの自動実行コマンドを送信する(GUIスレッドで実行)"""
        if device_name not in self.connections:
            return
        group = self._find_group_of_device(device_name)
        if not group:
            return
        commands = group.get("auto_commands", [])
        if not commands:
            return
        from PyQt6.QtCore import QTimer
        # シェルのプロンプトが出るまで少し待ってから送信する
        QTimer.singleShot(
            800,
            lambda: self.macro_manager.start_command_list(device_name, list(commands), 1000)
        )
        self.status_bar.showMessage(f"{device_name}: 自動実行コマンドを送信します...")

    def _dispose_connection(self, device_name: str):
        """接続を閉じてから辞書から外す

        辞書から del するだけだと、シリアルは COM ポートを掴んだまま、
        SSH は SSHClient と Transport スレッドを抱えたまま残る。各接続は
        parent=self で作られていて Qt からも参照され続けるため、参照を
        捨てても解放されない。Windows の COM は同一プロセス内でも排他な
        ので、掴まれたままだと再接続が Access is denied で通らず、アプリを
        再起動するまで復旧できない。

        disconnect() ではなく dispose() を呼ぶ。disconnect() は末尾で
        disconnected を出すので、切断処理の中から呼ぶと再入する。
        """
        conn = self.connections.pop(device_name, None)
        if conn is None:
            return
        try:
            conn.dispose()
        except Exception as e:
            print(f"[Connection] {device_name} の後始末に失敗: {e}")

    def _on_connection_closed(self, device_name: str):
        """接続切断時の処理（SSH/シリアル共通）"""
        self.status_bar.showMessage(f"{device_name} から切断されました")
        
        # SFTP接続を切断
        if device_name in self.sftp_managers:
            try:
                self.sftp_managers[device_name].disconnect()
            except Exception:
                pass
            del self.sftp_managers[device_name]
            
            # 現在表示中のSFTPパネルをクリア
            if self.sftp_panel.current_device == device_name:
                self.sftp_panel.clear()
        
        # 接続を閉じてから削除（閉じないとポートを掴んだまま残る）
        self._dispose_connection(device_name)

        # 切断メッセージと再接続方法を表示
        self.terminal_widget.show_notice(
            device_name, 
            "\n\n========================================\n"
            "セッションが切断されました\n"
            "========================================\n"
            "Enterキーを押すと再接続します\n\n"
        )
        
        # 再接続可能な状態にする
        self.terminal_widget.enable_reconnect(device_name, self._reconnect_device)
    
    def _on_connection_error(self, device_name: str, error: str):
        """接続エラー時の処理（SSH/シリアル共通）"""
        self.status_bar.showMessage(f"{device_name}: エラー - {error}")
        
        # エラーメッセージを表示
        if "送信エラー" in error or "Socket is closed" in error:
            # 送信エラーの場合は切断として扱う
            self._on_connection_closed(device_name)
        else:
            # その他のエラー
            self.terminal_widget.show_notice(device_name, f"\nエラー: {error}\n")
            self._dispose_connection(device_name)
    
    def _reconnect_device(self, device_name: str):
        """
        機器に再接続
        
        Args:
            device_name: 機器名
        """
        # 機器情報を取得
        if device_name not in self.device_info:
            self.terminal_widget.show_notice(device_name, "\n再接続情報が見つかりません\n")
            return
        
        # 再接続メッセージ
        self.terminal_widget.show_notice(device_name, "再接続中...\n\n")
        
        # 接続処理を実行
        device_data = self.device_info[device_name]
        self._on_connect_requested(device_data)
    
    def _on_connect_button_clicked(self):
        """
        「接続」ボタンがクリックされたときの処理
        """
        # 選択中の機器を取得
        result = self.device_tree.get_selected_device()
        
        if result is None:
            # 何も選択されていない場合は何もしない
            return
        
        group_name, device_data = result
        
        # 接続処理を実行（ダブルクリックと同じ処理）
        self._on_connect_requested(device_data)
    
    def _on_terminal_resized(self, device_name: str, cols: int, rows: int):
        """端末の行数・桁数の変化を機器へ伝える (RFC 4254 6.7)。

        SSH だけが対応している。Telnet (RFC 1073) と シリアルは未対応
        なので、伝えられない接続では黙って何もしない。
        """
        conn = self.connections.get(device_name)
        if conn is not None and hasattr(conn, "set_terminal_size"):
            conn.set_terminal_size(cols, rows)
        if device_name == self.terminal_widget.get_current_tab_name():
            self.terminal_size_label.setText("%d x %d" % (cols, rows))

    def _on_tab_closed(self, device_name: str):
        """
        タブが閉じられたときの処理
        
        Args:
            device_name: 機器名
        """
        # マクロマネージャーのクリーンアップ
        self.macro_manager.cleanup_device(device_name)
        
        # SFTP接続を切断
        if device_name in self.sftp_managers:
            try:
                self.sftp_managers[device_name].disconnect()
            except Exception:
                pass
            del self.sftp_managers[device_name]
            
            # 現在表示中のSFTPパネルをクリア
            if self.sftp_panel.current_device == device_name:
                self.sftp_panel.clear()
        
        # SSH接続を切断（接続が存在する場合のみ）
        if device_name in self.connections:
            try:
                self.connections[device_name].disconnect()
            except Exception as e:
                print(f"切断エラー: {e}")
            
            # 接続辞書から削除（もう一度存在確認）
            if device_name in self.connections:
                del self.connections[device_name]
            
            self.status_bar.showMessage(f"{device_name} の接続を切断しました")
    
    def _on_add_group(self):
        """グループ追加ダイアログを表示"""
        # 既存のグループ名リストを取得
        existing_groups = [g["name"] for g in self.config_manager.get_groups()]

        # ダイアログ表示
        dialog = GroupDialog(self, existing_groups=existing_groups)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            # グループ名と自動実行コマンドを取得
            group_name = dialog.get_group_name()
            auto_commands = dialog.get_auto_commands()

            # 設定に追加
            if self.config_manager.add_group(group_name, auto_commands):
                # ツリーを再読み込み
                self._load_devices()
                self.status_bar.showMessage(f"グループ '{group_name}' を追加しました")
            else:
                QMessageBox.warning(self, "エラー", "グループの追加に失敗しました。")

    def _warn_change_failed(self, what: str, applied_in_memory: bool):
        """
        設定の変更に失敗したことを知らせる

        ConfigManager の各 mutator は save_config() の前に in-memory の設定を
        書き換えるため、戻り値の False だけでは「保存だけ失敗した（実行中の
        状態は変更済み）」と「そもそも変更が適用されなかった」を区別できない。
        呼び出し側が事後状態を見て判定し、ここへ渡す。

        どちらの場合もツリーを実行中の状態へ合わせ直してから知らせる。

        Args:
            what: 失敗した操作の名前（例: "グループ名の変更"）
            applied_in_memory: 実行中の設定には変更が適用されているか
        """
        self._load_devices()
        if applied_in_memory:
            QMessageBox.warning(
                self, "エラー",
                f"{what}を設定ファイルへ保存できませんでした。\n"
                "変更はこのセッション中のみ有効で、アプリを終了すると失われます。")
        else:
            QMessageBox.warning(self, "エラー", f"{what}に失敗しました。")

    def _on_edit_group(self, group_name: str):
        """
        グループ編集ダイアログを表示

        Args:
            group_name: 編集対象のグループ名
        """
        # 既存のグループ名リストと、現在の自動実行コマンドを取得
        existing_groups = [g["name"] for g in self.config_manager.get_groups()]
        group = self.config_manager.get_group(group_name)
        auto_commands = list(group.get("auto_commands", [])) if group else []

        # ダイアログ表示
        dialog = GroupDialog(self, group_name=group_name,
                             existing_groups=existing_groups,
                             auto_commands=auto_commands)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        new_group_name = dialog.get_group_name()
        new_auto_commands = dialog.get_auto_commands()

        # 改名は名前が変わったときだけ行う。同じ名前で rename_group を呼ぶと
        # 「既に存在します」と判定されて False が返り、誤った警告が出るため。
        if new_group_name != group_name:
            if not self.config_manager.rename_group(group_name, new_group_name):
                # rename_group は「保存失敗」のほか「対象が無い」「新名が重複」でも
                # False を返す。実行中の設定に改名が反映されているかで見分ける。
                renamed = (self.config_manager.get_group(group_name) is None
                           and self.config_manager.get_group(new_group_name) is not None)
                self._warn_change_failed("グループ名の変更", renamed)
                return

        # 自動実行コマンドは改名後の名前で保存する
        if not self.config_manager.set_group_auto_commands(new_group_name, new_auto_commands):
            # こちらも「対象が無い」場合と「保存失敗」の両方で False になる。
            group_now = self.config_manager.get_group(new_group_name)
            # 元から同じ値なら、保存に失敗しても失われる変更は無い。
            # 「セッション中のみ有効」と案内しないよう、値が実際に変わったかも見る。
            applied = (auto_commands != new_auto_commands
                       and group_now is not None
                       and group_now.get("auto_commands") == new_auto_commands)
            self._warn_change_failed("自動実行コマンドの保存", applied)
            return

        self._load_devices()
        if new_group_name != group_name:
            self.status_bar.showMessage(
                f"グループ '{group_name}' を '{new_group_name}' に変更しました")
        else:
            self.status_bar.showMessage(f"グループ '{group_name}' を更新しました")
    
    def _on_delete_group(self, group_name: str):
        """
        グループ削除
        
        Args:
            group_name: 削除対象のグループ名
        """
        # グループ内の機器数を確認
        group = self.config_manager.get_group(group_name)
        if group and len(group.get("devices", [])) > 0:
            QMessageBox.warning(
                self,
                "削除エラー",
                f"グループ '{group_name}' には機器が含まれています。\n"
                "先に機器を削除するか、他のグループに移動してください。"
            )
            return
        
        # 確認ダイアログ
        reply = QMessageBox.question(
            self,
            "削除確認",
            f"グループ '{group_name}' を削除しますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            if self.config_manager.remove_group(group_name):
                # ツリーを再読み込み
                self._load_devices()
                self.status_bar.showMessage(f"グループ '{group_name}' を削除しました")
            else:
                QMessageBox.warning(self, "エラー", "グループの削除に失敗しました。")
    
    def _on_save_log(self):
        """ログ保存メニューがクリックされたときの処理"""
        self.terminal_widget.save_current_log()
    
    def _on_start_log_recording(self):
        """ログ記録開始メニューがクリックされたときの処理"""
        self.terminal_widget.start_log_recording()
    
    def _on_stop_log_recording(self):
        """ログ記録停止メニューがクリックされたときの処理"""
        self.terminal_widget.stop_log_recording()
    
    def _on_copy(self):
        """現在のターミナルの選択範囲をクリップボードへコピーする"""
        terminal = self.terminal_widget.get_current_terminal()
        if terminal is None:
            return
        terminal.copy()

    def _on_paste(self):
        """クリップボードの内容を現在のターミナルから機器へ送信する"""
        from .terminal_widget import InteractiveTerminal

        terminal = self.terminal_widget.get_current_terminal()
        # ホームタブは読み取り専用の QTextEdit で送信先を持たない。
        # 接続タブでも再接続待機中は送信できない（can_send_input が見分ける）。
        if not isinstance(terminal, InteractiveTerminal) or not terminal.can_send_input():
            self.status_bar.showMessage("ペーストできるのは接続中のターミナルタブだけです")
            return
        terminal.custom_paste()

    def _apply_terminal_settings_from_config(self):
        """config の settings.terminal をターミナルへ適用する"""
        self.terminal_widget.apply_terminal_settings(
            self.config_manager.get_server_settings("terminal"))

    def _change_font_size(self, delta: int):
        """
        ターミナルのフォントサイズを変更して保存する

        Args:
            delta: 増減量（+1 / -1）
        """
        # config の生値ではなく、いま適用されている正規化済みの値を基準にする。
        # 生値は手編集で壊れていることがあり（"12" / null / true / 1000）、
        # そのまま加算すると TypeError で落ちるか、表示と無関係な値へ飛ぶ。
        current = self.terminal_widget.current_terminal_settings()["font_size"]
        new_size = max(self.FONT_SIZE_MIN, min(self.FONT_SIZE_MAX, current + delta))
        if new_size == current:
            self.status_bar.showMessage(
                f"フォントサイズは {current}pt です（{self.FONT_SIZE_MIN}〜{self.FONT_SIZE_MAX}pt）")
            return

        # settings 全体を置換する update_settings ではなく、浅いマージの
        # set_server_settings を使う（ui_layout など他のセクションを消さないため）
        saved = self.config_manager.set_server_settings("terminal", {"font_size": new_size})
        self._apply_terminal_settings_from_config()
        if saved:
            self.status_bar.showMessage(f"フォントサイズ: {new_size}pt")
        else:
            self.status_bar.showMessage(
                f"フォントサイズ: {new_size}pt（設定ファイルへ保存できませんでした）")

    def _on_font_size_wheel(self, delta: int):
        """Ctrl+ホイールでのフォントサイズ変更

        表示メニューの拡大/縮小と同じ経路を通す。QTextEdit の組込みズームは
        config を通らず上下限も効かないため、こちらへ寄せている。
        """
        self._change_font_size(delta)

    def _on_font_size_increase(self):
        """フォントサイズを1pt大きくする"""
        self._change_font_size(1)

    def _on_font_size_decrease(self):
        """フォントサイズを1pt小さくする"""
        self._change_font_size(-1)

    def _on_settings(self):
        """設定ダイアログを表示する

        config.json の読み込みに失敗していても開く。破損時はロード時に
        バックアップを取ったうえでデフォルト設定で動く仕様で、起動時の
        ダイアログも「新しい設定を保存すると config.json が再作成されます」と
        案内している。ここで塞ぐと復旧手段が無くなる。
        """
        dialog = SettingsDialog(self, config_manager=self.config_manager)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._apply_terminal_settings_from_config()
            self.status_bar.showMessage("設定を保存しました")

    def _on_macro_settings(self):
        """マクロ設定メニューがクリックされたときの処理"""
        # 現在アクティブなタブを取得
        current_tab_name = self.terminal_widget.get_current_tab_name()
        
        if not current_tab_name or current_tab_name == "ホーム":
            QMessageBox.information(
                self,
                "マクロ設定",
                "マクロ設定を行うには、まず機器に接続してください。"
            )
            return
        
        # 現在の状態を取得
        keepalive_active = self.macro_manager.is_keepalive_active(current_tab_name)
        command_list_active = self.macro_manager.is_command_list_active(current_tab_name)
        
        # マクロダイアログを表示
        dialog = MacroDialog(
            self,
            device_name=current_tab_name,
            keepalive_active=keepalive_active,
            command_list_active=command_list_active,
            config_manager=self.config_manager
        )
        
        # シグナル接続
        dialog.keepalive_start_requested.connect(
            lambda interval: self._start_keepalive(current_tab_name, interval)
        )
        dialog.keepalive_stop_requested.connect(
            lambda: self._stop_keepalive(current_tab_name)
        )
        dialog.command_list_start_requested.connect(
            lambda commands, delay: self.macro_manager.start_command_list(
                current_tab_name, commands, delay
            )
        )
        dialog.command_list_stop_requested.connect(
            lambda: self.macro_manager.stop_command_list(current_tab_name)
        )
        
        dialog.exec()
    
    def _start_keepalive(self, device_name: str, interval: int):
        """キープアライブを開始してUI更新"""
        # 間隔を保存
        self.keepalive_intervals[device_name] = interval
        
        self.macro_manager.start_keepalive(device_name, interval)
        self.terminal_widget.set_keepalive_status(device_name, True)
        self.status_bar.showMessage(f"{device_name}: キープアライブ開始（{interval}秒間隔）")
    
    def _stop_keepalive(self, device_name: str):
        """キープアライブを停止してUI更新"""
        self.macro_manager.stop_keepalive(device_name)
        self.terminal_widget.set_keepalive_status(device_name, False)
        self.status_bar.showMessage(f"{device_name}: キープアライブ停止")
    
    def _on_macro_execute_requested(self, device_name: str, macro_name: str):
        """
        右クリックメニューからマクロ実行が要求されたときの処理
        
        Args:
            device_name: 機器名
            macro_name: マクロ名
        """
        # マクロ情報を取得
        macro = self.config_manager.get_macro_by_name(macro_name)
        if not macro:
            QMessageBox.warning(
                self,
                "マクロ実行エラー",
                f"マクロ '{macro_name}' が見つかりません。"
            )
            return
        
        # コマンドリストを取得
        commands = macro.get("commands", [])
        if not commands:
            QMessageBox.warning(
                self,
                "マクロ実行エラー",
                f"マクロ '{macro_name}' にコマンドが設定されていません。"
            )
            return
        
        # マクロを実行
        self.macro_manager.start_command_list(device_name, commands, 1000)
        self.status_bar.showMessage(f"マクロ '{macro_name}' を実行中...")
    
    def _on_macro_settings_from_context(self, device_name: str):
        """
        右クリックメニューからマクロ設定が要求されたときの処理
        
        Args:
            device_name: 機器名
        """
        # 現在の状態を取得
        keepalive_active = self.macro_manager.is_keepalive_active(device_name)
        command_list_active = self.macro_manager.is_command_list_active(device_name)
        
        # マクロダイアログを表示
        dialog = MacroDialog(
            self,
            device_name=device_name,
            keepalive_active=keepalive_active,
            command_list_active=command_list_active,
            config_manager=self.config_manager
        )
        
        # シグナル接続（UI更新も行う）
        dialog.keepalive_start_requested.connect(
            lambda interval: self._start_keepalive(device_name, interval)
        )
        dialog.keepalive_stop_requested.connect(
            lambda: self._stop_keepalive(device_name)
        )
        dialog.command_list_start_requested.connect(
            lambda commands, delay: self.macro_manager.start_command_list(
                device_name, commands, delay
            )
        )
        dialog.command_list_stop_requested.connect(
            lambda: self.macro_manager.stop_command_list(device_name)
        )
        
        dialog.exec()
    
    def _on_keepalive_start_requested(self, device_name: str):
        """
        右クリックメニューからキープアライブ開始が要求されたときの処理
        
        Args:
            device_name: 機器名
        """
        # 保存された間隔を使用（未設定の場合はデフォルト60秒）
        interval = self.keepalive_intervals.get(device_name, 60)
        self._start_keepalive(device_name, interval)
    
    def _on_keepalive_stop_requested(self, device_name: str):
        """
        右クリックメニューからキープアライブ停止が要求されたときの処理
        
        Args:
            device_name: 機器名
        """
        # キープアライブを停止
        self._stop_keepalive(device_name)
    
    @property
    def _tab_index(self):
        """キー -> 現在のタブ位置（並べ替え後も正しい値を返す）"""
        return {k: self.tool_tabs.indexOf(w) for k, w in self._tool_scrolls.items()}

    def _tool_order(self):
        """現在のタブ並び順をキーのリストで返す"""
        pairs = sorted(((self.tool_tabs.indexOf(w), k)
                        for k, w in self._tool_scrolls.items()
                        if self.tool_tabs.indexOf(w) >= 0))
        return [k for _idx, k in pairs]

    def _restore_tab_order(self):
        """保存済みのタブ並び順を適用する（未保存・不整合なら既定のまま）"""
        try:
            ui = self.config_manager.config.get("settings", {}).get("ui_layout", {})
            order = ui.get("tool_order")
            if not isinstance(order, list):
                return
            for target, key in enumerate(k for k in order if k in self._tool_scrolls):
                cur = self.tool_tabs.indexOf(self._tool_scrolls[key])
                if cur >= 0 and cur != target:
                    self.tool_tabs.tabBar().moveTab(cur, target)
        except Exception:
            pass  # 並び順の復元失敗は無視して既定順で続行

    def _select_tool_tab(self, key):
        """指定ツールのタブへ切替え、ツールエリアを表示状態にする。"""
        if self.tool_tabs.isHidden():
            self.tool_tabs.setVisible(True)
        idx = self._tab_index.get(key)
        if idx is not None:
            self.tool_tabs.setCurrentIndex(idx)

    def _restore_layout(self):
        """保存済みのスプリッター幅・選択タブを復元（無ければ既定のまま）。"""
        try:
            ui = self.config_manager.config.get("settings", {}).get("ui_layout", {})
            sizes = ui.get("splitter_sizes")
            if isinstance(sizes, list) and len(sizes) == self.main_splitter.count():
                self.main_splitter.setSizes([int(s) for s in sizes])
            tab = ui.get("tool_tab")
            if isinstance(tab, int) and 0 <= tab < self.tool_tabs.count():
                self.tool_tabs.setCurrentIndex(tab)
        except Exception:
            pass  # レイアウト復元失敗は無視して既定で続行

    def _save_layout(self):
        """スプリッター幅・選択タブを設定に保存する。"""
        try:
            settings = self.config_manager.config.setdefault("settings", {})
            settings["ui_layout"] = {
                "splitter_sizes": list(self.main_splitter.sizes()),
                "tool_tab": self.tool_tabs.currentIndex(),
                "tool_order": self._tool_order(),
            }
            self.config_manager.save_config()
        except Exception:
            pass  # 保存失敗で終了処理を止めない

    def _tool_area_context_menu(self, pos):
        """タブバーの右クリックメニュー: そのタブの別ウィンドウ化 / エリアの非表示。"""
        menu = QMenu(self)
        index = self.tool_tabs.tabBar().tabAt(pos)
        key = self._tool_key_at(index)
        if key is not None:
            title = self.tool_tabs.tabText(index)
            if getattr(self, "_detached", {}).get(key):
                act_win = menu.addAction("「%s」をタブに戻す" % title)
                act_win.triggered.connect(lambda _=False, k=key: self._reattach_tool(k))
            else:
                act_win = menu.addAction("「%s」を別ウィンドウで開く" % title)
                act_win.triggered.connect(lambda _=False, k=key: self._detach_tool(k))
            menu.addSeparator()
        act = menu.addAction("ツールエリアを非表示")
        act.triggered.connect(self._toggle_tool_area)
        menu.exec(self.tool_tabs.tabBar().mapToGlobal(pos))

    def _tool_key_at(self, index):
        """タブ index に対応するツールキーを返す（範囲外は None）"""
        if index is None or index < 0:
            return None
        for key, idx in self._tab_index.items():
            if idx == index:
                return key
        return None

    def _detach_tool_by_index(self, index):
        """タブ index のツールを別ウィンドウへ切り離す（ドラッグから呼ばれる）"""
        key = self._tool_key_at(index)
        if key is None or getattr(self, "_detached", {}).get(key):
            return
        self._detach_tool(key)

    def _toggle_tool_area(self):
        """ツールエリア全体の表示/非表示を切り替える。"""
        show = self.tool_tabs.isHidden()
        self.tool_tabs.setVisible(show)
        if hasattr(self, "toggle_tool_area_action"):
            self.toggle_tool_area_action.setChecked(show)

    # 接続先リストを戻すときの幅。畳んだ状態から出すと 0 のままなので、
    # 何も見えず「戻らない」と受け取られる
    DEVICE_LIST_WIDTH = 250

    def _toggle_device_list(self):
        """接続先リストの表示/非表示を切り替える。

        仕切りを幅 0 まで引いた状態は、見た目は隠れているのに
        ウィジェットとしては表示中。分割位置は次回起動へ持ち越されるので、
        そのまま終了すると「表示メニューを押しても何も起きない」
        （実際には一度隠してから出し直している）ように見える。
        幅が無いものは隠れていると見なす。
        """
        sizes = self.main_splitter.sizes()
        hidden = self.device_tree.isHidden() or (sizes and sizes[0] < 40)
        self.device_tree.setVisible(hidden)
        if hidden and sizes and sizes[0] < 40:
            spare = max(sizes[1] - self.DEVICE_LIST_WIDTH, 100)
            self.main_splitter.setSizes(
                [self.DEVICE_LIST_WIDTH, spare] + sizes[2:])
        if hasattr(self, "toggle_device_list_action"):
            self.toggle_device_list_action.setChecked(hidden)

    def _toggle_sftp_panel(self):
        """SFTPクライアントパネルの表示/非表示を切り替え"""
        self._select_tool_tab("sftp")
    
    def _on_sftp_dock_visibility_changed(self, visible: bool):
        """
        SFTPクライアントドックの表示状態が変更されたときの処理
        
        Args:
            visible: 表示状態
        """
        self.sftp_panel_action.setChecked(visible)
    
    def _toggle_sftp_server_panel(self):
        """SFTPサーバーパネルの表示/非表示を切り替え"""
        self._select_tool_tab("sftp_server")
    
    def _on_sftp_server_dock_visibility_changed(self, visible: bool):
        """
        SFTPサーバードックの表示状態が変更されたときの処理
        
        Args:
            visible: 表示状態
        """
        self.sftp_server_panel_action.setChecked(visible)
    
    def _toggle_tftp_server_panel(self):
        """TFTPサーバーパネルの表示/非表示を切り替え"""
        self._select_tool_tab("tftp_server")
    
    def _on_tftp_server_dock_visibility_changed(self, visible: bool):
        """TFTPサーバードックの表示状態が変更されたときの処理"""
        self.tftp_server_panel_action.setChecked(visible)
    
    def _toggle_ftp_server_panel(self):
        """FTPサーバーパネルの表示/非表示を切り替え"""
        self._select_tool_tab("ftp_server")
    
    def _on_ftp_server_dock_visibility_changed(self, visible: bool):
        """FTPサーバードックの表示状態が変更されたときの処理"""
        self.ftp_server_panel_action.setChecked(visible)
    
    def _toggle_syslog_panel(self):
        """Syslog タブを別ウィンドウにデタッチ/タブに戻す（単一インスタンスを付け替え）。"""
        if getattr(self, "_detached", {}).get("syslog"):
            self._reattach_tool("syslog")
        else:
            self._detach_tool("syslog")

    def _toggle_snmp_panel(self):
        """SNMP タブを別ウィンドウにデタッチ/タブに戻す（単一インスタンスを付け替え）。"""
        if getattr(self, "_detached", {}).get("snmp"):
            self._reattach_tool("snmp")
        else:
            self._detach_tool("snmp")

    def _detach_tool(self, key):
        """タブ内のパネルを外し独立ウィンドウへ移す（同一インスタンスを reparent）。
        タブ側はプレースホルダ表示。ウィンドウを閉じるとタブに戻る。"""
        idx = self._tab_index[key]
        scroll = self.tool_tabs.widget(idx)
        panel = scroll.takeWidget()          # スクロールからパネルを外す（削除しない）
        title = self.tool_tabs.tabText(idx)
        win = DetachedToolWindow(key, self._reattach_tool)
        win.setWindowTitle(title)
        win.resize(800, 600)
        lay = QVBoxLayout(win)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(panel)
        self._detached = getattr(self, "_detached", {})
        self._detached[key] = win
        ph = QLabel("別ウィンドウで表示中")
        ph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setWidget(ph)
        self._set_tool_action_checked(key, True)
        win.show()

    def _reattach_tool(self, key):
        """デタッチしたパネルをタブへ戻す。"""
        win = getattr(self, "_detached", {}).get(key)
        if not win:
            return
        # 先に辞書から外す。closeEvent 経由の再入（close -> closeEvent -> ここ）を防ぐ
        self._detached.pop(key, None)
        idx = self._tab_index[key]
        scroll = self.tool_tabs.widget(idx)
        item = win.layout().itemAt(0)
        panel = item.widget() if item is not None else None
        if panel is not None:
            # ウィンドウ破棄に巻き込まれてパネルまで解放されないよう、親を外してから移す
            win.layout().removeWidget(panel)
            panel.setParent(None)
            scroll.setWidget(panel)          # プレースホルダは setWidget が破棄する
        self._set_tool_action_checked(key, False)
        # 差し替えた closeEvent を戻してから閉じる（再入防止）。破棄は Qt に任せる
        try:
            del win.closeEvent
        except Exception:
            pass
        win.hide()
        win.deleteLater()

    def _set_tool_action_checked(self, key, checked):
        """デタッチ状態をメニューのチェックに同期（該当アクションがある場合のみ）。"""
        act = getattr(self, {"syslog": "syslog_panel_action",
                             "snmp": "snmp_panel_action"}.get(key, ""), None)
        if act is not None and act.isCheckable():
            act.setChecked(checked)

    def _on_version_info(self):
        """バージョン情報ダイアログを表示"""
        try:
            from __version__ import __version__, APP_NAME, GITHUB_REPO
            version = __version__
            app_name = APP_NAME
            repo = GITHUB_REPO
        except ImportError:
            version = "不明"
            app_name = "NetBelt"
            repo = "gomanizm/NetBelt"
        
        # 決め打ちの色は暗い配色で沈む。地に追従させる。
        dim_colour = theme.dim(theme.surface(self)).name()
        info_text = f"""<h2>{app_name}</h2>
<p><b>バージョン:</b> {version}</p>
<p><b>リポジトリ:</b> <a href="https://github.com/{repo}">github.com/{repo}</a></p>
<br>
<p style="font-size: 10pt; color: {dim_colour};">
Copyright (C) 2026 NetBelt Contributors<br>
<br>
This program comes with ABSOLUTELY NO WARRANTY.<br>
This is free software, and you are welcome to redistribute it
under the terms of the GNU General Public License version 3 or later.<br>
See <a href="https://www.gnu.org/licenses/gpl-3.0.html">https://www.gnu.org/licenses/gpl-3.0.html</a>
for details.
</p>
"""
        
        msg = QMessageBox(self)
        msg.setWindowTitle("バージョン情報")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(info_text)
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()
    
    def _on_check_for_updates(self):
        """手動で更新をチェック"""
        self.status_bar.showMessage("更新を確認中...")
        
        # 非同期でチェック
        import threading
        def check_thread():
            try:
                github_token = self.config_manager.get_github_token()
                version_mgr = VersionManager(github_token=github_token)
                update_info = version_mgr.check_for_updates()
                
                if update_info and update_info.get('error'):
                    # 確認そのものが失敗したときに「最新です」と言わない。
                    # 更新経路が壊れていても利用者が気づけなくなるため。
                    self.update_check_error.emit(update_info['error'])
                elif update_info and update_info.get('available'):
                    # メインスレッドでダイアログを表示
                    self.update_available.emit(update_info)
                else:
                    # 最新版を使用中
                    self.no_update_available.emit()
            except Exception as e:
                print(f"[Update] 更新チェックエラー: {e}")
                self.update_check_error.emit(str(e))
        
        threading.Thread(target=check_thread, daemon=True).start()
    
    def _show_no_update_message(self):
        """最新版使用中メッセージを表示"""
        try:
            from __version__ import __version__
            version = __version__
        except ImportError:
            version = "不明"
        
        QMessageBox.information(
            self,
            "更新確認",
            f"現在のバージョン（v{version}）は最新です。"
        )
        self.status_bar.showMessage("最新バージョンを使用中")
    
    def _show_update_check_error(self, error: str):
        """更新チェックエラーを表示"""
        QMessageBox.warning(
            self,
            "更新確認エラー",
            f"更新の確認に失敗しました。\n\n{error}"
        )
        self.status_bar.showMessage(f"更新確認エラー: {error}")
    
    def _on_port_checker(self):
        """ポートチェッカーを起動"""
        from .port_checker_gui import PortCheckerGUI
        
        # 既に起動済みかチェック
        if hasattr(self, 'port_checker_window') and self.port_checker_window.isVisible():
            # 既に表示されている場合は前面に表示
            self.port_checker_window.raise_()
            self.port_checker_window.activateWindow()
            return
        
        # 新しいウィンドウを作成
        self.port_checker_window = PortCheckerGUI()
        self.port_checker_window.show()
        self.status_bar.showMessage("ポートチェッカーを起動しました")
    
    def _on_sftp_error(self, device_name: str, error_message: str):
        """
        SFTP操作時のエラー処理
        
        Args:
            device_name: デバイス名
            error_message: エラーメッセージ
        """
        print(f"SFTP Error [{device_name}]: {error_message}")
        self.status_bar.showMessage(f"SFTP エラー ({device_name}): {error_message}")
    
    def _check_for_updates_on_startup(self):
        """起動時の更新チェック"""
        # 設定で無効化されている場合はスキップ
        if not self.config_manager.get_check_on_startup():
            return
        
        # 未適用の更新をチェック
        self._check_pending_updates()
        
        # 新しい更新をチェック（非同期）
        import threading
        def check_thread():
            try:
                # GitHubトークンを取得
                github_token = self.config_manager.get_github_token()
                version_mgr = VersionManager(github_token=github_token)
                
                # 古い更新ファイルをクリーンアップ
                version_mgr.cleanup_old_updates(24)
                
                update_info = version_mgr.check_for_updates()
                
                if update_info and update_info.get('error'):
                    # 起動時は記録に留める（毎回ダイアログを出すと煩わしい）
                    print(f"[Update] {update_info['error']}")
                elif update_info and update_info.get('available'):
                    # スキップされたバージョンかチェック
                    skipped_version = self.config_manager.get_skipped_version()
                    if skipped_version == update_info.get('version'):
                        print(f"[Update] バージョン {update_info.get('version')} はスキップ済み")
                        return
                    
                    # メインスレッドでダイアログを表示（シグナル経由）
                    self.update_available.emit(update_info)
            except Exception as e:
                print(f"[Update] 更新チェックエラー: {e}")
        
        threading.Thread(target=check_thread, daemon=True).start()
    
    def _check_pending_updates(self):
        """未適用の更新ファイルをチェック"""
        version_mgr = VersionManager()
        pending_files = version_mgr.get_pending_update_files()
        
        for zip_path in pending_files:
            if not os.path.exists(zip_path):
                continue

            # ダウンロード時の検証を通ったファイルだけを候補にする。
            # 中断などで残った未検証のZIPを、検証なしで適用させない。
            if not version_mgr.is_verified_update(zip_path):
                print(f"[Main] 検証されていない更新ファイルのため無視します: {zip_path}")
                continue
            
            # ファイルの年齢を確認
            file_age_hours = (datetime.now().timestamp() - os.path.getmtime(zip_path)) / 3600
            
            if file_age_hours > 24:
                # 24時間以上前のファイルは削除済み（cleanup_old_updatesで）
                continue

            # いま動いているものより新しいときだけ勧める。版を見ないと、
            # 手で入れ直したあとに残った古い ZIP でダウングレードさせてしまう。
            pending_version = version_mgr.pending_version(zip_path)
            if not pending_version:
                print("[Main] 版が分からない更新ファイルのため無視します: "
                      f"{zip_path}")
                continue
            if VersionManager.compare_versions(
                    pending_version, version_mgr.CURRENT_VERSION) <= 0:
                print("[Main] 現在のバージョン以下のため無視します: "
                      f"{pending_version}")
                continue
            
            # 適用確認ダイアログ
            reply = QMessageBox.question(
                self,
                "未適用の更新",
                f"前回ダウンロードした更新 v{pending_version}"
                f"（{file_age_hours:.0f}時間前）がまだ適用されていません。\n"
                f"現在のバージョンは v{version_mgr.CURRENT_VERSION} です。\n\n"
                "今すぐ更新を適用しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            
            if reply == QMessageBox.StandardButton.Yes:
                self._apply_pending_update(zip_path)
            
            # 最初の1つだけ処理
            break
    
    def _apply_pending_update(self, zip_path: str):
        """未適用の更新を適用"""
        # updater.batのパスを取得
        import sys
        
        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
            app_path = sys.executable
        else:
            # __file__ = <アプリのルート>\src\ui\main_window.py
            # 2回上がって src/ に、さらに1回上がってアプリのルートに
            app_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            app_path = sys.executable
        
        updater_path = os.path.join(app_dir, "updater.bat")
        
        # デバッグ情報
        print(f"[MainWindow] app_dir: {app_dir}")
        print(f"[MainWindow] updater_path: {updater_path}")
        print(f"[MainWindow] updater.bat exists: {os.path.exists(updater_path)}")
        
        try:
            import subprocess
            # cmd はコンマと等号も引数の区切りとして扱うが、Python の
            # リスト渡しは空白を含む引数しか引用符で包まない。インストール
            # 先に , や = があると updater 側で %1 が途中で切れ、更新が
            # 当たらないまま終わる。自分で包んでコマンド行として渡す。
            command = '"{}" "{}" "{}"'.format(
                updater_path, zip_path, app_path)
            subprocess.Popen(
                command,
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
            
            # アプリケーションを終了
            from PyQt6.QtWidgets import QApplication
            QApplication.quit()
        except Exception as e:
            QMessageBox.critical(
                self,
                "エラー",
                f"更新の適用に失敗しました。\n\n{str(e)}"
            )
    
    def _show_update_dialog(self, update_info: dict):
        """更新ダイアログを表示（メインスレッドで実行）"""
        from .dialogs.update_dialog import UpdateDialog
        
        dialog = UpdateDialog(self, update_info)
        result = dialog.exec()
        
        # 最終チェック時刻を更新
        self.config_manager.set_last_check_time(datetime.now().isoformat())
        
        if result == UpdateDialog.SKIP_VERSION:
            # このバージョンをスキップ
            self.config_manager.set_skipped_version(update_info.get('version'))
            self.status_bar.showMessage(f"バージョン {update_info.get('version')} をスキップしました")
    
    def closeEvent(self, event):
        """
        アプリケーション終了時の処理
        
        Args:
            event: 終了イベント
        """
        # レイアウト（スプリッター幅・選択タブ）を保存
        self._save_layout()
        # Syslogレシーバーを停止
        if hasattr(self, 'syslog_receiver') and self.syslog_receiver.is_running:
            print("[Main] Stopping Syslog receiver...")
            self.syslog_receiver.stop()
        
        # SFTPサーバーを停止
        if hasattr(self, 'sftp_server_panel') and self.sftp_server_panel.sftp_server.is_running:
            print("[Main] Stopping SFTP server...")
            self.sftp_server_panel.sftp_server.stop()
        
        # TFTPサーバーを停止
        if hasattr(self, 'tftp_server_panel') and self.tftp_server_panel.tftp_server.is_running:
            print("[Main] Stopping TFTP server...")
            self.tftp_server_panel.tftp_server.stop()
        
        # FTPサーバーを停止
        if hasattr(self, 'ftp_server_panel') and self.ftp_server_panel.ftp_server.is_running:
            print("[Main] Stopping FTP server...")
            self.ftp_server_panel.ftp_server.stop()
        
        # SNMP Trap 受信とワーカースレッドを停止
        # 実行中の QThread を残したまま終了すると、Qt の後片付けで
        # 解放済みオブジェクトに触れてプロセスが異常終了しうる。
        if hasattr(self, 'snmp_panel') and hasattr(self.snmp_panel, 'snmp_manager'):
            try:
                print("[Main] Stopping SNMP threads...")
                self.snmp_panel.snmp_manager.cancel_operation()
                self.snmp_panel.snmp_manager.stop_trap_receiver()
            except Exception as e:
                print(f"[Main] SNMP 停止エラー: {e}")

        # すべてのマクロをクリーンアップ
        for device_name in list(self.connections.keys()):
            self.macro_manager.cleanup_device(device_name)
        
        # すべてのSFTP接続を切断
        for device_name, sftp_mgr in list(self.sftp_managers.items()):
            try:
                sftp_mgr.disconnect()
            except Exception:
                pass
        self.sftp_managers.clear()
        
        # すべての接続を切断（SSH/シリアル）
        for device_name, conn in list(self.connections.items()):
            conn.disconnect()
        
        self.connections.clear()
        
        # イベントを受け入れて終了
        event.accept()