"""Pure units: compose_transcript, Bus, LagMeter, detect_proxy, endpoint overrides, Api.start checks."""
import argparse
import os
import types
import urllib.request

import pytest

import app
import live_translator as lt
import meeting_notes
import soniox_engine
import voice_clone


def test_endpoints_point_at_local_mocks():
    for value, env in ((lt.URL, "LIVE_TRANSLATOR_URL"),
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
    monkeypatch.setattr(lt, "start_hotkey", lambda callback: False)
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


def test_start_needs_the_key_of_the_selected_engine(api, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert api._settings["engine"] == "soniox"  # the default
    assert api.start() == {"ok": False, "error": "no_key"}
    assert api.started_engine == 0


def test_start_needs_vb_cable(api, monkeypatch):
    monkeypatch.setenv(soniox_engine.KEY_ENV, "test-key")
    api.devices = [SPEAKERS]
    assert api.start() == {"ok": False, "error": "no_cable"}
    assert api.started_engine == 0


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
