"""Cartesia cloned voice (CloneVoice, speak_once, create_clone) and https_request over plain HTTP."""
import asyncio
import contextlib
import json
import threading

import pytest

import voice_clone
from mocks import FakeSink, b64, form_fields, free_port, read_until, stop, until

KEY = "cartesia-test-key"
VOICE_ID = "voice-abc"
FORMAT = {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000}


def chunk(cid, pcm):
    return json.dumps({"type": "chunk", "context_id": cid, "data": b64(pcm), "done": False})


def done(cid):
    return json.dumps({"type": "done", "context_id": cid, "done": True})


def request(transcript, cid, cont, buffer_ms=500):
    return {"model_id": "sonic-3.6", "transcript": transcript, "voice": VOICE_ID, "language": "en",
            "context_id": cid, "output_format": FORMAT, "continue": cont, "max_buffer_delay_ms": buffer_ms}


def make_voice(sink, played, on_first_audio=None):
    return voice_clone.CloneVoice(KEY, VOICE_ID, "en", played.append, None, 500, sink, on_first_audio)


async def run_voice(voice, sink):
    task = asyncio.create_task(voice.run())
    await until(lambda: sink.statuses, what="Cartesia connected")
    return task


async def test_requests_and_playback_order(ws_server):
    msgs, seen, release = [], {}, threading.Event()

    async def handler(ws):
        seen["path"], seen["key"] = ws.request.path, ws.request.headers.get("X-API-Key")
        await read_until(ws, msgs, lambda m: sum(not x["continue"] for x in m) == 2)
        first, second = msgs[0]["context_id"], msgs[-1]["context_id"]
        await ws.send(chunk(second, b"B1"))  # phrase 2 is ready first...
        await ws.send(chunk(first, b"A1"))
        await until(release.is_set, what="release")
        await ws.send(done(first))  # ...but is heard only after phrase 1 is done
        await ws.send(chunk(second, b"B2"))
        await ws.send(done(second))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played, first_audio = FakeSink(), [], []
    voice = make_voice(sink, played, lambda: first_audio.append(1))
    task = await run_voice(voice, sink)
    try:
        await voice.say("Hello")
        await voice.say(" there.")  # sentence end closes the phrase
        await voice.say("Bye!")
        await until(lambda: len(first_audio) == 2, what="audio of both phrases")
        assert played == [b"A1"]
        release.set()
        await until(lambda: not voice.order, what="both phrases done")
    finally:
        await stop(task)

    first, second = msgs[0]["context_id"], msgs[-1]["context_id"]
    assert first != second
    assert msgs == [request("Hello", first, True), request(" there.", first, True), request("", first, False),
                    request("Bye!", second, True), request("", second, False)]
    assert played == [b"A1", b"B1", b"B2"]
    assert seen == {"path": "/cartesia?cartesia_version=test", "key": KEY}
    assert sink.statuses == [("Мой голос", "подключено", True)] and sink.notes == []


async def test_watchdog_closes_an_idle_phrase(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 2)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    voice.IDLE = 0.1
    task = await run_voice(voice, sink)
    watchdog = asyncio.create_task(voice.watchdog())
    try:
        await voice.say("Hello")  # no sentence end
        await until(lambda: len(msgs) == 2, what="end of phrase")
    finally:
        await stop(watchdog)
        await stop(task)
    cid = msgs[0]["context_id"]
    assert msgs == [request("Hello", cid, True), request("", cid, False)]
    assert voice.context is None


async def test_error_skips_the_phrase(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 4)
        first, second = msgs[0]["context_id"], msgs[2]["context_id"]
        await ws.send(chunk(second, b"B1"))
        await ws.send(json.dumps({"type": "error", "context_id": first, "error_code": "voice_not_found",
                                  "title": "Voice not found", "message": "No voice abc."}))
        await ws.send(done(second))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.")
        await voice.say("Two.")
        await until(lambda: not voice.order, what="both phrases finished")
    finally:
        await stop(task)
    assert played == [b"B1"]
    assert sink.notes == ["[Мой голос] Voice not found: No voice abc."]
    assert sink.statuses[-1] == ("Мой голос", "клон не найден — запиши голос заново", False)


async def test_cancel_all(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: sum("cancel" in x for x in m) == 2)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.")
        await voice.say("Two")
        first, second = voice.order
        await voice.cancel_all()
        assert voice.context is None and voice.text == ""
        assert not (voice.order or voice.pending or voice.finished or voice.heard)
        await until(lambda: sum("cancel" in m for m in msgs) == 2, what="cancel messages")
    finally:
        await stop(task)
    assert msgs[-2:] == [{"context_id": first, "cancel": True}, {"context_id": second, "cancel": True}]


async def test_text_said_while_reconnecting_is_spoken_once_connected(ws_server):
    connections, msgs = [], []

    async def handler(ws):
        connections.append(ws)
        if len(connections) == 1:
            await ws.close()  # the VPN drops
            return
        await read_until(ws, msgs, lambda m: len(m) == 1)
        await ws.send(chunk(msgs[0]["context_id"], b"A1"))
        await ws.send(done(msgs[0]["context_id"]))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        await until(lambda: voice.ws is None, what="the drop")
        await voice.say("Hello")  # gpt-realtime-translate goes on while Cartesia reconnects
        await voice.say(" there.")
        await until(lambda: played, what="the phrase after the reconnect")
    finally:
        await stop(task)
    assert msgs == [request("Hello there.", msgs[0]["context_id"], False)]  # all of it, closed
    assert played == [b"A1"] and len(connections) == 2


async def test_a_reconnect_keeps_the_phrases_not_heard_yet(ws_server):
    connections, first, second = [], [], []

    async def handler(ws):
        connections.append(ws)
        if len(connections) > 1:
            await read_until(ws, second, lambda m: len(m) == 1)
            await ws.send(chunk(second[0]["context_id"], b"A1"))
            await ws.send(done(second[0]["context_id"]))
            await ws.wait_closed()
            return
        await read_until(ws, first, lambda m: sum(not x["continue"] for x in m) == 2)
        await ws.send(chunk(first[-1]["context_id"], b"B1"))  # the second phrase arrived in full...
        await ws.send(done(first[-1]["context_id"]))
        await ws.close()  # ...then the VPN drops before any audio of the first one

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.")
        await voice.say("Two.")
        await until(lambda: len(played) == 2, what="both phrases")
    finally:
        await stop(task)
    assert second == [request("One.", first[0]["context_id"], False)]  # only the one not heard goes again
    assert played == [b"A1", b"B1"] and sink.notes == []


async def test_a_phrase_queued_through_a_long_outage_is_dropped(ws_server, monkeypatch):
    connections, msgs = [], []

    async def handler(ws):
        connections.append(ws)
        if len(connections) == 1:
            await ws.close()
            return
        async for raw in ws:
            msgs.append(json.loads(raw))

    monkeypatch.setattr(voice_clone.CloneVoice, "TTL", 0.05)
    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        await until(lambda: voice.ws is None, what="the drop")
        await voice.say("Old news.")
        await until(lambda: sink.notes, what="the stale phrase dropped")
        await asyncio.sleep(0.1)
    finally:
        await stop(task)
    assert sink.notes == ["[Мой голос] не озвучено (не было связи): Old news."]
    assert msgs == [] and not voice.order


async def test_rejected_key(ws_server):
    ws_server.reject = 401
    with pytest.raises(voice_clone.CloneError, match="CARTESIA_API_KEY"):
        await asyncio.wait_for(make_voice(FakeSink(), []).run(), 5)


async def test_speak_once(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 1)
        cid = msgs[0]["context_id"]
        await ws.send(chunk(cid, b"ab"))
        await ws.send(chunk(cid, b"cd"))
        await ws.send(done(cid))
        await ws.wait_closed()

    ws_server.handler = handler
    pcm = await asyncio.wait_for(voice_clone.speak_once(KEY, VOICE_ID, "en", "Hello", None), 5)
    assert pcm == b"abcd"
    assert msgs[0]["transcript"] == "Hello" and msgs[0]["continue"] is False and msgs[0]["voice"] == VOICE_ID


# --- REST -------------------------------------------------------------------------

def test_https_request_over_plain_http(http_server):
    http_server.routes[("POST", "/echo?x=1&y=2")] = (202, b"accepted")
    status, body = voice_clone.https_request("POST", f"{voice_clone.TTS_API}/echo?x=1&y=2",
                                             {"X-Test": "yes"}, b"\x00\x01payload", None)
    assert (status, body) == (202, b"accepted")
    [req] = http_server.requests
    assert (req.method, req.path, req.body) == ("POST", "/echo?x=1&y=2", b"\x00\x01payload")
    assert req.headers["X-Test"] == "yes"
    assert req.headers["Host"] == voice_clone.TTS_API.removeprefix("http://")


def test_https_request_get_error_status(http_server):
    status, body = voice_clone.https_request("GET", f"{voice_clone.TTS_API}/missing", {}, None, None)
    assert status == 404 and json.loads(body) == {"error": "no route"}


def test_https_request_goes_through_an_http_proxy(http_server):
    target = f"http://127.0.0.1:{free_port()}/v1/tts-models"  # nothing listens there: only the proxy can answer
    with contextlib.suppress(OSError):
        voice_clone.https_request("GET", target, {}, None, voice_clone.TTS_API)  # the mock plays the proxy
    assert http_server.requests, "the proxy was never contacted"


def test_create_clone(http_server):
    http_server.routes[("POST", "/voices/clone")] = (200, {"id": "clone-1"})
    wav = b"RIFF\x00\x00WAVE" + bytes(range(256))
    assert voice_clone.create_clone(KEY, wav, "Мой голос", "ru", None) == "clone-1"
    [req] = http_server.requests
    assert req.headers["X-API-Key"] == KEY
    assert req.headers["Cartesia-Version"] == voice_clone.CARTESIA_VERSION
    fields = form_fields(req.headers["Content-Type"], req.body)
    assert {name: value for name, (_, value) in fields.items() if name != "clip"} == {
        "name": "Мой голос".encode(), "language": b"ru", "description": b"Live Translator voice"}
    head, data = fields["clip"]
    assert 'filename="voice.wav"' in head and data == wav


@pytest.mark.parametrize("status, message", [(401, "отклонила ключ"), (403, "отклонила ключ"), (500, "HTTP 500")])
def test_create_clone_errors(http_server, status, message):
    http_server.routes[("POST", "/voices/clone")] = (status, {"error": "nope"})
    with pytest.raises(voice_clone.CloneError, match=message):
        voice_clone.create_clone(KEY, b"RIFF", "x", "ru", None)
