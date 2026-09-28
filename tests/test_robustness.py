"""Regression tests from the global audit: lifecycle races, reconnects, proxies, settings, clones."""
import argparse
import asyncio
import base64
import json
import threading
import time
import types

import pytest

import app
import live_translator as lt
import soniox_engine
import voice_clone
from mocks import FakeSink, free_port, read_until, stop, until
from test_soniox_stt import ACK, channel
from test_soniox_tts import KEY, configs, make_voice, run_voice, terminated
from test_units import CABLE, SPEAKERS


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
    monkeypatch.setattr(lt, "start_hotkey", lambda callback: False)
    monkeypatch.setattr(lt, "Engine", StubEngine)
    monkeypatch.setattr(app, "sd", types.SimpleNamespace(query_devices=lambda: [SPEAKERS, CABLE]))
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
    assert live_api.save_settings({"me_lang": "en"}) == {"restarted": True}
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
    async def offline(*args):
        raise voice_clone.CloneError("Нет связи с Soniox")

    monkeypatch.setattr(soniox_engine, "speak_once", offline)
    assert live_api.preview_voice("Adrian") == {"ok": False, "error": "Нет связи с Soniox"}


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


# --- engine: pause, reconnects ------------------------------------------------------------------

def test_pause_silences_my_voice():
    engine = lt.Engine(argparse.Namespace(), FakeSink())
    fed = []
    engine.players = [types.SimpleNamespace(feed=fed.append)]
    engine._play(b"a")
    engine.paused = True
    engine._play(b"b")
    assert engine._silenced() and fed == [b"a"]


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
        await ch.queue.put(b"old audio")
        await until(lambda: len(attempts) >= 2, what="second attempt")
        assert not task.done()  # InvalidMessage no longer kills the channel
    finally:
        await stop(task)
        server.close()
    assert sink.statuses[0] == ("Я → EN", "нет связи, переподключение… (VPN включён?)", False)
    assert ch.queue.empty()  # audio captured while offline does not pile up


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
