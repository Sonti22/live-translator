"""
Real-desktop check of the stealth layer (Windows only; not part of pytest). Nothing is saved: pixels are
compared in memory and only colour statistics are printed.

  py -3 tools/window_smoke.py capture   a magenta test window is grabbed with and without WDA_EXCLUDEFROMCAPTURE
  py -3 tools/window_smoke.py app       the real Api and pywebview windows, read back from the OS (no engine, no keys)
  py -3 tools/window_smoke.py           both

Exit code 0 when every check passed.
"""
import argparse
import ctypes
import os
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import winstealth  # noqa: E402

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")


def grab(box):
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=box, include_layered_windows=True).convert("RGB")


def pixels(image):
    data = image.tobytes()
    return list(zip(data[0::3], data[1::3], data[2::3]))


def share(image, test):
    """The fraction of pixels for which test(r, g, b) holds."""
    found = pixels(image)
    return sum(1 for p in found if test(*p)) / len(found)


def mean(image):
    found = pixels(image)
    return tuple(round(sum(p[i] for p in found) / len(found)) for i in range(3))


def changed(one, other, tolerance=24):
    """The fraction of pixels that differ between two grabs of the same region by more than `tolerance`."""
    pairs = list(zip(pixels(one), pixels(other)))
    return sum(1 for a, b in pairs if max(abs(x - y) for x, y in zip(a, b)) > tolerance) / len(pairs)


def magenta(r, g, b):
    return r > 200 and g < 60 and b > 200


def green(r, g, b):
    return g > r + 50 and g > b + 50


# --- mode 1: a test window of a known colour ----------------------------------------------

def make_test_window(box, ready, stop):
    """A 200x200 magenta popup at box; runs its own message loop in this thread."""
    user32, gdi32, kernel32 = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
    lresult = ctypes.c_ssize_t
    proc_type = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = lresult
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                       wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH

    class WNDCLASS(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", proc_type), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    proc = proc_type(lambda hwnd, msg, wparam, lparam: user32.DefWindowProcW(hwnd, msg, wparam, lparam))
    name = "LTStealthSmoke"
    cls = WNDCLASS(0, proc, 0, 0, kernel32.GetModuleHandleW(None), None, None,
                   gdi32.CreateSolidBrush(0xFF00FF), None, name)  # COLORREF is 0x00BBGGRR: magenta either way
    user32.RegisterClassW(ctypes.byref(cls))
    x, y, w, h = box
    hwnd = user32.CreateWindowExW(0x8 | 0x80, name, "smoke", 0x80000000 | 0x10000000, x, y, w, h,  # TOPMOST|TOOLWINDOW, POPUP|VISIBLE
                                  None, None, cls.hInstance, None)
    ready.append(hwnd)
    msg = wintypes.MSG()
    while not stop.is_set():
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)
    user32.DestroyWindow(hwnd)


def capture_test():
    print("capture: a magenta window, grabbed from the screen in memory")
    ctypes.windll.user32.SetProcessDPIAware()
    width = ctypes.windll.user32.GetSystemMetrics(0)
    box = (width - 260, 60, 200, 200)
    region = (box[0], box[1], box[0] + box[2], box[1] + box[3])
    ready, stop = [], threading.Event()
    threading.Thread(target=make_test_window, args=(box, ready, stop), daemon=True).start()
    deadline = time.monotonic() + 5
    while not ready and time.monotonic() < deadline:
        time.sleep(0.05)
    hwnd = ready[0] if ready else None
    check("test window created", bool(hwnd))
    if not hwnd:
        return
    try:
        time.sleep(0.6)
        seen = grab(region)
        before = share(seen, magenta)
        check("magenta visible to a screen grab", before > 0.9, f"magenta={before:.2f} mean={mean(seen)}")
        check("hide_from_capture accepted", winstealth.hide_from_capture(hwnd, True))
        check("OS reports the affinity", winstealth.get_affinity(hwnd) == winstealth.WDA_EXCLUDEFROMCAPTURE,
              f"affinity={winstealth.get_affinity(hwnd)}")
        time.sleep(0.6)
        seen = grab(region)
        after = share(seen, magenta)
        check("magenta NOT in the grab once excluded", after < 0.05, f"magenta={after:.2f} mean={mean(seen)}")
        winstealth.hide_from_capture(hwnd, False)
        time.sleep(0.6)
        back = share(grab(region), magenta)
        check("magenta back when the flag is cleared", back > 0.9, f"magenta={back:.2f}")
        time.sleep(1.0)
    finally:
        stop.set()


# --- mode 2: the real app windows ---------------------------------------------------------

def app_test():
    print("app: real Api + pywebview windows (temp data dir, no engine, no keys)")
    ctypes.windll.user32.SetProcessDPIAware()
    tmp = Path(tempfile.mkdtemp(prefix="lt-smoke-"))
    import live_translator as lt
    lt.APP_DIR, lt.ENV_FILE = tmp, tmp / ".env"
    lt.start_hotkey = lambda *args, **kwargs: False  # the user's own app may hold the hotkeys
    import app
    import webview
    app.SETTINGS_FILE, app.RECORDS_DIR, app.LOG_FILE, app.SAMPLE_FILE = (
        tmp / "settings.json", tmp / "records", tmp / "live_translator.log", tmp / "voice_sample")
    for env in list(app.KEY_ENVS.values()):
        os.environ.pop(env, None)
    threading.Timer(90, lambda: os._exit(2)).start()  # a hung window must not hang the run

    api = app.Api(argparse.Namespace(proxy=None))
    window = webview.create_window(app.MAIN_TITLE, url=str(app.UI_DIR / "index.html"), js_api=api,
                                   width=1240, height=780, min_size=(900, 560), background_color="#1B1B1B")
    api._window = window
    window.events.shown += api._on_shown
    result = {}

    def find(title):
        found = winstealth.find_windows(title)
        return found[0] if found else None

    def until(what, predicate, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        result.setdefault("timeout", []).append(what)
        return None

    def rect(hwnd):
        box = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(box))
        return box.left, box.top, box.right, box.bottom

    def alpha_of(hwnd):
        key, value, flags = wintypes.DWORD(), ctypes.c_ubyte(), wintypes.DWORD()
        fn = ctypes.windll.user32.GetLayeredWindowAttributes
        fn.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_ubyte),
                       ctypes.POINTER(wintypes.DWORD)]
        return value.value if fn(hwnd, ctypes.byref(key), ctypes.byref(value), ctypes.byref(flags)) else None

    def strip(hwnd):
        left, top, right, bottom = rect(hwnd)
        return grab((left + 30, top, right - 60, top + 3))  # the overlay's green top border

    def see_through(sub):
        """Mean colour of an empty part of the subtitles over a magenta backdrop: the backdrop must shine through."""
        left, top, right, bottom = rect(sub)
        ready, stop = [], threading.Event()
        threading.Thread(target=make_test_window, args=((left, top, right - left, bottom - top), ready, stop),
                         daemon=True).start()
        until("backdrop window", lambda: ready)
        winstealth._lib().SetWindowPos(sub, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)  # topmost again, above the backdrop
        winstealth.hide_from_capture(sub, False)
        time.sleep(0.6)
        seen = mean(grab((left + 20, top + 8, left + 220, top + 38)))
        winstealth.hide_from_capture(sub, True)
        stop.set()
        time.sleep(0.5)  # the backdrop is destroyed by its own thread
        return seen

    def hook():
        try:
            main = until("main window", lambda: find(app.MAIN_TITLE))
            until("main affinity", lambda: main and winstealth.get_affinity(main) == 0x11)
            result["main"] = winstealth.get_affinity(main) if main else None
            api.toggle_overlay()
            sub = until("subtitles window", lambda: find(app.OVERLAY_TITLE))
            until("subtitles affinity", lambda: sub and winstealth.get_affinity(sub) == 0x11)
            until("subtitles visible", lambda: sub and ctypes.windll.user32.IsWindowVisible(sub))
            time.sleep(0.8)
            result["overlay"] = winstealth.get_affinity(sub) if sub else None
            if sub:
                lib = winstealth._lib()
                style = winstealth._exstyle(lib, sub)
                result["style"] = style
                result["visible"] = bool(ctypes.windll.user32.IsWindowVisible(sub))
                result["alpha"] = alpha_of(sub)
                result["status"] = api.get_stealth_status()
                excluded = share(strip(sub), green)
                winstealth.hide_from_capture(sub, False)
                time.sleep(0.6)
                visible = share(strip(sub), green)
                winstealth.hide_from_capture(sub, True)
                time.sleep(0.6)
                again = share(strip(sub), green)
                result["strip"] = (excluded, visible, again)
                result["see_through"] = see_through(sub)
            if main:
                api.close_overlay()
                until("subtitles closed", lambda: not find(app.OVERLAY_TITLE))
                winstealth._lib().SetWindowPos(main, -1, 0, 0, 0, 0, 0x1 | 0x2 | 0x10)  # above whatever took the focus
                time.sleep(0.5)
                left, top, right, bottom = rect(main)
                region = (left + 200, top + 200, left + 600, top + 500)
                excluded = grab(region)
                winstealth.set_visible(main, False)  # the ground truth: what the desktop shows without the window
                time.sleep(0.4)
                gone = grab(region)
                winstealth.set_visible(main, True)
                winstealth.hide_from_capture(main, False)
                time.sleep(0.6)
                plain = grab(region)
                winstealth.hide_from_capture(main, True)
                result["main_diff"] = (changed(excluded, gone, 4), changed(plain, gone, 4))
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
        finally:
            for w in list(webview.windows):
                w.destroy()

    webview.start(hook, private_mode=True)

    if "error" in result or "timeout" in result:
        check("app run", False, str(result.get("error") or result["timeout"]))
    check("main window affinity", result.get("main") == 0x11, f"affinity={result.get('main')}")
    check("subtitles affinity", result.get("overlay") == 0x11, f"affinity={result.get('overlay')}")
    style = result.get("style")
    if style is not None:
        check("subtitles have no taskbar button / Alt+Tab entry",
              style & winstealth.WS_EX_TOOLWINDOW and not style & winstealth.WS_EX_APPWINDOW, f"exstyle=0x{style:x}")
        check("subtitles opened and visible", result.get("visible"))
        check("subtitles opacity applied", result.get("alpha") == round(app.DEFAULTS["overlay_opacity"] * 255),
              f"alpha={result.get('alpha')}")
        check("get_stealth_status agrees", result.get("status", {}).get("main") is True
              and result["status"].get("overlay") is True and result["status"].get("supported") is True,
              str(result.get("status")))
    if "strip" in result:
        excluded, visible, again = result["strip"]
        check("subtitles border invisible to a grab while excluded, visible when not",
              excluded < 0.05 and visible > 0.3 and again < 0.05,
              f"green share excluded={excluded:.2f} plain={visible:.2f} excluded again={again:.2f}")
    if "see_through" in result:
        r, g, b = result["see_through"]
        check("subtitles are see-through: a magenta backdrop shines through", r > 35 and b > 35 and g < 40,
              f"mean rgb={result['see_through']} (opaque would be about 22, 22, 22)")
    if "main_diff" in result:
        excluded, plain = result["main_diff"]
        check("an excluded main window grabs like no window at all, a plain one does not",
              plain > 0.1 and excluded < 0.02,
              f"differs from the window-less desktop: excluded={excluded:.3f} plain={plain:.3f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", nargs="?", default="all", choices=("capture", "app", "all"))
    mode = parser.parse_args().mode
    if sys.platform != "win32":
        sys.exit("Windows only")
    print(f"Windows build {winstealth.build()}, supported={winstealth.supported()}")
    if mode in ("capture", "all"):
        capture_test()
    if mode in ("app", "all"):
        app_test()
    print("RESULT:", "all checks passed" if CHECKS and all(CHECKS) else "FAILED")
    sys.stdout.flush()
    os._exit(0 if CHECKS and all(CHECKS) else 1)  # webview threads must not keep the process alive


if __name__ == "__main__":
    main()
