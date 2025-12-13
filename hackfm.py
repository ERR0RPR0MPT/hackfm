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

class playlist_file_source(gr.sync_block):
    """
    Stream PCM data from all WAV/MP3 files in a directory (playlist) sequentially as shorts.
    For MP3 (and for WAVs with incorrect sample rate), auto-convert on the fly into WAV with 44100Hz, original channels/sample format.
    Uses ./temp folder for transient data. Does not downmix or change bit depth.
    Loops through all files repeatedly if repeat=True.
    音频自动归一化到满幅（-1~1区间），避免响度太小或炸音
    """
    def __init__(self, dir_path, repeat=True, dtype=np.int16, chunk_size=4096, target_headroom=0.98):
        gr.sync_block.__init__(self,
            name="playlist_file_source",
            in_sig=None,
            out_sig=[np.int16])
        self.dir_path = dir_path
        self.repeat = repeat
        self.dtype = dtype
        self.chunk_size = chunk_size
        self.target_headroom = target_headroom  # 保证最大值不会爆 1.0，防削波，典型取0.98~0.99
        self.temp_dir = os.path.abspath('./temp')
        os.makedirs(self.temp_dir, exist_ok=True)
        self._clear_temp_dir()
        self.file_list = self._find_audio_files()
        if not self.file_list:
            raise RuntimeError(f"No audio files found in {self.dir_path}")
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
        # 如果不是 44100Hz 或者不是单声道(ch!=1)，则需要转换
        if sr != 44100 or ch != 1:
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
            '-ac', '1',  # 关键修复：强制转换为单声道 (-ac 1)
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
        
        # 检查是否需要转换（MP3/FLAC/OGG, 错误的采样率, 或立体声）
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
        out = output_items[0]
        produced = 0
        
        while produced < len(out):
            if self.current_file is None:
                out[produced:] = 0
                return produced
            
            to_read = min((len(out)-produced), self.chunk_size)
            data = self.current_file.read(to_read * np.dtype(self.dtype).itemsize)
            samples = np.frombuffer(data, dtype=self.dtype)
            
            if len(samples) == 0:
                self.next_file()
                if self.current_file is None:
                     out[produced:] = 0
                     return produced
                continue

            # 归一化提升响度，防炸音
            float_samples = samples.astype(np.float32) * self.current_gain
            # 限幅，防止极端取样溢出
            float_samples = np.clip(float_samples, -32767, 32767)
            
            int_samples = float_samples.astype(self.dtype)
            out[produced:produced+len(int_samples)] = int_samples
            produced += len(int_samples)
            
            if len(samples) < to_read:
                self.next_file()
                if self.current_file is None:
                    out[produced:] = 0
                    return produced
                    
        return produced

    def stop(self):
        if self.current_file is not None:
            self.current_file.close()
            self.current_file = None
        self._cleanup_old_temp()
        return super().stop()

class FM_console(gr.top_block):
    def __init__(self, music_dir, freq, power):
        gr.top_block.__init__(self, "FM Playlist Transmitter", catch_exceptions=True)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Blocks
        ##################################################
        
        # 修复2: 提升WFM的中间正交采样率(quad_rate)。
        # WFM带宽约 200kHz，原 88.2kHz 采样率严重不足，会导致混叠炸音。
        # 这里设为 44100 * 8 = 352800 Hz，足以容纳 WFM 频谱。
        self.target_quad_rate = 352800
        
        self.rational_resampler_xxx_0 = filter.rational_resampler_ccc(
                interpolation=2000000,
                decimation=self.target_quad_rate, # 对应修改这里，保持匹配
                taps=[],
                fractional_bw=0)

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

        # 调整低通滤波器，WFM 广播标准音频带宽通常为 15kHz
        self.low_pass_filter_0 = filter.fir_filter_fff(
            1,
            firdes.low_pass(
                1,
                44100,
                15000,  # 5000 -> 15000 for WFM
                1000,   # Widen transition for smoother rolloff
                window.WIN_HAMMING,
                6.76))

        self.blocks_short_to_float_0 = blocks.short_to_float(1, 1)

        # 修复1: 大幅降低进入 FM 调制器的音量。
        # FM 调制包含预加重 (Pre-emphasis)，会大幅提升高频能量。
        # 如果输入接近 1.0，高频部分会严重超标，导致频偏过大和破音。
        self.blocks_multiply_const_vxx_0 = blocks.multiply_const_ff(0.000006)

        # 使用 WFM 发送模块替换原有的 AM/IQ 注入方式
        # audio_rate: 44.1k
        # quad_rate: 352.8k (44.1k * 8) - 修复采样率问题
        self.analog_wfm_tx_0 = analog.wfm_tx(
            audio_rate=44100,
            quad_rate=self.target_quad_rate,
            tau=75e-6,
            max_dev=75000,
            # fh=-1.0, # Removed: fh argument is often not supported in standard wfm_tx python bindings
        )

        self.playlist_file_source_0 = playlist_file_source(music_dir, repeat=True, chunk_size=4096)

        ##################################################
        # Connections
        ##################################################
        self.connect((self.playlist_file_source_0, 0), (self.blocks_short_to_float_0, 0))
        self.connect((self.blocks_short_to_float_0, 0), (self.blocks_multiply_const_vxx_0, 0))
        self.connect((self.blocks_multiply_const_vxx_0, 0), (self.low_pass_filter_0, 0))
        # 新的 WFM 连接路径
        self.connect((self.low_pass_filter_0, 0), (self.analog_wfm_tx_0, 0))
        self.connect((self.analog_wfm_tx_0, 0), (self.rational_resampler_xxx_0, 0))
        self.connect((self.rational_resampler_xxx_0, 0), (self.osmosdr_sink_0, 0))

def main(top_block_cls=FM_console, options=None):
    parser = ArgumentParser(description="FM Transmitter with GNU Radio Playlist")
    parser.add_argument('-d', '--dir', type=str, required=True, help="Path to directory containing WAV/MP3/FLAC/OGG files")
    parser.add_argument('-f', '--frequency', type=int, required=True, help="Transmission frequency in Hz")
    parser.add_argument('-g', '--gain', type=int, required=True, help="Transmission power in dB")
    args = parser.parse_args()

    tb = top_block_cls(music_dir=args.dir, freq=args.frequency, power=args.gain)

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    tb.start()
    tb.flowgraph_started.set()
    try:
        input('Press Enter to quit: ')
    except EOFError:
        pass
    tb.stop()
    tb.wait()

if __name__ == '__main__':
    main()
