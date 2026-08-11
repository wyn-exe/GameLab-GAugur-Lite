import argparse
import asyncio
import logging
import os
import time
import concurrent.futures
from typing import Dict, Optional, Tuple, List

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from torchvision.transforms import functional as TF

import cudacanvas

from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer
from aiortc.contrib.signaling import BYE, add_signaling_arguments, create_signaling
from aiortc.mediastreams import MediaStreamError
from aiortc.stats import RTCTransportStats


# -----------------------------
# Utilities
# -----------------------------
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def save_image_bgr(filename: str, img_chw_float01: torch.Tensor) -> None:
    """
    Save a torch image tensor [C,H,W] float in [0,1] to disk using OpenCV (BGR).
    """
    img = img_chw_float01.detach().cpu().clamp(0, 1)
    img = (img.permute(1, 2, 0).numpy() * 255.0).astype(np.uint8)  # HWC RGB
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    cv2.imwrite(filename, img)


def upsample_chw(img_chw: torch.Tensor, scale: float, mode: str = "bicubic") -> torch.Tensor:
    """
    Upsample a [C,H,W] image by 'scale' using torch interpolation on the current device.
    """
    if scale == 1.0:
        return img_chw
    if scale <= 0:
        raise ValueError("scale must be > 0")

    img_bchw = img_chw.unsqueeze(0)  # [1,C,H,W]
    align_corners = False if mode in ("bilinear", "bicubic") else None
    out = F.interpolate(img_bchw, scale_factor=scale, mode=mode, align_corners=align_corners)
    return out[0].clamp(0, 1)


def draw_boxes_on_chw(
    img_chw: torch.Tensor,
    boxes_xyxy: torch.Tensor,
    thickness: int = 2,
) -> torch.Tensor:
    """
    Draw simple green rectangle borders on a GPU torch image [C,H,W] in-place-ish (returns a new tensor).
    boxes_xyxy: [N,4] in pixel coords (x1,y1,x2,y2).
    """
    if boxes_xyxy.numel() == 0:
        return img_chw

    assert img_chw.ndim == 3 and img_chw.shape[0] == 3, "expected RGB [3,H,W]"
    _, H, W = img_chw.shape

    out = img_chw.clone()

    # clamp + int
    b = boxes_xyxy.round().to(dtype=torch.int64)
    b[:, 0].clamp_(0, W - 1)
    b[:, 2].clamp_(0, W - 1)
    b[:, 1].clamp_(0, H - 1)
    b[:, 3].clamp_(0, H - 1)

    # green color
    green = torch.tensor([0.0, 1.0, 0.0], device=out.device, dtype=out.dtype)[:, None, None]

    for i in range(b.shape[0]):
        x1, y1, x2, y2 = b[i].tolist()
        if x2 <= x1 or y2 <= y1:
            continue

        t = max(1, int(thickness))

        # top
        out[:, y1 : min(y1 + t, H), x1 : x2] = green
        # bottom
        out[:, max(y2 - t, 0) : y2, x1 : x2] = green
        # left
        out[:, y1 : y2, x1 : min(x1 + t, W)] = green
        # right
        out[:, y1 : y2, max(x2 - t, 0) : x2] = green

    return out.clamp(0, 1)


# -----------------------------
# Object detector (optional)
# -----------------------------
class TorchObjectDetector:
    """
    Fast-ish, no-extra-deps detector using torchvision SSDLite320 MobileNetV3.
    Runs on GPU if available.
    """
    def __init__(self, device: torch.device, threshold: float = 0.35):
        self.device = device
        self.threshold = float(threshold)

        # Lazy import: only happens if --detect is used
        from torchvision.models.detection import ssdlite320_mobilenet_v3_large
        from torchvision.models.detection.ssdlite import SSDLite320_MobileNet_V3_Large_Weights

        weights = SSDLite320_MobileNet_V3_Large_Weights.DEFAULT
        self.transforms = weights.transforms()
        self.model = ssdlite320_mobilenet_v3_large(weights=weights).to(device)
        self.model.eval()

        # small warmup (helps remove first-frame spike)
        with torch.inference_mode():
            dummy = torch.zeros((3, 320, 320), device=device, dtype=torch.float32)
            _ = self.model([dummy])

    @torch.inference_mode()
    def predict(self, img_chw_01: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, float]:
        """
        img_chw_01: [3,H,W] float in [0,1] on self.device
        returns: boxes [N,4], scores [N], labels [N], inference_ms
        """
        t0 = time.perf_counter()

        # apply weights' transforms (handles resize/normalize as the model expects)
        x = self.transforms(img_chw_01)  # [3,*,*] tensor
        outputs = self.model([x])[0]

        infer_ms = (time.perf_counter() - t0) * 1000.0

        boxes = outputs.get("boxes", torch.empty((0, 4), device=self.device))
        scores = outputs.get("scores", torch.empty((0,), device=self.device))
        labels = outputs.get("labels", torch.empty((0,), device=self.device))

        if scores.numel() > 0:
            keep = scores >= self.threshold
            boxes = boxes[keep]
            scores = scores[keep]
            labels = labels[keep]

        return boxes, scores, labels, infer_ms


# -----------------------------
# Stats printer
# -----------------------------
async def print_transport_bitrate(pc: RTCPeerConnection, interval_s: float = 1.5) -> None:
    prev: Dict[str, Tuple[int, object]] = {}

    while True:
        try:
            stats = await pc.getStats()
            for stat in stats.values():
                if isinstance(stat, RTCTransportStats):
                    prev_entry = prev.get(stat.id)
                    if prev_entry is not None:
                        prev_bytes, prev_ts = prev_entry
                        bytes_diff = stat.bytesReceived - prev_bytes
                        dt = (stat.timestamp - prev_ts).total_seconds()
                        if dt > 0 and bytes_diff > 0:
                            mbps = (8.0 * bytes_diff / dt) / 1_000_000.0
                            logging.info("Transport %s bitrate: %.2f Mbps", stat.id, mbps)

                    prev[stat.id] = (stat.bytesReceived, stat.timestamp)

            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return
        except Exception:
            logging.exception("Stats task error")
            await asyncio.sleep(interval_s)


# -----------------------------
# Track consumer
# -----------------------------
class TrackConsumer:
    def __init__(
        self,
        device: torch.device,
        out_dir: Optional[str] = None,
        save_frames: bool = False,
        max_workers: int = 4,
        upsample: bool = False,
        upsample_scale: float = 1.0,
        upsample_mode: str = "bicubic",
        detect: bool = False,
        detect_threshold: float = 0.35,
        detect_every: int = 1,
        detect_max: int = 25,
    ):
        self.device = device

        # saving
        self.out_dir = out_dir
        self.save_frames = save_frames
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

        # upsample
        self.upsample = upsample
        self.upsample_scale = upsample_scale
        self.upsample_mode = upsample_mode

        # detection
        self.detect = detect
        self.detect_threshold = float(detect_threshold)
        self.detect_every = max(1, int(detect_every))
        self.detect_max = max(1, int(detect_max))
        self.detector: Optional[TorchObjectDetector] = None
        self._last_boxes: Optional[torch.Tensor] = None
        self._last_scale_for_boxes: float = 1.0

        self._tasks: List[asyncio.Task] = []

        if self.save_frames:
            if not self.out_dir:
                raise ValueError("out_dir must be set when save_frames=True")
            ensure_dir(self.out_dir)

        if self.upsample and self.upsample_scale == 1.0:
            logging.warning("Upsample enabled but scale=1.0 (no-op)")

        if self.detect:
            logging.info("Loading detector (SSDLite320 MobileNetV3) on %s ...", device)
            self.detector = TorchObjectDetector(device=device, threshold=self.detect_threshold)

    def add_track(self, track) -> None:
        self._tasks.append(asyncio.create_task(self._consume(track)))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except Exception:
                pass
        self._tasks = []
        self.executor.shutdown(wait=False)

    async def _consume(self, track) -> None:
        start_time = time.time()
        frames_in_window = 0
        frame_idx = 0

        while True:
            try:
                frame = await track.recv()
                frame_idx += 1
                frames_in_window += 1

                # aiortc frame -> numpy RGB
                rgb = frame.to_ndarray(format="rgb24")

                # numpy RGB -> torch [C,H,W] float [0,1]
                img_chw = TF.to_tensor(rgb).to(self.device, dtype=torch.float32).clamp(0, 1)

                # keep original for detection (faster)
                img_for_det = img_chw

                # optional upsample (for display/save)
                scale = 1.0
                if self.upsample:
                    scale = float(self.upsample_scale)
                    img_chw = upsample_chw(img_chw, scale, self.upsample_mode)

                # optional detection (run every N frames)
                if self.detect and self.detector is not None:
                    if (frame_idx % self.detect_every) == 0:
                        boxes, scores, labels, infer_ms = self.detector.predict(img_for_det)

                        # keep top-K by score
                        if scores.numel() > 0:
                            k = min(self.detect_max, scores.numel())
                            topk = torch.topk(scores, k=k, largest=True).indices
                            boxes = boxes[topk]

                        # scale boxes if we upsample for display
                        if scale != 1.0 and boxes.numel() > 0:
                            boxes = boxes * scale

                        self._last_boxes = boxes
                        self._last_scale_for_boxes = scale

                        logging.info(
                            "Det: %d boxes (thr=%.2f) | infer=%.2f ms | every=%d",
                            int(boxes.shape[0]),
                            self.detect_threshold,
                            infer_ms,
                            self.detect_every,
                        )

                    # draw last boxes (even on skipped frames)
                    if self._last_boxes is not None and self._last_boxes.numel() > 0:
                        img_chw = draw_boxes_on_chw(img_chw, self._last_boxes, thickness=2)

                # show on GPU
                cudacanvas.im_show(img_chw)

                # optional saving (post-processing image)
                if self.save_frames and self.out_dir:
                    filename = os.path.join(self.out_dir, f"{frame_idx:05d}.png")
                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(self.executor, save_image_bgr, filename, img_chw)

                # FPS print (once per ~1s)
                elapsed = time.time() - start_time
                if elapsed >= 1.0:
                    fps = frames_in_window / elapsed
                    logging.info("Client FPS: %.2f", fps)
                    start_time = time.time()
                    frames_in_window = 0

            except MediaStreamError:
                logging.info("MediaStream ended")
                return
            except asyncio.CancelledError:
                return
            except Exception:
                logging.exception("Track consumer error")
                return


# -----------------------------
# Main run logic
# -----------------------------
async def run(
    pc: RTCPeerConnection,
    signaling,
    role: str,
    player: Optional[MediaPlayer],
    consumer: TrackConsumer,
) -> None:
    def add_tracks() -> None:
        if player and player.audio:
            pc.addTrack(player.audio)
        if player and player.video:
            pc.addTrack(player.video)

    @pc.on("track")
    def on_track(track) -> None:
        logging.info("Receiving track: %s", track.kind)
        if track.kind == "video":
            consumer.add_track(track)

    await signaling.connect()

    if role == "offer":
        add_tracks()
        await pc.setLocalDescription(await pc.createOffer())
        await signaling.send(pc.localDescription)

    while True:
        obj = await signaling.receive()

        if isinstance(obj, RTCSessionDescription):
            await pc.setRemoteDescription(obj)

            if obj.type == "offer":
                add_tracks()
                await pc.setLocalDescription(await pc.createAnswer())
                await signaling.send(pc.localDescription)

        elif isinstance(obj, RTCIceCandidate):
            await pc.addIceCandidate(obj)

        elif obj is BYE:
            logging.info("Signaling BYE received, exiting")
            break


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="aiortc client: GPU display + optional frame dump + optional upsample + optional GPU object detection"
    )
    parser.add_argument("role", choices=["offer", "answer"])
    parser.add_argument("--play-from", dest="play_from", help="Read media from a file and send it (offer side).")

    # Saving
    parser.add_argument("--save-frames", action="store_true", help="Save received video frames as PNGs.")
    parser.add_argument("--out-dir", default="screendecode", help="Output directory for saved frames.")

    # Upsample
    parser.add_argument("--upsample", action="store_true", help="Upsample frames before display/save.")
    parser.add_argument("--upsample-scale", type=float, default=1.0, help="Upsample factor (e.g., 2.0, 1.5).")
    parser.add_argument(
        "--upsample-mode",
        choices=["nearest", "bilinear", "bicubic"],
        default="bicubic",
        help="Interpolation mode for upsampling.",
    )

    # Detection
    parser.add_argument("--detect", action="store_true", help="Run GPU object detection (TorchVision SSDLite320).")
    parser.add_argument("--detect-threshold", type=float, default=0.35, help="Detection confidence threshold.")
    parser.add_argument("--detect-every", type=int, default=2, help="Run detection every N frames (perf knob).")
    parser.add_argument("--detect-max", type=int, default=25, help="Max boxes to draw.")

    parser.add_argument("--verbose", "-v", action="count", default=0)
    add_signaling_arguments(parser)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.upsample and args.upsample_scale <= 0:
        raise ValueError("--upsample-scale must be > 0")
    if args.detect_threshold < 0 or args.detect_threshold > 1:
        raise ValueError("--detect-threshold must be in [0,1]")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logging.info("Using device: %s", device)
    logging.info("Upsample: %s (scale=%.3f, mode=%s)", args.upsample, args.upsample_scale, args.upsample_mode)
    logging.info("Detect: %s (thr=%.2f, every=%d, max=%d)", args.detect, args.detect_threshold, args.detect_every, args.detect_max)

    signaling = create_signaling(args)
    pc = RTCPeerConnection()

    player = MediaPlayer(args.play_from) if args.play_from else None
    consumer = TrackConsumer(
        device=device,
        out_dir=args.out_dir,
        save_frames=args.save_frames,
        max_workers=4,
        upsample=args.upsample,
        upsample_scale=args.upsample_scale,
        upsample_mode=args.upsample_mode,
        detect=args.detect,
        detect_threshold=args.detect_threshold,
        detect_every=args.detect_every,
        detect_max=args.detect_max,
    )

    stats_task = asyncio.create_task(print_transport_bitrate(pc))

    try:
        await run(pc=pc, signaling=signaling, role=args.role, player=player, consumer=consumer)
    finally:
        stats_task.cancel()
        await consumer.stop()
        await signaling.close()
        await pc.close()


if __name__ == "__main__":
    asyncio.run(main())
