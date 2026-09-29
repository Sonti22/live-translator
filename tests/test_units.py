"""Pure units: compose_transcript, Bus, LagMeter, detect_proxy, endpoint overrides, Api.start checks,
stealth device checks, the voice provider of the Soniox engine."""
import argparse
import asyncio
import collections
import json
import os
import re
import sys
import threading
import types
import urllib.request
import wave
from pathlib import Path

import numpy as np
import pytest

import app
import live_translator as lt
import meeting_notes
import netcheck
import soniox_engine
import voice_clone
from mocks import FakePlayer, FakeSink, until

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import latency_test  # noqa: E402

REFRESH = lt.refresh_devices  # the real one, for its own tests


@pytest.fixture(autouse=True)
def _portaudio_is_never_restarted(monkeypatch):
    """No test re-initialises the real PortAudio (the engine does it before every call)."""
    monkeypatch.setattr(lt, "refresh_devices", lambda: False)


REFRESH_DEVICES = app.refresh_devices  # conftest stubs it in every test


def test_endpoints_point_at_local_mocks():
    for value, env in ((lt.URL, "LIVE_TRANSLATOR_URL"),
                       (netcheck.TRACE_URL, "LIVE_TRANSLATOR_TRACE_URL"),
                       (netcheck.SONIOX_EU_STT, "LIVE_TRANSLATOR_SONIOX_EU_STT"),
                       (voice_clone.TTS_URL, "LIVE_TRANSLATOR_TTS_URL"),
                       (voice_clone.TTS_API, "LIVE_TRANSLATOR_TTS_API"),
                       (soniox_engine.STT_URL, "LIVE_TRANSLATOR_SONIOX_STT"),
                       (soniox_engine.TTS_URL, "LIVE_TRANSLATOR_SONIOX_TTS"),
                       (soniox_engine.API_URL, "LIVE_TRANSLATOR_SONIOX_API"),
                       (meeting_notes.API_URL, "LIVE_TRANSLATOR_OPENAI_API")):
        assert value == os.environ[env]
        assert value.split("://", 1)[1].startswith("127.0.0.1:")


# --- compose_transcript --------------------------------------------------------------

def test_compose_transcript_pairs_phrases_chronologically():
    deltas = [
        (0.0, "me_src", "Привет, "), (0.4, "me_src", "как дела?"),
        (0.5, "me_dst", "Hi, "), (0.9, "me_dst", "how are you?"),
        (3.0, "them_src", "Fine, thanks."), (3.2, "them_dst", "Хорошо, спасибо."),
        (6.0, "me_src", "Отлично"),  # no translation yet
    ]
    assert app.compose_transcript(deltas) == [
        "[00:00] Я: Привет, как дела?",
        "        → Hi, how are you?",
        "[00:03] Собеседник: Fine, thanks.",
        "        → Хорошо, спасибо.",
        "[00:06] Я: Отлично",
    ]


def test_compose_transcript_pause_splits_a_phrase():
    assert app.compose_transcript([(0.0, "me_src", "Привет"), (1.5, "me_src", "мир"), (125.0, "me_src", "Да.")]) == [
        "[00:00] Я: Привет", "[00:01] Я: мир", "[02:05] Я: Да."]


def test_compose_transcript_translation_without_source():
    assert app.compose_transcript([(2.0, "them_dst", "Привет.")]) == ["[00:02] Собеседник: —", "        → Привет."]


def test_compose_transcript_empty():
    assert app.compose_transcript([]) == []


def test_compose_transcript_keeps_hours():
    [line] = app.compose_transcript([(3725.0, "me_src", "Да.")])
    assert line in ("[62:05] Я: Да.", "[1:02:05] Я: Да.", "[01:02:05] Я: Да.")


# --- Bus ----------------------------------------------------------------------------

def test_bus_since():
    bus = app.Bus()
    bus.caption("me_src", "Я", "Привет")
    bus.note("hello")
    bus.status("Я → EN", "подключено", True)
    bus.lag(1.234)
    assert bus.since(0) == [
        {"type": "caption", "kind": "me_src", "text": "Привет", "seq": 1},
        {"type": "note", "text": "hello", "seq": 2},
        {"type": "status", "label": "Я → EN", "text": "подключено", "ok": True, "seq": 3},
        {"type": "lag", "value": 1.23, "seq": 4},
    ]
    assert [e["seq"] for e in bus.since(2)] == [3, 4]
    assert bus.since(4) == [] and bus.since(10) == []
    assert [(kind, text) for _, kind, text, _speaker in bus.record] == [("me_src", "Привет")]


def test_bus_since_after_trimming():
    bus = app.Bus()
    assert bus.since(0) == []
    for i in range(2 * app.Bus.MAX + 7):
        bus.emit(type="note", text=str(i))
    assert len(bus._events) <= app.Bus.MAX
    first = bus._events[0]["seq"]
    assert first > 1  # old events were trimmed
    for seq in (0, first - 1, first, first + 1, bus.seq // 2 + 3, bus.seq - 1, bus.seq):
        assert [e["seq"] for e in bus.since(seq)] == list(range(max(seq + 1, first), bus.seq + 1))


def test_bus_trims_a_quarter_past_max():
    bus = app.Bus()
    for i in range(app.Bus.MAX + 1):
        bus.emit(type="note", text=str(i))
    assert len(bus._events) == app.Bus.MAX + 1 - app.Bus.MAX // 4
    assert bus._events[0]["seq"] == app.Bus.MAX // 4 + 1
    assert bus.since(0) == bus._events  # a poller that fell behind gets what is left


# --- LagMeter -----------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(lt, "time", clock)  # only live_translator sees the fake clock
    return clock


def test_lag_from_phrase_start_to_first_sound(clock):
    lag = lt.LagMeter()
    lag.on_input(100)  # quiet: not speech
    assert lag.speech_start is None
    lag.on_input(5000)
    clock.t += 0.3
    lag.on_input(5000)  # same phrase
    assert lag.speech_start == 1000.0
    clock.t += 0.9
    assert lag.on_output() == pytest.approx(1.2)
    clock.t += 0.1
    assert lag.on_output() is None  # rest of the same answer
    clock.t += 2.0
    assert lag.on_output() is None  # new sound, but no new phrase


def test_lag_next_phrase_measured_again(clock):
    lag = lt.LagMeter()
    lag.on_input(5000)
    clock.t += 1.0
    assert lag.on_output() == pytest.approx(1.0)
    clock.t += 1.0
    lag.on_input(5000)  # after a pause: a new phrase
    clock.t += 0.5
    assert lag.on_output() == pytest.approx(0.5)


def test_lag_keeps_first_unanswered_phrase_until_stale(clock):
    lag = lt.LagMeter()
    lag.on_input(5000)
    clock.t += 1.0
    lag.on_input(5000)  # pause, but the first phrase is still unanswered
    assert lag.speech_start == 1000.0
    clock.t += 6.0
    lag.on_input(5000)  # stale: start over
    assert lag.speech_start == 1007.0


def test_lag_too_long_is_dropped(clock):
    lag = lt.LagMeter()
    lag.on_input(5000)
    clock.t += lt.LagMeter.STALE + 1
    assert lag.on_output() is None
    assert lag.speech_start is None


def test_lag_output_without_speech(clock):
    assert lt.LagMeter().on_output() is None


# --- detect_proxy -------------------------------------------------------------------

@pytest.mark.parametrize("explicit, expected", [
    ("none", None), ("NONE", None),
    ("socks5h://127.0.0.1:1080", "socks5h://127.0.0.1:1080"),
    ("http://proxy.local:3128", "http://proxy.local:3128"),
])
def test_detect_proxy_explicit(monkeypatch, explicit, expected):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: pytest.fail("system proxy must not be read"))
    assert lt.detect_proxy(explicit) == expected


@pytest.mark.parametrize("system, expected", [
    # what Windows reports for ProxyServer "socks=127.0.0.1:10808"
    ({"socks": "socks://127.0.0.1:10808", "http": "socks4://127.0.0.1:10808", "https": "socks4://127.0.0.1:10808"},
     "socks5h://127.0.0.1:10808"),
    ({"socks": "socks4://127.0.0.1:1080"}, "socks5h://127.0.0.1:1080"),
    ({"all": "socks5://10.0.0.1:1080"}, "socks5h://10.0.0.1:1080"),
    ({"http": "http://127.0.0.1:10809", "https": "http://127.0.0.1:10809"}, "http://127.0.0.1:10809"),
    ({"https": "https://proxy.local:8443"}, "https://proxy.local:8443"),
    ({}, None),
    ({"no": "*"}, None),
])
def test_detect_proxy_system(monkeypatch, system, expected):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: dict(system))
    monkeypatch.setattr(lt, "_local_proxy_down", lambda url: False)  # pretend the VPN client is running
    assert lt.detect_proxy(None) == expected
    assert lt.detect_proxy("") == expected


# --- Api.start checks -----------------------------------------------------------------

CABLE = {"name": "CABLE Input (VB-Audio Virtual Cable)", "max_output_channels": 2, "max_input_channels": 0}
SPEAKERS = {"name": "Speakers (Realtek(R) Audio)", "max_output_channels": 2, "max_input_channels": 0}


@pytest.fixture
def api(monkeypatch, tmp_path):
    """app.Api with defaults: no hotkey, no real .env / settings.json / devices, no engine thread."""
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(lt, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(lt, "start_hotkey", lambda callback, **kw: False)
    for name in app.KEY_ENVS.values():
        monkeypatch.delenv(name, raising=False)
    api = app.Api(argparse.Namespace(proxy=None))
    api.devices = [SPEAKERS, CABLE]
    monkeypatch.setattr(lt, "query_devices", lambda: api.devices)
    api.started_engine = 0

    def start_engine():
        api.started_engine += 1

    monkeypatch.setattr(api, "_start_engine", start_engine)
    return api


@pytest.mark.parametrize("engine, env", [("soniox", soniox_engine.KEY_ENV), ("openai", "OPENAI_API_KEY")])
def test_start_with_the_engine_key(api, monkeypatch, engine, env):
    monkeypatch.setenv(env, "test-key")
    api._settings["engine"] = engine
    assert api.start()["ok"] is True
    assert api.started_engine == 1


def test_start_needs_some_engine_key(api):
    assert api.start() == {"ok": False, "error": "no_key"}
    assert api.started_engine == 0


def test_start_falls_back_to_openai_without_a_soniox_key(api, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert api._settings["engine"] == "soniox"  # the default
    result = api.start()
    assert result["ok"] is True and result["engine"] == "openai" and "OpenAI" in result["notice"]
    assert api._settings["engine"] == "openai" and api._settings["engine_auto"] is True
    assert api.started_engine == 1


def test_soniox_key_switches_an_automatic_openai_back(api, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert "OpenAI" in api._auto_engine()  # what the window does on start
    assert api._settings["engine"] == "openai"
    result = api.set_key("soniox-key", "soniox")
    assert result["ok"] and result["engine"] == "soniox" and "Записать голос" in result["notice"]


def test_a_key_saved_mid_call_switches_the_engine_at_the_next_start(api, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert api.start()["engine"] == "openai"
    monkeypatch.setattr(api, "_running", lambda: True)
    result = api.set_key("soniox-key", "soniox")
    assert (result["engine"], result["notice"]) == ("openai", None)  # the call keeps its voice
    monkeypatch.setattr(api, "_running", lambda: False)
    assert api.start()["engine"] == "soniox"


def test_engine_chosen_by_hand_is_kept(api, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv(soniox_engine.KEY_ENV, "soniox-key")
    api.save_settings({"engine": "openai", "engine_auto": False})
    assert api._auto_engine() is None
    assert api._settings["engine"] == "openai"


@pytest.fixture
def both_keys(api, monkeypatch):
    monkeypatch.setenv(soniox_engine.KEY_ENV, "soniox-key")
    monkeypatch.setenv(voice_clone.KEY_ENV, "cartesia-key")
    return api


def test_a_cartesia_key_makes_cartesia_the_voice(both_keys):
    assert both_keys._settings["voice_provider"] == "soniox"  # the default until the key is there
    result = both_keys.start()
    assert result["ok"] and "Cartesia" in result["notice"]
    assert both_keys._settings["voice_provider"] == "cartesia" and result["settings"]["voice_provider"] == "cartesia"
    assert both_keys._args().voice_provider == "cartesia"
    assert both_keys._notice() is None  # said once


def test_the_cartesia_key_switches_the_voice_when_it_is_saved(api, monkeypatch):
    monkeypatch.setenv(soniox_engine.KEY_ENV, "soniox-key")
    result = api.set_key("cartesia-key", "cartesia")
    assert result["ok"] and "Cartesia" in result["notice"]
    assert result["settings"]["voice_provider"] == "cartesia"  # the window shows it without another get_state


def test_the_window_learns_of_the_switch_from_the_state(both_keys, monkeypatch):
    monkeypatch.setattr(lt, "wasapi_index", lambda: 0)
    monkeypatch.setattr(lt, "default_name", lambda kind: None)
    both_keys.devices = [{**d, "hostapi": 0} for d in (SPEAKERS, CABLE)]
    state = both_keys.get_state()
    assert "Cartesia" in state["notice"] and state["settings"]["voice_provider"] == "cartesia"


def test_a_provider_picked_by_hand_is_kept(both_keys):
    both_keys.save_settings({"voice_provider": "soniox", "provider_auto": False})
    assert both_keys._auto_provider() is None and both_keys._settings["voice_provider"] == "soniox"


@pytest.mark.parametrize("state", ["running", "restarting"])
def test_the_voice_never_changes_mid_call(both_keys, monkeypatch, state):
    if state == "running":
        monkeypatch.setattr(both_keys, "_running", lambda: True)
    else:
        both_keys._restarting = True  # the old engine is gone, the new one not started yet
    assert both_keys._auto_provider() is None and both_keys._settings["voice_provider"] == "soniox"


def test_the_openai_engine_has_no_voice_provider_to_switch(both_keys):
    both_keys.save_settings({"engine": "openai", "engine_auto": False})
    assert both_keys._auto_provider() is None and both_keys._settings["voice_provider"] == "soniox"


def test_my_soniox_clone_is_not_traded_for_a_stock_cartesia_voice(both_keys):
    both_keys.save_settings({"voice": "clone", "soniox_voice_id": "s-mine"})
    assert both_keys._auto_provider() is None and both_keys._settings["voice_provider"] == "soniox"
    assert both_keys._clone_provider() == "cartesia"  # the next clone is made there, and switches then


def test_a_cartesia_clone_of_mine_comes_back_with_the_provider(both_keys):
    both_keys.save_settings({"voice": "builtin", "clone_auto_off": True, "cartesia_voice_id": "c-mine"})
    assert "Cartesia" in both_keys._auto_provider()
    assert both_keys._settings["voice"] == "clone" and both_keys._settings["clone_auto_off"] is False


def test_a_stock_voice_stays_when_no_clone_was_wanted(both_keys):
    both_keys.save_settings({"cartesia_voice_id": "c-mine"})
    both_keys._auto_provider()
    assert both_keys._settings["voice"] == "builtin"


@pytest.mark.parametrize("provider", ["inworld", "soniox"])
def test_the_clone_goes_where_the_voice_is_spoken(both_keys, monkeypatch, provider):
    monkeypatch.setenv("INWORLD_API_KEY", "inworld-key")
    both_keys.save_settings({"voice_provider": provider, "provider_auto": False})
    assert both_keys._clone_provider() == provider  # picked by hand: no automatic choice


def test_start_needs_vb_cable(api, monkeypatch):
    monkeypatch.setenv(soniox_engine.KEY_ENV, "test-key")
    api.devices = [SPEAKERS]
    assert api.start() == {"ok": False, "error": "no_cable"}
    assert api.started_engine == 0


@pytest.mark.parametrize("cable, found", [
    (CABLE, True),
    ({**CABLE, "name": "CABLE-A Input (VB-Audio Cable A)"}, True),  # only VB-Cable A/B installed
    ({**CABLE, "name": "CABLE Output (VB-Audio Virtual Cable)", "max_output_channels": 0, "max_input_channels": 2},
     False),  # a microphone: nothing to play my voice into
    (SPEAKERS, False),
])
def test_the_window_and_start_find_the_cable_alike(api, monkeypatch, cable, found):
    """The pre-call check is offered only with a cable (state.cable_ok): it must be the one start() needs, and the
    engine must then find it too (the default «CABLE Input» is not there with only VB-Cable A installed)."""
    monkeypatch.setenv(soniox_engine.KEY_ENV, "test-key")
    monkeypatch.setattr(lt, "wasapi_index", lambda: 0)
    monkeypatch.setattr(lt, "default_name", lambda kind: None)
    api.devices = [{**d, "hostapi": 0} for d in (SPEAKERS, cable)]
    monkeypatch.setattr(lt, "sd", types.SimpleNamespace(
        query_devices=lambda i=None: api.devices if i is None else api.devices[i]))
    assert api.get_state()["cable_ok"] is found
    assert api.start()["ok"] is found
    if found:
        assert lt.pick_device(api._args().out, "output") == 1


def test_only_another_vb_cable_becomes_the_cable_everywhere(api, monkeypatch):
    """«CABLE Input» (the default) missing, VB-Cable A installed: its full WASAPI name is saved, so the source
    popover marks it and the engine opens it; a cable picked by hand is never replaced."""
    monkeypatch.setattr(lt, "wasapi_index", lambda: 0)
    monkeypatch.setattr(lt, "default_name", lambda kind: None)
    cable_a = {**CABLE, "name": "CABLE-A Input (VB-Audio Cable A)", "hostapi": 0}
    api.devices = [{**SPEAKERS, "hostapi": 0}, {**cable_a, "name": cable_a["name"][:31], "hostapi": 1}, cable_a]
    assert api.get_state()["settings"]["cable"] == "CABLE-A Input (VB-Audio Cable A)"
    assert json.loads(app.SETTINGS_FILE.read_text(encoding="utf-8"))["cable"] == "CABLE-A Input (VB-Audio Cable A)"
    api._settings["cable"] = "CABLE-B Input"  # picked by hand, unplugged now: the engine says it is missing
    assert api.get_state()["settings"]["cable"] == "CABLE-B Input"


def test_the_window_lists_the_devices_windows_has_now(api, monkeypatch):
    """A headset plugged in after launch shows up in the lists; never re-read while the call has streams open."""
    monkeypatch.setattr(lt, "wasapi_index", lambda: 0)
    monkeypatch.setattr(lt, "default_name", lambda kind: None)
    monkeypatch.setattr(app, "refresh_devices", REFRESH_DEVICES)
    monkeypatch.delattr(lt, "refresh_devices", raising=False)
    api.devices = [{**d, "hostapi": 0} for d in (SPEAKERS, CABLE)]
    api.get_state()  # an engine without refresh_devices yet
    headset = {"name": "Headset (Jabra)", "max_input_channels": 1, "max_output_channels": 2, "hostapi": 0}
    monkeypatch.setattr(lt, "refresh_devices", lambda: api.devices.append(headset), raising=False)
    assert "Headset (Jabra)" in api.get_state()["mics"]
    monkeypatch.setattr(api, "_running", lambda: True)
    api.get_state()
    assert api.devices.count(headset) == 1


def test_the_window_learns_the_pause_from_the_state(api, monkeypatch):
    """The main window shows «Пауза» as long as the mini-subtitles keep the call paused, even after a reload."""
    monkeypatch.setattr(lt, "wasapi_index", lambda: 0)
    monkeypatch.setattr(lt, "default_name", lambda kind: None)
    api.devices = [{**d, "hostapi": 0} for d in (SPEAKERS, CABLE)]
    assert api.get_state()["paused"] is False
    api.set_paused(True)
    assert api.get_state()["paused"] is True and api.poll(0)["paused"] is True


# --- Api -> engine arguments, hotkeys, default devices ------------------------------------------

def test_window_never_passes_my_voice_through(api):
    assert api._args().passthrough is False  # the call would hear my Russian


def test_levers_reach_the_engine(api):
    args = api._args()
    assert (args.speed, args.speed_boost, args.trim_silence, args.instant_phrases, args.auto_finalize) == (
        1.0, True, True, True, True)
    api._settings.update(speed=1.2, speed_boost=False, trim_silence=False, instant_phrases=False,
                         auto_finalize=False)
    args = api._args()
    assert (args.speed, args.speed_boost, args.trim_silence, args.instant_phrases, args.auto_finalize) == (
        1.2, False, False, False, False)


@pytest.mark.parametrize("engine, provider, voice_name, voice_id", [
    ("soniox", "soniox", "Adrian", "s-1"),
    ("soniox", "cartesia", "c-katie", "c-1"),
    ("soniox", "inworld", "Clive", "i-1"),
    ("soniox", "elevenlabs", "Adrian", "s-1"),  # unknown: Soniox
    ("openai", "inworld", "c-katie", "c-1"),    # the OpenAI engine's clone is always Cartesia
])
def test_args_follow_the_voice_provider(api, engine, provider, voice_name, voice_id):
    api._settings.update(engine=engine, voice_provider=provider, soniox_voice_id="s-1", cartesia_voice_id="c-1",
                         inworld_voice_id="i-1", cartesia_builtin_id="c-katie")
    args = api._args()
    expected = "cartesia" if engine == "openai" else provider if provider in lt.PROVIDER_NAMES else "soniox"
    assert (args.voice_provider, args.voice_name, args.voice_id) == (expected, voice_name, voice_id)
    assert args.inworld_model == "inworld-tts-2-flash"


def test_voice_changes_restart_the_engine():
    assert {"voice_provider", "cartesia_builtin_id", "inworld_voice_name", "inworld_voice_id", "inworld_model",
            "speed_boost", "trim_silence", "instant_phrases", "auto_finalize"} <= app.ENGINE_KEYS
    assert set(app.BUILTIN_FIELDS) == set(lt.PROVIDER_NAMES) <= set(app.PRICE_PER_MIN)
    assert app.KEY_ENVS["inworld"] == "INWORLD_API_KEY"


def test_both_hotkeys_are_registered(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "settings.json")
    registered = []

    def start_hotkey(callback, vk=0x4D, ident=1):
        registered.append((callback, vk, ident))
        return ident == 1  # Ctrl+Alt+Space is taken by another program

    monkeypatch.setattr(lt, "start_hotkey", start_hotkey)
    monkeypatch.setattr(lt, "default_name", {"input": "Microphone (USB)", "output": "Headphones"}.get)
    devices = [{**d, "hostapi": 0} for d in (SPEAKERS, CABLE)]
    monkeypatch.setattr(lt, "query_devices", lambda: devices)
    monkeypatch.setattr(lt, "wasapi_index", lambda: 0)
    api = app.Api(argparse.Namespace(proxy=None))
    assert [(vk, ident) for _, vk, ident in registered] == [(0x4D, 1), (0x20, 2)]
    state = api.get_state()
    assert (state["hotkey"], state["hotkey_done"]) == ("Ctrl+Alt+M", None)
    assert (state["default_mic"], state["default_out"]) == ("Microphone (USB)", "Headphones")
    api._on_done_hotkey()  # not running: nothing happens
    finished = []
    api._engine = types.SimpleNamespace(finish_turn=lambda: finished.append(True))
    registered[1][0]()  # Ctrl+Alt+Space pressed
    assert finished == [True]


def test_compose_transcript_names_several_speakers():
    lines = app.compose_transcript([
        (0.0, "them_src", "Hello.", "1"), (0.5, "them_dst", "Привет.", "1"),
        (3.0, "them_src", "Hi!", "2"), (3.4, "them_dst", "Здравствуйте!", "2"),
        (5.0, "me_src", "Добрый день.", None),
    ])
    assert lines == ["[00:00] Собеседник 1: Hello.", "        → Привет.",
                     "[00:03] Собеседник 2: Hi!", "        → Здравствуйте!",
                     "[00:05] Я: Добрый день."]


def test_compose_transcript_single_speaker_keeps_plain_label():
    assert app.compose_transcript([(0.0, "them_src", "Hello.", "1")]) == ["[00:00] Собеседник: Hello."]


def test_save_record_keeps_header(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "RECORDS_DIR", tmp_path)
    (tmp_path / "r.txt").write_text("Live Translator — x\nДлительность: 00:01:00\n\n[00:00] Я: Превет.\n", encoding="utf-8")
    api = app.Api.__new__(app.Api)
    assert api.save_record("r.txt", "[00:00] Я: Привет.\n        → Hello.\n")
    assert (tmp_path / "r.txt").read_text(encoding="utf-8").splitlines() == [
        "Live Translator — x", "Длительность: 00:01:00", "", "[00:00] Я: Привет.", "        → Hello."]
    assert not api.save_record("missing.txt", "x")


def test_detect_proxy_skips_a_switched_off_local_vpn(monkeypatch):
    import socket
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"socks": f"socks://127.0.0.1:{port}"})
    listener.listen()
    try:
        assert lt.detect_proxy(None) == f"socks5h://127.0.0.1:{port}"  # VPN client running
    finally:
        listener.close()
    assert lt.detect_proxy(None) is None  # switched off: connect directly instead of failing
    assert lt.detect_proxy(f"socks5h://127.0.0.1:{port}") == f"socks5h://127.0.0.1:{port}"  # explicit wins


# --- stealth: only my synthesized English goes into the call ---------------------------------

@pytest.mark.parametrize("mic, out, monitor, expected", [
    ("Microphone (Realtek(R) Audio)", "Headphones (Realtek(R) Audio)", None, set()),
    ("CABLE Output (VB-Audio Virtual Cable)", "Headphones", None, {"mic"}),
    ("Microphone", "CABLE Input (VB-Audio Virtual Cable)", None, {"out"}),
    ("Microphone", "Headphones", "CABLE Input (VB-Audio Virtual Cable)", {"monitor"}),
    (None, None, None, set()),  # unknown devices are not a problem
])
def test_device_problems(mic, out, monitor, expected):
    problems = lt.device_problems(mic, out, monitor)
    assert set(problems) == expected
    assert all(any("а" <= c <= "я" for c in text) for text in problems.values())  # shown to the user as is


def test_default_name_asks_windows_every_time(monkeypatch):
    speakers = iter(["CABLE Input (VB-Audio Virtual Cable)", "Headphones (Realtek(R) Audio)"])
    monkeypatch.setattr(lt, "sc", types.SimpleNamespace(
        default_speaker=lambda: types.SimpleNamespace(name=next(speakers)),
        default_microphone=lambda: types.SimpleNamespace(name="Microphone (USB)")))
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: pytest.fail("PortAudio's defaults are from startup"))
    assert lt.default_name("output") == "CABLE Input (VB-Audio Virtual Cable)"
    assert lt.default_name("output") == "Headphones (Realtek(R) Audio)"  # fixed in Windows settings: no restart
    assert lt.default_name("input") == "Microphone (USB)"


def test_default_name_falls_back_to_portaudio(monkeypatch):
    def gone():
        raise RuntimeError("no default device")

    monkeypatch.setattr(lt, "sc", types.SimpleNamespace(default_speaker=gone, default_microphone=gone))
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 7)
    monkeypatch.setattr(lt, "device_name", {7: "Speakers"}.get)
    assert lt.default_name("output") == "Speakers"


class FakeStream:
    """A sounddevice stream: `closed` after close()."""

    def __init__(self):
        self.closed = False


def test_portaudio_is_restarted_only_while_no_stream_is_open(monkeypatch):
    calls, changed = [], [True]
    monkeypatch.setattr(lt, "sd", types.SimpleNamespace(
        _StreamBase=FakeStream, _initialized=1,
        _terminate=lambda: calls.append("terminate"), _initialize=lambda: calls.append("initialize")))
    monkeypatch.setattr(lt, "_devices_changed", lambda: changed[0])
    preview = FakeStream()  # a voice preview still playing: re-initialising would close it under its owner
    opening = FakeStream.__new__(FakeStream)  # a stream being opened on another thread right now
    assert REFRESH() is False
    preview.closed = True
    assert REFRESH() is False
    del opening
    changed[0] = False  # PortAudio lists the devices Windows has: nothing to re-read
    assert REFRESH() is False and calls == []
    changed[0] = True
    assert REFRESH() is True and calls == ["terminate", "initialize"]


def test_a_refresh_waits_for_a_player_being_opened_and_leaves_it_open(monkeypatch):
    calls, opening, release = [], threading.Event(), threading.Event()

    def slow_open(**kwargs):  # a preview's stream on a pywebview thread, PortAudio still opening it
        opening.set()
        release.wait(5)
        return FakeStream()

    monkeypatch.setattr(lt, "sd", types.SimpleNamespace(
        _StreamBase=FakeStream, _initialized=1, RawOutputStream=slow_open,
        _terminate=lambda: calls.append("terminate"), _initialize=lambda: calls.append("initialize")))
    monkeypatch.setattr(lt, "stream_kwargs", lambda device, blocksize=lt.BLOCK: {})
    monkeypatch.setattr(lt, "_devices_changed", lambda: True)
    players, refreshed = [], []
    preview = threading.Thread(target=lambda: players.append(lt.Player(3)))
    preview.start()
    opening.wait(5)
    refresh = threading.Thread(target=lambda: refreshed.append(REFRESH()))  # the window's get_state meanwhile
    refresh.start()
    try:
        refresh.join(0.2)
        assert refreshed == [] and calls == []  # it waits, and a query after it never meets PortAudio half restarted
    finally:
        release.set()
        preview.join(5)
        refresh.join(5)
    assert players and refreshed == [False] and calls == []  # the preview's stream is open now: left alone


def windows_devices(monkeypatch, windows):
    """soundcard listing the names in windows["input"] / windows["output"] (None: Windows can't say)."""
    def listing(kind):
        if windows[kind] is None:
            raise RuntimeError("no COM")
        return [types.SimpleNamespace(name=name) for name in windows[kind]]

    monkeypatch.setattr(lt, "sc", types.SimpleNamespace(all_microphones=lambda: listing("input"),
                                                        all_speakers=lambda: listing("output")))
    monkeypatch.setattr(lt, "ctypes", types.SimpleNamespace(windll=types.SimpleNamespace(ole32=types.SimpleNamespace(
        CoInitializeEx=lambda *args: 0, CoUninitialize=lambda: None))))


def test_portaudio_is_restarted_only_for_devices_it_does_not_list(monkeypatch):
    fake_devices(monkeypatch, [{**LAPTOP_MIC, "hostapi": 0, "name": "Microphone Array (Realtek(R) Au"}, LAPTOP_MIC,
                               {**SPEAKERS, "hostapi": 1}], default_input=1)
    windows = {"input": [LAPTOP_MIC["name"]], "output": [SPEAKERS["name"]]}
    windows_devices(monkeypatch, windows)
    assert lt._devices_changed() is False  # the MME entry with its cut name is no device of its own
    windows["input"].append("Headset Microphone (Jabra)")  # plugged in since PortAudio started
    assert lt._devices_changed() is True
    windows["input"].pop()
    windows["output"] = []  # the speakers are gone
    assert lt._devices_changed() is True
    windows["output"] = None
    assert lt._devices_changed() is True  # Windows can't say: restarted to be sure


def held_elsewhere():
    """Whether another thread would have to wait for PORTAUDIO now."""
    free = []

    def probe():
        free.append(lt.PORTAUDIO.acquire(blocking=False))
        if free[0]:
            lt.PORTAUDIO.release()

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(5)
    return not free[0]


def test_the_window_picks_checks_and_opens_a_device_in_one_hold_of_portaudio(monkeypatch):
    cable_out = {**LAPTOP_MIC, "name": "CABLE Output (VB-Audio Virtual Cable)"}
    devices = [{**SPEAKERS, "hostapi": 1}, {**CABLE, "hostapi": 1}, LAPTOP_MIC, cable_out]
    fake_devices(monkeypatch, devices, default_input=2)
    opened = []

    def stream(callback, device):  # no refresh can renumber the devices between the pick and this
        opened.append((device, held_elsewhere()))
        return Stream()

    lt.sd.RawOutputStream = lt.sd.RawInputStream = stream
    monkeypatch.setattr(lt, "stream_kwargs", lambda device, blocksize=lt.BLOCK: {"device": device})
    windows = {"output": SPEAKERS["name"], "input": LAPTOP_MIC["name"]}
    monkeypatch.setattr(lt, "windows_default", windows.get)
    player = lt.open_headphones(None)
    assert opened == [(0, True)] and player.stream.events == ["start"]
    mic = lt.open_input(None, lambda *args: None)
    assert opened[-1] == (2, True) and mic.events == []  # started by its owner
    windows.update(output=CABLE["name"], input=cable_out["name"])  # Windows fell back to the cable
    with pytest.raises(lt.Fatal, match="Прослушивание звучит только в наушниках, а выбран «CABLE Input"):
        lt.open_headphones(None)
    with pytest.raises(lt.Fatal, match="Выберите настоящий микрофон: сейчас программа слушает «CABLE Output"):
        lt.open_input(None, lambda *args: None)
    assert len(opened) == 2 and lt.query_devices() is devices


def test_a_microphone_opens_at_the_rate_asked_for_else_at_the_engines_own(monkeypatch):
    fake_devices(monkeypatch, [{**LAPTOP_MIC, "default_samplerate": 44100.0}], default_input=0)
    monkeypatch.setattr(lt, "windows_default", {}.get)
    made = []
    lt.sd.WasapiSettings = lambda auto_convert: "auto"
    lt.sd.RawInputStream = lambda **kwargs: made.append(kwargs) or Stream()
    assert lt.native_rate(None) == 44100
    lt.open_input(None, print, samplerate=44100)
    lt.open_input(None, print)
    assert [(m["device"], m["samplerate"], m["channels"]) for m in made] == [(0, 44100, 1), (0, lt.RATE, 1)]


def test_the_engine_refreshes_the_devices_before_picking_them(monkeypatch):
    order = []
    monkeypatch.setattr(lt, "refresh_devices", lambda: order.append("refresh"))
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: order.append(kind) or 1)
    monkeypatch.setattr(lt, "device_name", lambda index: "Microphone (USB)")
    monkeypatch.setattr(lt, "default_name", lambda kind: "CABLE Input (VB-Audio Virtual Cable)")  # stops the start
    refuse_audio(monkeypatch)
    with pytest.raises(lt.Fatal):
        asyncio.run(lt.Engine(device_args(), FakeSink()).run())
    assert order == ["refresh", "output", "input"]


LAPTOP_MIC = {"name": "Microphone Array (Realtek(R) Audio)", "max_input_channels": 2, "max_output_channels": 0,
              "hostapi": 1}


def fake_devices(monkeypatch, devices, default_input):
    """sounddevice listing `devices` (MME is host API 0, WASAPI 1) with PortAudio's default microphone from when it
    started."""
    apis = [{"name": "MME"}, {"name": "Windows WASAPI", "default_input_device": default_input,
                                "default_output_device": -1}]
    monkeypatch.setattr(lt, "sd", types.SimpleNamespace(
        query_hostapis=lambda index=None: apis if index is None else apis[index],
        query_devices=lambda index=None: devices if index is None else devices[index],
        default=types.SimpleNamespace(device=[-1, -1])))


def test_the_default_microphone_is_the_one_windows_has_now(monkeypatch):
    jabra = "Headset Microphone (Jabra)"
    devices = [{**LAPTOP_MIC, "hostapi": 0}, LAPTOP_MIC, {**LAPTOP_MIC, "name": jabra, "hostapi": 0},
               {**LAPTOP_MIC, "name": jabra}]
    fake_devices(monkeypatch, devices, default_input=1)
    windows = {"input": jabra}  # connected after the start, and made the default by Windows
    monkeypatch.setattr(lt, "windows_default", windows.get)
    assert lt.pick_device(None, "input") == 3  # its WASAPI entry
    engine = lt.Engine(argparse.Namespace(inp=None), FakeSink())
    engine.mic, opened = types.SimpleNamespace(close=lambda: None), []
    reopened = types.SimpleNamespace(start=lambda: None)
    assert engine._reopen_mic(lambda device: opened.append(device) or reopened) == jabra
    assert opened == [3]  # a lost microphone comes back as the default of now
    windows["input"] = "Microphone (USB)"  # not in PortAudio's list yet
    assert lt.pick_device(None, "input") == 1
    windows["input"] = None  # Windows can't say
    assert lt.pick_device(None, "input") == 1


def test_a_lost_microphone_never_comes_back_as_the_cable(monkeypatch):
    jabra, cable_out = "Headset Microphone (Jabra)", "CABLE Output (VB-Audio Virtual Cable)"
    fake_devices(monkeypatch, [LAPTOP_MIC, {**LAPTOP_MIC, "name": jabra}, {**LAPTOP_MIC, "name": cable_out}],
                 default_input=1)
    windows, unplugged, opened = {"input": cable_out}, [True], []  # the headset dropped, Windows fell back to the cable
    monkeypatch.setattr(lt, "windows_default", windows.get)

    def open_mic(device):
        opened.append(device)
        if device == 1 and unplugged[0]:
            raise RuntimeError("Error opening RawInputStream: Device unavailable")
        return types.SimpleNamespace(start=lambda: None, close=lambda: None)

    engine = lt.Engine(argparse.Namespace(inp=None), FakeSink())
    engine.mic, engine.mic_device = types.SimpleNamespace(close=lambda: None), 1  # the call started on the headset
    with pytest.raises(lt.Fatal, match="Windows переключил микрофон на «CABLE Output .+» — подключите настоящий"):
        engine._reopen_mic(open_mic)
    assert opened == [1]  # the headset tried, the cable never opened: my channel would hear our own English
    windows["input"] = LAPTOP_MIC["name"]  # a real microphone is the default now: the call goes on with it
    assert engine._reopen_mic(open_mic) == LAPTOP_MIC["name"] and opened == [1, 1, 0]
    unplugged[0] = False
    assert engine._reopen_mic(open_mic) == jabra and opened == [1, 1, 0, 1]  # the one the call started with first


def refuse_audio(monkeypatch):
    monkeypatch.setattr(lt, "Player", lambda device: pytest.fail("no audio device may open"))


def device_args(**changes):
    return argparse.Namespace(**{**dict(no_me=False, no_listen=True, out="CABLE Input", inp=None, monitor=False,
                                        monitor_device=None, passthrough=False), **changes})


def test_a_cable_microphone_is_refused_before_any_audio_opens(monkeypatch):
    names = {1: "CABLE Output (VB-Audio Virtual Cable)", 2: "CABLE Input (VB-Audio Virtual Cable)"}
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 1 if kind == "input" else 2)
    monkeypatch.setattr(lt, "device_name", names.get)
    monkeypatch.setattr(lt, "default_name", lambda kind: "Headphones (Realtek(R) Audio)")
    refuse_audio(monkeypatch)
    with pytest.raises(lt.Fatal, match="Выберите настоящий микрофон"):
        asyncio.run(lt.Engine(device_args(), FakeSink()).run())


def test_a_cable_default_output_is_refused(monkeypatch):
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 1)
    monkeypatch.setattr(lt, "device_name", lambda index: "Microphone (USB)")
    monkeypatch.setattr(lt, "default_name", lambda kind: "CABLE Input (VB-Audio Virtual Cable)")
    refuse_audio(monkeypatch)
    with pytest.raises(lt.Fatal, match="системные звуки"):
        asyncio.run(lt.Engine(device_args(), FakeSink()).run())


class PortAudioError(Exception):
    pass


class Stream:
    """A sounddevice stream that records what is done to it; `refuse` = "start" fails like a blocked device."""

    def __init__(self, refuse=None):
        self.refuse, self.events = refuse, []

    def start(self):
        if self.refuse == "start":
            raise PortAudioError("Error starting stream: Unanticipated host error [PaErrorCode -9999]")
        self.events.append("start")

    def stop(self):
        self.events.append("stop")

    def close(self):
        self.events.append("close")


def stream_player(device, stream=None):
    return types.SimpleNamespace(device=device, stream=stream or Stream(), gain=1.0)


def test_monitor_on_the_cable_is_skipped(monkeypatch):
    names = {1: "Microphone (USB)", 2: "CABLE Input (VB-Audio Virtual Cable)", 3: "CABLE In 16ch"}
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 1 if kind == "input" else 3 if name is None else 2)
    monkeypatch.setattr(lt, "device_name", names.get)
    monkeypatch.setattr(lt, "default_name", lambda kind: "Headphones")
    opened = []
    monkeypatch.setattr(lt, "Player", lambda device: opened.append(device) or stream_player(device))
    monkeypatch.setattr(lt, "stream_kwargs", lambda device, blocksize=lt.BLOCK: {})

    def no_mic(**kwargs):
        raise RuntimeError("stop before the microphone opens")

    monkeypatch.setattr(lt, "sd", types.SimpleNamespace(RawInputStream=no_mic, PortAudioError=PortAudioError))
    engine = lt.Engine(device_args(monitor=True), FakeSink())
    with pytest.raises(RuntimeError, match="stop before"):
        asyncio.run(engine.run())
    assert opened == [2] and engine.monitor is None  # the call's cable only, no second copy into it
    assert any("Слышать себя" in note for note in engine.sink.notes)


@pytest.mark.parametrize("refused", ["microphone", "microphone start", "cable start"])
def test_a_device_windows_refuses_is_a_russian_error_and_nothing_stays_open(monkeypatch, refused):
    message = ("Не удалось открыть «CABLE Input (VB-Audio Virtual Cable)»: проверьте" if refused == "cable start" else
               "Микрофон «Microphone (USB)» недоступен: разрешите приложениям доступ к микрофону")
    names = {1: "Microphone (USB)", 2: "CABLE Input (VB-Audio Virtual Cable)"}
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 1 if kind == "input" else 2)
    monkeypatch.setattr(lt, "device_name", names.get)
    monkeypatch.setattr(lt, "default_name", lambda kind: "Headphones (Realtek(R) Audio)")
    monkeypatch.setattr(lt, "stream_kwargs", lambda device, blocksize=lt.BLOCK: {})
    streams = []

    def open_mic(callback):
        if refused == "microphone":  # e.g. "Let desktop apps access your microphone" is off
            raise PortAudioError("Error opening RawInputStream: Unanticipated host error [PaErrorCode -9999]")
        streams.append(Stream("start" if refused == "microphone start" else None))
        return streams[-1]

    def player(device):
        streams.append(Stream("start" if refused == "cable start" else None))
        return stream_player(device, streams[-1])

    monkeypatch.setattr(lt, "sd", types.SimpleNamespace(RawInputStream=open_mic, PortAudioError=PortAudioError))
    monkeypatch.setattr(lt, "Player", player)
    sink = FakeSink()
    with pytest.raises(lt.Fatal) as error:
        asyncio.run(lt.Engine(device_args(), sink).run())
    assert str(error.value).startswith(message) and "PaErrorCode" not in str(error.value)
    assert any("PaErrorCode" in note for note in sink.notes)  # PortAudio's own words stay in the log
    assert streams and all(s.events[-1:] == ["close"] for s in streams)  # not one stream left open


def test_no_microphone_at_all_is_a_russian_error(monkeypatch):
    fake_devices(monkeypatch, [], default_input=-1)
    monkeypatch.setattr(lt, "windows_default", lambda kind: None)
    with pytest.raises(lt.Fatal, match="Windows не видит ни одного микрофона"):
        lt.pick_device(None, "input")


def test_a_monitor_windows_refuses_is_skipped_and_the_call_goes_on(monkeypatch):
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 5)
    monkeypatch.setattr(lt, "device_name", lambda index: "Headphones (Realtek(R) Audio)")
    monkeypatch.setattr(lt, "sd", types.SimpleNamespace(PortAudioError=PortAudioError))
    refused = Stream("start")
    monkeypatch.setattr(lt, "Player", lambda device: stream_player(device, refused))
    engine = lt.Engine(argparse.Namespace(monitor=False, monitor_device=None), FakeSink())
    engine.players = [FakePlayer()]
    engine.set_monitor(True)
    assert engine.monitor is None and len(engine.players) == 1 and refused.events == ["close"]
    assert "«Слышать себя» пропущено. Не удалось открыть «Headphones (Realtek(R) Audio)»" in engine.sink.notes[-1]


def test_monitor_switched_on_mid_call_refuses_the_cable(monkeypatch):
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 5)
    monkeypatch.setattr(lt, "device_name", lambda index: "CABLE Input (VB-Audio Virtual Cable)")
    refuse_audio(monkeypatch)
    engine = lt.Engine(argparse.Namespace(monitor=False, monitor_device=None), FakeSink())
    engine.players = [FakePlayer()]
    engine.set_monitor(True)
    assert engine.monitor is None and len(engine.players) == 1
    assert "Слышать себя" in engine.sink.notes[0]


# --- audio buffers, backlog, "I finished" ---------------------------------------------------------

def test_player_has_no_extra_block_and_reports_what_is_queued(monkeypatch):
    opened = []
    monkeypatch.setattr(lt, "sd", types.SimpleNamespace(
        query_hostapis=lambda index=None: [{"name": "MME"}], query_devices=lambda device: {"hostapi": 0},
        RawOutputStream=lambda **kwargs: opened.append(kwargs) or kwargs))
    player = lt.Player(4)
    assert opened[0]["blocksize"] == 0  # the device's own buffer: no extra 20 ms before the call hears it
    assert lt.stream_kwargs(4)["blocksize"] == lt.BLOCK  # the microphone keeps 20 ms blocks
    assert player.buffered == 0.0
    player.feed(bytes(lt.RATE // 10 * 2))
    assert player.buffered == pytest.approx(0.1)
    player.clear()
    assert player.buffered == 0.0


def test_backlog_is_what_the_call_player_has_queued():
    engine = lt.Engine(argparse.Namespace(), FakeSink())
    assert engine._backlog() == 0.0  # not running
    engine.players = [types.SimpleNamespace(buffered=0.7), types.SimpleNamespace(buffered=3.0)]  # + monitor
    assert engine._backlog() == 0.7


async def test_finish_turn_forces_the_finalizer_on_the_engine_loop():
    engine = lt.Engine(argparse.Namespace(), FakeSink())
    engine.finish_turn()  # not running: nothing to do
    forced = []
    engine.loop = asyncio.get_running_loop()
    engine.me_channel = lt.Channel("Я", "en", asyncio.Queue(), [], "me")
    engine.finish_turn()  # the STT channel has no finalizer
    engine.me_channel.finalizer = types.SimpleNamespace(
        force=lambda: forced.append(asyncio.get_running_loop() is engine.loop))
    await asyncio.to_thread(engine.finish_turn)  # from the hotkey thread
    await until(lambda: forced, what="force()")
    assert forced == [True]


def test_finish_turn_after_the_engine_stopped():
    engine = lt.Engine(argparse.Namespace(), FakeSink())
    engine.loop = asyncio.new_event_loop()
    engine.loop.close()
    engine.me_channel = types.SimpleNamespace(finalizer=types.SimpleNamespace(force=lambda: None))
    engine.finish_turn()  # no RuntimeError in the hotkey thread


# --- my voice in the Soniox engine: Soniox, Cartesia or Inworld ----------------------------------

class VoiceStandIn:
    """Records how the engine builds a voice (the constructor every provider's voice class shares)."""

    def __init__(self, api_key, voice, language, play, proxy, sink, on_first_audio=None, **kwargs):
        self.api_key, self.voice, self.language, self.kwargs = api_key, voice, language, kwargs


@pytest.fixture
def voices(monkeypatch):
    """Stand-ins for SonioxVoice, CartesiaVoice, InworldVoice and the phrase cache."""
    classes = {name: type(name, (VoiceStandIn,), {}) for name in ("SonioxVoice", "CartesiaVoice", "InworldVoice")}
    monkeypatch.setattr(soniox_engine, "SonioxVoice", classes["SonioxVoice"])
    classes["cartesia_default"] = None  # what cartesia_engine.default_voice finds in the library
    monkeypatch.setitem(sys.modules, "cartesia_engine", types.SimpleNamespace(
        CartesiaVoice=classes["CartesiaVoice"], default_voice=lambda key, proxy: classes["cartesia_default"]))
    monkeypatch.setitem(sys.modules, "inworld_engine", types.SimpleNamespace(
        InworldVoice=classes["InworldVoice"], KEY_ENV="INWORLD_API_KEY", DEFAULT_MODEL="inworld-tts-2-flash",
        DEFAULT_VOICE="Clive"))
    monkeypatch.setitem(sys.modules, "phrases", types.SimpleNamespace(
        PhraseCache=lambda cache_dir, key: ("cache", cache_dir.name, key)))
    for env in ("CARTESIA_API_KEY", "INWORLD_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    return classes


def voice_args(**changes):
    args = dict(voice="builtin", voice_id=None, voice_name=None, lang="en", speed=1.1, voice_provider="soniox",
                speed_boost=True, trim_silence=True, instant_phrases=True, inworld_model=None,
                delivery="balanced", match_rate=True)
    return argparse.Namespace(**{**args, **changes})


def make_voice(args):
    engine = lt.Engine(args, FakeSink())
    return engine, engine._make_voice("soniox-key", None, lt.LagMeter())


def test_soniox_voice_gets_every_lever(voices):
    engine, voice = make_voice(voice_args())
    assert type(voice).__name__ == "SonioxVoice"
    assert (voice.api_key, voice.voice, voice.language) == ("soniox-key", "Adrian", "en")
    assert voice.kwargs == {"speed": 1.1, "backlog": engine._backlog, "speed_boost": True, "trim": True,
                            "phrases": ("cache", "phrases", "soniox|tts-rt-v2|Adrian|en|1.1"),
                            "delivery": "balanced", "match_rate": True}


@pytest.mark.parametrize("provider", ["soniox", "cartesia", "inworld"])
def test_every_voice_provider_gets_the_delivery_and_match_rate(voices, monkeypatch, provider):
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-key")
    monkeypatch.setenv("INWORLD_API_KEY", "inworld-key")
    _, voice = make_voice(voice_args(voice_provider=provider, voice_name="Stock", delivery="natural", match_rate=False))
    assert (voice.kwargs["delivery"], voice.kwargs["match_rate"]) == ("natural", False)


def test_a_voice_asked_for_without_a_delivery_is_balanced_and_matches_my_pace(voices):
    args = voice_args()
    del args.delivery, args.match_rate  # a console namespace of an older caller
    _, voice = make_voice(args)
    assert (voice.kwargs["delivery"], voice.kwargs["match_rate"]) == ("balanced", True)


def test_cartesia_voice_speaks_my_clone(voices, monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-key")
    _, voice = make_voice(voice_args(voice_provider="cartesia", voice="clone", voice_id="c-1", speed_boost=False,
                                     trim_silence=False))
    assert type(voice).__name__ == "CartesiaVoice"
    assert (voice.api_key, voice.voice) == ("cartesia-key", "c-1")
    assert voice.kwargs["model"] == voice_clone.TTS_MODEL
    assert (voice.kwargs["speed_boost"], voice.kwargs["trim"]) == (False, False)
    assert voice.kwargs["phrases"][2] == f"cartesia|{voice_clone.TTS_MODEL}|c-1|en|1.1"


def test_inworld_voice_uses_its_model_and_default_voice(voices, monkeypatch):
    monkeypatch.setenv("INWORLD_API_KEY", "inworld-key")
    _, voice = make_voice(voice_args(voice_provider="inworld", inworld_model="inworld-tts-2", instant_phrases=False))
    assert type(voice).__name__ == "InworldVoice"
    assert (voice.api_key, voice.voice, voice.kwargs["model"]) == ("inworld-key", "Clive", "inworld-tts-2")
    assert voice.kwargs["phrases"] is None


def test_instant_phrases_only_for_english(voices):
    _, voice = make_voice(voice_args(lang="de"))
    assert voice.kwargs["phrases"] is None


@pytest.mark.parametrize("changes, message", [
    ({"voice_provider": "cartesia"}, "Нужен ключ Cartesia"),
    ({"voice_provider": "inworld"}, "Нужен ключ Inworld"),
    ({"voice": "clone"}, "Клон голоса для Soniox ещё не создан"),
])
def test_voice_without_a_key_or_a_clone_is_fatal(voices, changes, message):
    with pytest.raises(lt.Fatal, match=message):
        make_voice(voice_args(**changes))


def test_cartesia_without_a_chosen_voice_takes_a_male_english_one(voices, monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-key")
    with pytest.raises(lt.Fatal, match="Выберите голос Cartesia"):
        make_voice(voice_args(voice_provider="cartesia"))  # the library has no English voice
    voices["cartesia_default"] = "blake-id"
    _, voice = make_voice(voice_args(voice_provider="cartesia"))
    assert voice.voice == "blake-id"


def test_unknown_provider_falls_back_to_soniox(voices):
    assert lt.voice_provider(argparse.Namespace()) == "soniox"
    assert lt.voice_provider(argparse.Namespace(voice_provider="elevenlabs")) == "soniox"
    _, voice = make_voice(voice_args(voice_provider=None))
    assert type(voice).__name__ == "SonioxVoice"


def test_console_defaults_to_the_faster_voice_with_every_lever():
    args = lt.build_parser().parse_args([])
    assert args.speed == 1.0 and args.voice_provider == "soniox"
    assert (args.delivery, args.match_rate) == ("balanced", True)
    assert (args.speed_boost, args.trim_silence, args.instant_phrases, args.auto_finalize) == (True,) * 4
    args = lt.build_parser().parse_args(["--voice-provider", "cartesia", "--no-trim", "--no-auto-finalize",
                                         "--delivery", "fast", "--no-match-rate"])
    assert (args.voice_provider, args.trim_silence, args.auto_finalize) == ("cartesia", False, False)
    assert (args.delivery, args.match_rate) == ("fast", False)


@pytest.mark.parametrize("region, expected", [(None, ""), ("", ""), ("us", "us"), ("eu", "eu")])
def test_the_console_region_reaches_the_soniox_engine_module(monkeypatch, region, expected):
    seen = []
    monkeypatch.setattr(soniox_engine, "use_region", seen.append, raising=False)
    lt.use_soniox_region(region)
    assert seen == [expected]
    assert lt.build_parser().parse_args(["--region", "eu"]).region == "eu"
    assert lt.build_parser().parse_args([]).region is None


def test_a_region_is_ignored_by_an_engine_module_without_regions(monkeypatch):
    monkeypatch.delattr(soniox_engine, "use_region", raising=False)
    lt.use_soniox_region("eu")  # nothing to switch: no error either


# --- tools/latency_test.py: what the other person hears, clause by clause ------------------------------

def tone(seconds, level=8000):
    return np.full(int(round(seconds * lt.RATE)), level, "<i2").tobytes()


def quiet(seconds):
    return bytes(int(round(seconds * lt.RATE)) * 2)


def write_wav(path, rate, width, channels, frames):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


def test_wav_is_mixed_to_mono_and_resampled(tmp_path):
    stereo = np.tile(np.array([1000, 3000], "<i2"), 4800).tobytes()  # 0.1 s at 48 kHz, L=1000 R=3000
    pcm = np.frombuffer(latency_test.read_wav(write_wav(tmp_path / "a.wav", 48000, 2, 2, stereo)), "<i2")
    assert len(pcm) == 2400 and abs(int(pcm.mean()) - 2000) <= 1
    eight = latency_test.read_wav(write_wav(tmp_path / "b.wav", 24000, 1, 1, bytes([192]) * 240))
    assert set(np.frombuffer(eight, "<i2")) == {16383}  # 8-bit is unsigned: 192 is half way up
    same = tone(0.01)
    assert latency_test.read_wav(write_wav(tmp_path / "c.wav", lt.RATE, 2, 1, same)) == same


def test_silence_around_a_clip():
    lead, tail = latency_test.silence(quiet(0.08) + tone(0.2) + quiet(0.14))
    assert lead == pytest.approx(0.08, abs=0.001) and tail == pytest.approx(0.14, abs=0.001)
    assert latency_test.silence(quiet(0.1)) == pytest.approx((0.1, 0.0))


@pytest.fixture
def timeline(monkeypatch):
    clock = types.SimpleNamespace(t=10.0)
    monkeypatch.setattr(latency_test, "time", types.SimpleNamespace(monotonic=lambda: clock.t))
    tl = latency_test.Timeline()
    tl.voice = types.SimpleNamespace(order=collections.deque(["s1"]))
    tl.clock = clock
    return tl


def test_clause_table_from_the_trace_hook(timeline):
    tl, clock = timeline, timeline.clock
    tl.trace("open", "s1")
    tl.trace("text", "s1", text="Hello,", end=False)
    clock.t = 10.2
    tl.trace("text", "s1", text="", end=True)
    clock.t = 10.6
    tl.trace("first_audio", "s1", pcm=b"")
    tl.play(quiet(0.08) + tone(0.2) + quiet(0.14))
    tl.voice.order = collections.deque(["s2"])  # s1 finished speaking
    clock.t = 10.7
    tl.trace("text", "s2", text=" World.", end=True)
    clock.t = 10.9
    tl.trace("first_audio", "s2", pcm=b"")  # s1 still plays until 11.02: 0.12 s queued
    tl.play(quiet(0.05) + tone(0.1))
    first, second = tl.rows(10.0)
    assert first["text"] == "Hello," and second["text"] == "World."
    expected = [{"final": 0.0, "end": 0.2, "first_audio": 0.6, "audible": 0.68, "wait": 0.48, "backlog": 0.0,
                 "lead": 0.08, "tail": 0.14},
                {"final": 0.7, "end": 0.7, "first_audio": 0.9, "audible": 1.07, "wait": 0.37, "backlog": 0.12,
                 "lead": 0.05, "tail": 0.0}]
    for row, want in zip((first, second), expected):
        assert {k: round(row[k], 3) for k in want} == pytest.approx(want, abs=0.002)
    sink = latency_test.Probe(tl)
    result = latency_test.metrics(tl, sink, 10.0, 10.5)
    assert result["first_audible"] == pytest.approx(0.68, abs=0.002)
    assert result["last_word"] == pytest.approx(11.17 - 10.5, abs=0.002)
    assert result["wait"] == pytest.approx(0.425, abs=0.002)  # median over the clauses


def test_medians_skip_what_a_run_did_not_measure():
    runs = [{"first_audible": 2.0, "wait": None}, {"first_audible": 3.0, "wait": 0.4}, {"first_audible": 2.5}]
    result = latency_test.medians(runs)
    assert result["first_audible"] == 2.5 and result["wait"] == 0.4 and result["tail"] is None


def test_done_presses_the_finalizer_when_there_is_one():
    ch = lt.Channel("Я", "en", None, [], "me")
    assert latency_test.press_done(ch) is False
    forced = []
    ch.finalizer = types.SimpleNamespace(force=lambda: forced.append(1))
    assert latency_test.press_done(ch) is True and forced == [1]


@pytest.mark.parametrize("provider", ["soniox", "cartesia", "inworld"])
def test_latency_voice_is_built_like_the_app(api, voices, timeline, monkeypatch, provider):
    """The tool measures the voice the call hears: the app's levers, instant phrases and model."""
    monkeypatch.setenv("CARTESIA_API_KEY", "c-key")
    monkeypatch.setenv("INWORLD_API_KEY", "i-key")
    settings = {"speed": 1.2, "speed_boost": False, "trim_silence": False, "instant_phrases": True,
                "delivery": "natural", "match_rate": False,
                "voice_provider": provider, app.BUILTIN_FIELDS[provider]: "v-1"}
    api._settings.update(settings)
    in_app = lt.Engine(api._args(), FakeSink())._make_voice("s-key", None, lt.LagMeter())
    args = latency_test.build_parser().parse_args(["--provider", provider, "--speed", "1.2"])
    voice = latency_test.make_voice(args, {"soniox": "s-key"}, "v-1", None, FakeSink(), timeline, settings)
    assert (type(voice), voice.api_key, voice.voice, voice.language) == (
        type(in_app), in_app.api_key, in_app.voice, in_app.language)
    assert voice.kwargs == {**in_app.kwargs, "backlog": timeline.backlog}  # the simulated call's queue
    assert voice.kwargs["phrases"] is not None
    assert (voice.kwargs["delivery"], voice.kwargs["match_rate"]) == ("natural", False)


def test_latency_delivery_is_the_apps_unless_given(voices, timeline):
    parse = latency_test.build_parser().parse_args
    installed = {"delivery": "natural", "match_rate": False}
    assert latency_test.delivery_of(parse([]), installed) == "natural"
    assert latency_test.delivery_of(parse(["--delivery", "fast"]), installed) == "fast"
    assert latency_test.delivery_of(parse([]), {}) == "balanced"
    assert latency_test.delivery_of(parse([]), {"delivery": "sudden"}) == "balanced"  # a hand-edited settings.json
    args = parse(["--provider", "soniox", "--delivery", "fast", "--match-rate"])
    voice = latency_test.make_voice(args, {"soniox": "s-key"}, "v-1", None, FakeSink(), timeline, installed)
    assert (voice.kwargs["delivery"], voice.kwargs["match_rate"]) == ("fast", True)
    assert latency_test.delivery_note(args, installed) == "подача: fast, копирует мой темп · "
    assert latency_test.delivery_note(parse([]), installed) == "подача: natural, свой темп · "
    assert latency_test.delivery_note(parse(["--engine", "openai"]), installed) == ""


def test_latency_region_is_the_apps_unless_given(monkeypatch):
    parse = latency_test.build_parser().parse_args
    assert latency_test.region_of(parse([]), {"soniox_region": "eu"}) == "eu"
    assert latency_test.region_of(parse(["--region", "us"]), {"soniox_region": "eu"}) == "us"
    assert latency_test.region_of(parse([]), {"soniox_region": ""}) == "us"
    assert latency_test.region_of(parse([]), {}) == "us"
    seen = []
    monkeypatch.setattr(soniox_engine, "use_region", seen.append, raising=False)  # the engine's own switch
    latency_test.use_region("eu")
    latency_test.use_region("us")
    assert seen == ["eu", "us"]


def test_latency_region_without_an_engine_switch_sets_the_eu_hosts_by_hand(monkeypatch):
    monkeypatch.delattr(soniox_engine, "use_region", raising=False)
    monkeypatch.setattr(soniox_engine, "STT_URL", "wss://us-stt")
    monkeypatch.setattr(soniox_engine, "TTS_URL", "wss://us-tts")
    latency_test.use_region("us")
    assert (soniox_engine.STT_URL, soniox_engine.TTS_URL) == ("wss://us-stt", "wss://us-tts")
    latency_test.use_region("eu")
    assert (soniox_engine.STT_URL, soniox_engine.TTS_URL) == (netcheck.SONIOX_EU_STT, netcheck.SONIOX_EU_TTS)


def test_latency_levers_are_the_apps_unless_given():
    parse, settings = latency_test.build_parser().parse_args, {"speed_boost": False, "auto_finalize": False}
    levers = (("boost", "speed_boost"), ("trim", "trim_silence"), ("phrases", "instant_phrases"),
              ("finalize", "auto_finalize"))
    args = parse([])
    assert [latency_test.lever(getattr(args, a), settings, k) for a, k in levers] == [False, True, True, False]
    args = parse(["--boost", "--no-trim", "--no-phrases", "--no-finalize"])
    assert [latency_test.lever(getattr(args, a), settings, k) for a, k in levers] == [True, False, False, False]
    keys = {"soniox": "s-key", "cartesia": "c-key", "inworld": "i-key", "openai": "o-key"}
    assert latency_test.rtt_probes(parse(["--provider", "cartesia"]), keys) == [
        ("Soniox STT", soniox_engine.STT_URL, None), ("Cartesia TTS", voice_clone.TTS_URL, {"X-API-Key": "c-key"})]


@pytest.mark.parametrize("provider, settings, expected", [
    ("soniox", {}, "Adrian"),
    ("soniox", {"voice": "clone", "soniox_voice_id": "s-1"}, "s-1"),
    ("inworld", {"voice": "clone", "soniox_voice_id": "s-1"}, "Clive"),  # no Inworld clone: its default voice
    ("cartesia", {"cartesia_builtin_id": "c-katie"}, "c-katie"),
    ("cartesia", {}, None),  # nothing to speak with: the tool says so
])
def test_latency_voice_comes_from_the_installed_app(voices, provider, settings, expected):
    args = latency_test.build_parser().parse_args(["--provider", provider])
    assert latency_test.pick_voice(args, settings) == expected


def test_installed_settings_follow_the_apps_speed_migration(monkeypatch, tmp_path):
    monkeypatch.setattr(latency_test, "INSTALLED", tmp_path)
    assert latency_test.installed_settings() == {}
    for saved, speed in [({"speed": 1.1, "voice": "clone"}, 1.0),  # the old default: settings v3 lowers it
                         ({"speed": 1.1, "settings_version": 3}, 1.1),  # picked since
                         ({"speed": 1.0}, 1.0),
                         ({"speed": 1.3, "settings_version": 2}, 1.3)]:
        (tmp_path / "settings.json").write_text(json.dumps(saved), encoding="utf-8")
        assert latency_test.installed_settings()["speed"] == speed, saved


# --- docs and scripts ------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def test_the_readme_promises_the_python_the_scripts_require():
    """On 3.10 a slow websocket handshake raises asyncio.TimeoutError, no OSError there: the call would end."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.findall(r"Python (\d\.\d+)\+", readme) == ["3.11"]
    for script in ("start.bat", "start_console.bat", "build_exe.bat"):
        assert "sys.exit(sys.version_info < (3, 11))" in (ROOT / script).read_text(encoding="utf-8"), script


def test_the_documented_latency_baseline_is_the_current_measurement():
    """Agents judge a regression by CLAUDE.md: it quotes latency_test's own metrics, measured after the rework."""
    status = " ".join((ROOT / "CLAUDE.md").read_text(encoding="utf-8").split("## Status", 1)[1].split())
    labels = {key: label for key, label, _ in latency_test.METRICS}
    for key, seconds in (("first_audible", "+1.9 s"), ("last_word", "+2.9 s")):
        assert f"«{labels[key]}» {seconds}" in status, key


def test_the_window_uses_portaudio_only_through_the_engine_lock():
    """PortAudio is re-initialised before every call; a query or a stream opened meanwhile fails or crashes.
    app.py reaches audio devices only through live_translator's locked helpers."""
    assert not hasattr(app, "sd")
