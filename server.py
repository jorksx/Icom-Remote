import sys
import subprocess
import asyncio
import json
import logging
import os
import time
import threading
import queue
import ctypes
import ctypes.wintypes
import wave
import datetime
import socket
import shutil

try:
    import aiohttp
    from aiohttp import web
    import serial
    import serial.tools.list_ports
    import pystray
    from PIL import Image, ImageDraw
    import sounddevice as sd
    import numpy as np
    import webview
    import aprslib
    import aprs_modem
except ImportError:
    logging.warning("Asennetaan tarvittavat kirjastot...")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install",
        "aiohttp", "pyserial", "pystray", "pillow", "sounddevice", "numpy", "pywebview", "aprslib"
    ])
    import aiohttp
    from aiohttp import web
    import serial
    import serial.tools.list_ports
    import pystray
    from PIL import Image, ImageDraw
    import sounddevice as sd
    import numpy as np
    import webview
    import aprslib
    import aprs_modem

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(BASE_DIR, 'server.log'), encoding='utf-8')
    ]
)
CONFIG_FILE = os.path.join(BASE_DIR, 'icom_config.json')

ICOM_MODELS = {
    0x94: "IC-7300",
    0xA2: "IC-9700",
    0x98: "IC-7610",
    0x8E: "IC-7851",
    0x80: "IC-7850",
    0x6A: "IC-7800",
    0x74: "IC-7700",
    0x7A: "IC-7410",
    0x60: "IC-9100",
    0x76: "IC-7200",
    0x70: "IC-7000",
    0x88: "IC-7100",
    0xA4: "IC-705",
    0x66: "IC-746PRO",
    0x56: "IC-756PRO",
    0x5C: "IC-756PROII",
    0x6E: "IC-756PROIII",
    0x50: "IC-756",
    0x40: "IC-775",
    0x3C: "IC-781",
    0x46: "IC-706MKIIG",
    0x58: "IC-706MKII",
    0x4E: "IC-706",
    0x48: "IC-703",
    0x2C: "IC-751A",
    0x26: "IC-735",
    0x04: "IC-725",
    0x28: "IC-726",
    0x38: "IC-728",
    0x30: "IC-737",
    0x44: "IC-738",
    0x1E: "IC-765",
    0x20: "IC-761",
    0x8A: "IC-2730",
    0x86: "ID-5100",
    0x90: "ID-4100",
    0xA0: "ID-52",
    0x9A: "ID-51",
    0x82: "ID-31",
    0x6C: "ID-1",
    0x32: "IC-970",
    0x2E: "IC-275",
    0x34: "IC-375",
    0x36: "IC-475",
    0x3E: "IC-575",
    0x96: "IC-R8600",
    0x64: "IC-R8500",
}

YAESU_MODELS = {
    "0650": "FT-891",
    "0570": "FT-991",
    "0670": "FT-991A",
    "0710": "FTDX10",
    "0680": "FTDX101D",
    "0681": "FTDX101MP",
    "0730": "FT-710",
    "0580": "FTDX3000",
    "0460": "FTDX1200",
    "0450": "FT-450",
    "0520": "FT-2000",
    "0380": "FT-950",
    "0240": "FT-857",
    "0250": "FT-897",
    "0280": "FT-817",
    "0620": "FT-818"
}


APP_PORT = 8085          # HTTPS Web / Mobile portti
LOCAL_HTTP_PORT = 8086   # Paikallinen HTTP-portti työpöytäikkunalle
WINDOW_TITLE = "Icom 7300 & 9700 Remote"


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", ctypes.wintypes.DWORD),
        ("hwndOwner", ctypes.wintypes.HWND),
        ("hInstance", ctypes.wintypes.HINSTANCE),
        ("lpstrFilter", ctypes.wintypes.LPCWSTR),
        ("lpstrCustomFilter", ctypes.wintypes.LPWSTR),
        ("nMaxCustFilter", ctypes.wintypes.DWORD),
        ("nFilterIndex", ctypes.wintypes.DWORD),
        ("lpstrFile", ctypes.wintypes.LPWSTR),
        ("nMaxFile", ctypes.wintypes.DWORD),
        ("lpstrFileTitle", ctypes.wintypes.LPWSTR),
        ("nMaxFileTitle", ctypes.wintypes.DWORD),
        ("lpstrInitialDir", ctypes.wintypes.LPCWSTR),
        ("lpstrTitle", ctypes.wintypes.LPCWSTR),
        ("Flags", ctypes.wintypes.DWORD),
        ("nFileOffset", ctypes.wintypes.WORD),
        ("nFileExtension", ctypes.wintypes.WORD),
        ("lpstrDefExt", ctypes.wintypes.LPCWSTR),
        ("lCustData", ctypes.wintypes.LPARAM),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", ctypes.wintypes.LPCWSTR),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", ctypes.wintypes.DWORD),
        ("FlagsEx", ctypes.wintypes.DWORD),
    ]


def save_wav_dialog_blocking(default_filename: str) -> str:
    """Avaa Windowsin natiivin 'Save As...' -ikkunan äänitiedoston tallennusta varten."""
    try:
        buf = ctypes.create_unicode_buffer(default_filename, 1024)
        ofn = OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        ofn.hwndOwner = None
        ofn.lpstrFilter = "WAV Audio File (*.wav)\0*.wav\0All Files (*.*)\0*.*\0\0"
        ofn.nFilterIndex = 1
        ofn.lpstrFile = ctypes.cast(buf, ctypes.wintypes.LPWSTR)
        ofn.nMaxFile = 1024
        ofn.lpstrTitle = "Save Recorded Radio Audio"
        ofn.lpstrDefExt = "wav"
        # OFN_OVERWRITEPROMPT (0x2) | OFN_NOCHANGEDIR (0x8) | OFN_PATHMUSTEXIST (0x800) | OFN_EXPLORER (0x80000)
        ofn.Flags = 0x00000002 | 0x00000008 | 0x00000800 | 0x00080000
        if ctypes.windll.comdlg32.GetSaveFileNameW(ctypes.byref(ofn)):
            path = buf.value
            if path and not path.lower().endswith(".wav"):
                path += ".wav"
            return path
    except Exception as e:
        logging.error(f"Save dialog error: {e}")
    return ""

DEFAULT_CONFIG = {
    "web_pin": "7300",
    "web_pin_enabled": True,
    "web_pin_updated": 1729240000,
    "bookmarks": [
        {"id": "bm_1", "name": "80m FT8", "hz": 3573000, "mode": "USB-D", "radio_id": "1"},
        {"id": "bm_2", "name": "80m Voice Net", "hz": 3699000, "mode": "LSB", "radio_id": "1"},
        {"id": "bm_3", "name": "40m FT8", "hz": 7074000, "mode": "USB-D", "radio_id": "1"},
        {"id": "bm_4", "name": "20m FT8", "hz": 14074000, "mode": "USB-D", "radio_id": "1"},
        {"id": "bm_5", "name": "2m FT8", "hz": 144174000, "mode": "USB-D", "radio_id": "2"},
        {"id": "bm_6", "name": "2m FM Calling", "hz": 145500000, "mode": "FM", "radio_id": "2"},
        {"id": "bm_7", "name": "70cm FM Calling", "hz": 433500000, "mode": "FM", "radio_id": "2"}
    ],
    "radios": {
        "1": {
            "name": "IC-7300",
            "port": "COM8",
            "baud": 115200,
            "default_addr": 0x94,
            "detected_addr": 0x94,
            "auto_connect": True,
            "vfo_offset": 0,
            "ptt_key_code": 0,
            "ptt_key_name": "None",
            "ptt_global": False,
            "audio_rx_dev": "1",
            "audio_tx_dev": "8",
            "audio_spk_dev": "",
            "audio_mic_dev": "",
            "audio_rx_vol": 100,
            "sql_pct": 0,
            "rigctld_port": 4532,
            "vcom_port": "",
            "wf_floor": 0,
            "wf_gain": 60,
            "wf_peak": 85,
            "wf_speed": 2,
            "wf_mode": "3d"
        },
        "2": {
            "name": "IC-9700",
            "port": "COM9",
            "baud": 115200,
            "default_addr": 0xA2,
            "detected_addr": 0xA2,
            "auto_connect": True,
            "vfo_offset": 0,
            "ptt_key_code": 0,
            "ptt_key_name": "None",
            "ptt_global": False,
            "audio_rx_dev": "3",
            "audio_tx_dev": "10",
            "audio_spk_dev": "",
            "audio_mic_dev": "",
            "audio_rx_vol": 100,
            "sql_pct": 0,
            "rigctld_port": 4533,
            "vcom_port": "",
            "wf_floor": 0,
            "wf_gain": 60,
            "wf_peak": 85,
            "wf_speed": 2,
            "wf_mode": "3d"
        },
        "3": {
            "name": "Yaesu FT-891",
            "brand": "auto",
            "port": "",
            "baud": 38400,
            "default_addr": 0x94,
            "detected_addr": 0x94,
            "auto_connect": False,
            "vfo_offset": 0,
            "ptt_key_code": 0,
            "ptt_key_name": "None",
            "ptt_global": False,
            "audio_rx_dev": "",
            "audio_tx_dev": "",
            "audio_spk_dev": "",
            "audio_mic_dev": "",
            "audio_rx_vol": 100,
            "sql_pct": 0,
            "rigctld_port": 4534,
            "vcom_port": "",
            "wf_floor": 0,
            "wf_gain": 60,
            "wf_peak": 85,
            "wf_speed": 2,
            "wf_mode": "3d"
        }
    },
    "active_radios": ["1"],
    "aprs": {
        "rx_enabled": False,
        "radio_id": "2",
        "digi_enabled": False,
        "my_call": "N0CALL-10",
        "allow_wide1": True,
        "allow_wide2": True,
        "tx_delay_ms": 250,
        "beacon_enabled": False,
        "beacon_interval_min": 15,
        "beacon_comment": "Icom Remote APRS Digipeater",
        "my_symbol": "#",
        "my_symbol_table": "/",
        "my_lat": 60.1699,
        "my_lon": 24.9384,
        "my_grid": "KP20le"
    }
}


def load_config():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        # Jos vanha yhden radion konfiguraatio, päivitetään uuteen muotoon
        if "radios" not in raw:
            conf = json.loads(json.dumps(DEFAULT_CONFIG))
            if raw.get("port"):
                conf["radios"]["1"]["port"] = raw.get("port")
                conf["radios"]["1"]["baud"] = int(raw.get("baud", 115200))
            save_config(conf)
            return conf
        # Varmistetaan yleiset asetukset
        if "web_pin" not in raw:
            raw["web_pin"] = DEFAULT_CONFIG["web_pin"]
        if "web_pin_enabled" not in raw:
            raw["web_pin_enabled"] = DEFAULT_CONFIG["web_pin_enabled"]
        if "web_pin_updated" not in raw:
            raw["web_pin_updated"] = DEFAULT_CONFIG["web_pin_updated"]
        if "bookmarks" not in raw or not isinstance(raw["bookmarks"], list):
            raw["bookmarks"] = list(DEFAULT_CONFIG["bookmarks"])
        if "aprs" not in raw or not isinstance(raw["aprs"], dict):
            raw["aprs"] = dict(DEFAULT_CONFIG["aprs"])
        else:
            for k, v in DEFAULT_CONFIG["aprs"].items():
                raw["aprs"].setdefault(k, v)
        if "active_radios" not in raw or not isinstance(raw["active_radios"], list):
            raw["active_radios"] = ["1"]
        # Varmistetaan oletuskentät
        for rid in ("1", "2", "3"):
            if rid not in raw["radios"]:
                raw["radios"][rid] = dict(DEFAULT_CONFIG["radios"][rid])
            else:
                for k, v in DEFAULT_CONFIG["radios"][rid].items():
                    raw["radios"][rid].setdefault(k, v)
        return raw
    except Exception:
        conf = json.loads(json.dumps(DEFAULT_CONFIG))
        save_config(conf)
        return conf


def save_config(conf):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(conf, indent=2, ensure_ascii=False, fp=f)
    except Exception as e:
        logging.error(f"Config save error: {e}")


global_config = load_config()
app_loop = None
desktop_window_focused = True

# 64-bit safe Win32 API function signatures to prevent 0xc0000005 access violations
_user32 = ctypes.windll.user32
_user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
_user32.GetForegroundWindow.argtypes = []
_user32.GetAncestor.restype = ctypes.wintypes.HWND
_user32.GetAncestor.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]


def is_desktop_window_active():
    """Tarkistaa turvallisesti onko työpöytäikkuna aktiivinen ilman muistirikkoutumisia."""
    if desktop_window_focused:
        return True
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return False
        root_hwnd = _user32.GetAncestor(hwnd, 2) or hwnd
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(root_hwnd, buf, 256)
        if WINDOW_TITLE in buf.value:
            return True
        _user32.GetWindowTextW(hwnd, buf, 256)
        if WINDOW_TITLE in buf.value:
            return True
        return False
    except Exception:
        return desktop_window_focused


def civ_command(cmd, subcmd=None, data=None, to_addr=0x94):
    payload = bytearray([0xFE, 0xFE, to_addr, 0xE0, cmd])
    if subcmd is not None:
        payload.append(subcmd)
    if data:
        payload.extend(data)
    payload.append(0xFD)
    return bytes(payload)


def hz_to_bcd(hz):
    hz = max(0, int(hz))
    s = str(hz).zfill(10)
    pairs = [s[i:i+2] for i in range(0, 10, 2)][::-1]
    return bytes(int(p, 16) for p in pairs)


def bcd_to_hz(data):
    try:
        hz_str = ''.join(f"{b:02x}" for b in reversed(data))
        return int(hz_str)
    except Exception:
        return 0


def _ic9700_band(hz: int) -> str:
    if 140000000 <= hz <= 150000000:
        return '2m'
    if 420000000 <= hz <= 450000000:
        return '70cm'
    if 1200000000 <= hz <= 1350000000:
        return '23cm'
    return ''


def offset_hz_to_bcd3(offset_hz: int) -> bytes:
    """Muuntaa erotustaajuuden (Hz) Icomin 3-tavuiseen BCD-muotoon (komento 0D: [1kHz|100Hz], [100kHz|10kHz], [10MHz|1MHz])."""
    return hz_to_bcd(max(0, int(offset_hz)))[1:4]


def bcd3_to_offset_hz(data: bytes) -> int:
    if len(data) == 3:
        return bcd_to_hz(bytes([0x00]) + data + bytes([0x00]))
    elif len(data) >= 5:
        return bcd_to_hz(data[:5])
    return 0


def ctcss_to_bcd3(tone_hz: float) -> bytes:
    """Muuntaa CTCSS-aliäänen (esim. 123.0 Hz) Icomin 3-tavuiseen BCD-muotoon (1B 00 / 1B 01: 0x00, [100Hz|10Hz], [1Hz|0.1Hz])."""
    tenths = max(670, min(2541, int(round(float(tone_hz) * 10))))
    s = f"{tenths:04d}"
    return bytes([0x00, int(s[0:2], 16), int(s[2:4], 16)])


def bcd3_to_ctcss(data: bytes) -> float:
    if len(data) >= 3:
        try:
            return int(f"{data[1]:02x}{data[2]:02x}") / 10.0
        except Exception:
            pass
    return 123.0


class RadioController:
    def __init__(self, radio_id: str, default_addr: int = 0x94):
        self.radio_id = str(radio_id)
        self.ser = None
        self.reader_task = None
        self.poller_task = None
        rconf = global_config["radios"].get(self.radio_id, {})
        saved_addr = int(rconf.get("detected_addr", default_addr) or default_addr)
        self.radio_addr = saved_addr
        self.default_addr = default_addr
        self.scope_divs = [[] for _ in range(11)]
        self.scope_span_hz = 100000
        self.scope_mode_center = True
        self.scope_edge_low = 0
        self.scope_edge_high = 0
        self.last_scope_time = 0.0
        self.last_radio_reply = 0.0
        self.running = False
        self.key_ptt_held = False
        self.current_band = 0  # 0 = MAIN, 1 = SUB (IC-9700)
        self.last_power_set_time = 0.0
        self.power_off_until = 0.0
        self.ptt_release_until = 0.0
        self.cat_ptt = False
        self.user_ptt = False
        self.syncing = False
        self._sync_lock = None
        self.vcom_bridge = None
        self.ser_lock = threading.Lock()

        is_9700 = (self.radio_addr == 0xA2)
        default_hz = 145500000 if is_9700 else 14200000
        default_sub_hz = 433500000
        default_mode = 'FM' if is_9700 else 'USB'
        self.brand = rconf.get("brand", "auto")
        if self.brand == "yaesu":
            model_str = rconf.get("name") or "FT-891"
            default_hz = 14200000
            default_mode = 'USB'
            is_9700 = False
        else:
            model_str = 'IC-9700' if is_9700 else 'IC-7300'
        self.has_hw_scope = True if self.brand != "yaesu" else False
        self.state = {
            'radio_id': self.radio_id,
            'brand': self.brand,
            'name': model_str,
            'model': model_str,
            'port': rconf.get('port', ''),
            'baud': int(rconf.get('baud', 115200)),
            'taajuus_mhz': f"{default_hz / 1000000:.6f}",
            'taajuus_hz': default_hz,
            'mode': default_mode,
            'filter': 1,
            'data_on': False,
            # IC-9700 Dual RX (MAIN & SUB) kentät:
            'dual_rx': 1 if is_9700 else 0,
            'active_band': 0,  # 0 = MAIN, 1 = SUB
            'main_hz': default_hz,
            'main_mode': default_mode,
            'main_filter': 1,
            'sub_hz': default_sub_hz,
            'sub_mode': 'FM',
            'sub_filter': 1,
            # Toistinasetukset (VHF/UHF IC-9700):
            'dup': 'OFF',          # 'OFF', 'DUP-', 'DUP+'
            'offset_mhz': 1.6,     # Erotustaajuus MHz (esim. 0.6 tai 1.6)
            'tone_mode': 'OFF',    # 'OFF', '1750Hz', 'TONE', 'TSQL'
            'ctcss': 123.0,        # Aliääni Hz (67.0 .. 254.1)
            'tone_burst': 0,       # 1 kun 1750 Hz avauspiippi on päällä
            'smeter': 'S0',
            'smeter_pct': 0.0,
            'sql_pct': int(rconf.get('sql_pct', 0) or 0),
            'ptt': 0,
            'power_pct': 100,
            'connected': False,   # Onko sarjaportti avattu (Start painettu)
            'online': False       # Vastaako radio oikeasti (virrat päällä)
        }
        self._update_model_name()

    def write_civ(self, cmd_bytes: bytes):
        """Thread-safe CI-V write method."""
        if self.ser and self.ser.is_open:
            with self.ser_lock:
                try:
                    self.ser.write(cmd_bytes)
                except Exception:
                    pass

    def write_raw(self, data_bytes: bytes):
        """Thread-safe serial raw write method (for Yaesu CAT)."""
        if self.ser and self.ser.is_open:
            with self.ser_lock:
                try:
                    self.ser.write(data_bytes)
                except Exception:
                    pass

    def _update_model_name(self):
        if getattr(self, 'brand', 'auto') == 'yaesu':
            saved_name = global_config.get("radios", {}).get(self.radio_id, {}).get("name", "")
            model_name = saved_name if (saved_name and not saved_name.startswith("IC-")) else "FT-891"
            self.state['model'] = model_name
            self.state['name'] = model_name
            return
        detected_model = ICOM_MODELS.get(self.radio_addr, f"ICOM (0x{self.radio_addr:02X})")
        self.state['model'] = detected_model
        self.state['name'] = detected_model
        if self.radio_id in global_config.get("radios", {}):
            global_config["radios"][self.radio_id]["detected_addr"] = self.radio_addr
            global_config["radios"][self.radio_id]["name"] = detected_model

    def _enable_scope_hw(self):
        if getattr(self, 'brand', 'auto') == 'yaesu':
            return
        if self.ser and self.ser.is_open:
            try:
                # 27 10 01 = Scope waveform data stream ON (IC-7300 & IC-9700)
                # 27 11 01 = Scope CI-V data output ON (IC-9700 CI-V setting)
                # 27 14 00 = Scope sweep speed FAST (00=FAST, 01=MID, 02=SLOW)
                self.ser.write(civ_command(0x27, 0x10, bytes([0x01]), to_addr=self.radio_addr))
                self.ser.write(civ_command(0x27, 0x11, bytes([0x01]), to_addr=self.radio_addr))
                self.ser.write(civ_command(0x27, 0x14, bytes([0x00]), to_addr=self.radio_addr))
            except Exception:
                pass

    async def _sync_ic9700_dual(self):
        """Lukee IC-9700:n molemmat vastaanottimet (MAIN & SUB) sekä Dualwatch-tilan turvallisesti."""
        if not self.ser or not self.ser.is_open or self.radio_addr != 0xA2 or getattr(self, 'brand', 'auto') == 'yaesu':
            return
        was_syncing = getattr(self, 'syncing', False)
        self.syncing = True
        self.initial_synced = True
        try:
            # 1. Pysäytetään scope-datan lähetys (27 10 00) synkronoinnin ajaksi,
            # jotta 500-tavuiset vesiputouspaketit eivät törmää CI-V-kyselyihin,
            # ja odotetaan 80 ms että puskurissa olevat vanhat vastaukset tyhjenevät:
            self.write_civ(civ_command(0x27, 0x10, bytes([0x00]), to_addr=self.radio_addr))
            await asyncio.sleep(0.08)

            # 2. Kysytään aktiivinen bandi (07 D2), Dualwatch-tila (16 59), RF Power (14 0A) ja PTT (1C 00)
            self.write_civ(civ_command(0x07, 0xD2, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.05)
            self.write_civ(civ_command(0x16, 0x59, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.05)
            self.write_civ(civ_command(0x14, 0x0A, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.04)
            self.write_civ(civ_command(0x1C, 0x00, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.05)

            orig_band = self.state.get('active_band', 0)

            # Jos Dualwatch on pois päältä, kytketään se päälle (16 59 01) jotta SUB VFO on luettavissa
            if self.state.get('dual_rx', 0) == 0:
                self.state['dual_rx'] = 1
                self.write_civ(civ_command(0x16, 0x59, bytes([0x01]), to_addr=self.radio_addr))
                await asyncio.sleep(0.06)

            # 3. Vaihdetaan SUB VFO:lle (07 D1), odotetaan että siirto on valmis, ja luetaan SUB taajuus (03) & mode (04)
            self.write_civ(civ_command(0x07, 0xD1, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.08)
            self.current_band = 1
            self.state['active_band'] = 1
            self.write_civ(civ_command(0x03, None, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.08)
            self.write_civ(civ_command(0x04, None, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.08)

            # 4. Vaihdetaan MAIN VFO:lle (07 D0), odotetaan että SUB-vastaukset on luettu, ja luetaan MAIN taajuus (03) & mode (04)
            self.write_civ(civ_command(0x07, 0xD0, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.08)
            self.current_band = 0
            self.state['active_band'] = 0
            self.write_civ(civ_command(0x03, None, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.08)
            self.write_civ(civ_command(0x04, None, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.08)
            self.write_civ(civ_command(0x1A, 0x06, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.05)
            # Luetaan myös toistinasetukset (DUP, Offset, Tone, TSQL, CTCSS)
            for r_cmd in (
                civ_command(0x0F, None, None, to_addr=self.radio_addr),
                civ_command(0x0C, None, None, to_addr=self.radio_addr),
                civ_command(0x16, 0x42, None, to_addr=self.radio_addr),
                civ_command(0x16, 0x43, None, to_addr=self.radio_addr),
                civ_command(0x1B, 0x00, None, to_addr=self.radio_addr),
            ):
                self.write_civ(r_cmd)
                await asyncio.sleep(0.035)

            # 5. Jos SUB oli alun perin valittuna, palautetaan valinta SUB VFO:lle
            if orig_band == 1:
                self.write_civ(civ_command(0x07, 0xD1, None, to_addr=self.radio_addr))
                await asyncio.sleep(0.07)
                self.current_band = 1
                self.state['active_band'] = 1
                self.state['taajuus_hz'] = self.state.get('sub_hz', self.state['taajuus_hz'])
                self.state['mode'] = self.state.get('sub_mode', self.state['mode'])
                self.state['filter'] = self.state.get('sub_filter', self.state['filter'])
            else:
                self.current_band = 0
                self.state['active_band'] = 0
                self.state['taajuus_hz'] = self.state.get('main_hz', self.state['taajuus_hz'])
                self.state['mode'] = self.state.get('main_mode', self.state['mode'])
                self.state['filter'] = self.state.get('main_filter', self.state['filter'])
            self.state['taajuus_mhz'] = f"{(self.state['taajuus_hz'] / 1000000):.6f}"

            self._enable_scope_hw()
            await broadcast_state(self.radio_id)
        except Exception as e:
            logging.debug(f"IC-9700 dual sync error: {e}")
        finally:
            if not was_syncing:
                self.syncing = False

    async def _sync_initial_state(self):
        """Hakee radion kaikki nykyiset asetukset (taajuus, mode, filtteri, DATA, RF Power, PTT, Dual RX) heti yhdistettäessä."""
        if not self.ser or not self.ser.is_open or getattr(self, 'syncing', False):
            return
        self.syncing = True
        self.initial_synced = True
        try:
            if getattr(self, 'brand', 'auto') == 'yaesu':
                # EX11051; varmistaa 11-05 SSB MIC SELECT = REAR (1) ja EX1107; lukee 11-07 SSB BFO -tilan.
                # ML1000;ML0000; asettaa Monitor Level = 0 ja Monitor OFF (vaimentaa FT-891:n tunnetun CAT-naputuksen kaiuttimesta!).
                # AI1; ottaa käyttöön Auto Information -tilan (taajuus ja mode päivittyvät automaattisesti nupista).
                self.write_raw(b"AI1;TX0;EX11051;ID;FA;MD0;EX1107;SM0;PC;ML1000;ML0000;")
                await asyncio.sleep(0.05)
                await broadcast_state(self.radio_id)
                return

            if self.radio_addr == 0xA2:
                self.write_civ(civ_command(0x19, 0x00, None, to_addr=self.radio_addr))
                await asyncio.sleep(0.04)
                await self._sync_ic9700_dual()
                return

            self._enable_scope_hw()
            await asyncio.sleep(0.03)
            # Kysytään RF Power (14 0A), taajuus (03), mode/filtteri (04), DATA-tila (1A 06), PTT (1C 00) ja S-mittari (15 02)
            for cmd_bytes in (
                civ_command(0x19, 0x00, None, to_addr=self.radio_addr),
                civ_command(0x14, 0x0A, None, to_addr=self.radio_addr),
                civ_command(0x03, None, None, to_addr=self.radio_addr),
                civ_command(0x04, None, None, to_addr=self.radio_addr),
                civ_command(0x1A, 0x06, None, to_addr=self.radio_addr),
                civ_command(0x1C, 0x00, None, to_addr=self.radio_addr),
                civ_command(0x15, 0x02, None, to_addr=self.radio_addr),
            ):
                if self.ser and self.ser.is_open:
                    self.write_civ(cmd_bytes)
                    await asyncio.sleep(0.035)

            await broadcast_state(self.radio_id)
        except Exception as e:
            logging.debug(f"Radio {self.radio_id} initial sync error: {e}")
        finally:
            self.syncing = False

    async def connect(self, port, baudrate):
        await self.disconnect_async()

        if not port:
            self.state['connected'] = False
            self.state['online'] = False
            await broadcast_state(self.radio_id)
            return {"status": "offline", "message": "No COM port"}

        self.state['port'] = port
        self.state['baud'] = int(baudrate)
        global_config["radios"][self.radio_id]["port"] = port
        global_config["radios"][self.radio_id]["baud"] = int(baudrate)
        save_config(global_config)

        try:
            self.ser = serial.Serial()
            self.ser.port = port
            self.ser.baudrate = int(baudrate)
            self.ser.timeout = 0
            self.ser.dtr = True
            self.ser.rts = True
            try:
                self.ser.open()
            except PermissionError:
                await asyncio.sleep(0.3)
                self.ser.open()
            self.ser.dtr = True
            self.ser.rts = True

            self.running = True
            self.initial_synced = False
            self.user_ptt = False
            self.cat_ptt = False
            self.ptt_release_until = time.monotonic() + 0.8
            self.state['ptt'] = 0
            self.state['connected'] = True
            self.state['online'] = False
            self.last_scope_time = 0.0
            self.last_radio_reply = 0.0
            self.power_off_until = 0.0

            self.reader_task = asyncio.create_task(self._read_serial_loop())

            # Kysytään aloituskyselyt ilman ylimääräisiä päällekkäisiä kyselyitä
            await asyncio.sleep(0.05)
            if getattr(self, 'brand', 'auto') == 'yaesu':
                self.write_raw(b"AI1;TX0;ID;FA;MD0;ML1000;ML0000;")
                await asyncio.sleep(0.06)

            if not self.state['online']:
                seen_addrs = []
                for probe_addr in [self.radio_addr, 0x94, 0xA2, 0x98, 0x8E, 0xA4, 0x00]:
                    if self.state['online']:
                        break
                    if probe_addr in seen_addrs:
                        continue
                    seen_addrs.append(probe_addr)
                    if self.ser and self.ser.is_open:
                        self.write_civ(civ_command(0x03, None, None, to_addr=probe_addr))
                        await asyncio.sleep(0.04)
                if not self.state['online']:
                    self.write_raw(b"AI0;TX0;ID;FA;MD0;")
                    await asyncio.sleep(0.06)

            while getattr(self, 'syncing', False):
                await asyncio.sleep(0.04)
            if self.state['online'] and not getattr(self, 'initial_synced', False):
                await self._sync_initial_state()

            self.poller_task = asyncio.create_task(self._poll_loop())
            await broadcast_state(self.radio_id)
            return {"status": "ok"}
        except Exception as e:
            logging.info(f"Radio {self.radio_id} ({port}) offline: {e}")
            self.running = False
            self.state['connected'] = False
            self.state['online'] = False
            await broadcast_state(self.radio_id)
            return {"status": "offline"}

    async def set_radio_power(self, power_on: bool):
        """Käynnistää (18 01 + 0xFE heräte) tai sammuttaa (18 00) radion USB CI-V -väylän kautta."""
        rconf = global_config["radios"].get(self.radio_id, {})
        port = self.state.get('port') or rconf.get('port', '')
        baud = int(self.state.get('baud') or rconf.get('baud', 115200))

        if not self.state['connected'] or not self.ser or not self.ser.is_open:
            if not port:
                return {"status": "offline", "error": "No COM port selected"}
            await self.connect(port, baud)

        if not self.ser or not self.ser.is_open:
            return {"status": "offline", "error": "Serial port not open"}

        try:
            if power_on:
                self.power_off_until = 0.0
                if getattr(self, 'brand', 'auto') == 'yaesu':
                    self.write_raw(b"PS1;ID;FA;MD0;")
                    return {"status": "ok"}
                if baud >= 115200:
                    fe_count = 155
                elif baud >= 57600:
                    fe_count = 80
                elif baud >= 38400:
                    fe_count = 55
                elif baud >= 19200:
                    fe_count = 30
                else:
                    fe_count = 16
                preamble = b'\xfe' * fe_count
                other_addr = 0xA2 if self.radio_addr == 0x94 else 0x94
                self.ser.write(preamble + civ_command(0x18, 0x01, None, to_addr=self.radio_addr))
                await asyncio.sleep(0.20)
                if not self.state['online']:
                    self.ser.write(preamble + civ_command(0x18, 0x01, None, to_addr=other_addr))
                # Kysytään taajuutta käynnistymisen aikana, jotta tila muuttuu heti ONLINEksi kun radio herää
                for _ in range(10):
                    await asyncio.sleep(0.25)
                    if self.state['online'] or not self.ser or not self.ser.is_open:
                        break
                    self.ser.write(civ_command(0x03, None, None, to_addr=self.radio_addr))
            else:
                # Estetään radion sammumisen aikana tulevien hännystavujen (FB ACK ja viimeiset scope-paketit)
                # nostamasta tilaa takaisin ONLINEksi
                self.power_off_until = time.monotonic() + 4.0
                self.last_radio_reply = 0.0
                self.user_ptt = False
                self.state['online'] = False
                self.state['ptt'] = 0
                self.state['smeter'] = 'S0'
                self.state['smeter_pct'] = 0.0
                if getattr(self, 'brand', 'auto') == 'yaesu':
                    self.write_raw(b"TX0;PS0;")
                else:
                    self.ser.write(civ_command(0x18, 0x00, None, to_addr=self.radio_addr))
                try:
                    self.ser.reset_input_buffer()
                except Exception:
                    pass
                await broadcast_state(self.radio_id)
            return {"status": "ok"}
        except Exception as e:
            logging.error(f"Radio {self.radio_id} power cmd error: {e}")
            return {"status": "error", "error": str(e)}

    async def disconnect_async(self):
        self.running = False
        self.user_ptt = False
        self.cat_ptt = False
        self.state['connected'] = False
        self.state['online'] = False
        self.state['ptt'] = 0
        self.state['smeter'] = 'S0'
        self.state['smeter_pct'] = 0.0
        if 'audio_manager' in globals() and self.radio_id in audio_manager.channels:
            audio_manager.channels[self.radio_id]._stop_tx_stream()
        for t in (self.reader_task, self.poller_task):
            if t and not t.done():
                t.cancel()
        self.reader_task = None
        self.poller_task = None
        if self.ser and self.ser.is_open:
            try:
                # Varmistetaan että PTT vapautuu yhteyden katketessa
                if getattr(self, 'brand', 'icom') == 'yaesu':
                    self.ser.write(b"TX0;")
                else:
                    self.ser.write(civ_command(0x1C, 0x00, bytes([0x00]), to_addr=self.radio_addr))
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        await broadcast_state(self.radio_id)

    def disconnect_sync(self):
        self.running = False
        self.user_ptt = False
        self.cat_ptt = False
        self.state['connected'] = False
        self.state['online'] = False
        if 'audio_manager' in globals() and self.radio_id in audio_manager.channels:
            audio_manager.channels[self.radio_id]._stop_tx_stream()
        if self.ser and self.ser.is_open:
            try:
                # Varmistetaan että PTT vapautuu yhteyden katketessa
                if getattr(self, 'brand', 'icom') == 'yaesu':
                    self.ser.write(b"TX0;")
                else:
                    self.ser.write(civ_command(0x1C, 0x00, bytes([0x00]), to_addr=self.radio_addr))
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    async def _read_serial_loop(self):
        buffer = bytearray()
        while self.running and self.state['connected']:
            try:
                waiting = self.ser.in_waiting if (self.ser and self.ser.is_open) else 0
                if waiting > 0:
                    buffer.extend(self.ser.read(waiting))

                    # 1. Tarkistetaan Icom CI-V kehykset (FE FE ... FD)
                    while b'\xfe\xfe' in buffer:
                        idx_fe = buffer.find(b'\xfe\xfe')
                        idx_fd = buffer.find(b'\xfd', idx_fe)
                        if idx_fd == -1:
                            # Kehys on vielä kesken
                            break
                        msg = buffer[idx_fe:idx_fd+1]
                        buffer = buffer[idx_fd+1:]

                        if self.vcom_bridge and self.vcom_bridge.vser and self.vcom_bridge.vser.is_open:
                            try:
                                self.vcom_bridge.vser.write(msg)
                            except Exception:
                                pass
                        if time.monotonic() < self.power_off_until:
                            continue
                        if len(msg) >= 7 and msg[3] == 0xE0 and msg[4] == 0x07:
                            if msg[5] == 0xD0:
                                self.current_band = 0
                            elif msg[5] == 0xD1:
                                self.current_band = 1
                        if len(msg) >= 6 and (msg[2] == 0xE0 or msg[2] == 0x00) and msg[3] != 0xE0:
                            if len(msg) < 7 or msg[4] in (0xFA, 0xFB):
                                continue
                            self.brand = 'icom'
                            self.state['brand'] = 'icom'
                            self.has_hw_scope = True
                            model_changed = (self.radio_addr != msg[3])
                            if model_changed:
                                self.radio_addr = msg[3]
                                self._update_model_name()
                                save_config(global_config)
                            was_offline = not self.state['online']
                            self.last_radio_reply = time.monotonic()
                            self.state['online'] = True
                            if was_offline or model_changed:
                                asyncio.create_task(self._sync_initial_state())
                            await self._parse_civ(msg, force_broadcast=(was_offline or model_changed))

                    # 2. Tarkistetaan Yaesu ASCII kehykset (; päätteiset)
                    while b';' in buffer:
                        if b'\xfe\xfe' in buffer:
                            break
                        idx_semi = buffer.find(b';')
                        chunk = buffer[:idx_semi+1]
                        buffer = buffer[idx_semi+1:]
                        try:
                            text_cmd = chunk.decode('ascii', errors='ignore').strip()
                            if text_cmd.endswith(';'):
                                cmd2 = text_cmd[:2].upper()
                                if cmd2 in ('ID', 'FA', 'FB', 'MD', 'SM', 'TX', 'PC', 'AC', 'IF', 'AI', 'PS', 'EX'):
                                    self.brand = 'yaesu'
                                    self.state['brand'] = 'yaesu'
                                    self.has_hw_scope = False
                                    await self._parse_yaesu(text_cmd)
                        except Exception:
                            pass

                    if len(buffer) > 4096:
                        buffer = buffer[-512:]

                    await asyncio.sleep(0.001)
                else:
                    await asyncio.sleep(0.004)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.info(f"Radio {self.radio_id} serial disconnected: {e}")
                self.state['online'] = False
                self.state['connected'] = False
                self.running = False
                try:
                    if self.ser:
                        self.ser.close()
                except Exception:
                    pass
                await broadcast_state(self.radio_id)
                break

    async def _parse_yaesu(self, text: str, force_broadcast=False):
        state_changed = force_broadcast
        was_offline = not self.state['online']
        self.state['online'] = True
        self.last_radio_reply = time.monotonic()

        text = text.strip()
        if not text.endswith(';'):
            return

        cmd = text[:2].upper()
        body = text[2:-1]

        if cmd == 'ID':
            model_name = YAESU_MODELS.get(body, f"FT-{body}" if body else "Yaesu")
            self.brand = 'yaesu'
            self.state['brand'] = 'yaesu'
            self.has_hw_scope = False
            was_model = self.state.get('model')
            cur_baud = self.ser.baudrate if self.ser else self.state.get('baud')
            if was_model != model_name or self.state.get('baud') != cur_baud:
                self.state['model'] = model_name
                self.state['name'] = model_name
                if cur_baud:
                    self.state['baud'] = cur_baud
                    global_config["radios"].setdefault(self.radio_id, {})["baud"] = cur_baud
                state_changed = True
                global_config["radios"].setdefault(self.radio_id, {})["name"] = model_name
                global_config["radios"][self.radio_id]["brand"] = "yaesu"
                save_config(global_config)
                self.write_raw(b"AI1;ML1000;ML0000;")
                if was_offline:
                    asyncio.create_task(self._sync_initial_state())

        elif cmd == 'FA':
            try:
                hz = int(body)
                if hz > 0 and self.state.get('taajuus_hz') != hz:
                    self.state['taajuus_hz'] = hz
                    self.state['taajuus_mhz'] = f"{(hz / 1000000):.6f}"
                    self.state['main_hz'] = hz
                    state_changed = True
            except ValueError:
                pass

        elif cmd == 'FB':
            try:
                hz = int(body)
                if hz > 0 and self.state.get('sub_hz') != hz:
                    self.state['sub_hz'] = hz
                    state_changed = True
            except ValueError:
                pass

        elif cmd == 'MD':
            if len(body) >= 2 and body[0] == '0':
                m_code = body[1].upper()
                YAESU_MODES = {
                    "1": "LSB", "2": "USB", "3": "CW", "4": "FM", "5": "AM",
                    "6": "RTTY", "7": "CW", "8": "LSB-D", "9": "RTTY",
                    "A": "FM-D", "B": "FM", "C": "USB-D", "D": "AM", "E": "FM"
                }
                new_mode = YAESU_MODES.get(m_code, "USB")
                # Yaesu FT-891:ssa SSB-tilassa (MD01/MD02) todellinen sivukaista (LSB vs USB)
                # määräytyy valikon 11-07 [SSB BFO] (EX1107: 0=USB, 1=LSB, 2=AUTO) mukaan:
                if m_code in ("1", "2"):
                    bfo = getattr(self, 'yaesu_ssb_bfo', None)
                    if bfo == '0':
                        new_mode = "USB"
                    elif bfo == '1':
                        new_mode = "LSB"
                is_data = new_mode.endswith('-D')
                if self.state.get('mode') != new_mode or self.state.get('data_on') != is_data:
                    self.state['mode'] = new_mode
                    self.state['main_mode'] = new_mode
                    self.state['data_on'] = is_data
                    state_changed = True

        elif cmd == 'EX':
            # 11-07 [SSB BFO]: EX11070; = USB, EX11071; = LSB, EX11072; = AUTO
            if body.startswith('1107') and len(body) >= 5:
                bfo_code = body[4]
                self.yaesu_ssb_bfo = bfo_code
                cur_m = self.state.get('mode', 'USB')
                if cur_m in ('LSB', 'USB'):
                    actual_ssb = 'USB' if bfo_code == '0' else ('LSB' if bfo_code == '1' else cur_m)
                    if cur_m != actual_ssb:
                        self.state['mode'] = actual_ssb
                        self.state['main_mode'] = actual_ssb
                        state_changed = True

        elif cmd == 'SM':
            try:
                val = int(body[1:]) if (len(body) > 1 and body[0] == '0') else int(body)
                # Yaesu FT-891 hardware SM0nnn (0..255) calibration:
                # 0..126 = S0..S9 (126 = S9 -> mapped to 60.0% on the UI S-meter bar)
                # 127..230 = S9+5dB..S9+60dB (143 = S9+10dB, 160 = S9+20dB, 195 = S9+40dB, 230 = S9+60dB)
                if val <= 4:
                    s_str = "S0"
                    s_pct = 0.0
                elif val <= 126:
                    s_unit = max(1, min(9, int(round((val / 126.0) * 9.0))))
                    s_str = f"S{s_unit}"
                    s_pct = round((val / 126.0) * 60.0, 2)
                else:
                    over_db = min(60, max(5, int(round(((val - 126.0) / 104.0) * 60.0 / 5.0)) * 5))
                    s_str = f"S9 + {over_db}dB"
                    s_pct = round(min(100.0, 60.0 + ((val - 126.0) / 104.0) * 40.0), 2)

                if self.state.get('smeter') != s_str or abs(float(self.state.get('smeter_pct', 0)) - s_pct) >= 0.8:
                    self.state['smeter'] = s_str
                    self.state['smeter_pct'] = s_pct
                    state_changed = True

                ch = audio_manager.channels.get(self.radio_id) if 'audio_manager' in globals() else None
                if ch is not None and ch.sql_pct > 0.0 and s_pct >= ch.sql_pct:
                    ch.sql_last_open = time.monotonic()
            except ValueError:
                pass

        elif cmd == 'PC':
            try:
                pwr = int(body)
                if self.state.get('power_pct') != pwr:
                    self.state['power_pct'] = pwr
                    state_changed = True
            except ValueError:
                pass

        elif cmd == 'TX':
            is_tx = (body in ('1', '2'))
            # Estetään SignaLinkin VOX-viiveen (DLY hang-time) aiheuttama PTT:n uudelleenlukittuminen
            # heti sen jälkeen kun käyttäjä on vapauttanut PTT-napin:
            if is_tx and not getattr(self, 'user_ptt', False) and not getattr(self, 'cat_ptt', False) and time.monotonic() < getattr(self, 'ptt_release_until', 0.0):
                return
            tila = 1 if is_tx else 0
            if tila == 0 and (getattr(self, 'cat_ptt', False) or getattr(self, 'user_ptt', False)):
                return
            if self.state.get('ptt') != tila:
                self.state['ptt'] = tila
                state_changed = True

        elif cmd == 'AC':
            is_on = (body == '001')
            t_val = 1 if is_on else 0
            if self.state.get('atu') != t_val:
                self.state['atu'] = t_val
                state_changed = True

        if state_changed or was_offline:
            await broadcast_state(self.radio_id)

    async def _parse_civ(self, msg, force_broadcast=False):
        if len(msg) < 6:
            return
        cmd = msg[4]
        state_changed = force_broadcast
        in_tune_guard = time.monotonic() < getattr(self, 'tune_guard_until', 0.0)

        # Taajuus (00 tai 03)
        if (cmd == 0x00 or cmd == 0x03) and len(msg) >= 11:
            if not in_tune_guard:
                hz = bcd_to_hz(msg[5:10])
                if hz > 0:
                    if self.state['taajuus_hz'] != hz:
                        self.state['taajuus_hz'] = hz
                        self.state['taajuus_mhz'] = f"{(hz / 1000000):.6f}"
                        state_changed = True
                    if self.current_band == 1:
                        if self.state['sub_hz'] != hz:
                            self.state['sub_hz'] = hz
                            state_changed = True
                    else:
                        if self.state['main_hz'] != hz:
                            self.state['main_hz'] = hz
                            state_changed = True

        # Mode (01 tai 04)
        elif (cmd == 0x01 or cmd == 0x04) and len(msg) >= 7:
            if not in_tune_guard:
                m_byte = msg[5]
                fil_val = int(msg[6]) if (len(msg) >= 8 and msg[6] in (1, 2, 3)) else self.state.get('filter', 1)
                self.state['filter'] = fil_val
                inv_mode = {
                    0x00: "LSB", 0x01: "USB", 0x02: "AM",
                    0x03: "CW", 0x04: "RTTY", 0x05: "FM",
                    0x07: "CW", 0x08: "RTTY", 0x17: "DV"
                }
                base_mode = inv_mode.get(m_byte, "USB")
                new_mode = f"{base_mode}-D" if (self.state.get('data_on') and base_mode in ("LSB", "USB", "FM")) else base_mode
                if self.state['mode'] != new_mode:
                    self.state['mode'] = new_mode
                    state_changed = True
                if self.current_band == 1:
                    if self.state['sub_mode'] != new_mode or self.state['sub_filter'] != fil_val:
                        self.state['sub_mode'] = new_mode
                        self.state['sub_filter'] = fil_val
                        state_changed = True
                else:
                    if self.state['main_mode'] != new_mode or self.state['main_filter'] != fil_val:
                        self.state['main_mode'] = new_mode
                        self.state['main_filter'] = fil_val
                        state_changed = True

        # IC-9700 Aktiivinen Bandi (07 D2: 00=MAIN, 01=SUB)
        elif cmd == 0x07 and len(msg) >= 8 and msg[5] == 0xD2:
            if not in_tune_guard:
                ab = 1 if msg[6] == 0x01 else 0
                self.current_band = ab
                if self.state['active_band'] != ab:
                    self.state['active_band'] = ab
                    if ab == 1:
                        self.state['taajuus_hz'] = self.state['sub_hz']
                        self.state['mode'] = self.state['sub_mode']
                        self.state['filter'] = self.state['sub_filter']
                    else:
                        self.state['taajuus_hz'] = self.state['main_hz']
                        self.state['mode'] = self.state['main_mode']
                        self.state['filter'] = self.state['main_filter']
                    self.state['taajuus_mhz'] = f"{(self.state['taajuus_hz'] / 1000000):.6f}"
                    state_changed = True

        # IC-9700 Dualwatch / Sub Band ON/OFF (16 59: 00=Single RX, 01=Dual RX)
        elif cmd == 0x16 and len(msg) >= 8 and msg[5] == 0x59:
            dw = 1 if msg[6] == 0x01 else 0
            if self.state['dual_rx'] != dw:
                self.state['dual_rx'] = dw
                state_changed = True

        # Toistinääni (Repeater Tone 16 42: 00=OFF, 01=ON) ja TSQL (16 43: 00=OFF, 01=ON)
        elif cmd == 0x16 and len(msg) >= 8 and msg[5] in (0x42, 0x43):
            if not in_tune_guard:
                is_on = (msg[6] == 0x01)
                if msg[5] == 0x42:
                    if is_on and self.state.get('tone_mode') != 'TONE':
                        self.state['tone_mode'] = 'TONE'
                        state_changed = True
                    elif not is_on and self.state.get('tone_mode') == 'TONE':
                        self.state['tone_mode'] = 'OFF'
                        state_changed = True
                elif msg[5] == 0x43:
                    if is_on and self.state.get('tone_mode') != 'TSQL':
                        self.state['tone_mode'] = 'TSQL'
                        state_changed = True
                    elif not is_on and self.state.get('tone_mode') == 'TSQL':
                        self.state['tone_mode'] = 'OFF'
                        state_changed = True

        # Duplex-suunta (0F: 10=Simplex/OFF, 11=DUP-, 12=DUP+)
        elif cmd == 0x0F and len(msg) >= 7:
            if not in_tune_guard:
                d_map = {0x10: 'OFF', 0x11: 'DUP-', 0x12: 'DUP+'}
                if msg[5] in d_map:
                    new_dup = d_map[msg[5]]
                    if self.state.get('dup') != new_dup:
                        self.state['dup'] = new_dup
                        state_changed = True

        # Duplex-erotustaajuuden luku (0C)
        elif cmd == 0x0C and len(msg) >= 9:
            if not in_tune_guard:
                off_hz = bcd3_to_offset_hz(msg[5:-1])
                if off_hz > 0:
                    off_mhz = round(off_hz / 1000000.0, 3)
                    if self.state.get('offset_mhz') != off_mhz:
                        self.state['offset_mhz'] = off_mhz
                        state_changed = True

        # CTCSS-aliäänitaajuuden luku (1B 00 = Tone, 1B 01 = TSQL)
        elif cmd == 0x1B and len(msg) >= 10 and msg[5] in (0x00, 0x01):
            if not in_tune_guard:
                c_hz = bcd3_to_ctcss(msg[6:9])
                if 60.0 <= c_hz <= 260.0 and self.state.get('ctcss') != c_hz:
                    self.state['ctcss'] = c_hz
                    state_changed = True

        # Transceiver Model ID (19 00)
        elif cmd == 0x19 and len(msg) >= 7 and msg[5] == 0x00:
            dev_id = msg[-2]
            detected_model = ICOM_MODELS.get(dev_id)
            if detected_model and self.state.get('model') != detected_model:
                self.state['model'] = detected_model
                self.state['name'] = detected_model
                if self.radio_id in global_config.get("radios", {}):
                    global_config["radios"][self.radio_id]["name"] = detected_model
                state_changed = True

        # Data-tilan luku (1A 06)
        elif cmd == 0x1A and len(msg) >= 8 and msg[5] == 0x06:
            data_flag = msg[6]
            self.state['data_on'] = (data_flag == 0x01)
            if len(msg) >= 9 and msg[7] in (1, 2, 3):
                self.state['filter'] = int(msg[7])
            current_base = self.state.get('mode', 'USB').split('-')[0]
            new_mode = f"{current_base}-D" if (self.state['data_on'] and current_base in ("LSB", "USB", "FM")) else current_base
            if self.state['mode'] != new_mode:
                self.state['mode'] = new_mode
                state_changed = True

        # RF Power -tehotason luku (14 0A: 0000..0255 BCD -> 0..100%)
        elif cmd == 0x14 and len(msg) >= 9 and msg[5] == 0x0A:
            if time.monotonic() - self.last_power_set_time >= 0.5:
                try:
                    val_str = ''.join(f"{b:02x}" for b in msg[6:8])
                    val = int(val_str)
                    pwr_pct = max(0, min(100, int(round((val / 255.0) * 100.0))))
                    if self.state.get('power_pct') != pwr_pct:
                        self.state['power_pct'] = pwr_pct
                        state_changed = True
                except Exception:
                    pass

        # PTT tilan luku / kuittaus (1C 00)
        elif cmd == 0x1C and len(msg) >= 8 and msg[5] == 0x00:
            ptt_val = 1 if msg[6] == 0x01 else 0
            if ptt_val == 0 and (getattr(self, 'cat_ptt', False) or getattr(self, 'user_ptt', False)):
                return
            if ptt_val == 1 and not getattr(self, 'cat_ptt', False) and not getattr(self, 'user_ptt', False) and (time.monotonic() - getattr(self, 'last_ptt_off_time', 0.0) < 0.8):
                return
            if self.state['ptt'] != ptt_val:
                self.state['ptt'] = ptt_val
                state_changed = True

        # S-mittari (15 02): S0..S9 = 0..60% (sininen), S9..+60dB = 60..100% (keltainen->punainen)
        elif cmd == 0x15 and msg[5] == 0x02 and len(msg) >= 9:
            try:
                val_str = ''.join(f"{b:02x}" for b in msg[6:8])
                val = int(val_str)
                s_num = (val / 255.0) * 120.0
                if val == 0:
                    s_txt = "S0"
                    s_pct = 0.0
                elif s_num <= 54.0:
                    s_txt = f"S{max(1, int(s_num / 6))}"
                    s_pct = round((s_num / 54.0) * 60.0, 2)
                else:
                    s_txt = f"S9 + {int(s_num - 54)}dB"
                    s_pct = round(min(100.0, 60.0 + ((s_num - 54.0) / 60.0) * 40.0), 2)
                if self.state['smeter'] != s_txt or abs(self.state['smeter_pct'] - s_pct) >= 0.3:
                    self.state['smeter'] = s_txt
                    self.state['smeter_pct'] = s_pct
                    state_changed = True
                ch = audio_manager.channels.get(self.radio_id) if 'audio_manager' in globals() else None
                if ch is not None and ch.sql_pct > 0.0 and s_pct >= ch.sql_pct:
                    ch.sql_last_open = time.monotonic()
            except Exception:
                pass

        # Waterfall / Spectrum (27 00)
        # Division 1 contains BCD metadata (mode, frequency, span) and 0 waveform bytes.
        # Divisions 2..11 contain the 475 waveform amplitude bytes in msg[9:-1] (msg[8] is MaxDiv = 0x11).
        elif cmd == 0x27 and msg[5] == 0x00:
            now_t = time.monotonic()
            self.last_scope_time = now_t
            if len(msg) >= 8:
                bcd = msg[7]
                real_div = (bcd // 16) * 10 + (bcd % 16)
                if real_div == 1:
                    try:
                        # msg[8] on MaxDiv (0x11), msg[9] on Scope Mode:
                        # 0x00 = Center, 0x01 = Fixed, 0x02 = Scroll-C, 0x03 = Scroll-F
                        scope_mode = msg[9] if len(msg) > 9 else 0
                        if scope_mode == 0:  # Center mode: msg[10:15]=Center BCD, msg[15:20]=Half-Span (±Hz) BCD
                            if len(msg) >= 20:
                                f_center = bcd_to_hz(msg[10:15])
                                half_span = bcd_to_hz(msg[15:20])
                                if 1000 <= half_span <= 5000000:
                                    self.scope_span_hz = half_span * 2
                                    self.scope_mode_center = True
                                    if f_center > 0:
                                        self.scope_edge_low = f_center - half_span
                                        self.scope_edge_high = f_center + half_span
                        elif scope_mode in (1, 2, 3):  # Fixed / Scroll mode: msg[10:15]=Low BCD, msg[15:20]=High BCD
                            if len(msg) >= 20:
                                f_low = bcd_to_hz(msg[10:15])
                                f_high = bcd_to_hz(msg[15:20])
                                if f_high > f_low:
                                    self.scope_span_hz = f_high - f_low
                                    self.scope_mode_center = False
                                    self.scope_edge_low = f_low
                                    self.scope_edge_high = f_high
                    except Exception:
                        pass
                if 1 <= real_div <= 10:
                    # Scope-pyyhkäisy on kesken (div 1..10): pidetään pollaus hetken tauolla ettei CI-V törmää
                    self.scope_burst_until = now_t + 0.038
                elif real_div == 11:
                    self.scope_burst_until = 0.0
                if len(msg) > 9 and 2 <= real_div <= 11:
                    self.scope_divs[real_div - 2] = list(msg[9:-1])
                    if real_div == 11 and ws_clients:
                        full_row = []
                        for part in self.scope_divs[:10]:
                            full_row.extend(part)
                        if len(full_row) == 475:
                            try:
                                # Pidetään Icomin 475 biniä täysin terävinä (ilman horisontaalista blur-suodinta!)
                                # ja korostetaan kapeita signaalihuippuja 1D Unsharp Mask -terävöityksellä:
                                arr = np.array(full_row, dtype=np.float32)
                                lap = np.zeros_like(arr)
                                lap[1:-1] = arr[1:-1] - 0.5 * (arr[:-2] + arr[2:])
                                arr_sharp = arr + np.maximum(0.0, lap) * 0.48

                                # 72. persentiili edustaa todellisen pohjakohinan yläreunaa (aiempi 25. persentiili
                                # jätti 75% pohjakohinasta kynnyksen yläpuolelle aiheuttaen leveää "mössöä" 2D-vesiputoukseen):
                                p_floor = float(np.percentile(arr, 72))
                                if not hasattr(self, 'scope_noise_floor'):
                                    self.scope_noise_floor = p_floor
                                alpha = 0.28 if p_floor < self.scope_noise_floor else 0.08
                                self.scope_noise_floor = (1.0 - alpha) * self.scope_noise_floor + alpha * p_floor
                                snr = np.maximum(0.0, arr_sharp - (self.scope_noise_floor + 1.2))
                                processed_row = np.round(np.clip(snr * 2.15, 0.0, 160.0), 1).tolist()
                            except Exception:
                                processed_row = full_row
                            ws_msg = json.dumps({
                                "type": "scope",
                                "radio_id": self.radio_id,
                                "div": 0x11,
                                "span_hz": getattr(self, 'scope_span_hz', 100000),
                                "center_mode": getattr(self, 'scope_mode_center', True),
                                "edge_low": getattr(self, 'scope_edge_low', 0),
                                "edge_high": getattr(self, 'scope_edge_high', 0),
                                "data": processed_row
                            })
                            await broadcast_ws(ws_msg)

        if state_changed and ws_clients and not getattr(self, 'syncing', False):
            await broadcast_state(self.radio_id)

    async def _poll_loop(self):
        tick = 0
        while self.running and self.state['connected']:
            try:
                now = time.monotonic()
                if now < self.power_off_until:
                    tick += 1
                    await asyncio.sleep(0.08)
                    continue

                if self.state['online'] and (now - self.last_radio_reply > 6.0):
                    self.state['online'] = False
                    self.state['smeter'] = 'S0'
                    self.state['smeter_pct'] = 0.0
                    await broadcast_state(self.radio_id)

                if self.ser and self.ser.is_open:
                    if not self.state['online']:
                        # Kysellään aina sekä Icomia että Yaesua kun radio on offline-tilassa,
                        # jotta radion tai COM-portin vaihto lennosta toimii välittömästi
                        if tick % 12 == 0:
                            self.write_civ(civ_command(0x03, None, None, self.radio_addr))
                        elif tick % 12 == 6:
                            alt_addr = 0xA2 if self.radio_addr == 0x94 else 0x94
                            self.write_civ(civ_command(0x03, None, None, alt_addr))
                        if tick % 4 == 0:
                            self.write_raw(b"ID;FA;MD0;")

                        # Automaattinen baudinopeuden haku jos radio ei vastaa nykyisellä nopeudella (~1.8s välein)
                        if tick >= 24 and tick % 16 == 0 and getattr(self, 'brand', 'auto') in ('yaesu', 'auto'):
                            candidate_bauds = [19200, 38400, 9600, 4800, 115200]
                            cur_b = self.ser.baudrate
                            next_idx = (candidate_bauds.index(cur_b) + 1) % len(candidate_bauds) if cur_b in candidate_bauds else 0
                            next_b = candidate_bauds[next_idx]
                            try:
                                self.ser.baudrate = next_b
                                self.state['baud'] = next_b
                                self.ser.reset_input_buffer()
                                self.write_raw(b"ID;FA;")
                            except Exception:
                                pass
                    else:
                        if getattr(self, 'syncing', False):
                            tick += 1
                            await asyncio.sleep(0.08)
                            continue

                        if self.state.get('ptt') == 1:
                            self.last_radio_reply = now
                            self.last_scope_time = now
                            # Tarkistetaan TX-tila myös lähetyksen aikana, jotta ulkoisen PTT:n (esim. SignaLink / WSJT-X)
                            # vapautuminen havaitaan heti eikä PTT jää jumiin päälle!
                            if tick % 3 == 0:
                                if getattr(self, 'brand', 'icom') == 'yaesu':
                                    self.write_raw(b"TX;")
                                else:
                                    self.write_civ(civ_command(0x1C, 0x00, None, self.radio_addr))
                            tick += 1
                            poll_sleep = 0.25 if getattr(self, 'brand', 'icom') == 'yaesu' else 0.08
                            await asyncio.sleep(poll_sleep)
                            continue

                        if getattr(self, 'brand', 'icom') == 'yaesu':
                            # Yaesu FT-891: Rauhallinen tahti CAT-väylälle kaiutin-naputuksen estämiseksi.
                            # AI1; huolehtii taajuuden (FA) ja moden (MD) automaattisesta päivityksestä nupista käännettäessä.
                            step = tick % 6
                            if step in (0, 2, 4):
                                self.write_raw(b"SM0;")
                            elif step == 1:
                                self.write_raw(b"TX;")
                            elif step == 3:
                                self.write_raw(b"FA;")
                            elif step == 5:
                                self.write_raw(b"MD0;")
                        else:
                            # Icom polling
                            if now - self.last_scope_time > 2.0:
                                self._enable_scope_hw()
                                self.last_scope_time = now - 0.5

                            wait_guard = 0
                            while time.monotonic() < getattr(self, 'scope_burst_until', 0.0) and wait_guard < 8:
                                await asyncio.sleep(0.004)
                                wait_guard += 1

                            step = tick % 10
                            if step == 0:
                                self.write_civ(civ_command(0x03, None, None, self.radio_addr))
                            elif step == 3:
                                self.write_civ(civ_command(0x04, None, None, self.radio_addr))
                            elif step == 6:
                                self.write_civ(civ_command(0x14, 0x0A, None, self.radio_addr))
                            elif step == 9:
                                if self.radio_addr == 0xA2:
                                    b_main = _ic9700_band(self.state.get('main_hz', 0))
                                    b_sub = _ic9700_band(self.state.get('sub_hz', 0))
                                    if b_main and b_main == b_sub and (now - getattr(self, 'last_dual_sync_time', 0.0) > 2.5):
                                        self.last_dual_sync_time = now
                                        asyncio.create_task(self._sync_ic9700_dual())
                                    else:
                                        sub_cycle = (tick // 10) % 3
                                        if sub_cycle == 0:
                                            self.write_civ(civ_command(0x07, 0xD2, None, self.radio_addr))
                                        elif sub_cycle == 1:
                                            self.write_civ(civ_command(0x16, 0x59, None, self.radio_addr))
                                        else:
                                            self.write_civ(civ_command(0x1A, 0x06, None, self.radio_addr))
                                else:
                                    self.write_civ(civ_command(0x1A, 0x06, None, self.radio_addr))
                            else:
                                self.write_civ(civ_command(0x15, 0x02, None, self.radio_addr))
                    tick += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.debug(f"Radio {self.radio_id} poll error: {e}")
            poll_sleep = 0.25 if getattr(self, 'brand', 'icom') == 'yaesu' else 0.075
            await asyncio.sleep(poll_sleep)

    async def set_ptt(self, ptt_on: bool, is_cat: bool = False):
        tila = 1 if ptt_on else 0
        self.user_ptt = (tila == 1) and (not is_cat)
        self.cat_ptt = is_cat and (tila == 1)
        if tila == 1:
            self.last_ptt_on_time = time.monotonic()
        else:
            self.last_ptt_off_time = time.monotonic()
            self.ptt_release_until = time.monotonic() + 0.8
        self.state['ptt'] = tila

        # 1. Lähetetään PTT-komento radiolle VÄLITTÖMÄSTI (0 ms viiveellä ennen äänivirtojen sulkemista!),
        # jotta radion T/R-rele päästää vastaanotolle samalla millisekunnilla kun PTT vapautetaan:
        if self.ser and self.ser.is_open and self.state['online']:
            try:
                if getattr(self, 'brand', 'icom') == 'yaesu':
                    self.write_raw(b"TX1;" if tila else b"TX0;")
                else:
                    self.write_civ(civ_command(0x1C, 0x00, bytes([tila]), self.radio_addr))
                    if tila == 1:
                        await asyncio.sleep(0.015)
                        self.write_civ(civ_command(0x1C, 0x00, bytes([tila]), self.radio_addr))
            except Exception:
                pass

        # 2. Käsitellään TX/RX-äänivirrat ilman että PortAudion sulkeminen blokkaa säiettä:
        if tila == 0:
            if self.radio_id in audio_manager.channels:
                audio_manager.channels[self.radio_id]._stop_tx_stream(async_close=True)
            if self.ser and self.ser.is_open and self.state['online'] and getattr(self, 'brand', 'icom') != 'yaesu':
                await asyncio.sleep(0.012)
                self.write_civ(civ_command(0x1C, 0x00, bytes([0x00]), self.radio_addr))
        elif tila == 1 and not self.cat_ptt:
            if self.radio_id in audio_manager.channels:
                audio_manager.channels[self.radio_id]._start_tx_stream()
        await broadcast_state(self.radio_id)

    def set_ptt_sync(self, ptt_val: int, is_cat: bool = False):
        tila = 1 if ptt_val else 0
        self.user_ptt = (tila == 1) and (not is_cat)
        self.cat_ptt = is_cat and (tila == 1)
        if tila == 1:
            self.last_ptt_on_time = time.monotonic()
        else:
            self.last_ptt_off_time = time.monotonic()
            self.ptt_release_until = time.monotonic() + 0.8
        self.state['ptt'] = tila

        if self.ser and self.ser.is_open and self.state.get('online'):
            with self.ser_lock:
                try:
                    if getattr(self, 'brand', 'icom') == 'yaesu':
                        cmd = b"TX1;" if tila else b"TX0;"
                        self.ser.write(cmd)
                        if tila == 1:
                            time.sleep(0.015)
                            self.ser.write(cmd)
                    else:
                        cmd = civ_command(0x1C, 0x00, bytes([tila]), self.radio_addr)
                        self.ser.write(cmd)
                        time.sleep(0.020)
                        self.ser.write(cmd)
                except Exception as e:
                    logging.error(f"set_ptt_sync error on radio {self.radio_id}: {e}")

        if tila == 0:
            if self.radio_id in audio_manager.channels:
                audio_manager.channels[self.radio_id]._stop_tx_stream(async_close=True)
        elif tila == 1 and not self.cat_ptt:
            if self.radio_id in audio_manager.channels:
                audio_manager.channels[self.radio_id]._start_tx_stream()
        if app_loop and not app_loop.is_closed():
            asyncio.run_coroutine_threadsafe(broadcast_state(self.radio_id), app_loop)

    def set_frequency_sync(self, hz: int):
        self.state['taajuus_hz'] = hz
        self.state['taajuus_mhz'] = f"{(hz / 1000000):.6f}"
        if self.current_band == 1:
            self.state['sub_hz'] = hz
        else:
            self.state['main_hz'] = hz
        if self.ser and self.ser.is_open and self.state.get('online'):
            with self.ser_lock:
                try:
                    if getattr(self, 'brand', 'icom') == 'yaesu':
                        self.ser.write(f"FA{hz:09d};".encode('ascii'))
                    else:
                        self.ser.write(civ_command(0x05, None, hz_to_bcd(hz), to_addr=self.radio_addr))
                        time.sleep(0.025)
                except Exception as e:
                    logging.error(f"set_frequency_sync error on radio {self.radio_id}: {e}")
        if app_loop and not app_loop.is_closed():
            asyncio.run_coroutine_threadsafe(broadcast_state(self.radio_id), app_loop)

    def set_mode_sync(self, mode_str: str, filter_val: int = 1):
        self.state['mode'] = mode_str
        self.state['filter'] = filter_val
        self.state['data_on'] = ('-D' in mode_str or 'PKT' in mode_str or 'DATA' in mode_str)
        if self.current_band == 1:
            self.state['sub_mode'] = mode_str
            self.state['sub_filter'] = filter_val
        else:
            self.state['main_mode'] = mode_str
            self.state['main_filter'] = filter_val
        if self.ser and self.ser.is_open and self.state.get('online'):
            with self.ser_lock:
                try:
                    if getattr(self, 'brand', 'icom') == 'yaesu':
                        if mode_str == "LSB":
                            self.yaesu_ssb_bfo = '1'
                        elif mode_str == "USB":
                            self.yaesu_ssb_bfo = '0'
                        y_map = {
                            "LSB": "EX11071;MD01;EX11071;", "USB": "EX11070;MD02;EX11070;",
                            "CW": "MD03;", "FM": "MD04;", "AM": "MD05;", "RTTY": "MD09;",
                            "DATA-USB": "MD0C;", "USB-D": "MD0C;", "PKTUSB": "MD0C;", "DATA": "MD0C;", "D-U": "MD0C;",
                            "DATA-LSB": "MD08;", "LSB-D": "MD08;", "PKTLSB": "MD08;", "D-L": "MD08;",
                            "DATA-FM": "MD0A;", "FM-D": "MD0A;", "PKTFM": "MD0A;", "D-F": "MD0A;"
                        }
                        cmd = y_map.get(mode_str, 'EX11070;MD02;EX11070;')
                        self.ser.write(cmd.encode('ascii'))
                    else:
                        m_base = mode_str.split('-')[0].upper().replace("PKT", "")
                        mode_map = {'LSB': 0x00, 'USB': 0x01, 'AM': 0x02, 'CW': 0x03, 'RTTY': 0x04, 'FM': 0x05, 'WFM': 0x06, 'CW-R': 0x07, 'RTTY-R': 0x08, 'DV': 0x17}
                        code = mode_map.get(m_base, 0x01)
                        is_data = 1 if ('-D' in mode_str or 'PKT' in mode_str or 'DATA' in mode_str) else 0
                        self.ser.write(civ_command(0x06, None, bytes([code, filter_val]), to_addr=self.radio_addr))
                        if m_base in ("LSB", "USB", "FM"):
                            time.sleep(0.02)
                            self.ser.write(civ_command(0x1A, 0x06, bytes([is_data, filter_val]), to_addr=self.radio_addr))
                except Exception as e:
                    logging.error(f"set_mode_sync error on radio {self.radio_id}: {e}")
        if app_loop and not app_loop.is_closed():
            asyncio.run_coroutine_threadsafe(broadcast_state(self.radio_id), app_loop)

    async def apply_repeater_settings(self, dup=None, offset_mhz=None, tone_mode=None, ctcss=None):
        """Asettaa VHF/UHF-toistinasetukset (DUP-/+, erotustaajuus, Tone/TSQL/1750Hz ja CTCSS-aliääni) radiolle."""
        if dup is not None:
            self.state['dup'] = str(dup).upper()
        if offset_mhz is not None:
            try:
                self.state['offset_mhz'] = round(abs(float(offset_mhz)), 3)
            except Exception:
                pass
        if tone_mode is not None:
            self.state['tone_mode'] = str(tone_mode)
        if ctcss is not None:
            try:
                self.state['ctcss'] = round(float(ctcss), 1)
            except Exception:
                pass

        if not self.ser or not self.ser.is_open or not self.state.get('online') or getattr(self, 'brand', 'icom') == 'yaesu':
            return

        addr = self.radio_addr
        try:
            # 1. Duplex-suunta (0F: 10=Simplex/OFF, 11=DUP-, 12=DUP+)
            if dup is not None:
                d_str = self.state['dup']
                d_sub = 0x11 if d_str == 'DUP-' else (0x12 if d_str == 'DUP+' else 0x10)
                self.write_civ(civ_command(0x0F, d_sub, None, addr))
                await asyncio.sleep(0.03)

            # 2. Duplex-erotustaajuus (0D: 3 tavua BCD)
            if offset_mhz is not None and self.state['offset_mhz'] > 0:
                off_hz = int(round(self.state['offset_mhz'] * 1000000))
                self.write_civ(civ_command(0x0D, None, offset_hz_to_bcd3(off_hz), addr))
                await asyncio.sleep(0.03)

            # 3. CTCSS-aliäänen taajuus (1B 00 = Repeater Tone, 1B 01 = TSQL)
            if ctcss is not None or tone_mode in ('TONE', 'TSQL'):
                c_bytes = ctcss_to_bcd3(self.state.get('ctcss', 123.0))
                self.write_civ(civ_command(0x1B, 0x00, c_bytes, addr))
                await asyncio.sleep(0.03)
                if self.state.get('tone_mode') == 'TSQL':
                    self.write_civ(civ_command(0x1B, 0x01, c_bytes, addr))
                    await asyncio.sleep(0.03)

            # 4. Tone-tila (16 42 = Repeater Tone, 16 43 = TSQL)
            if tone_mode is not None:
                tm = self.state.get('tone_mode', 'OFF')
                if tm == 'TONE':
                    self.write_civ(civ_command(0x16, 0x43, bytes([0x00]), addr))
                    await asyncio.sleep(0.025)
                    self.write_civ(civ_command(0x16, 0x42, bytes([0x01]), addr))
                    await asyncio.sleep(0.025)
                elif tm == 'TSQL':
                    self.write_civ(civ_command(0x16, 0x42, bytes([0x00]), addr))
                    await asyncio.sleep(0.025)
                    self.write_civ(civ_command(0x16, 0x43, bytes([0x01]), addr))
                    await asyncio.sleep(0.025)
                else:
                    # 'OFF' tai '1750Hz' (1750Hz lähetetään erillisellä TONE-avauspiippinapilla)
                    self.write_civ(civ_command(0x16, 0x42, bytes([0x00]), addr))
                    await asyncio.sleep(0.025)
                    self.write_civ(civ_command(0x16, 0x43, bytes([0x00]), addr))
                    await asyncio.sleep(0.025)
        except Exception as e:
            logging.debug(f"Radio {self.radio_id} apply_repeater_settings error: {e}")

    async def set_tone_burst(self, active: bool, freq_hz: float = 1750.0):
        """Lähettää 1750 Hz avauspiipin (Tone Burst) toistimelle kytkemällä PTT:n ja syöttämällä 1750 Hz siniaallon TX-audioon."""
        ch = audio_manager.channels.get(self.radio_id) if 'audio_manager' in globals() else None
        if active:
            self.state['tone_burst'] = 1
            if ch:
                ch.tone_burst_freq = float(freq_hz or 1750.0)
                ch.tone_burst_phase = 0.0
                ch.tone_burst_active = True
            # Varmistetaan IC-9700/IC-7300 DATA OFF MOD = MIC, USB (04), jotta sekä käsimikki että USB-ääni moduloivat FM/SSB-tilassa
            if self.ser and self.ser.is_open and self.state.get('online') and getattr(self, 'brand', 'icom') != 'yaesu':
                self.write_civ(civ_command(0x1A, 0x05, bytes([0x01, 0x15, 0x04]), self.radio_addr))
                await asyncio.sleep(0.02)
            await self.set_ptt(True)
            if ch:
                ch._start_tx_stream(force=True)
        else:
            self.state['tone_burst'] = 0
            if ch:
                ch.tone_burst_active = False
            await self.set_ptt(False)


radios = {
    "1": RadioController("1", default_addr=0x94),
    "2": RadioController("2", default_addr=0xA2),
    "3": RadioController("3", default_addr=0x94)
}

ws_clients = set()
ws_lock = asyncio.Lock()


async def broadcast_ws(msg_str: str):
    if not ws_clients:
        return
    async with ws_lock:
        dead = []
        for ws in list(ws_clients):
            try:
                await ws.send_str(msg_str)
            except Exception:
                dead.append(ws)
        for ws in dead:
            ws_clients.discard(ws)


async def broadcast_state(radio_id: str = None):
    if not ws_clients:
        return
    if radio_id:
        r = radios[radio_id]
        msg = json.dumps({
            "type": "tila",
            "radio_id": radio_id,
            "state": r.state,
            "all_states": {k: v.state for k, v in radios.items()}
        })
        await broadcast_ws(msg)
    else:
        for rid, r in radios.items():
            msg = json.dumps({
                "type": "tila",
                "radio_id": rid,
                "state": r.state,
                "all_states": {k: v.state for k, v in radios.items()}
            })
            await broadcast_ws(msg)


class RadioAudioChannel:
    """Yhden radion äänikanava (tukee selaimen WebSocket-ääntä, PC:n kaiutin/mikrofonireititystä sekä suoraa radion äänitallennusta)."""
    def __init__(self, radio_id: str):
        self.radio_id = str(radio_id)
        self.rx_stream = None
        self.rx_stream_dev = None
        self.tx_stream = None
        self.tx_lock = threading.Lock()
        self.ws_clients = set()
        self.tx_q = queue.Queue(maxsize=120)
        self.tx_buffer = np.array([], dtype=np.float32)
        self.spk_q = queue.Queue(maxsize=120)
        self.spk_buffer = np.array([], dtype=np.float32)
        self.sample_rate = 16000
        self.rx_vol = 1.0
        self.last_af_scope_time = 0.0
        self.af_window = np.hanning(4096).astype(np.float32)
        self.af_ring = np.zeros(4096, dtype=np.float32)
        self.af_noise_db = -62.0
        rconf = global_config["radios"].get(self.radio_id, {})
        self.sql_pct = float(rconf.get("sql_pct", 0) or 0)
        self.sql_last_open = 0.0
        self.active = False
        self.local_active = False
        self.recording = False
        self.rec_chunks = []
        self.rec_start_time = 0.0
        self.rx_dev = None
        self.tx_dev = None
        self.spk_dev = None
        self.mic_dev = None
        self.tone_burst_active = False
        self.tone_burst_freq = 1750.0
        self.tone_burst_phase = 0.0

    def set_volume(self, vol_pct: float):
        self.rx_vol = max(0.0, min(2.0, float(vol_pct) / 100.0))
        global_config["radios"][self.radio_id]["audio_rx_vol"] = int(vol_pct)

    def set_squelch(self, sql_pct: float):
        self.sql_pct = max(0.0, min(100.0, float(sql_pct)))
        val_int = int(round(self.sql_pct))
        global_config["radios"][self.radio_id]["sql_pct"] = val_int
        if self.radio_id in radios:
            radios[self.radio_id].state["sql_pct"] = val_int
            if self.sql_pct > 0.0 and float(radios[self.radio_id].state.get("smeter_pct", 0.0)) >= self.sql_pct:
                self.sql_last_open = time.monotonic()

    def is_squelch_open(self) -> bool:
        if self.sql_pct <= 0.0:
            return True
        r_state = radios[self.radio_id].state
        if not r_state.get("online", False):
            return False
        now_m = time.monotonic()
        if float(r_state.get("smeter_pct", 0.0)) >= self.sql_pct:
            self.sql_last_open = now_m
            return True
        return (now_m - self.sql_last_open) <= 0.35

    def feed_tx_samples(self, samples_f32: np.ndarray):
        r = radios.get(self.radio_id)
        # Syötetään mikrofonin ääntä VAIN kun käyttäjä itse pitää tämän ohjelman PTT:tä pohjassa (user_ptt=True).
        # Huom! Älä koskaan avaa PortAudio-virtaa (_start_tx_stream) tästä C-tason audiocallback-säikeestä,
        # sillä se aiheuttaa natiivin muistivirheen (kaatumisen) kun PTT:tä painellaan toistuvasti!
        if r and getattr(r, 'user_ptt', False) and r.state.get('ptt') == 1 and self.active:
            if getattr(r, 'cat_ptt', False):
                return
            if not self.tx_q.full():
                try:
                    self.tx_q.put_nowait(samples_f32.copy())
                except Exception:
                    pass

    def _ensure_rx_stream(self):
        """Varmistaa että radion USB RX -sisääntulovirta on auki kuuntelua ja/tai tallennusta varten."""
        if self.rx_dev is None:
            saved_rx = global_config["radios"].get(self.radio_id, {}).get("audio_rx_dev", "")
            if saved_rx:
                try:
                    self.rx_dev = int(saved_rx)
                except Exception:
                    pass
        if self.rx_dev is None:
            return
        if self.rx_stream is not None and self.rx_stream_dev == self.rx_dev:
            try:
                if self.rx_stream.active:
                    return
            except Exception:
                pass
            try:
                self.rx_stream.stop()
                self.rx_stream.close()
            except Exception:
                pass
            self.rx_stream = None

        def rx_callback(indata, frames, time_info, status):
            if indata.shape[1] >= 2:
                mono = ((indata[:, 0] + indata[:, 1]) * 0.65).astype(np.float32)
            else:
                mono = indata[:, 0].copy()

            sql_open = self.is_squelch_open()
            gated_mono = mono if sql_open else np.zeros_like(mono)

            # 1. REITITETÄÄN RX-ÄÄNI KAIUTTIMILLE JA WEB-ASIAKKAILLE ENSIMMÄISENÄ (0 ms viiveellä ennen FFT/APRS-laskentaa!):
            if self.active:
                if self.local_active and self.spk_dev is not None and radios[self.radio_id].state.get('ptt') != 1:
                    scaled = (gated_mono * self.rx_vol).astype(np.float32)
                    if self.spk_q.full():
                        try:
                            self.spk_q.get_nowait()
                        except Exception:
                            pass
                    try:
                        self.spk_q.put_nowait(scaled)
                    except Exception:
                        pass
                if self.ws_clients and app_loop and not app_loop.is_closed():
                    data_bytes = gated_mono.tobytes()
                    for ws in list(self.ws_clients):
                        if not ws.closed:
                            try:
                                asyncio.run_coroutine_threadsafe(ws.send_bytes(data_bytes), app_loop)
                            except Exception:
                                pass

            # 2. Tallennetaan vain ja ainoastaan tästä radiosta tuleva ääni (16-bit PCM)
            if self.recording:
                pcm16 = (np.clip(gated_mono * self.rx_vol, -1.0, 1.0) * 32767.0).astype(np.int16)
                self.rec_chunks.append(pcm16.tobytes())

            # 3. APRS demodulation
            if 'aprs_engine' in globals() and aprs_engine and aprs_engine.rx_enabled and str(self.radio_id) == str(aprs_engine.radio_id):
                try:
                    aprs_engine.feed_audio(self.radio_id, mono)
                except Exception:
                    pass

            # 4. Audio FFT Spectrum & Waterfall for radios without hardware scope (e.g. Yaesu FT-891)
            r = radios.get(self.radio_id)
            if r and (not getattr(r, 'has_hw_scope', True) or getattr(r, 'brand', '') == 'yaesu'):
                try:
                    n_s = len(mono)
                    if n_s >= 4096:
                        self.af_ring[:] = mono[-4096:]
                    elif n_s > 0:
                        self.af_ring[:-n_s] = self.af_ring[n_s:]
                        self.af_ring[-n_s:] = mono
                except Exception:
                    pass

                now_t = time.monotonic()
                if (now_t - getattr(self, 'last_af_scope_time', 0.0)) >= 0.045:
                    self.last_af_scope_time = now_t
                    try:
                        # 4096-pisteen Hanning-ikkunoitu ja normalisoitu FFT (resoluutio 3.9 Hz / bin, 0..3200 Hz = 820 biniä)
                        windowed = (self.af_ring - np.mean(self.af_ring)) * self.af_window
                        fft = np.abs(np.fft.rfft(windowed)) / 1024.0
                        fft_sub = fft[:820]
                        x_orig = np.linspace(0, 3200, len(fft_sub))
                        x_new = np.linspace(0, 3200, 475)
                        interp = np.interp(x_new, x_orig, fft_sub)
                        db = 20.0 * np.log10(np.maximum(interp, 1e-7))

                        # Adaptiivinen taustakohinatason seuranta SSB-päästökaistalta (250..3000 Hz = binit 35..445)
                        passband_floor = float(np.percentile(db[35:445], 25))
                        passband_floor = max(-92.0, min(-22.0, passband_floor))
                        alpha = 0.25 if passband_floor < self.af_noise_db else 0.05
                        self.af_noise_db = (1.0 - alpha) * self.af_noise_db + alpha * passband_floor

                        snr_db = db - self.af_noise_db
                        # Vaimennetaan <120 Hz DC/50Hz hurinapiikit (binit 0..18)
                        snr_db[:18] *= np.linspace(0.0, 1.0, 18)

                        # Skaalataan SNR (dB yli kohinatason) -> 0..160 (sama skaala kuin Icomin CI-V vesiputouksessa):
                        scaled_fft = np.round(np.clip((snr_db - 1.5) * 2.3, 0.0, 160.0), 1).tolist()
                        ws_msg = json.dumps({
                            "type": "scope",
                            "radio_id": self.radio_id,
                            "div": 0x11,
                            "span_hz": 3200,
                            "center_mode": False,
                            "edge_low": 0,
                            "edge_high": 3200,
                            "data": scaled_fft
                        })
                        if app_loop and not app_loop.is_closed():
                            asyncio.run_coroutine_threadsafe(broadcast_ws(ws_msg), app_loop)
                    except Exception:
                        pass

        dev_info = sd.query_devices(self.rx_dev)
        in_ch = 2 if int(dev_info.get('max_input_channels', 1)) >= 2 else 1
        self.rx_stream = sd.InputStream(
            samplerate=self.sample_rate, channels=in_ch, dtype='float32',
            device=self.rx_dev, callback=rx_callback, blocksize=256, latency='low'
        )
        self.rx_stream.start()
        self.rx_stream_dev = self.rx_dev

    def start_recording(self, rx_id=None, vol_pct=None):
        """Käynnistää välittömästi radion oman RX-äänen tallennuksen muistiin."""
        if vol_pct is not None:
            self.set_volume(vol_pct)
        if rx_id is not None and str(rx_id) != "":
            self.rx_dev = int(rx_id)
        elif self.rx_dev is None:
            saved_rx = global_config["radios"].get(self.radio_id, {}).get("audio_rx_dev", "")
            if str(saved_rx) != "":
                self.rx_dev = int(saved_rx)

        if self.rx_dev is None:
            raise ValueError("No Radio RX Input device selected")

        global_config["radios"][self.radio_id]["audio_rx_dev"] = str(self.rx_dev)
        save_config(global_config)

        self.rec_chunks = []
        self.rec_start_time = time.time()
        self.recording = True
        self._ensure_rx_stream()

    def stop_recording(self) -> bytes:
        """Lopettaa tallennuksen ja palauttaa kertyneen 16-bit PCM -datan."""
        self.recording = False
        pcm_bytes = b"".join(self.rec_chunks)
        self.rec_chunks = []
        if not self.active and self.rx_stream is not None:
            try:
                self.rx_stream.stop()
                self.rx_stream.close()
            except Exception:
                pass
            self.rx_stream = None
            self.rx_stream_dev = None
        return pcm_bytes

    def _start_tx_stream(self, force: bool = False):
        """Käynnistää TX-äänivirran vain kun PC:n tai webin mikrofoni lähettää puhetta (tai 1750 Hz Tone Burst lähetetään)."""
        with self.tx_lock:
            if self.tx_dev is None:
                saved_tx = global_config["radios"].get(self.radio_id, {}).get("audio_tx_dev", "")
                if str(saved_tx).strip() != "":
                    try:
                        self.tx_dev = int(saved_tx)
                    except Exception:
                        pass
                if self.tx_dev is None and force:
                    try:
                        for idx, d in enumerate(sd.query_devices()):
                            if int(d.get('max_output_channels', 0)) > 0 and 'usb audio codec' in d.get('name', '').lower():
                                self.tx_dev = idx
                                break
                    except Exception:
                        pass
            if self.tx_dev is None or (not self.active and not force and not getattr(self, 'tone_burst_active', False)):
                return
            r = radios.get(self.radio_id)
            if r and getattr(r, 'cat_ptt', False) and not force:
                return
            if self.tx_stream is not None:
                try:
                    if self.tx_stream.active:
                        return
                except Exception:
                    pass
                try:
                    self.tx_stream.stop()
                    self.tx_stream.close()
                except Exception:
                    pass
                self.tx_stream = None

            while not self.tx_q.empty():
                try: self.tx_q.get_nowait()
                except Exception: break
            # Matalan viiveen puskuri (256 näytettä = 16 ms)
            self.tx_buffer = np.zeros(256, dtype=np.float32)

            def tx_callback(outdata, frames, time_info, status):
                r_curr = radios.get(self.radio_id)
                if r_curr and r_curr.state.get('ptt') != 1:
                    outdata.fill(0)
                    return
                if getattr(self, 'tone_burst_active', False):
                    freq = float(getattr(self, 'tone_burst_freq', 1750.0) or 1750.0)
                    t = np.arange(frames, dtype=np.float32) / float(self.sample_rate)
                    wave = (0.70 * np.sin(2.0 * np.pi * freq * t + self.tone_burst_phase)).astype(np.float32)
                    self.tone_burst_phase = float((self.tone_burst_phase + 2.0 * np.pi * freq * frames / float(self.sample_rate)) % (2.0 * np.pi))
                    outdata[:, 0] = wave
                    return
                while not self.tx_q.empty():
                    try:
                        self.tx_buffer = np.concatenate((self.tx_buffer, self.tx_q.get_nowait()))
                    except Exception:
                        break
                # Estetään puskurin liiallinen kasvu
                if len(self.tx_buffer) > frames * 4:
                    self.tx_buffer = self.tx_buffer[-frames * 2:]
                if len(self.tx_buffer) >= frames:
                    outdata[:, 0] = self.tx_buffer[:frames]
                    self.tx_buffer = self.tx_buffer[frames:]
                elif len(self.tx_buffer) > 0:
                    l = len(self.tx_buffer)
                    outdata[:l, 0] = self.tx_buffer
                    outdata[l:, 0] = 0
                    self.tx_buffer = np.array([], dtype=np.float32)
                else:
                    outdata.fill(0)

            try:
                self.tx_stream = sd.OutputStream(
                    samplerate=self.sample_rate, channels=1, dtype='float32',
                    device=self.tx_dev, callback=tx_callback, blocksize=256, latency='low'
                )
                self.tx_stream.start()
            except Exception as e:
                logging.error(f"Radio {self.radio_id} TX Stream start error: {e}")

    def _stop_tx_stream(self, async_close: bool = False):
        """Sulkee TX-äänivirran välittömästi ilman että Windowsin ääniajuri viivästyttää RX-äänen palautumista."""
        while not self.tx_q.empty():
            try: self.tx_q.get_nowait()
            except Exception: break
        self.tx_buffer = np.array([], dtype=np.float32)
        # Tyhjennetään myös kaiutinpuskuri vanhasta hiljaisuudesta, jotta tuore RX-ääni kuuluu heti ensimmäisestä 16 ms blokista!
        while not self.spk_q.empty():
            try: self.spk_q.get_nowait()
            except Exception: break
        self.spk_buffer = np.array([], dtype=np.float32)

        def _close_now():
            with self.tx_lock:
                st = self.tx_stream
                self.tx_stream = None
                if st is not None:
                    try:
                        st.abort()
                    except Exception:
                        try: st.stop()
                        except Exception: pass
                    try:
                        st.close()
                    except Exception:
                        pass

        if async_close:
            threading.Thread(target=_close_now, daemon=True).start()
        else:
            _close_now()

    def start(self, rx_id=None, tx_id=None, spk_id=None, mic_id=None, vol_pct=100, local=True):
        if local:
            self.local_active = True
        self.set_volume(vol_pct)
        rconf = global_config["radios"][self.radio_id]

        if rx_id is None or str(rx_id).strip() == "":
            rx_id = rconf.get("audio_rx_dev")
        if tx_id is None or str(tx_id).strip() == "":
            tx_id = rconf.get("audio_tx_dev")
        if spk_id is None:
            spk_id = rconf.get("audio_spk_dev")
        if mic_id is None:
            mic_id = rconf.get("audio_mic_dev")

        self.rx_dev = int(rx_id) if (rx_id is not None and str(rx_id).strip() != "") else None
        self.tx_dev = int(tx_id) if (tx_id is not None and str(tx_id).strip() != "") else None
        self.spk_dev = int(spk_id) if (spk_id is not None and str(spk_id).strip() != "") else None
        self.mic_dev = int(mic_id) if (mic_id is not None and str(mic_id).strip() != "") else None

        # Estetään vahingossa ristiin valitut Radio TX Output ja PC Speaker -laitteet:
        # Jos PC Speaker (spk_dev) osoittaa samaan radion USB-äänikorttiin kuin Radio RX Input (esim. SignaLink),
        # radion oma RX-kohina syötettäisiin suoraan takaisin SignaLinkiin, mikä laukaisee SignaLinkin VOX-PTT:n jatkuvasti päälle!
        if self.rx_dev is not None and self.spk_dev is not None:
            try:
                rx_name = sd.query_devices(self.rx_dev).get('name', '').strip().lower()
                spk_name = sd.query_devices(self.spk_dev).get('name', '').strip().lower()
                tx_name = sd.query_devices(self.tx_dev).get('name', '').strip().lower() if self.tx_dev is not None else ''
                rx_codec = rx_name.split('(')[-1].strip(' )') if '(' in rx_name else rx_name
                spk_codec = spk_name.split('(')[-1].strip(' )') if '(' in spk_name else spk_name
                tx_codec = tx_name.split('(')[-1].strip(' )') if '(' in tx_name else tx_name
                if rx_codec and spk_codec and (rx_name == spk_name or (len(rx_codec) >= 5 and rx_codec == spk_codec)):
                    if self.tx_dev is not None and tx_codec != rx_codec:
                        logging.warning(f"[Audio] Radio {self.radio_id}: Swapped TX ({self.tx_dev}) and Speaker ({self.spk_dev}) to prevent SignaLink VOX loopback!")
                        self.tx_dev, self.spk_dev = self.spk_dev, self.tx_dev
                    else:
                        logging.warning(f"[Audio] Radio {self.radio_id}: Disabled Speaker ({self.spk_dev}) because it matches Radio RX codec ({rx_name}) and would cause VOX loopback!")
                        self.spk_dev = None
            except Exception:
                pass

        # Alustetaan radion oma kaiutinpuskuri pienellä 1 blokin (64 ms) puskurityynyllä,
        # jotta kahden eri USB-äänikortin kellovaihe-ero ei aiheuta pätkimistä.
        while not self.spk_q.empty():
            try:
                self.spk_q.get_nowait()
            except Exception:
                break
        self.spk_buffer = np.zeros(256, dtype=np.float32)

        if local:
            rconf["audio_rx_dev"] = "" if self.rx_dev is None else str(self.rx_dev)
            rconf["audio_tx_dev"] = "" if self.tx_dev is None else str(self.tx_dev)
            rconf["audio_spk_dev"] = "" if self.spk_dev is None else str(self.spk_dev)
            rconf["audio_mic_dev"] = "" if self.mic_dev is None else str(self.mic_dev)
            save_config(global_config)

        # 1. Radion RX-ulostulon kaappaus (Radio -> PC/Web, tukee myös IC-9700 2-kanavaista Main+Sub ääntä)
        if self.rx_dev is not None:
            try:
                self._ensure_rx_stream()
            except Exception as e:
                logging.error(f"Radio {self.radio_id} RX Stream error: {e}")

        # 2. Radion TX-sisääntulon syöttö (PC/Web -> Radio)
        # TX-äänivirta pidetään oletuksena suljettuna ja avataan ainoastaan tarvittaessa
        # puhetta lähetettäessä (_start_tx_stream), jolloin WSJT-X voi vapaasti lähettää FT8-ääntä.
        self._stop_tx_stream()

        self.active = True
        audio_manager.refresh_shared_devices()

    def stop(self, keep_recording: bool = False, local_only: bool = False):
        if local_only:
            self.local_active = False
        else:
            self.local_active = False
            self.active = False

        if not self.local_active and len(self.ws_clients) == 0 and not keep_recording and not self.recording:
            self.active = False
            if self.rx_stream:
                try:
                    self.rx_stream.stop()
                    self.rx_stream.close()
                except Exception:
                    pass
                self.rx_stream = None
                self.rx_stream_dev = None

        self._stop_tx_stream()
        while not self.spk_q.empty():
            try:
                self.spk_q.get_nowait()
            except Exception:
                break
        self.spk_buffer = np.array([], dtype=np.float32)
        audio_manager.refresh_shared_devices()


class DualAudioManager:
    """Hallitsee molempien radioiden äänikanavia sekä jaettuja PC-mikrofoni- ja kaiutinulostuloja."""
    def __init__(self):
        self.channels = {
            "1": RadioAudioChannel("1"),
            "2": RadioAudioChannel("2"),
            "3": RadioAudioChannel("3")
        }
        self.shared_mics = {}      # dev_id -> sd.InputStream
        self.shared_speakers = {}  # dev_id -> sd.OutputStream
        self.sample_rate = 16000

    def refresh_shared_devices(self):
        # Selvitetään mitkä paikalliset mikrofonit ja kaiuttimet ovat aktiivisina PC:llä
        needed_mics = set()
        needed_spks = set()
        for ch in self.channels.values():
            if ch.active and ch.local_active:
                if ch.mic_dev is not None:
                    needed_mics.add(ch.mic_dev)
                if ch.spk_dev is not None:
                    needed_spks.add(ch.spk_dev)

        # Suljetaan tarpeettomat mikrofonit
        for dev_id in list(self.shared_mics.keys()):
            if dev_id not in needed_mics:
                try:
                    self.shared_mics[dev_id].stop()
                    self.shared_mics[dev_id].close()
                except Exception:
                    pass
                del self.shared_mics[dev_id]

        # Avataan uudet jaetut mikrofonit (sama mikrofoni voi ohjata molempia radioita yhtä aikaa!)
        for dev_id in needed_mics:
            if dev_id not in self.shared_mics:
                def make_mic_cb(d_id):
                    def mic_cb(indata, frames, time_info, status):
                        mono = indata[:, 0].copy()
                        for ch in self.channels.values():
                            if ch.active and ch.mic_dev == d_id:
                                ch.feed_tx_samples(mono)
                    return mic_cb
                try:
                    st = sd.InputStream(
                        samplerate=self.sample_rate, channels=1, dtype='float32',
                        device=dev_id, callback=make_mic_cb(dev_id), blocksize=256, latency='low'
                    )
                    st.start()
                    self.shared_mics[dev_id] = st
                except Exception as e:
                    logging.error(f"Shared Mic ({dev_id}) error: {e}")

        # Suljetaan tarpeettomat kaiuttimet
        for dev_id in list(self.shared_speakers.keys()):
            if dev_id not in needed_spks:
                try:
                    self.shared_speakers[dev_id].stop()
                    self.shared_speakers[dev_id].close()
                except Exception:
                    pass
                del self.shared_speakers[dev_id]

        # Avataan uudet jaetut kaiuttimet/kuulokkeet (aito reaaliaikainen summamiksaus molemmille radioille!)
        for dev_id in needed_spks:
            if dev_id not in self.shared_speakers:
                def make_spk_cb(d_id):
                    def spk_cb(outdata, frames, time_info, status):
                        mix = np.zeros(frames, dtype=np.float32)
                        any_audio = False
                        for ch in self.channels.values():
                            if not ch.active or ch.spk_dev != d_id:
                                continue
                            if radios[ch.radio_id].state.get('ptt') == 1:
                                while not ch.spk_q.empty():
                                    try: ch.spk_q.get_nowait()
                                    except Exception: break
                                ch.spk_buffer = np.array([], dtype=np.float32)
                                continue
                            while not ch.spk_q.empty():
                                try:
                                    chunk = ch.spk_q.get_nowait()
                                    ch.spk_buffer = np.concatenate((ch.spk_buffer, chunk))
                                except Exception:
                                    break
                            # Pidetään kaiutinpuskuri tiukkana (max ~32-48 ms), jotta RX-ääni kuuluu välittömästi PTT:n vapautuessa!
                            if len(ch.spk_buffer) > frames * 3:
                                ch.spk_buffer = ch.spk_buffer[-frames * 2:]
                            if len(ch.spk_buffer) >= frames:
                                mix += ch.spk_buffer[:frames]
                                ch.spk_buffer = ch.spk_buffer[frames:]
                                any_audio = True
                            elif len(ch.spk_buffer) > 0:
                                l = len(ch.spk_buffer)
                                mix[:l] += ch.spk_buffer
                                ch.spk_buffer = np.array([], dtype=np.float32)
                                any_audio = True
                        if any_audio:
                            outdata[:, 0] = np.clip(mix, -1.0, 1.0)
                        else:
                            outdata.fill(0)
                    return spk_cb

                try:
                    st = sd.OutputStream(
                        samplerate=self.sample_rate, channels=1, dtype='float32',
                        device=dev_id, callback=make_spk_cb(dev_id), blocksize=256, latency='low'
                    )
                    st.start()
                    self.shared_speakers[dev_id] = st
                except Exception as e:
                    logging.error(f"Shared Speaker ({dev_id}) error: {e}")

    def stop_all(self):
        for ch in self.channels.values():
            ch.recording = False
            ch.stop()


audio_manager = DualAudioManager()

# APRS Engine setup
aprs_engine = aprs_modem.AprsEngine(stations_file=os.path.join(BASE_DIR, "aprs_stations.json"))
aprs_engine.configure(global_config.get("aprs", {}))

def resolve_wasapi_output_device(dev_idx: int) -> int:
    """Finds the lower-latency Windows WASAPI output device corresponding to the configured index."""
    try:
        dev_info = sd.query_devices(dev_idx)
        hostapi = sd.query_hostapis(dev_info['hostapi'])
        if hostapi['name'] == 'Windows WASAPI':
            return dev_idx
        target_name = dev_info['name'].lower()
        keywords = [part.strip() for part in target_name.replace('(', ' ').replace(')', ' ').split() if len(part.strip()) > 3]
        for idx, d in enumerate(sd.query_devices()):
            if d['max_output_channels'] > 0:
                h = sd.query_hostapis(d['hostapi'])
                if h['name'] == 'Windows WASAPI':
                    dname = d['name'].lower()
                    if (keywords and all(k in dname for k in keywords)) or (('icom' in target_name or '9700' in target_name) and ('icom' in dname or '9700' in dname)):
                        logging.info(f"[Audio] Resolved dev {dev_idx} ({dev_info['name']}) -> WASAPI dev {idx} ({d['name']})")
                        return idx
    except Exception as e:
        logging.warning(f"[Audio] Could not resolve WASAPI device: {e}")
    return dev_idx


def aprs_tx_audio_handler(radio_id: str, audio_samples: np.ndarray):
    r = radios.get(str(radio_id))
    if not r or not r.state.get('online', False):
        logging.warning(f"APRS TX skipped: radio {radio_id} not online")
        return
    
    # Check Frequency Safety Lock: block transmission if VFO is tuned away from 144.800 MHz FM
    aprs_cfg = global_config.get("aprs", {})
    freq_lock_enabled = aprs_cfg.get("freq_lock_enabled", True)
    target_hz = int(aprs_cfg.get("aprs_freq_hz", 144800000))
    
    curr_band = getattr(r, 'current_band', 0)
    if curr_band == 1:
        curr_hz = int(r.state.get('sub_hz') or r.state.get('taajuus_hz') or 0)
        curr_mode = str(r.state.get('sub_mode') or r.state.get('mode') or 'FM')
    else:
        curr_hz = int(r.state.get('main_hz') or r.state.get('taajuus_hz') or 0)
        curr_mode = str(r.state.get('main_mode') or r.state.get('mode') or 'FM')

    if freq_lock_enabled and curr_hz > 0:
        is_freq_match = abs(curr_hz - target_hz) <= 500
        is_mode_match = curr_mode.upper().startswith("FM")
        if not (is_freq_match and is_mode_match):
            reason = f"Radio {radio_id} VFO is on {curr_hz/1e6:.4f} MHz {curr_mode} (Lock requires {target_hz/1e6:.3f} MHz FM)"
            logging.warning(f"[APRS TX SAFETY LOCK] BLOCKED: {reason}! TX aborted.")
            if ws_clients and app_loop and not app_loop.is_closed():
                msg = json.dumps({
                    "type": "aprs_tx_blocked",
                    "reason": reason,
                    "curr_hz": curr_hz,
                    "curr_mode": curr_mode,
                    "target_hz": target_hz
                })
                for ws in list(ws_clients):
                    if not ws.closed:
                        try:
                            asyncio.run_coroutine_threadsafe(ws.send_str(msg), app_loop)
                        except Exception:
                            pass
            return

    # Brief carrier sense check (max 0.25s) only if channel is heavily active (> 60%)
    wait_start = time.monotonic()
    while (time.monotonic() - wait_start) < 0.25:
        if float(r.state.get('smeter_pct', 0)) < 60.0:
            break
        time.sleep(0.05)

    rconf = global_config.get("radios", {}).get(str(radio_id), {})
    tx_dev = rconf.get("audio_tx_dev")
    if not tx_dev:
        logging.warning(f"APRS TX skipped: no audio_tx_dev configured for radio {radio_id}")
        return

    try:
        tx_dev_int = int(tx_dev)
    except Exception:
        logging.warning(f"APRS TX skipped: invalid audio_tx_dev '{tx_dev}'")
        return

    effective_tx_dev = resolve_wasapi_output_device(tx_dev_int)

    try:
        # Guarantee radio mod source is USB before transmitting using thread-safe write_civ
        if r.ser and r.ser.is_open:
            try:
                addr = getattr(r, 'radio_addr', 0xA2)
                # 1A 05 01 15 03 = DATA OFF MOD -> USB
                r.write_civ(civ_command(0x1A, 0x05, bytes([0x01, 0x15, 0x03]), to_addr=addr))
                time.sleep(0.015)
                # 1A 05 01 16 03 = DATA1 MOD -> USB
                r.write_civ(civ_command(0x1A, 0x05, bytes([0x01, 0x16, 0x03]), to_addr=addr))
                time.sleep(0.015)
            except Exception as e:
                logging.warning(f"[APRS TX] Error setting mod input: {e}")

        # Ensure audio_samples is float32 1D mono (matching wfweb mono WASAPI stream)
        if audio_samples.ndim > 1:
            audio_samples = audio_samples[:, 0]
        audio_samples = np.ascontiguousarray(audio_samples, dtype=np.float32)

        logging.info(f"[APRS TX] Keying PTT (is_cat=True) on radio {radio_id} (samples={len(audio_samples)}, sr=48000, dev={effective_tx_dev})")
        r.set_ptt_sync(1, is_cat=True)
        time.sleep(0.08)  # Allow relay and transmitter PA to settle before audio tones start
        sd.play(audio_samples, samplerate=48000, device=effective_tx_dev, blocking=True)
        time.sleep(0.15)  # Flush DAC buffer completely before dropping carrier to protect FCS CRC tail
    except Exception as e:
        logging.error(f"APRS TX error playing audio: {e}")
    finally:
        r.set_ptt_sync(0, is_cat=True)
        logging.info(f"[APRS TX] Unkeyed PTT on radio {radio_id}")

def aprs_on_packet(pkt: dict):
    if ws_clients and app_loop and not app_loop.is_closed():
        msg = json.dumps({"type": "aprs_packet", "packet": pkt})
        for ws in list(ws_clients):
            if not ws.closed:
                try:
                    asyncio.run_coroutine_threadsafe(ws.send_str(msg), app_loop)
                except Exception:
                    pass

def aprs_on_digipeat(info: dict):
    if ws_clients and app_loop and not app_loop.is_closed():
        msg = json.dumps({"type": "aprs_digipeated", "info": info})
        for ws in list(ws_clients):
            if not ws.closed:
                try:
                    asyncio.run_coroutine_threadsafe(ws.send_str(msg), app_loop)
                except Exception:
                    pass

aprs_engine.set_callbacks(on_packet=aprs_on_packet, on_digipeat=aprs_on_digipeat, tx_audio=aprs_tx_audio_handler)


class RigctldServer:
    """Kevyt ja luotettava Hamlib rigctld -yhteensopiva TCP-palvelin WSJT-X, JTDX ja muiden digiohjelmien CAT-ohjaukseen."""
    DUMP_STATE_PAYLOAD = (
        "1\n1\n0\n"
        "150000.000000 1500000000.000000 0x1dff -1 -1 0x17e00007 0x1f\n"
        "0 0 0 0 0 0 0\n"
        "150000.000000 1500000000.000000 0x1dff 5000 100000 0x17e00007 0x1f\n"
        "0 0 0 0 0 0 0\n"
        "0x1dff 1\n0x1dff 0\n0 0\n"
        "0xc0c 2400\n0xc0c 1800\n0xc0c 3000\n0xc0c 0\n"
        "0x2 500\n0x2 2400\n0x2 50\n0x2 0\n"
        "0x10 300\n0x10 2400\n0x10 50\n0x10 0\n"
        "0x1 8000\n0x1 2400\n0x1 10000\n"
        "0x1020 15000\n0x1020 8000\n0x40 230000\n"
        "0 0\n9990\n9990\n10000\n0\n10 \n10 20 30 \n"
        "0xffffffffffffffff\n0xffffffffffffffff\n0xfffffffff7ffffff\n"
        "0xfffeff7083ffffff\n0xffffffffffffffff\n0xffffffffffffffbf\n"
        "vfo_ops=0x7ffffff\nptt_type=0x1\ntargetable_vfo=0x10c3\n"
        "has_set_vfo=1\nhas_get_vfo=1\nhas_set_freq=1\nhas_get_freq=1\n"
        "has_set_conf=1\nhas_get_conf=1\nhas_power2mW=1\nhas_mW2power=1\n"
        "has_get_ant=1\nhas_set_ant=1\ntimeout=0\nrig_model=1\n"
        "rigctld_version=Hamlib 4.7.1\ndone\n"
    )

    def __init__(self, radio_id: str, port: int):
        self.radio_id = str(radio_id)
        self.port = int(port)
        self.running = False
        self.sock = None
        self.thread = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"rigctld-{self.radio_id}")
        self.thread.start()

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _run(self):
        while self.running:
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.sock.bind(("127.0.0.1", self.port))
                self.sock.listen(8)
                logging.info(f"Rigctld CAT-palvelin käynnissä radiolle {self.radio_id} osoitteessa 127.0.0.1:{self.port}")
                while self.running:
                    try:
                        conn, addr = self.sock.accept()
                        t = threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True)
                        t.start()
                    except Exception:
                        break
            except Exception as e:
                logging.warning(f"Rigctld server bind error on port {self.port}: {e}")
                time.sleep(2.0)

    def _handle_client(self, conn, addr):
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        buf = ""
        radio = radios.get(self.radio_id)
        client_keyed = False
        try:
            while self.running:
                data = conn.recv(1024)
                if not data:
                    break
                buf += data.decode("latin1", errors="ignore")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    cmd_line = line.strip()
                    if not cmd_line:
                        continue
                    parts = cmd_line.split()
                    c = parts[0]

                    if c in ("\\dump_state", "dump_state"):
                        conn.sendall(self.DUMP_STATE_PAYLOAD.encode("latin1"))
                    elif c in ("\\chk_vfo", "chk_vfo"):
                        conn.sendall(b"0\n")
                    elif c in ("\\get_powerstat", "get_powerstat"):
                        is_on = 1 if radio and radio.state.get("online") else 0
                        conn.sendall(f"{is_on}\n".encode("latin1"))
                    elif c in ("\\set_powerstat", "set_powerstat"):
                        conn.sendall(b"RPRT 0\n")
                    elif c in ("f", "\\get_freq", "get_freq"):
                        cur_freq = int(radio.state.get("taajuus_hz", 14074000)) if radio else 14074000
                        conn.sendall(f"{cur_freq}\n".encode("latin1"))
                    elif c in ("F", "\\set_freq", "set_freq"):
                        if len(parts) > 1 and radio:
                            try:
                                hz = int(float(parts[1]))
                                radio.set_frequency_sync(hz)
                            except Exception:
                                pass
                        conn.sendall(b"RPRT 0\n")
                    elif c in ("m", "\\get_mode", "get_mode"):
                        cur_mode = (radio.state.get("mode", "USB") if radio else "USB").upper()
                        is_data = (radio.state.get("data_on", False) or "-D" in cur_mode or "DATA" in cur_mode or "PKT" in cur_mode) if radio else False
                        if "USB" in cur_mode:
                            base = "USB"
                        elif "LSB" in cur_mode:
                            base = "LSB"
                        elif "FM" in cur_mode:
                            base = "FM"
                        elif "CW" in cur_mode:
                            base = "CW"
                        elif "RTTY" in cur_mode:
                            base = "RTTY"
                        elif "AM" in cur_mode:
                            base = "AM"
                        else:
                            base = cur_mode.split("-")[0]
                        if is_data and base in ("USB", "LSB", "FM"):
                            resp = f"PKT{base}\n3000\n"
                        elif base in ("USB", "LSB"):
                            resp = f"{base}\n3000\n"
                        elif base == "CW":
                            resp = "CW\n500\n"
                        elif base == "RTTY":
                            resp = "RTTY\n500\n"
                        elif base == "AM":
                            resp = "AM\n6000\n"
                        elif base == "FM":
                            resp = "FM\n15000\n"
                        else:
                            resp = f"{base}\n3000\n"
                        conn.sendall(resp.encode("latin1"))
                    elif c in ("M", "\\set_mode", "set_mode"):
                        if len(parts) > 1 and radio:
                            m_arg = parts[1].upper()
                            if "PKT" in m_arg:
                                target_mode = m_arg.replace("PKT", "") + "-D"
                            elif m_arg in ("DATA", "DATA-USB", "DATA_USB", "D-U", "DIGI", "DIG"):
                                target_mode = "USB-D"
                            elif m_arg in ("DATA-LSB", "DATA_LSB", "D-L"):
                                target_mode = "LSB-D"
                            elif m_arg in ("DATA-FM", "DATA_FM", "D-F"):
                                target_mode = "FM-D"
                            else:
                                target_mode = m_arg
                            try:
                                radio.set_mode_sync(target_mode)
                                logging.info(f"[Rigctld-{self.radio_id}] CAT Mode -> {target_mode} (from {m_arg})")
                            except Exception:
                                pass
                        conn.sendall(b"RPRT 0\n")
                    elif c in ("t", "\\get_ptt", "get_ptt"):
                        is_tx = 1 if radio and radio.state.get("ptt") == 1 else 0
                        conn.sendall(f"{is_tx}\n".encode("latin1"))
                    elif c in ("T", "\\set_ptt", "set_ptt"):
                        if len(parts) > 1 and radio:
                            try:
                                ptt_val = int(parts[1])
                                client_keyed = (ptt_val == 1)
                                radio.set_ptt_sync(ptt_val, is_cat=True)
                                logging.info(f"[Rigctld-{self.radio_id}] CAT PTT -> {ptt_val}")
                            except Exception:
                                pass
                        conn.sendall(b"RPRT 0\n")
                    elif c in ("v", "\\get_vfo", "get_vfo"):
                        conn.sendall(b"VFOA\n")
                    elif c in ("V", "\\set_vfo", "set_vfo"):
                        conn.sendall(b"RPRT 0\n")
                    elif c in ("s", "\\get_split_vfo", "get_split_vfo"):
                        conn.sendall(b"0\nVFOA\n")
                    elif c in ("S", "\\set_split_vfo", "set_split_vfo"):
                        conn.sendall(b"RPRT 0\n")
                    elif c in ("i", "\\get_split_freq", "get_split_freq"):
                        cur_freq = int(radio.state.get("taajuus_hz", 14074000)) if radio else 14074000
                        conn.sendall(f"{cur_freq}\n".encode("latin1"))
                    elif c in ("I", "\\set_split_freq", "set_split_freq"):
                        if len(parts) > 1 and radio:
                            try:
                                hz = int(float(parts[1]))
                                radio.set_frequency_sync(hz)
                            except Exception:
                                pass
                        conn.sendall(b"RPRT 0\n")
                    elif c in ("x", "\\get_split_mode", "get_split_mode"):
                        conn.sendall(b"PKTUSB\n3000\n")
                    elif c in ("X", "\\set_split_mode", "set_split_mode"):
                        if len(parts) > 1 and radio:
                            m_arg = parts[1].upper()
                            if "PKT" in m_arg:
                                target_mode = m_arg.replace("PKT", "") + "-D"
                            else:
                                target_mode = m_arg
                            try:
                                radio.set_mode_sync(target_mode)
                            except Exception:
                                pass
                        conn.sendall(b"RPRT 0\n")
                    elif c in ("l", "\\get_level", "get_level"):
                        conn.sendall(b"0\n")
                    elif c in ("L", "\\set_level", "set_level"):
                        conn.sendall(b"RPRT 0\n")
                    elif c == "q":
                        conn.sendall(b"RPRT 0\n")
                        return
                    else:
                        conn.sendall(b"RPRT 0\n")
        except Exception:
            pass
        finally:
            if client_keyed and radio and radio.cat_ptt:
                try:
                    radio.set_ptt_sync(0, is_cat=True)
                    logging.info(f"[Rigctld-{self.radio_id}] Released CAT PTT on client disconnect")
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass


class VirtualComBridge:
    """Reitittää CI-V-viestejä valinnaisen virtuaalisen COM-portin (esim. com0com COM11<->COM12) ja radion välillä."""
    def __init__(self, radio_id: str):
        self.radio_id = str(radio_id)
        self.vser = None
        self.running = False
        self.thread = None

    def start(self, port: str, baud: int = 115200):
        self.stop()
        if not port:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, args=(port, baud), daemon=True, name=f"vcom-{self.radio_id}")
        self.thread.start()

    def stop(self):
        self.running = False
        if self.vser and self.vser.is_open:
            try: self.vser.close()
            except Exception: pass
        self.vser = None

    def _run(self, port: str, baud: int):
        while self.running:
            try:
                self.vser = serial.Serial(port, baud, timeout=0.04)
                logging.info(f"Virtual COM bridge avattu radiolle {self.radio_id} porttiin {port}")
                while self.running and self.vser and self.vser.is_open:
                    waiting = self.vser.in_waiting
                    if waiting > 0:
                        raw = self.vser.read(waiting)
                        radio = radios.get(self.radio_id)
                        if radio and radio.ser and radio.ser.is_open:
                            try:
                                radio.ser.write(raw)
                            except Exception:
                                pass
                    time.sleep(0.005)
            except Exception as e:
                time.sleep(2.0)


rigctld_servers = {
    "1": RigctldServer("1", 4532),
    "2": RigctldServer("2", 4533),
    "3": RigctldServer("3", 4534)
}


keybind_listening_radio = None


async def global_ptt_monitor_loop():
    """Tarkkailee Win32 GetAsyncKeyState-rajapinnalla määritettyjä PTT-näppäimiä ja hiiren sivunappeja."""
    global keybind_listening_radio
    MOUSE_VK_NAMES = {
        0x05: "Mouse4",
        0x06: "Mouse5",
        0x04: "MouseMid"
    }
    while True:
        try:
            win_active = is_desktop_window_active()

            # Jos käyttäjä on painanut "PTT Key" -nappia, napataan hiiren sivunapit (XButton1 / XButton2 / Middle)
            # suoraan Windowsin kernel-tasolta, vaikka WebView2 nielisi selaintapahtuman!
            if keybind_listening_radio and win_active:
                listen_rid = str(keybind_listening_radio)
                for m_vk, m_name in MOUSE_VK_NAMES.items():
                    if bool(_user32.GetAsyncKeyState(m_vk) & 0x8000):
                        keybind_listening_radio = None
                        if listen_rid in global_config.get("radios", {}):
                            global_config["radios"][listen_rid]["ptt_key_code"] = m_vk
                            global_config["radios"][listen_rid]["ptt_key_name"] = m_name
                            save_config(global_config)
                        await broadcast_ws(json.dumps({
                            "type": "keybind_captured",
                            "radio_id": listen_rid,
                            "code": m_vk,
                            "name": m_name
                        }))
                        await asyncio.sleep(0.25)
                        break

            for rid, r in radios.items():
                rconf = global_config["radios"].get(rid, {})
                vk = int(rconf.get("ptt_key_code", 0) or 0)
                if vk <= 0 or not r.state.get("online"):
                    if r.key_ptt_held:
                        r.key_ptt_held = False
                        await r.set_ptt(False)
                    continue

                allow_global = bool(rconf.get("ptt_global", False))
                is_pressed = bool(_user32.GetAsyncKeyState(vk) & 0x8000)

                if is_pressed:
                    # Jos global-täppä on päällä, PTT menee päälle vaikka kirjoittaisit toisessa ohjelmassa!
                    # Jos global-täppä on pois päältä, vaaditaan että oma ikkuna on aktiivinen.
                    if allow_global or win_active:
                        if not r.key_ptt_held:
                            r.key_ptt_held = True
                            await r.set_ptt(True)
                else:
                    if r.key_ptt_held:
                        r.key_ptt_held = False
                        await r.set_ptt(False)
        except Exception as e:
            logging.debug(f"Global PTT monitor error: {e}")
        await asyncio.sleep(0.005)


def get_resource_path(relative_path):
    for base in [
        os.getcwd(),
        os.path.dirname(os.path.abspath(sys.executable)),
        os.path.dirname(os.path.abspath(__file__))
    ]:
        candidate = os.path.join(base, relative_path)
        if os.path.exists(candidate):
            return candidate
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return relative_path


async def handle_index(request):
    file_path = get_resource_path('index.html')
    with open(file_path, 'r', encoding='utf-8') as f:
        return web.Response(text=f.read(), content_type='text/html', headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0'
        })


async def handle_desktop(request):
    file_path = get_resource_path('desktop.html')
    with open(file_path, 'r', encoding='utf-8') as f:
        return web.Response(text=f.read(), content_type='text/html', headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0'
        })


def get_local_web_url():
    import socket
    ip = "localhost"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    cert_file = get_resource_path('cert.pem')
    key_file = get_resource_path('key.pem')
    scheme = "https" if (os.path.exists(cert_file) and os.path.exists(key_file)) else "http"
    return f"{scheme}://{ip}:{APP_PORT}"


async def api_open_browser(request):
    import webbrowser
    try:
        data = await request.json()
    except Exception:
        data = {}
    url = data.get("url") or get_local_web_url()
    await asyncio.to_thread(webbrowser.open, url)
    return web.json_response({"status": "ok", "url": url})


async def api_ports(request):
    ports = [
        {"device": p.device, "description": p.description}
        for p in serial.tools.list_ports.comports()
    ]
    return web.json_response({
        "ports": [p["device"] for p in ports],
        "port_details": ports,
        "config": global_config,
        "states": {rid: r.state for rid, r in radios.items()},
        "web_url": get_local_web_url()
    })


async def api_connect(request):
    data = await request.json()
    rid = str(data.get('radio_id', '1'))
    if rid not in radios:
        rid = '1'
    port = data.get('port', global_config["radios"][rid].get("port"))
    baud = int(data.get('baud', global_config["radios"][rid].get("baud", 115200)))
    res = await radios[rid].connect(port, baud)
    return web.json_response(res)


async def api_disconnect(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    rid = str(data.get('radio_id', '1'))
    if rid in radios:
        await radios[rid].disconnect_async()
        audio_manager.channels[rid].stop()
    return web.json_response({"status": "ok"})


async def api_config_update(request):
    data = await request.json()
    rid = str(data.get('radio_id', '1'))
    changed = False
    if "bookmarks" in data and isinstance(data["bookmarks"], list):
        global_config["bookmarks"] = data["bookmarks"]
        changed = True
    if "aprs" in data and isinstance(data["aprs"], dict):
        global_config.setdefault("aprs", {}).update(data["aprs"])
        aprs_engine.configure(global_config["aprs"])
        changed = True
    if "active_radios" in data and isinstance(data["active_radios"], list):
        global_config["active_radios"] = [str(x) for x in data["active_radios"] if str(x) in ("1", "2", "3")]
        changed = True
    if rid in global_config["radios"]:
        for key in ("port", "baud", "brand", "vfo_offset", "ptt_key_code", "ptt_key_name", "ptt_global",
                    "audio_rx_dev", "audio_tx_dev", "audio_spk_dev", "audio_mic_dev", "audio_rx_vol",
                    "wf_floor", "wf_gain", "wf_peak", "wf_speed", "wf_mode"):
            if key in data:
                global_config["radios"][rid][key] = data[key]
                changed = True
    if changed:
        save_config(global_config)
    return web.json_response({"status": "ok", "config": global_config["radios"].get(rid, {}), "bookmarks": global_config.get("bookmarks", [])})


async def api_shutdown(request):
    try:
        aprs_engine.stop()
        for r in radios.values():
            r.disconnect_sync()
        audio_manager.stop_all()
        for srv in rigctld_servers.values():
            srv.stop()
    except Exception:
        pass
    loop = asyncio.get_running_loop()
    loop.call_later(0.4, lambda: os._exit(0))
    return web.json_response({"status": "sammutetaan"})


async def api_audio_devices(request):
    try:
        devices = sd.query_devices()
        ins = []
        outs = []
        for i, d in enumerate(devices):
            # Näytetään pää-äänirajapinnan (MME / hostapi 0) laitteet selkeästi ilman duplikaatteja
            if d.get('hostapi', 0) != 0:
                continue
            if d['max_input_channels'] > 0:
                ins.append({"id": str(i), "label": d['name']})
            if d['max_output_channels'] > 0:
                outs.append({"id": str(i), "label": d['name']})
        return web.json_response({
            "inputs": ins,
            "outputs": outs,
            "active": {rid: ch.local_active for rid, ch in audio_manager.channels.items()},
            "recording": {rid: ch.recording for rid, ch in audio_manager.channels.items()},
            "config": global_config["radios"]
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_audio_start(request):
    data = await request.json()
    rid = str(data.get('radio_id', '1'))
    if rid not in audio_manager.channels:
        rid = '1'
    rconf = global_config["radios"].get(rid, {})
    rx_id = data.get('rx')
    tx_id = data.get('tx')
    spk_id = data.get('spk')
    mic_id = data.get('mic')
    vol = data.get('vol', rconf.get('audio_rx_vol', 100))
    local = bool(data.get('local', True if ('spk' in data or 'mic' in data) else False))
    try:
        audio_manager.channels[rid].start(rx_id, tx_id, spk_id, mic_id, vol, local=local)
        return web.json_response({"status": "ok", "active": True, "config": global_config["radios"].get(rid, {})})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_audio_stop(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    rid = data.get('radio_id')
    local_only = bool(data.get('local_only', False))
    if rid and str(rid) in audio_manager.channels:
        audio_manager.channels[str(rid)].stop(keep_recording=True, local_only=local_only)
    else:
        audio_manager.stop_all()
    return web.json_response({"status": "ok", "active": False})


async def api_audio_volume(request):
    data = await request.json()
    rid = str(data.get('radio_id', '1'))
    vol = float(data.get('vol', 100))
    if rid in audio_manager.channels:
        audio_manager.channels[rid].set_volume(vol)
        save_config(global_config)
    return web.json_response({"status": "ok"})


async def api_squelch(request):
    data = await request.json()
    rid = str(data.get('radio_id', '1'))
    sql_pct = float(data.get('sql_pct', 0))
    if rid in audio_manager.channels:
        audio_manager.channels[rid].set_squelch(sql_pct)
        save_config(global_config)
        await broadcast_state(rid)
    return web.json_response({"status": "ok", "sql_pct": int(round(sql_pct))})


async def api_rec_start(request):
    data = await request.json()
    rid = str(data.get('radio_id', '1'))
    if rid not in audio_manager.channels:
        rid = '1'
    rx_id = data.get('rx')
    vol = data.get('vol')
    try:
        audio_manager.channels[rid].start_recording(rx_id=rx_id, vol_pct=vol)
        return web.json_response({"status": "ok", "recording": True})
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=400)


async def api_rec_stop(request):
    data = await request.json()
    rid = str(data.get('radio_id', '1'))
    if rid not in audio_manager.channels:
        rid = '1'
    ch = audio_manager.channels[rid]
    pcm_bytes = ch.stop_recording()
    if not pcm_bytes:
        return web.json_response({"status": "empty", "recording": False})

    rstate = radios[rid].state
    model = rstate.get('model', f'Radio{rid}').replace(' ', '_')
    freq_mhz = rstate.get('taajuus_mhz', '0.000000')
    mode_str = rstate.get('mode', 'RX')
    stamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    default_name = f"{model}_{freq_mhz}MHz_{mode_str}_{stamp}.wav"

    save_path = await asyncio.to_thread(save_wav_dialog_blocking, default_name)
    if not save_path:
        return web.json_response({"status": "cancelled", "recording": False})

    try:
        def write_wav_file():
            with wave.open(save_path, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit PCM
                wf.setframerate(ch.sample_rate)
                wf.writeframes(pcm_bytes)

        await asyncio.to_thread(write_wav_file)
        return web.json_response({"status": "saved", "path": save_path, "recording": False})
    except Exception as e:
        logging.error(f"WAV save error: {e}")
        return web.json_response({"status": "error", "error": str(e), "recording": False}, status=500)


async def api_cmd(request):
    data = await request.json()
    rid = str(data.get('radio_id', '1'))
    radio = radios.get(rid, radios["1"])

    cmd_type = data.get('type')
    if cmd_type == 'radio_power':
        power_on = bool(data.get('power', True))
        if data.get('port'):
            radio.state['port'] = data.get('port')
            global_config["radios"][rid]["port"] = data.get('port')
        if data.get('baud'):
            radio.state['baud'] = int(data.get('baud'))
            global_config["radios"][rid]["baud"] = int(data.get('baud'))
        res = await radio.set_radio_power(power_on)
        return web.json_response(res)

    if not radio.state['connected'] or not radio.ser or not radio.ser.is_open:
        return web.json_response({"status": "offline"})

    try:
        if getattr(radio, 'brand', 'icom') == 'yaesu':
            # Yaesu FT-891:ssa pelkkä MD01;/MD02; ei vaihda sivukaistaa jos valikko 11-07 [SSB BFO]
            # on lukittu LSB/USB-tilaan tai AUTO-tilassa (<10 MHz pakottaa LSB).
            # EX11071;MD01;EX11071; pakottaa sekä SSB BFO:n että moden LSB:lle,
            # ja EX11070;MD02;EX11070; pakottaa sekä SSB BFO:n että moden USB:lle!
            y_map = {
                "LSB": "EX11071;MD01;EX11071;", "USB": "EX11070;MD02;EX11070;", "CW": "MD03;", "FM": "MD04;",
                "AM": "MD05;", "RTTY": "MD09;", "DATA-USB": "MD0C;", "USB-D": "MD0C;",
                "DATA-LSB": "MD08;", "LSB-D": "MD08;", "DATA-FM": "MD0A;", "FM-D": "MD0A;"
            }
            if cmd_type == 'taajuus':
                hz = int(data['hz'])
                # Jos mukana tulee myös mode (esim. Bookmarkista), asetetaan Yaesulle AINA ensin MODE (MD0x;)
                # ja vasta sen jälkeen TAAJUUS (FA00xxxxxxx;). Jos taajuus asetetaan ennen modea (esim. AM -> LSB),
                # Yaesu FT-891 siirtää taajuutta +0.7 kHz (BFO-offset) moden vaihtuessa!
                if data.get('mode'):
                    mode_str = str(data['mode']).upper()
                    if mode_str == 'LSB':
                        radio.yaesu_ssb_bfo = '1'
                    elif mode_str == 'USB':
                        radio.yaesu_ssb_bfo = '0'
                    radio.state['mode'] = mode_str
                    radio.state['main_mode'] = mode_str
                    radio.state['data_on'] = mode_str.endswith('-D')
                    radio.write_raw(f"{y_map.get(mode_str, 'EX11070;MD02;EX11070;')}".encode('ascii'))
                    await asyncio.sleep(0.045)
                radio.state['taajuus_hz'] = hz
                radio.state['taajuus_mhz'] = f"{(hz / 1000000):.6f}"
                radio.state['main_hz'] = hz
                radio.write_raw(f"FA{hz:09d};".encode('ascii'))
                if data.get('mode'):
                    await asyncio.sleep(0.035)
                    radio.write_raw(f"FA{hz:09d};".encode('ascii'))
                await broadcast_state(rid)
            elif cmd_type == 'mode':
                mode_str = data['mode'].upper()
                if mode_str == 'LSB':
                    radio.yaesu_ssb_bfo = '1'
                elif mode_str == 'USB':
                    radio.yaesu_ssb_bfo = '0'
                radio.state['mode'] = mode_str
                radio.state['main_mode'] = mode_str
                radio.state['data_on'] = mode_str.endswith('-D')
                radio.write_raw(f"{y_map.get(mode_str, 'EX11070;MD02;EX11070;')}".encode('ascii'))
                await broadcast_state(rid)
            elif cmd_type == 'ptt':
                tila = int(data['ptt'])
                await radio.set_ptt(tila == 1)
            elif cmd_type == 'atu':
                radio.write_raw(b"AC002;")
            elif cmd_type == 'teho':
                teho_f = float(data['teho'])
                radio.last_power_set_time = time.monotonic()
                w = max(5, min(100, int(round(teho_f * 100))))
                radio.state['power_pct'] = w
                radio.write_raw(f"PC{w:03d};".encode('ascii'))
                await broadcast_state(rid)
            return web.json_response({"status": "ok"})

        addr = radio.radio_addr
        if cmd_type == 'taajuus':
            hz = int(data['hz'])
            target_vfo = data.get('vfo')  # 'main', 'sub' tai None
            radio.tune_guard_until = time.monotonic() + 0.90
            radio.syncing = True
            try:
                need_select_main = False
                need_swap_ms = False
                need_dual_on = False
                need_select_vfo = None

                if addr == 0xA2 and data.get('swap_if_band_diff'):
                    # Kirjanmerkistä hypättäessä käytetään aina MAIN VFO:ta, ja jos uusi taajuus on eri bandilla
                    # kuin nykyinen MAIN VFO (esim. MAIN=VHF 2m ja bookmark=UHF 70cm), painetaan ensin Swap M/S (07 B0),
                    # jolloin vanha MAIN VFO siirtyy SUB VFO:ksi ja MAIN VFO vaihtuu uudelle bandille!
                    if radio.current_band == 1:
                        need_select_main = True
                        radio.current_band = 0
                        radio.state['active_band'] = 0
                    cur_main_hz = radio.state.get('main_hz', 0)
                    cur_main_band = _ic9700_band(cur_main_hz)
                    new_band = _ic9700_band(hz)
                    if cur_main_band and new_band and cur_main_band != new_band:
                        need_swap_ms = True
                        radio.state['sub_hz'] = cur_main_hz
                        radio.state['sub_mode'] = radio.state.get('main_mode', 'FM')
                        radio.state['sub_filter'] = radio.state.get('main_filter', 1)
                elif addr == 0xA2 and target_vfo in ('main', 'sub'):
                    want_band = 1 if target_vfo == 'sub' else 0
                    if want_band == 1 and radio.state.get('dual_rx', 0) == 0:
                        need_dual_on = True
                        radio.state['dual_rx'] = 1
                    if radio.current_band != want_band:
                        need_select_vfo = want_band
                        radio.current_band = want_band
                        radio.state['active_band'] = want_band

                if radio.current_band == 1:
                    old_sub = radio.state.get('sub_hz', 433500000)
                    if addr == 0xA2 and _ic9700_band(hz) and _ic9700_band(hz) == _ic9700_band(radio.state.get('main_hz', 0)):
                        if _ic9700_band(old_sub) and _ic9700_band(old_sub) != _ic9700_band(hz):
                            radio.state['main_hz'] = old_sub
                    radio.state['sub_hz'] = hz
                else:
                    old_main = radio.state.get('main_hz', 145500000)
                    if addr == 0xA2 and not need_swap_ms and _ic9700_band(hz) and _ic9700_band(hz) == _ic9700_band(radio.state.get('sub_hz', 0)):
                        if _ic9700_band(old_main) and _ic9700_band(old_main) != _ic9700_band(hz):
                            radio.state['sub_hz'] = old_main
                    radio.state['main_hz'] = hz

                radio.state['taajuus_hz'] = hz
                radio.state['taajuus_mhz'] = f"{(hz / 1000000):.6f}"

                mode_cmd_bytes = None
                data_cmd_bytes = None
                if data.get('mode'):
                    mode_str = str(data['mode']).upper()
                    fil_nro = int(data.get('filter', radio.state.get('filter', 1)))
                    base_mode = mode_str.split('-')[0]
                    is_data = 0x01 if ("-D" in mode_str or "-U" in mode_str) else 0x00
                    radio.state['data_on'] = (is_data == 0x01)
                    radio.state['mode'] = mode_str
                    if radio.current_band == 1:
                        radio.state['sub_mode'] = mode_str
                    else:
                        radio.state['main_mode'] = mode_str
                    m_map = {
                        "LSB": 0x00, "USB": 0x01, "AM": 0x02,
                        "CW": 0x03, "RTTY": 0x04, "FM": 0x05, "DV": 0x17
                    }
                    mode_byte = m_map.get(base_mode, 0x01)
                    mode_cmd_bytes = civ_command(0x06, mode_byte, bytes([fil_nro]), addr)
                    if base_mode in ("LSB", "USB", "FM"):
                        data_cmd_bytes = civ_command(0x1A, 0x06, bytes([is_data, fil_nro]), addr)

                if any(k in data for k in ('dup', 'offset_mhz', 'tone_mode', 'ctcss')):
                    if data.get('dup') is not None:
                        radio.state['dup'] = str(data['dup']).upper()
                    if data.get('offset_mhz') is not None:
                        radio.state['offset_mhz'] = round(float(data['offset_mhz']), 3)
                    if data.get('tone_mode') is not None:
                        radio.state['tone_mode'] = str(data['tone_mode'])
                    if data.get('ctcss') is not None:
                        radio.state['ctcss'] = round(float(data['ctcss']), 1)

                # Lähetetään nyt CI-V-komennot radiolle oikeassa järjestyksessä:
                if need_select_main:
                    radio.write_civ(civ_command(0x07, 0xD0, None, addr))
                    await asyncio.sleep(0.045)
                if need_swap_ms:
                    radio.write_civ(civ_command(0x07, 0xB0, None, addr))
                    await asyncio.sleep(0.055)
                if need_dual_on:
                    radio.write_civ(civ_command(0x16, 0x59, bytes([0x01]), addr))
                    await asyncio.sleep(0.04)
                if need_select_vfo is not None:
                    radio.write_civ(civ_command(0x07, 0xD1 if need_select_vfo == 1 else 0xD0, None, addr))
                    await asyncio.sleep(0.04)

                # Asetetaan taajuus (0x05) heti bandin vaihdon jälkeen:
                radio.write_civ(civ_command(0x05, None, hz_to_bcd(hz), addr))
                if mode_cmd_bytes is not None:
                    await asyncio.sleep(0.035)
                    radio.write_civ(mode_cmd_bytes)
                if data_cmd_bytes is not None:
                    await asyncio.sleep(0.035)
                    radio.write_civ(data_cmd_bytes)
                if any(k in data for k in ('dup', 'offset_mhz', 'tone_mode', 'ctcss')):
                    await asyncio.sleep(0.035)
                    await radio.apply_repeater_settings(
                        dup=data.get('dup'),
                        offset_mhz=data.get('offset_mhz'),
                        tone_mode=data.get('tone_mode'),
                        ctcss=data.get('ctcss')
                    )
            finally:
                radio.tune_guard_until = time.monotonic() + 0.45
                radio.syncing = False
            await broadcast_state(rid)

        elif cmd_type == 'select_band':
            want_band = 1 if str(data.get('band', '0')).lower() in ('1', 'sub') else 0
            if want_band == 1 and radio.state.get('dual_rx', 0) == 0:
                radio.state['dual_rx'] = 1
                radio.write_civ(civ_command(0x16, 0x59, bytes([0x01]), addr))
                await asyncio.sleep(0.04)
            radio.write_civ(civ_command(0x07, 0xD1 if want_band == 1 else 0xD0, None, addr))
            await asyncio.sleep(0.05)
            radio.current_band = want_band
            radio.state['active_band'] = want_band
            radio.write_civ(civ_command(0x03, None, None, addr))
            await asyncio.sleep(0.04)
            radio.write_civ(civ_command(0x04, None, None, addr))
            await asyncio.sleep(0.03)
            radio.write_civ(civ_command(0x14, 0x0A, None, addr))
            if want_band == 1:
                radio.state['taajuus_hz'] = radio.state['sub_hz']
                radio.state['mode'] = radio.state['sub_mode']
                radio.state['filter'] = radio.state['sub_filter']
            else:
                radio.state['taajuus_hz'] = radio.state['main_hz']
                radio.state['mode'] = radio.state['main_mode']
                radio.state['filter'] = radio.state['main_filter']
            radio.state['taajuus_mhz'] = f"{(radio.state['taajuus_hz'] / 1000000):.6f}"
            await broadcast_state(rid)

        elif cmd_type == 'dual_rx':
            dw = 1 if int(data.get('dual_rx', 1)) else 0
            radio.state['dual_rx'] = dw
            radio.ser.write(civ_command(0x16, 0x59, bytes([dw]), addr))
            if dw == 0 and radio.current_band == 1:
                await asyncio.sleep(0.04)
                radio.current_band = 0
                radio.state['active_band'] = 0
                radio.ser.write(civ_command(0x07, 0xD0, None, addr))
                await asyncio.sleep(0.03)
                radio.state['taajuus_hz'] = radio.state['main_hz']
                radio.state['mode'] = radio.state['main_mode']
                radio.state['filter'] = radio.state['main_filter']
                radio.state['taajuus_mhz'] = f"{(radio.state['taajuus_hz'] / 1000000):.6f}"
            await broadcast_state(rid)

        elif cmd_type == 'swap_bands':
            radio.ser.write(civ_command(0x07, 0xB0, None, addr))
            # Vaihdetaan MAIN ja SUB arvot keskenään
            radio.state['main_hz'], radio.state['sub_hz'] = radio.state['sub_hz'], radio.state['main_hz']
            radio.state['main_mode'], radio.state['sub_mode'] = radio.state['sub_mode'], radio.state['main_mode']
            radio.state['main_filter'], radio.state['sub_filter'] = radio.state['sub_filter'], radio.state['main_filter']
            if radio.current_band == 1:
                radio.state['taajuus_hz'] = radio.state['sub_hz']
                radio.state['mode'] = radio.state['sub_mode']
                radio.state['filter'] = radio.state['sub_filter']
            else:
                radio.state['taajuus_hz'] = radio.state['main_hz']
                radio.state['mode'] = radio.state['main_mode']
                radio.state['filter'] = radio.state['main_filter']
            radio.state['taajuus_mhz'] = f"{(radio.state['taajuus_hz'] / 1000000):.6f}"
            await asyncio.sleep(0.05)
            radio.ser.write(civ_command(0x03, None, None, addr))
            await broadcast_state(rid)

        elif cmd_type == 'mode':
            mode_str = data['mode'].upper()
            fil_nro = int(data.get('filter', radio.state.get('filter', 1)))
            radio.state['filter'] = fil_nro

            base_mode = mode_str.split('-')[0]
            is_data = 0x01 if ("-D" in mode_str or "-U" in mode_str) else 0x00
            radio.state['data_on'] = (is_data == 0x01)
            radio.state['mode'] = mode_str
            if radio.current_band == 1:
                radio.state['sub_mode'] = mode_str
                radio.state['sub_filter'] = fil_nro
            else:
                radio.state['main_mode'] = mode_str
                radio.state['main_filter'] = fil_nro

            m_map = {
                "LSB": 0x00, "USB": 0x01, "AM": 0x02,
                "CW": 0x03, "RTTY": 0x04, "FM": 0x05, "DV": 0x17
            }
            mode_byte = m_map.get(base_mode, 0x01)

            radio.ser.write(civ_command(0x06, mode_byte, bytes([fil_nro]), addr))
            if base_mode in ("LSB", "USB", "FM"):
                await asyncio.sleep(0.04)
                radio.ser.write(civ_command(0x1A, 0x06, bytes([is_data, fil_nro]), addr))
            await broadcast_state(rid)

        elif cmd_type == 'ptt':
            tila = int(data['ptt'])
            await radio.set_ptt(tila == 1)

        elif cmd_type == 'atu':
            radio.ser.write(civ_command(0x1C, 0x01, bytes([0x01]), addr))
            await asyncio.sleep(0.4)
            radio.ser.write(civ_command(0x1C, 0x01, bytes([0x02]), addr))

        elif cmd_type == 'teho':
            teho_f = float(data['teho'])
            radio.last_power_set_time = time.monotonic()
            radio.state['power_pct'] = max(0, min(100, int(round(teho_f * 100))))
            val = max(0, min(255, int(round(teho_f * 255))))
            val_str = str(val).zfill(4)
            data_bytes = bytes([int(val_str[0:2], 16), int(val_str[2:4], 16)])
            radio.ser.write(civ_command(0x14, 0x0A, data_bytes, addr))
            await broadcast_state(rid)

        elif cmd_type == 'repeater':
            await radio.apply_repeater_settings(
                dup=data.get('dup'),
                offset_mhz=data.get('offset_mhz'),
                tone_mode=data.get('tone_mode'),
                ctcss=data.get('ctcss')
            )
            await broadcast_state(rid)

        elif cmd_type == 'tone_burst':
            active = bool(int(data.get('active', 0)))
            freq_hz = float(data.get('freq', 1750.0))
            await radio.set_tone_burst(active, freq_hz)
            await broadcast_state(rid)

        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "virhe", "error": str(e)})


async def websocket_handler(request):
    global desktop_window_focused, keybind_listening_radio
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_clients.add(ws)
    await broadcast_state()
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    d = json.loads(msg.data)
                    if d.get("type") == "window_focus":
                        desktop_window_focused = bool(d.get("focused", True))
                    elif d.get("type") == "ptt":
                        prid = str(d.get("radio_id", "1"))
                        if prid in radios:
                            await radios[prid].set_ptt(bool(int(d.get("ptt", 0))))
                    elif d.get("type") == "keybind_listen":
                        if d.get("listening"):
                            keybind_listening_radio = str(d.get("radio_id", "1"))
                        elif keybind_listening_radio == str(d.get("radio_id", "1")):
                            keybind_listening_radio = None
                except Exception:
                    pass
    finally:
        ws_clients.discard(ws)
    return ws


async def websocket_audio_handler(request):
    rid = str(request.query.get('radio', '1'))
    if rid not in audio_manager.channels:
        rid = '1'
    try:
        client_sr = int(float(request.query.get('sample_rate', 16000) or 16000))
    except Exception:
        client_sr = 16000
    ch = audio_manager.channels[rid]
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ch.ws_clients.add(ws)
    ch.active = True
    if ch.rx_dev is None:
        rconf = global_config["radios"].get(rid, {})
        rx_saved = rconf.get("audio_rx_dev")
        if rx_saved:
            ch.rx_dev = int(rx_saved)
    if ch.tx_dev is None:
        rconf = global_config["radios"].get(rid, {})
        tx_saved = rconf.get("audio_tx_dev")
        if tx_saved:
            ch.tx_dev = int(tx_saved)
    ch._ensure_rx_stream()
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                if radios[rid].state.get('ptt') == 1:
                    data = np.frombuffer(msg.data, dtype=np.float32)
                    if client_sr != 16000 and client_sr > 0 and len(data) > 0:
                        num_out = int(round(len(data) * 16000.0 / client_sr))
                        if num_out > 0:
                            data = np.interp(
                                np.linspace(0.0, len(data) - 1, num_out),
                                np.arange(len(data)),
                                data
                            ).astype(np.float32)
                    ch.feed_tx_samples(data)
    finally:
        ch.ws_clients.discard(ws)
        if not ch.local_active and len(ch.ws_clients) == 0 and not ch.recording:
            ch.stop()
    return ws


async def api_wsjtx_config(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    rid = str(data.get('radio_id', '1'))
    port = 4532 if rid == '1' else 4533
    target_kw = "7300" if rid == '1' else "9700"
    model_name = "IC-7300" if rid == '1' else "IC-9700"

    sound_in_name = None
    sound_out_name = None
    try:
        for d in sd.query_devices():
            if d.get('hostapi', 0) == 0:
                name = d['name']
                if target_kw in name:
                    if d['max_input_channels'] > 0 and not sound_in_name:
                        sound_in_name = name
                    if d['max_output_channels'] > 0 and not sound_out_name:
                        sound_out_name = name
    except Exception:
        pass

    ini_path = os.path.expandvars(r'%LOCALAPPDATA%\WSJT-X\WSJT-X.ini')
    if not os.path.exists(ini_path):
        return web.json_response({
            "status": "error",
            "error": f"WSJT-X.ini not found. Open WSJT-X / WSJT-Z once and close it, or configure manually:\nRig: Hamlib NET rigctl\nNetwork server: 127.0.0.1:{port}\nPTT Method: CAT"
        }, status=404)

    bak_path = ini_path + '.backup_icom_remote'
    try:
        if not os.path.exists(bak_path):
            shutil.copy2(ini_path, bak_path)

        with open(ini_path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()

        new_lines = []
        found_rig = False
        found_netport = False
        found_sound_in = False
        found_sound_out = False
        for line in lines:
            if line.startswith('Rig='):
                new_lines.append("Rig=Hamlib NET rigctl\n")
                found_rig = True
            elif line.startswith('CATNetworkPort='):
                new_lines.append(f"CATNetworkPort=127.0.0.1:{port}\n")
                found_netport = True
            elif line.startswith('SoundInName=') and sound_in_name:
                new_lines.append(f"SoundInName={sound_in_name}\n")
                found_sound_in = True
            elif line.startswith('SoundOutName=') and sound_out_name:
                new_lines.append(f"SoundOutName={sound_out_name}\n")
                found_sound_out = True
            else:
                new_lines.append(line)

        if not found_rig:
            new_lines.append("Rig=Hamlib NET rigctl\n")
        if not found_netport:
            new_lines.append(f"CATNetworkPort=127.0.0.1:{port}\n")
        if not found_sound_in and sound_in_name:
            new_lines.append(f"SoundInName={sound_in_name}\n")
        if not found_sound_out and sound_out_name:
            new_lines.append(f"SoundOutName={sound_out_name}\n")

        with open(ini_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)

        wsjtx_running = False
        try:
            import subprocess
            out = subprocess.check_output('tasklist /FI "IMAGENAME eq wsjt*" /NH', shell=True).decode('latin1', errors='ignore')
            if "wsjt" in out.lower():
                wsjtx_running = True
        except Exception:
            pass

        aud_msg = f"\nAudio In: {sound_in_name}\nAudio Out: {sound_out_name}" if (sound_in_name and sound_out_name) else ""
        running_warn = "\n\n⚠️ IMPORTANT: WSJT-X is currently open! Please close WSJT-X and re-open it now so these changes take effect!" if wsjtx_running else "\n\nYou can now open WSJT-X!"
        return web.json_response({
            "status": "ok",
            "message": f"WSJT-X configured for {model_name}!\n\nRig: Hamlib NET rigctl\nNetwork server: 127.0.0.1:{port}\nPTT Method: CAT{aud_msg}{running_warn}",
            "port": port
        })
    except Exception as e:
        return web.json_response({"status": "error", "error": str(e)}, status=500)


async def api_wsjtx_revert(request):
    ini_path = os.path.expandvars(r'%LOCALAPPDATA%\WSJT-X\WSJT-X.ini')
    bak_path = ini_path + '.backup_icom_remote'
    if os.path.exists(bak_path):
        try:
            shutil.copy2(bak_path, ini_path)
            return web.json_response({"status": "ok", "message": "WSJT-X configuration restored from backup!"})
        except Exception as e:
            return web.json_response({"status": "error", "error": str(e)}, status=500)
    return web.json_response({"status": "error", "error": "Backup file not found"}, status=404)


async def api_wsjtx_status(request):
    ini_path = os.path.expandvars(r'%LOCALAPPDATA%\WSJT-X\WSJT-X.ini')
    installed = os.path.exists(ini_path) or os.path.exists(r'C:\WSJT\wsjtz\bin\wsjtx.exe')
    cur_rig = ""
    cur_port = ""
    if os.path.exists(ini_path):
        try:
            with open(ini_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if line.startswith('Rig='):
                        cur_rig = line.split('=', 1)[1].strip()
                    elif line.startswith('CATNetworkPort='):
                        cur_port = line.split('=', 1)[1].strip()
        except Exception:
            pass
    return web.json_response({
        "installed": installed,
        "rig": cur_rig,
        "network_port": cur_port,
        "rigctld_ports": {"1": 4532, "2": 4533}
    })


async def api_auth_status(request):
    pin_enabled = bool(global_config.get("web_pin_enabled", True))
    pin_val = str(global_config.get("web_pin", "7300")).strip()
    required = pin_enabled and bool(pin_val)
    return web.json_response({
        "pin_required": required,
        "pin_ver": global_config.get("web_pin_updated", 0)
    })


async def api_auth_verify(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    pin_enabled = bool(global_config.get("web_pin_enabled", True))
    pin_val = str(global_config.get("web_pin", "7300")).strip()
    if not pin_enabled or not pin_val:
        return web.json_response({"status": "ok", "message": "No PIN required"})
    submitted = str(data.get("pin", "")).strip()
    if submitted == pin_val:
        return web.json_response({"status": "ok"})
    return web.json_response({"status": "error", "message": "Invalid PIN"}, status=401)


async def api_web_pin(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)
    if "web_pin_enabled" in data:
        global_config["web_pin_enabled"] = bool(data["web_pin_enabled"])
    if "web_pin" in data:
        global_config["web_pin"] = str(data["web_pin"]).strip()
    global_config["web_pin_updated"] = int(time.time())
    save_config(global_config)
    return web.json_response({
        "status": "ok",
        "web_pin": str(global_config.get("web_pin", "7300")),
        "web_pin_enabled": bool(global_config.get("web_pin_enabled", True)),
        "web_pin_updated": global_config.get("web_pin_updated")
    })


async def api_aprs_status(request):
    return web.json_response(aprs_engine.get_status())


async def api_aprs_stations(request):
    with aprs_engine.lock:
        return web.json_response({
            "stations": list(aprs_engine.heard_stations.values()),
            "recent_packets": aprs_engine.recent_packets[-100:]
        })


async def api_aprs_config(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"status": "error", "message": "Invalid JSON"}, status=400)
    
    aprs_engine.configure(data)
    
    aprs_cfg = global_config.setdefault("aprs", {})
    for k in ["rx_enabled", "radio_id", "digi_enabled", "repeat_all", "wx_enabled", "freq_lock_enabled", "aprs_freq_hz", "my_call", "tocall", "allow_wide1", "allow_wide2",
              "tx_delay_ms", "beacon_enabled", "beacon_interval_min", "beacon_comment",
              "my_symbol", "my_symbol_table", "my_lat", "my_lon", "my_grid"]:
        if k in data:
            aprs_cfg[k] = data[k]
    save_config(global_config)
    
    if aprs_engine.rx_enabled and aprs_engine.radio_id in audio_manager.channels:
        audio_manager.channels[aprs_engine.radio_id]._ensure_rx_stream()

    if ws_clients and app_loop and not app_loop.is_closed():
        msg = json.dumps({"type": "aprs_status", "status": aprs_engine.get_status()})
        for ws in list(ws_clients):
            if not ws.closed:
                try:
                    asyncio.run_coroutine_threadsafe(ws.send_str(msg), app_loop)
                except Exception:
                    pass

    return web.json_response({"status": "ok", "config": aprs_engine.get_status()})


async def api_aprs_clear(request):
    aprs_engine.clear_stations()
    return web.json_response({"status": "ok", "message": "Heard stations cleared"})


async def api_aprs_send_beacon(request):
    if not aprs_engine.my_call or aprs_engine.my_call == "N0CALL-10":
        return web.json_response({"status": "error", "message": "Please configure your Callsign first in APRS settings."}, status=400)
    
    rid = str(aprs_engine.radio_id or "2")
    r = radios.get(rid)
    aprs_cfg = global_config.get("aprs", {})
    if aprs_cfg.get("freq_lock_enabled", True) and r and r.state.get('online', False):
        curr_band = getattr(r, 'current_band', 0)
        curr_hz = int((r.state.get('sub_hz') if curr_band == 1 else r.state.get('main_hz')) or r.state.get('taajuus_hz', 0) or 0)
        curr_mode = str((r.state.get('sub_mode') if curr_band == 1 else r.state.get('main_mode')) or r.state.get('mode', 'FM') or 'FM')
        target_hz = int(aprs_cfg.get("aprs_freq_hz", 144800000))
        if curr_hz > 0 and (abs(curr_hz - target_hz) > 500 or not curr_mode.upper().startswith("FM")):
            return web.json_response({
                "status": "error",
                "message": f"TX BLOCKED by Frequency Safety Lock! Radio {rid} is tuned to {curr_hz/1e6:.4f} MHz ({curr_mode}). Please tune to {target_hz/1e6:.3f} MHz FM first."
            }, status=400)

    success = aprs_engine.send_beacon()
    return web.json_response({"status": "ok" if success else "error"})


async def api_aprs_fetch_weather(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    force = bool(data.get("force", True))
    wx = aprs_engine.get_weather(force=force)
    if wx:
        return web.json_response({"status": "ok", "weather": wx})
    return web.json_response({"status": "error", "message": "Failed to fetch weather from Open-Meteo. Check internet connection and coordinates."}, status=500)


async def api_aprs_tune(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    rid = str(data.get("radio_id", aprs_engine.radio_id or "2"))
    r = radios.get(rid)
    if not r or not r.ser or not r.ser.is_open:
        return web.json_response({"status": "error", "message": f"Radio {rid} is not connected."}, status=400)
    
    freq_hz = int(data.get("freq_hz", 144800000))
    r.set_frequency_sync(freq_hz)
    addr = r.radio_addr
    r.ser.write(civ_command(0x05, None, hz_to_bcd(freq_hz), addr))
    await asyncio.sleep(0.04)
    r.set_mode_sync("FM", 1)
    r.ser.write(civ_command(0x06, 0x05, bytes([0x01]), addr))
    await asyncio.sleep(0.02)
    # Ensure data mode off
    r.ser.write(civ_command(0x1A, 0x06, bytes([0x00, 0x01]), addr))
    await asyncio.sleep(0.02)
    # Ensure DATA OFF MOD is USB (0x03)
    r.ser.write(civ_command(0x1A, 0x05, bytes([0x01, 0x15, 0x03]), addr))
    await broadcast_state(rid)
    return web.json_response({"status": "ok", "message": f"Tuned radio {rid} to {freq_hz/1e6:.3f} MHz FM"})


def create_app():
    app = web.Application()
    app.add_routes([
        web.get('/', handle_index),
        web.get('/desktop', handle_desktop),
        web.get('/api/ports', api_ports),
        web.get('/api/auth_status', api_auth_status),
        web.post('/api/auth_verify', api_auth_verify),
        web.post('/api/web_pin', api_web_pin),
        web.post('/api/connect', api_connect),
        web.post('/api/disconnect', api_disconnect),
        web.post('/api/config', api_config_update),
        web.post('/api/shutdown', api_shutdown),
        web.post('/api/cmd', api_cmd),
        web.get('/api/audio_devices', api_audio_devices),
        web.post('/api/audio_start', api_audio_start),
        web.post('/api/audio_stop', api_audio_stop),
        web.post('/api/audio_volume', api_audio_volume),
        web.post('/api/squelch', api_squelch),
        web.post('/api/rec_start', api_rec_start),
        web.post('/api/rec_stop', api_rec_stop),
        web.post('/api/open_browser', api_open_browser),
        web.post('/api/wsjtx_config', api_wsjtx_config),
        web.post('/api/wsjtx_revert', api_wsjtx_revert),
        web.get('/api/wsjtx_status', api_wsjtx_status),
        web.get('/api/aprs/status', api_aprs_status),
        web.get('/api/aprs/stations', api_aprs_stations),
        web.post('/api/aprs/config', api_aprs_config),
        web.post('/api/aprs/clear', api_aprs_clear),
        web.post('/api/aprs/send_beacon', api_aprs_send_beacon),
        web.post('/api/aprs/tune', api_aprs_tune),
        web.post('/api/aprs/fetch_weather', api_aprs_fetch_weather),
        web.get('/api/aprs/weather', api_aprs_fetch_weather),
        web.get('/ws', websocket_handler),
        web.get('/ws_audio', websocket_audio_handler)
    ])
    static_dir = os.path.join(BASE_DIR, 'static')
    if os.path.exists(static_dir):
        app.router.add_static('/static', path=static_dir)
    return app


def start_background_server(ready_event: threading.Event):
    global app_loop
    import ssl

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app_loop = loop

    app = create_app()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())

    # 1. Paikallinen HTTP-palvelin työpöydän omaa ikkunaa varten (127.0.0.1:8086)
    http_site = web.TCPSite(runner, '127.0.0.1', LOCAL_HTTP_PORT)
    loop.run_until_complete(http_site.start())

    # 2. HTTPS-palvelin mobiili-/etäselaimia varten (0.0.0.0:8085)
    try:
        cert_file = get_resource_path('cert.pem')
        key_file = get_resource_path('key.pem')
        if os.path.exists(cert_file) and os.path.exists(key_file):
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(cert_file, key_file)
            https_site = web.TCPSite(runner, '0.0.0.0', APP_PORT, ssl_context=ssl_context)
            loop.run_until_complete(https_site.start())
            logging.info(f"Mobiili/Web HTTPS-palvelin käynnissä portissa {APP_PORT}")
        else:
            web_site = web.TCPSite(runner, '0.0.0.0', APP_PORT)
            loop.run_until_complete(web_site.start())
    except Exception as e:
        logging.error(f"Web port {APP_PORT} start error: {e}")

    async def init_tasks():
        asyncio.create_task(global_ptt_monitor_loop())
        for srv in rigctld_servers.values():
            srv.start()
        active_rids = global_config.get("active_radios", ["1"])
        for rid in active_rids:
            rconf = global_config["radios"].get(str(rid), {})
            if rconf.get("auto_connect", True) and rconf.get("port") and str(rid) in radios:
                await radios[str(rid)].connect(rconf["port"], rconf.get("baud", 115200))

    loop.create_task(init_tasks())
    ready_event.set()
    loop.run_forever()


def setup_tray(window_ref_list):
    try:
        def create_image():
            image = Image.new('RGB', (64, 64), color=(24, 24, 24))
            dc = ImageDraw.Draw(image)
            dc.rectangle([8, 18, 56, 54], outline=(77, 166, 255), width=3)
            dc.rectangle([14, 24, 50, 40], fill=(40, 167, 69))
            dc.ellipse([24, 44, 40, 50], fill=(200, 200, 200))
            dc.line([15, 18, 15, 6], fill=(77, 166, 255), width=3)
            return image

        def show_desktop(icon, item):
            if window_ref_list:
                try:
                    window_ref_list[0].show()
                    window_ref_list[0].restore()
                except Exception:
                    pass

        def open_mobile_web(icon, item):
            import webbrowser
            webbrowser.open(f'https://localhost:{APP_PORT}')

        def on_quit(icon, item):
            icon.stop()
            try:
                for r in radios.values():
                    r.disconnect_sync()
                audio_manager.stop_all()
                for srv in rigctld_servers.values():
                    srv.stop()
            except Exception:
                pass
            os._exit(0)

        image = create_image()
        menu = pystray.Menu(
            pystray.MenuItem('Show Control Window', show_desktop, default=True),
            pystray.MenuItem('Open Web Interface (Browser)', open_mobile_web),
            pystray.MenuItem('Quit Icom Remote', on_quit)
        )
        icon = pystray.Icon("IcomDualRemote", image, WINDOW_TITLE, menu)
        icon.run_detached()
    except Exception as e:
        logging.error(f"Tray icon error: {e}")


if __name__ == '__main__':
    ready_evt = threading.Event()
    srv_thread = threading.Thread(target=start_background_server, args=(ready_evt,), daemon=True)
    srv_thread.start()
    ready_evt.wait(timeout=5.0)

    win_list = []
    setup_tray(win_list)

    try:
        scr_w = user32.GetSystemMetrics(0)
        scr_h = user32.GetSystemMetrics(1)
        win_w = max(1400, min(1860, scr_w - 90))
        win_h = max(860, min(1000, scr_h - 90))
    except Exception:
        win_w, win_h = 1780, 960

    win = webview.create_window(
        WINDOW_TITLE,
        f'http://127.0.0.1:{LOCAL_HTTP_PORT}/desktop?t={int(time.time())}',
        x=80,
        y=15,
        width=win_w,
        height=win_h,
        min_size=(1200, 680),
        background_color='#141417'
    )
    win_list.append(win)

    def on_closed():
        try:
            for r in radios.values():
                r.disconnect_sync()
            audio_manager.stop_all()
        except Exception:
            pass
        os._exit(0)

    win.events.closed += on_closed
    storage_path = os.path.join(BASE_DIR, 'webview_data')
    os.makedirs(storage_path, exist_ok=True)
    webview.start(private_mode=False, storage_path=storage_path)