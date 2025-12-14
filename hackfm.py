#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# GNU Radio Python Flow Graph
# Title: FM Playlist Transmitter
# GNU Radio version: 3.10.11.0

from gnuradio import analog
from gnuradio import blocks
import pmt
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
from gnuradio.fft import window
import sys
import signal
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
import osmosdr
import time
import threading
import os
import numpy as np
import wave
import shutil
import subprocess
import tempfile
import math  # 添加 math 库用于计算 FM 灵敏度
import sys
import msvcrt  # Windows控制台输入
from enum import Enum
try:
    from PyQt5.QtWidgets import *
    from PyQt5.QtCore import *
    from PyQt5.QtGui import *
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False
    print("PyQt5未安装，请运行: pip install PyQt5")

class PlayMode(Enum):
    SEQUENTIAL = "顺序播放"
    SHUFFLE = "随机播放"
    REPEAT_ONE = "单曲循环"

class PlaybackController:
    def __init__(self, playlist_source):
        self.playlist_source = playlist_source
        self.play_mode = PlayMode.SEQUENTIAL
        self.paused = False
        self.seek_offset = 0  # 跳转偏移量（秒）
        self.current_file_pos = 0  # 当前文件播放位置（秒）
        self.last_update_time = time.time()
        
    def set_play_mode(self, mode):
        """设置播放模式"""
        if isinstance(mode, PlayMode):
            self.play_mode = mode
        else:
            # 字符串转换
            mode_map = {
                '1': PlayMode.SEQUENTIAL,
                '2': PlayMode.SHUFFLE,
                '3': PlayMode.REPEAT_ONE
            }
            if mode in mode_map:
                self.play_mode = mode_map[mode]
                
        # 更新播放源的配置
        if self.playlist_source:
            if self.play_mode == PlayMode.SHUFFLE:
                self.playlist_source.shuffle = True
            else:
                self.playlist_source.shuffle = False
                
    def toggle_pause(self):
        """暂停/继续播放"""
        self.paused = not self.paused
        return self.paused
        
    def seek_forward(self, seconds=10):
        """前进指定秒数"""
        self.seek_offset += seconds
        
    def seek_backward(self, seconds=10):
        """后退指定秒数"""
        self.seek_offset -= seconds
        if self.seek_offset < 0:
            self.seek_offset = 0
            
    def next_track(self):
        """下一曲"""
        if self.playlist_source:
            self.playlist_source.next_file()
            self.seek_offset = 0
            self.current_file_pos = 0
            
    def previous_track(self):
        """上一曲"""
        if self.playlist_source and len(self.playlist_source.file_list) > 0:
            # 回到上一首或当前歌曲重新开始
            if self.current_file_pos > 3:  # 如果播放超过3秒，重新开始当前歌曲
                self.seek_offset = 0
                self.current_file_pos = 0
            else:  # 否则回到上一首
                self.playlist_source.current_file_idx -= 2
                if self.playlist_source.current_file_idx < -1:
                    self.playlist_source.current_file_idx = len(self.playlist_source.file_list) - 2
                self.playlist_source.next_file()
                self.seek_offset = 0
                self.current_file_pos = 0
                
    def update_position(self):
        """更新播放位置"""
        if not self.paused:
            current_time = time.time()
            time_diff = current_time - self.last_update_time
            self.current_file_pos += time_diff
            self.last_update_time = current_time
            
        # 处理跳转
        if abs(self.seek_offset) > 0.1 and self.playlist_source and self.playlist_source.current_file:
            try:
                # 计算目标位置
                current_pos = self.playlist_source.current_file.tell()
                bytes_per_second = 44100 * 2 * 2  # 44100Hz, 2声道, 2字节/sample
                target_pos = current_pos + int(self.seek_offset * bytes_per_second)
                
                # 确保位置在有效范围内
                if target_pos >= 44:  # WAV文件头44字节
                    self.playlist_source.current_file.seek(target_pos)
                else:
                    self.playlist_source.current_file.seek(44)
                    
                self.seek_offset = 0
            except Exception:
                pass  # 如果跳转失败，忽略
                
    def get_current_info(self):
        """获取当前播放信息"""
        if not self.playlist_source or not self.playlist_source.current_file:
            return "无播放信息"
            
        current_file = self.playlist_source.current_file_path
        if not current_file:
            return "无播放信息"
            
        # 获取文件名
        file_name = os.path.basename(current_file)
        
        # 获取播放位置
        position = self.current_file_pos + self.seek_offset
        if position < 0:
            position = 0
            
        # 格式化时间
        minutes = int(position // 60)
        seconds = int(position % 60)
        
        # 获取播放模式
        mode_text = self.play_mode.value
        
        # 获取状态
        status = "暂停" if self.paused else "播放中"
        
        return f"[{status}] {file_name} | {minutes:02d}:{seconds:02d} | {mode_text}"

class DisplayManager:
    def __init__(self, controller):
        self.controller = controller
        self.running = True
        self.display_thread = None
        self.input_thread = None
        
    def start(self):
        """启动显示和输入监控线程"""
        self.display_thread = threading.Thread(target=self._display_loop, daemon=True)
        self.input_thread = threading.Thread(target=self._input_loop, daemon=True)
        
        self.display_thread.start()
        self.input_thread.start()
        
    def stop(self):
        """停止所有线程"""
        self.running = False
        
    def _display_loop(self):
        """显示循环，每秒更新一次"""
        while self.running:
            try:
                # 更新播放位置
                self.controller.update_position()
                
                # 获取当前信息
                info = self.controller.get_current_info()
                
                # 清空当前行并显示信息
                print(f"\r{' ' * 100}\r{info}", end='', flush=True)
                
                time.sleep(1)  # 每秒更新一次
                
            except Exception as e:
                print(f"\r显示错误: {e}", end='', flush=True)
                time.sleep(1)
                
    def _input_loop(self):
        """输入监控循环"""
        print("\n\n播放控制命令:")
        print("空格键 - 暂停/继续")
        print("n - 下一曲")
        print("p - 上一曲")
        print("→ - 前进10秒")
        print("← - 后退10秒")
        print("1 - 顺序播放")
        print("2 - 随机播放") 
        print("3 - 单曲循环")
        print("q - 退出")
        print("-" * 50)
        
        while self.running:
            try:
                if msvcrt.kbhit():  # 检查是否有按键
                    key = msvcrt.getch().decode('utf-8', errors='ignore').lower()
                    
                    if key == ' ':  # 空格键 - 暂停/继续
                        paused = self.controller.toggle_pause()
                        status = "已暂停" if paused else "继续播放"
                        print(f"\r{status}", end='', flush=True)
                        
                    elif key == 'n':  # 下一曲
                        self.controller.next_track()
                        print(f"\r下一曲", end='', flush=True)
                        
                    elif key == 'p':  # 上一曲
                        self.controller.previous_track()
                        print(f"\r上一曲", end='', flush=True)
                        
                    elif key == '\xe0':  # 特殊键（方向键）
                        # 读取第二个字节
                        key2 = msvcrt.getch().decode('utf-8', errors='ignore')
                        if key2 == 'M':  # 右箭头 - 前进
                            self.controller.seek_forward()
                            print(f"\r前进10秒", end='', flush=True)
                        elif key2 == 'K':  # 左箭头 - 后退
                            self.controller.seek_backward()
                            print(f"\r后退10秒", end='', flush=True)
                            
                    elif key in ['1', '2', '3']:  # 播放模式
                        mode_map = {'1': '顺序播放', '2': '随机播放', '3': '单曲循环'}
                        self.controller.set_play_mode(key)
                        print(f"\r切换到{mode_map[key]}", end='', flush=True)
                        
                    elif key == 'q':  # 退出
                        print(f"\r正在退出...")
                        self.running = False
                        break
                        
                time.sleep(0.1)
            except Exception as e:
                print(f"\r输入错误: {e}", end='', flush=True)
                

class FMApplicationGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FM发射器控制面板")
        self.setGeometry(100, 100, 800, 600)
        
        # FM发射器相关
        self.fm_console = None
        self.controller = None
        self.is_playing = False
        self.update_timer = None
        
        # 创建界面
        self.create_widgets()
        
        # 设置窗口图标（如果有的话）
        # self.setWindowIcon(QIcon('icon.png'))
        
    def create_widgets(self):
        """创建界面组件"""
        # 创建中央部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 主布局
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # 标题
        title_label = QLabel("FM发射器控制面板")
        title_label.setAlignment(Qt.AlignCenter)
        title_font = QFont("微软雅黑", 16, QFont.Bold)
        title_label.setFont(title_font)
        main_layout.addWidget(title_label)
        
        # 主要内容区域 - 水平布局
        content_layout = QHBoxLayout()
        main_layout.addLayout(content_layout)
        
        # 左侧控制面板
        control_group = QGroupBox("播放控制")
        control_layout = QVBoxLayout(control_group)
        
        # 播放控制按钮
        self.play_pause_btn = QPushButton("▶ 开始播放")
        self.play_pause_btn.setFont(QFont("微软雅黑", 12, QFont.Bold))
        self.play_pause_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 10px;
                border-radius: 5px;
                min-width: 120px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b40;
            }
        """)
        self.play_pause_btn.clicked.connect(self.toggle_play_pause)
        control_layout.addWidget(self.play_pause_btn, alignment=Qt.AlignCenter)
        
        # 导航按钮
        nav_layout = QHBoxLayout()
        self.prev_btn = QPushButton("⏮ 上一曲")
        self.prev_btn.setFont(QFont("微软雅黑", 10))
        self.prev_btn.clicked.connect(self.previous_track)
        nav_layout.addWidget(self.prev_btn)
        
        self.next_btn = QPushButton("⏭ 下一曲")
        self.next_btn.setFont(QFont("微软雅黑", 10))
        self.next_btn.clicked.connect(self.next_track)
        nav_layout.addWidget(self.next_btn)
        control_layout.addLayout(nav_layout)
        
        # 跳转按钮
        seek_layout = QHBoxLayout()
        self.seek_back_btn = QPushButton("⏪ 后退10秒")
        self.seek_back_btn.setFont(QFont("微软雅黑", 10))
        self.seek_back_btn.clicked.connect(self.seek_backward)
        seek_layout.addWidget(self.seek_back_btn)
        
        self.seek_forward_btn = QPushButton("⏩ 前进10秒")
        self.seek_forward_btn.setFont(QFont("微软雅黑", 10))
        self.seek_forward_btn.clicked.connect(self.seek_forward)
        seek_layout.addWidget(self.seek_forward_btn)
        control_layout.addLayout(seek_layout)
        
        # 播放模式
        mode_group = QGroupBox("播放模式")
        mode_layout = QVBoxLayout(mode_group)
        
        self.play_mode_group = QButtonGroup()
        self.mode_sequential = QRadioButton("顺序播放")
        self.mode_sequential.setChecked(True)
        self.mode_shuffle = QRadioButton("随机播放")
        self.mode_repeat = QRadioButton("单曲循环")
        
        self.play_mode_group.addButton(self.mode_sequential, 1)
        self.play_mode_group.addButton(self.mode_shuffle, 2)
        self.play_mode_group.addButton(self.mode_repeat, 3)
        
        self.play_mode_group.buttonClicked.connect(self.change_play_mode)
        
        mode_layout.addWidget(self.mode_sequential)
        mode_layout.addWidget(self.mode_shuffle)
        mode_layout.addWidget(self.mode_repeat)
        control_layout.addWidget(mode_group)
        
        # 参数设置
        param_group = QGroupBox("发射参数")
        param_layout = QFormLayout(param_group)
        
        self.freq_input = QLineEdit("100.0")
        self.freq_input.setFont(QFont("微软雅黑", 10))
        param_layout.addRow("频率 (MHz):", self.freq_input)
        
        self.power_input = QLineEdit("30")
        self.power_input.setFont(QFont("微软雅黑", 10))
        param_layout.addRow("功率 (dB):", self.power_input)
        control_layout.addWidget(param_group)
        
        # 音乐目录
        dir_group = QGroupBox("音乐目录")
        dir_layout = QVBoxLayout(dir_group)
        
        self.dir_label = QLabel("未选择目录")
        self.dir_label.setFont(QFont("微软雅黑", 10))
        self.dir_label.setWordWrap(True)
        dir_layout.addWidget(self.dir_label)
        
        self.browse_btn = QPushButton("📁 选择目录")
        self.browse_btn.setFont(QFont("微软雅黑", 10))
        self.browse_btn.clicked.connect(self.browse_directory)
        dir_layout.addWidget(self.browse_btn)
        control_layout.addWidget(dir_group)
        
        # 添加弹簧
        control_layout.addStretch()
        content_layout.addWidget(control_group)
        
        # 右侧信息显示
        info_group = QGroupBox("播放信息")
        info_layout = QVBoxLayout(info_group)
        
        # 当前播放信息
        self.current_song_label = QLabel("当前无播放")
        self.current_song_label.setFont(QFont("微软雅黑", 12, QFont.Bold))
        self.current_song_label.setWordWrap(True)
        info_layout.addWidget(self.current_song_label)
        
        self.time_label = QLabel("时间: 00:00")
        self.time_label.setFont(QFont("微软雅黑", 10))
        info_layout.addWidget(self.time_label)
        
        self.mode_label = QLabel("模式: 顺序播放")
        self.mode_label.setFont(QFont("微软雅黑", 10))
        info_layout.addWidget(self.mode_label)
        
        self.status_label = QLabel("状态: 停止")
        self.status_label.setFont(QFont("微软雅黑", 10))
        info_layout.addWidget(self.status_label)
        
        # 播放列表
        playlist_group = QGroupBox("播放列表")
        playlist_layout = QVBoxLayout(playlist_group)
        
        self.playlist_widget = QListWidget()
        self.playlist_widget.setFont(QFont("微软雅黑", 10))
        playlist_layout.addWidget(self.playlist_widget)
        
        info_layout.addWidget(playlist_group)
        content_layout.addWidget(info_group)
        
        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")
         
    def browse_directory(self):
        """浏览音乐目录"""
        directory = QFileDialog.getExistingDirectory(self, "选择音乐目录")
        if directory:
            self.dir_entry.setText(directory)
            self.update_playlist_display(directory)
            
    def update_playlist_display(self, directory):
        """更新播放列表显示"""
        self.playlist_widget.clear()
        try:
            # 查找音频文件
            audio_files = []
            valid_extensions = ('.wav', '.mp3', '.flac', '.ogg')
            for root, dirs, files in os.walk(directory):
                files = sorted(files)
                for file in files:
                    if file.lower().endswith(valid_extensions):
                        audio_files.append(file)
                        
            # 添加到列表框
            for file in audio_files:
                self.playlist_widget.addItem(file)
                
            self.status_bar.showMessage(f"找到 {len(audio_files)} 个音频文件")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"读取目录失败: {str(e)}")
            
    def toggle_play_pause(self):
        """切换播放/暂停状态"""
        if not self.dir_entry.text() or self.dir_entry.text() == "":
            QMessageBox.warning(self, "警告", "请先选择音乐目录")
            return
            
        try:
            if not self.is_playing:
                self.start_playback()
            else:
                self.pause_playback()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"播放控制失败: {str(e)}")
            
    def start_playback(self):
        """开始播放"""
        # 获取参数
        freq_mhz = float(self.freq_entry.text())
        power_db = int(self.power_entry.text())
        directory = self.dir_entry.text()
        
        # 转换为Hz
        freq_hz = int(freq_mhz * 1e6)
        
        # 创建FM发射器
        self.fm_console = FM_console(music_dir=directory, freq=freq_hz, power=power_db)
        self.controller = self.fm_console.controller
        
        # 启动发射器
        self.fm_console.start()
        self.fm_console.flowgraph_started.set()
        
        self.is_playing = True
        self.play_pause_btn.setText("⏸ 暂停播放")
        self.status_bar.showMessage("正在播放")
        
        # 开始更新显示
        self.update_display()
        
    def pause_playback(self):
        """暂停播放"""
        if self.controller:
            self.controller.toggle_pause()
            if self.controller.paused:
                self.play_pause_btn.setText("▶ 继续播放")
                self.status_bar.showMessage("已暂停")
            else:
                self.play_pause_btn.setText("⏸ 暂停播放")
                self.status_bar.showMessage("正在播放")
                
    def stop_playback(self):
        """停止播放"""
        if self.fm_console:
            self.fm_console.stop()
            self.fm_console.wait()
            self.fm_console = None
            self.controller = None
            
        self.is_playing = False
        self.play_pause_btn.setText("▶ 开始播放")
        self.status_bar.showMessage("已停止")
        self.current_song_label.setText("当前无播放")
        self.time_label.setText("时间: 00:00")
        
        # 停止更新定时器
        if self.update_timer:
            self.update_timer.stop()
            
    def next_track(self):
        """下一曲"""
        if self.controller:
            self.controller.next_track()
            self.status_bar.showMessage("切换到下一曲")
            
    def previous_track(self):
        """上一曲"""
        if self.controller:
            self.controller.previous_track()
            self.status_bar.showMessage("切换到上一曲")
            
    def seek_forward(self):
        """前进10秒"""
        if self.controller:
            self.controller.seek_forward()
            self.status_bar.showMessage("前进10秒")
            
    def seek_backward(self):
        """后退10秒"""
        if self.controller:
            self.controller.seek_backward()
            self.status_bar.showMessage("后退10秒")
            
    def change_play_mode(self):
        """改变播放模式"""
        if self.controller:
            if self.seq_radio.isChecked():
                mode = "1"
                mode_text = "顺序播放"
            elif self.shuffle_radio.isChecked():
                mode = "2"
                mode_text = "随机播放"
            elif self.repeat_radio.isChecked():
                mode = "3"
                mode_text = "单曲循环"
            else:
                return
                
            self.controller.set_play_mode(mode)
            self.status_bar.showMessage(f"切换到{mode_text}")
            
    def update_display(self):
        """更新显示信息"""
        if self.controller and self.is_playing:
            try:
                # 更新播放位置
                self.controller.update_position()
                
                # 获取当前信息
                info = self.controller.get_current_info()
                
                # 解析信息
                if "无播放信息" not in info:
                    # 提取文件名
                    if "]" in info and "|" in info:
                        parts = info.split("|")
                        if len(parts) >= 2:
                            status_file = parts[0].strip()
                            time_part = parts[1].strip()
                            mode_part = parts[2].strip() if len(parts) > 2 else ""
                            
                            # 提取文件名
                            if "]" in status_file:
                                file_name = status_file.split("]")[1].strip()
                                self.current_song_label.setText(file_name)
                            
                            # 更新时间
                            self.time_label.setText(f"时间: {time_part}")
                            
                            # 更新模式
                            self.mode_label.setText(f"模式: {mode_part}")
                            
                            # 更新状态
                            if "暂停" in status_file:
                                self.status_label.setText("状态: 暂停")
                            else:
                                self.status_label.setText("状态: 播放中")
                
                # 继续更新
                self.update_timer = QTimer()
                self.update_timer.timeout.connect(self.update_display)
                self.update_timer.start(1000)  # 1秒更新一次
                
            except Exception as e:
                self.status_bar.showMessage(f"更新显示错误: {str(e)}")
                self.update_timer = QTimer()
                self.update_timer.timeout.connect(self.update_display)
                self.update_timer.start(1000)  # 1秒更新一次
        
    def closeEvent(self, event):
        """窗口关闭事件处理"""
        if self.is_playing:
            reply = QMessageBox.question(self, '退出', '正在播放中，确定要退出吗？',
                                       QMessageBox.Yes | QMessageBox.No,
                                       QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.stop_playback()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()
            
    def run(self):
        """运行GUI应用"""
        self.show()  # 显示窗口
        # 注意：PyQt5的事件循环将在主程序的app.exec_()中运行
        
    def create_widgets(self):
        """创建界面组件"""
        # 创建中央部件
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # 主布局
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # 标题
        title_label = QLabel("FM发射器控制面板")
        title_label.setAlignment(Qt.AlignCenter)
        title_font = QFont("微软雅黑", 16, QFont.Bold)
        title_label.setFont(title_font)
        main_layout.addWidget(title_label)
        
        # 主要内容区域 - 水平布局
        content_layout = QHBoxLayout()
        main_layout.addLayout(content_layout)
        
        # 左侧控制面板
        control_group = QGroupBox("播放控制")
        control_layout = QVBoxLayout(control_group)
        
        # 播放控制按钮
        self.play_pause_btn = QPushButton("▶ 开始播放")
        self.play_pause_btn.setFont(QFont("微软雅黑", 12, QFont.Bold))
        self.play_pause_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 10px;
                border-radius: 5px;
                min-width: 120px;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b40;
            }
        """)
        self.play_pause_btn.clicked.connect(self.toggle_play_pause)
        control_layout.addWidget(self.play_pause_btn, alignment=Qt.AlignCenter)
        
        # 导航按钮（上一曲/下一曲）
        nav_layout = QHBoxLayout()
        self.prev_btn = QPushButton("⏮ 上一曲")
        self.prev_btn.setFont(QFont("微软雅黑", 10))
        self.prev_btn.clicked.connect(self.previous_track)
        nav_layout.addWidget(self.prev_btn)
        
        self.next_btn = QPushButton("⏭ 下一曲")
        self.next_btn.setFont(QFont("微软雅黑", 10))
        self.next_btn.clicked.connect(self.next_track)
        nav_layout.addWidget(self.next_btn)
        control_layout.addLayout(nav_layout)
        
        # 跳转按钮（前进/后退）
        seek_layout = QHBoxLayout()
        self.seek_back_btn = QPushButton("⏪ 后退10秒")
        self.seek_back_btn.setFont(QFont("微软雅黑", 10))
        self.seek_back_btn.clicked.connect(self.seek_backward)
        seek_layout.addWidget(self.seek_back_btn)
        
        self.seek_forward_btn = QPushButton("⏩ 前进10秒")
        self.seek_forward_btn.setFont(QFont("微软雅黑", 10))
        self.seek_forward_btn.clicked.connect(self.seek_forward)
        seek_layout.addWidget(self.seek_forward_btn)
        control_layout.addLayout(seek_layout)
        
        # 播放模式选择
        mode_group = QGroupBox("播放模式")
        mode_layout = QVBoxLayout(mode_group)
        
        self.play_mode_var = "1"  # 默认顺序播放
        
        self.seq_radio = QRadioButton("顺序播放")
        self.seq_radio.setChecked(True)
        self.seq_radio.clicked.connect(lambda: self.change_play_mode())
        mode_layout.addWidget(self.seq_radio)
        
        self.shuffle_radio = QRadioButton("随机播放")
        self.shuffle_radio.clicked.connect(lambda: self.change_play_mode())
        mode_layout.addWidget(self.shuffle_radio)
        
        self.repeat_radio = QRadioButton("单曲循环")
        self.repeat_radio.clicked.connect(lambda: self.change_play_mode())
        mode_layout.addWidget(self.repeat_radio)
        
        control_layout.addWidget(mode_group)
        
        # 发射参数设置
        param_group = QGroupBox("发射参数")
        param_layout = QFormLayout(param_group)
        
        self.freq_var = "100.0"
        self.power_var = "30"
        
        self.freq_entry = QLineEdit(self.freq_var)
        self.freq_entry.setMaximumWidth(100)
        param_layout.addRow("频率 (MHz):", self.freq_entry)
        
        self.power_entry = QLineEdit(self.power_var)
        self.power_entry.setMaximumWidth(100)
        param_layout.addRow("功率 (dB):", self.power_entry)
        
        control_layout.addWidget(param_group)
        
        # 音乐目录选择
        dir_group = QGroupBox("音乐目录")
        dir_layout = QVBoxLayout(dir_group)
        
        self.dir_var = ""
        self.dir_entry = QLineEdit(self.dir_var)
        self.dir_entry.setReadOnly(True)
        dir_layout.addWidget(self.dir_entry)
        
        self.browse_btn = QPushButton("📁 选择目录")
        self.browse_btn.setFont(QFont("微软雅黑", 10))
        self.browse_btn.clicked.connect(self.browse_directory)
        dir_layout.addWidget(self.browse_btn)
        
        control_layout.addWidget(dir_group)
        
        # 添加弹簧使控件向上对齐
        control_layout.addStretch()
        
        content_layout.addWidget(control_group)
        
        # 右侧信息显示区域
        right_panel = QVBoxLayout()
        
        # 播放信息组
        info_group = QGroupBox("播放信息")
        info_layout = QVBoxLayout(info_group)
        
        self.current_song_label = QLabel("当前无播放")
        self.current_song_label.setFont(QFont("微软雅黑", 12, QFont.Bold))
        self.current_song_label.setWordWrap(True)
        info_layout.addWidget(self.current_song_label)
        
        self.time_label = QLabel("时间: 00:00")
        self.time_label.setFont(QFont("微软雅黑", 10))
        info_layout.addWidget(self.time_label)
        
        self.mode_label = QLabel("模式: 顺序播放")
        self.mode_label.setFont(QFont("微软雅黑", 10))
        info_layout.addWidget(self.mode_label)
        
        self.status_label = QLabel("状态: 停止")
        self.status_label.setFont(QFont("微软雅黑", 10))
        info_layout.addWidget(self.status_label)
        
        right_panel.addWidget(info_group)
        
        # 播放列表组
        playlist_group = QGroupBox("播放列表")
        playlist_layout = QVBoxLayout(playlist_group)
        
        self.playlist_widget = QListWidget()
        self.playlist_widget.setMaximumHeight(200)
        playlist_layout.addWidget(self.playlist_widget)
        
        right_panel.addWidget(playlist_group)
        
        content_layout.addLayout(right_panel)
        
        # 状态栏
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪")


class playlist_file_source(gr.sync_block):
    """
    Stream PCM data from all WAV/MP3 files in a directory (playlist) sequentially as shorts.
    For MP3 (and for WAVs with incorrect sample rate), auto-convert on the fly into WAV with 44100Hz, original channels/sample format.
    Uses ./temp folder for transient data. Does not downmix or change bit depth.
    Loops through all files repeatedly if repeat=True.
    音频自动归一化到满幅（-1~1区间），避免响度太小或炸音
    
    [Update for Stereo]
    Force conversion to 2 channels (Stereo). Outputs two streams (L, R).
    """
    def __init__(self, dir_path, repeat=True, dtype=np.int16, chunk_size=4096, target_headroom=0.98, shuffle=False):
        gr.sync_block.__init__(self,
            name="playlist_file_source",
            in_sig=None,
            # [Stereo] 输出两个 int16 端口：左声道，右声道
            out_sig=[np.int16, np.int16])
        self.dir_path = dir_path
        self.repeat = repeat
        self.shuffle = shuffle
        self.dtype = dtype
        self.chunk_size = chunk_size
        self.target_headroom = target_headroom  # 保证最大值不会爆 1.0，防削波，典型取0.98~0.99
        self.temp_dir = os.path.abspath('./temp')
        os.makedirs(self.temp_dir, exist_ok=True)
        self._clear_temp_dir()
        self.file_list = self._find_audio_files()
        if not self.file_list:
            raise RuntimeError(f"No audio files found in {self.dir_path}")
        
        # 随机播放模式：打乱文件列表
        if self.shuffle:
            import random
            random.shuffle(self.file_list)
            print(f"随机播放模式已启用，共 {len(self.file_list)} 首歌曲")
            # 保存原始文件列表用于避免重复
            self._played_indices = set()
            self._original_file_list = self.file_list.copy()
        
        self.current_file_idx = 0
        self.current_file = None
        self.current_file_path = None
        self.current_gain = 1.0   # 会在 open_current_file 时更新
        self.open_current_file()

    def _clear_temp_dir(self):
        for f in os.listdir(self.temp_dir):
            path = os.path.join(self.temp_dir, f)
            if os.path.isfile(path):
                os.remove(path)

    def _find_audio_files(self):
        audio_files = []
        # 添加更多支持的格式：.flac, .ogg, .mp3, .wav
        valid_extensions = ('.wav', '.mp3', '.flac', '.ogg')
        for root, dirs, files in os.walk(self.dir_path):
            files = sorted(files)
            for file in files:
                if file.lower().endswith(valid_extensions):
                    audio_files.append(os.path.join(root, file))
        return audio_files

    def _get_wav_info(self, fname):
        try:
            with wave.open(fname, 'rb') as w:
                sr = w.getframerate()
                ch = w.getnchannels()
                sw = w.getsampwidth()
                return sr, ch, sw
        except Exception:
            return None, None, None

    def _needs_conversion(self, fname):
        # 检查是否为非 WAV 格式 (MP3, FLAC, OGG 等)
        if fname.lower().endswith(('.mp3', '.flac', '.ogg')):
            return True
        
        sr, ch, sw = self._get_wav_info(fname)
        # 关键修复：除了检查采样率，还必须检查声道数
        # [Stereo] 如果不是 44100Hz 或者不是双声道(ch!=2)，则需要转换
        if sr != 44100 or ch != 2:
            return True
        return False

    def _make_temp_wav(self, source_path):
        base = os.path.basename(source_path)
        name, _ext = os.path.splitext(base)
        temp_wav = os.path.join(self.temp_dir, f"{name}_{int(time.time()*1e6)%1000000}.wav")
        
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-y',
            '-i', source_path,
            '-ar', '44100',
            '-ac', '2',  # 关键修复：[Stereo] 强制转换为双声道 (-ac 2)
            temp_wav
        ]
        ret = subprocess.call(cmd)
        if ret != 0:
            raise RuntimeError(f"ffmpeg failed to convert {source_path}")
        return temp_wav

    def _wav_max_abs(self, fpath):
        # 只取最大幅度，避免读取全部造成内存溢出
        try:
            with wave.open(fpath, 'rb') as w:
                sample_width = w.getsampwidth()
                channels = w.getnchannels()
                dtype = np.int16 if sample_width == 2 else None
                if dtype is None:
                    return 32767  # 不支持其他类型，返回最大
                
                max_abs = 0
                buffer_size = 4096 * channels
                
                while True:
                    data = w.readframes(buffer_size)
                    if not data:
                        break
                    arr = np.frombuffer(data, dtype=dtype)
                    if arr.size == 0:
                        continue
                    m = np.abs(arr).max()
                    if m > max_abs:
                        max_abs = m
                return max_abs
        except Exception:
            return 32767

    def open_current_file(self):
        self._cleanup_old_temp()
        real_path = self.file_list[self.current_file_idx]
        
        # 检查是否需要转换（MP3/FLAC/OGG, 错误的采样率, 或非立体声）
        if self._needs_conversion(real_path):
            temp_wav = self._make_temp_wav(real_path)
            self.current_file_path = temp_wav
        else:
            self.current_file_path = real_path
            
        self.current_file = open(self.current_file_path, 'rb')
        self.current_file.seek(44)
        print(f"Now playing: {real_path}")
        
        # 计算最大幅度，用于归一化
        max_abs = self._wav_max_abs(self.current_file_path)
        if max_abs == 0:
            self.current_gain = 1.0
        else:
            self.current_gain = float(self.target_headroom * 32767.0 / max_abs)
        
        # 不要超过2倍，过大说明采样值异常
        if self.current_gain > 2.0:
            self.current_gain = 2.0
            
        print(f"Auto gain factor: {self.current_gain:.3f} (file max abs sample={max_abs})")

    def _cleanup_old_temp(self):
        if hasattr(self, 'current_file_path') and self.current_file_path:
            if os.path.abspath(self.current_file_path).startswith(self.temp_dir) and os.path.exists(self.current_file_path):
                try:
                    os.remove(self.current_file_path)
                except Exception:
                    pass

    def next_file(self):
        if self.shuffle:
            # 随机播放模式：随机选择下一首歌，避免重复播放同一首歌
            import random
            if len(self._played_indices) >= len(self.file_list):
                # 所有歌曲都已播放过，重置播放记录
                self._played_indices.clear()
                if self.repeat:
                    # 重新打乱列表
                    random.shuffle(self.file_list)
                    print("所有歌曲已播放完毕，重新打乱播放列表")
                else:
                    self.current_file = None
                    self._cleanup_old_temp()
                    return
            
            # 选择一个未播放过的歌曲
            available_indices = [i for i in range(len(self.file_list)) if i not in self._played_indices]
            self.current_file_idx = random.choice(available_indices)
            self._played_indices.add(self.current_file_idx)
        else:
            # 顺序播放模式：按索引递增
            self.current_file_idx += 1
            if self.current_file_idx >= len(self.file_list):
                if self.repeat:
                    self.current_file_idx = 0
                else:
                    self.current_file = None
                    self._cleanup_old_temp()
                    return
        self.open_current_file()

    def work(self, input_items, output_items):
        out_l = output_items[0]
        out_r = output_items[1]
        
        # 确保输出长度一致
        n_out = min(len(out_l), len(out_r))
        produced = 0
        
        while produced < n_out:
            if self.current_file is None:
                out_l[produced:] = 0
                out_r[produced:] = 0
                return produced
            
            # 检查是否暂停（通过外部控制器）
            if hasattr(self, 'controller') and self.controller and self.controller.paused:
                # 暂停时输出静音
                out_l[produced:] = 0
                out_r[produced:] = 0
                return produced
            
            # 每次读取 chunk_size 个 sample FRAMES (每个 frame 包含 2 个 int16)
            to_read_frames = min((n_out - produced), self.chunk_size)
            
            # 乘以通道数 (2)
            data = self.current_file.read(to_read_frames * 2 * np.dtype(self.dtype).itemsize)
            samples = np.frombuffer(data, dtype=self.dtype)
            
            if len(samples) == 0:
                self.next_file()
                if self.current_file is None:
                     out_l[produced:] = 0
                     out_r[produced:] = 0
                     return produced
                continue
            
            # [Stereo] 归一化并分离声道
            # Reshape 为 (-1, 2)
            # 假如读取到的不是完整的帧（末尾），需要截断到偶数
            if len(samples) % 2 != 0:
                samples = samples[:-1]
                
            frame_count = len(samples) // 2
            stereo_samples = samples.reshape(-1, 2)
            
            float_samples = stereo_samples.astype(np.float32) * self.current_gain
            float_samples = np.clip(float_samples, -32767, 32767)
            int_samples = float_samples.astype(self.dtype)
            
            # 写入输出端口
            out_l[produced:produced+frame_count] = int_samples[:, 0]
            out_r[produced:produced+frame_count] = int_samples[:, 1]
            
            produced += frame_count
            
            if frame_count < to_read_frames:
                self.next_file()
                if self.current_file is None:
                    out_l[produced:] = 0
                    out_r[produced:] = 0
                    return produced
                    
        return produced

    def stop(self):
        if self.current_file is not None:
            self.current_file.close()
            self.current_file = None
        self._cleanup_old_temp()
        return super().stop()

class FM_console(gr.top_block):

    def __init__(self, music_dir, freq, power, shuffle=False):
        gr.top_block.__init__(self, "FM Playlist Transmitter", catch_exceptions=True)
        self.flowgraph_started = threading.Event()
        self.shuffle = shuffle
        self.controller = None  # 播放控制器
        self.display_manager = None  # 显示管理器

        ##################################################
        # Blocks
        ##################################################
        
        # 修复2: 提升WFM的中间正交采样率(quad_rate)。
        # WFM带宽约 200kHz，原 88.2kHz 采样率严重不足，会导致混叠炸音。
        # 这里设为 44100 * 8 = 352800 Hz，足以容纳 WFM 频谱 (或 MPX 频谱)。
        self.audio_rate = 44100
        self.target_quad_rate = 352800
        
        # [Stereo] 立体声参数
        self.tau = 75e-6  # 预加重时间常数 (US: 75us, EU: 50us)
        self.pilot_freq = 19000
        self.subcarrier_freq = 38000
        self.max_dev = 75000 # 75kHz 频偏

        self.rational_resampler_xxx_0 = filter.rational_resampler_ccc(
                interpolation=2000000,
                decimation=self.target_quad_rate, # 对应修改这里，保持匹配
                taps=[],
                fractional_bw=0)

        # 替换 hack 为 hack
        self.osmosdr_sink_0 = osmosdr.sink(
            args="numchan=" + str(1) + " " + 'hackrf,bias_tx=0'
        )

        self.osmosdr_sink_0.set_time_unknown_pps(osmosdr.time_spec_t())
        self.osmosdr_sink_0.set_sample_rate(2000000)
        self.osmosdr_sink_0.set_center_freq(freq, 0)
        self.osmosdr_sink_0.set_freq_corr(0, 0)
        self.osmosdr_sink_0.set_gain(power, 0)
        
        # 修复3: 降低 IF 和 BB 增益。原 40dB 极易导致硬件发射级饱和失真。
        # 建议通过 set_gain (RF Gain) 调节主功率，内部增益保持线性区。
        self.osmosdr_sink_0.set_if_gain(20, 0)
        self.osmosdr_sink_0.set_bb_gain(20, 0)
        
        self.osmosdr_sink_0.set_antenna('', 0)
        self.osmosdr_sink_0.set_bandwidth(0, 0)

        # [Stereo] 左声道 LPF
        # 调整低通滤波器，WFM 广播标准音频带宽通常为 15kHz
        lpf_taps = firdes.low_pass(
                1,
                44100,
                15000,  # 5000 -> 15000 for WFM
                1000,   # Widen transition for smoother rolloff
                window.WIN_HAMMING,
                6.76)
        
        self.low_pass_filter_left = filter.fir_filter_fff(1, lpf_taps)
        # [Stereo] 右声道 LPF
        self.low_pass_filter_right = filter.fir_filter_fff(1, lpf_taps)

        # [Stereo] 预加重 (Pre-emphasis)
        # 修复：移除了导致崩溃的 firdes.fm_deemph 调用，直接使用 analog.fm_preemph
        self.fm_preemph_left = analog.fm_preemph(self.audio_rate, self.tau)
        self.fm_preemph_right = analog.fm_preemph(self.audio_rate, self.tau)

        # [Stereo] 升采样 L/R 到 quad_rate (352.8k) 以便生成 MPX
        self.resampler_left = filter.rational_resampler_fff(
            interpolation=8, decimation=1)
        self.resampler_right = filter.rational_resampler_fff(
            interpolation=8, decimation=1)

        self.blocks_short_to_float_l = blocks.short_to_float(1, 1)
        self.blocks_short_to_float_r = blocks.short_to_float(1, 1)

        # 修复1: 大幅降低进入 FM 调制器的音量。
        # FM 调制包含预加重 (Pre-emphasis)，会大幅提升高频能量。
        # 如果输入接近 1.0，高频部分会严重超标，导致频偏过大和破音。
        # [Stereo] 这里保持原有增益，因为后续手动构建 MPX 仍需控制总幅度
        gain_val = 0.000006
        self.blocks_multiply_const_l = blocks.multiply_const_ff(gain_val)
        self.blocks_multiply_const_r = blocks.multiply_const_ff(gain_val)

        # [Stereo] MPX 编码组件
        # 1. 矩阵
        self.add_sum = blocks.add_ff(1) # L+R
        self.sub_diff = blocks.sub_ff(1) # L-R
        
        # 2. 信号源
        # Pilot 19kHz, amplitude 0.1 (10% modulation)
        self.sig_pilot = analog.sig_source_f(self.target_quad_rate, analog.GR_SIN_WAVE, self.pilot_freq, 0.1, 0)
        # Subcarrier 38kHz, amplitude 1.0 (carrier for DSB-SC)
        self.sig_subcarrier = analog.sig_source_f(self.target_quad_rate, analog.GR_SIN_WAVE, self.subcarrier_freq, 1.0, 0)
        
        # 3. 调制 L-R
        self.mul_mod = blocks.multiply_ff(1)
        
        # 4. 混合 MPX (Sum + Pilot + Modulated_Diff)
        self.add_mpx = blocks.add_ff(1)
        
        # 5. FM Modulator
        # Sensitivity = 2 * pi * max_dev / samp_rate
        self.sensitivity = 2 * math.pi * self.max_dev / self.target_quad_rate
        self.fm_mod = analog.frequency_modulator_fc(self.sensitivity)

        # 使用 WFM 发送模块替换原有的 AM/IQ 注入方式 -> [Stereo] 已替换为 MPX 链
        
        self.playlist_file_source_0 = playlist_file_source(music_dir, repeat=True, chunk_size=4096, shuffle=self.shuffle)
        
        # 创建播放控制器和显示管理器
        self.controller = PlaybackController(self.playlist_file_source_0)
        self.display_manager = DisplayManager(self.controller)
        
        # 将控制器关联到播放源
        self.playlist_file_source_0.controller = self.controller

        ##################################################
        # Connections
        ##################################################
        # 1. 源 -> Float -> Gain
        self.connect((self.playlist_file_source_0, 0), (self.blocks_short_to_float_l, 0))
        self.connect((self.playlist_file_source_0, 1), (self.blocks_short_to_float_r, 0))
        
        self.connect((self.blocks_short_to_float_l, 0), (self.blocks_multiply_const_l, 0))
        self.connect((self.blocks_short_to_float_r, 0), (self.blocks_multiply_const_r, 0))

        # 2. Gain -> Pre-emphasis -> LPF (15k)
        self.connect((self.blocks_multiply_const_l, 0), (self.fm_preemph_left, 0))
        self.connect((self.blocks_multiply_const_r, 0), (self.fm_preemph_right, 0))
        
        self.connect((self.fm_preemph_left, 0), (self.low_pass_filter_left, 0))
        self.connect((self.fm_preemph_right, 0), (self.low_pass_filter_right, 0))

        # 3. LPF -> Resample (44.1k -> 352.8k)
        self.connect((self.low_pass_filter_left, 0), (self.resampler_left, 0))
        self.connect((self.low_pass_filter_right, 0), (self.resampler_right, 0))

        # 4. Stereo Matrix (L+R, L-R)
        self.connect((self.resampler_left, 0), (self.add_sum, 0))
        self.connect((self.resampler_right, 0), (self.add_sum, 1)) # Sum = L+R
        
        self.connect((self.resampler_left, 0), (self.sub_diff, 0))
        self.connect((self.resampler_right, 0), (self.sub_diff, 1)) # Diff = L-R

        # 5. MPX Generation
        # Modulate Diff: (L-R) * 38k
        self.connect((self.sub_diff, 0), (self.mul_mod, 0))
        self.connect((self.sig_subcarrier, 0), (self.mul_mod, 1))
        
        # Sum All: (L+R) + Pilot(19k) + Modulated_Diff
        self.connect((self.add_sum, 0), (self.add_mpx, 0))
        self.connect((self.sig_pilot, 0), (self.add_mpx, 1))
        self.connect((self.mul_mod, 0), (self.add_mpx, 2))

        # 6. FM Modulation -> Resample -> Sink
        # 新的 WFM 连接路径 [Stereo]
        self.connect((self.add_mpx, 0), (self.fm_mod, 0))
        self.connect((self.fm_mod, 0), (self.rational_resampler_xxx_0, 0))
        self.connect((self.rational_resampler_xxx_0, 0), (self.osmosdr_sink_0, 0))

def main(top_block_cls=FM_console, options=None):
    parser = ArgumentParser(description="FM Transmitter with GNU Radio Playlist")
    parser.add_argument('-d', '--dir', type=str, required=True, help="Path to directory containing WAV/MP3/FLAC/OGG files")
    parser.add_argument('-f', '--frequency', type=int, required=True, help="Transmission frequency in Hz")
    parser.add_argument('-g', '--gain', type=int, required=True, help="Transmission power in dB")
    parser.add_argument('-s', '--shuffle', action='store_true', help="Enable shuffle mode for random playback")
    parser.add_argument('--gui', action='store_true', help="Enable GUI mode")
    args = parser.parse_args()

    tb = top_block_cls(music_dir=args.dir, freq=args.frequency, power=args.gain, shuffle=args.shuffle)

    def sig_handler(sig=None, frame=None):
        if tb.display_manager:
            tb.display_manager.stop()
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    tb.start()
    tb.flowgraph_started.set()
    
    # 如果启用GUI模式
    if args.gui:
        app = QApplication(sys.argv)
        gui = FMApplicationGUI()
        
        # 连接FM控制台到GUI
        gui.fm_console = tb
        gui.controller = tb.controller
        
        # 设置GUI的播放控制器
        if tb.controller:
            tb.controller.gui = gui
        
        # 运行GUI
        gui.run()
        
        # PyQt5事件循环
        try:
            sys.exit(app.exec_())
        except KeyboardInterrupt:
            pass
        finally:
            # 清理退出
            if tb.display_manager:
                tb.display_manager.stop()
            tb.stop()
            tb.wait()
    else:
        # 启动显示管理器（终端模式）
        if tb.display_manager:
            tb.display_manager.start()

        try:
            # 等待显示管理器停止（用户按q键退出）
            if tb.display_manager:
                while tb.display_manager.running:
                    time.sleep(0.5)
            else:
                input('Press Enter to quit: ')
        except KeyboardInterrupt:
            pass
        except EOFError:
            pass

        # 清理退出
        if tb.display_manager:
            tb.display_manager.stop()
        tb.stop()
        tb.wait()

if __name__ == '__main__':
    main()
