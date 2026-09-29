"""
Windows-only window tricks over user32 (ctypes): keep a window out of screen capture, out of the taskbar and
Alt+Tab, see-through and click-through. Import-safe on other systems, where everything simply fails softly.

Every function logs and returns False (None / [] for the readers) instead of raising, so the app degrades
silently where the OS cannot do it. The user32 handle is a private WinDLL with argtypes/restype declared
(a missing restype truncates a 64-bit handle); `_load` is the seam the tests replace with a fake.
"""
import ctypes
import functools
import logging
import os
import sys
from ctypes import wintypes

log = logging.getLogger("winstealth")

WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11  # never WDA_MONITOR (0x1): it paints a black box that the viewers would see
MIN_BUILD = 19041  # Windows 10 2004: the first build that knows WDA_EXCLUDEFROMCAPTURE
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_APPWINDOW = 0x20, 0x80, 0x80000, 0x40000
WS_EX_NOACTIVATE = 0x8000000
LWA_ALPHA = 0x2
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
SWP_SHOWWINDOW, SWP_HIDEWINDOW = 0x40, 0x80
SWP_KEEP = SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE
OPACITY_MIN, OPACITY_MAX = 0.3, 1.0

_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM) if sys.platform == "win32" else None
_HWND, _UINT, _INT = wintypes.HWND, wintypes.UINT, ctypes.c_int
_SIGNATURES = {  # name: (argtypes, restype)
    "SetWindowDisplayAffinity": ([_HWND, wintypes.DWORD], wintypes.BOOL),
    "GetWindowDisplayAffinity": ([_HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
    "EnumWindows": ([_ENUM_PROC, wintypes.LPARAM], wintypes.BOOL),
    "GetWindowThreadProcessId": ([_HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
    "GetWindowTextW": ([_HWND, wintypes.LPWSTR, _INT], _INT),
    "GetWindowLongPtrW": ([_HWND, _INT], ctypes.c_ssize_t),
    "SetWindowLongPtrW": ([_HWND, _INT, ctypes.c_ssize_t], ctypes.c_ssize_t),
    "SetLayeredWindowAttributes": ([_HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD], wintypes.BOOL),
    "SetWindowPos": ([_HWND, _HWND, _INT, _INT, _INT, _INT, _UINT], wintypes.BOOL),
    "ShowWindow": ([_HWND, _INT], wintypes.BOOL),
    "IsWindowVisible": ([_HWND], wintypes.BOOL),
    "IsWindow": ([_HWND], wintypes.BOOL),
}
_lib_cache = None


def _load():
    if sys.platform != "win32":
        raise OSError("user32 exists on Windows only")
    return ctypes.WinDLL("user32", use_last_error=True)  # private: our argtypes never leak into other code


def _declare(lib):
    for name, (argtypes, restype) in _SIGNATURES.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = argtypes, restype


def _lib():
    global _lib_cache
    if _lib_cache is None:
        lib = _load()
        _declare(lib)
        _lib_cache = lib
    return _lib_cache


def _clear_error():
    getattr(ctypes, "set_last_error", int)(0)


def _last_error():
    return getattr(ctypes, "get_last_error", int)()


def _safe(default):
    def decorate(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception as e:
                log.warning("%s failed: %s: %s", function.__name__, type(e).__name__, e)
                return default
        return wrapper
    return decorate


def _check(ok, what):
    if not ok:
        raise OSError(f"{what} refused (error {_last_error()})")


def _exstyle(lib, hwnd):
    _clear_error()
    style = lib.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if style == 0 and _last_error():
        raise OSError(f"GetWindowLongPtrW failed (error {_last_error()})")
    return style & 0xFFFFFFFF


def _set_exstyle(lib, hwnd, style):
    _clear_error()
    lib.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
    if _last_error():
        raise OSError(f"SetWindowLongPtrW failed (error {_last_error()})")


def _reposition(lib, hwnd, flag):
    _check(lib.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_KEEP | flag), "SetWindowPos")


def build():
    """The Windows build number; 0 when unknown."""
    try:
        return sys.getwindowsversion().build
    except Exception:
        return 0


def supported():
    """Whether this Windows can exclude a window from capture (build 19041+) and user32 is reachable."""
    if build() < MIN_BUILD:
        return False
    try:
        _lib()
    except Exception:
        return False
    return True


@_safe(False)
def hide_from_capture(hwnd, hidden):
    """Exclude the window from screen sharing and recordings (or bring it back). False if the OS refuses."""
    if build() < MIN_BUILD:
        log.warning("hide_from_capture: Windows build %s is older than %s, the window stays capturable",
                    build(), MIN_BUILD)
        return False
    _check(_lib().SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if hidden else WDA_NONE),
           "SetWindowDisplayAffinity")
    return True


@_safe(None)
def get_affinity(hwnd):
    """The window's display affinity as the OS reports it (0x11 = excluded from capture); None if unreadable."""
    value = wintypes.DWORD()
    _check(_lib().GetWindowDisplayAffinity(hwnd, ctypes.pointer(value)), "GetWindowDisplayAffinity")
    return value.value


@_safe([])
def find_windows(title, pid=None):
    """Top-level windows of the process `pid` (default: this one) titled exactly `title`.

    Not FindWindowW: WebView2 has child windows, titles repeat and another program may share one."""
    lib = _lib()
    pid = os.getpid() if pid is None else pid
    found = []
    text = ctypes.create_unicode_buffer(len(title) + 2)
    owner = wintypes.DWORD()

    def visit(hwnd, _):
        lib.GetWindowThreadProcessId(hwnd, ctypes.pointer(owner))
        if owner.value == pid and lib.GetWindowTextW(hwnd, text, len(text)) and text.value == title:
            found.append(hwnd)
        return True

    callback = _ENUM_PROC(visit) if _ENUM_PROC else visit
    _check(lib.EnumWindows(callback, 0), "EnumWindows")
    return found


@_safe(False)
def set_tool_window(hwnd, on):
    """No taskbar button and no Alt+Tab entry (on) or back to normal (off)."""
    lib = _lib()
    style = _exstyle(lib, hwnd)
    wanted = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW if on else style & ~WS_EX_TOOLWINDOW
    if wanted == style:
        return True
    shown = bool(lib.IsWindowVisible(hwnd))
    if shown:  # the shell only refreshes its lists when the window is shown again
        _reposition(lib, hwnd, SWP_HIDEWINDOW)
    try:
        _set_exstyle(lib, hwnd, wanted)
    finally:
        if shown:
            _reposition(lib, hwnd, SWP_SHOWWINDOW)
    return True


@_safe(False)
def set_opacity(hwnd, fraction):
    """Whole-window transparency, clamped to 0.3..1.0 (below that the text is unreadable)."""
    fraction = min(OPACITY_MAX, max(OPACITY_MIN, float(fraction)))
    lib = _lib()
    style = _exstyle(lib, hwnd)
    if not style & WS_EX_LAYERED:
        _set_exstyle(lib, hwnd, style | WS_EX_LAYERED)
    _check(lib.SetLayeredWindowAttributes(hwnd, 0, round(fraction * 255), LWA_ALPHA), "SetLayeredWindowAttributes")
    return True


@_safe(False)
def set_click_through(hwnd, on):
    """The mouse passes through the window to whatever is under it."""
    lib = _lib()
    style = _exstyle(lib, hwnd)
    if on:
        _set_exstyle(lib, hwnd, style | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        if not style & WS_EX_LAYERED:  # a layered window stays invisible until its attributes are set
            _check(lib.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA), "SetLayeredWindowAttributes")
    elif style & WS_EX_TRANSPARENT:
        _set_exstyle(lib, hwnd, style & ~WS_EX_TRANSPARENT)  # stays layered: the opacity is kept
    return True


@_safe(False)
def set_visible(hwnd, on):
    """Show without taking focus from the call app, or hide."""
    lib = _lib()
    _check(lib.IsWindow(hwnd), "IsWindow")
    lib.ShowWindow(hwnd, SW_SHOWNOACTIVATE if on else SW_HIDE)  # its result is the previous state, not an error
    return True
