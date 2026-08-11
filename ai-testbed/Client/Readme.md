## Client (Receiver) — Run Instructions

### 1) Edit `client.sh` variables
Open `Client/client.sh` and update the values under **“EDIT THESE VARIABLES”**:

- **WebRTC signaling address (Python / aiortc):**
  - `SIGNAL_IP` → the **server’s IP** used for signaling
  - `SIGNAL_PORT` → the **server’s signaling port** (must match the server side)

- **Optional frame saving (Python):**
  - `SAVE_FRAMES=1` enables saving PNG frames using `client.py`
  - `OUT_DIR="stream_results"` chooses the output folder name  
  - Set `SAVE_FRAMES=0` to disable saving.

- **Optional upsampling before display/save (Python):**
  - `UPSAMPLE=1` enables upsampling on the received frames
  - `UPSAMPLE_SCALE="2.0"` (e.g., `2.0`, `3.0`, `4.0`)
  - `UPSAMPLE_MODE="bicubic"` (`nearest`, `bilinear`, or `bicubic`)
  - Set `UPSAMPLE=0` to disable upsampling.

- **Optional object detection on the received frames (Python / GPU):**
  - `DETECT=1` enables detection in `client.py`
  - `DETECT_THRESHOLD="0.35"` sets the confidence threshold *(0–1)*  
    - Higher = fewer boxes (more strict), Lower = more boxes (more sensitive)
  - `DETECT_EVERY="1"` runs detection every **N** frames  
    - Use `5` or `10` to reduce overhead while keeping periodic detections
  - `DETECT_MAX="25"` caps the maximum number of drawn detections per frame  
  - Set `DETECT=0` to disable detection.

---

### 2) Update the C++ input client target (keyboard/mouse TCP)
The C++ input client **does not** take IP/port from `client.sh` in this setup.

You must open **`Client/client.cpp`** and update the **server IP and port** inside the file (where it currently connects using a hardcoded IP like `XX.XXX.XX.XXX` and port like `XXXX`).  
This must point to the **machine running the input server** (the server-side C++ program).

> If you only change `SIGNAL_IP/SIGNAL_PORT` in `client.sh` but you forget to update the hardcoded IP/port in `client.cpp`, video may work but keyboard/mouse will not.

---

### 3) Run the client
From the `Client/` directory:

```bash
chmod +x client.sh
./client.sh
```

---

### What `client.sh` does
- Starts the **Python WebRTC client** (`client.py`) using the signaling IP/port you set.
- Always rebuilds the **C++ input client** (`client.cpp`) and runs it.
- If the Python WebRTC client exits (or you press Ctrl+C), it automatically stops the C++ input client too.
