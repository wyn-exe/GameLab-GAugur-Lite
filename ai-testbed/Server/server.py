#!/usr/bin/env python3
"""aiortc server: screen capture + optional EVCA complexity analyzer + optional QR overlay.

Downsample happens BEFORE QR + complexity by default (faster + matches transmitted resolution).

Optional EVCA -> bitrate ladder control
- If enabled, the server computes EVCA SC per frame, averages it over each GOP window,
  then selects a bitrate from a ladder defined in code (EVCA_BITRATE_LADDER_Mbps).
- The selected bitrate is written to shared memory (bitrate_send_shm) for your patched
  aiortc GCC to read.
- The value is capped by GCC's estimate from shared memory (bitrate_shm) when available:
    target = min(ladder_bitrate, gcc_bitrate)

Shared memory (expected by your patched GCC)
- bitrate_shm      : int32 (bps) written by GCC (your patch packs GCC bitrate here)
- bitrate_send_shm : int32 (bps) read by GCC (your patch reads override bitrate here)

To change the bitrate logic, edit EVCA_BITRATE_LADDER_Mbps below.
"""

import argparse
import asyncio
import atexit
import logging
import struct
import time
from functools import lru_cache
from multiprocessing import shared_memory
from typing import Optional, List, Tuple

import cv2
import mss
import numpy as np

# =========================================================
# EVCA -> bitrate ladder (EDIT IN CODE)
# =========================================================
# Each entry is (SC_upper_threshold, Mbps).
EVCA_BITRATE_LADDER_Mbps: List[Tuple[float, int]] = [
    (55.0, 8),
    (70.0, 12),
    (float("inf"), 16),
]

# Safety clamps (EDIT IN CODE)
EVCA_MIN_Mbps = 2
EVCA_MAX_Mbps = 50

# Shared memory names (keep consistent with your patched aiortc)
SHM_GCC_NAME = "bitrate_shm"
SHM_OUT_NAME = "bitrate_send_shm"
SHM_INT32_SIZE = 4


def _ladder_bps(sc_avg: float) -> int:
    """Map GOP-average SC -> bitrate (bps) using the code-defined ladder."""
    sc = float(sc_avg)
    mbps = int(EVCA_BITRATE_LADDER_Mbps[-1][1])
    for thr, m in EVCA_BITRATE_LADDER_Mbps:
        if sc <= float(thr):
            mbps = int(m)
            break
    bps = mbps * 1_000_000
    bps = max(EVCA_MIN_Mbps * 1_000_000, min(int(bps), EVCA_MAX_Mbps * 1_000_000))
    return int(bps)


# ---------------------------------------------------------
# Bootstrap: ensure bitrate_send_shm exists BEFORE importing aiortc
# (important if your patched aiortc opens the shm at import time)
# ---------------------------------------------------------
_BOOTSTRAP_SHM: Optional[shared_memory.SharedMemory] = None
try:
    _BOOTSTRAP_SHM = shared_memory.SharedMemory(name=SHM_OUT_NAME, create=True, size=SHM_INT32_SIZE)
    # Do NOT initialize to 0 (patched GCC would force bitrate 0 until first GOP update).
    struct.pack_into("i", _BOOTSTRAP_SHM.buf, 0, int(_ladder_bps(0.0)))
except FileExistsError:
    try:
        _BOOTSTRAP_SHM = shared_memory.SharedMemory(name=SHM_OUT_NAME)
    except Exception:
        _BOOTSTRAP_SHM = None
except Exception:
    _BOOTSTRAP_SHM = None


@atexit.register
def _bootstrap_cleanup() -> None:
    # We intentionally do NOT unlink shared memory here.
    # Users typically clean /dev/shm manually between runs.
    try:
        if _BOOTSTRAP_SHM is not None:
            _BOOTSTRAP_SHM.close()
    except Exception:
        pass


from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaBlackhole, MediaRecorder
from aiortc.contrib.signaling import BYE, add_signaling_arguments, create_signaling
from aiortc.stats import RTCOutboundRtpStreamStats
from av import VideoFrame


# -----------------------------
# Shared-memory bitrate control
# -----------------------------
class SharedMemoryBitrateController:
    """EVCA->bitrate ladder written into shared memory for a patched GCC."""

    INT_FMT = "i"  # int32

    def __init__(self, shm_gcc_name: str = SHM_GCC_NAME, shm_out_name: str = SHM_OUT_NAME):
        self.shm_gcc_name = str(shm_gcc_name)
        self.shm_out_name = str(shm_out_name)

        self._shm_gcc: Optional[shared_memory.SharedMemory] = None
        self._shm_out: Optional[shared_memory.SharedMemory] = None

    def open(self) -> None:
        # GCC shm is created by your patched aiortc (or another process).
        try:
            self._shm_gcc = shared_memory.SharedMemory(name=self.shm_gcc_name)
        except FileNotFoundError:
            self._shm_gcc = None
            logging.warning("GCC shm '%s' not found yet (will skip GCC capping until it appears).", self.shm_gcc_name)
        except Exception:
            self._shm_gcc = None
            logging.exception("Failed to open GCC shm '%s'.", self.shm_gcc_name)

        # Out shm should already exist due to bootstrap; attach (or create as fallback).
        try:
            self._shm_out = shared_memory.SharedMemory(name=self.shm_out_name)
        except FileNotFoundError:
            self._shm_out = shared_memory.SharedMemory(name=self.shm_out_name, create=True, size=SHM_INT32_SIZE)
            struct.pack_into(self.INT_FMT, self._shm_out.buf, 0, int(_ladder_bps(0.0)))

    def close(self) -> None:
        for shm in (self._shm_gcc, self._shm_out):
            try:
                if shm is not None:
                    shm.close()
            except Exception:
                pass

    def _read_int32(self, shm: Optional[shared_memory.SharedMemory]) -> Optional[int]:
        if shm is None:
            return None
        try:
            return int(struct.unpack_from(self.INT_FMT, shm.buf, 0)[0])
        except Exception:
            return None

    def read_gcc_bps(self) -> Optional[int]:
        # Lazy attach if GCC shm appears later.
        if self._shm_gcc is None:
            try:
                self._shm_gcc = shared_memory.SharedMemory(name=self.shm_gcc_name)
            except Exception:
                self._shm_gcc = None
        return self._read_int32(self._shm_gcc)

    def write_target_bps(self, bps: int) -> None:
        if self._shm_out is None:
            return
        try:
            struct.pack_into(self.INT_FMT, self._shm_out.buf, 0, int(max(0, int(bps))))
        except Exception:
            pass

    def update_from_complexity(self, sc_avg: float) -> Tuple[int, int, Optional[int]]:
        """Compute target bitrate from SC, cap by GCC if available, write to shm.

        Returns: (target_bps_written, desired_bps, gcc_bps_or_None)
        """
        desired = _ladder_bps(sc_avg)
        gcc = self.read_gcc_bps()
        target = desired
        if gcc is not None and gcc > 0:
            target = min(int(desired), int(gcc))

        # Clamp again for safety
        target = max(EVCA_MIN_Mbps * 1_000_000, min(int(target), EVCA_MAX_Mbps * 1_000_000))
        self.write_target_bps(target)
        return int(target), int(desired), gcc


# -----------------------------
# Optional complexity analyzer
# -----------------------------
class ComplexityAnalyzer:
    """EVCA-style complexity analyzer with lazy imports."""

    def __init__(self, block_size: int = 16, device: str = "cuda"):
        try:
            import torch
            import torch_dct as dct
            from libs.weight_dct_frame import weight_dct_frame
        except Exception as e:
            raise RuntimeError(
                "Complexity analyzer enabled but required deps are missing.\n"
                "Install: torch, torch_dct and ensure libs/weight_dct_frame.py exists.\n"
                f"Original error: {e}"
            )

        self.torch = torch
        self.dct = dct
        self.weight_dct_frame = weight_dct_frame

        self.block_size = int(block_size)
        self.device = torch.device(device if torch.cuda.is_available() and str(device).startswith("cuda") else "cpu")
        self._weights = self.weight_dct_frame(self.block_size, self.device)

    def evca_sc(self, bgr_frame: np.ndarray) -> float:
        h, w, _ = bgr_frame.shape
        bs = self.block_size

        img_yuv = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2YUV_I420)
        Y = img_yuv[:h, :w]

        h2 = (h // bs) * bs
        w2 = (w // bs) * bs
        if h2 <= 0 or w2 <= 0:
            return 0.0
        Y = Y[:h2, :w2]

        torch = self.torch
        dct = self.dct

        Yt = torch.from_numpy(Y).to(self.device)
        b = Yt.unfold(0, bs, bs).unfold(1, bs, bs).contiguous().view(-1, bs, bs)
        DTs = dct.dct_2d(b)

        energy = torch.abs(DTs * self._weights.unsqueeze(0))
        sc_blocks = energy.mean(dim=(1, 2)) / (bs * bs)
        return float(sc_blocks.mean().item())


# -----------------------------
# Optional QR overlay for quality logging
# -----------------------------
class QualityLogger:
    """Optional QR-code overlay for quality logging."""

    def __init__(self, qr_size: int = 160, margin: int = 16, corner: str = "tr"):
        try:
            import qrcode  # noqa: F401
            from PIL import Image  # noqa: F401
        except Exception as e:
            raise RuntimeError(
                "Quality logging enabled but deps are missing.\n"
                "Install: qrcode[pil]\n"
                f"Original error: {e}"
            )

        self.qr_size = int(qr_size)
        self.margin = int(margin)
        self.corner = str(corner)

    @staticmethod
    @lru_cache(maxsize=2048)
    def _make_qr_rgb(payload: str) -> np.ndarray:
        import qrcode

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=4,
            border=1,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return np.array(img, dtype=np.uint8)

    def overlay(self, bgr: np.ndarray, payload: str) -> None:
        qr_rgb = self._make_qr_rgb(payload)
        qr_rgb = cv2.resize(qr_rgb, (self.qr_size, self.qr_size), interpolation=cv2.INTER_NEAREST)
        qr_bgr = cv2.cvtColor(qr_rgb, cv2.COLOR_RGB2BGR)

        h, w, _ = bgr.shape
        m = self.margin
        qs = self.qr_size
        if qs <= 0 or h <= 0 or w <= 0:
            return

        if self.corner == "tr":
            y0, x0 = m, w - qs - m
        elif self.corner == "tl":
            y0, x0 = m, m
        elif self.corner == "br":
            y0, x0 = h - qs - m, w - qs - m
        else:  # "bl"
            y0, x0 = h - qs - m, m

        y0 = max(0, min(y0, h - qs))
        x0 = max(0, min(x0, w - qs))
        bgr[y0 : y0 + qs, x0 : x0 + qs] = qr_bgr


# -----------------------------
# Stats printer
# -----------------------------
async def print_sender_bitrate(pc: RTCPeerConnection, interval_s: float = 1.5):
    prev = {}
    while True:
        try:
            for sender in pc.getSenders():
                if sender and sender.track:
                    stats = await sender.getStats()
                    for st in stats.values():
                        if isinstance(st, RTCOutboundRtpStreamStats):
                            key = sender
                            prev_entry = prev.get(key)
                            if prev_entry is not None:
                                prev_bytes, prev_ts = prev_entry
                                bytes_diff = st.bytesSent - prev_bytes
                                dt = (st.timestamp - prev_ts).total_seconds()
                                if dt > 0 and bytes_diff > 0:
                                    mbps = (8.0 * bytes_diff / dt) / 1_000_000.0
                                    logging.info("Sender %s bitrate: %.2f Mbps", sender.track.kind, mbps)
                            prev[key] = (st.bytesSent, st.timestamp)

            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return
        except Exception:
            logging.exception("Stats task error")
            await asyncio.sleep(interval_s)


# -----------------------------
# Screen capture track
# -----------------------------
class CaptureScreen(VideoStreamTrack):
    def __init__(
        self,
        width: int = 1920,
        height: int = 1080,
        monitor_number: int = 1,
        downsample: int = 1,
        enable_complexity: bool = False,
        gop_len: int = 45,
        complexity_block: int = 16,
        quality_logging: bool = False,
        qr_size: int = 160,
        qr_corner: str = "tr",
        bitrate_ctl: Optional[SharedMemoryBitrateController] = None,
    ):
        super().__init__()
        self.width = int(width)
        self.height = int(height)
        self.monitor_number = int(monitor_number)

        self.downsample = max(1, int(downsample))
        self.out_w = max(1, self.width // self.downsample)
        self.out_h = max(1, self.height // self.downsample)

        self.enable_complexity = bool(enable_complexity)
        self.gop_len = int(gop_len)

        self.quality_logging = bool(quality_logging)
        self._frame_idx = 0

        self._fps_start = time.time()
        self._fps_count = 0

        self._bitrate_ctl = bitrate_ctl

        self._analyzer = None
        self._sc_sum = 0.0
        self._gop_count = 0
        if self.enable_complexity:
            self._analyzer = ComplexityAnalyzer(block_size=complexity_block)

        self._ql = None
        if self.quality_logging:
            self._ql = QualityLogger(qr_size=qr_size, corner=qr_corner)

        logging.info(
            "Capture %dx%d, downsample=%d -> send %dx%d",
            self.width,
            self.height,
            self.downsample,
            self.out_w,
            self.out_h,
        )

    async def recv(self):
        pts, time_base = await self.next_timestamp()

        with mss.mss() as sct:
            mon = sct.monitors[self.monitor_number]
            left = mon["left"] + (mon["width"] - self.width) // 2
            top = mon["top"] + (mon["height"] - self.height) // 2
            bbox = {"top": top, "left": left, "width": self.width, "height": self.height}

            raw = np.array(sct.grab(bbox))  # BGRA
            bgr = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)

        self._frame_idx += 1

        # Downsample FIRST
        if self.downsample > 1:
            bgr = cv2.resize(bgr, (self.out_w, self.out_h), interpolation=cv2.INTER_CUBIC)

        # Optional QR overlay
        if self.quality_logging and self._ql is not None:
            payload = f"frame={self._frame_idx:05d};t={time.time():.3f};res={self.out_w}x{self.out_h}"
            self._ql.overlay(bgr, payload)

        # Optional complexity analysis (+ optional bitrate control at GOP boundary)
        if self.enable_complexity and self._analyzer is not None:
            sc = self._analyzer.evca_sc(bgr)
            self._sc_sum += sc
            self._gop_count += 1

            if self._gop_count >= self.gop_len:
                sc_avg = self._sc_sum / max(1, self._gop_count)
                msg = f"EVCA SC (avg over {self.gop_len} frames): {sc_avg:.4f}"

                if self._bitrate_ctl is not None:
                    target_bps, desired_bps, gcc_bps = self._bitrate_ctl.update_from_complexity(sc_avg)
                    gcc_str = "None" if gcc_bps is None else f"{gcc_bps/1e6:.2f}"
                    msg += f" | ladder={desired_bps/1e6:.2f}Mbps | GCC={gcc_str}Mbps -> target={target_bps/1e6:.2f}Mbps"

                logging.info(msg)
                self._sc_sum = 0.0
                self._gop_count = 0

        frame = VideoFrame.from_ndarray(bgr, format="bgr24")
        frame.pts = pts
        frame.time_base = time_base

        # FPS logging
        self._fps_count += 1
        elapsed = time.time() - self._fps_start
        if elapsed >= 1.0:
            fps = self._fps_count / elapsed
            logging.info("Server FPS: %.2f", fps)
            self._fps_start = time.time()
            self._fps_count = 0

        return frame


# -----------------------------
# Main aiortc run loop
# -----------------------------
async def run(pc, recorder, signaling, role: str, track: CaptureScreen):
    def add_tracks():
        pc.addTrack(track)

    @pc.on("track")
    def on_track(t):
        logging.info("Receiving %s", t.kind)
        recorder.addTrack(t)

    await signaling.connect()

    if role == "offer":
        add_tracks()
        await pc.setLocalDescription(await pc.createOffer())
        await signaling.send(pc.localDescription)

    while True:
        obj = await signaling.receive()

        if isinstance(obj, RTCSessionDescription):
            await pc.setRemoteDescription(obj)
            await recorder.start()

            if obj.type == "offer":
                add_tracks()
                await pc.setLocalDescription(await pc.createAnswer())
                await signaling.send(pc.localDescription)

        elif isinstance(obj, RTCIceCandidate):
            await pc.addIceCandidate(obj)

        elif obj is BYE:
            logging.info("Exiting")
            break


async def main():
    parser = argparse.ArgumentParser(description="aiortc server: screen capture + optional complexity/quality logging")
    parser.add_argument("role", choices=["offer", "answer"])
    parser.add_argument("--record-to", help="Write received media to a file.")
    parser.add_argument("--verbose", "-v", action="count", default=0)

    # Capture config
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--monitor", type=int, default=1, help="mss monitor index (1 = primary)")

    # Downsample
    parser.add_argument(
        "--downsample",
        type=int,
        default=1,
        help="Downsample factor before sending (1=no downsample, 2=half res, 3=third res, ...)",
    )

    # Complexity analyzer
    parser.add_argument("--enable-complexity", action="store_true", help="Enable EVCA complexity analysis")
    parser.add_argument("--gop-len", type=int, default=45, help="Frames per GOP window for averaging SC")
    parser.add_argument("--complexity-block", type=int, default=16, help="Block size for EVCA DCT")

    # EVCA -> bitrate ladder control (shared memory)
    parser.add_argument(
        "--evca-bitrate-control",
        action="store_true",
        help="Enable EVCA GOP-average -> bitrate ladder and write override bitrate to shared memory",
    )

    # Quality logging (QR overlay)
    parser.add_argument(
        "--quality-logging",
        action="store_true",
        help="Overlay QR code (frame id + timestamp + resolution) onto frames before sending",
    )
    parser.add_argument("--qr-size", type=int, default=160, help="QR code size in pixels")
    parser.add_argument("--qr-corner", choices=["tr", "tl", "br", "bl"], default="tr", help="QR placement corner")

    add_signaling_arguments(parser)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    signaling = create_signaling(args)
    pc = RTCPeerConnection()
    recorder = MediaRecorder(args.record_to) if args.record_to else MediaBlackhole()

    bitrate_ctl: Optional[SharedMemoryBitrateController] = None
    if args.evca_bitrate_control:
        if not args.enable_complexity:
            logging.warning("--evca-bitrate-control requires EVCA; enabling --enable-complexity automatically.")
            args.enable_complexity = True
        bitrate_ctl = SharedMemoryBitrateController()
        bitrate_ctl.open()

    track = CaptureScreen(
        width=args.width,
        height=args.height,
        monitor_number=args.monitor,
        downsample=args.downsample,
        enable_complexity=args.enable_complexity,
        gop_len=args.gop_len,
        complexity_block=args.complexity_block,
        quality_logging=args.quality_logging,
        qr_size=args.qr_size,
        qr_corner=args.qr_corner,
        bitrate_ctl=bitrate_ctl,
    )

    stats_task = asyncio.create_task(print_sender_bitrate(pc))

    try:
        await run(pc=pc, recorder=recorder, signaling=signaling, role=args.role, track=track)
    finally:
        stats_task.cancel()
        if bitrate_ctl is not None:
            bitrate_ctl.close()
        await recorder.stop()
        await signaling.close()
        await pc.close()


if __name__ == "__main__":
    asyncio.run(main())
