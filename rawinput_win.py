"""
Windows Raw Input mouse capture.

Reads true RELATIVE mouse motion (RAWMOUSE.lLastX/lLastY) via the Win32 Raw
Input API. This is delivered independently of the low-level hook chain, so the
server can simultaneously SUPPRESS the cooked mouse events with pynput
(freezing the visible cursor and stopping clicks from reaching the server) while
still receiving real motion here.

Usage:
    stop = start_raw_mouse_capture(lambda dx, dy: ...)
    ...
    stop()

The callback runs on the capture thread; keep it fast (e.g. schedule onto an
asyncio loop) and do not block.
"""

import ctypes
import threading
from ctypes import wintypes
from typing import Callable, Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# --- Constants -------------------------------------------------------------
WM_INPUT = 0x00FF
WM_QUIT = 0x0012
RIM_TYPEMOUSE = 0
RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_GENERIC_MOUSE = 0x02
MOUSE_MOVE_ABSOLUTE = 0x01
HWND_MESSAGE = wintypes.HWND(-3)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


# --- Structures ------------------------------------------------------------
class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wintypes.USHORT),
        ("usUsage", wintypes.USHORT),
        ("dwFlags", wintypes.DWORD),
        ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD),
        ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE),
        ("wParam", wintypes.WPARAM),
    ]


class _RAWMOUSE_BUTTONS(ctypes.Structure):
    _fields_ = [("usButtonFlags", wintypes.USHORT),
                ("usButtonData", wintypes.USHORT)]


class _RAWMOUSE_U(ctypes.Union):
    _fields_ = [("ulButtons", wintypes.ULONG),
                ("buttons", _RAWMOUSE_BUTTONS)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [
        ("usFlags", wintypes.USHORT),
        ("u", _RAWMOUSE_U),
        ("ulRawButtons", wintypes.ULONG),
        ("lLastX", wintypes.LONG),
        ("lLastY", wintypes.LONG),
        ("ulExtraInformation", wintypes.ULONG),
    ]


class _RAWINPUT_DATA(ctypes.Union):
    _fields_ = [("mouse", RAWMOUSE)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("data", _RAWINPUT_DATA)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


# --- Function signatures (required for 64-bit pointer correctness) ----------
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterClassW.restype = wintypes.ATOM
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.RegisterRawInputDevices.restype = wintypes.BOOL
user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE),
                                           wintypes.UINT, wintypes.UINT]
user32.GetRawInputData.restype = wintypes.UINT
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                   wintypes.LPVOID, ctypes.POINTER(wintypes.UINT),
                                   wintypes.UINT]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.DestroyWindow.argtypes = [wintypes.HWND]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]


def start_raw_mouse_capture(callback: Callable[[int, int], None]):
    """Start capturing relative mouse motion. Returns a stop() function.

    `callback(dx, dy)` is invoked on the capture thread for each motion event.
    """
    started = threading.Event()
    state = {"thread_id": 0, "hwnd": None, "wndproc": None}

    def _thread():
        # Keep a reference to the WNDPROC so it isn't garbage-collected.
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_INPUT:
                size = wintypes.UINT(0)
                user32.GetRawInputData(lparam, RID_INPUT, None,
                                       ctypes.byref(size),
                                       ctypes.sizeof(RAWINPUTHEADER))
                if size.value:
                    buf = ctypes.create_string_buffer(size.value)
                    got = user32.GetRawInputData(lparam, RID_INPUT, buf,
                                                 ctypes.byref(size),
                                                 ctypes.sizeof(RAWINPUTHEADER))
                    if got != 0xFFFFFFFF:
                        ri = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
                        if ri.header.dwType == RIM_TYPEMOUSE:
                            m = ri.data.mouse
                            # We only handle relative motion (normal mice).
                            if not (m.usFlags & MOUSE_MOVE_ABSOLUTE):
                                dx, dy = m.lLastX, m.lLastY
                                if dx or dy:
                                    try:
                                        callback(dx, dy)
                                    except Exception:
                                        pass
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        proc = WNDPROC(wndproc)
        state["wndproc"] = proc

        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASS()
        cls.lpfnWndProc = proc
        cls.hInstance = hinst
        cls.lpszClassName = "MegaMindRawInput"
        user32.RegisterClassW(ctypes.byref(cls))

        hwnd = user32.CreateWindowExW(0, "MegaMindRawInput", "megamind", 0,
                                      0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
        state["hwnd"] = hwnd
        state["thread_id"] = kernel32.GetCurrentThreadId()

        rid = RAWINPUTDEVICE()
        rid.usUsagePage = HID_USAGE_PAGE_GENERIC
        rid.usUsage = HID_USAGE_GENERIC_MOUSE
        rid.dwFlags = RIDEV_INPUTSINK     # receive input even when not focused
        rid.hwndTarget = hwnd
        user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                       ctypes.sizeof(RAWINPUTDEVICE))

        started.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if hwnd:
            user32.DestroyWindow(hwnd)

    t = threading.Thread(target=_thread, daemon=True, name="raw-mouse-capture")
    t.start()
    started.wait(timeout=5)

    def stop():
        tid = state["thread_id"]
        if tid:
            user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)

    return stop


if __name__ == "__main__":
    # Quick standalone check: prints relative deltas. Move the mouse.
    import time
    print("Raw Input test — move the mouse (Ctrl+C to stop).")
    n = {"i": 0}

    def cb(dx, dy):
        n["i"] += 1
        print(f"dx={dx:+4d} dy={dy:+4d}")

    stop = start_raw_mouse_capture(cb)
    try:
        time.sleep(15)
    except KeyboardInterrupt:
        pass
    stop()
    print("done.")
