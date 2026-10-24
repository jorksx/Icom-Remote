"""
APRS (Automatic Packet Reporting System) Engine for ICOM Remote.
Includes:
- Dual Demodulation Engine:
  1. High-Performance Dire Wolf Integration via KISS TCP (using bundled or local direwolf.exe)
  2. Pure Python Multi-Slicer Bell 202 AFSK Demodulator with Dire Wolf AGC algorithm as fallback
- High-Fidelity 48 kHz Bell 202 AFSK Modulator (continuous phase, exact integer symbol timing, anti-clipping amplitude)
- AX.25 Frame Encoder and Decoder (CRC-16 CCITT, AX.25 v2.0 C-bit compliance, NRZI, bit stuffing/unstuffing)
- New-N APRS Digipeater with duplicate suppression and hop decrementing
- Station list tracking, distance/bearing calculations, and Maidenhead Grid conversion
- Position beacon generator with automatic and manual beaconing
"""

import time
import math
import hashlib
import threading
import logging
import json
import urllib.request
import os
import sys
import socket
import subprocess
import tempfile
import wave
import atexit
import re
from typing import Callable, Optional, Dict, Any, List, Tuple
import numpy as np

try:
    import aprslib
except ImportError:
    aprslib = None


# ============================================================================
# AX.25 CRC-16 CCITT & Callsign Encoding
# ============================================================================

def crc16_ccitt(data: bytes) -> int:
    """CRC-16 CCITT for AX.25 (reversed polynomial 0x8408)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc ^ 0xFFFF


def encode_callsign(call: str, is_last: bool = False, has_repeated: bool = False, is_digi: bool = False) -> bytes:
    """
    Encodes an amateur radio callsign with optional SSID for AX.25 address fields.
    According to AX.25 v2.2 specification / Dire Wolf:
    - Destination SSID has Command/Response bit 7 = 1 (0xE0) for UI frames.
    - Source SSID has Command/Response bit 7 = 1 (0xE0) for UI frames.
    - Digipeater SSIDs have H-bit 7 = 1 (0x80) when has-been-repeated, 0x60 when not.
    - Bit 0 is the HDLC address extension bit (1 if last address, 0 otherwise).
    """
    call = call.upper().strip()
    if '-' in call:
        base, ssid_s = call.split('-', 1)
        try:
            ssid = int(ssid_s)
        except Exception:
            ssid = 0
    else:
        base = call
        ssid = 0

    base = base.ljust(6)[:6]
    res = bytearray()
    for char in base:
        res.append((ord(char) << 1) & 0xFE)

    if is_digi:
        base_ssid = 0x60
        if has_repeated:
            base_ssid |= 0x80  # H-bit (has-been-digipeated)
    else:
        base_ssid = 0xE0

    ssid_byte = base_ssid | ((ssid & 0x0F) << 1)
    if is_last:
        ssid_byte |= 0x01  # Extension bit
    res.append(ssid_byte)
    return bytes(res)


def parse_callsign(addr_bytes: bytes) -> Tuple[str, bool, bool]:
    """Parses a 7-byte AX.25 address field into (callsign-ssid, h_bit, is_last)."""
    call = ''.join(chr((b >> 1) & 0x7F) for b in addr_bytes[:6]).strip()
    ssid_byte = addr_bytes[6]
    ssid = (ssid_byte >> 1) & 0x0F
    h_bit = bool(ssid_byte & 0x80)
    is_last = bool(ssid_byte & 0x01)
    call_str = f"{call}-{ssid}" if ssid != 0 else call
    return call_str, h_bit, is_last


def make_ax25_frame(dest: str, src: str, path: List[str], payload: str) -> bytes:
    """Constructs a complete AX.25 UI frame with FCS (CRC) bytes appended."""
    frame = bytearray()
    is_last = (len(path) == 0)
    frame.extend(encode_callsign(dest, is_last=False, is_digi=False))
    frame.extend(encode_callsign(src, is_last=is_last, is_digi=False))
    for i, p in enumerate(path):
        is_last = (i == len(path) - 1)
        has_star = p.endswith('*')
        clean_p = p[:-1] if has_star else p
        frame.extend(encode_callsign(clean_p, is_last=is_last, has_repeated=has_star, is_digi=True))
    frame.append(0x03)  # UI frame (Unnumbered Information)
    frame.append(0xF0)  # No layer 3 protocol
    frame.extend(payload.encode('latin-1', errors='replace'))
    
    # Calculate and append CRC-16 CCITT (little-endian)
    crc = crc16_ccitt(bytes(frame))
    frame.append(crc & 0xFF)
    frame.append((crc >> 8) & 0xFF)
    return bytes(frame)


def parse_ax25_frame(frame: bytes, has_crc: bool = False) -> Optional[Tuple[str, str, List[str], str]]:
    """
    Parses an AX.25 UI frame into (dest, src, path, payload).
    has_crc: True if frame includes the trailing 2-byte FCS (e.g. from internal demod),
             False if frame is from KISS (where Dire Wolf already stripped the CRC).
    """
    min_len = 16 if has_crc else 14
    if len(frame) < min_len:
        return None
    dest, _, _ = parse_callsign(frame[0:7])
    src, _, is_last = parse_callsign(frame[7:14])
    idx = 14
    path = []
    end_limit = len(frame) - (4 if has_crc else 2)
    while not is_last and idx + 7 <= end_limit:
        call, h_bit, is_last = parse_callsign(frame[idx:idx+7])
        path.append(f"{call}*" if h_bit else call)
        idx += 7
    if idx + 2 > len(frame):
        return None
    ctrl = frame[idx]
    pid = frame[idx+1]
    
    if has_crc:
        payload_bytes = frame[idx+2:-2]
    else:
        payload_bytes = frame[idx+2:]
        
    payload = payload_bytes.decode('latin-1', errors='replace')
    return dest, src, path, payload


# ============================================================================
# Bell 202 1200 Baud AFSK Modulator & Bit Serializer
# ============================================================================

def frame_to_bits(frame: bytes, pre_flags: int = 52, post_flags: int = 8) -> List[int]:
    """
    Converts AX.25 frame bytes into bit-stuffed NRZI audio levels.
    pre_flags: Number of preamble flag bytes (0x7E). Default 52 flags = ~347 ms delay.
    post_flags: Number of postamble flag bytes (0x7E). Default 8 flags = ~53 ms.
    """
    FLAG = 0x7E  # 01111110
    
    raw_bits = []
    ones_count = 0
    for b in frame:
        for bit_idx in range(8):  # LSB first
            bit = (b >> bit_idx) & 1
            raw_bits.append(bit)
            if bit == 1:
                ones_count += 1
                if ones_count == 5:
                    raw_bits.append(0)  # Stuffed 0
                    ones_count = 0
            else:
                ones_count = 0
                
    flag_bits = []
    for _ in range(pre_flags):
        for bit_idx in range(8):
            flag_bits.append((FLAG >> bit_idx) & 1)
            
    post_bits = []
    for _ in range(post_flags):
        for bit_idx in range(8):
            post_bits.append((FLAG >> bit_idx) & 1)
            
    all_bits = flag_bits + raw_bits + post_bits
    
    # NRZI encoding: 0 causes transition, 1 causes no transition
    nrzi_levels = []
    current_level = 1
    for b in all_bits:
        if b == 0:
            current_level = 1 - current_level
        nrzi_levels.append(current_level)
        
    return nrzi_levels


def modulate_afsk(nrzi_levels: List[int], sample_rate: int = 48000, baud: int = 1200, amplitude: float = 0.50) -> np.ndarray:
    """
    Generates high-fidelity continuous-phase Bell 202 AFSK audio samples.
    - Uses exact 48000 Hz sample rate (40.0 integer samples per bit) for ZERO symbol jitter.
    - Mark tone = 1200 Hz, Space tone = 2200 Hz.
    - Amplitude 0.38 (-8.5 dBFS) prevents FM transmitter audio clipping and harmonic splatter.
    """
    samps_per_bit = int(round(sample_rate / baud))  # exactly 40 samples at 48000 Hz
    total_samples = len(nrzi_levels) * samps_per_bit
    audio = np.zeros(total_samples, dtype=np.float32)
    
    two_pi = 2.0 * math.pi
    dphi_mark = two_pi * 1200.0 / sample_rate
    dphi_space = two_pi * 2200.0 / sample_rate
    
    phase = 0.0
    idx = 0
    for level in nrzi_levels:
        dphi = dphi_mark if level == 0 else dphi_space
        for _ in range(samps_per_bit):
            phase = (phase + dphi) % two_pi
            audio[idx] = math.sin(phase) * amplitude
            idx += 1
            
    return audio


# ============================================================================
# Streaming Multi-Slicer Pure-Python AFSK Demodulator (Fallback Engine)
# ============================================================================

class StreamingAfskDemod:
    """
    Streaming 1200 baud Bell 202 AFSK Demodulator with Dire Wolf AGC & Multi-Slicers.
    Operates on incoming audio chunks (e.g. 1024 float32 samples at 16000 or 48000 Hz).
    Uses dual-tone I/Q matched envelope detector + dynamic AGC + multi-threshold DPLL slicers.
    """
    def __init__(self, sample_rate: int = 16000, baud: int = 1200, offsets: Tuple[float, ...] = (0.0, -0.12, 0.12)):
        self.fs = sample_rate
        self.baud = baud
        self.dphase = baud / sample_rate
        self.offsets = offsets
        
        # Symbol filter window
        self.N = max(4, int(round(sample_rate / baud)))
        self.kernel = np.ones(self.N, dtype=np.float32) / self.N
        
        # LO phase tracking
        self.phase_m = 0.0
        self.phase_s = 0.0
        self.dphi_m = 2.0 * math.pi * 1200.0 / sample_rate
        self.dphi_s = 2.0 * math.pi * 2200.0 / sample_rate
        
        # Overlap buffers for continuous convolution
        self.prev_im = np.zeros(self.N - 1, dtype=np.float32)
        self.prev_qm = np.zeros(self.N - 1, dtype=np.float32)
        self.prev_is = np.zeros(self.N - 1, dtype=np.float32)
        self.prev_qs = np.zeros(self.N - 1, dtype=np.float32)
        
        # Dire Wolf AGC envelope trackers
        self.m_peak = 0.1
        self.m_valley = 0.0
        self.s_peak = 0.1
        self.s_valley = 0.0
        self.fast_attack = 0.70
        self.slow_decay = 0.000100
        
        # Parallel slicers
        self.slicers = []
        for off in self.offsets:
            self.slicers.append({
                'offset': off,
                'phase': 0.0,
                'prev_d': 0.0,
                'prev_lvl': 1,
                'shift_reg': 0,
                'in_frame': False,
                'ones_cnt': 0,
                'cur_byte': 0,
                'bit_in_byte': 0,
                'byte_buf': bytearray()
            })

    def reset(self):
        self.prev_im.fill(0)
        self.prev_qm.fill(0)
        self.prev_is.fill(0)
        self.prev_qs.fill(0)
        self.m_peak = 0.1
        self.m_valley = 0.0
        self.s_peak = 0.1
        self.s_valley = 0.0
        for sl in self.slicers:
            sl['phase'] = 0.0
            sl['prev_d'] = 0.0
            sl['prev_lvl'] = 1
            sl['shift_reg'] = 0
            sl['in_frame'] = False
            sl['ones_cnt'] = 0
            sl['cur_byte'] = 0
            sl['bit_in_byte'] = 0
            sl['byte_buf'] = bytearray()

    def _agc_sample(self, in_val: float, peak: float, valley: float) -> Tuple[float, float, float]:
        """Dire Wolf AGC IIR envelope follower."""
        if in_val >= peak:
            peak = in_val * self.fast_attack + peak * (1.0 - self.fast_attack)
        else:
            peak = in_val * self.slow_decay + peak * (1.0 - self.slow_decay)
            
        if in_val <= valley:
            valley = in_val * self.fast_attack + valley * (1.0 - self.fast_attack)
        else:
            valley = in_val * self.slow_decay + valley * (1.0 - self.slow_decay)
            
        diff = peak - valley
        if diff > 1e-6:
            xc = max(valley, min(peak, in_val))
            norm = (xc - 0.5 * (peak + valley)) / diff
            return norm, peak, valley
        return 0.0, peak, valley

    def process_chunk(self, chunk: np.ndarray) -> List[bytes]:
        new_frames = []
        n_samples = len(chunk)
        if n_samples == 0:
            return new_frames
            
        # Continuous phase LO angles (prevents precision loss over time)
        idx = np.arange(n_samples, dtype=np.float32)
        two_pi = 2.0 * math.pi
        angles_m = (self.phase_m + idx * self.dphi_m) % two_pi
        angles_s = (self.phase_s + idx * self.dphi_s) % two_pi
        self.phase_m = (self.phase_m + n_samples * self.dphi_m) % two_pi
        self.phase_s = (self.phase_s + n_samples * self.dphi_s) % two_pi
        
        cos_m = np.cos(angles_m)
        sin_m = np.sin(angles_m)
        cos_s = np.cos(angles_s)
        sin_s = np.sin(angles_s)
        
        im_cur = chunk * cos_m
        qm_cur = chunk * sin_m
        is_cur = chunk * cos_s
        qs_cur = chunk * sin_s
        
        im_full = np.concatenate([self.prev_im, im_cur])
        qm_full = np.concatenate([self.prev_qm, qm_cur])
        is_full = np.concatenate([self.prev_is, is_cur])
        qs_full = np.concatenate([self.prev_qs, qs_cur])
        
        self.prev_im = im_full[-len(self.prev_im):]
        self.prev_qm = qm_full[-len(self.prev_qm):]
        self.prev_is = is_full[-len(self.prev_is):]
        self.prev_qs = qs_full[-len(self.prev_qs):]
        
        im_f = np.convolve(im_full, self.kernel, mode='valid')
        qm_f = np.convolve(qm_full, self.kernel, mode='valid')
        is_f = np.convolve(is_full, self.kernel, mode='valid')
        qs_f = np.convolve(qs_full, self.kernel, mode='valid')
        
        m_amp = np.sqrt(im_f**2 + qm_f**2)
        s_amp = np.sqrt(is_f**2 + qs_f**2)
        
        dphase = self.dphase
        for k in range(n_samples):
            m_a = m_amp[k]
            s_a = s_amp[k]
            m_norm, self.m_peak, self.m_valley = self._agc_sample(m_a, self.m_peak, self.m_valley)
            s_norm, self.s_peak, self.s_valley = self._agc_sample(s_a, self.s_peak, self.s_valley)
            discrim_val = m_norm - s_norm
            
            for sl in self.slicers:
                cur_d = discrim_val - sl['offset']
                prev_d = sl['prev_d']
                phase = sl['phase']
                
                # Zero-crossing nudge
                if (prev_d < 0 and cur_d >= 0) or (prev_d >= 0 and cur_d < 0):
                    if phase < 0.5:
                        phase -= phase * 0.25
                    else:
                        phase += (1.0 - phase) * 0.25
                        
                phase_next = phase + dphase
                if phase < 0.5 and phase_next >= 0.5:
                    cur_lvl = 1 if cur_d > 0 else 0
                    bit = 0 if cur_lvl != sl['prev_lvl'] else 1
                    sl['prev_lvl'] = cur_lvl
                    
                    sl['shift_reg'] = ((sl['shift_reg'] >> 1) | (bit << 7)) & 0xFF
                    if sl['shift_reg'] == 0x7E:
                        if sl['in_frame']:
                            if len(sl['byte_buf']) >= 16:
                                payload = bytes(sl['byte_buf'][:-2])
                                fcs = bytes(sl['byte_buf'][-2:])
                                exp_crc = fcs[0] | (fcs[1] << 8)
                                if crc16_ccitt(payload) == exp_crc:
                                    # Output payload frame without CRC bytes
                                    if payload not in new_frames:
                                        new_frames.append(payload)
                            sl['byte_buf'] = bytearray()
                            sl['cur_byte'] = 0
                            sl['bit_in_byte'] = 0
                            sl['ones_cnt'] = 0
                        sl['in_frame'] = True
                        sl['ones_cnt'] = 0
                    elif sl['in_frame']:
                        if bit == 1:
                            sl['ones_cnt'] += 1
                            if sl['ones_cnt'] >= 7:
                                sl['in_frame'] = False
                                sl['byte_buf'] = bytearray()
                            else:
                                sl['cur_byte'] |= (1 << sl['bit_in_byte'])
                                sl['bit_in_byte'] += 1
                        else:
                            if sl['ones_cnt'] == 5:
                                sl['ones_cnt'] = 0  # discard stuffed zero
                            else:
                                sl['ones_cnt'] = 0
                                sl['bit_in_byte'] += 1
                                
                        if sl['bit_in_byte'] == 8:
                            sl['byte_buf'].append(sl['cur_byte'])
                            sl['cur_byte'] = 0
                            sl['bit_in_byte'] = 0
                            
                sl['phase'] = phase_next - 1.0 if phase_next >= 1.0 else phase_next
                sl['prev_d'] = cur_d
                
        return new_frames


# ============================================================================
# Dire Wolf Process & KISS Client Manager
# ============================================================================

def find_gen_packets_binary() -> Optional[str]:
    """Finds the gen_packets.exe binary on the system."""
    base_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(base_dir, "gen_packets.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen_packets.exe"),
        os.path.join(os.path.expanduser("~"), "OneDrive", "APRS", "gen_packets.exe"),
        os.path.join(os.path.expanduser("~"), "Desktop", "wfweb", "wfweb-release", "gen_packets.exe"),
        os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "Dire Wolf", "gen_packets.exe")
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    import shutil
    from_path = shutil.which("gen_packets")
    if from_path:
        return from_path
    return None


def generate_with_gen_packets(tnc2_str: str, sample_rate: int = 48000, amplitude: int = 200) -> Optional[np.ndarray]:
    """Generates standard Bell 202 AFSK audio using Dire Wolf gen_packets.exe."""
    exe = find_gen_packets_binary()
    if not exe:
        return None
    tmp_wav = os.path.join(tempfile.gettempdir(), f"aprs_tx_{os.getpid()}_{int(1000*time.time())}.wav")
    cmd = [exe, "-a", str(amplitude), "-r", str(sample_rate), "-o", tmp_wav, "-"]
    creationflags = 0x08000000 if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags
        )
        input_data = tnc2_str.strip().encode('latin-1')
        proc.communicate(input=input_data, timeout=3.0)
        if proc.returncode == 0 and os.path.exists(tmp_wav):
            with wave.open(tmp_wav, 'rb') as wf:
                n = wf.getnframes()
                raw_bytes = wf.readframes(n)
                audio = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                # Strip leading silence if any so tones start cleanly
                nonzero = np.nonzero(audio)[0]
                if len(nonzero) > 0:
                    audio = audio[nonzero[0]:]
                return audio
    except Exception as e:
        logging.warning(f"gen_packets execution error: {e}")
    finally:
        if os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except Exception:
                pass
    return None


def find_direwolf_binary() -> Optional[str]:
    """Finds the direwolf.exe binary on the system."""
    base_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(base_dir, "direwolf.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "direwolf.exe"),
        os.path.join(os.path.expanduser("~"), "OneDrive", "APRS", "direwolf.exe"),
        os.path.join(os.path.expanduser("~"), "Desktop", "wfweb", "wfweb-release", "direwolf.exe"),
        os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "Dire Wolf", "direwolf.exe")
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
            
    # Try PATH
    import shutil
    from_path = shutil.which("direwolf")
    if from_path:
        return from_path
        
    return None


class DirewolfManager:
    """
    Manages Dire Wolf running as a high-performance background TNC engine.
    Connects to Dire Wolf via KISS TCP socket (port 8001), streaming decoded AX.25 frames.
    """
    def __init__(self, on_frame_callback: Callable[[bytes], None]):
        self.on_frame_callback = on_frame_callback
        self.process: Optional[subprocess.Popen] = None
        self.kiss_thread: Optional[threading.Thread] = None
        self.running = False
        self.port = 8001
        self.conf_path = ""
        self.active_dev_name = ""
        self.connected = False
        atexit.register(self.stop)

    def is_port_in_use(self, port: int) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect(('127.0.0.1', port))
            s.close()
            return True
        except Exception:
            return False

    def start(self, rx_dev_name: str, my_call: str, port: int = 8001) -> bool:
        self.stop()
        dw_exe = find_direwolf_binary()
        if not dw_exe:
            logging.warning("[DirewolfManager] direwolf.exe not found on system; falling back to internal DSP demodulator.")
            return False

        self.port = port
        self.active_dev_name = rx_dev_name

        # If port 8001 is already accepting connections (e.g. standalone Dire Wolf already running),
        # connect directly without spawning a duplicate process!
        if self.is_port_in_use(self.port):
            logging.info(f"[DirewolfManager] KISS port {self.port} already active; attaching directly.")
            self.running = True
            self.kiss_thread = threading.Thread(target=self._kiss_loop, daemon=True, name="direwolf-kiss-rx")
            self.kiss_thread.start()
            return True

        # Clean device name substring for Dire Wolf ADEVICE line
        dev_match = rx_dev_name.strip()
        if "Icom 9700" in dev_match or "IC-9700" in dev_match:
            dev_match = "Icom 9700"
        elif "Icom 7300" in dev_match or "IC-7300" in dev_match:
            dev_match = "IC-7300"
        elif "(" in dev_match:
            dev_match = dev_match.split("(")[0].strip()

        self.conf_path = os.path.join(tempfile.gettempdir(), f"direwolf_icom_{os.getpid()}.conf")
        conf_content = (
            f'ADEVICE "{dev_match}" null\n'
            f'CHANNEL 0\n'
            f'MYCALL {my_call}\n'
            f'MODEM 1200\n'
            f'AGWPORT 0\n'
            f'KISSPORT {self.port}\n'
        )
        try:
            with open(self.conf_path, "w", encoding="utf-8") as f:
                f.write(conf_content)
        except Exception as e:
            logging.error(f"[DirewolfManager] Failed to write config: {e}")
            return False

        cmd = [dw_exe, "-c", self.conf_path, "-q", "h", "-t", "0"]
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            logging.info(f"[DirewolfManager] Started direwolf (PID {self.process.pid}) on KISS port {self.port} for audio device '{dev_match}'")
        except Exception as e:
            logging.error(f"[DirewolfManager] Failed to launch direwolf: {e}")
            return False

        self.running = True
        self.kiss_thread = threading.Thread(target=self._kiss_loop, daemon=True, name="direwolf-kiss-rx")
        self.kiss_thread.start()
        return True

    def stop(self):
        self.running = False
        self.connected = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=1.5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
        if self.conf_path and os.path.exists(self.conf_path):
            try:
                os.remove(self.conf_path)
            except Exception:
                pass
            self.conf_path = ""

    def _kiss_loop(self):
        time.sleep(1.0)
        while self.running:
            s = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(4.0)
                s.connect(('127.0.0.1', self.port))
                s.settimeout(None)
                self.connected = True
                logging.info(f"[DirewolfManager] Connected to KISS TCP port {self.port}")
                
                in_frame = False
                escape = False
                frame = bytearray()
                
                while self.running:
                    data = s.recv(4096)
                    if not data:
                        break
                    for b in data:
                        if b == 0xC0:  # FEND
                            if in_frame and len(frame) > 1:
                                cmd = frame[0] & 0x0F
                                if cmd == 0:  # Data frame on channel 0
                                    raw_ax25 = bytes(frame[1:])
                                    try:
                                        self.on_frame_callback(raw_ax25)
                                    except Exception as e:
                                        logging.error(f"[DirewolfManager] Error in frame callback: {e}")
                            frame = bytearray()
                            in_frame = True
                            escape = False
                        elif in_frame:
                            if escape:
                                if b == 0xDC:
                                    frame.append(0xC0)
                                elif b == 0xDD:
                                    frame.append(0xDB)
                                else:
                                    frame.append(b)
                                escape = False
                            elif b == 0xDB:  # FESC
                                escape = True
                            else:
                                frame.append(b)
            except Exception as e:
                self.connected = False
                if self.running:
                    time.sleep(2.0)
            finally:
                self.connected = False
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass


# ============================================================================
# Geographic Helpers (Maidenhead Grid, Haversine, Bearing)
# ============================================================================

def latlon_to_grid(lat: float, lon: float) -> str:
    """Converts latitude and longitude into 6-character Maidenhead locator (e.g. KP20le)."""
    try:
        lon_c = lon + 180.0
        lat_c = lat + 90.0
        f1 = chr(ord('A') + int(lon_c / 20.0))
        f2 = chr(ord('A') + int(lat_c / 10.0))
        lon_rem = lon_c % 20.0
        lat_rem = lat_c % 10.0
        d1 = str(int(lon_rem / 2.0))
        d2 = str(int(lat_rem / 1.0))
        lon_sub = (lon_rem % 2.0) * 12.0
        lat_sub = (lat_rem % 1.0) * 24.0
        s1 = chr(ord('a') + int(lon_sub))
        s2 = chr(ord('a') + int(lat_sub))
        return f"{f1}{f2}{d1}{d2}{s1}{s2}"
    except Exception:
        return ""


def grid_to_latlon(grid: str) -> Optional[Tuple[float, float]]:
    """Converts Maidenhead locator (4 or 6 chars) to approximate center latitude and longitude."""
    try:
        g = grid.strip().upper()
        if len(g) < 4:
            return None
        f1 = ord(g[0]) - ord('A')
        f2 = ord(g[1]) - ord('A')
        d1 = int(g[2])
        d2 = int(g[3])
        s1, s2 = 0.5, 0.5
        if len(g) >= 6:
            s1 = (ord(g[4].lower()) - ord('a') + 0.5) / 24.0
            s2 = (ord(g[5].lower()) - ord('a') + 0.5) / 24.0
        lon = -180.0 + f1 * 20.0 + d1 * 2.0 + s1 * 2.0
        lat = -90.0 + f2 * 10.0 + d2 * 1.0 + s2 * 1.0
        return round(lat, 5), round(lon, 5)
    except Exception:
        return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates great-circle distance between two points on Earth in kilometers."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2.0) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return round(R * c, 1)


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[int, str]:
    """Calculates forward azimuth bearing in degrees (0-359) and 16-point cardinal direction."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    brg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    deg = int(round(brg)) % 360
    cardinals = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                 "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    card = cardinals[int((deg + 11.25) / 22.5) % 16]
    return deg, card


def format_aprs_pos(lat: float, lon: float, sym_table: str = '/', sym: str = '#', comment: str = '') -> str:
    """
    Formats latitude and longitude in standard uncompressed APRS format:
    e.g. '!6115.40N/02315.00E#Icom Remote APRS Digipeater'
    """
    lat_deg = int(abs(lat))
    lat_min = (abs(lat) - lat_deg) * 60.0
    lat_dir = 'N' if lat >= 0 else 'S'
    lat_str = f"{lat_deg:02d}{lat_min:05.2f}{lat_dir}"
    
    lon_deg = int(abs(lon))
    lon_min = (abs(lon) - lon_deg) * 60.0
    lon_dir = 'E' if lon >= 0 else 'W'
    lon_str = f"{lon_deg:03d}{lon_min:05.2f}{lon_dir}"
    
    table = sym_table if sym_table in ['/', '\\'] else '/'
    s_char = sym if len(sym) == 1 else '#'
    return f"!{lat_str}{table}{lon_str}{s_char}{comment}"


# ============================================================================


def fetch_open_meteo_weather(lat: float, lon: float, timeout: float = 6.0) -> Optional[Dict[str, Any]]:
    """
    Fetches real-time weather metrics from Open-Meteo API.
    Zero API key required, 100% free and reliable.
    """
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={lat:.4f}&longitude={lon:.4f}&"
            f"current=temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m,wind_gusts_10m"
        )
        req = urllib.request.Request(url, headers={'User-Agent': 'IcomRemoteAPRS/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        cur = data.get("current", {})
        return {
            "temp_c": float(cur.get("temperature_2m", 0.0)),
            "humidity": float(cur.get("relative_humidity_2m", 50.0)),
            "pressure": float(cur.get("surface_pressure", 1013.2)),
            "wind_kmh": float(cur.get("wind_speed_10m", 0.0)),
            "wind_deg": float(cur.get("wind_direction_10m", 0.0)),
            "gust_kmh": float(cur.get("wind_gusts_10m", 0.0)),
            "timestamp": time.time(),
        }
    except Exception as e:
        logging.warning(f"[APRS WX] Error fetching Open-Meteo weather: {e}")
        return None


def format_aprs_wx(temp_c: float, hum_pct: float, pres_hpa: float,
                   wind_deg: Optional[float] = None, wind_kmh: Optional[float] = None,
                   gust_kmh: Optional[float] = None) -> str:
    """
    Encodes weather metrics into APRS standard raw weather data format.
    Temp in Fahrenheit (tNNN), Humidity (hNN), Barometer in tenths of hPa (bNNNNN),
    Wind direction (cNNN), Wind speed sustained in mph (sNNN), Gust in mph (gNNN).
    """
    temp_f = int(round(temp_c * 1.8 + 32))
    if temp_f < 0:
        t_str = f"t-{abs(temp_f):02d}"
    else:
        t_str = f"t{temp_f:03d}"

    hum = int(round(hum_pct))
    h_str = "h00" if hum >= 100 else f"h{max(1, min(99, hum)):02d}"

    b_val = int(round(pres_hpa * 10))
    b_str = f"b{b_val:05d}"

    if wind_deg is not None and wind_kmh is not None:
        w_deg = int(round(wind_deg)) % 360
        w_spd = int(round(wind_kmh * 0.621371))
        c_str = f"c{w_deg:03d}"
        s_str = f"s{w_spd:03d}"
    else:
        c_str = "c..."
        s_str = "s..."

    if gust_kmh is not None:
        g_spd = int(round(gust_kmh * 0.621371))
        g_str = f"g{g_spd:03d}"
    else:
        g_str = "g..."

    return f"{c_str}{s_str}{g_str}{t_str}r...p...P...{h_str}{b_str}"


def format_aprs_wx_pos(lat: float, lon: float, wx_data: Dict[str, Any], comment: str = '') -> str:
    lat_deg = int(abs(lat))
    lat_min = (abs(lat) - lat_deg) * 60.0
    lat_dir = 'N' if lat >= 0 else 'S'
    lat_str = f"{lat_deg:02d}{lat_min:05.2f}{lat_dir}"

    lon_deg = int(abs(lon))
    lon_min = (abs(lon) - lon_deg) * 60.0
    lon_dir = 'E' if lon >= 0 else 'W'
    lon_str = f"{lon_deg:03d}{lon_min:05.2f}{lon_dir}"

    wx_str = format_aprs_wx(
        temp_c=wx_data.get('temp_c', 0.0),
        hum_pct=wx_data.get('humidity', 50.0),
        pres_hpa=wx_data.get('pressure', 1013.25),
        wind_deg=wx_data.get('wind_deg'),
        wind_kmh=wx_data.get('wind_kmh'),
        gust_kmh=wx_data.get('gust_kmh')
    )
    # APRS Weather Station uses Primary Table '/' and Symbol '_'
    return f"!{lat_str}/{lon_str}_{wx_str}{comment}"

# APRS Digipeater New-N Logic
# ============================================================================

def digipeat_path(path: List[str], my_call: str, allow_wide1: bool = True, allow_wide2: bool = True, repeat_all: bool = True) -> Optional[List[str]]:
    """
    Evaluates APRS path for digipeating.
    Supports universal repeating (repeating all valid packets to expand network coverage),
    while strictly preventing packet loops and self-repeats.
    """
    if not my_call:
        return None

    my_clean = my_call.upper().strip()
    my_base = my_clean.split('-')[0]

    # 1. Never repeat if our exact digipeater callsign already appears in the path (with or without '*')
    if path:
        for p in path:
            clean_p = p.upper().strip().rstrip('*')
            if clean_p == my_clean:
                return None  # We already repeated this packet

    # 2. Check for standard New-N un-digipeated hops (e.g. WIDE1-1, WIDE2-1, TRACE2-2)
    target_idx = None
    if path:
        for i, p in enumerate(path):
            if not p.endswith('*'):
                target_idx = i
                break

    if target_idx is not None:
        cand = path[target_idx].upper().strip()
        new_path = list(path)

        # Direct call match: e.g. OH1W-10 or OH1W
        if cand == my_clean or cand == my_base:
            new_path[target_idx] = f"{my_clean}*"
            return new_path

        # WIDE1-n or WIDE1
        m1 = re.match(r"^WIDE1(?:-(\d+))?$", cand)
        if m1 and allow_wide1:
            n = int(m1.group(1)) if m1.group(1) else 1
            new_path[target_idx] = f"{my_clean}*"
            if n > 1:
                new_path.insert(target_idx + 1, f"WIDE1-{n-1}")
            return new_path

        # WIDE2-n or WIDE2 / WIDE-n / WIDE
        m2 = re.match(r"^WIDE2?(?:-(\d+))?$", cand)
        if m2 and allow_wide2:
            n = int(m2.group(1)) if m2.group(1) else 1
            new_path[target_idx] = f"{my_clean}*"
            if n > 1:
                new_path.insert(target_idx + 1, f"WIDE2-{n-1}")
            return new_path

        # TRACE1-n or TRACE1
        m3 = re.match(r"^TRACE1(?:-(\d+))?$", cand)
        if m3 and allow_wide1:
            n = int(m3.group(1)) if m3.group(1) else 1
            new_path[target_idx] = f"{my_clean}*"
            if n > 1:
                new_path.insert(target_idx + 1, f"TRACE1-{n-1}")
            return new_path

        # TRACE2-n or TRACE2 / TRACE-n / TRACE
        m4 = re.match(r"^TRACE2?(?:-(\d+))?$", cand)
        if m4 and allow_wide2:
            n = int(m4.group(1)) if m4.group(1) else 1
            new_path[target_idx] = f"{my_clean}*"
            if n > 1:
                new_path.insert(target_idx + 1, f"TRACE2-{n-1}")
            return new_path

        # Legacy RELAY or ECHO
        if cand in ("RELAY", "ECHO") and allow_wide1:
            new_path[target_idx] = f"{my_clean}*"
            return new_path

    # 3. Universal repeating mode (Repeat All): If enabled, retransmit even if all previous hops
    # were exhausted (e.g. OH2RCH-3*, WIDE1*, WIDE2*) or if sent direct without a path, by appending our call!
    if not repeat_all:
        return None  # Official standard New-N: packet had no remaining un-digipeated hops!

    new_path = list(path) if path else []
    # AX.25 UI frame allows up to 6 digipeaters in the address header
    if len(new_path) >= 6:
        new_path = new_path[-5:]
    new_path.append(f"{my_clean}*")
    return new_path


# ============================================================================
# Main APRS Engine
# ============================================================================

class AprsEngine:
    """
    Central APRS Engine for Icom Remote.
    Handles:
    - Background Dire Wolf TNC management (via KISS TCP) with pure Python fallback
    - Heard station list tracking, persistence, and WebSocket broadcasting
    - New-N Digipeating with duplicate suppression
    - Periodic and manual APRS position beacons
    """
    def __init__(self, stations_file: str = "aprs_stations.json"):
        self.stations_file = stations_file
        self.lock = threading.Lock()
        
        # Audio & Demodulators
        self.demod = StreamingAfskDemod(sample_rate=16000, baud=1200)
        self.direwolf_mgr = DirewolfManager(on_frame_callback=self._handle_raw_frame)
        
        # Config options
        self.rx_enabled = False
        self.radio_id = "2"  # Default to IC-9700
        self.rx_dev_name = "Icom 9700"
        self.digi_enabled = False
        self.repeat_all = True
        self.wx_enabled = False
        self.cached_weather: Optional[Dict[str, Any]] = None
        self.cached_weather_time = 0.0
        self.saved_symbol = "-"
        self.saved_symbol_table = "/"
        self.my_call = "OH1W-10"
        self.tocall = "APZICM"
        self.allow_wide1 = True
        self.allow_wide2 = True
        self.tx_delay_ms = 350
        self.beacon_enabled = False
        self.beacon_interval_min = 15
        self.beacon_comment = "Icom Remote APRS Digipeater"
        self.my_symbol = "#"
        self.my_symbol_table = "/"
        self.my_lat = 61.2567
        self.my_lon = 22.3546
        self.my_grid = "KP11EG"
        self.freq_lock_enabled = True
        self.aprs_freq_hz = 144800000
        
        # State
        self.heard_stations: Dict[str, Dict[str, Any]] = {}
        self.recent_packets: List[Dict[str, Any]] = []
        self.duplicate_cache: Dict[str, float] = {}  # hash -> timestamp
        self.digipeated_count = 0
        self.last_beacon_time = 0.0
        
        # Callbacks
        self.on_packet_callback: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_digipeat_callback: Optional[Callable[[Dict[str, Any]], None]] = None
        self.tx_audio_callback: Optional[Callable[[str, np.ndarray], None]] = None
        
        # Background workers
        self.running = True
        self.digi_queue = []
        self.digi_lock = threading.Lock()
        
        self.load_stations()
        
        # Start background digipeat & beacon thread
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="aprs-worker")
        self.worker_thread.start()

    def set_callbacks(self, on_packet=None, on_digipeat=None, tx_audio=None):
        self.on_packet_callback = on_packet
        self.on_digipeat_callback = on_digipeat
        self.tx_audio_callback = tx_audio

    def configure(self, cfg: Dict[str, Any]):
        with self.lock:
            old_rx = self.rx_enabled
            old_radio = self.radio_id
            old_call = self.my_call

            if "rx_enabled" in cfg:
                self.rx_enabled = bool(cfg["rx_enabled"])
            if "radio_id" in cfg:
                self.radio_id = str(cfg["radio_id"])
            if "rx_dev_name" in cfg:
                self.rx_dev_name = str(cfg["rx_dev_name"])
            if "digi_enabled" in cfg:
                self.digi_enabled = bool(cfg["digi_enabled"])
            if "repeat_all" in cfg:
                self.repeat_all = bool(cfg["repeat_all"])
            if "wx_enabled" in cfg:
                new_wx = bool(cfg["wx_enabled"])
                if new_wx and not self.wx_enabled:
                    self.saved_symbol = self.my_symbol if self.my_symbol != "_" else "-"
                    self.saved_symbol_table = self.my_symbol_table
                    self.my_symbol = "_"
                    self.my_symbol_table = "/"
                elif not new_wx and self.wx_enabled:
                    self.my_symbol = getattr(self, "saved_symbol", "-")
                    self.my_symbol_table = getattr(self, "saved_symbol_table", "/")
                self.wx_enabled = new_wx
            if "my_call" in cfg:
                self.my_call = str(cfg["my_call"]).upper().strip()
            if "tocall" in cfg:
                self.tocall = str(cfg["tocall"]).strip().upper()[:6] or "APZICM"
            if "allow_wide1" in cfg:
                self.allow_wide1 = bool(cfg["allow_wide1"])
            if "allow_wide2" in cfg:
                self.allow_wide2 = bool(cfg["allow_wide2"])
            if "tx_delay_ms" in cfg:
                self.tx_delay_ms = max(50, min(1000, int(cfg["tx_delay_ms"])))
            if "beacon_enabled" in cfg:
                self.beacon_enabled = bool(cfg["beacon_enabled"])
            if "beacon_interval_min" in cfg:
                self.beacon_interval_min = max(1, min(120, int(cfg["beacon_interval_min"])))
            if "beacon_comment" in cfg:
                self.beacon_comment = str(cfg["beacon_comment"]).strip()
            if "my_symbol" in cfg:
                self.my_symbol = str(cfg["my_symbol"])[:1] or "#"
            if "my_symbol_table" in cfg:
                self.my_symbol_table = str(cfg["my_symbol_table"])[:1] or "/"
            if "freq_lock_enabled" in cfg:
                self.freq_lock_enabled = bool(cfg["freq_lock_enabled"])
            if "aprs_freq_hz" in cfg:
                try:
                    self.aprs_freq_hz = int(cfg["aprs_freq_hz"])
                except Exception:
                    pass
            if "my_lat" in cfg and "my_lon" in cfg:
                try:
                    self.my_lat = float(cfg["my_lat"])
                    self.my_lon = float(cfg["my_lon"])
                    self.my_grid = latlon_to_grid(self.my_lat, self.my_lon)
                except Exception:
                    pass
            elif "my_grid" in cfg and cfg["my_grid"]:
                coords = grid_to_latlon(cfg["my_grid"])
                if coords:
                    self.my_lat, self.my_lon = coords
                    self.my_grid = cfg["my_grid"].strip().upper()

        # Manage background Dire Wolf TNC process
        if self.rx_enabled:
            # If newly enabled, or radio/call changed while running
            if not old_rx or old_radio != self.radio_id or old_call != self.my_call:
                self.direwolf_mgr.start(self.rx_dev_name, self.my_call)
        else:
            if old_rx:
                self.direwolf_mgr.stop()

    def get_weather(self, force: bool = False) -> Optional[Dict[str, Any]]:
        now = time.time()
        if not force and self.cached_weather and (now - self.cached_weather_time) < 600.0:
            return self.cached_weather
        if self.my_lat is not None and self.my_lon is not None:
            wx = fetch_open_meteo_weather(self.my_lat, self.my_lon)
            if wx:
                self.cached_weather = wx
                self.cached_weather_time = now
                return wx
        return self.cached_weather

    def get_status(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "rx_enabled": self.rx_enabled,
                "radio_id": self.radio_id,
                "rx_dev_name": self.rx_dev_name,
                "direwolf_connected": getattr(self.direwolf_mgr, "connected", False),
                "digi_enabled": self.digi_enabled,
                "repeat_all": self.repeat_all,
                "wx_enabled": self.wx_enabled,
                "weather": self.cached_weather,
                "my_call": self.my_call,
                "tocall": getattr(self, "tocall", "APZICM"),
                "allow_wide1": self.allow_wide1,
                "allow_wide2": self.allow_wide2,
                "tx_delay_ms": self.tx_delay_ms,
                "beacon_enabled": self.beacon_enabled,
                "beacon_interval_min": self.beacon_interval_min,
                "beacon_comment": self.beacon_comment,
                "my_symbol": self.my_symbol,
                "my_symbol_table": self.my_symbol_table,
                "my_lat": self.my_lat,
                "my_lon": self.my_lon,
                "my_grid": self.my_grid,
                "freq_lock_enabled": getattr(self, "freq_lock_enabled", True),
                "aprs_freq_hz": getattr(self, "aprs_freq_hz", 144800000),
                "heard_count": len(self.heard_stations),
                "digipeated_count": self.digipeated_count,
            }

    def feed_audio(self, radio_id: str, chunk_f32: np.ndarray):
        """Called directly inside audio_manager.rx_callback for pure Python DSP fallback."""
        if not self.rx_enabled or str(radio_id) != str(self.radio_id):
            return
        # If Dire Wolf is already running and connected, fallback DSP is skipped to save CPU
        if getattr(self.direwolf_mgr, "connected", False):
            return
        try:
            frames = self.demod.process_chunk(chunk_f32)
            for f in frames:
                self._handle_raw_frame(f)
        except Exception as e:
            logging.error(f"APRS internal demodulator error: {e}")

    def _handle_raw_frame(self, frame: bytes):
        """Dispatches an incoming AX.25 frame (from Dire Wolf or fallback DSP)."""
        parsed_ax25 = parse_ax25_frame(frame, has_crc=False)
        if not parsed_ax25:
            return
            
        dest, src, path, payload = parsed_ax25
        path_str = ",".join(path)
        tnc2_str = f"{src}>{dest}{',' + path_str if path_str else ''}:{payload}"
        
        # Parse packet using aprslib if available
        parsed_dict = None
        if aprslib is not None:
            try:
                parsed_dict = aprslib.parse(tnc2_str)
            except Exception:
                pass
                
        now = time.time()
        pkt: Dict[str, Any] = {
            "timestamp": now,
            "raw": tnc2_str,
            "from": src,
            "to": dest,
            "path": path,
            "payload": payload,
            "latitude": None,
            "longitude": None,
            "grid": None,
            "symbol": "/",
            "symbol_table": "/",
            "comment": payload,
            "speed": None,
            "course": None,
            "altitude": None,
            "dist_km": None,
            "bearing_deg": None,
            "bearing_cardinal": None,
        }
        
        if parsed_dict:
            lat = parsed_dict.get("latitude")
            lon = parsed_dict.get("longitude")
            if lat is not None and lon is not None:
                pkt["latitude"] = round(float(lat), 5)
                pkt["longitude"] = round(float(lon), 5)
                pkt["grid"] = latlon_to_grid(pkt["latitude"], pkt["longitude"])
                if self.my_lat is not None and self.my_lon is not None:
                    pkt["dist_km"] = haversine_km(self.my_lat, self.my_lon, pkt["latitude"], pkt["longitude"])
                    b_deg, b_card = calculate_bearing(self.my_lat, self.my_lon, pkt["latitude"], pkt["longitude"])
                    pkt["bearing_deg"] = b_deg
                    pkt["bearing_cardinal"] = b_card
                    
            pkt["symbol"] = parsed_dict.get("symbol", "/") or "/"
            pkt["symbol_table"] = parsed_dict.get("symbol_table", "/") or "/"
            if parsed_dict.get("comment"):
                pkt["comment"] = parsed_dict.get("comment")
            if parsed_dict.get("speed") is not None:
                pkt["speed"] = parsed_dict.get("speed")
            if parsed_dict.get("course") is not None:
                pkt["course"] = parsed_dict.get("course")
            if parsed_dict.get("altitude") is not None:
                pkt["altitude"] = parsed_dict.get("altitude")

        # Update station tracking
        with self.lock:
            self.recent_packets.append(pkt)
            if len(self.recent_packets) > 300:
                self.recent_packets.pop(0)
                
            st = self.heard_stations.setdefault(src, {
                "call": src,
                "first_seen": now,
                "last_seen": now,
                "packet_count": 0,
                "latitude": None,
                "longitude": None,
                "grid": None,
                "symbol": "/",
                "symbol_table": "/",
                "comment": "",
                "speed": None,
                "course": None,
                "altitude": None,
                "dist_km": None,
                "bearing_deg": None,
                "bearing_cardinal": None,
                "path": path,
            })
            st["last_seen"] = now
            st["packet_count"] += 1
            st["path"] = path
            if pkt["latitude"] is not None:
                st["latitude"] = pkt["latitude"]
                st["longitude"] = pkt["longitude"]
                st["grid"] = pkt["grid"]
                st["symbol"] = pkt["symbol"]
                st["symbol_table"] = pkt["symbol_table"]
                st["dist_km"] = pkt["dist_km"]
                st["bearing_deg"] = pkt["bearing_deg"]
                st["bearing_cardinal"] = pkt["bearing_cardinal"]
            if pkt["comment"]:
                st["comment"] = pkt["comment"]
            if pkt["speed"] is not None:
                st["speed"] = pkt["speed"]
            if pkt["course"] is not None:
                st["course"] = pkt["course"]
            if pkt["altitude"] is not None:
                st["altitude"] = pkt["altitude"]

        self.save_stations()

        # Emit to WebSocket clients
        if self.on_packet_callback:
            try:
                self.on_packet_callback(pkt)
            except Exception as e:
                logging.error(f"Error in on_packet_callback: {e}")

        # Digipeat check
        if self.digi_enabled:
            self._check_and_queue_digipeat(dest, src, path, payload)

    def _check_and_queue_digipeat(self, dest: str, src: str, path: List[str], payload: str):
        """Checks duplicate suppression cache and queues frame for repeating."""
        # Only suppress packets originating from this exact digipeater station (e.g. OH1W-10)
        # Mobile OH1W-7, portable OH1W-9 etc. are repeated normally!
        if src.upper().strip() == self.my_call.upper().strip():
            return

        hash_key = hashlib.md5(f"{src}:{payload}".encode('utf-8')).hexdigest()
        now = time.time()
        
        with self.lock:
            # Clean old hashes (> 30s)
            self.duplicate_cache = {h: t for h, t in self.duplicate_cache.items() if (now - t) < 30.0}
            if hash_key in self.duplicate_cache:
                return  # Duplicate! Drop
            self.duplicate_cache[hash_key] = now

        rep_path = digipeat_path(path, self.my_call, self.allow_wide1, self.allow_wide2, self.repeat_all)
        if not rep_path:
            return  # Not eligible or not requested
            
        rep_frame = make_ax25_frame(dest, src, rep_path, payload)
        rep_path_str = ",".join(rep_path)
        rep_raw = f"{src}>{dest}{',' + rep_path_str if rep_path_str else ''}:{payload}"
        
        logging.info(f"[APRS DIGI] Queueing digipeat: {rep_raw}")
        with self.digi_lock:
            self.digi_queue.append({
                "frame": rep_frame,
                "raw": rep_raw,
                "src": src,
                "dest": dest,
                "path": rep_path,
                "timestamp": now,
            })

    def _worker_loop(self):
        """Background thread handling digipeater transmission and periodic beacons."""
        while self.running:
            now = time.time()
            
            # Check TX queue (digipeated frames and position beacons)
            item_to_send = None
            with self.digi_lock:
                if self.digi_queue:
                    item_to_send = self.digi_queue.pop(0)
                    
            if item_to_send:
                raw_tnc2 = item_to_send.get("raw")
                self._transmit_frame(item_to_send["frame"], raw_tnc2=raw_tnc2)
                item_type = item_to_send.get("type", "digi")
                if item_type == "digi":
                    with self.lock:
                        self.digipeated_count += 1
                    if self.on_digipeat_callback:
                        try:
                            self.on_digipeat_callback(item_to_send)
                        except Exception:
                            pass
                elif item_type == "beacon":
                    self.last_beacon_time = time.time()
                # Anti-collision pause
                time.sleep(0.3)
                continue

            # Check Beacon timer
            if self.beacon_enabled and self.my_call and self.my_lat is not None and self.my_lon is not None:
                interval_sec = self.beacon_interval_min * 60
                if (now - self.last_beacon_time) >= interval_sec:
                    self.send_beacon()

            time.sleep(0.1)

    def send_beacon(self) -> bool:
        """Constructs and enqueues an APRS position beacon for background transmission."""
        if not self.my_call or self.my_call == "N0CALL-10":
            return False
            
        if self.wx_enabled:
            wx = self.get_weather()
            if wx:
                comment = self.beacon_comment or "Icom Remote WX"
                payload = format_aprs_wx_pos(self.my_lat, self.my_lon, wx, comment)
            else:
                payload = format_aprs_pos(self.my_lat, self.my_lon, '/', '_', self.beacon_comment)
        else:
            payload = format_aprs_pos(
                self.my_lat, self.my_lon,
                self.my_symbol_table, self.my_symbol,
                self.beacon_comment
            )
        path = ["WIDE1-1", "WIDE2-1"] if self.allow_wide1 else ["WIDE2-2"]
        tocall = getattr(self, "tocall", "APZICM") or "APZICM"
        frame = make_ax25_frame(tocall, self.my_call, path, payload)
        
        raw = f"{self.my_call}>{tocall},{','.join(path)}:{payload}"
        logging.info(f"[APRS BEACON] Enqueueing beacon: {raw}")
        
        with self.digi_lock:
            self.digi_queue.append({
                "type": "beacon",
                "frame": frame,
                "raw": raw,
                "timestamp": time.time(),
            })
        return True

    def _transmit_frame(self, frame: bytes, raw_tnc2: Optional[str] = None):
        """Converts frame to 48 kHz Bell 202 AFSK audio and calls tx_audio_callback."""
        if not self.tx_audio_callback:
            return

        # High-fidelity continuous-phase Bell 202 AFSK modulator:
        # Generates 60 preamble flags (400 ms) + bit-stuffed NRZI payload + FCS (CRC) + 16 postamble flags (106 ms)
        # in a single continuous phase-aligned stream. Zero symbol jitter, no half-bit clock phase jumps.
        target_ms = max(400, self.tx_delay_ms)
        pre_flags = max(60, int(round((target_ms / 1000.0) * 1200.0 / 8.0)))
        post_flags = 16
        bits = frame_to_bits(frame, pre_flags=pre_flags, post_flags=post_flags)
        audio = modulate_afsk(bits, sample_rate=48000, baud=1200, amplitude=0.95)
        logging.info(f"[APRS TX] Generated {len(audio)/48000.0:.2f}s audio with pure Python modulator ({pre_flags} pre flags, {post_flags} post flags, {len(audio)} samples)")

        try:
            self.tx_audio_callback(self.radio_id, audio)
        except Exception as e:
            logging.error(f"[APRS TX] Error transmitting APRS frame: {e}")

    def load_stations(self):
        if not os.path.exists(self.stations_file):
            return
        try:
            with open(self.stations_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self.heard_stations = data
        except Exception as e:
            logging.error(f"Error loading APRS stations: {e}")

    def save_stations(self):
        try:
            with open(self.stations_file, 'w', encoding='utf-8') as f:
                json.dump(self.heard_stations, f, indent=2)
        except Exception as e:
            logging.error(f"Error saving APRS stations: {e}")

    def clear_stations(self):
        with self.lock:
            self.heard_stations.clear()
            self.recent_packets.clear()
        self.save_stations()

    def stop(self):
        self.running = False
        self.direwolf_mgr.stop()
