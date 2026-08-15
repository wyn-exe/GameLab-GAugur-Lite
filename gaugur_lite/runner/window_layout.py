"""只操作本次 run 的已知 Pyxel 窗口，完成 1–4 窗口 grid_2x2 布局。"""

from __future__ import annotations

import ctypes
import itertools
import math
import os
import time
from collections.abc import Mapping
from ctypes import wintypes
from typing import Any

from ..workloads.window_probe import RECT, capture_window


class MONITORINFO(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    )


def grid_rectangles(
    *, left: int, top: int, right: int, bottom: int, count: int, padding: int = 8
) -> tuple[tuple[int, int, int, int], ...]:
    """返回互不相交的外窗矩形；纯函数便于无 GUI 单元测试。"""

    if not 1 <= count <= 4:
        raise ValueError("grid_2x2 只支持 1–4 个窗口")
    if right <= left or bottom <= top or padding < 0:
        raise ValueError("显示器工作区或 padding 非法")
    columns = 1 if count == 1 else 2
    rows = math.ceil(count / columns)
    cell_width = (right - left) // columns
    cell_height = (bottom - top) // rows
    rectangles = []
    for index in range(count):
        row, column = divmod(index, columns)
        x = left + column * cell_width + padding
        y = top + row * cell_height + padding
        width = cell_width - 2 * padding
        height = cell_height - 2 * padding
        if width <= 0 or height <= 0:
            raise ValueError("显示器工作区不足以放置窗口")
        rectangles.append((x, y, width, height))
    return tuple(rectangles)


def rectangles_overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    if any(first.get(key) is None or second.get(key) is None for key in (
        "window_left", "window_top", "window_width", "window_height"
    )):
        return True
    a_left = int(first["window_left"])
    a_top = int(first["window_top"])
    a_right = a_left + int(first["window_width"])
    a_bottom = a_top + int(first["window_height"])
    b_left = int(second["window_left"])
    b_top = int(second["window_top"])
    b_right = b_left + int(second["window_width"])
    b_bottom = b_top + int(second["window_height"])
    return a_left < b_right and b_left < a_right and a_top < b_bottom and b_top < a_bottom


def _enumerate_monitors() -> list[dict[str, int]]:
    if os.name != "nt":
        return []
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    monitor_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )
    user32.EnumDisplayMonitors.argtypes = (
        wintypes.HDC,
        ctypes.POINTER(RECT),
        monitor_proc,
        wintypes.LPARAM,
    )
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    user32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MONITORINFO))
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    monitors: list[dict[str, int]] = []

    @monitor_proc
    def callback(handle: int, _hdc: int, _rect: Any, _data: int) -> bool:
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return True
        monitors.append(
            {
                "handle": int(handle),
                "left": int(info.rcWork.left),
                "top": int(info.rcWork.top),
                "right": int(info.rcWork.right),
                "bottom": int(info.rcWork.bottom),
                "primary": int(bool(info.dwFlags & 1)),
            }
        )
        return True

    if not user32.EnumDisplayMonitors(0, None, callback, 0):
        raise OSError(ctypes.get_last_error(), "EnumDisplayMonitors 失败")
    # display_index=0 稳定对应主显示器，其余显示器再按坐标排序。
    return sorted(monitors, key=lambda item: (-item["primary"], item["left"], item["top"]))


def wait_for_windows(
    *,
    titles: tuple[str, ...],
    expected_pids: Mapping[str, int],
    timeout_s: float,
) -> list[dict[str, Any]]:
    """轮询窗口句柄，并把标题命中的窗口绑定到本次受管子进程 PID。"""

    if timeout_s <= 0:
        raise ValueError("窗口等待 timeout_s 必须大于 0")
    deadline = time.perf_counter() + timeout_s
    observations: list[dict[str, Any]] = []
    while True:
        observations = [capture_window(title) for title in titles]
        ready = all(
            item.get("found") is True
            and int(item.get("process_pid") or 0) == int(expected_pids[title])
            for title, item in zip(titles, observations, strict=True)
        )
        if ready:
            return observations
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            details = [
                {
                    "title": title,
                    "found": item.get("found"),
                    "actual_pid": item.get("process_pid"),
                    "expected_pid": expected_pids[title],
                }
                for title, item in zip(titles, observations, strict=True)
            ]
            raise RuntimeError(f"窗口 ready 复核超时: {details}")
        time.sleep(min(0.05, remaining))


def arrange_windows_grid(
    *,
    titles: tuple[str, ...],
    expected_pids: Mapping[str, int],
    display_index: int,
    layout: str = "grid_2x2",
    timeout_s: float = 1.5,
) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("窗口排列仅支持 Windows")
    if layout != "grid_2x2":
        raise ValueError(f"不支持的窗口布局: {layout}")
    if len(set(titles)) != len(titles):
        raise ValueError("同一次 run 的窗口标题必须唯一")
    if set(expected_pids) != set(titles):
        raise ValueError("expected_pids 必须与窗口标题一一对应")
    monitors = _enumerate_monitors()
    if not 0 <= display_index < len(monitors):
        raise ValueError(f"display_index={display_index} 超出显示器数量 {len(monitors)}")
    monitor = monitors[display_index]
    rectangles = grid_rectangles(
        left=monitor["left"],
        top=monitor["top"],
        right=monitor["right"],
        bottom=monitor["bottom"],
        count=len(titles),
    )
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowPos.argtypes = (
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    )
    user32.SetWindowPos.restype = wintypes.BOOL
    # 跨进程异步排布，避免 Pyxel 子进程暂未泵送窗口消息时同步阻塞。
    swp_flags = 0x0004 | 0x0010 | 0x0040 | 0x4000
    initial = wait_for_windows(
        titles=titles,
        expected_pids=expected_pids,
        timeout_s=timeout_s,
    )
    moved = []
    for title, snapshot, (left, top, width, height) in zip(
        titles, initial, rectangles, strict=True
    ):
        hwnd = int(snapshot.get("hwnd") or 0)
        if not user32.SetWindowPos(hwnd, 0, left, top, width, height, swp_flags):
            raise OSError(ctypes.get_last_error(), f"SetWindowPos 失败: {title}")
        moved.append({"title": title, "hwnd": hwnd, "requested_rect": [left, top, width, height]})

    deadline = time.perf_counter() + timeout_s
    observations: list[dict[str, Any]] = []
    pairwise_overlap: list[list[str]] = []
    healthy = False
    while True:
        observations = [capture_window(title) for title in titles]
        pairwise_overlap = []
        for first_index, second_index in itertools.combinations(range(len(observations)), 2):
            if rectangles_overlap(observations[first_index], observations[second_index]):
                pairwise_overlap.append([titles[first_index], titles[second_index]])
        healthy = all(
            item.get("found") is True
            and int(item.get("process_pid") or 0) == int(expected_pids[title])
            and item.get("visible") is True
            and item.get("minimized") is False
            and item.get("monitor_handle") == monitor["handle"]
            and int(item.get("client_width") or 0) > 0
            and int(item.get("client_height") or 0) > 0
            for title, item in zip(titles, observations, strict=True)
        ) and not pairwise_overlap
        if healthy or time.perf_counter() >= deadline:
            break
        time.sleep(0.05)
    return {
        "schema_version": 1,
        "status": "passed" if healthy else "failed",
        "layout": layout,
        "display_index": display_index,
        "monitor": monitor,
        "moved": moved,
        "observations": observations,
        "pairwise_overlap": pairwise_overlap,
        "external_occlusion_checked": False,
    }
