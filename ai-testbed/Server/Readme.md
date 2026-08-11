## Server (Sender) — Run Instructions

### 1) Edit `server.sh` variables
Open `server.sh` and update the values under **“EDIT THESE VARIABLES”**:

#### A) WebRTC signaling (Python `server.py`)
These must match what the **client** uses:
- `SIGNAL_IP` → the IP address used for signaling (your server’s reachable IP)
- `SIGNAL_PORT` → signaling port (must match client)

#### B) Video / capture configuration (Python `server.py`)
Controls what the server captures and streams:
- `WIDTH` / `HEIGHT` → capture resolution (e.g., `1920x1080`)
- `MONITOR` → which monitor to capture (usually `1` = primary)
- `DOWNSAMPLE` → downsample factor before encoding:
  - `1` = no downsample
  - `2` = half resolution
  - `3` = third resolution  
  (Useful to reduce bandwidth / latency.)

#### C) Optional EVCA complexity analyzer (Python `server.py`)
Only used if you want complexity metrics:
- `ENABLE_COMPLEXITY=1` to enable, `0` to disable
- `GOP_LEN` → GOP length used for averaging / logging (only if enabled)
- `COMPLEXITY_BLOCK` → EVCA block size (only if enabled)

#### D) Optional quality logging (Python `server.py`)
If enabled, the server overlays QR codes for quality evaluation:
- `ENABLE_QUALITY_LOGGING=1` to enable, `0` to disable
- `QR_SIZE` → QR code size (pixels)
- `QR_CORNER` → placement: `tr | tl | br | bl`

#### E) Optional EVCA → bitrate ladder control (rate-controller hook)
This enables a simple *content-aware* bitrate controller driven by EVCA complexity. When enabled, the server computes EVCA SC and maps it to a target bitrate “ladder”, then applies that target through the sender-side rate-control hook (shared memory / encoder controls).
- `ENABLE_EVCA_BITRATE=1` to enable, `0` to disable  
- When enabled, `server.sh` adds: `--evca-bitrate-control`
---

### 2) Update the C++ input server target (keyboard/mouse TCP)
The C++ input server **does not** take IP/port from `server.sh` in this setup.

You must open **`Server/server.cpp`** and update the **server IP and port** inside the file (where it currently connects using a hardcoded IP like `XX.XXX.XX.XXX` and port like `XXXX`).  
This must point to the **machine running the input server** (the server-side C++ program).

> If you only change `SIGNAL_IP/SIGNAL_PORT` in `server.sh` but you forget to update the hardcoded IP/port in `server.cpp`, video may work but keyboard/mouse will not.

---

### ) Run the server
From the `Server/` directory:

```bash
chmod +x server.sh
./server.sh
```

---

