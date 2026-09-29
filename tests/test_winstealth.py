"""winstealth against a fake user32: the exact flags and calls, never a raise, never WDA_MONITOR."""
import ctypes
import os
import sys

import pytest

import winstealth as ws

PID = os.getpid()


class Fn:
    """A user32 function: callable, with the argtypes/restype slots ctypes gives every foreign function."""

    def __init__(self, impl):
        self.impl, self.argtypes, self.restype = impl, None, None

    def __call__(self, *args):
        return self.impl(*args)


class Win:
    def __init__(self, title="", pid=PID, style=0, visible=True):
        self.title, self.pid, self.style, self.visible = title, pid, style, visible
        self.affinity, self.alpha = 0, None


class FakeUser32:
    """Enough of user32 to see what winstealth asks for. `refuse` names the functions that fail (error 5)."""

    def __init__(self):
        self.windows, self.calls, self.refuse, self.error = {}, [], set(), 0
        for name in ws._SIGNATURES:
            setattr(self, name, Fn(getattr(self, "_" + name)))

    def add(self, hwnd, **kwargs):
        self.windows[hwnd] = Win(**kwargs)
        return self.windows[hwnd]

    def names(self):
        return [call[0] for call in self.calls]

    def _refused(self, name):
        if name in self.refuse:
            self.error = 5
        return name in self.refuse

    def _known(self, hwnd):
        if hwnd not in self.windows:
            self.error = 1400  # invalid window handle
        return self.windows.get(hwnd)

    def _SetWindowDisplayAffinity(self, hwnd, flag):
        self.calls.append(("SetWindowDisplayAffinity", hwnd, flag))
        win = self._known(hwnd)
        if self._refused("SetWindowDisplayAffinity") or not win:
            return 0
        win.affinity = flag
        return 1

    def _GetWindowDisplayAffinity(self, hwnd, out):
        win = self._known(hwnd)
        if self._refused("GetWindowDisplayAffinity") or not win:
            return 0
        out.contents.value = win.affinity
        return 1

    def _EnumWindows(self, callback, lparam):
        if self._refused("EnumWindows"):
            return 0
        for hwnd in list(self.windows):
            if not callback(hwnd, lparam):
                break
        return 1

    def _GetWindowThreadProcessId(self, hwnd, out):
        out.contents.value = self.windows[hwnd].pid
        return 7

    def _GetWindowTextW(self, hwnd, buffer, size):
        buffer.value = self.windows[hwnd].title[:size - 1]
        return len(buffer.value)

    def _GetWindowLongPtrW(self, hwnd, index):
        assert index == ws.GWL_EXSTYLE
        win = self._known(hwnd)
        return 0 if self._refused("GetWindowLongPtrW") or not win else win.style

    def _SetWindowLongPtrW(self, hwnd, index, value):
        assert index == ws.GWL_EXSTYLE
        self.calls.append(("SetWindowLongPtrW", hwnd, value))
        win = self._known(hwnd)
        if self._refused("SetWindowLongPtrW") or not win:
            return 0
        win.style = value
        return 1

    def _SetLayeredWindowAttributes(self, hwnd, key, alpha, flags):
        self.calls.append(("SetLayeredWindowAttributes", hwnd, key, alpha, flags))
        win = self._known(hwnd)
        if self._refused("SetLayeredWindowAttributes") or not win:
            return 0
        if not win.style & ws.WS_EX_LAYERED:  # the real one fails too, and the window stays invisible
            self.error = 87
            return 0
        win.alpha = alpha
        return 1

    def _SetWindowPos(self, hwnd, after, x, y, width, height, flags):
        self.calls.append(("SetWindowPos", hwnd, flags))
        win = self._known(hwnd)
        if self._refused("SetWindowPos") or not win:
            return 0
        if flags & ws.SWP_HIDEWINDOW:
            win.visible = False
        if flags & ws.SWP_SHOWWINDOW:
            win.visible = True
        return 1

    def _ShowWindow(self, hwnd, command):
        self.calls.append(("ShowWindow", hwnd, command))
        win = self.windows[hwnd]
        was, win.visible = win.visible, command != ws.SW_HIDE
        return was

    def _IsWindowVisible(self, hwnd):
        return int(self.windows[hwnd].visible)

    def _IsWindow(self, hwnd):
        return int(hwnd in self.windows)


def install_fake_user32(monkeypatch):
    """winstealth talks to a fresh FakeUser32 on a supported Windows build."""
    fake = FakeUser32()
    monkeypatch.setattr(ws, "_load", lambda: fake)
    monkeypatch.setattr(ws, "_lib_cache", None)
    monkeypatch.setattr(ws, "build", lambda: 19045)
    monkeypatch.setattr(ws, "_last_error", lambda: fake.error)
    monkeypatch.setattr(ws, "_clear_error", lambda: setattr(fake, "error", 0))
    return fake


@pytest.fixture
def user32(monkeypatch):
    return install_fake_user32(monkeypatch)


# --- the Win32 values, spelled out: every other test compares against these names, so a wrong
# --- value (e.g. SW_SHOW = 5 for SW_SHOWNOACTIVATE = 4, which steals the focus in a call) needs one place to be caught

def test_the_win32_constants_have_the_documented_values():
    assert ws.SW_HIDE == 0
    assert ws.SW_SHOWNOACTIVATE == 4  # 5 is SW_SHOW: it activates the window and takes the keyboard from the call app
    assert ws.WDA_NONE == 0
    assert ws.WDA_EXCLUDEFROMCAPTURE == 0x11  # 1 is WDA_MONITOR: a black box that the viewers would see
    assert ws.MIN_BUILD == 19041
    assert ws.WS_EX_TOOLWINDOW == 0x80
    assert ws.WS_EX_LAYERED == 0x80000
    assert ws.WS_EX_TRANSPARENT == 0x20
    assert ws.WS_EX_APPWINDOW == 0x40000
    assert ws.WS_EX_NOACTIVATE == 0x8000000
    assert ws.GWL_EXSTYLE == -20
    assert ws.LWA_ALPHA == 0x2
    assert (ws.SWP_NOSIZE, ws.SWP_NOMOVE, ws.SWP_NOZORDER, ws.SWP_NOACTIVATE) == (0x1, 0x2, 0x4, 0x10)
    assert (ws.SWP_SHOWWINDOW, ws.SWP_HIDEWINDOW) == (0x40, 0x80)


# --- capture exclusion -------------------------------------------------------------------

def test_hiding_sets_the_exclude_from_capture_affinity(user32):
    user32.add(1)
    assert ws.hide_from_capture(1, True) is True
    assert user32.windows[1].affinity == 0x11
    assert ws.hide_from_capture(1, False) is True
    assert user32.windows[1].affinity == 0


def test_a_refusal_is_false_and_never_falls_back_to_wda_monitor(user32):
    user32.add(1)
    user32.refuse.add("SetWindowDisplayAffinity")
    assert ws.hide_from_capture(1, True) is False
    assert [call[2] for call in user32.calls] == [0x11]  # WDA_MONITOR (1) would paint a black box for the viewers


def test_an_unknown_window_is_false(user32):
    assert ws.hide_from_capture(404, True) is False


def test_an_old_windows_build_is_left_alone(user32, monkeypatch, caplog):
    user32.add(1)
    monkeypatch.setattr(ws, "build", lambda: 18363)
    assert ws.hide_from_capture(1, True) is False
    assert user32.calls == []
    assert "18363" in caplog.text


def test_affinity_is_read_back_and_none_when_unreadable(user32):
    user32.add(1).affinity = 0x11
    assert ws.get_affinity(1) == 0x11
    assert ws.get_affinity(404) is None
    user32.refuse.add("GetWindowDisplayAffinity")
    assert ws.get_affinity(1) is None


def test_every_user32_function_gets_argtypes_and_restype(user32):
    ws.hide_from_capture(1, True)
    for name in ws._SIGNATURES:
        function = getattr(user32, name)
        assert function.argtypes and function.restype is not None, name
    assert user32.GetWindowLongPtrW.restype is ctypes.c_ssize_t  # a 32-bit result would cut a 64-bit style
    assert user32.SetWindowLongPtrW.argtypes[2] is ctypes.c_ssize_t
    assert all(user32.SetWindowPos.argtypes[i] is ws.wintypes.HWND for i in (0, 1))


@pytest.mark.skipif(sys.platform != "win32", reason="user32")
def test_the_real_user32_is_private_and_declared():
    lib = ws._load()
    assert lib is not ctypes.windll.user32  # other code keeps its own, undeclared handle
    ws._declare(lib)
    assert lib.GetWindowLongPtrW.restype is ctypes.c_ssize_t


# --- finding windows ---------------------------------------------------------------------

def test_find_windows_needs_the_exact_title_and_this_process(user32):
    user32.add(1, title="Live Translator")
    user32.add(2, title="Live Translator", pid=PID + 1)  # another program with the same title
    user32.add(3, title="Live Translator - notes")  # not exact
    user32.add(4, title="live translator")
    user32.add(5, title="Live Translator")
    assert ws.find_windows("Live Translator") == [1, 5]
    assert ws.find_windows("Live Translator", pid=PID + 1) == [2]
    assert ws.find_windows("Missing") == []


def test_find_windows_reads_long_titles(user32):
    title = "Субтитры — Live Translator"
    user32.add(1, title=title)
    assert ws.find_windows(title) == [1]


def test_find_windows_fails_soft(user32):
    user32.add(1, title="Live Translator")
    user32.refuse.add("EnumWindows")
    assert ws.find_windows("Live Translator") == []


# --- taskbar and Alt+Tab -----------------------------------------------------------------

def test_tool_window_swaps_the_taskbar_style_and_refreshes_the_shell(user32):
    win = user32.add(1, style=ws.WS_EX_APPWINDOW | ws.WS_EX_NOACTIVATE)
    assert ws.set_tool_window(1, True) is True
    assert win.style == ws.WS_EX_TOOLWINDOW | ws.WS_EX_NOACTIVATE
    assert win.visible
    assert user32.calls == [
        ("SetWindowPos", 1, ws.SWP_KEEP | ws.SWP_HIDEWINDOW),  # hidden and shown again, never activated
        ("SetWindowLongPtrW", 1, ws.WS_EX_TOOLWINDOW | ws.WS_EX_NOACTIVATE),
        ("SetWindowPos", 1, ws.SWP_KEEP | ws.SWP_SHOWWINDOW),
    ]
    assert ws.SWP_KEEP & ws.SWP_NOACTIVATE


def test_tool_window_of_a_hidden_window_stays_hidden(user32):
    win = user32.add(1, visible=False)
    assert ws.set_tool_window(1, True) is True
    assert win.style == ws.WS_EX_TOOLWINDOW and not win.visible
    assert "SetWindowPos" not in user32.names()


def test_tool_window_already_set_touches_nothing(user32):
    user32.add(1, style=ws.WS_EX_TOOLWINDOW)
    assert ws.set_tool_window(1, True) is True
    assert user32.calls == []


def test_tool_window_off_restores_the_style(user32):
    win = user32.add(1, style=ws.WS_EX_TOOLWINDOW | ws.WS_EX_LAYERED)
    assert ws.set_tool_window(1, False) is True
    assert win.style == ws.WS_EX_LAYERED


def test_a_refused_style_change_still_shows_the_window_again(user32):
    win = user32.add(1)
    user32.refuse.add("SetWindowLongPtrW")
    assert ws.set_tool_window(1, True) is False
    assert win.visible  # the subtitles are never left hidden by a half-done change


# --- see-through and click-through -------------------------------------------------------

@pytest.mark.parametrize("fraction, alpha", [(0.85, 217), (0.5, 128), (1.0, 255), (2.0, 255), (0.1, 76), (-3, 76)])
def test_opacity_is_clamped_to_readable(user32, fraction, alpha):
    win = user32.add(1)
    assert ws.set_opacity(1, fraction) is True
    assert win.alpha == alpha
    assert win.style & ws.WS_EX_LAYERED
    assert user32.calls[-1] == ("SetLayeredWindowAttributes", 1, 0, alpha, ws.LWA_ALPHA)


def test_opacity_keeps_the_other_styles(user32):
    win = user32.add(1, style=ws.WS_EX_TOOLWINDOW | ws.WS_EX_NOACTIVATE)
    ws.set_opacity(1, 0.7)
    assert win.style == ws.WS_EX_TOOLWINDOW | ws.WS_EX_NOACTIVATE | ws.WS_EX_LAYERED


def test_opacity_of_a_layered_window_does_not_restyle_it(user32):
    user32.add(1, style=ws.WS_EX_LAYERED)
    ws.set_opacity(1, 0.7)
    assert "SetWindowLongPtrW" not in user32.names()


def test_opacity_garbage_is_false(user32):
    user32.add(1)
    assert ws.set_opacity(1, "abc") is False
    assert ws.set_opacity(1, None) is False
    assert user32.calls == []


def test_click_through_on_is_transparent_and_layered_and_stays_visible(user32):
    win = user32.add(1)
    assert ws.set_click_through(1, True) is True
    assert win.style == ws.WS_EX_LAYERED | ws.WS_EX_TRANSPARENT
    assert win.alpha == 255  # a layered window is invisible until its attributes are set


def test_click_through_keeps_a_chosen_opacity(user32):
    win = user32.add(1)
    ws.set_opacity(1, 0.6)
    ws.set_click_through(1, True)
    assert win.alpha == round(0.6 * 255)


def test_click_through_off_clears_only_transparent(user32):
    win = user32.add(1)
    ws.set_opacity(1, 0.6)
    ws.set_click_through(1, True)
    assert ws.set_click_through(1, False) is True
    assert win.style == ws.WS_EX_LAYERED  # still layered: the opacity survives
    assert win.alpha == round(0.6 * 255)


def test_click_through_off_on_a_plain_window_touches_nothing(user32):
    user32.add(1)
    assert ws.set_click_through(1, False) is True
    assert user32.calls == []


# --- show and hide -----------------------------------------------------------------------

def test_show_does_not_activate_and_hide_hides(user32):
    win = user32.add(1, visible=False)
    assert ws.set_visible(1, True) is True
    assert user32.calls[-1] == ("ShowWindow", 1, ws.SW_SHOWNOACTIVATE) and win.visible
    assert ws.set_visible(1, False) is True
    assert user32.calls[-1] == ("ShowWindow", 1, ws.SW_HIDE) and not win.visible


def test_showing_a_window_that_is_gone_is_false(user32):
    assert ws.set_visible(404, True) is False
    assert user32.calls == []


# --- never raises ------------------------------------------------------------------------

def test_nothing_raises_when_user32_cannot_load(monkeypatch):
    def broken():
        raise OSError("no user32")

    monkeypatch.setattr(ws, "_load", broken)
    monkeypatch.setattr(ws, "_lib_cache", None)
    assert ws.supported() is False
    assert ws.hide_from_capture(1, True) is False
    assert ws.get_affinity(1) is None
    assert ws.find_windows("x") == []
    assert [ws.set_tool_window(1, True), ws.set_opacity(1, 0.5), ws.set_click_through(1, True),
            ws.set_visible(1, True)] == [False] * 4


def test_nothing_raises_when_a_call_blows_up(user32):
    user32.add(1)

    def boom(*args):
        raise ctypes.ArgumentError("bad handle")

    for name in ws._SIGNATURES:
        setattr(user32, name, Fn(boom))
    assert ws.hide_from_capture(1, True) is False
    assert ws.get_affinity(1) is None
    assert ws.find_windows("x") == []
    assert [ws.set_tool_window(1, True), ws.set_opacity(1, 0.5), ws.set_click_through(1, True),
            ws.set_visible(1, True)] == [False] * 4


def test_supported_follows_the_build(user32, monkeypatch):
    assert ws.supported() is True
    monkeypatch.setattr(ws, "build", lambda: ws.MIN_BUILD - 1)
    assert ws.supported() is False
