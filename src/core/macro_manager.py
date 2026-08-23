"""
マクロ管理モジュール

キープアライブ機能とコマンドリスト実行機能を提供します。
"""

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from typing import Dict, List, Optional, Callable
import time


class MacroManager(QObject):
    """マクロ機能を管理するクラス"""
    
    # シグナル
    command_output = pyqtSignal(str, str)  # (device_name, output_text)
    macro_finished = pyqtSignal(str)  # (device_name)
    macro_error = pyqtSignal(str, str)  # (device_name, error_message)
    
    def __init__(self):
        super().__init__()
        
        # キープアライブタイマー管理（device_name -> QTimer）
        self._keepalive_timers: Dict[str, QTimer] = {}
        
        # コマンドリスト実行管理（device_name -> コマンドリスト）
        self._command_lists: Dict[str, List[str]] = {}
        self._command_indices: Dict[str, int] = {}
        self._command_delays: Dict[str, int] = {}  # ミリ秒単位
        self._command_timers: Dict[str, QTimer] = {}
        
        # コマンド送信コールバック（device_name -> callback）
        self._send_callbacks: Dict[str, Callable] = {}
    
    def register_send_callback(self, device_name: str, callback: Callable):
        """
        コマンド送信コールバックを登録
        
        Args:
            device_name: 機器名
            callback: コマンド送信関数（引数: command文字列）
        """
        self._send_callbacks[device_name] = callback
    
    def unregister_send_callback(self, device_name: str):
        """
        コマンド送信コールバックを解除
        
        Args:
            device_name: 機器名
        """
        if device_name in self._send_callbacks:
            del self._send_callbacks[device_name]
    
    def start_keepalive(self, device_name: str, interval_seconds: int = 60):
        """
        キープアライブを開始
        
        Args:
            device_name: 機器名
            interval_seconds: 送信間隔（秒）
        """
        # 既にキープアライブが動作している場合は停止
        self.stop_keepalive(device_name)
        
        # タイマーを作成
        timer = QTimer()
        timer.timeout.connect(lambda: self._send_keepalive(device_name))
        timer.start(interval_seconds * 1000)  # ミリ秒単位
        
        self._keepalive_timers[device_name] = timer
    
    def stop_keepalive(self, device_name: str):
        """
        キープアライブを停止
        
        Args:
            device_name: 機器名
        """
        if device_name in self._keepalive_timers:
            self._keepalive_timers[device_name].stop()
            del self._keepalive_timers[device_name]
    
    def is_keepalive_active(self, device_name: str) -> bool:
        """
        キープアライブが動作中かチェック
        
        Args:
            device_name: 機器名
        
        Returns:
            bool: 動作中の場合True
        """
        return device_name in self._keepalive_timers
    
    def _send_keepalive(self, device_name: str):
        """
        キープアライブ用のエンターを送信
        
        Args:
            device_name: 機器名
        """
        if device_name in self._send_callbacks:
            # 空のエンターを送信
            self._send_callbacks[device_name]("\r")
    
    def start_command_list(
        self, 
        device_name: str, 
        commands: List[str], 
        delay_ms: int = 1000
    ):
        """
        コマンドリスト実行を開始
        
        Args:
            device_name: 機器名
            commands: 実行するコマンドのリスト
            delay_ms: コマンド間の遅延（ミリ秒）
        """
        # 既にコマンドリストが実行中の場合は停止
        self.stop_command_list(device_name)
        
        # コマンドリストを保存
        self._command_lists[device_name] = commands
        self._command_indices[device_name] = 0
        self._command_delays[device_name] = delay_ms
        
        # タイマーを作成
        timer = QTimer()
        timer.timeout.connect(lambda: self._execute_next_command(device_name))
        self._command_timers[device_name] = timer
        
        # 最初のコマンドを実行
        self._execute_next_command(device_name)
    
    def stop_command_list(self, device_name: str):
        """
        コマンドリスト実行を停止
        
        Args:
            device_name: 機器名
        """
        if device_name in self._command_timers:
            self._command_timers[device_name].stop()
            del self._command_timers[device_name]
        
        if device_name in self._command_lists:
            del self._command_lists[device_name]
            del self._command_indices[device_name]
            del self._command_delays[device_name]
    
    def is_command_list_active(self, device_name: str) -> bool:
        """
        コマンドリスト実行中かチェック
        
        Args:
            device_name: 機器名
        
        Returns:
            bool: 実行中の場合True
        """
        return device_name in self._command_timers
    
    def _execute_next_command(self, device_name: str):
        """
        次のコマンドを実行
        
        Args:
            device_name: 機器名
        """
        if device_name not in self._command_lists:
            return
        
        commands = self._command_lists[device_name]
        index = self._command_indices[device_name]
        
        # すべてのコマンドを実行した場合
        if index >= len(commands):
            self.macro_finished.emit(device_name)
            self.stop_command_list(device_name)
            return
        
        # コマンドを送信
        command = commands[index]
        if device_name in self._send_callbacks:
            # コマンド送信（改行付き）
            self._send_callbacks[device_name](command + "\r")
            
            # インデックスを進める
            self._command_indices[device_name] = index + 1
            
            # 次のコマンドのためにタイマーを設定
            delay = self._command_delays[device_name]
            self._command_timers[device_name].start(delay)
        else:
            # コールバックが登録されていない場合はエラー
            self.macro_error.emit(
                device_name,
                "コマンド送信コールバックが登録されていません"
            )
            self.stop_command_list(device_name)
    
    def cleanup_device(self, device_name: str):
        """
        機器切断時のクリーンアップ
        
        Args:
            device_name: 機器名
        """
        self.stop_keepalive(device_name)
        self.stop_command_list(device_name)
        self.unregister_send_callback(device_name)


class MacroPreset:
    """マクロプリセット（保存用）"""
    
    def __init__(self, name: str, commands: List[str], description: str = ""):
        self.name = name
        self.commands = commands
        self.description = description
    
    def to_dict(self) -> dict:
        """辞書形式に変換"""
        return {
            "name": self.name,
            "commands": self.commands,
            "description": self.description
        }
    
    @staticmethod
    def from_dict(data: dict) -> 'MacroPreset':
        """辞書から作成"""
        return MacroPreset(
            name=data.get("name", ""),
            commands=data.get("commands", []),
            description=data.get("description", "")
        )