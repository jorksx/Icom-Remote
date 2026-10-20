"""
APRS (Automatic Packet Reporting System) Engine for ICOM Remote.
Includes:
- 1200 baud Bell 202 AFSK Demodulator (I/Q matched envelope detector + zero-crossing DPLL)
- 1200 baud Bell 202 AFSK Modulator
- AX.25 Frame Encoder and Decoder (CRC-16 CCITT, NRZI, bit stuffing/unstuffing)
- New-N APRS Digipeater with duplicate suppression and hop decrementing
- Station list tracking, distance/bearing calculations, and Maidenhead Grid conversion
- Position beacon generator
"""

import time
import math
import hashlib
import threading
import logging
import json
import os
import re
from typing import Callable, Optional, Dict, Any, List, Tuple
import numpy as np

try:
    import aprslib
except ImportError:
    aprslib = None

# CRC-16 CCITT for AX.25 (reversed polynomial 0x8408)
def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc ^ 0xFFFF


def encode_callsign(call: str, is_last: bool = False, has_repeated: bool = False) -> bytes:
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
    ssid_byte = 0x60 | ((ssid & 0x0F) << 1)
    if has_repeated:
        ssid_byte |= 0x80  # H-bit (has-been-digipeated)
    if is_last:
        ssid_byte |= 0x01  # Extension bit
    res.append(ssid_byte)
    return bytes(res)


def parse_callsign(addr_bytes: bytes) -> Tuple[str, bool, bool]:
    call = ''.join(chr((b >> 1) & 0x7F) for b in addr_bytes[:6]).strip()
    ssid_byte = addr_bytes[6]
    ssid = (ssid_byte >> 1) & 0x0F
    h_bit = bool(ssid_byte & 0x80)
    is_last = bool(ssid_byte & 0x01)
    call_str = f"{call}-{ssid}" if ssid != 0 else call
    return call_str, h_bit, is_last


def make_ax25_frame(dest: str, src: str, path: List[str], payload: str) -> bytes:
    frame = bytearray()
    is_last = (len(path) == 0)
    frame.extend(encode_callsign(dest, False, False))
    frame.extend(encode_callsign(src, is_last, False))
    for i, p in enumerate(path):
        is_last = (i == len(path) - 1)
        has_star = p.endswith('*')
        clean_p = p[:-1] if has_star else p
        frame.extend(encode_callsign(clean_p, is_last, has_star))
    frame.append(0x03)  # UI frame
    frame.append(0xF0)  # No layer 3 protocol
    frame.extend(payload.encode('latin-1', errors='replace'))
    
    # Calculate CRC
    crc = crc16_ccitt(bytes(frame))
    frame.append(crc & 0xFF)
    frame.append((crc >> 8) & 0xFF)
    return bytes(frame)


def parse_ax25_frame(frame: bytes) -> Optional[Tuple[str, str, List[str], str]]:
    if len(frame) < 16:
        return None
    dest, _, _ = parse_callsign(frame[0:7])
    src, _, is_last = parse_callsign(frame[7:14])
    idx = 14
    path = []
    while not is_last and idx + 7 <= len(frame) - 4:
        call, h_bit, is_last = parse_callsign(frame[idx:idx+7])
        path.append(f"{call}*" if h_bit else call)
        idx += 7
    if idx + 2 > len(frame):
        return None
    ctrl = frame[idx]
    pid = frame[idx+1]
    payload = frame[idx+2:-2].decode('latin-1', errors='replace')
    return dest, src, path, payload


def frame_to_bits(frame: bytes, pre_flags: int = 32, post_flags: int = 8) -> List[int]:
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


def modulate_afsk(nrzi_levels: List[int], sample_rate: int = 16000, baud: int = 1200) -> np.ndarray:
    samps_per_bit = sample_rate / baud
    total_samples = int(round(len(nrzi_levels) * samps_per_bit))
    audio = np.zeros(total_samples, dtype=np.float32)
    two_pi = 2.0 * np.pi
    phase = 0.0
    dt = 1.0 / sample_rate
    
    for s_idx, level in enumerate(nrzi_levels):
        freq = 1200.0 if level == 1 else 2200.0
        idx_start = int(round(s_idx * samps_per_bit))
        idx_end = int(round((s_idx + 1) * samps_per_bit))
        if idx_end > total_samples:
            idx_end = total_samples
        n_samples = idx_end - idx_start
        if n_samples <= 0:
            continue
        dphase = two_pi * freq * dt
        phases = phase + np.arange(1, n_samples + 1) * dphase
        audio[idx_start:idx_end] = np.sin(phases)
        phase = phases[-1] % two_pi
        
    return audio


class StreamingAfskDemod:
    """
    Streaming 1200 baud Bell 202 AFSK Demodulator.
    Operates on incoming audio chunks (e.g. 1024 float32 samples at 16000 Hz).
    Uses dual-tone I/Q matched envelope detector + zero-crossing DPLL.
    """
    def __init__(self, sample_rate: int = 16000, baud: int = 1200):
        self.fs = sample_rate
        self.baud = baud
        self.N = int(round(sample_rate / baud))  # 13 samples at 16000 Hz
        self.kernel = np.ones(self.N, dtype=np.float32) / self.N
        self.sample_idx = 0
        
        self.prev_im = np.zeros(self.N - 1, dtype=np.float32)
        self.prev_qm = np.zeros(self.N - 1, dtype=np.float32)
        self.prev_is = np.zeros(self.N - 1, dtype=np.float32)
        self.prev_qs = np.zeros(self.N - 1, dtype=np.float32)
        
        self.phase = 0.0
        self.dphase = baud / sample_rate
        self.prev_discrim = 0.0
        self.prev_level = 1
        
        self.shift_reg = 0
        self.in_frame = False
        self.ones_count = 0
        self.cur_byte = 0
        self.bit_in_byte = 0
        self.byte_buf = bytearray()

    def reset(self):
        self.prev_im.fill(0)
        self.prev_qm.fill(0)
        self.prev_is.fill(0)
        self.prev_qs.fill(0)
        self.phase = 0.0
        self.prev_discrim = 0.0
        self.prev_level = 1
        self.shift_reg = 0
        self.in_frame = False
        self.ones_count = 0
        self.cur_byte = 0
        self.bit_in_byte = 0
        self.byte_buf = bytearray()

    def process_chunk(self, chunk: np.ndarray) -> List[bytes]:
        new_frames = []
        if len(chunk) == 0:
            return new_frames
            
        n_samples = len(chunk)
        indices = np.arange(self.sample_idx, self.sample_idx + n_samples, dtype=np.float32)
        self.sample_idx += n_samples
        
        two_pi = 2.0 * np.pi
        cos_m = np.cos(two_pi * 1200.0 * indices / self.fs)
        sin_m = np.sin(two_pi * 1200.0 * indices / self.fs)
        cos_s = np.cos(two_pi * 2200.0 * indices / self.fs)
        sin_s = np.sin(two_pi * 2200.0 * indices / self.fs)
        
        im_cur = (chunk * cos_m).astype(np.float32)
        qm_cur = (chunk * sin_m).astype(np.float32)
        is_cur = (chunk * cos_s).astype(np.float32)
        qs_cur = (chunk * sin_s).astype(np.float32)
        
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
        
        discrim = (im_f**2 + qm_f**2) - (is_f**2 + qs_f**2)
        
        phase = self.phase
        dphase = self.dphase
        prev_d = self.prev_discrim
        prev_lvl = self.prev_level
        shift_reg = self.shift_reg
        in_frame = self.in_frame
        ones_cnt = self.ones_count
        cur_byte = self.cur_byte
        bit_in_byte = self.bit_in_byte
        byte_buf = self.byte_buf
        
        for k in range(n_samples):
            cur_d = discrim[k]
            # Zero-crossing detector with phase nudge
            if (prev_d < 0 and cur_d >= 0) or (prev_d >= 0 and cur_d < 0):
                if phase < 0.5:
                    phase -= phase * 0.25
                else:
                    phase += (1.0 - phase) * 0.25
                    
            phase_next = phase + dphase
            if phase < 0.5 and phase_next >= 0.5:
                # Sample bit at eye center
                cur_lvl = 1 if cur_d > 0 else 0
                bit = 0 if cur_lvl != prev_lvl else 1
                prev_lvl = cur_lvl
                
                shift_reg = ((shift_reg >> 1) | (bit << 7)) & 0xFF
                if shift_reg == 0x7E:
                    if in_frame:
                        if len(byte_buf) >= 15:
                            payload_bytes = bytes(byte_buf[:-2])
                            fcs_bytes = bytes(byte_buf[-2:])
                            expected_crc = fcs_bytes[0] | (fcs_bytes[1] << 8)
                            if crc16_ccitt(payload_bytes) == expected_crc:
                                new_frames.append(bytes(byte_buf))
                        byte_buf = bytearray()
                        cur_byte = 0
                        bit_in_byte = 0
                        ones_cnt = 0
                    in_frame = True
                    ones_cnt = 0
                elif in_frame:
                    if bit == 1:
                        ones_cnt += 1
                        if ones_cnt >= 7:
                            in_frame = False
                            byte_buf = bytearray()
                        else:
                            cur_byte |= (1 << bit_in_byte)
                            bit_in_byte += 1
                    else:
                        if ones_cnt == 5:
                            # Stuffed bit discarded
                            ones_cnt = 0
                        else:
                            ones_cnt = 0
                            bit_in_byte += 1
                            
                    if bit_in_byte == 8:
                        byte_buf.append(cur_byte)
                        cur_byte = 0
                        bit_in_byte = 0
                        
            if phase_next >= 1.0:
                phase = phase_next - 1.0
            else:
                phase = phase_next
            prev_d = cur_d
            
        self.phase = phase
        self.prev_discrim = prev_d
        self.prev_level = prev_lvl
        self.shift_reg = shift_reg
        self.in_frame = in_frame
        self.ones_count = ones_cnt
        self.cur_byte = cur_byte
        self.bit_in_byte = bit_in_byte
        self.byte_buf = byte_buf
        
        return new_frames


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
        lon = (ord(g[0]) - ord('A')) * 20.0 - 180.0
        lat = (ord(g[1]) - ord('A')) * 10.0 - 90.0
        lon += (ord(g[2]) - ord('0')) * 2.0
        lat += (ord(g[3]) - ord('0')) * 1.0
        if len(g) >= 6:
            lon += (ord(g[4]) - ord('A') + 0.5) * (2.0 / 24.0)
            lat += (ord(g[5]) - ord('A') + 0.5) * (1.0 / 24.0)
        else:
            lon += 1.0
            lat += 0.5
        return lat, lon
    except Exception:
        return None


def calc_distance_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[float, float, str]:
    """Calculates Haversine distance in km, bearing in degrees, and cardinal direction."""
    try:
        R = 6371.0  # Earth radius in km
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        dist = R * c
        
        y = math.sin(dlambda) * math.cos(phi2)
        x = math.cos(phi1)*math.sin(phi2) - math.sin(phi1)*math.cos(phi2)*math.cos(dlambda)
        bearing = (math.degrees(math.atan2(y, x)) + 360) % 360
        
        # Cardinal direction
        dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        idx = int((bearing + 11.25) / 22.5) % 16
        cardinal = dirs[idx]
        return round(dist, 1), round(bearing, 0), cardinal
    except Exception:
        return 0.0, 0.0, ""


def format_aprs_pos(lat: float, lon: float, sym_table: str = '/', sym_char: str = '#', comment: str = '') -> str:
    """Formats coordinates into standard uncompressed APRS position beacon payload."""
    lat_deg = int(abs(lat))
    lat_min = (abs(lat) - lat_deg) * 60.0
    lat_hemi = 'N' if lat >= 0 else 'S'
    lat_str = f"{lat_deg:02d}{lat_min:05.2f}{lat_hemi}"
    
    lon_deg = int(abs(lon))
    lon_min = (abs(lon) - lon_deg) * 60.0
    lon_hemi = 'E' if lon >= 0 else 'W'
    lon_str = f"{lon_deg:03d}{lon_min:05.2f}{lon_hemi}"
    
    return f"!{lat_str}{sym_table}{lon_str}{sym_char}{comment}"


def digipeat_path(path: List[str], my_call: str, allow_wide1: bool = True, allow_wide2: bool = True) -> Optional[List[str]]:
    """
    Evaluates an APRS packet path for digipeating according to New-N paradigm.
    Returns modified path if packet is eligible for digipeating, or None if it should be dropped.
    """
    my_clean = my_call.upper().strip()
    if not my_clean:
        return None

    # Check if we already digipeated this frame
    for p in path:
        p_clean = p.rstrip('*').upper()
        if p_clean == my_clean and p.endswith('*'):
            return None  # Loop prevention: already digipeated by us!

    new_path = list(path)
    digipeated = False
    
    for i, p in enumerate(path):
        if p.endswith('*'):
            continue  # Already completed hop
            
        p_clean = p.upper()
        if p_clean == my_clean:
            # Explicitly addressed to us
            new_path[i] = f"{my_clean}*"
            digipeated = True
            break
            
        m = re.match(r'^(WIDE|TRACE)([1-7])-([1-7])$', p_clean)
        if m:
            alias = m.group(1)
            total = int(m.group(2))
            remaining = int(m.group(3))
            
            if alias == 'WIDE' and total == 1 and not allow_wide1:
                continue
            if alias == 'WIDE' and total == 2 and not allow_wide2:
                continue
                
            if remaining == 1:
                # Last hop for this alias
                new_path[i] = f"{my_clean}*"
                digipeated = True
                break
            elif remaining > 1:
                # Decrement hop count
                new_path[i] = f"{alias}{total}-{remaining - 1}"
                new_path.insert(i, f"{my_clean}*")
                digipeated = True
                break
                
    if digipeated:
        return new_path
    return None


class AprsEngine:
    """
    Full APRS Engine managing:
    - Audio feed from radio
    - Demodulation
    - Packet parsing & station database
    - Digipeating & Duplicate Suppression
    - Periodic position beaconing
    - Persistence to JSON
    """
    def __init__(self, stations_file: str = "aprs_stations.json"):
        self.stations_file = stations_file
        self.lock = threading.Lock()
        
        # Audio & Demodulator
        self.demod = StreamingAfskDemod(sample_rate=16000, baud=1200)
        
        # Config options
        self.rx_enabled = False
        self.radio_id = "2"  # Default to IC-9700
        self.digi_enabled = False
        self.my_call = "N0CALL-10"
        self.allow_wide1 = True
        self.allow_wide2 = True
        self.tx_delay_ms = 250
        self.beacon_enabled = False
        self.beacon_interval_min = 15
        self.beacon_comment = "Icom Remote APRS Digipeater"
        self.my_symbol = "#"
        self.my_symbol_table = "/"
        self.my_lat = 60.1699
        self.my_lon = 24.9384
        self.my_grid = "KP20le"
        
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
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

    def set_callbacks(self, on_packet=None, on_digipeat=None, tx_audio=None):
        self.on_packet_callback = on_packet
        self.on_digipeat_callback = on_digipeat
        self.tx_audio_callback = tx_audio

    def configure(self, cfg: Dict[str, Any]):
        with self.lock:
            if "rx_enabled" in cfg:
                self.rx_enabled = bool(cfg["rx_enabled"])
            if "radio_id" in cfg:
                self.radio_id = str(cfg["radio_id"])
            if "digi_enabled" in cfg:
                self.digi_enabled = bool(cfg["digi_enabled"])
            if "my_call" in cfg:
                self.my_call = str(cfg["my_call"]).upper().strip()
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

    def get_status(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "rx_enabled": self.rx_enabled,
                "radio_id": self.radio_id,
                "digi_enabled": self.digi_enabled,
                "my_call": self.my_call,
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
                "heard_count": len(self.heard_stations),
                "digipeated_count": self.digipeated_count,
            }

    def feed_audio(self, radio_id: str, chunk_f32: np.ndarray):
        """Called directly inside audio_manager.rx_callback."""
        if not self.rx_enabled or str(radio_id) != str(self.radio_id):
            return
        try:
            frames = self.demod.process_chunk(chunk_f32)
            for f in frames:
                self._handle_raw_frame(f)
        except Exception as e:
            logging.error(f"APRS demodulator error: {e}")

    def _handle_raw_frame(self, frame: bytes):
        parsed_ax25 = parse_ax25_frame(frame)
        if not parsed_ax25:
            return
            
        dest, src, path, payload = parsed_ax25
        path_str = ",".join(path)
        tnc2_str = f"{src}>{dest}{',' + path_str if path_str else ''}:{payload}"
        
        # Parse with aprslib if available
        parsed_dict = None
        if aprslib is not None:
            try:
                parsed_dict = aprslib.parse(tnc2_str)
            except Exception:
                pass
                
        # Build packet object
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
            pkt["latitude"] = parsed_dict.get("latitude")
            pkt["longitude"] = parsed_dict.get("longitude")
            pkt["symbol"] = parsed_dict.get("symbol", "/")
            pkt["symbol_table"] = parsed_dict.get("symbol_table", "/")
            pkt["comment"] = parsed_dict.get("comment", payload)
            pkt["speed"] = parsed_dict.get("speed")
            pkt["course"] = parsed_dict.get("course")
            pkt["altitude"] = parsed_dict.get("altitude")

        # Fallback coordinate regex parser if aprslib missed it
        if pkt["latitude"] is None and len(payload) >= 19:
            # Match standard APRS uncompressed: !DDMM.mmN/DDDMM.mmE or =DDMM.mmN/DDDMM.mmE
            m = re.search(r'([!=/@])(\d{2})(\d{2}\.\d{2})([NS])([\\/])(\d{3})(\d{2}\.\d{2})([EW])(.)(.*)', payload)
            if m:
                try:
                    lat_d = int(m.group(2))
                    lat_m = float(m.group(3))
                    lat_sign = 1 if m.group(4) == 'N' else -1
                    lat_val = (lat_d + lat_m / 60.0) * lat_sign
                    
                    lon_d = int(m.group(6))
                    lon_m = float(m.group(7))
                    lon_sign = 1 if m.group(8) == 'E' else -1
                    lon_val = (lon_d + lon_m / 60.0) * lon_sign
                    
                    pkt["latitude"] = lat_val
                    pkt["longitude"] = lon_val
                    pkt["symbol_table"] = m.group(5)
                    pkt["symbol"] = m.group(9)
                    pkt["comment"] = m.group(10)
                except Exception:
                    pass

        # Calculate Grid, Distance, Bearing if coordinates exist
        if pkt["latitude"] is not None and pkt["longitude"] is not None:
            pkt["grid"] = latlon_to_grid(pkt["latitude"], pkt["longitude"])
            if self.my_lat is not None and self.my_lon is not None:
                d, b, c = calc_distance_bearing(self.my_lat, self.my_lon, pkt["latitude"], pkt["longitude"])
                pkt["dist_km"] = d
                pkt["bearing_deg"] = b
                pkt["bearing_cardinal"] = c

        # Update station database
        with self.lock:
            st = self.heard_stations.get(src, {})
            pkt_count = st.get("packet_count", 0) + 1
            st.update({
                "call": src,
                "last_seen": now,
                "packet_count": pkt_count,
                "to": dest,
                "path": path,
                "raw": tnc2_str,
                "comment": pkt["comment"],
                "symbol": pkt["symbol"],
                "symbol_table": pkt["symbol_table"],
            })
            if pkt["latitude"] is not None and pkt["longitude"] is not None:
                st["latitude"] = pkt["latitude"]
                st["longitude"] = pkt["longitude"]
                st["grid"] = pkt["grid"]
                st["dist_km"] = pkt["dist_km"]
                st["bearing_deg"] = pkt["bearing_deg"]
                st["bearing_cardinal"] = pkt["bearing_cardinal"]
                if pkt["speed"] is not None: st["speed"] = pkt["speed"]
                if pkt["course"] is not None: st["course"] = pkt["course"]
                if pkt["altitude"] is not None: st["altitude"] = pkt["altitude"]

            self.heard_stations[src] = st
            
            # Add to recent packets
            self.recent_packets.append(pkt)
            if len(self.recent_packets) > 150:
                self.recent_packets = self.recent_packets[-150:]

        self.save_stations()

        # Fire callback
        if self.on_packet_callback:
            try:
                self.on_packet_callback(pkt)
            except Exception as e:
                logging.error(f"APRS packet callback error: {e}")

        # Evaluate for Digipeater
        if self.digi_enabled and self.my_call and src.upper() != self.my_call.upper():
            self._evaluate_digipeat(dest, src, path, payload, tnc2_str)

    def _evaluate_digipeat(self, dest: str, src: str, path: List[str], payload: str, raw_tnc: str):
        # 1. Duplicate suppression
        # Hash source call + payload
        hash_key = hashlib.md5(f"{src}:{payload}".encode('utf-8')).hexdigest()
        now = time.time()
        
        with self.lock:
            # Clean old hashes (> 30s)
            self.duplicate_cache = {h: t for h, t in self.duplicate_cache.items() if (now - t) < 30.0}
            if hash_key in self.duplicate_cache:
                return  # Duplicate! Drop
            self.duplicate_cache[hash_key] = now

        # 2. Check path eligibility
        rep_path = digipeat_path(path, self.my_call, self.allow_wide1, self.allow_wide2)
        if not rep_path:
            return  # Not eligible or not requested

        # 3. Build repeated frame
        rep_frame = make_ax25_frame(dest, src, rep_path, payload)
        
        rep_path_str = ",".join(rep_path)
        rep_raw = f"{src}>{dest}{',' + rep_path_str if rep_path_str else ''}:{payload}"
        
        logging.info(f"[APRS DIGI] Repeating packet: {rep_raw}")
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
            
            # Check Digipeater queue
            item_to_send = None
            with self.digi_lock:
                if self.digi_queue:
                    item_to_send = self.digi_queue.pop(0)
                    
            if item_to_send:
                self._transmit_frame(item_to_send["frame"])
                with self.lock:
                    self.digipeated_count += 1
                if self.on_digipeat_callback:
                    try:
                        self.on_digipeat_callback(item_to_send)
                    except Exception:
                        pass
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
        """Constructs and transmits an APRS position beacon."""
        if not self.my_call:
            return False
            
        payload = format_aprs_pos(
            self.my_lat, self.my_lon,
            self.my_symbol_table, self.my_symbol,
            self.beacon_comment
        )
        path = ["WIDE1-1", "WIDE2-1"] if self.allow_wide1 else ["WIDE2-2"]
        frame = make_ax25_frame("APRS", self.my_call, path, payload)
        
        raw = f"{self.my_call}>APRS,{','.join(path)}:{payload}"
        logging.info(f"[APRS BEACON] Transmitting: {raw}")
        
        self.last_beacon_time = time.time()
        self._transmit_frame(frame)
        return True

    def _transmit_frame(self, frame: bytes):
        """Converts frame to AFSK audio and calls tx_audio_callback."""
        if not self.tx_audio_callback:
            return
            
        # Preamble flags based on tx_delay_ms (1 flag = 8 bits = 6.67 ms at 1200 baud)
        pre_flags = max(16, int(round((self.tx_delay_ms / 1000.0) * 1200.0 / 8.0)))
        bits = frame_to_bits(frame, pre_flags=pre_flags, post_flags=8)
        audio = modulate_afsk(bits, sample_rate=16000, baud=1200)
        
        try:
            self.tx_audio_callback(self.radio_id, audio)
        except Exception as e:
            logging.error(f"Error transmitting APRS frame: {e}")

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
