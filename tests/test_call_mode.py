"""Call mode: the app's windows out of screen capture, the subtitles that open with a call, the panic hotkey."""
import argparse
import threading
import time
import types

import pytest

import app
import live_translator as lt
import soniox_engine
import winstealth as ws
from test_robustness import StubEngine
from test_units import CABLE, SPEAKERS
from test_winstealth import install_fake_user32

MAIN, SUBTITLES = app.MAIN_TITLE, app.OVERLAY_TITLE


def wait_for(predicate, what="condition", timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


class Event(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self


class FakeWindow:
    """A pywebview window: the OS side of it is a Win in the fake user32."""

    def __init__(self, desk, hwnd):
        self.desk, self.hwnd, self.shown = desk, hwnd, 0
        self.events = types.SimpleNamespace(closed=Event(), moved=Event(), resized=Event())

    def show(self):
        self.shown += 1

    def destroy(self):
        self.desk.user32.windows.pop(self.hwnd, None)
        for handler in self.events.closed:
            handler()


class Desk:
    """A desktop the Api can be tested on: fake user32, fake pywebview windows, the hotkeys it asked for."""

    def __init__(self, api, user32, hotkeys):
        self.api, self.user32, self.hotkeys = api, user32, hotkeys
        self.created, self.windows, self.appear, self.hwnd = [], [], True, 100

    def add(self, title, **kwargs):
        self.hwnd += 1
        self.user32.add(self.hwnd, title=title, **kwargs)
        return self.user32.windows[self.hwnd]

    def create_window(self, title, **kwargs):
        self.created.append((title, kwargs))
        if self.appear:
            self.add(title, visible=not kwargs.get("hidden"))
        window = FakeWindow(self, self.hwnd if self.appear else None)
        self.windows.append(window)
        return window

    def hotkey(self, ident):
        return next(callback for callback, _, i in self.hotkeys if i == ident)

    def overlay_events(self):
        return [e["value"] for e in self.api._bus.since(0) if e["type"] == "overlay"]


@pytest.fixture
def desk(monkeypatch, tmp_path):
    user32 = install_fake_user32(monkeypatch)
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(app, "RECORDS_DIR", tmp_path / "records")
    monkeypatch.setattr(lt, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(lt, "refresh_devices", lambda: False)
    monkeypatch.setattr(lt, "Engine", StubEngine)
    devices = [{**d, "hostapi": 0} for d in (SPEAKERS, CABLE)]
    monkeypatch.setattr(lt, "query_devices", lambda: devices)
    monkeypatch.setattr(lt, "wasapi_index", lambda: 0)
    monkeypatch.setattr(lt, "default_name", {"input": "Microphone (USB)", "output": "Headphones"}.get)
    monkeypatch.setattr(app, "set_dark_title_bar", lambda title: None)  # the real one would find the installed app
    monkeypatch.setattr(app, "STEALTH_WAIT", 0.3)
    monkeypatch.setattr(app, "STEALTH_STEP", 0.01)
    for name in app.KEY_ENVS.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(soniox_engine.KEY_ENV, "soniox-key")
    StubEngine.made = []
    hotkeys = []
    monkeypatch.setattr(lt, "start_hotkey", lambda callback, vk=0x4D, ident=1: hotkeys.append((callback, vk, ident)) or True)
    api = app.Api(argparse.Namespace(proxy=None))
    api._window = types.SimpleNamespace(on_top=False)
    desk = Desk(api, user32, hotkeys)
    monkeypatch.setattr(app.webview, "create_window", desk.create_window, raising=False)
    yield desk
    api._stop_engine()


# --- the saved settings reach the open windows -----------------------------------------

def test_hide_from_capture_setting_reaches_both_windows(desk):
    windows = [desk.add(MAIN), desk.add(SUBTITLES)]
    for win in windows:
        win.affinity = ws.WDA_EXCLUDEFROMCAPTURE
    desk.api.save_settings({"hide_from_capture": False})
    assert [win.affinity for win in windows] == [ws.WDA_NONE, ws.WDA_NONE]  # off clears the flag
    desk.api.save_settings({"hide_from_capture": True})
    assert [win.affinity for win in windows] == [ws.WDA_EXCLUDEFROMCAPTURE] * 2


def test_opacity_and_click_through_apply_to_the_subtitles_only(desk):
    main, subtitles = desk.add(MAIN), desk.add(SUBTITLES)
    desk.api.save_settings({"overlay_opacity": 0.5})
    assert subtitles.alpha == 128 and subtitles.style & ws.WS_EX_TOOLWINDOW
    desk.api.save_settings({"overlay_opacity": 9})
    assert subtitles.alpha == 255  # clamped
    desk.api.save_settings({"overlay_opacity": 0.01})
    assert subtitles.alpha == 76  # never less than 0.3: unreadable
    desk.api.save_settings({"overlay_click_through": True})
    assert subtitles.style & ws.WS_EX_TRANSPARENT and subtitles.alpha == 76
    desk.api.save_settings({"overlay_click_through": False})
    assert not subtitles.style & ws.WS_EX_TRANSPARENT and subtitles.alpha == 76
    assert (main.style, main.alpha) == (0, None)


def test_a_garbage_opacity_falls_back_to_the_default(desk):
    subtitles = desk.add(SUBTITLES)
    desk.api.save_settings({"overlay_opacity": "abc"})
    assert subtitles.alpha == round(app.DEFAULTS["overlay_opacity"] * 255)


def test_other_settings_leave_the_windows_alone(desk):
    desk.add(MAIN)
    desk.add(SUBTITLES)
    desk.api.save_settings({"volume": 0.3, "overlay_auto": False})
    assert desk.user32.calls == []


def test_saving_settings_without_windows_is_fine(desk):
    desk.api.save_settings({"hide_from_capture": False, "overlay_opacity": 0.4})
    assert desk.user32.calls == []


# --- the stealth status ----------------------------------------------------------------

def test_stealth_status_is_read_back_from_the_os(desk):
    status = desk.api.get_stealth_status()
    assert status == {"supported": True, "enabled": True, "main": None, "overlay": None, "build": 19045}
    main, subtitles = desk.add(MAIN), desk.add(SUBTITLES)
    status = desk.api.get_stealth_status()
    assert (status["main"], status["overlay"]) == (False, False)  # open, but the OS says they can be captured
    main.affinity = ws.WDA_EXCLUDEFROMCAPTURE
    assert desk.api.get_stealth_status()["main"] is True
    desk.add(SUBTITLES).affinity = ws.WDA_EXCLUDEFROMCAPTURE  # a second window of that title must be excluded too
    subtitles.affinity = ws.WDA_EXCLUDEFROMCAPTURE
    assert desk.api.get_stealth_status()["overlay"] is True
    subtitles.affinity = ws.WDA_NONE
    assert desk.api.get_stealth_status()["overlay"] is False


def test_stealth_status_shows_the_setting_apart_from_the_reality(desk):
    desk.add(MAIN).affinity = ws.WDA_EXCLUDEFROMCAPTURE
    desk.api.save_settings({"hide_from_capture": False})
    assert desk.api.get_stealth_status()["enabled"] is False


def test_stealth_status_on_an_old_windows(desk, monkeypatch):
    monkeypatch.setattr(ws, "build", lambda: 18363)
    status = desk.api.get_stealth_status()
    assert (status["supported"], status["build"]) == (False, 18363)
    assert set(status) == {"supported", "enabled", "main", "overlay", "build"}


def test_stealth_status_when_ctypes_fails(desk, monkeypatch):
    def broken():
        raise OSError("no user32")

    monkeypatch.setattr(ws, "_load", broken)
    monkeypatch.setattr(ws, "_lib_cache", None)
    assert desk.api.get_stealth_status() == {"supported": False, "enabled": True, "main": None, "overlay": None,
                                             "build": 19045}


def test_stealth_status_never_raises(desk, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ws, "find_windows", boom)
    status = desk.api.get_stealth_status()
    assert status["supported"] is False and status["main"] is None and status["overlay"] is None


# --- the subtitles -----------------------------------------------------------------------

def test_a_call_opens_the_subtitles_once(desk):
    assert desk.api.start()["ok"]
    assert len(desk.created) == 1
    title, kwargs = desk.created[0]
    assert title == SUBTITLES
    assert kwargs["hidden"] is True and kwargs["focus"] is False  # no flash on a shared screen, no stolen keyboard
    assert kwargs["on_top"] and kwargs["frameless"] and kwargs["min_size"] == (360, 110)
    assert desk.overlay_events() == [True]  # the main window's button follows
    assert desk.api.start()["ok"]  # the same call again
    assert len(desk.created) == 1


def test_subtitles_closed_by_me_stay_closed_for_the_call(desk):
    desk.api.start()
    desk.windows[0].destroy()
    assert desk.api._overlay is None
    assert desk.api.start()["ok"]
    assert len(desk.created) == 1
    desk.api.stop()
    desk.api.start()  # the next call is a fresh one
    assert len(desk.created) == 2


def test_subtitles_opened_by_hand_are_not_opened_twice(desk):
    assert desk.api.toggle_overlay() is True
    desk.api.start()
    assert len(desk.created) == 1
    assert desk.overlay_events() == []


def test_no_subtitles_when_auto_is_off(desk):
    desk.api.save_settings({"overlay_auto": False})
    assert desk.api.start()["ok"]
    assert desk.created == [] and desk.overlay_events() == []


def test_no_subtitles_without_a_window_to_put_them_on(desk):
    desk.api._window = None  # the API used without the desktop app
    assert desk.api.start()["ok"]
    assert desk.created == [] and desk.overlay_events() == []


def test_a_failed_start_opens_nothing(desk, monkeypatch):
    monkeypatch.delenv(soniox_engine.KEY_ENV)
    assert desk.api.start() == {"ok": False, "error": "no_key"}
    assert desk.created == []


def test_subtitles_that_cannot_be_created_do_not_break_the_call(desk, monkeypatch):
    def broken(title, **kwargs):
        raise RuntimeError("no window")

    monkeypatch.setattr(app.webview, "create_window", broken, raising=False)
    assert desk.api.start()["ok"]
    assert desk.api._overlay is None and desk.overlay_events() == []


def test_toggle_overlay_closes_the_subtitles(desk):
    assert desk.api.toggle_overlay() is True
    assert desk.api.toggle_overlay() is False
    assert desk.api._overlay is None


def test_the_subtitles_are_dressed_before_they_appear(desk):
    desk.api.toggle_overlay()
    win = desk.user32.windows[desk.windows[0].hwnd]
    wait_for(lambda: win.visible, "the subtitles to appear")
    assert win.affinity == ws.WDA_EXCLUDEFROMCAPTURE
    assert win.style & ws.WS_EX_TOOLWINDOW and not win.style & ws.WS_EX_APPWINDOW
    assert win.alpha == 217 and not win.style & ws.WS_EX_TRANSPARENT
    names = desk.user32.names()
    assert names.index("SetWindowDisplayAffinity") < names.index("ShowWindow")
    assert desk.user32.calls[-1][2] == ws.SW_SHOWNOACTIVATE
    assert desk.windows[0].shown == 0  # window.show() would take the keyboard focus


def test_the_subtitles_follow_the_saved_call_mode_settings(desk):
    desk.api.save_settings({"hide_from_capture": False, "overlay_opacity": 0.5, "overlay_click_through": True})
    desk.api.toggle_overlay()
    win = desk.user32.windows[desk.windows[0].hwnd]
    wait_for(lambda: win.visible, "the subtitles to appear")
    assert win.affinity == ws.WDA_NONE and win.alpha == 128
    assert win.style & ws.WS_EX_TRANSPARENT and win.style & ws.WS_EX_TOOLWINDOW


def test_subtitles_whose_window_is_not_found_are_still_shown(desk):
    desk.appear = False
    desk.api.toggle_overlay()
    wait_for(lambda: desk.windows[0].shown == 1, "the fallback show")


def test_subtitles_stay_hidden_while_the_panic_switch_is_on(desk):
    desk.hotkey(3)()
    desk.api.toggle_overlay()
    win = desk.user32.windows[desk.windows[0].hwnd]
    wait_for(lambda: win.alpha == 217, "the subtitles to be dressed")
    time.sleep(0.1)
    assert not win.visible and desk.windows[0].shown == 0
    desk.hotkey(3)()
    assert win.visible


# --- the main window ---------------------------------------------------------------------

def test_the_main_window_is_excluded_when_it_is_shown(desk):
    main = desk.add(MAIN)
    desk.api._on_shown()
    wait_for(lambda: main.affinity == ws.WDA_EXCLUDEFROMCAPTURE, "the main window to be excluded")


def test_the_main_window_is_found_even_if_it_appears_a_moment_late(desk):
    threading.Timer(0.1, desk.add, args=(MAIN,)).start()
    desk.api._on_shown()
    wait_for(lambda: any(w.affinity == ws.WDA_EXCLUDEFROMCAPTURE for w in desk.user32.windows.values()),
             "the late main window to be excluded")


def test_the_main_window_stays_capturable_when_the_setting_is_off(desk):
    main = desk.add(MAIN)
    main.affinity = ws.WDA_EXCLUDEFROMCAPTURE
    desk.api.save_settings({"hide_from_capture": False})
    main.affinity = ws.WDA_EXCLUDEFROMCAPTURE
    desk.api._on_shown()
    wait_for(lambda: main.affinity == ws.WDA_NONE, "the flag to be cleared")


# --- the panic hotkey --------------------------------------------------------------------

def test_the_hide_hotkey_is_registered_next_to_the_other_two(desk):
    assert [(vk, ident) for _, vk, ident in desk.hotkeys] == [(0x4D, 1), (0x20, 2), (0x48, 3)]
    assert desk.api.get_state()["hotkey_hide"] == "Ctrl+Alt+H"
    assert lt.HOTKEY_HIDE_NAME == "Ctrl+Alt+H"


def test_a_hide_hotkey_taken_by_another_program_is_reported(desk, monkeypatch):
    monkeypatch.setattr(lt, "start_hotkey", lambda callback, vk=0x4D, ident=1: ident != 3)
    api = app.Api(argparse.Namespace(proxy=None))
    assert api.get_state()["hotkey_hide"] is None


def test_the_hide_hotkey_hides_and_restores_every_window_of_the_app(desk):
    main, subtitles = desk.add(MAIN), desk.add(SUBTITLES)
    other = desk.add("Zoom Meeting")
    hide = desk.hotkey(3)
    hide()
    assert (main.visible, subtitles.visible, other.visible) == (False, False, True)
    hide()
    assert (main.visible, subtitles.visible, other.visible) == (True, True, True)
    commands = [call[2] for call in desk.user32.calls if call[0] == "ShowWindow"]
    assert commands == [ws.SW_HIDE] * 2 + [ws.SW_SHOWNOACTIVATE] * 2  # never an activating show: no focus theft


def test_the_hide_hotkey_with_no_windows_is_harmless(desk):
    desk.hotkey(3)()
    desk.hotkey(3)()
    assert desk.user32.calls == []
