#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

cleanup() {
  kill ${PY_PID:-} ${IO_PID:-} 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# =========================
# EDIT THESE VARIABLES
# =========================
SIGNAL_IP="192.168.2.114"
SIGNAL_PORT="8080"

PYTHON_BIN="python"
SERVER_PY="server.py"

SERVER_CPP="server.cpp"
SERVER_BIN="./server"

# Video / capture config
WIDTH=1920
HEIGHT=1080
MONITOR=1
DOWNSAMPLE=1              # 1=no downsample, 2=half res, 3=third res, ...

# Optional analysis / logging
ENABLE_COMPLEXITY=1       # 1 = enable EVCA complexity analyzer (logs SC every GOP)
GOP_LEN=45                # used when ENABLE_COMPLEXITY=1 or ENABLE_EVCA_BITRATE=1
COMPLEXITY_BLOCK=16       # used when ENABLE_COMPLEXITY=1 or ENABLE_EVCA_BITRATE=1

# Optional EVCA -> bitrate ladder control (writes bitrate_send_shm)
ENABLE_EVCA_BITRATE=1     # 1 = enable EVCA->bitrate control

ENABLE_QUALITY_LOGGING=1  # 1 = overlay QR codes onto frames
QR_SIZE=20                # only used if ENABLE_QUALITY_LOGGING=1
QR_CORNER="tr"            # tr|tl|br|bl (only used if ENABLE_QUALITY_LOGGING=1)
# =========================

echo "[1/2] Start WebRTC server (signaling ${SIGNAL_IP}:${SIGNAL_PORT})"

PY_ARGS=(offer
  --width "${WIDTH}"
  --height "${HEIGHT}"
  --monitor "${MONITOR}"
  --downsample "${DOWNSAMPLE}"
  --signaling tcp-socket
  --signaling-host "${SIGNAL_IP}"
  --signaling-port "${SIGNAL_PORT}"
)

if [ "${ENABLE_COMPLEXITY}" -eq 1 ]; then
  PY_ARGS+=(--enable-complexity --gop-len "${GOP_LEN}" --complexity-block "${COMPLEXITY_BLOCK}")
fi

if [ "${ENABLE_EVCA_BITRATE}" -eq 1 ]; then
  # server.py auto-enables complexity when --evca-bitrate-control is set
  PY_ARGS+=(--evca-bitrate-control --gop-len "${GOP_LEN}" --complexity-block "${COMPLEXITY_BLOCK}")
fi

if [ "${ENABLE_QUALITY_LOGGING}" -eq 1 ]; then
  PY_ARGS+=(--quality-logging --qr-size "${QR_SIZE}" --qr-corner "${QR_CORNER}")
fi

${PYTHON_BIN} "${SERVER_PY}" "${PY_ARGS[@]}" &
PY_PID=$!

echo "[2/2] Build + run C++ input server (always rebuild)"
g++ -O2 -std=c++17 "${SERVER_CPP}" -o "${SERVER_BIN}" -lX11 -lXtst -pthread
chmod +x "${SERVER_BIN}"

${SERVER_BIN} &
IO_PID=$!

# Wait until python exits (Ctrl+C triggers cleanup trap)
wait ${PY_PID} || true
