from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QPushButton, QHBoxLayout, QMenu
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QDrag
from typing import List, Dict, Optional, Set
from core.serial_connection import list_serial_ports
from core.config_manager import is_readable_macro

class DeviceTree(QWidget):
    # シグナル定義
    device_connect = pyqtSignal(str, dict)  # グループ名, 機器データ
    device_edit = pyqtSignal(str, dict)     # グループ名, 機器データ
    # グループ名, 機器名, 機器データ。機器データも添えるのは、同じグループに
    # 同名が並んでいるときに、右クリックした項目を名前だけでは指せないため
    device_delete = pyqtSignal(str, str, dict)
    device_duplicate = pyqtSignal(str, dict) # グループ名, 機器データ
    connect_requested = pyqtSignal(dict)    # 機器データ
    # 移動元グループ名, 移動先グループ名, デバイス名, 機器データ。
    # 機器データも添えるのは device_delete と同じ理由で、同じグループに
    # 同名が並んでいるときに掴んだ項目を名前だけでは指せないため
    device_moved = pyqtSignal(str, str, str, dict)
    group_add_requested = pyqtSignal()  # グループ追加要求
    group_edit_requested = pyqtSignal(str)  # グループ編集要求（グループ名）
    group_delete_requested = pyqtSignal(str)  # グループ削除要求（グループ名）
    hide_requested = pyqtSignal()  # このエリアを隠す要求（戻すのは表示メニュー）
    # 自動検出ポートのボーレート変更（ポート名, ボーレート）。ツリーの外に
    # ある再接続用の写しへ届けるために出す
    serial_baudrate_changed = pyqtSignal(str, int)
    # 機器メニューの「ツール」から、接続中のセッションへの操作を求める（機器名, …）
    keepalive_start_requested = pyqtSignal(str)
    keepalive_stop_requested = pyqtSignal(str)
    macro_execute_requested = pyqtSignal(str, str)  # 機器名, マクロ名
    macro_stop_requested = pyqtSignal(str)
    
    # 一般的なボーレート値
    BAUD_RATES = [300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]
    
    def __init__(self):
        super().__init__()
        # 機器名 -> そのセッションの状態（set_tools_state_provider を参照）
        self._tools_state_provider = None
        # (機器名, 項目の機器データ) -> 接続先が同じか（set_tools_target_check を参照）
        self._tools_target_check = None
        # _set_baudrate の最中か（restore_baudrate の作り直しを 1 回にまとめる）
        self._applying_baudrate = False
        # 「コンソール接続」の折りたたみ状態（None = まだ一度も作っていない）。
        # 作り直すたびに開き直さないよう、作り直す直前の値をここへ控える
        self._console_group_expanded: Optional[bool] = None
        self._create_ui()
        
        # シリアルポート監視用
        self._current_serial_ports: Set[str] = set()  # 現在のポート一覧
        self._serial_port_baudrates: Dict[str, int] = {}  # ポートごとのボーレート設定
        self._serial_monitor_timer = QTimer(self)
        self._serial_monitor_timer.timeout.connect(self._check_serial_ports)
        self._serial_monitor_timer.start(1000)  # 1秒ごとにチェック
    
    def _create_ui(self):
        """UI作成"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        
        # ツリーウィジェット
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("接続先リスト")
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        
        # ドラッグアンドドロップを有効化
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDragDropMode(QTreeWidget.DragDropMode.InternalMove)
        
        # ドロップイベントをカスタム処理するためにサブクラス化したツリーを使用
        self.tree.dropEvent = self._on_drop_event
        
        layout.addWidget(self.tree)
        
        # ボタンエリア
        button_layout = QHBoxLayout()
        
        self.btn_connect = QPushButton("接続")
        self.btn_disconnect = QPushButton("切断")
        self.btn_add = QPushButton("追加")
        
        button_layout.addWidget(self.btn_connect)
        button_layout.addWidget(self.btn_disconnect)
        button_layout.addWidget(self.btn_add)
        
        layout.addLayout(button_layout)
    
    def load_from_config(self, groups: List[Dict]):  # 修正
        """
        設定からツリーを構築
        
        Args:
            groups: グループリスト
        """
        # 作り直しで消える折りたたみ状態を、消す前に控える
        self._remember_console_expanded()
        self.tree.clear()
        
        for group_data in groups:
            # グループアイテム作成
            group_item = QTreeWidgetItem(self.tree, [group_data["name"]])
            
            # 機器アイテム作成
            for device in group_data.get("devices", []):
                device_name = f"{device['name']} ({device['host']})"
                device_item = QTreeWidgetItem(group_item, [device_name])
                # 機器情報をアイテムに保存（後で使う）
                device_item.setData(0, Qt.ItemDataRole.UserRole, device)
            
            # グループを展開
            group_item.setExpanded(True)
        
        # コンソール接続グループを追加
        self._add_serial_ports_group()
    
    def _add_serial_ports_group(self):
        """コンソール接続（シリアルポート）グループを追加"""
        # シリアルポート一覧を取得
        serial_ports = list_serial_ports()
        
        # 現在のポート一覧を更新
        self._current_serial_ports = {port['port'] for port in serial_ports}
        
        if not serial_ports:
            return  # シリアルポートがない場合は何もしない
        
        # コンソール接続グループを作成
        console_group = QTreeWidgetItem(self.tree, ["コンソール接続"])
        
        # 各シリアルポートをアイテムとして追加
        for port_info in serial_ports:
            port_name = port_info['port']
            description = port_info['description']
            
            # 表示名を作成
            display_name = f"{port_name} - {description}"
            
            # デバイスアイテムを作成
            device_item = QTreeWidgetItem(console_group, [display_name])
            
            # ボーレート設定を取得（保存されていなければデフォルトの9600）
            baudrate = self._serial_port_baudrates.get(port_name, 9600)
            
            # シリアル接続用のデバイスデータを作成
            device_data = {
                'name': port_name,
                'type': 'serial',  # シリアル接続であることを示す
                'protocol': 'serial',  # 接続プロトコルを明示
                'port': port_name,
                'baudrate': baudrate,
                'description': description,
                # config 由来ではなく、その場で検出した項目だという印。
                # 機器名の一意性検査は config しか見ないので、ここの名前は
                # 登録機器と同じになりうる。印が無いと、同名の機器が属する
                # グループの自動実行コマンドがコンソールへ流れる
                'source': 'autodetect'
            }
            
            # デバイスデータを保存
            device_item.setData(0, Qt.ItemDataRole.UserRole, device_data)
        
        # 折りたたみ状態を戻す（まだ一度も作っていなければ開いた状態）
        console_group.setExpanded(
            True if self._console_group_expanded is None
            else self._console_group_expanded)

    def _find_console_group(self) -> Optional[QTreeWidgetItem]:
        """ツリーにある「コンソール接続」グループを返す（無ければ None）"""
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            item = root.child(i)
            if item.text(0) == "コンソール接続":
                return item
        return None

    def _remember_console_expanded(self):
        """作り直しの前に「コンソール接続」の折りたたみ状態を控える

        作り直すとグループは別の項目になるので、畳んだ状態は項目と一緒に
        消える。消す直前の値をここで覚えておき、_add_serial_ports_group が
        戻す。まだ一度も作っていないときは控えるものが無いので触らない。
        """
        item = self._find_console_group()
        if item is not None:
            self._console_group_expanded = item.isExpanded()
    
    def detected_port_names(self) -> Set[str]:
        """いまツリーに並んでいる自動検出ポートの名前を返す

        自動検出のCOMポートは config に無いので、機器名の一意性検査が使う
        find_device_group には掛からない。検査側から第4の名前空間として
        参照できるよう、ツリーの実体を見て答える。

        Returns:
            自動検出項目の機器名の集合（無ければ空集合）
        """
        names: Set[str] = set()
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            group_item = root.child(i)
            for j in range(group_item.childCount()):
                data = group_item.child(j).data(0, Qt.ItemDataRole.UserRole)
                if isinstance(data, dict) and data.get('source') == 'autodetect':
                    name = data.get('name')
                    if name:
                        names.add(name)
        return names

    def _check_serial_ports(self):
        """シリアルポートの変化を定期的にチェック"""
        # 現在のシリアルポート一覧を取得
        current_ports = list_serial_ports()
        new_ports = {port['port'] for port in current_ports}
        
        # 変化があった場合のみ更新
        if new_ports != self._current_serial_ports:
            # 削除されたポートを検出
            removed_ports = self._current_serial_ports - new_ports
            if removed_ports:
                print(f"[INFO] シリアルポート削除を検出: {', '.join(removed_ports)}")
            
            # 追加されたポートを検出
            added_ports = new_ports - self._current_serial_ports
            if added_ports:
                print(f"[INFO] シリアルポート追加を検出: {', '.join(added_ports)}")
            
            # リストを更新
            self.refresh_serial_ports()
    
    def refresh_serial_ports(self):
        """シリアルポート一覧を更新"""
        # 既存のコンソール接続グループを削除（折りたたみ状態は控えてから）
        item = self._find_console_group()
        if item is not None:
            self._console_group_expanded = item.isExpanded()
            self.tree.invisibleRootItem().removeChild(item)
        
        # 再度追加
        self._add_serial_ports_group()
    
    def set_tools_state_provider(self, provider):
        """「ツール」に出すセッションの状態を返す関数を登録する

        接続先リストはセッションを持たないので、開くたびに聞く。
        provider(機器名) は connected / keepalive_active /
        command_list_active / macros を持つ辞書を返す。
        """
        self._tools_state_provider = provider

    def set_tools_target_check(self, check):
        """項目の接続先が、その名前のセッションの接続先と同じかを答える関数を登録する

        状態は機器名で引くので、名前だけが同じ別の接続先の項目（後から挿した
        自動検出の COM3 と、登録機器の「COM3」など）からも、そのセッションを
        操作できてしまう。check(機器名, 項目の機器データ) が False なら
        「ツール」を灰色にする。
        """
        self._tools_target_check = check

    def _add_tools_menu(self, menu, device_name, device_data=None):
        """機器メニューに「ツール」を足す（選ばれた項目は要求のシグナルを出す）

        キープアライブとマクロは、以前は端末の右クリックにあった。端末の
        右クリックは貼り付けにしたので、ここへ移した。接続していない機器では
        灰色にして選べないようにする（隠すと、どこにあるのか分からなくなる）。
        項目の接続先がセッションの接続先と違うときも灰色にする。
        ログの保存・記録はメニューバーの「ログ」にあるので入れない。
        """
        state = {}
        if self._tools_state_provider is not None:
            state = self._tools_state_provider(device_name) or {}
        enabled = bool(state.get("connected"))
        if enabled and self._tools_target_check is not None:
            enabled = bool(self._tools_target_check(device_name, device_data))
        tools = menu.addMenu("ツール")
        tools.setEnabled(enabled)

        if state.get("keepalive_active"):
            tools.addAction("キープアライブ停止").triggered.connect(
                lambda: self.keepalive_stop_requested.emit(device_name))
        else:
            tools.addAction("キープアライブ開始").triggered.connect(
                lambda: self.keepalive_start_requested.emit(device_name))
        # 読み手側の保険。状態は設定から来るので、リストでない値や、名前で
        # 指せない要素（辞書でない・name が文字列でない）が混ざると、
        # ここで落ちて右クリックが一切開かなくなる
        raw_macros = state.get("macros")
        macros = ([m for m in raw_macros if is_readable_macro(m)]
                  if isinstance(raw_macros, list) else [])
        if macros:
            macro_menu = tools.addMenu("マクロ実行")
            for macro in macros:
                name = macro.get("name", "")
                description = macro.get("description", "")
                text = f"{name} - {description}" if description else name
                macro_menu.addAction(text).triggered.connect(
                    lambda checked=False, n=name:
                    self.macro_execute_requested.emit(device_name, n))
        # 実行中のマクロを止める。これが無いと、誤ったマクロを流し始めたとき
        # タブを閉じる以外に中断する手段が無い
        if state.get("command_list_active"):
            tools.addAction("マクロ停止").triggered.connect(
                lambda: self.macro_stop_requested.emit(device_name))

    def _add_hide_action(self, menu):
        """どの右クリックメニューにも「非表示」を足す。

        機器の上・グループの上・空欄のどこで押しても同じように隠せる
        ようにする。戻すのは表示メニューから。
        """
        menu.addSeparator()
        return menu.addAction("接続先リストを非表示")

    @staticmethod
    def _discard_menu(menu):
        """開き終えた右クリックメニューを項目ごと捨てる。

        このウィジェットを親にした QMenu は、閉じただけでは子として残る。
        右クリックのたびに QMenu 1 件と項目が積み上がる（実測: 空欄メニューを
        5 回開いて QMenu 5 件 / QAction 20 件）。項目は menu.addAction で
        作っておりメニューが所有しているので、メニューを捨てれば一緒に
        片付く。

        Args:
            menu: 表示し終えた QMenu
        """
        menu.deleteLater()

    def _show_context_menu(self, position):
        """
        右クリックメニューを表示

        Args:
            position: クリック位置
        """
        item = self.tree.itemAt(position)
        
        # アイテムがない場合（空欄）はグループ追加メニューを表示
        if not item:
            self._show_empty_area_menu(position)
            return
        
        # 機器データを取得
        device_data = item.data(0, Qt.ItemDataRole.UserRole)
        
        # グループアイテムの場合はグループメニューを表示
        if not device_data:
            self._show_group_menu(position, item)
            return
        
        # 親グループ名を取得
        parent = item.parent()
        if not parent:
            return
        group_name = parent.text(0)
        
        # コンソール接続かどうかをチェック
        is_serial = device_data.get('type') == 'serial'
        
        # コンテキストメニュー作成
        menu = QMenu(self)
        
        connect_action = menu.addAction("接続")
        self._add_tools_menu(menu, device_data["name"], device_data)
        
        # コンソール接続の場合はボーレート設定メニューを追加
        if is_serial:
            menu.addSeparator()
            
            # ボーレート(Baud Rate)サブメニュー
            baudrate_menu = menu.addMenu("ボーレート(Baud Rate)")
            current_baudrate = device_data.get('baudrate', 9600)
            
            baudrate_actions = []
            for baud in self.BAUD_RATES:
                action = baudrate_menu.addAction(f"{baud} baud")
                # 現在のボーレートにチェックマークを付ける
                if baud == current_baudrate:
                    action.setCheckable(True)
                    action.setChecked(True)
                baudrate_actions.append((action, baud))
            
            edit_action = None
            delete_action = None
            duplicate_action = None
        else:
            # コンソール接続以外は編集・削除・複製メニューを追加
            edit_action = menu.addAction("編集")
            delete_action = menu.addAction("削除")
            menu.addSeparator()
            duplicate_action = menu.addAction("複製")
            baudrate_actions = []
        
        hide_action = self._add_hide_action(menu)

        # メニュー実行
        try:
            action = menu.exec(self.tree.viewport().mapToGlobal(position))
        finally:
            self._discard_menu(menu)

        # アクション処理
        if action == hide_action:
            self.hide_requested.emit()
        elif action == connect_action:
            self.device_connect.emit(group_name, device_data)
        elif action == edit_action and edit_action is not None:
            self.device_edit.emit(group_name, device_data)
        elif action == delete_action and delete_action is not None:
            self.device_delete.emit(group_name, device_data["name"], device_data)
        elif action == duplicate_action and duplicate_action is not None:
            self.device_duplicate.emit(group_name, device_data)
        elif is_serial and baudrate_actions:
            # ボーレート変更処理
            for baud_action, baud_value in baudrate_actions:
                if action == baud_action:
                    self._set_baudrate(device_data['port'], baud_value)
                    break
    
    def get_selected_device(self) -> Optional[tuple]:
        """
        選択中の機器情報を取得
        
        Returns:
            (グループ名, 機器データ) のタプル、または None
        """
        item = self.tree.currentItem()
        if not item:
            return None
        
        device_data = item.data(0, Qt.ItemDataRole.UserRole)
        if not device_data:
            return None
        
        parent = item.parent()
        if not parent:
            return None
        
        group_name = parent.text(0)
        return (group_name, device_data)
    
    def _show_empty_area_menu(self, position):
        """
        空欄右クリックメニューを表示
        
        Args:
            position: クリック位置
        """
        menu = QMenu(self)
        add_group_action = menu.addAction("グループを追加")
        hide_action = self._add_hide_action(menu)

        try:
            action = menu.exec(self.tree.viewport().mapToGlobal(position))
        finally:
            self._discard_menu(menu)

        if action == add_group_action:
            self.group_add_requested.emit()
        elif action == hide_action:
            self.hide_requested.emit()
    
    def _show_group_menu(self, position, group_item: QTreeWidgetItem):
        """
        グループ右クリックメニューを表示
        
        Args:
            position: クリック位置
            group_item: グループアイテム
        """
        group_name = group_item.text(0)
        menu = QMenu(self)

        # コンソール接続グループは編集・削除不可。それでも非表示は出す
        edit_action = delete_action = None
        if group_name != "コンソール接続":
            edit_action = menu.addAction("グループを編集")
            # Defaultグループは削除不可
            if group_name != "Default":
                delete_action = menu.addAction("グループを削除")
        hide_action = self._add_hide_action(menu)

        try:
            action = menu.exec(self.tree.viewport().mapToGlobal(position))
        finally:
            self._discard_menu(menu)

        if action == hide_action:
            self.hide_requested.emit()
        elif action == edit_action and edit_action is not None:
            self.group_edit_requested.emit(group_name)
        elif action == delete_action and delete_action is not None:
            self.group_delete_requested.emit(group_name)
    
    def _set_baudrate(self, port: str, baudrate: int):
        """
        シリアルポートのボーレートを設定
        
        Args:
            port: ポート名
            baudrate: ボーレート値
        """
        # ボーレート設定を保存
        self._serial_port_baudrates[port] = baudrate

        # ツリーの外にも同じ値を持っている相手がいる。MainWindow は初回接続
        # 時の機器データを再接続用に写しており、そこを更新しないと Enter に
        # よる再接続だけ旧ボーレートのまま繋がる。開いているポートがこの値を
        # 拒んだ場合は、MainWindow が restore_baudrate() で実際の値を書き戻す
        # （その作り直しは下の 1 回にまとめる）
        self._applying_baudrate = True
        try:
            self.serial_baudrate_changed.emit(port, baudrate)
        finally:
            self._applying_baudrate = False

        # 受け付けられたと分かってから記録する。要求した時点で「設定しました」
        # と出すと、拒まれたときにログだけが実際の値と食い違う
        applied = self._serial_port_baudrates.get(port, baudrate)
        if applied == baudrate:
            print(f"[INFO] {port} のボーレートを {baudrate} baud に設定しました")
        else:
            print(f"[INFO] {port} のボーレートを {baudrate} baud に"
                  f"変更できませんでした（{applied} baud のままです）")

        # リストを更新（表示には影響しないが、内部データを更新）
        self.refresh_serial_ports()

    def restore_baudrate(self, port: str, baudrate: int):
        """ポートが拒んだボーレートの表示を、実際の値へ戻す

        _set_baudrate は選んだ値を先に覚えて表示し直す。ポートがその値を
        拒むと、チェックと次の接続が使えない値のまま残るので、MainWindow が
        実際の値で呼び直す。serial_baudrate_changed は出さない（出すと
        開いている接続へもう一度同じ値を設定しにいく）。

        _set_baudrate から呼ばれている間は値を書くだけにする。一覧の作り直しは
        実機のポート列挙を伴い、「コンソール接続」を作り直すたびに折りたたみ
        状態も戻るので、拒否 1 回につき 1 度で済ませる（_set_baudrate が
        最後に行う）。接続できたときの経路はここで作り直す。

        Args:
            port: ポート名
            baudrate: 実際に使っているボーレート
        """
        self._serial_port_baudrates[port] = baudrate
        if self._applying_baudrate:
            return
        self.refresh_serial_ports()

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
        """
        アイテムがダブルクリックされたときの処理
        
        Args:
            item: クリックされたアイテム
            column: クリックされた列
        """
        # 機器データを取得
        device_data = item.data(0, Qt.ItemDataRole.UserRole)
        
        # 機器アイテムのみ処理（グループは無視）
        if device_data:
            self.connect_requested.emit(device_data)
    
    def _on_drop_event(self, event):
        """
        ドロップイベント処理
        
        Args:
            event: ドロップイベント
        """
        # ドロップ先のアイテムを取得
        drop_indicator = self.tree.dropIndicatorPosition()
        target_item = self.tree.itemAt(event.position().toPoint())
        
        if not target_item:
            event.ignore()
            return
        
        # ドラッグ元のアイテムを取得
        source_item = self.tree.currentItem()
        if not source_item:
            event.ignore()
            return
        
        # デバイスデータを取得
        device_data = source_item.data(0, Qt.ItemDataRole.UserRole)
        if not device_data:
            event.ignore()
            return
        
        # コンソール接続デバイスはドラッグできない
        if device_data.get('type') == 'serial':
            event.ignore()
            return
        
        # 移動元のグループを取得
        source_parent = source_item.parent()
        if not source_parent:
            event.ignore()
            return
        source_group_name = source_parent.text(0)
        
        # コンソール接続グループからは移動できない
        if source_group_name == "コンソール接続":
            event.ignore()
            return
        
        # ドロップ先のグループを決定
        target_parent = target_item.parent()
        if target_parent:
            # デバイスアイテムにドロップした場合は、その親グループに移動
            target_group_name = target_parent.text(0)
        else:
            # グループアイテムにドロップした場合は、そのグループに移動
            target_group_name = target_item.text(0)
        
        # コンソール接続グループには移動できない
        if target_group_name == "コンソール接続":
            event.ignore()
            return
        
        # 同じグループ内での移動は無視
        if source_group_name == target_group_name:
            event.ignore()
            return
        
        # デバイス移動シグナルを発行
        device_name = device_data.get('name')
        if device_name:
            self.device_moved.emit(source_group_name, target_group_name,
                                   device_name, device_data)
            event.accept()
        else:
            event.ignore()
