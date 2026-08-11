# GAMELAB - AI-ENABLED CLOUD GAMING TESTBED

## Setup (Conda)

> **CUDA note:** `requirements.txt` currently pulls **PyTorch CUDA 12.1** wheels (`cu121`).  
> If your machine uses a different CUDA version, update the `--extra-index-url` in `requirements.txt` accordingly.

```bash
conda create -n ai-testbed python=3.10 -y
conda activate ai-testbed

# Fix for a common aiortc/OpenSSL crash:
# "ffi_prep_closure(): bad user_data" (libffi/cffi mismatch)
conda install -y -c conda-forge libffi cffi cryptography pyopenssl

pip install -r requirements.txt
```

> **Note:** This project uses a **custom fork of aiortc** installed via `requirements.txt`.
---

## Overview

This repository provides a **Python-first, AI-enabled cloud gaming (CG) testbed** that exposes the end-to-end CG pipeline and makes it easy to plug in **custom AI/ML modules** (e.g., learned rate control, super-resolution, object detection, QoE models, analytics).

### High-level flow

1. The **client** captures user inputs (keyboard/mouse) and forwards them to the server over a lightweight TCP channel.
2. The **server** runs the game, renders frames, and captures them via a screen grabber.
3. Frames pass through a **Python processing pipeline**, where users can:
   - inspect / modify frames **before encoding**
   - implement custom **AI/ML logic** (rate control, content adaptation, QoE estimation, etc.)
4. The encoder compresses frames (H.264 / VP8), and WebRTC (via aiortc) delivers them to the client.
5. The **client** decodes and displays frames. The display path supports **tensor-first workflows** so PyTorch tensors can be visualized directly on the GPU using **cudacanvas** without CPU copies.

![System overview](AI_TestBed_Architecture.png?raw=true)

---

## References

- **aiortc (WebRTC in Python):** https://github.com/aiortc/aiortc  
- **MSS (screen capture):** https://github.com/BoboTiG/python-mss  
- **EVCA (frame complexity analysis):** https://github.com/cd-athena/EVCA  
- **cudacanvas (GPU tensor display):** https://github.com/OutofAi/cudacanvas
