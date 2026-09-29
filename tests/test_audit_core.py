"""Regression tests from the audit of live_translator.py: listen-only devices, a dead output stream, the blocking
Cartesia lookup, a rejected OpenAI handshake, the .env file, malformed OpenAI frames."""
import argparse
import asyncio
import json
import os
import threading
import types
from http import HTTPStatus

import pytest
from websockets.asyncio.server import serve

import live_translator as lt
import voice_clone
from mocks import FakeSink, b64, stop, until

HEADPHONES, CABLE_IN = "Headphones (Realtek(R) Audio)", "CABLE Input (VB-Audio Virtual Cable)"
KEY = "sk-test"


@pytest.fixture(autouse=True)
def _portaudio_is_never_restarted(monkeypatch):
    monkeypatch.setattr(lt, "refresh_devices", lambda: False)


# --- id 0: listen-only mode does not touch the microphone --------------------------------------------

def device_args(**changes):
    return argparse.Namespace(**{**dict(
        no_me=True, no_listen=False, out="CABLE Input", inp=None, listen=None, monitor=False, monitor_device=None,
        passthrough=False, proxy="none", engine="soniox", voice="off", lang="en", their_lang="ru"), **changes})


class CableStream:
    def __init__(self):
        self.events = []

    def start(self):
        self.events.append("start")

    def stop(self):
        self.events.append("stop")

    def close(self):
        self.events.append("close")


def listen_only_devices(monkeypatch, mic_present):
    """Windows with a cable to play into, and a microphone only when `mic_present`."""
    def pick(name, kind):
        if kind == "input" and not mic_present:
            raise lt.Fatal("Windows не видит ни одного микрофона: подключите его.")
        return 1 if kind == "input" else 2

    cable = types.SimpleNamespace(stream=CableStream(), gain=1.0, feed=lambda pcm: None, clear=lambda: None)
    monkeypatch.setattr(lt, "pick_device", pick)
    monkeypatch.setattr(lt, "device_name", {1: "Microphone (USB)", 2: CABLE_IN}.get)
    monkeypatch.setattr(lt, "default_name", lambda kind: HEADPHONES)
    monkeypatch.setattr(lt, "windows_default", lambda kind: HEADPHONES)
    monkeypatch.setattr(lt, "Player", lambda device: cable)
    monkeypatch.setattr(lt, "stream_kwargs", lambda device, blocksize=lt.BLOCK: {})
    return cable


@pytest.mark.parametrize("mic_present", [False, True])
def test_listen_only_neither_picks_nor_opens_the_microphone(monkeypatch, mic_present):
    listen_only_devices(monkeypatch, mic_present)
    opened = []
    engine = lt.Engine(device_args(), FakeSink())
    engine._open_devices(lambda device: opened.append(device))  # a missing microphone is not an error here
    assert opened == [] and engine.mic is None and engine.mic_device is None
    assert not any(note.startswith("Микрофон") for note in engine.sink.notes)
    assert "Для звонка" in engine.sink.notes[-1]


async def test_a_listen_only_call_starts_without_a_microphone(monkeypatch):
    listen_only_devices(monkeypatch, mic_present=False)
    jobs, sink = [], FakeSink()
    sink.level = lambda me, them: None
    sink.run = lambda: asyncio.Event().wait()
    monkeypatch.setattr(lt, "start_loopback", lambda *a, **kw: ("Speakers", threading.Event()))
    engine = lt.Engine(device_args(), sink)
    monkeypatch.setattr(engine, "_soniox_jobs", lambda me, them, proxy, lag: jobs.append(1) or [])
    task = asyncio.create_task(engine.run())
    try:
        await until(lambda: jobs or task.done(), what="the call started")
        assert jobs and not task.done()
    finally:
        await stop(task)
    assert engine.mic is None


@pytest.mark.parametrize("changes", [dict(no_me=False), dict(passthrough=True)])
def test_a_missing_microphone_still_stops_a_call_that_uses_it(monkeypatch, changes):
    listen_only_devices(monkeypatch, mic_present=False)
    with pytest.raises(lt.Fatal, match="не видит ни одного микрофона"):
        lt.Engine(device_args(**changes), FakeSink())._open_devices(lambda device: pytest.fail("no microphone"))


# --- id 3: a dead output stream is not busy for ever -------------------------------------------------

class DeadStreamError(Exception):
    pass


class OutputStream:
    """A sounddevice output stream: `active` turns False when its device goes away; a closed one raises."""

    def __init__(self):
        self.active, self.closed = True, False

    def __getattribute__(self, name):
        if name == "active" and object.__getattribute__(self, "closed"):
            raise DeadStreamError("Invalid stream pointer")
        return object.__getattribute__(self, name)


def player_on(monkeypatch, stream):
    monkeypatch.setattr(lt, "sd", types.SimpleNamespace(RawOutputStream=lambda **kwargs: stream))
    monkeypatch.setattr(lt, "stream_kwargs", lambda device, blocksize=lt.BLOCK: {})
    return lt.Player(3)


def test_a_player_whose_stream_died_is_not_busy_and_stops_buffering(monkeypatch):
    stream = OutputStream()
    player = player_on(monkeypatch, stream)
    player.feed(bytes(2000))
    assert player.busy and player.buffered > 0  # a living stream: queued speech counts
    stream.active = False  # headphones unplugged: PortAudio stopped calling back
    assert not player.busy  # the loopback gate opens again
    assert player.buffered == 0
    player.feed(bytes(2000))
    assert not player.busy and player.buffered == 0  # dropped, not queued for a device that is gone


def test_a_player_with_a_closed_stream_is_not_busy(monkeypatch):
    stream = OutputStream()
    player = player_on(monkeypatch, stream)
    player.feed(bytes(2000))
    stream.closed = True
    assert not player.busy
    player.feed(bytes(2000))
    assert player.buffered == 0


def test_a_living_player_is_unchanged(monkeypatch):
    player = player_on(monkeypatch, OutputStream())
    assert not player.busy
    player.feed(bytes(lt.RATE // 10 * 2))
    assert player.busy and player.buffered == pytest.approx(0.1)
    player.clear()
    assert player.buffered == 0.0


# --- id 10: the Cartesia library lookup does not block the event loop --------------------------------

async def test_the_default_cartesia_voice_is_looked_up_off_the_event_loop(monkeypatch):
    listen_only_devices(monkeypatch, mic_present=True)
    monkeypatch.setenv("SONIOX_API_KEY", "soniox-key")
    monkeypatch.setenv(voice_clone.KEY_ENV, "cartesia-key")
    calls = []

    def default_voice(key, proxy):
        try:
            asyncio.get_running_loop()
            calls.append("on the event loop")
        except RuntimeError:
            calls.append("in a worker thread")
        raise voice_clone.CloneError("stop here")

    import cartesia_engine
    monkeypatch.setattr(cartesia_engine, "default_voice", default_voice)
    monkeypatch.setattr(lt, "sd", types.SimpleNamespace(RawInputStream=lambda callback, **kw: types.SimpleNamespace(
        start=lambda: None, stop=lambda: None, close=lambda: None, active=True)))
    sink = FakeSink()
    sink.level = lambda me, them: None
    args = device_args(no_me=False, no_listen=True, voice="builtin", voice_provider="cartesia", voice_name=None,
                       voice_id=None, delivery="balanced", match_rate=True, speed=1.0)
    with pytest.raises(lt.Fatal, match="stop here"):
        await asyncio.wait_for(lt.Engine(args, sink).run(), 5)
    assert calls == ["in a worker thread"]


def test_make_voice_still_finds_the_default_voice_itself(monkeypatch):
    """Called directly (no run() before it), _make_voice asks the library as it always did."""
    import cartesia_engine
    monkeypatch.setenv(voice_clone.KEY_ENV, "cartesia-key")
    asked = []
    monkeypatch.setattr(cartesia_engine, "default_voice", lambda key, proxy: asked.append(key) or "blake")
    monkeypatch.setattr(cartesia_engine, "CartesiaVoice",
                        lambda key, voice, *a, **kw: types.SimpleNamespace(voice=voice))
    args = argparse.Namespace(voice="builtin", voice_id=None, voice_name=None, lang="en", speed=1.0,
                              voice_provider="cartesia", instant_phrases=False)
    voice = lt.Engine(args, FakeSink())._make_voice("soniox-key", None, lt.LagMeter())
    assert voice.voice == "blake" and asked == ["cartesia-key"]


# --- id 19: a handshake refused for a spent balance is fatal ------------------------------------------

async def refusing_server(monkeypatch, status, body):
    """A websocket endpoint that refuses every handshake with `status` and a JSON `body`; (server, attempts)."""
    attempts = []

    def process_request(connection, request):
        attempts.append(1)
        return connection.respond(status, json.dumps(body))

    async def handler(ws):
        pytest.fail("the handshake must be refused")

    server = await serve(handler, "127.0.0.1", 0, process_request=process_request)
    monkeypatch.setattr(lt, "URL", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/openai")
    return server, attempts


def openai_channel():
    return lt.Channel("Я", "en", asyncio.Queue(), [], "me")


async def test_a_spent_balance_at_the_handshake_is_fatal(monkeypatch):
    error = {"error": {"message": "You exceeded your current quota", "type": "insufficient_quota",
                       "code": "insufficient_quota"}}
    server, attempts = await refusing_server(monkeypatch, HTTPStatus.TOO_MANY_REQUESTS, error)
    sink = FakeSink()
    try:
        with pytest.raises(lt.Fatal) as fatal:
            await asyncio.wait_for(lt.run_channel(openai_channel(), KEY, None, sink), 5)
    finally:
        server.close()
    assert "HTTP 429" in str(fatal.value) and "Пополни баланс" in str(fatal.value)
    assert len(attempts) == 1  # not retried


async def test_an_ordinary_rate_limit_is_retried_after_a_pause(monkeypatch):
    server, attempts = await refusing_server(monkeypatch, HTTPStatus.TOO_MANY_REQUESTS,
                                             {"error": {"code": "rate_limit_exceeded"}})
    assert lt.RATE_LIMIT_DELAY >= 5  # not every 2 s at a server that asked to slow down
    monkeypatch.setattr(lt, "RATE_LIMIT_DELAY", 0.05)
    sink = FakeSink()
    task = asyncio.create_task(lt.run_channel(openai_channel(), KEY, None, sink))
    try:
        await until(lambda: len(attempts) >= 2, what="a second attempt")
        assert not task.done()
    finally:
        await stop(task)
        server.close()
    assert sink.statuses and all(text == "HTTP 429, переподключение…" for _, text, _ in sink.statuses)


async def test_other_refused_handshakes_are_still_retried_after_2_seconds(monkeypatch):
    server, attempts = await refusing_server(monkeypatch, HTTPStatus.BAD_GATEWAY, {"error": "bad gateway"})
    sink = FakeSink()
    task = asyncio.create_task(lt.run_channel(openai_channel(), KEY, None, sink))
    try:
        await until(lambda: attempts and sink.statuses, what="the first attempt")
        assert sink.statuses[0][1] == "HTTP 502, переподключение…"
        assert not task.done()
    finally:
        await stop(task)
        server.close()


async def test_a_refused_key_stays_fatal(monkeypatch):
    server, _ = await refusing_server(monkeypatch, HTTPStatus.UNAUTHORIZED, {"error": {"code": "invalid_api_key"}})
    try:
        with pytest.raises(lt.Fatal, match="HTTP 401"):
            await asyncio.wait_for(lt.run_channel(openai_channel(), KEY, None, FakeSink()), 5)
    finally:
        server.close()


# --- ids 22, 23: the .env file ------------------------------------------------------------------------

def env_bytes():
    return lt.ENV_FILE.read_bytes().replace(b"\r\n", b"\n")  # Windows text mode writes CRLF


def test_save_api_key_keeps_the_other_lines_and_replaces_its_own(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    lt.ENV_FILE.write_text("# my keys\nSONIOX_API_KEY=soniox-1\n\nOPENAI_API_KEY=old\nCARTESIA_API_KEY=c-1\n",
                           encoding="utf-8")
    lt.save_api_key("new")
    assert lt.ENV_FILE.read_text(encoding="utf-8") == (
        "# my keys\nSONIOX_API_KEY=soniox-1\n\nCARTESIA_API_KEY=c-1\nOPENAI_API_KEY=new\n")
    assert os.environ["OPENAI_API_KEY"] == "new"
    assert not lt.ENV_FILE.with_name(lt.ENV_FILE.name + ".tmp").exists()


def test_a_write_that_fails_halfway_leaves_the_env_file_as_it_was(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    lt.ENV_FILE.write_text("SONIOX_API_KEY=soniox-1\nOPENAI_API_KEY=old\n", encoding="utf-8")
    before = env_bytes()

    def dies_halfway(self, data, *args, **kwargs):  # the disk fills up, the process is killed
        with open(self, "w", encoding="utf-8") as f:
            f.write(data[:10])
        raise OSError("No space left on device")

    monkeypatch.setattr(type(lt.ENV_FILE), "write_text", dies_halfway)
    with pytest.raises(OSError):
        lt.save_api_key("new")
    assert env_bytes() == before
    assert not lt.ENV_FILE.with_name(lt.ENV_FILE.name + ".tmp").exists()
    assert "OPENAI_API_KEY" not in os.environ or os.environ["OPENAI_API_KEY"] != "new"


@pytest.mark.parametrize("key", ["abc\ndef", "abc\r\ndef", "abc\rdef", "abc\n"])
def test_a_key_with_a_line_break_is_refused(monkeypatch, key):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    lt.ENV_FILE.write_text("SONIOX_API_KEY=soniox-1\n", encoding="utf-8")
    with pytest.raises(ValueError) as error:
        lt.save_api_key(key)
    assert "abc" not in str(error.value)  # a key is never repeated in a message
    assert env_bytes() == b"SONIOX_API_KEY=soniox-1\n" and "OPENAI_API_KEY" not in os.environ


def test_a_key_saved_without_an_env_file_creates_it(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    lt.save_api_key("fresh")
    assert env_bytes() == b"OPENAI_API_KEY=fresh\n" and lt.load_api_key() == "fresh"


def test_an_env_file_saved_with_a_bom_is_read(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    lt.ENV_FILE.write_bytes(b"\xef\xbb\xbfOPENAI_API_KEY=sk-bom\nSONIOX_API_KEY=so-1\n")
    assert lt.load_api_key() == "sk-bom" and lt.load_api_key("SONIOX_API_KEY") == "so-1"
    lt.save_api_key("sk-new", "SONIOX_API_KEY")
    assert lt.load_api_key() == "sk-bom" and lt.load_api_key("SONIOX_API_KEY") == "sk-new"


def test_an_env_file_in_a_legacy_code_page_does_not_crash(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SONIOX_API_KEY", raising=False)
    comment = "# ключи для звонков".encode("cp1251")
    lt.ENV_FILE.write_bytes(comment + b"\nOPENAI_API_KEY=sk-x\n")
    assert lt.load_api_key() == "sk-x"
    assert lt.load_api_key("SONIOX_API_KEY") is None
    lt.save_api_key("so-1", "SONIOX_API_KEY")  # the line it cannot read is kept byte for byte
    assert env_bytes() == comment + b"\nOPENAI_API_KEY=sk-x\nSONIOX_API_KEY=so-1\n"
    assert lt.load_api_key("SONIOX_API_KEY") == "so-1"


def test_load_api_key_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    lt.ENV_FILE.write_bytes(b"\xff\xfe\x00garbage")
    assert lt.load_api_key() == "from-env"


# --- id 25: a malformed frame is skipped, not fatal ----------------------------------------------------

MALFORMED = [
    "not json at all {",
    "[1, 2, 3]",
    '"just a string"',
    "42",
    json.dumps({"type": "session.output_audio.delta"}),
    json.dumps({"type": "session.output_audio.delta", "delta": "AAAAA"}),  # bad base64 padding
    json.dumps({"type": "session.output_audio.delta", "delta": 123}),
    json.dumps({"type": "session.input_transcript.delta"}),
    json.dumps({"type": "session.output_transcript.delta", "delta": None}),
    json.dumps({"type": "session.output_transcript.delta", "delta": ["x"]}),
    json.dumps({"type": "error", "error": "boom"}),
    json.dumps({"type": "error", "error": {"code": ["insufficient_quota"]}}),  # an unhashable code
    b"\xff\xfe binary frame",
]


async def test_malformed_frames_are_skipped_and_the_session_goes_on(ws_server):
    pcm = b"\x10\x00" * 240

    async def handler(ws):
        await ws.recv()  # session.update
        for frame in MALFORMED:
            await ws.send(frame)
        await ws.send(json.dumps({"type": "session.input_transcript.delta", "delta": "Привет"}))
        await ws.send(json.dumps({"type": "session.output_audio.delta", "delta": b64(pcm)}))
        await ws.send(json.dumps({"type": "session.output_transcript.delta", "delta": "Hello"}))

    ws_server.handler = handler
    sink, fed = FakeSink(), []
    ch = lt.Channel("Я", "en", asyncio.Queue(), [types.SimpleNamespace(feed=fed.append)], "me")
    await asyncio.wait_for(lt.run_session(ch, KEY, None, sink), 5)
    assert fed == [pcm]
    assert [c[0] for c in sink.captions] == ["me_src", "me_dst"]
    assert [c[2] for c in sink.captions] == ["Привет", "Hello"]
    assert not any(marker in note for note in sink.notes for marker in ("not json", "AAAAA", "xff", "123"))
