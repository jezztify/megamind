"""
Cross-platform virtual-desktop geometry.

A "virtual desktop" is the bounding box of ALL monitors. Its origin can be
negative (e.g. a monitor positioned to the left of / above the primary). All of
MegaMind's edge detection and cursor clamping works in this space so that
multi-monitor machines behave correctly.

get_virtual_bounds() -> (min_x, min_y, width, height)
"""

import platform


def set_dpi_aware() -> None:
    """Windows: opt into per-monitor DPI awareness so the coordinates reported
    by pynput/Raw Input match GetSystemMetrics(virtual screen). Must be called
    before any window is created or metrics are queried. No-op elsewhere."""
    if platform.system() != "Windows":
        return
    import ctypes
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def get_virtual_bounds() -> tuple[int, int, int, int]:
    system = platform.system()
    if system == "Windows":
        b = _windows_bounds()
        if b:
            return b
    elif system == "Darwin":
        b = _macos_bounds()
        if b:
            return b
    return _tk_bounds()


def _windows_bounds():
    import ctypes
    u = ctypes.windll.user32
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    x = u.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = u.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = u.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = u.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        return None
    return (int(x), int(y), int(w), int(h))


def _macos_bounds():
    try:
        import Quartz
    except Exception:
        return None
    try:
        err, displays, count = Quartz.CGGetActiveDisplayList(16, None, None)
        if err or not count:
            return None
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")
        for did in displays[:count]:
            r = Quartz.CGDisplayBounds(did)
            min_x = min(min_x, r.origin.x)
            min_y = min(min_y, r.origin.y)
            max_x = max(max_x, r.origin.x + r.size.width)
            max_y = max(max_y, r.origin.y + r.size.height)
        return (int(min_x), int(min_y), int(max_x - min_x), int(max_y - min_y))
    except Exception:
        return None


def _tk_bounds():
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    w = root.winfo_screenwidth()
    h = root.winfo_screenheight()
    root.destroy()
    return (0, 0, w, h)


if __name__ == "__main__":
    set_dpi_aware()
    print("virtual bounds (min_x, min_y, width, height):", get_virtual_bounds())
