"""Regression tests from the global audit: lifecycle races, reconnects, proxies, settings, clones."""
import argparse
import asyncio
import base64
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
import types
import urllib.request
from pathlib import Path

import numpy as np
import pytest

import app
import live_translator as lt
import soniox_engine
import voice_clone
from mocks import FakeSink, free_port, read_until, stop, until
from test_soniox_stt import ACK, channel
from test_soniox_tts import KEY, configs, make_voice, run_voice, terminated
from test_units import CABLE, SPEAKERS


@pytest.fixture(autouse=True)
def _portaudio_is_never_restarted(monkeypatch):
    """No test re-initialises the real PortAudio (the engine does it before every call)."""
    monkeypatch.setattr(lt, "refresh_devices", lambda: False)


# --- Api lifecycle with a real engine thread -------------------------------------------

class StubEngine:
    made = []

    def __init__(self, args, sink):
        self.args = args
        StubEngine.made.append(self)

    def set_muted(self, muted):
        pass

    def set_paused(self, paused):
        pass

    async def run(self):
        await asyncio.Event().wait()


@pytest.fixture
def live_api(monkeypatch, tmp_path):
    """app.Api whose engine thread runs a do-nothing engine."""
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(app, "RECORDS_DIR", tmp_path / "records")
    monkeypatch.setattr(lt, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(lt, "start_hotkey", lambda callback, **kw: False)
    monkeypatch.setattr(lt, "Engine", StubEngine)
    monkeypatch.setattr(lt, "query_devices", lambda: [SPEAKERS, CABLE])
    for name in app.KEY_ENVS.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(soniox_engine.KEY_ENV, "soniox-key")
    StubEngine.made = []
    api = app.Api(argparse.Namespace(proxy=None))
    yield api
    api._stop_engine()


def running_events(api, since):
    return [e["value"] for e in api._bus.since(since) if e["type"] == "running"]


def test_restart_mid_call_does_not_report_a_stop(live_api):
    assert live_api.start()["ok"]
    seq = live_api._bus.seq
    assert live_api.save_settings({"me_lang": "en"}) == {"restarted": True, "pending": False}
    assert running_events(live_api, seq) == [True]  # the UI would stop the call on a False here
    assert live_api._running() and len(StubEngine.made) == 2
    assert live_api.poll(seq)["running"] is True


def test_poll_reports_running_while_the_engine_is_swapped(live_api):
    assert live_api.start()["ok"]
    seen = []
    stop_engine = live_api._stop_engine

    def slow_stop():
        stop_engine()
        seen.append(live_api.poll(0)["running"])  # old thread gone, new one not started yet

    live_api._stop_engine = slow_stop
    live_api.save_settings({"speed": 1.2})
    assert seen == [True]


def test_concurrent_stops_count_the_session_once(live_api):
    assert live_api.start()["ok"]
    live_api._started = time.time() - 600
    barrier = threading.Barrier(2)

    def stop():
        barrier.wait()
        live_api.stop()

    threads = [threading.Thread(target=stop) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert live_api._settings["usage_seconds"] == pytest.approx(1200, abs=5)  # 600 s x 2 channels, once
    assert json.loads(app.SETTINGS_FILE.read_text(encoding="utf-8"))["usage_seconds"] == pytest.approx(1200, abs=5)
    assert not live_api._running()


def test_stop_during_a_restart_leaves_nothing_running(live_api):
    assert live_api.start()["ok"]
    stop_engine, stopper = live_api._stop_engine, []

    def stop_then_press_stop():
        stop_engine()
        if not stopper:  # the user presses ■ while the engine is being swapped
            stopper.append(threading.Thread(target=live_api.stop))
            stopper[0].start()

    live_api._stop_engine = stop_then_press_stop
    live_api.save_settings({"voice_name": "Daniel"})
    stopper[0].join(5)
    assert not live_api._running() and live_api._started is None


def test_a_state_read_during_a_restart_never_switches_the_engine(live_api, monkeypatch):
    """A Soniox key saved mid-call, then the overlay opens (get_state) while a settings restart swaps the engine."""
    monkeypatch.delenv(soniox_engine.KEY_ENV)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert live_api.start()["engine"] == "openai"
    monkeypatch.setenv(soniox_engine.KEY_ENV, "soniox-key")
    stop_engine, notices = live_api._stop_engine, []

    def stop_then_read_the_state():
        stop_engine()
        if not notices:
            notices.append(live_api._auto_engine())  # the old engine is gone, the new one not started yet

    live_api._stop_engine = stop_then_read_the_state
    assert live_api.save_settings({"speed": 1.2})["restarted"]
    assert notices == [None] and live_api._settings["engine"] == "openai"
    assert StubEngine.made[1].args.engine == "openai"


def test_start_while_the_last_call_is_still_closing(live_api, monkeypatch):
    """▶ right after ■, while the old engine still closes a Bluetooth headset: never «started» with no call."""
    monkeypatch.setattr(app, "STOP_WAIT", 0.1)
    release = threading.Event()

    class SlowToClose(StubEngine):
        async def run(self):
            try:
                await asyncio.Event().wait()
            finally:
                release.wait(5)  # a WASAPI stop that hangs

    monkeypatch.setattr(lt, "Engine", SlowToClose)
    assert live_api.start()["ok"]
    live_api.stop()
    assert live_api._running() and live_api._started is None
    assert live_api.start() == {"ok": False, "error": "stopping"}
    release.set()
    result = live_api.start()  # the old engine is gone by now
    assert result["ok"] and result["started"] is not None and len(StubEngine.made) == 2


def test_a_setting_changed_mid_sentence_waits_for_a_pause(live_api, monkeypatch):
    """The speed slider mid-call: the English being spoken is not cut off mid-word, the restart comes in a pause."""
    monkeypatch.setattr(app, "RESTART_QUIET", 1.0)
    assert live_api.start()["ok"]
    speaking = types.SimpleNamespace(busy=True)
    live_api._engine.players = [speaking]
    seq = live_api._bus.seq
    assert live_api.save_settings({"speed": 1.2}) == {"restarted": False, "pending": True}
    assert live_api.save_settings({"speed": 1.25})["pending"]  # one restart applies both
    time.sleep(0.2)
    assert len(StubEngine.made) == 1
    live_api._bus.level(0.4, 0.0)  # the English is over, but I go on talking
    speaking.busy = False
    time.sleep(0.2)
    assert len(StubEngine.made) == 1
    live_api._restarter.join(5)
    assert len(StubEngine.made) == 2 and StubEngine.made[1].args.speed == 1.25
    assert [e["type"] for e in live_api._bus.since(seq)].count("restarted") == 1
    assert live_api.poll(seq)["running"] is True


@pytest.mark.parametrize("key, flag", [("me_on", "no_me"), ("listen_on", "no_listen")])
@pytest.mark.parametrize("on", [False, True])
def test_a_side_switched_on_or_off_mid_sentence_applies_at_once(live_api, key, flag, on):
    """«Я» unticked while I go on talking to someone in the room: the call must not hear that translated. A side
    switched on mid-sentence: translated from then on, not only after the next pause."""
    live_api._settings[key] = not on
    assert live_api.start()["ok"]
    live_api._engine.players = [types.SimpleNamespace(busy=True)]  # English still playing, and both sides talk
    live_api._bus.level(0.4, 0.4)
    assert live_api.save_settings({key: on}) == {"restarted": True, "pending": False}
    assert len(StubEngine.made) == 2 and getattr(StubEngine.made[1].args, flag) is not on


def test_a_pending_restart_is_dropped_when_the_call_stops(live_api):
    assert live_api.start()["ok"]
    live_api._engine.players = [types.SimpleNamespace(busy=True)]
    assert live_api.save_settings({"speed": 1.2})["pending"]
    live_api.stop()
    live_api._restarter.join(5)
    assert len(StubEngine.made) == 1 and not live_api._running()


def test_the_openai_engine_is_not_restarted_for_settings_it_does_not_use(live_api, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    live_api._settings.update(engine="openai", engine_auto=False)
    assert live_api.start()["ok"]
    result = live_api.save_settings({"keywords": ["Сурен = Suren"], "context": "Собеседование", "diarize": False})
    assert result == {"restarted": False, "pending": False} and len(StubEngine.made) == 1


# --- settings.json ------------------------------------------------------------------------

def test_damaged_settings_are_kept_aside(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "settings.json")
    app.SETTINGS_FILE.write_text('{"soniox_voice_id": "v1"}}', encoding="utf-8")
    assert app.load_settings() == app.DEFAULTS
    assert (tmp_path / "settings.json.bad").read_text(encoding="utf-8") == '{"soniox_voice_id": "v1"}}'


def test_settings_are_replaced_atomically(live_api):
    live_api.save_settings({"font": 20})
    assert json.loads(app.SETTINGS_FILE.read_text(encoding="utf-8"))["font"] == 20
    assert not app.SETTINGS_FILE.with_suffix(".json.tmp").exists()


@pytest.mark.parametrize("saved, speed", [
    ({"speed": 1.0}, 1.1),                          # the old default: now a little faster
    ({"speed": 1.2}, 1.2),                          # chosen by hand: kept
    ({"speed": 1.0, "settings_version": 2}, 1.0),   # 1.0 chosen after the update: kept
    ({}, 1.1),
])
def test_old_default_speed_is_migrated_once(monkeypatch, tmp_path, saved, speed):
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "settings.json")
    app.SETTINGS_FILE.write_text(json.dumps(saved), encoding="utf-8")
    settings = app.load_settings()
    assert settings["speed"] == speed and settings["settings_version"] == 2


# --- voice clone, recording ------------------------------------------------------------

def test_new_clone_deletes_the_replaced_one(live_api, http_server, monkeypatch, tmp_path):
    monkeypatch.setattr(lt, "APP_DIR", tmp_path)
    monkeypatch.setattr(app, "SAMPLE_FILE", tmp_path / "voice_sample")
    (tmp_path / "voice_sample.wav").write_bytes(b"RIFF....WAVE")
    live_api._settings["soniox_voice_id"] = "old-voice"
    http_server.routes[("POST", "/v1/voices")] = (201, {"id": "new-voice"})
    http_server.routes[("GET", "/v1/voices/new-voice")] = (200, {"models": [{"model": "tts-rt-v2", "status": "ready"}]})
    http_server.routes[("DELETE", "/v1/voices/old-voice")] = (204, b"")
    assert live_api.create_clone() == {"ok": True, "provider": "soniox"}
    assert live_api._settings["soniox_voice_id"] == "new-voice" and live_api._settings["voice"] == "clone"
    assert [(r.method, r.path) for r in http_server.requests][-1] == ("DELETE", "/v1/voices/old-voice")


def test_failed_clone_is_deleted_right_away(live_api, http_server, monkeypatch, tmp_path):
    monkeypatch.setattr(lt, "APP_DIR", tmp_path)
    monkeypatch.setattr(app, "SAMPLE_FILE", tmp_path / "voice_sample")
    (tmp_path / "voice_sample.wav").write_bytes(b"RIFF....WAVE")
    http_server.routes[("POST", "/v1/voices")] = (201, {"id": "bad-voice"})
    http_server.routes[("GET", "/v1/voices/bad-voice")] = (
        200, {"models": [{"model": "tts-rt-v2", "status": "failed", "error_type": "invalid_audio"}]})
    http_server.routes[("DELETE", "/v1/voices/bad-voice")] = (204, b"")
    result = live_api.create_clone()
    assert result["ok"] is False and "invalid_audio" in result["error"]
    assert ("DELETE", "/v1/voices/bad-voice") in [(r.method, r.path) for r in http_server.requests]
    assert live_api._settings["soniox_voice_id"] is None


def test_recording_reports_a_missing_microphone(live_api, monkeypatch):
    def missing(name, kind):
        raise lt.Fatal("Аудиоустройство не найдено: 'Headset'")

    monkeypatch.setattr(lt, "pick_device", missing)
    result = live_api.record_sample(1)
    assert result["ok"] is False and "Headset" in result["error"]


def test_preview_errors_come_back_as_a_message(live_api, monkeypatch):
    async def offline(*args, **kwargs):
        raise voice_clone.CloneError("Нет связи с Soniox")

    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 3)
    monkeypatch.setattr(lt, "device_name", lambda index: "Headphones (Realtek(R) Audio)")
    monkeypatch.setattr(soniox_engine, "speak_once", offline)
    assert live_api.preview_voice("Adrian") == {"ok": False, "error": "Нет связи с Soniox"}


# --- voice providers of the Soniox engine: Cartesia and Inworld next to Soniox TTS ------------------

@pytest.fixture
def sample(monkeypatch, tmp_path):
    monkeypatch.setattr(lt, "APP_DIR", tmp_path)
    monkeypatch.setattr(app, "SAMPLE_FILE", tmp_path / "voice_sample")
    (tmp_path / "voice_sample.wav").write_bytes(b"RIFF....WAVE")


@pytest.fixture
def inworld(monkeypatch):
    """A stand-in inworld_engine module that records its calls."""
    calls = []
    module = types.SimpleNamespace(
        calls=calls, KEY_ENV="INWORLD_API_KEY",
        create_voice=lambda key, audio, proxy, filename="voice.wav": calls.append(("create", key, filename)) or "iw-2",
        delete_voice=lambda key, voice_id, proxy: calls.append(("delete", key, voice_id)),
        list_voices=lambda key, proxy: [{"name": "Clive", "gender": "male", "description": "British"}],
        speak_once=lambda key, voice, language, text, proxy, **kw: calls.append(("speak", voice, kw)) or b"\0\0")
    monkeypatch.setitem(sys.modules, "inworld_engine", module)
    monkeypatch.setenv("INWORLD_API_KEY", "inworld-key")
    return module


def test_cartesia_clone_for_the_soniox_engine_replaces_the_old_one(live_api, http_server, sample, monkeypatch):
    monkeypatch.setenv(voice_clone.KEY_ENV, "cartesia-key")
    live_api._settings.update(voice_provider="cartesia", cartesia_voice_id="old-c")
    http_server.routes[("POST", "/voices/clone")] = (200, {"id": "new-c"})
    http_server.routes[("DELETE", "/voices/old-c")] = (204, b"")
    assert live_api.create_clone() == {"ok": True, "provider": "cartesia"}
    assert live_api._settings["cartesia_voice_id"] == "new-c" and live_api._settings["voice"] == "clone"
    assert [(r.method, r.path) for r in http_server.requests] == [("POST", "/voices/clone"),
                                                                ("DELETE", "/voices/old-c")]
    assert http_server.requests[0].headers["X-API-Key"] == "cartesia-key"
    assert live_api._settings["soniox_voice_id"] is None  # the Soniox clone is not touched


def test_inworld_clone_is_ready_at_once(live_api, sample, inworld):
    live_api._settings.update(voice_provider="inworld", inworld_voice_id="iw-1")
    assert live_api.create_clone() == {"ok": True, "provider": "inworld"}
    assert live_api._settings["inworld_voice_id"] == "iw-2"
    assert inworld.calls == [("create", "inworld-key", "voice_sample.wav"), ("delete", "inworld-key", "iw-1")]


def test_clone_needs_the_key_of_the_chosen_provider(live_api, sample):
    live_api._settings["voice_provider"] = "inworld"
    assert live_api.create_clone() == {"ok": False, "error": "Нужен ключ Inworld (⚙ Настройки)."}


class PreviewStream(contextlib.nullcontext):
    def start(self):
        pass

    def close(self):
        pass


class PreviewPlayer:
    made = []

    def __init__(self, device):
        self.device, self.fed, self.gain = device, [], 1.0
        self.stream = PreviewStream()
        PreviewPlayer.made.append(self)

    def feed(self, pcm):
        self.fed.append(pcm)


@pytest.fixture
def headphones(monkeypatch):
    """The preview device: headphones unless a test says otherwise; no real audio, no waiting."""
    names = {3: "Headphones (Realtek(R) Audio)"}
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 3)
    monkeypatch.setattr(lt, "device_name", lambda index: names[index])
    monkeypatch.setattr(lt, "Player", PreviewPlayer)
    monkeypatch.setattr(app.time, "sleep", lambda seconds: None)
    PreviewPlayer.made = []
    return names


def test_preview_speaks_the_chosen_soniox_voice_at_my_speed(live_api, headphones, monkeypatch):
    calls = []

    async def speak(key, voice, language, text, proxy, **kw):
        calls.append((key, voice, language, kw))
        return b"\1\0"

    monkeypatch.setattr(soniox_engine, "speak_once", speak)
    assert live_api.preview_voice() == {"ok": True}
    assert calls == [("soniox-key", "Adrian", "en", {"speed": 1.1})]
    assert [(p.device, p.fed) for p in PreviewPlayer.made] == [(3, [b"\1\0"])]


def test_preview_of_an_inworld_voice(live_api, headphones, inworld):
    live_api._settings.update(voice_provider="inworld", inworld_model="inworld-tts-2")
    assert live_api.preview_voice() == {"ok": True}
    assert live_api.preview_voice("Olivia") == {"ok": True}  # ▶ next to a voice in the list
    assert [c for c in inworld.calls if c[0] == "speak"] == [
        ("speak", "Clive", {"model": "inworld-tts-2", "speed": 1.1}),
        ("speak", "Olivia", {"model": "inworld-tts-2", "speed": 1.1})]


def test_preview_of_a_cartesia_voice_is_the_voice_of_the_call(live_api, headphones, ws_server, monkeypatch):
    import cartesia_engine
    monkeypatch.setenv(voice_clone.KEY_ENV, "cartesia-key")
    monkeypatch.setattr(cartesia_engine, "default_voice", lambda key, proxy: "c-blake")  # what _make_voice takes
    msgs = []

    async def handler(ws):
        msg = json.loads(await ws.recv())
        msgs.append(msg)
        await ws.send(json.dumps({"type": "chunk", "context_id": msg["context_id"], "data": "AQA="}))
        await ws.send(json.dumps({"type": "done", "context_id": msg["context_id"]}))
        await ws.wait_closed()

    ws_server.handler = handler
    live_api._settings["voice_provider"] = "cartesia"
    assert live_api.preview_voice() == {"ok": True}  # no voice picked yet: the one the call would use
    live_api._settings["cartesia_builtin_id"] = "c-katie"
    assert live_api.preview_voice() == {"ok": True}
    live_api._settings.update(voice="clone", cartesia_voice_id="c-mine")
    assert live_api.preview_voice() == {"ok": True}
    assert [(m["voice"], m["generation_config"]) for m in msgs] == [
        ({"mode": "id", "id": voice}, {"speed": 1.1}) for voice in ("c-blake", "c-katie", "c-mine")]
    assert [p.fed for p in PreviewPlayer.made] == [[b"\1\0"]] * 3


def test_preview_of_the_openai_engine_clone(live_api, headphones, monkeypatch):
    monkeypatch.setenv(voice_clone.KEY_ENV, "cartesia-key")
    calls = []

    async def speak(key, voice, language, text, proxy):  # voice_clone.CloneVoice has no speed either
        calls.append(voice)
        return b"\1\0"

    monkeypatch.setattr(voice_clone, "speak_once", speak)
    live_api._settings.update(engine="openai", voice="clone", cartesia_voice_id="c-mine")
    assert live_api.preview_voice() == {"ok": True}
    assert calls == ["c-mine"]


def test_preview_never_plays_into_the_call(live_api, headphones, monkeypatch):
    headphones[3] = "CABLE Input (VB-Audio Virtual Cable)"  # "listen" resolves to the cable
    monkeypatch.setattr(soniox_engine, "speak_once", lambda *a, **kw: pytest.fail("nothing may be synthesized"))
    result = live_api.preview_voice("Adrian")
    assert result["ok"] is False and "только в наушниках" in result["error"]
    assert PreviewPlayer.made == []


def test_preview_finds_the_headphones_again_after_the_synthesis(live_api, headphones, monkeypatch):
    """A headset plugged in while the phrase is synthesized moves the device indices once get_state re-reads them:
    the index looked up before may now be the cable."""
    index = [3]
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: index[0])

    def speak(*args, **kwargs):
        headphones.update({3: "CABLE Input (VB-Audio Virtual Cable)", 5: "Headphones (Realtek(R) Audio)"})
        index[0] = 5
        return b"\1\0"

    monkeypatch.setattr(soniox_engine, "speak_once", speak)
    assert live_api.preview_voice("Adrian") == {"ok": True}
    assert [p.device for p in PreviewPlayer.made] == [5]


def test_voice_list_comes_from_the_chosen_provider(live_api, monkeypatch):
    monkeypatch.setenv(voice_clone.KEY_ENV, "cartesia-key")
    voices = [{"name": "Katie", "gender": "feminine", "description": "Friendly", "id": "c-katie"}]
    monkeypatch.setitem(sys.modules, "cartesia_engine", types.SimpleNamespace(
        list_voices=lambda key, proxy: voices if key == "cartesia-key" else []))
    live_api._settings["voice_provider"] = "cartesia"
    assert live_api.list_voices() == {"ok": True, "provider": "cartesia", "voices": voices}
    live_api._settings["voice_provider"] = "inworld"
    assert live_api.list_voices() == {"ok": False, "error": "Нужен ключ Inworld (⚙ Настройки)."}


def test_session_cost_includes_the_chosen_voice(live_api):
    live_api._settings["voice_provider"] = "cartesia"
    assert live_api.start()["ok"]
    live_api._started = time.time() - 600
    live_api.stop()
    assert live_api._settings["usage_cost"] == pytest.approx(10 * (2 * 0.002 + 0.0225), abs=0.002)


# --- closing the window --------------------------------------------------------------------

@pytest.fixture
def slow_notes(monkeypatch):
    """AI notes whose Responses API call answers only once released."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    release = threading.Event()

    def summarize(key, text, proxy):
        release.wait(5)
        return {"title": "Собеседование", "summary": "Кратко."}

    monkeypatch.setattr(app.meeting_notes, "summarize", summarize)
    return release


def test_closing_the_window_ends_the_process_without_waiting_for_the_notes(live_api, slow_notes, monkeypatch):
    """A windowless process making notes would keep both hotkeys (a new launch could not get them) and the exe."""
    exits = []
    monkeypatch.setattr(app.os, "_exit", exits.append)
    monkeypatch.setattr(app.logging, "shutdown", lambda: None)
    assert live_api.start()["ok"]
    live_api._bus.caption("me_dst", "Я", "Hello.")
    live_api._exit()
    assert exits == [0] and not live_api._running()
    notes = [t for t in threading.enumerate() if "_auto_notes" in t.name]
    assert notes and all(t.daemon for t in notes)
    [pending] = app.RECORDS_DIR.glob("*" + app.NOTES_PENDING)  # the next launch makes them if this one is gone
    slow_notes.set()
    for t in notes:
        t.join(5)
    assert not pending.exists() and len(list(app.RECORDS_DIR.glob("*.json"))) == 1


def test_notes_cut_off_by_closing_are_made_at_the_next_launch(live_api, slow_notes):
    app.RECORDS_DIR.mkdir()
    (app.RECORDS_DIR / "2026-09-28_10-00-00.txt").write_text(
        "Live Translator — x\nДлительность: 00:01:00\n\n[00:00] Я: Привет.\n", encoding="utf-8")
    marker = app.RECORDS_DIR / ("2026-09-28_10-00-00" + app.NOTES_PENDING)
    marker.touch()
    (app.RECORDS_DIR / ("deleted" + app.NOTES_PENDING)).touch()  # its record is gone: nothing to make
    slow_notes.set()
    live_api._resume_notes()
    asyncio.run(until(lambda: not marker.exists(), what="the notes"))
    assert sorted(p.name for p in app.RECORDS_DIR.iterdir()) == ["2026-09-28_10-00-00.json",
                                                                  "2026-09-28_10-00-00.txt"]


# --- overlay position ------------------------------------------------------------------------

def test_overlay_position_must_be_on_a_connected_screen(monkeypatch):
    monkeypatch.setattr(app.webview, "screens", [types.SimpleNamespace(x=0, y=0, width=1920, height=1080)],
                        raising=False)
    assert app.on_screen(100, 700, 780, 180)
    assert not app.on_screen(2500, 900, 780, 180)  # the second monitor is gone
    assert not app.on_screen(None, None, 780, 180)


# --- transcript ---------------------------------------------------------------------------

def test_transcript_pairs_by_time_so_an_extra_split_does_not_shift_the_rest():
    deltas = [(0.0, "me_src", "Я думаю,"), (1.4, "me_src", " что это хорошая идея."),
              (1.8, "me_dst", "I think it's a good idea."),
              (5.0, "me_src", "Давай начнём."), (5.4, "me_dst", "Let's start."),
              (9.0, "me_src", "Спасибо."), (9.3, "me_dst", "Thanks.")]
    assert app.compose_transcript(deltas) == [
        "[00:00] Я: Я думаю, что это хорошая идея.", "        → I think it's a good idea.",
        "[00:05] Я: Давай начнём.", "        → Let's start.",
        "[00:09] Я: Спасибо.", "        → Thanks.",
    ]


def test_transcript_phrase_without_translation_keeps_its_own_line():
    deltas = [(0.0, "me_src", "Угу."), (6.0, "me_src", "Давайте начнём."), (6.9, "me_dst", "Let's begin.")]
    assert app.compose_transcript(deltas) == [
        "[00:00] Я: Угу. Давайте начнём.", "        → Let's begin."]


# --- keys, proxies ----------------------------------------------------------------------------

def test_key_saved_in_the_app_wins_over_the_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(lt, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setenv("OPENAI_API_KEY", "old-global-key")
    lt.save_api_key("new-key")
    monkeypatch.setenv("OPENAI_API_KEY", "old-global-key")  # after a restart the global variable is back
    assert lt.load_api_key() == "new-key"
    monkeypatch.setenv(soniox_engine.KEY_ENV, "from-env")
    assert lt.load_api_key(soniox_engine.KEY_ENV) == "from-env"  # nothing saved: the environment still works


@pytest.mark.parametrize("typed, expected", [
    ("127.0.0.1:10808", "socks5h://127.0.0.1:10808"),
    ("socks://127.0.0.1:10808", "socks5h://127.0.0.1:10808"),
    ("SOCKS5://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
    (" http://127.0.0.1:10809 ", "http://127.0.0.1:10809"),
    ("None", None),
])
def test_typed_proxy_is_normalized(typed, expected):
    assert lt.detect_proxy(typed) == expected


@pytest.mark.parametrize("typed", ["ftp://127.0.0.1:21", "socks5h://"])
def test_invalid_proxy_is_a_clear_error(typed):
    with pytest.raises(lt.Fatal, match="Неверный адрес прокси"):
        lt.detect_proxy(typed)


def test_proxy_password_is_not_logged():
    assert lt.redact("socks5h://user:secret@1.2.3.4:1080") == "socks5h://***@1.2.3.4:1080"
    assert lt.redact("http://127.0.0.1:10809") == "http://127.0.0.1:10809"


def test_http_proxy_credentials_are_sent(http_server):
    proxy = voice_clone.TTS_API.replace("http://", "http://user:p%40ss@")  # the mock plays the proxy
    target = f"http://127.0.0.1:{free_port()}/v1/voices"
    try:
        voice_clone.https_request("GET", target, {}, None, proxy)
    except OSError:
        pass
    [req] = http_server.requests
    assert req.headers["Proxy-Authorization"] == "Basic " + base64.b64encode(b"user:p@ss").decode()


def test_unsupported_proxy_scheme_is_refused():
    with pytest.raises(voice_clone.CloneError, match="HTTPS-прокси"):
        voice_clone.https_request("GET", "https://api.soniox.com/v1/voices", {}, None, "https://proxy:8443")


# --- the suite itself: no network, no hanging --------------------------------------------------------

def test_tests_never_go_through_the_system_proxy(live_api, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")  # a system proxy, like a VPN client's
    assert urllib.request.getproxies() == {}
    assert live_api._proxy() is None  # the mocks are dialled directly, never through a VPN


def test_a_stuck_test_stops_the_run(tmp_path):
    (tmp_path / "test_stuck.py").write_text("import time\n\n\ndef test_stuck():\n    time.sleep(30)\n")
    path = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent), os.environ.get("PYTHONPATH")]))
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "no:cacheprovider", "-o", "test_timeout=1",
         "-q", str(tmp_path)],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": path}, capture_output=True, text=True, timeout=20)
    assert run.returncode != 0
    assert "test_stuck" in run.stderr  # where it got stuck


# --- engine: pause, reconnects ------------------------------------------------------------------

def test_pause_silences_my_voice():
    engine = lt.Engine(argparse.Namespace(), FakeSink())
    fed = []
    engine.players = [types.SimpleNamespace(feed=fed.append)]
    engine._play(b"a")
    engine.paused = True
    engine._play(b"b")
    assert engine._silenced() and fed == [b"a"]


HEADPHONES, CABLE_IN = "Headphones (Realtek(R) Audio)", "CABLE Input (VB-Audio Virtual Cable)"


async def test_a_cable_default_output_mid_call_pauses_my_voice_until_fixed(monkeypatch):
    default, on_loop = {"output": HEADPHONES}, []

    def windows_default(kind):
        on_loop.append(threading.current_thread() is threading.main_thread())
        return default[kind]

    monkeypatch.setattr(lt, "windows_default", windows_default)
    monkeypatch.setattr(lt.Engine, "WATCH", 0.01)
    sink, fed, cleared = FakeSink(), [], []
    engine = lt.Engine(argparse.Namespace(), sink)
    engine.players = [types.SimpleNamespace(feed=fed.append, clear=lambda: cleared.append(True))]
    task = asyncio.create_task(engine.watch_output())
    try:
        await until(lambda: len(on_loop) >= 2, what="checks")
        assert sink.statuses == [] and not engine._silenced()
        default["output"] = CABLE_IN  # the headset dropped, Windows fell back to the cable
        await until(lambda: sink.statuses, what="the cable noticed")
        engine._play(b"a")
        assert engine._silenced() and fed == [] and cleared == [True]  # what was queued is cut too
        await asyncio.sleep(0.05)
        default["output"] = HEADPHONES
        await until(lambda: len(sink.statuses) == 2, what="fixed")
        engine._play(b"b")
    finally:
        await stop(task)
    (label, problem, ok), fixed = sink.statuses  # each reported once, however often it is checked
    assert label == "Вывод звука" and "системные звуки" in problem and ok is False
    assert fixed[0] == "Вывод звука" and fixed[2] is True
    assert fed == [b"b"] and not engine._silenced()
    assert not any(on_loop)  # Windows is asked off the event loop


async def test_no_answer_from_windows_keeps_my_voice_going(monkeypatch):
    asked = []

    def no_answer():
        asked.append(True)
        raise RuntimeError("the default endpoint is changing")

    monkeypatch.setattr(lt, "sc", types.SimpleNamespace(default_speaker=no_answer))
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 2)  # PortAudio's default from the start...
    monkeypatch.setattr(lt, "device_name", {2: CABLE_IN}.get)  # ...the cable, fixed in Windows since
    monkeypatch.setattr(lt.Engine, "WATCH", 0.01)
    sink, cleared = FakeSink(), []
    engine = lt.Engine(argparse.Namespace(), sink)
    engine.players = [types.SimpleNamespace(feed=lambda pcm: None, clear=lambda: cleared.append(True))]
    task = asyncio.create_task(engine.watch_output())
    try:
        await until(lambda: len(asked) >= 3, what="checks")
    finally:
        await stop(task)
    assert sink.statuses == [] and cleared == [] and not engine._silenced()  # nothing queued was dropped


def fake_soundcard(default, opened, level=0.0):
    """soundcard with loopback recorders of this level: default["gone"] makes the open recorder fail like an unplugged
    device, default["missing"] makes opening fail."""

    class Recorder:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def record(self, numframes):
            if default.get("gone"):
                raise RuntimeError("device invalidated")
            time.sleep(0.005)
            return np.full((numframes, 1), level, np.float32)

    def get_microphone(id, include_loopback):
        if default.get("missing"):
            raise RuntimeError("no such device")
        opened.append(id)
        return types.SimpleNamespace(recorder=lambda samplerate, channels, blocksize: Recorder())

    return types.SimpleNamespace(default_speaker=lambda: types.SimpleNamespace(name=default["output"]),
                                 get_microphone=get_microphone,
                                 SoundcardRuntimeWarning=type("SoundcardRuntimeWarning", (RuntimeWarning,), {}))


async def test_lost_loopback_is_never_reopened_on_the_cable(monkeypatch):
    default, opened, statuses = {"output": HEADPHONES}, [], []
    monkeypatch.setattr(lt, "sc", fake_soundcard(default, opened))
    monkeypatch.setattr(lt, "ctypes", types.SimpleNamespace(
        windll=types.SimpleNamespace(ole32=types.SimpleNamespace(CoInitializeEx=lambda *args: 0))))
    heard, stop_loopback = lt.start_loopback(None, asyncio.get_running_loop(), asyncio.Queue(), lambda: False,
                                             on_status=lambda text, ok: statuses.append((text, ok)))
    try:
        assert heard == HEADPHONES
        default.update(output=CABLE_IN, gone=True)  # unplugged: Windows makes the cable its default output
        await until(lambda: len(statuses) == 2, what="the cable refused")
        await asyncio.sleep(1.2)  # it keeps waiting and says so once
        assert opened == [HEADPHONES] and len(statuses) == 2
        default["gone"] = False
        default["output"] = HEADPHONES
        await until(lambda: len(statuses) == 3, what="the headset back")
    finally:
        stop_loopback.set()
    assert statuses[0] == ("звук компьютера пропал (device invalidated), жду устройство…", False)
    assert "системные звуки" in statuses[1][0] and statuses[1][1] is False
    assert statuses[2] == (f"снова слышу: {HEADPHONES}", True) and opened == [HEADPHONES, HEADPHONES]


def following(monkeypatch, default, opened, level=0.0):
    """The loopback of the Windows default output, checking every 20 ms whether the call moved to another device."""
    monkeypatch.setattr(lt, "sc", fake_soundcard(default, opened, level))
    monkeypatch.setattr(lt, "ctypes", types.SimpleNamespace(
        windll=types.SimpleNamespace(ole32=types.SimpleNamespace(CoInitializeEx=lambda *args: 0))))
    monkeypatch.setattr(lt, "FOLLOW", 0.02)
    monkeypatch.setattr(lt, "FOLLOW_SILENT", 0.1)
    statuses = []
    _, stop_loopback = lt.start_loopback(None, asyncio.get_running_loop(), asyncio.Queue(), lambda: False,
                                         on_status=lambda text, ok: statuses.append((text, ok)))
    return statuses, stop_loopback


async def test_the_loopback_follows_the_default_output_to_another_device(monkeypatch):
    headset = "Headphones (Jabra Evolve2)"
    default, opened = {"output": HEADPHONES}, []
    statuses, stop_loopback = following(monkeypatch, default, opened)  # nobody plays to the headphones any more
    try:
        default["output"] = CABLE_IN  # never the cable...
        await asyncio.sleep(0.3)
        assert opened == [HEADPHONES] and statuses == []
        default["output"] = headset  # ...but a headset that connected: Zoom and Chrome play there now
        await until(lambda: statuses, what="the headset followed")
    finally:
        stop_loopback.set()
    assert opened == [HEADPHONES, headset] and statuses == [(f"теперь слышу: {headset}", True)]


async def test_the_loopback_stays_on_a_device_the_call_still_plays_on(monkeypatch):
    default, opened = {"output": HEADPHONES}, []
    statuses, stop_loopback = following(monkeypatch, default, opened, level=0.0005)  # the call's noise in a pause
    try:
        default["output"] = "Headphones (Jabra Evolve2)"  # a headset connected, but Zoom plays where it was told to
        await asyncio.sleep(0.4)
    finally:
        stop_loopback.set()
    assert opened == [HEADPHONES] and statuses == []  # the other side's subtitles go on


async def test_a_new_default_output_that_will_not_open_yet_is_reported(monkeypatch):
    headset = "Headphones (Jabra Evolve2)"
    default, opened = {"output": HEADPHONES}, []
    statuses, stop_loopback = following(monkeypatch, default, opened)
    try:
        default.update(output=headset, missing=True)  # Windows made it the default, but it won't open yet
        await until(lambda: statuses, what="the switch that failed")
        default["missing"] = False
        await until(lambda: len(statuses) == 2, what="the headset opened")
    finally:
        stop_loopback.set()
    assert statuses == [("звук компьютера пропал (no such device), жду устройство…", False),
                        (f"снова слышу: {headset}", True)]
    assert opened == [HEADPHONES, headset]


def taken(queue):
    frames = []
    while not queue.empty():
        frames.append(queue.get_nowait())
    return frames


def about_real_time(frames, seconds):
    """Silence frames only, about as many as real time makes in `seconds`."""
    return set(frames) == {lt.SILENCE} and 0.4 * seconds * 50 <= len(frames) <= 1.6 * seconds * 50


async def test_the_subtitle_channel_hears_silence_while_its_device_is_gone(monkeypatch):
    default, opened, statuses = {"output": HEADPHONES}, [], []
    monkeypatch.setattr(lt, "sc", fake_soundcard(default, opened, level=0.1))
    monkeypatch.setattr(lt, "ctypes", types.SimpleNamespace(
        windll=types.SimpleNamespace(ole32=types.SimpleNamespace(CoInitializeEx=lambda *args: 0))))
    queue = asyncio.Queue()
    heard, stop_loopback = lt.start_loopback(None, asyncio.get_running_loop(), queue, lambda: False,
                                             on_status=lambda text, ok: statuses.append((text, ok)))
    try:
        await until(lambda: queue.qsize() > 5, what="the call heard")
        default.update(gone=True, missing=True)  # unplugged, and not back yet
        await until(lambda: statuses, what="the loss")
        taken(queue)
        await asyncio.sleep(0.5)
        frames = taken(queue)
    finally:
        stop_loopback.set()
    assert about_real_time(frames, 0.5)  # Soniox ends their last phrase and keeps the stream


class FakeMic:
    """A microphone stream: `delay` s after start() its callback runs every 10 ms on its own thread, until close()
    or a loss."""

    def __init__(self, callback, delay=0.0):
        self.callback, self.delay = callback, delay
        self.active = self.lost = self.closed = False

    def start(self):
        self.active = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        time.sleep(self.delay)
        while self.active and not self.lost:
            self.callback(np.full(480, 3000, "<i2").tobytes(), 480, None, None)
            time.sleep(0.01)

    def close(self):
        self.active, self.closed = False, True


@pytest.mark.parametrize("loss", ["callbacks stop", "stream finished"])
async def test_a_lost_microphone_is_reported_and_reopened(monkeypatch, loss):
    tries, opened = [], []

    def pick_device(name, kind):
        tries.append((name, kind))
        if len(tries) == 1:
            raise lt.Fatal("Аудиоустройство не найдено")  # not back yet
        return 4

    def open_mic(device):
        opened.append(FakeMic(on_mic, delay=0.4))  # a headset slow to start is not taken for lost again
        return opened[-1]

    monkeypatch.setattr(lt, "pick_device", pick_device)
    monkeypatch.setattr(lt, "device_name", {4: "Headset Microphone (Jabra)"}.get)
    monkeypatch.setattr(lt.Engine, "MIC_SILENT", 0.2)
    monkeypatch.setattr(lt.Engine, "MIC_START", 1.0)
    sink = FakeSink()
    engine = lt.Engine(argparse.Namespace(inp="Jabra"), sink)

    def on_mic(*args):
        engine.mic_seen = time.monotonic()

    engine.mic = lost = FakeMic(on_mic)
    engine._start_mic()
    engine.mic_rms = 3000.0
    task = asyncio.create_task(engine.watch_mic(open_mic))
    try:
        await asyncio.sleep(0.4)
        assert sink.statuses == [] and tries == []  # delivering audio: left alone
        if loss == "callbacks stop":  # unplugged: WASAPI just stops calling back
            lost.lost = True
        else:
            lost.active = False
        await until(lambda: len(sink.statuses) == 2, what="the microphone back")
    finally:
        await stop(task)
        engine.mic.close()
    assert sink.statuses == [("Микрофон", "пропал — жду устройство…", False),
                             ("Микрофон", "снова слышу: Headset Microphone (Jabra)", True)]
    assert lost.closed and engine.mic is opened[0] and len(opened) == 1 and tries == [("Jabra", "input")] * 2
    assert engine.mic_rms == 0.0  # the meter does not freeze at the last level


async def test_my_channel_hears_silence_while_the_microphone_is_gone(monkeypatch):
    back = threading.Event()

    def pick_device(name, kind):
        if not back.is_set():
            raise lt.Fatal("Аудиоустройство не найдено")
        return 4

    monkeypatch.setattr(lt, "pick_device", pick_device)
    monkeypatch.setattr(lt, "device_name", {4: "Headset Microphone (Jabra)"}.get)
    monkeypatch.setattr(lt.Engine, "MIC_SILENT", 0.2)
    sink, forced, loop = FakeSink(), [], asyncio.get_running_loop()
    engine = lt.Engine(argparse.Namespace(inp=None), sink)
    engine.mic_q = asyncio.Queue()
    engine.me_channel = lt.Channel("Я", "en", engine.mic_q, [], "me")
    engine.me_channel.finalizer = types.SimpleNamespace(force=lambda: forced.append(True))

    def on_mic(*args):
        engine.mic_seen = time.monotonic()
        loop.call_soon_threadsafe(engine.mic_q.put_nowait, b"voice")

    engine.mic = lost = FakeMic(on_mic)
    engine._start_mic()
    task = asyncio.create_task(engine.watch_mic(lambda device: FakeMic(on_mic)))
    try:
        await asyncio.sleep(0.3)
        lost.lost = True  # Bluetooth dropped mid-sentence
        await until(lambda: sink.statuses, what="the loss")
        taken(engine.mic_q)
        await asyncio.sleep(0.5)
        gone = taken(engine.mic_q)
        back.set()
        await until(lambda: len(sink.statuses) == 2, what="the microphone back")
        taken(engine.mic_q)
        await asyncio.sleep(0.2)
        again = taken(engine.mic_q)
    finally:
        await stop(task)
        engine.mic.close()
    assert about_real_time(gone, 0.5)  # Soniox still hears the pause after my last words, and keeps the stream
    assert forced == [True]  # the phrase cut off is spoken now, not merged into what I say next
    assert again and set(again) == {b"voice"}  # no silence mixed into the microphone that is back


async def test_the_cable_windows_falls_back_to_is_shown_and_never_heard_as_my_microphone(monkeypatch):
    cable_out, back, opened = "CABLE Output (VB-Audio Virtual Cable)", threading.Event(), []
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 7)  # the Windows default microphone now
    monkeypatch.setattr(lt, "device_name", {4: "Headset Microphone (Jabra)", 7: cable_out}.get)
    monkeypatch.setattr(lt.Engine, "MIC_SILENT", 0.2)
    sink = FakeSink()
    engine = lt.Engine(argparse.Namespace(inp=None), sink)

    def on_mic(*args):
        engine.mic_seen = time.monotonic()

    def open_mic(device):
        if device == 4 and not back.is_set():
            raise RuntimeError("Error opening RawInputStream: Device unavailable")
        opened.append(device)
        return FakeMic(on_mic)

    engine.mic = lost = FakeMic(on_mic)
    engine.mic_device = 4  # the call started on the headset
    engine._start_mic()
    task = asyncio.create_task(engine.watch_mic(open_mic))
    try:
        await asyncio.sleep(0.3)
        lost.lost = True  # the headset dropped, and Windows made the cable its default microphone
        await until(lambda: len(sink.statuses) == 2, what="the cable refused")
        await asyncio.sleep(0.5)
        assert opened == [] and len(sink.statuses) == 2  # never opened, and said once
        back.set()
        await until(lambda: len(sink.statuses) == 3, what="the headset back")
    finally:
        await stop(task)
        engine.mic.close()
    assert sink.statuses == [
        ("Микрофон", "пропал — жду устройство…", False),
        ("Микрофон", f"Windows переключил микрофон на «{cable_out}» — подключите настоящий микрофон.", False),
        ("Микрофон", "снова слышу: Headset Microphone (Jabra)", True)]
    assert opened == [4]


async def test_the_engine_watches_its_devices_during_the_call(monkeypatch):
    default, mics, fed = {"output": HEADPHONES}, [], []
    call = types.SimpleNamespace(feed=fed.append, clear=lambda: None, gain=1.0, stream=types.SimpleNamespace(
        start=lambda: None, stop=lambda: None, close=lambda: None))
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 1 if kind == "input" else 2)
    monkeypatch.setattr(lt, "device_name", {1: "Microphone (USB)", 2: CABLE_IN}.get)
    monkeypatch.setattr(lt, "default_name", lambda kind: default[kind])
    monkeypatch.setattr(lt, "windows_default", lambda kind: default[kind])
    monkeypatch.setattr(lt, "Player", lambda device: call)
    monkeypatch.setattr(lt, "stream_kwargs", lambda device, blocksize=lt.BLOCK: {})
    monkeypatch.setattr(lt, "sd", types.SimpleNamespace(RawInputStream=lambda callback: mics.append(
        FakeMic(callback)) or mics[-1]))
    monkeypatch.setattr(lt.Engine, "WATCH", 0.02)
    monkeypatch.setattr(lt.Engine, "MIC_SILENT", 0.2)
    sink = FakeSink()
    sink.level = lambda me, them: None
    engine = lt.Engine(argparse.Namespace(no_me=False, no_listen=True, out="CABLE Input", inp=None, monitor=False,
                                          monitor_device=None, passthrough=True), sink)
    task = asyncio.create_task(engine.run())
    try:
        await until(lambda: fed, what="the microphone heard")
        mics[0].lost = True
        await until(lambda: ("Микрофон", "снова слышу: Microphone (USB)", True) in sink.statuses, what="reopened")
        default["output"] = CABLE_IN
        await until(lambda: any(label == "Вывод звука" for label, _, _ in sink.statuses), what="the cable noticed")
    finally:
        await stop(task)
    assert len(mics) == 2 and all(m.closed for m in mics) and engine.mic is None


def call_devices(monkeypatch, mics):
    """Fake devices for an Engine that runs a whole call; every microphone it opens is added to `mics`."""
    call = types.SimpleNamespace(feed=lambda pcm: None, clear=lambda: None, gain=1.0, stream=types.SimpleNamespace(
        start=lambda: None, stop=lambda: None, close=lambda: None))
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 1 if kind == "input" else 2)
    monkeypatch.setattr(lt, "device_name", {1: "Microphone (USB)", 2: CABLE_IN}.get)
    monkeypatch.setattr(lt, "default_name", lambda kind: HEADPHONES)
    monkeypatch.setattr(lt, "windows_default", lambda kind: HEADPHONES)
    monkeypatch.setattr(lt, "Player", lambda device: call)
    monkeypatch.setattr(lt, "stream_kwargs", lambda device, blocksize=lt.BLOCK: {})
    monkeypatch.setattr(lt, "sd", types.SimpleNamespace(RawInputStream=lambda callback: mics.append(
        FakeMic(callback)) or mics[-1]))
    monkeypatch.setattr(lt.Engine, "WATCH", 0.02)
    monkeypatch.setattr(lt.Engine, "MIC_SILENT", 0.2)


def other_tasks():
    return asyncio.all_tasks() - {asyncio.current_task()}


async def test_a_stopped_engine_leaves_no_task_pending(monkeypatch):
    """Cancelling Engine.run returns at once; its watchers must have ended by then, not be left to a closed loop."""
    mics = []
    call_devices(monkeypatch, mics)
    sink = FakeSink()
    sink.level = lambda me, them: None
    engine = lt.Engine(argparse.Namespace(no_me=False, no_listen=True, out="CABLE Input", inp=None, monitor=False,
                                          monitor_device=None, passthrough=True), sink)
    feeding = []

    async def feed_silence():
        feeding.append(1)
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)  # a cleanup that takes a moment

    monkeypatch.setattr(engine, "_feed_silence", feed_silence)
    monkeypatch.setattr(engine, "_mic_back", lambda open_mic: asyncio.Event().wait())  # the microphone stays gone
    task = asyncio.create_task(engine.run())
    await until(lambda: mics and mics[0].active, what="the microphone started")
    mics[0].lost = True
    await until(lambda: feeding, what="the loss noticed")
    await stop(task)
    assert not other_tasks()


async def test_an_engine_waits_for_its_jobs_to_finish_their_cleanup(monkeypatch):
    mics, jobs = [], []
    call_devices(monkeypatch, mics)
    sink = FakeSink()
    sink.level = lambda me, them: None
    sink.run = lambda: asyncio.Event().wait()

    async def job():
        jobs.append(asyncio.current_task())
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)  # closing a connection takes a moment

    args = argparse.Namespace(no_me=False, no_listen=True, out="CABLE Input", inp=None, monitor=False,
                              monitor_device=None, passthrough=False, proxy="none", engine="soniox", voice="off",
                              lang="en", their_lang="ru")
    engine = lt.Engine(args, sink)
    monkeypatch.setattr(engine, "_soniox_jobs", lambda me, them, proxy, lag: [job()])
    task = asyncio.create_task(engine.run())
    await until(lambda: jobs, what="the job running")
    await stop(task)
    assert jobs[0].done() and not other_tasks()
    assert all(m.closed for m in mics) and engine.mic is None


async def test_finish_waits_for_a_task_that_takes_a_moment_to_end():
    ended = []

    async def slow_to_end():
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)
            ended.append(1)

    task = asyncio.create_task(slow_to_end())
    await asyncio.sleep(0)
    await lt.finish([task])
    assert ended == [1] and task.done()
    await lt.finish([])  # nothing to wait for


async def test_finish_gives_up_on_a_task_that_will_not_end():
    release = asyncio.Event()

    async def stubborn():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    task = asyncio.create_task(stubborn())
    await asyncio.sleep(0)
    started = time.monotonic()
    await lt.finish([task], timeout=0.1)
    assert not task.done() and time.monotonic() - started < 2
    release.set()
    await task


def test_a_restart_or_stop_ends_every_task_of_the_engine_loop(live_api, monkeypatch):
    seen = []

    class Spawning(StubEngine):
        async def run(self):
            async def grand():
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0.05)  # a cleanup that takes a moment

            async def child():
                grandchild = asyncio.create_task(grand())
                seen.append(grandchild)
                try:
                    await asyncio.Event().wait()
                finally:
                    grandchild.cancel()  # asked to end, not awaited

            seen.append(asyncio.create_task(child()))
            await asyncio.Event().wait()

    def wait_for(count):
        deadline = time.monotonic() + 5
        while len(seen) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(seen) >= count

    monkeypatch.setattr(lt, "Engine", Spawning)
    assert live_api.start()["ok"]
    wait_for(2)
    assert live_api.save_settings({"me_lang": "en"})["restarted"]  # the first loop ends, a second one starts
    wait_for(4)
    assert all(t.done() for t in seen[:2])
    live_api.stop()
    assert all(t.done() for t in seen)


def test_close_loop_ends_what_is_left_and_closes():
    loop = asyncio.new_event_loop()
    left = []

    async def straggler():
        left.append(asyncio.current_task())
        await asyncio.Event().wait()

    loop.create_task(straggler())
    loop.run_until_complete(asyncio.sleep(0.01))
    lt.close_loop(loop)
    assert loop.is_closed() and left[0].cancelled()


async def test_connected_only_after_soniox_accepts_the_config(ws_server):
    async def handler(ws):
        await ws.recv()
        await asyncio.sleep(0.3)
        await ws.send(ACK)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    task = asyncio.create_task(soniox_engine.run_stt_channel(channel(), KEY, None, sink, "en", ["ru"], None))
    try:
        await asyncio.sleep(0.15)
        assert sink.statuses == []  # config sent, not accepted yet
        await until(lambda: sink.statuses, what="accepted")
    finally:
        await stop(task)
    assert sink.statuses == [("Я → EN", "подключено", True)]


async def test_rejected_config_is_fatal(ws_server):
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"tokens": [], "error_code": 400, "error_type": "model_not_available",
                                  "error_message": "The requested model is not available."}))
        await ws.wait_closed()

    ws_server.handler = handler
    with pytest.raises(soniox_engine.SonioxFatal, match="model is not available"):
        await asyncio.wait_for(
            soniox_engine.run_stt_channel(channel(), KEY, None, FakeSink(), "en", ["ru"], None), 5)


async def test_broken_handshake_is_retried(monkeypatch):
    attempts = []

    async def hang_up(reader, writer):  # accepts TCP, then closes before any HTTP response
        attempts.append(1)
        writer.close()

    server = await asyncio.start_server(hang_up, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(soniox_engine, "STT_URL", f"ws://127.0.0.1:{port}/")
    sink, ch = FakeSink(), channel()
    task = asyncio.create_task(soniox_engine.run_stt_channel(ch, KEY, None, sink, "en", ["ru"], None))
    try:
        for _ in range(soniox_engine.RECENT + 50):
            await ch.queue.put(bytes(960))
        await until(lambda: len(attempts) >= 2, what="second attempt")
        assert not task.done()  # InvalidMessage no longer kills the channel
    finally:
        await stop(task)
        server.close()
    assert sink.statuses[0] == ("Я → EN", "нет связи, переподключение… (VPN включён?)", False)
    assert ch.queue.qsize() == soniox_engine.RECENT  # offline audio does not pile up; the last 2 s stay


async def test_rejected_voice_is_not_rewarmed_in_a_loop(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            msgs.append(msg)
            if "model" in msg:  # a deleted clone: every stream is refused
                await ws.send(json.dumps({"stream_id": msg["stream_id"], "error_code": 400,
                                          "error_type": "voice_not_found", "error_message": "Voice not found."}))
                await ws.send(terminated(msg["stream_id"]))

    monkeypatch.setattr(soniox_engine.SonioxVoice, "REWARM", 0.1)
    ws_server.handler = handler
    sink = FakeSink()
    task = await run_voice(make_voice(sink, []), sink)
    try:
        await asyncio.sleep(0.8)
    finally:
        await stop(task)
    assert len(configs(msgs)) == 1
    assert sink.notes == ["[Мой голос] Voice not found."]


async def test_a_quiet_clause_is_spoken_without_waiting_for_the_pause(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: any(x.get("text_end") for x in m))
        await ws.wait_closed()

    monkeypatch.setattr(soniox_engine.SonioxVoice, "FLUSH", 0.05)
    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        await voice.say("My name is")
        await voice.say(" Suren,")
        await until(lambda: any(m.get("text_end") for m in msgs), what="text_end")
    finally:
        await stop(task)
    sid = msgs[0]["stream_id"]
    assert [m for m in msgs if "text" in m] == [
        {"stream_id": sid, "text": "My name is", "text_end": False},
        {"stream_id": sid, "text": " Suren,", "text_end": False},
        {"stream_id": sid, "text": "", "text_end": True},
    ]
