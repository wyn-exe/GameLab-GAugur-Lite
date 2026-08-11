#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

cleanup() {
  kill ${PY_PID:-} ${IO_PID:-} 2>/dev/null || true
  wait ${PY_PID:-} ${IO_PID:-} 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# =========================
# EDIT THESE VARIABLES
# =========================
SIGNAL_IP="192.168.2.114"
SIGNAL_PORT="8080"

# Frame saving (Python client.py)
SAVE_FRAMES=1              # 1 = enable --save-frames
OUT_DIR="stream_results"   # used only if SAVE_FRAMES=1

# Upsampling (Python client.py)
UPSAMPLE=0                 # 1 = enable --upsample
UPSAMPLE_SCALE="2.0"       # e.g., 2.0, 3.0, 4.0
UPSAMPLE_MODE="bicubic"    # nearest | bilinear | bicubic

# Object detection (Python client.py)
DETECT=1                   # 1 = enable --detect
DETECT_THRESHOLD="0.25"    # confidence threshold (0..1)
DETECT_EVERY="1"           # run detector every N frames (e.g., 1, 2, 5, 10)
DETECT_MAX="70"            # max boxes to draw

PYTHON_BIN="python"
CLIENT_PY="client.py"

CLIENT_CPP="client.cpp"
CLIENT_BIN="./client"
# =========================

PY_ARGS=(
  "answer"
  "--signaling" "tcp-socket"
  "--signaling-host" "${SIGNAL_IP}"
  "--signaling-port" "${SIGNAL_PORT}"
)

if [ "${SAVE_FRAMES}" -eq 1 ]; then
  PY_ARGS+=("--save-frames" "--out-dir" "${OUT_DIR}")
  echo "[info] Frame saving enabled -> ${OUT_DIR}"
fi

if [ "${UPSAMPLE}" -eq 1 ]; then
  PY_ARGS+=("--upsample" "--upsample-scale" "${UPSAMPLE_SCALE}" "--upsample-mode" "${UPSAMPLE_MODE}")
  echo "[info] Upsample enabled -> scale=${UPSAMPLE_SCALE}, mode=${UPSAMPLE_MODE}"
fi

if [ "${DETECT}" -eq 1 ]; then
  PY_ARGS+=(
    "--detect"
    "--detect-threshold" "${DETECT_THRESHOLD}"
    "--detect-every" "${DETECT_EVERY}"
    "--detect-max" "${DETECT_MAX}"
  )
  echo "[info] Detect enabled -> thr=${DETECT_THRESHOLD}, every=${DETECT_EVERY}, max=${DETECT_MAX}"
fi

echo "[1/2] Start WebRTC client (signaling ${SIGNAL_IP}:${SIGNAL_PORT})"
${PYTHON_BIN} "${CLIENT_PY}" "${PY_ARGS[@]}" &
PY_PID=$!

echo "[2/2] Build + run C++ input client (always rebuild)"
g++ -O2 -std=c++17 "${CLIENT_CPP}" -o "${CLIENT_BIN}" -lX11 -pthread
chmod +x "${CLIENT_BIN}"

${CLIENT_BIN} &
IO_PID=$!

wait ${PY_PID} || true
echo "[info] WebRTC client exited. Stopping input client..."
kill ${IO_PID} 2>/dev/null || true
wait ${IO_PID} 2>/dev/null || true
