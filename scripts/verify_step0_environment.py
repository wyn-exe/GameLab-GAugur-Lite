"""快速验收 Step 0 环境；失败时以非零状态退出。"""

from __future__ import annotations

import importlib.metadata
import json
import platform


def fail_import(check: str, exc: BaseException, hint: str) -> None:
    """将原生依赖导入错误写成可保存的 JSON，再以非零状态退出。"""
    print(
        json.dumps(
            {
                "status": "failed",
                "check": check,
                "python": platform.python_version(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "hint": hint,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    raise SystemExit(2) from None


try:
    import torch
except (ImportError, OSError) as exc:
    fail_import(
        "torch_import",
        exc,
        "安装 requirements-torch-cu121.txt 中锁定的 PyTorch 2.4.1+cu121",
    )

try:
    import psutil
except (ImportError, OSError) as exc:
    fail_import("psutil_import", exc, "重新安装 requirements-windows.txt")

try:
    from pynvml import nvmlDeviceGetHandleByIndex, nvmlDeviceGetName, nvmlInit, nvmlShutdown
except (ImportError, OSError) as exc:
    fail_import("pynvml_import", exc, "重新安装 requirements-windows.txt 并检查 NVIDIA 驱动")


def main() -> int:
    nvmlInit()
    try:
        handle = nvmlDeviceGetHandleByIndex(0)
        nvml_name = nvmlDeviceGetName(handle)
        if isinstance(nvml_name, bytes):
            nvml_name = nvml_name.decode("utf-8")
    finally:
        nvmlShutdown()

    cuda_available = torch.cuda.is_available()
    result = {
        "python": platform.python_version(),
        "pyxel": importlib.metadata.version("pyxel"),
        "torch": torch.__version__,
        "torch_cuda_runtime": torch.version.cuda,
        "cuda_available": cuda_available,
        "torch_gpu": torch.cuda.get_device_name(0) if cuda_available else None,
        "nvml_gpu": nvml_name,
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "ram_total_gib": round(psutil.virtual_memory().total / 1024**3, 2),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    expected = (
        result["pyxel"] == "2.9.8"
        and result["torch"].startswith("2.4.1")
        and result["torch_cuda_runtime"] == "12.1"
        and cuda_available
    )
    return 0 if expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
