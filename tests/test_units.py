"""Pure units: compose_transcript, Bus, LagMeter, detect_proxy, endpoint overrides, Api.start checks,
stealth device checks, the voice provider of the Soniox engine."""
import argparse
import asyncio
import collections
import os
import sys
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
    monkeypatch.setattr(app, "sd", types.SimpleNamespace(query_devices=lambda: api.devices))
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


def test_engine_chosen_by_hand_is_kept(api, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv(soniox_engine.KEY_ENV, "soniox-key")
    api.save_settings({"engine": "openai", "engine_auto": False})
    assert api._auto_engine() is None
    assert api._settings["engine"] == "openai"


def test_start_needs_vb_cable(api, monkeypatch):
    monkeypatch.setenv(soniox_engine.KEY_ENV, "test-key")
    api.devices = [SPEAKERS]
    assert api.start() == {"ok": False, "error": "no_cable"}
    assert api.started_engine == 0


# --- Api -> engine arguments, hotkeys, default devices ------------------------------------------

def test_window_never_passes_my_voice_through(api):
    assert api._args().passthrough is False  # the call would hear my Russian


def test_levers_reach_the_engine(api):
    args = api._args()
    assert (args.speed, args.speed_boost, args.trim_silence, args.instant_phrases, args.auto_finalize) == (
        1.1, True, True, True, True)
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
    monkeypatch.setattr(app, "sd", types.SimpleNamespace(query_devices=lambda: devices))
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


def test_monitor_on_the_cable_is_skipped(monkeypatch):
    names = {1: "Microphone (USB)", 2: "CABLE Input (VB-Audio Virtual Cable)", 3: "CABLE In 16ch"}
    monkeypatch.setattr(lt, "pick_device", lambda name, kind: 1 if kind == "input" else 3 if name is None else 2)
    monkeypatch.setattr(lt, "device_name", names.get)
    monkeypatch.setattr(lt, "default_name", lambda kind: "Headphones")
    opened = []
    monkeypatch.setattr(lt, "Player", lambda device: opened.append(device) or FakePlayer())
    monkeypatch.setattr(lt, "stream_kwargs", lambda device, blocksize=lt.BLOCK: {})

    def no_mic(**kwargs):
        raise RuntimeError("stop before the microphone opens")

    monkeypatch.setattr(lt, "sd", types.SimpleNamespace(RawInputStream=no_mic))
    engine = lt.Engine(device_args(monitor=True), FakeSink())
    with pytest.raises(RuntimeError, match="stop before"):
        asyncio.run(engine.run())
    assert opened == [2] and engine.monitor is None  # the call's cable only, no second copy into it
    assert any("Слышать себя" in note for note in engine.sink.notes)


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
    monkeypatch.setitem(sys.modules, "cartesia_engine", types.SimpleNamespace(CartesiaVoice=classes["CartesiaVoice"]))
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
                speed_boost=True, trim_silence=True, instant_phrases=True, inworld_model=None)
    return argparse.Namespace(**{**args, **changes})


def make_voice(args):
    engine = lt.Engine(args, FakeSink())
    return engine, engine._make_voice("soniox-key", None, lt.LagMeter())


def test_soniox_voice_gets_every_lever(voices):
    engine, voice = make_voice(voice_args())
    assert type(voice).__name__ == "SonioxVoice"
    assert (voice.api_key, voice.voice, voice.language) == ("soniox-key", "Adrian", "en")
    assert voice.kwargs == {"speed": 1.1, "backlog": engine._backlog, "speed_boost": True, "trim": True,
                            "phrases": ("cache", "phrases", "soniox|tts-rt-v2|Adrian|en|1.1")}


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


def test_cartesia_needs_a_chosen_voice(voices, monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "cartesia-key")
    with pytest.raises(lt.Fatal, match="Выберите голос Cartesia"):
        make_voice(voice_args(voice_provider="cartesia"))  # no built-in default: one is picked in the list


def test_unknown_provider_falls_back_to_soniox(voices):
    assert lt.voice_provider(argparse.Namespace()) == "soniox"
    assert lt.voice_provider(argparse.Namespace(voice_provider="elevenlabs")) == "soniox"
    _, voice = make_voice(voice_args(voice_provider=None))
    assert type(voice).__name__ == "SonioxVoice"


def test_console_defaults_to_the_faster_voice_with_every_lever():
    args = lt.build_parser().parse_args([])
    assert args.speed == 1.1 and args.voice_provider == "soniox"
    assert (args.speed_boost, args.trim_silence, args.instant_phrases, args.auto_finalize) == (True,) * 4
    args = lt.build_parser().parse_args(["--voice-provider", "cartesia", "--no-trim", "--no-auto-finalize"])
    assert (args.voice_provider, args.trim_silence, args.auto_finalize) == ("cartesia", False, False)


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


def test_latency_voice_is_built_like_the_app(voices, timeline):
    keys = {"soniox": "s-key", "cartesia": "c-key", "inworld": "i-key", "openai": "o-key"}
    args = latency_test.build_parser().parse_args(["--provider", "cartesia", "--no-trim", "--speed", "1.2"])
    voice = latency_test.make_voice(args, keys, "c-1", None, FakeSink(), timeline)
    assert type(voice).__name__ == "CartesiaVoice" and (voice.api_key, voice.voice) == ("c-key", "c-1")
    assert voice.kwargs == {"speed": 1.2, "backlog": timeline.backlog, "speed_boost": True, "trim": False,
                            "model": voice_clone.TTS_MODEL}
    assert latency_test.rtt_probes(args, keys) == [("Soniox STT", soniox_engine.STT_URL, None),
                                                   ("Cartesia TTS", voice_clone.TTS_URL, {"X-API-Key": "c-key"})]
    args.provider = "soniox"
    voice = latency_test.make_voice(args, keys, "Adrian", None, FakeSink(), timeline)
    assert type(voice).__name__ == "SonioxVoice" and "model" not in voice.kwargs


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


def test_installed_settings_get_the_new_default_speed(monkeypatch, tmp_path):
    monkeypatch.setattr(latency_test, "INSTALLED", tmp_path)
    assert latency_test.installed_settings() == {}
    (tmp_path / "settings.json").write_text('{"speed": 1.0, "voice": "clone"}', encoding="utf-8")
    assert latency_test.installed_settings()["speed"] == 1.1
