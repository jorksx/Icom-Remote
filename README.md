# Icom & Yaesu Remote - Multi-Radio Web & Desktop Controller

A modern, high-performance remote control and spectrum monitoring application for **Icom transceivers** (**IC-7300**, **IC-9700**, and other CI-V radios) as well as **Yaesu HF transceivers** (**FT-891**, **FT-991A**, **FT-DX10**).

Featuring real-time 2D/3D waterfall spectrum display with interactive bookmark markers, ultra-low-latency (16 ms) bidirectional audio streaming & contest-grade PTT turnaround, VHF/UHF repeater & CTCSS / 1750 Hz tone burst automation, built-in Hamlib `rigctld` server for WSJT-X / digital modes, per-radio WAV audio recording, APRS digipeater with OpenStreetMap, and a mobile-friendly HTTPS interface.

![ICOM Remote Screenshot](docs/screenshot.png)

---

## 🆕 What's New (Latest Release)

- **⚡ Ultra-Low Latency Contest PTT & RX Audio Recovery (16 ms):**
  - Zero-delay hardware PTT unkey (`TX0;` / `1C 00 00`) sent at `0 ms` before audio stream teardown.
  - Upgraded audio engine to `256`-sample (`16 ms`, `latency='low'`) blocks with asynchronous `abort()` TX teardown and instant speaker queue flush so you never miss the first syllable of an answering station in SSB contests.
  - Direct WebSocket PTT signaling and `5 ms` kernel-level keyboard & mouse side-button (`Mouse4` / `Mouse5` / `MouseMid`) PTT polling.
- **📍 Interactive Waterfall Bookmark Markers (IC-7300 & IC-9700):**
  - Saved bookmarks within the active waterfall span automatically appear on the 2D/3D hardware waterfall with a vertical dashed marker line, pointer triangle (`▼`), and station/repeater name badge (e.g., `Pori`).
  - **Click-on-Marker Tuning:** Click any bookmark badge directly on the waterfall to jump to that frequency and automatically load its mode and repeater settings.
  - Accurate CI-V `27 00` Division 1 real-time span decoding (`±2.5 kHz` .. `±500 kHz`) with left/right band-edge frequency indicators.
- **🗼 VHF/UHF Repeater, CTCSS & 1750 Hz Tone Burst System (IC-9700):**
  - Full repeater control bar for VHF/UHF: **DUP (`OFF` / `DUP-` / `DUP+`)**, **Shift Offset (MHz)**, **Tone Mode (`OFF` / `1750Hz` / `TONE` / `TSQL`)**, and **50 Standard CTCSS Sub-Audible Tones (`67.0 Hz` – `254.1 Hz`)**.
  - **Dedicated `🔔 TONE` (1750 Hz) Burst Button:** Compact wake-up tone button next to PTT that keys the radio and transmits a pure 1750 Hz sine wave burst (short click sends an automatic 650 ms pulse, or hold for manual length).
  - **Automatic Per-Repeater & Per-Frequency Memory:** Remembers `DUP`, `Shift`, `TONE`, and `CTCSS` settings for each repeater/bookmark automatically when switching frequencies.
  - **Smart Dual-VFO (`MAIN` / `SUB`) Band Swap:** Jumping from a VHF bookmark to a UHF bookmark (or vice versa) on IC-9700 automatically triggers `Swap M/S (07 B0)` so `MAIN VFO` seamlessly switches to the target band without flicker.
- **🔬 Razor-Sharp 2D & 3D Waterfall Engine:**
  - 1D Unsharp Mask peak deconvolution and 72nd-percentile adaptive noise floor tracking eliminate wide-pixel blur and background mush while keeping narrow CW/SSB/FM signals 1-pixel sharp.
  - Native 1×1 pixel high-DPI thermal noise decorrelation between CI-V sweeps.
- **📻 Up to 3 Simultaneous Radios & Full Yaesu FT-891 Support:**
  - Dynamic 1-to-3 radio deck layout (`IC-7300`, `IC-9700`, `Yaesu FT-891`) with top-bar visibility pills.
  - Full Yaesu CAT support including menu `11-07 [SSB BFO]` forced `LSB` / `USB` sideband switching, calibrated hardware S-meter (`S0..S9..+60dB`), and a dedicated **3.2 kHz Passband Audio FFT 2D/3D Waterfall**.
- **🎙️ Per-Radio Audio Recording (`REC` -> `.wav`) & Interactive S-Meter Squelch:**
  - Record received audio directly from any radio to a timestamped 16-bit PCM `.wav` file (`Model_Freq_Mode_Timestamp.wav`).
  - Clickable & draggable **SQL (Squelch)** threshold marker directly on the S-meter bar with automatic audio activation.

---

## ✨ Core Features

- **Multi-Radio Management (Up to 3 Radios Simultaneously):**
  - Control up to three transceivers at the same time (e.g., **IC-7300** for HF/50 MHz, **IC-9700** for VHF/UHF/1.2 GHz, and **Yaesu FT-891**).
  - Independent VFO controls, IC-9700 Dualwatch (`MAIN` & `SUB` VFO) synchronization, split operation, and quick band switching.

- **High-Resolution 2D & 3D Waterfall & Spectrum Engine:**
  - Real-time CI-V spectrum scope capture directly over USB at up to 60 FPS (plus 4096-point Hanning FFT audio scope for Yaesu).
  - **1D Peak Sharpening & Adaptive Noise Gate:** Keeps background band noise dark and clean while illuminating weak signals and SSB speech formants.
  - **Interactive Bookmark Overlay:** Displays saved bookmarks directly on the waterfall at their exact RF coordinates.
  - **Dynamic Floor, Gain, Peak & Speed Controls:** Floor range `-30` to `+60`, Gain `10` to `200`, Peak `30` to `160`, and scroll speed `1x` to `5x` with persistent per-radio memory.
  - **Interactive 3D Waterfall (3DSS):** High-density perspective surface ridges with synchronized dynamic range.
  - **Click-to-Tune & Mouse Wheel Tuning:** Click anywhere on the spectrum, waterfall, or bookmark markers to tune; hover and scroll mouse wheel to step in 0.5 kHz increments.

- **Low-Latency Bidirectional Audio & Contest PTT:**
  - Isolated audio paths: streaming audio to a mobile phone or remote browser does **not** trigger the local PC speakers in the shack.
  - Ultra-low 16 ms audio block processing with anti-VOX loopback protection for external interfaces (e.g., SignaLink USB).
  - Bind any keyboard key or mouse side button (`Mouse4`, `Mouse5`, `MouseMid`) as a local or global PTT trigger.

- **Frequency Bookmarks, Quick Bar & Repeater Profiles:**
  - Per-radio filtered **Quick Bookmarks** strip right below the VFO display (shows only bookmarks assigned to that specific transceiver).
  - Strict target radio locking (`IC-7300`, `IC-9700`, `Yaesu FT-891`, or `AUTO`) with a smooth *"Radio not connected"* fade notification if the target radio is offline.
  - Store complete VHF/UHF repeater profiles (`DUP-`/`DUP+`, `Shift MHz`, `TONE`/`TSQL`/`1750Hz`, and `CTCSS Hz`) inside bookmarks.
  - **Drag-and-Drop Reordering:** Freely rearrange saved station memories with drag handle (`⠿`) or quick `▲` / `▼` step buttons.

- **Mobile Web Interface (HTTPS) & Configurable PIN Security:**
  - Fully responsive web UI accessible from any smartphone, tablet, or secondary laptop on your LAN/Wi-Fi or over VPN/Tailscale.
  - Secure HTTPS support for microphone input and Web Audio API permissions.
  - **Configurable PIN Protection:** Quick topbar button to change PIN code or disable PIN prompt completely for seamless local network access.

- **Built-in Hamlib `rigctld` Emulation:**
  - Integrated rigctld TCP servers (ports `4532`, `4533`, and `4534`).
  - Connect **WSJT-X**, **JTDX**, **FLDigi**, **Log4OM**, or **N1MM** directly over TCP without needing physical CI-V splitters or virtual COM-port pairs.

- **Integrated 2m APRS Packet Radio, OpenStreetMap & Digipeater:**
  - Real-time 1200 baud Bell 202 AFSK packet demodulation directly from radio USB audio (144.800 MHz FM with Frequency Safety Lock).
  - Interactive **OpenStreetMap / CartoDB Dark Matter** station map with APRS symbols, SSID badges, distance/bearing, and weather station telemetry.
  - Autonomous **New-N Paradigm (`WIDE1-1`, `WIDE2-n`) Digipeater** with duplicate suppression, carrier-sense (CSMA), and periodic position/weather beaconing.

---

## 🚀 Getting Started

### Prerequisites

- **Python 3.10+** (if running from source)
- Icom transceiver connected via standard USB cable (Silicon Labs CP210x USB to UART bridge driver installed).
- Windows, Linux, or macOS.

### Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/jorks/icom-remote.git
   cd icom-remote
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure your radios:**
   - Copy `icom_config.example.json` to `icom_config.json` (or let the app auto-generate it on first launch):
   ```bash
   cp icom_config.example.json icom_config.json
   ```
   - Set the appropriate COM ports (e.g. `COM8` for IC-7300 and `COM9` for IC-9700) and baud rate (`115200`).

4. **Run the application:**
   ```bash
   python server.py
   ```

---

## 📱 Mobile Remote Access

1. Ensure the host computer and your mobile device are connected to the same local network (Wi-Fi) or VPN.
2. In the desktop application, the local HTTPS URL is displayed (e.g. `https://192.168.1.xxx:8085`).
3. Open the URL in your mobile browser (Safari, Chrome, Firefox).
4. Because self-signed certificates are used for local LAN encryption, accept the browser certificate warning once to enable audio and microphone streaming.

---

## 🛠️ Configuration & WSJT-X Setup

### Connecting WSJT-X to Built-in rigctld
1. In WSJT-X, open **File -> Settings -> Radio**.
2. Set **Rig:** `Hamlib NET rigctl`.
3. Set **Network Server:** `127.0.0.1:4532` (for Radio 1 / IC-7300) or `127.0.0.1:4533` (for Radio 2 / IC-9700).
4. Set **PTT Method:** `CAT`.
5. Click **Test CAT** — it will immediately turn green!

---

## 📦 Building Standalone Executable (Windows)

To package the application into a single standalone `.exe` using PyInstaller:

```bash
pyinstaller server.spec
```

The resulting executable will be generated inside the `dist/` directory.

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
