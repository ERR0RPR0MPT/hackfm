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
    args = parser.parse_args()

    tb = top_block_cls(music_dir=args.dir, freq=args.frequency, power=args.gain, shuffle=args.shuffle)

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
