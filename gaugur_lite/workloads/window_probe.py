"""Windows Pyxel 窗口的只读可见性、尺寸与 DPI 快照。"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any


class RECT(ctypes.Structure):
    _fields_ = (
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    )


def _empty(title: str, *, supported: bool) -> dict[str, Any]:
    return {
        "supported": supported,
        "title": title,
        "found": False,
        "hwnd": None,
        "process_pid": None,
        "visible": None,
        "minimized": None,
        "foreground": None,
        "client_width": None,
        "client_height": None,
        "window_left": None,
        "window_top": None,
        "window_width": None,
        "window_height": None,
        "dpi": None,
        "monitor_handle": None,
    }


def capture_window(title: str) -> dict[str, Any]:
    if os.name != "nt":
        return _empty(title, supported=False)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
    user32.MonitorFromWindow.restype = wintypes.HANDLE
    hwnd = int(user32.FindWindowW(None, title) or 0)
    if not hwnd:
        return _empty(title, supported=True)

    client = RECT()
    outer = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(client))
    user32.GetWindowRect(hwnd, ctypes.byref(outer))
    try:
        dpi = int(user32.GetDpiForWindow(hwnd))
    except AttributeError:
        dpi = None
    process_pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_pid))
    monitor = int(user32.MonitorFromWindow(hwnd, 2) or 0)
    return {
        "supported": True,
        "title": title,
        "found": True,
        "hwnd": hwnd,
        "process_pid": int(process_pid.value) or None,
        "visible": bool(user32.IsWindowVisible(hwnd)),
        "minimized": bool(user32.IsIconic(hwnd)),
        "foreground": int(user32.GetForegroundWindow() or 0) == hwnd,
        "client_width": int(client.right - client.left),
        "client_height": int(client.bottom - client.top),
        "window_left": int(outer.left),
        "window_top": int(outer.top),
        "window_width": int(outer.right - outer.left),
        "window_height": int(outer.bottom - outer.top),
        "dpi": dpi,
        "monitor_handle": monitor or None,
    }
