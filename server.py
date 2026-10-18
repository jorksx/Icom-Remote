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
except ImportError:
    logging.warning("Asennetaan tarvittavat kirjastot...")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install",
        "aiohttp", "pyserial", "pystray", "pillow", "sounddevice", "numpy", "pywebview"
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

logging.basicConfig(level=logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'icom_config.json')
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
            "vcom_port": ""
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
            "vcom_port": ""
        }
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
        # Varmistetaan oletuskentät
        for rid in ("1", "2"):
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
        self.scope_span_hz = 50000
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
        self.cat_ptt = False
        self.vcom_bridge = None

        is_9700 = (self.radio_addr == 0xA2)
        default_hz = 145500000 if is_9700 else 14200000
        default_sub_hz = 433500000
        default_mode = 'FM' if is_9700 else 'USB'
        model_str = 'IC-9700' if is_9700 else 'IC-7300'
        self.state = {
            'radio_id': self.radio_id,
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
            'smeter': 'S0',
            'smeter_pct': 0.0,
            'sql_pct': int(rconf.get('sql_pct', 0) or 0),
            'ptt': 0,
            'power_pct': 100,
            'connected': False,   # Onko sarjaportti avattu (Start painettu)
            'online': False       # Vastaako radio oikeasti (virrat päällä)
        }
        self._update_model_name()

    def _update_model_name(self):
        if self.radio_addr == 0x94:
            self.state['model'] = 'IC-7300'
            self.state['name'] = 'IC-7300'
        elif self.radio_addr == 0xA2:
            self.state['model'] = 'IC-9700'
            self.state['name'] = 'IC-9700'
        else:
            self.state['model'] = f"ICOM (0x{self.radio_addr:02X})"
            self.state['name'] = self.state['model']
        if self.radio_id in global_config.get("radios", {}):
            global_config["radios"][self.radio_id]["detected_addr"] = self.radio_addr
            global_config["radios"][self.radio_id]["name"] = self.state['name']

    def _enable_scope_hw(self):
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
        """Lukee IC-9700:n molemmat vastaanottimet (MAIN & SUB) sekä Dualwatch-tilan."""
        if not self.ser or not self.ser.is_open or self.radio_addr != 0xA2:
            return
        try:
            # 1. Kysytään Dualwatch-tila (16 59) ja RF Power (14 0A)
            self.ser.write(civ_command(0x16, 0x59, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.04)
            self.ser.write(civ_command(0x14, 0x0A, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.04)
            # 2. Luetaan SUB-puolen taajuus ja mode (07 D1 -> 03 -> 04)
            self.current_band = 1
            self.ser.write(civ_command(0x07, 0xD1, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.05)
            self.ser.write(civ_command(0x03, None, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.04)
            self.ser.write(civ_command(0x04, None, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.05)
            # 3. Palataan MAIN-puolelle ja luetaan sen taajuus, mode, data-tila ja RF Power
            self.current_band = 0
            self.state['active_band'] = 0
            self.ser.write(civ_command(0x07, 0xD0, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.05)
            self.ser.write(civ_command(0x03, None, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.04)
            self.ser.write(civ_command(0x04, None, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.04)
            self.ser.write(civ_command(0x1A, 0x06, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.04)
            self.ser.write(civ_command(0x14, 0x0A, None, to_addr=self.radio_addr))
            await asyncio.sleep(0.04)
            await broadcast_state(self.radio_id)
        except Exception as e:
            logging.debug(f"IC-9700 dual sync error: {e}")

    async def _sync_initial_state(self):
        """Hakee radion kaikki nykyiset asetukset (taajuus, mode, filtteri, DATA, RF Power, PTT, Dual RX) heti yhdistettäessä."""
        if not self.ser or not self.ser.is_open:
            return
        try:
            self._enable_scope_hw()
            await asyncio.sleep(0.03)
            # Kysytään RF Power (14 0A), taajuus (03), mode/filtteri (04), DATA-tila (1A 06), PTT (1C 00) ja S-mittari (15 02)
            for cmd_bytes in (
                civ_command(0x14, 0x0A, None, to_addr=self.radio_addr),
                civ_command(0x03, None, None, to_addr=self.radio_addr),
                civ_command(0x04, None, None, to_addr=self.radio_addr),
                civ_command(0x1A, 0x06, None, to_addr=self.radio_addr),
                civ_command(0x1C, 0x00, None, to_addr=self.radio_addr),
                civ_command(0x15, 0x02, None, to_addr=self.radio_addr),
            ):
                if self.ser and self.ser.is_open:
                    self.ser.write(cmd_bytes)
                    await asyncio.sleep(0.035)

            if self.radio_addr == 0xA2:
                await self._sync_ic9700_dual()
            else:
                await broadcast_state(self.radio_id)
        except Exception as e:
            logging.debug(f"Radio {self.radio_id} initial sync error: {e}")

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
            self.ser.setDTR(False)
            self.ser.setRTS(False)
            self.ser.open()

            self.running = True
            self.state['connected'] = True
            self.state['online'] = False
            self.last_scope_time = 0.0
            self.last_radio_reply = 0.0
            self.power_off_until = 0.0

            self.reader_task = asyncio.create_task(self._read_serial_loop())

            # Kysytään aluksi sekä tunnetulta osoitteelta että molemmilta Icom-osoitteilta (0x94 = IC-7300, 0xA2 = IC-9700)
            await asyncio.sleep(0.10)
            for probe_addr in [self.radio_addr, 0x94, 0xA2, 0x00]:
                if self.ser and self.ser.is_open:
                    self.ser.write(civ_command(0x03, None, None, to_addr=probe_addr))
                    await asyncio.sleep(0.03)

            await asyncio.sleep(0.10)
            if self.state['online']:
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
                self.state['online'] = False
                self.state['ptt'] = 0
                self.state['smeter'] = 'S0'
                self.state['smeter_pct'] = 0.0
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
        self.state['connected'] = False
        self.state['online'] = False
        self.state['ptt'] = 0
        self.state['smeter'] = 'S0'
        self.state['smeter_pct'] = 0.0
        for t in (self.reader_task, self.poller_task):
            if t and not t.done():
                t.cancel()
        self.reader_task = None
        self.poller_task = None
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(civ_command(0x1C, 0x00, bytes([0x00]), to_addr=self.radio_addr))
                self.ser.write(civ_command(0x27, 0x11, bytes([0x00]), to_addr=self.radio_addr))
                self.ser.write(civ_command(0x27, 0x10, bytes([0x00]), to_addr=self.radio_addr))
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        await broadcast_state(self.radio_id)

    def disconnect_sync(self):
        self.running = False
        self.state['connected'] = False
        self.state['online'] = False
        if self.ser and self.ser.is_open:
            try:
                self.ser.write(civ_command(0x1C, 0x00, bytes([0x00]), to_addr=self.radio_addr))
                self.ser.write(civ_command(0x27, 0x11, bytes([0x00]), to_addr=self.radio_addr))
                self.ser.write(civ_command(0x27, 0x10, bytes([0x00]), to_addr=self.radio_addr))
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
                    while 0xFD in buffer:
                        idx = buffer.find(b'\xfd')
                        msg = buffer[:idx+1]
                        buffer = buffer[idx+1:]

                        if b'\xfe\xfe' in msg:
                            msg = msg[msg.find(b'\xfe\xfe'):]
                            if self.vcom_bridge and self.vcom_bridge.vser and self.vcom_bridge.vser.is_open:
                                try:
                                    self.vcom_bridge.vser.write(msg)
                                except Exception:
                                    pass
                            # Jos radio on juuri sammutettu (Power OFF), ohitetaan sammumisen hännystavut
                            if time.monotonic() < self.power_off_until:
                                continue
                            # Seurataan myös lähetettyjen 07 D0 / 07 D1 komentojen echoa jotta tiedämme kumpi bandi vastaa
                            if len(msg) >= 7 and msg[3] == 0xE0 and msg[4] == 0x07:
                                if msg[5] == 0xD0:
                                    self.current_band = 0
                                elif msg[5] == 0xD1:
                                    self.current_band = 1
                            # Aito vastaus radiolta (ei tietokoneen oma CI-V echo, jossa msg[3] == 0xE0)
                            if len(msg) >= 6 and (msg[2] == 0xE0 or msg[2] == 0x00) and msg[3] != 0xE0:
                                # Pelkkä 6 tavun kuittaus (0xFB=OK tai 0xFA=NG) ei tarkoita että radio on päällä,
                                # sillä valmiustilassa oleva CI-V-väylä voi vastata 0xFA ja sammutuskomento 18 00 kuittaa 0xFB.
                                if len(msg) < 7 or msg[4] in (0xFA, 0xFB):
                                    continue
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

    async def _parse_civ(self, msg, force_broadcast=False):
        if len(msg) < 6:
            return
        cmd = msg[4]
        state_changed = force_broadcast

        # Taajuus (00 tai 03)
        if (cmd == 0x00 or cmd == 0x03) and len(msg) >= 11:
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
                        scope_mode = msg[8] if len(msg) > 8 else 0
                        if scope_mode in (0, 2):  # Center mode
                            if len(msg) >= 16:
                                span_code = msg[15]
                                SPAN_MAP = {
                                    0: 5000, 1: 10000, 2: 20000, 3: 50000,
                                    4: 100000, 5: 200000, 6: 500000, 7: 1000000
                                }
                                self.scope_span_hz = SPAN_MAP.get(span_code, 50000)
                                self.scope_mode_center = True
                        elif scope_mode in (1, 3):  # Fixed mode
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
                            ws_msg = json.dumps({
                                "type": "scope",
                                "radio_id": self.radio_id,
                                "div": 0x11,
                                "span_hz": getattr(self, 'scope_span_hz', 50000),
                                "center_mode": getattr(self, 'scope_mode_center', True),
                                "edge_low": getattr(self, 'scope_edge_low', 0),
                                "edge_high": getattr(self, 'scope_edge_high', 0),
                                "data": full_row
                            })
                            await broadcast_ws(ws_msg)

        if state_changed and ws_clients:
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

                # Jos radio ei ole vastannut > 6.0 sekuntiin, merkitään se rauhallisesti OFFLINE/STANDBY-tilaan
                if self.state['online'] and (now - self.last_radio_reply > 6.0):
                    self.state['online'] = False
                    self.state['smeter'] = 'S0'
                    self.state['smeter_pct'] = 0.0
                    await broadcast_state(self.radio_id)

                if self.ser and self.ser.is_open:
                    if not self.state['online']:
                        # Radio on kiinni / virrat pois: kokeillaan kevyesti molempia Icom-osoitteita (0x94 ja 0xA2)
                        if tick % 12 == 0:
                            self.ser.write(civ_command(0x03, None, None, self.radio_addr))
                        elif tick % 12 == 6:
                            alt_addr = 0xA2 if self.radio_addr == 0x94 else 0x94
                            self.ser.write(civ_command(0x03, None, None, alt_addr))
                    else:
                        # Jos lähetys on päällä (PTT), pidetään CI-V-väylä täysin hiljaisena,
                        # jotta USB-väylä ja radion DSP toimivat 100% häiriöttömästi modulointiaudiolla (FT8 tms.)
                        if self.state.get('ptt') == 1:
                            self.last_radio_reply = now
                            self.last_scope_time = now
                            await asyncio.sleep(0.08)
                            continue

                        # Jos radio on online, mutta emme ole saaneet yhtään scope-pakettia yli 2.0 sekuntiin:
                        if now - self.last_scope_time > 2.0:
                            self._enable_scope_hw()
                            self.last_scope_time = now - 0.5

                        # Odotetaan muutama millisekunti jos vesiputouspyyhkäisy (div 1..10) on juuri kesken,
                        # jotta pollaus lähtee siististi div 11 jälkeen eikä katkaise vesiputouspakettia!
                        wait_guard = 0
                        while time.monotonic() < getattr(self, 'scope_burst_until', 0.0) and wait_guard < 8:
                            await asyncio.sleep(0.004)
                            wait_guard += 1

                        # Lähetetään vain YKSI kysely per sykli, jotta CI-V-väylä jää vapaaksi nopealle vesiputoukselle
                        step = tick % 10
                        if step == 0:
                            self.ser.write(civ_command(0x03, None, None, self.radio_addr))
                        elif step == 3:
                            self.ser.write(civ_command(0x04, None, None, self.radio_addr))
                        elif step == 6:
                            self.ser.write(civ_command(0x14, 0x0A, None, self.radio_addr))
                        elif step == 9:
                            if self.radio_addr == 0xA2 and (tick // 10) % 2 == 1:
                                self.ser.write(civ_command(0x16, 0x59, None, self.radio_addr))
                            else:
                                self.ser.write(civ_command(0x1A, 0x06, None, self.radio_addr))
                        else:
                            self.ser.write(civ_command(0x15, 0x02, None, self.radio_addr))
                    tick += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.debug(f"Radio {self.radio_id} poll error: {e}")
            await asyncio.sleep(0.075)

    async def set_ptt(self, ptt_on: bool, is_cat: bool = False):
        tila = 1 if ptt_on else 0
        if self.state['ptt'] == tila and not is_cat and (not self.ser or not self.ser.is_open):
            return
        self.state['ptt'] = tila
        self.cat_ptt = is_cat and (tila == 1)
        if self.ser and self.ser.is_open and self.state['online']:
            try:
                self.ser.write(civ_command(0x1C, 0x00, bytes([tila]), self.radio_addr))
                if tila == 0:
                    await asyncio.sleep(0.015)
                    self.ser.write(civ_command(0x1C, 0x00, bytes([0x00]), self.radio_addr))
            except Exception:
                pass
        if tila == 0:
            if self.radio_id in audio_manager.channels:
                audio_manager.channels[self.radio_id]._stop_tx_stream()
        elif not self.cat_ptt:
            if self.radio_id in audio_manager.channels:
                audio_manager.channels[self.radio_id]._start_tx_stream()
        await broadcast_state(self.radio_id)

    def set_ptt_sync(self, ptt_val: int, is_cat: bool = False):
        tila = 1 if ptt_val else 0
        if self.state['ptt'] == tila and not is_cat:
            return
        self.state['ptt'] = tila
        self.cat_ptt = is_cat and (tila == 1)
        if self.ser and self.ser.is_open and self.state.get('online'):
            try:
                self.ser.write(civ_command(0x1C, 0x00, bytes([tila]), self.radio_addr))
                if tila == 0:
                    time.sleep(0.015)
                    self.ser.write(civ_command(0x1C, 0x00, bytes([0x00]), self.radio_addr))
            except Exception:
                pass
        if tila == 0:
            if self.radio_id in audio_manager.channels:
                audio_manager.channels[self.radio_id]._stop_tx_stream()
        elif not self.cat_ptt:
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
            try:
                self.ser.write(civ_command(0x05, None, hz_to_bcd(hz), to_addr=self.radio_addr))
            except Exception:
                pass
        if app_loop and not app_loop.is_closed():
            asyncio.run_coroutine_threadsafe(broadcast_state(self.radio_id), app_loop)

    def set_mode_sync(self, mode_str: str, filter_val: int = 1):
        self.state['mode'] = mode_str
        self.state['filter'] = filter_val
        self.state['data_on'] = ('-D' in mode_str)
        if self.current_band == 1:
            self.state['sub_mode'] = mode_str
            self.state['sub_filter'] = filter_val
        else:
            self.state['main_mode'] = mode_str
            self.state['main_filter'] = filter_val
        if self.ser and self.ser.is_open and self.state.get('online'):
            try:
                m_base = mode_str.split('-')[0].upper()
                mode_map = {'LSB': 0x00, 'USB': 0x01, 'AM': 0x02, 'CW': 0x03, 'RTTY': 0x04, 'FM': 0x05, 'WFM': 0x06, 'CW-R': 0x07, 'RTTY-R': 0x08, 'DV': 0x17}
                code = mode_map.get(m_base, 0x01)
                self.ser.write(civ_command(0x06, None, bytes([code, filter_val]), to_addr=self.radio_addr))
                if '-D' in mode_str:
                    time.sleep(0.02)
                    self.ser.write(civ_command(0x1A, 0x06, bytes([0x01, filter_val]), to_addr=self.radio_addr))
            except Exception:
                pass
        if app_loop and not app_loop.is_closed():
            asyncio.run_coroutine_threadsafe(broadcast_state(self.radio_id), app_loop)


radios = {
    "1": RadioController("1", default_addr=0x94),
    "2": RadioController("2", default_addr=0xA2)
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
        self.ws_clients = set()
        self.tx_q = queue.Queue(maxsize=120)
        self.tx_buffer = np.array([], dtype=np.float32)
        self.spk_q = queue.Queue(maxsize=120)
        self.spk_buffer = np.array([], dtype=np.float32)
        self.sample_rate = 16000
        self.rx_vol = 1.0
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
        if r and r.state.get('ptt') == 1 and self.active:
            if getattr(r, 'cat_ptt', False):
                return
            self._start_tx_stream()
            if not self.tx_q.full():
                try:
                    self.tx_q.put_nowait(samples_f32.copy())
                except Exception:
                    pass

    def _ensure_rx_stream(self):
        """Varmistaa että radion USB RX -sisääntulovirta on auki kuuntelua ja/tai tallennusta varten."""
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

            # Tallennetaan vain ja ainoastaan tästä radiosta tuleva ääni (16-bit PCM)
            if self.recording:
                pcm16 = (np.clip(gated_mono * self.rx_vol, -1.0, 1.0) * 32767.0).astype(np.int16)
                self.rec_chunks.append(pcm16.tobytes())

            if self.active:
                # Lähetetään WebSocket-asiakkaille (mobiiliselain)
                if self.ws_clients and app_loop and not app_loop.is_closed():
                    data_bytes = gated_mono.tobytes()
                    for ws in list(self.ws_clients):
                        if not ws.closed:
                            try:
                                asyncio.run_coroutine_threadsafe(ws.send_bytes(data_bytes), app_loop)
                            except Exception:
                                pass
                # Puskuroidaan PC-kaiuttimille VAIN JOS local_active on päällä (työpöytäsovelluksen pyynnöstä)
                if self.local_active and self.spk_dev is not None and radios[self.radio_id].state.get('ptt') != 1:
                    scaled = (gated_mono * self.rx_vol).astype(np.float32)
                    if not self.spk_q.full():
                        try:
                            self.spk_q.put_nowait(scaled)
                        except Exception:
                            pass

        dev_info = sd.query_devices(self.rx_dev)
        in_ch = 2 if int(dev_info.get('max_input_channels', 1)) >= 2 else 1
        self.rx_stream = sd.InputStream(
            samplerate=self.sample_rate, channels=in_ch, dtype='float32',
            device=self.rx_dev, callback=rx_callback, blocksize=1024
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

    def _start_tx_stream(self):
        """Käynnistää TX-äänivirran vain kun PC:n tai webin mikrofoni lähettää puhetta."""
        if self.tx_dev is None or not self.active:
            return
        r = radios.get(self.radio_id)
        if r and getattr(r, 'cat_ptt', False):
            return
        if self.tx_stream is not None:
            try:
                if self.tx_stream.active:
                    return
            except Exception:
                pass
            self._stop_tx_stream()

        while not self.tx_q.empty():
            try: self.tx_q.get_nowait()
            except Exception: break
        # Alustetaan pienellä puskurityynyllä (1024 näytettä = 64 ms), jotta WiFi/mobiiliyhteyden jitter ei aiheuta pätkimistä
        self.tx_buffer = np.zeros(1024, dtype=np.float32)

        def tx_callback(outdata, frames, time_info, status):
            while not self.tx_q.empty():
                try:
                    self.tx_buffer = np.concatenate((self.tx_buffer, self.tx_q.get_nowait()))
                except Exception:
                    break
            # Estetään puskurin liiallinen kasvu verkon yli tulevassa puheessa
            if len(self.tx_buffer) > frames * 6:
                self.tx_buffer = self.tx_buffer[-frames * 2:]
            if len(self.tx_buffer) >= frames:
                outdata[:, 0] = self.tx_buffer[:frames]
                self.tx_buffer = self.tx_buffer[frames:]
            elif len(self.tx_buffer) > 0:
                l = len(self.tx_buffer)
                outdata[:l, 0] = self.tx_buffer
                outdata[l:, 0] = 0
                self.tx_buffer = np.zeros(frames // 2, dtype=np.float32)
            else:
                outdata.fill(0)

        try:
            self.tx_stream = sd.OutputStream(
                samplerate=self.sample_rate, channels=1, dtype='float32',
                device=self.tx_dev, callback=tx_callback, blocksize=1024
            )
            self.tx_stream.start()
        except Exception as e:
            logging.error(f"Radio {self.radio_id} TX Stream start error: {e}")

    def _stop_tx_stream(self):
        """Sulkee TX-äänivirran heti kun puhetta ei lähetetä, jotta radion USB-äänikortti pysyy vapaana WSJT-X:lle."""
        if self.tx_stream:
            try:
                self.tx_stream.stop()
                self.tx_stream.close()
            except Exception:
                pass
            self.tx_stream = None
        while not self.tx_q.empty():
            try: self.tx_q.get_nowait()
            except Exception: break
        self.tx_buffer = np.array([], dtype=np.float32)

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

        # Alustetaan radion oma kaiutinpuskuri pienellä 1 blokin (64 ms) puskurityynyllä,
        # jotta kahden eri USB-äänikortin kellovaihe-ero ei aiheuta pätkimistä.
        while not self.spk_q.empty():
            try:
                self.spk_q.get_nowait()
            except Exception:
                break
        self.spk_buffer = np.zeros(1024, dtype=np.float32)

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
            "2": RadioAudioChannel("2")
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
                        device=dev_id, callback=make_mic_cb(dev_id), blocksize=1024
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
                            while not ch.spk_q.empty():
                                try:
                                    chunk = ch.spk_q.get_nowait()
                                    ch.spk_buffer = np.concatenate((ch.spk_buffer, chunk))
                                except Exception:
                                    break
                            # Estetään puskurin liiallinen kasvu kelloryöminnän aikana
                            if len(ch.spk_buffer) > frames * 6:
                                ch.spk_buffer = ch.spk_buffer[-frames * 2:]
                            if len(ch.spk_buffer) >= frames:
                                mix += ch.spk_buffer[:frames]
                                ch.spk_buffer = ch.spk_buffer[frames:]
                                any_audio = True
                            elif len(ch.spk_buffer) > 0:
                                l = len(ch.spk_buffer)
                                mix[:l] += ch.spk_buffer
                                # Lisätään pieni 32 ms tyyny, jotta USB- ja PC-äänikortin kellovaihe ei napsu
                                ch.spk_buffer = np.zeros(frames // 2, dtype=np.float32)
                                any_audio = True
                        if any_audio:
                            outdata[:, 0] = np.clip(mix, -1.0, 1.0)
                        else:
                            outdata.fill(0)
                    return spk_cb

                try:
                    st = sd.OutputStream(
                        samplerate=self.sample_rate, channels=1, dtype='float32',
                        device=dev_id, callback=make_spk_cb(dev_id), blocksize=1024
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


class RigctldServer:
    """Kevyt ja luotettava Hamlib rigctld -yhteensopiva TCP-palvelin WSJT-X, JTDX ja muiden digiohjelmien CAT-ohjaukseen."""
    DUMP_STATE_PAYLOAD = (
        "1\n1\n0\n"
        "150000.000000 1500000000.000000 0x1ff -1 -1 0x17e00007 0x1f\n"
        "0 0 0 0 0 0 0\n"
        "150000.000000 1500000000.000000 0x1ff 5000 100000 0x17e00007 0x1f\n"
        "0 0 0 0 0 0 0\n"
        "0x1ff 1\n0x1ff 0\n0 0\n"
        "0xc 2400\n0xc 1800\n0xc 3000\n0xc 0\n"
        "0x2 500\n0x2 2400\n0x2 50\n0x2 0\n"
        "0x10 300\n0x10 2400\n0x10 50\n0x10 0\n"
        "0x1 8000\n0x1 2400\n0x1 10000\n"
        "0x20 15000\n0x20 8000\n0x40 230000\n"
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
                        cur_mode = radio.state.get("mode", "USB") if radio else "USB"
                        is_data = radio.state.get("data_on", False) or ("-D" in cur_mode) if radio else False
                        base = cur_mode.split("-")[0].upper()
                        if is_data:
                            resp = f"PKT{base}\n3000\n" if base in ("USB", "LSB", "FM") else f"{base}\n3000\n"
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
                            else:
                                target_mode = m_arg
                            try:
                                radio.set_mode_sync(target_mode)
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
                                radio.set_ptt_sync(ptt_val, is_cat=True)
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
                        conn.sendall(b"RPRT 0\n")
                    elif c in ("x", "\\get_split_mode", "get_split_mode"):
                        conn.sendall(b"PKTUSB\n3000\n")
                    elif c in ("X", "\\set_split_mode", "set_split_mode"):
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
    "2": RigctldServer("2", 4533)
}


async def global_ptt_monitor_loop():
    """Tarkkailee Win32 GetAsyncKeyState-rajapinnalla määritettyjä PTT-näppäimiä."""
    while True:
        try:
            win_active = is_desktop_window_active()
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
        await asyncio.sleep(0.015)


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
    if rid in global_config["radios"]:
        for key in ("port", "baud", "vfo_offset", "ptt_key_code", "ptt_key_name", "ptt_global",
                    "audio_rx_dev", "audio_tx_dev", "audio_spk_dev", "audio_mic_dev", "audio_rx_vol"):
            if key in data:
                global_config["radios"][rid][key] = data[key]
        save_config(global_config)
    return web.json_response({"status": "ok", "config": global_config["radios"].get(rid, {})})


async def api_shutdown(request):
    try:
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
        return web.json_response({"status": "ok", "active": True})
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
        addr = radio.radio_addr
        if cmd_type == 'taajuus':
            hz = int(data['hz'])
            target_vfo = data.get('vfo')  # 'main', 'sub' tai None
            if addr == 0xA2 and target_vfo in ('main', 'sub'):
                want_band = 1 if target_vfo == 'sub' else 0
                if want_band == 1 and radio.state.get('dual_rx', 0) == 0:
                    radio.state['dual_rx'] = 1
                    radio.ser.write(civ_command(0x16, 0x59, bytes([0x01]), addr))
                    await asyncio.sleep(0.04)
                if radio.current_band != want_band:
                    radio.current_band = want_band
                    radio.state['active_band'] = want_band
                    radio.ser.write(civ_command(0x07, 0xD1 if want_band == 1 else 0xD0, None, addr))
                    await asyncio.sleep(0.04)
            radio.state['taajuus_hz'] = hz
            radio.state['taajuus_mhz'] = f"{(hz / 1000000):.6f}"
            if radio.current_band == 1:
                radio.state['sub_hz'] = hz
            else:
                radio.state['main_hz'] = hz
            radio.ser.write(civ_command(0x05, None, hz_to_bcd(hz), addr))
            await broadcast_state(rid)

        elif cmd_type == 'select_band':
            want_band = 1 if str(data.get('band', '0')).lower() in ('1', 'sub') else 0
            if want_band == 1 and radio.state.get('dual_rx', 0) == 0:
                radio.state['dual_rx'] = 1
                radio.ser.write(civ_command(0x16, 0x59, bytes([0x01]), addr))
                await asyncio.sleep(0.04)
            radio.current_band = want_band
            radio.state['active_band'] = want_band
            radio.ser.write(civ_command(0x07, 0xD1 if want_band == 1 else 0xD0, None, addr))
            await asyncio.sleep(0.04)
            radio.ser.write(civ_command(0x03, None, None, addr))
            await asyncio.sleep(0.03)
            radio.ser.write(civ_command(0x04, None, None, addr))
            await asyncio.sleep(0.03)
            radio.ser.write(civ_command(0x14, 0x0A, None, addr))
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

        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"status": "virhe", "error": str(e)})


async def websocket_handler(request):
    global desktop_window_focused
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


def create_app():
    app = web.Application()
    app.add_routes([
        web.get('/', handle_index),
        web.get('/desktop', handle_desktop),
        web.get('/api/ports', api_ports),
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
        web.get('/ws', websocket_handler),
        web.get('/ws_audio', websocket_audio_handler)
    ])
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
        for rid in ("1", "2"):
            rconf = global_config["radios"].get(rid, {})
            if rconf.get("auto_connect", True) and rconf.get("port"):
                await radios[rid].connect(rconf["port"], rconf.get("baud", 115200))

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
    webview.start()