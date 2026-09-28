"""Cartesia voice for the Soniox engine (cartesia_engine.CartesiaVoice, list_voices) against local mocks."""
import asyncio
import json
import threading

import pytest

import cartesia_engine
import voice_clone
from mocks import FakeSink, b64, read_until, stop, until
from voice_clone import CloneError

KEY = "cartesia-test-key"
VOICE_ID = "voice-abc"
CONNECTED = ("Мой голос", "подключено", True)


def chunk(cid, pcm):
    return json.dumps({"type": "chunk", "context_id": cid, "data": b64(pcm), "done": False, "status_code": 206})


def done(cid):
    return json.dumps({"type": "done", "context_id": cid, "done": True, "status_code": 206})


def error(cid, status, code, title, message):
    return json.dumps({"type": "error", "context_id": cid, "done": True, "status_code": status,
                       "error_code": code, "title": title, "message": message})


def request(cid, transcript, cont=False, speed=None):
    msg = {"model_id": "sonic-3.6", "transcript": transcript, "voice": {"mode": "id", "id": VOICE_ID},
           "language": "en", "context_id": cid, "output_format": cartesia_engine.FORMAT, "continue": cont,
           "max_buffer_delay_ms": 0}
    if speed:
        msg["generation_config"] = {"speed": speed}
    return msg


def contexts(msgs):
    return list(dict.fromkeys(m["context_id"] for m in msgs))


def make_voice(sink, played, **kwargs):
    return cartesia_engine.CartesiaVoice(KEY, VOICE_ID, "en", played.append, None, sink, **kwargs)


async def run_voice(voice, sink):
    task = asyncio.create_task(voice.run())
    await until(lambda: sink.statuses, what="Cartesia connected")
    return task


async def test_a_clause_is_one_request(ws_server):
    msgs, seen = [], {}

    async def handler(ws):
        seen["path"], seen["key"] = ws.request.path, ws.request.headers.get("X-API-Key")
        await read_until(ws, msgs, lambda m: len(m) == 1)
        cid = msgs[0]["context_id"]
        await ws.send(chunk(cid, b"A1"))
        await ws.send(json.dumps({"type": "flush_done", "context_id": cid, "done": False, "flush_done": True}))
        await ws.send(chunk(cid, b"A2"))
        await ws.send(done(cid))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played, first_audio = FakeSink(), [], []
    voice = make_voice(sink, played, on_first_audio=lambda: first_audio.append(1))
    task = await run_voice(voice, sink)
    try:
        assert voice.current is None and not voice.live  # no warm stream
        await voice.say("Hello there.", end=True)
        await until(lambda: not voice.order and not voice.live, what="the clause heard")
    finally:
        await stop(task)
    cid = msgs[0]["context_id"]
    assert msgs == [request(cid, "Hello there.")]
    assert seen == {"path": "/cartesia?cartesia_version=test", "key": KEY}
    assert played == [b"A1", b"A2"] and first_audio == [1]
    assert sink.statuses == [CONNECTED] and sink.notes == []


async def test_streamed_text_is_continued_and_closed(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 3)
        await ws.wait_closed()

    monkeypatch.setattr(cartesia_engine.CartesiaVoice, "FLUSH", 0.05)
    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [], speed=1.1)
    task = await run_voice(voice, sink)
    try:
        await voice.say("My name is")
        await voice.say(" Suren,")
        await until(lambda: len(msgs) == 3, what="the end of the clause")
    finally:
        await stop(task)
    cid = msgs[0]["context_id"]
    assert msgs == [request(cid, "My name is", True, speed=1.1), request(cid, " Suren,", True, speed=1.1),
                    request(cid, "", False, speed=1.1)]


async def test_clauses_are_heard_in_order(ws_server):
    msgs, release = [], threading.Event()

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 2)
        first, second = contexts(msgs)
        await ws.send(chunk(second, b"B1"))
        await ws.send(chunk(first, b"A1"))
        await until(release.is_set, what="release")
        await ws.send(done(first))
        await ws.send(chunk(second, b"B2"))
        await ws.send(done(second))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.", end=True)
        await voice.say("Two.", end=True)
        await until(lambda: played == [b"A1"] and voice.streams[voice.order[1]].buf == b"B1", what="first audio")
        release.set()
        await until(lambda: not voice.order, what="both clauses heard")
    finally:
        await stop(task)
    assert played == [b"A1", b"B1", b"B2"]


async def test_a_missing_voice_is_reported_and_skipped(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 2)
        first, second = contexts(msgs)
        await ws.send(error(first, 404, "invalid_voice_id", "Voice not found", "No voice abc."))
        await ws.send(chunk(second, b"B1"))
        await ws.send(done(second))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.", end=True)
        await voice.say("Two.", end=True)
        await until(lambda: not voice.order, what="both clauses done")
    finally:
        await stop(task)
    assert played == [b"B1"]
    assert sink.statuses[-1] == ("Мой голос", "клон недоступен — запиши голос заново", False)
    assert sink.notes == ["[Мой голос] Voice not found: No voice abc."]


async def test_a_busy_server_gets_the_clause_again(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 1)
        await ws.send(error(msgs[0]["context_id"], 429, "rate_limited", "Too many requests", "Slow down."))
        await read_until(ws, msgs, lambda m: len(m) == 2)
        await ws.send(chunk(msgs[1]["context_id"], b"A1"))
        await ws.send(done(msgs[1]["context_id"]))
        await ws.wait_closed()

    monkeypatch.setattr(cartesia_engine.CartesiaVoice, "RETRY", 0.05)
    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.", end=True)
        await until(lambda: played, what="the clause after a retry")
    finally:
        await stop(task)
    first, again = contexts(msgs)
    assert msgs[1] == request(again, "One.") and sink.notes == []


async def test_a_rejected_key_is_fatal(ws_server):
    ws_server.reject = 401
    with pytest.raises(CloneError, match="CARTESIA_API_KEY"):
        await asyncio.wait_for(make_voice(FakeSink(), []).run(), 5)


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
        await voice.say("One.", end=True)
        await voice.say("Two")
        first, second = voice.order
        await voice.cancel_all()
        assert not (voice.order or voice.streams or voice.live) and voice.current is None
        await until(lambda: sum("cancel" in m for m in msgs) == 2, what="cancel messages")
    finally:
        await stop(task)
    assert msgs[-2:] == [{"context_id": first, "cancel": True}, {"context_id": second, "cancel": True}]


async def test_preview_speed(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 1)
        await ws.send(chunk(msgs[0]["context_id"], b"ab"))
        await ws.send(done(msgs[0]["context_id"]))
        await ws.wait_closed()

    ws_server.handler = handler
    assert await asyncio.wait_for(voice_clone.speak_once(KEY, VOICE_ID, "en", "Hi", None, speed=1.2), 5) == b"ab"
    assert msgs[0]["generation_config"] == {"speed": 1.2}


# --- REST -------------------------------------------------------------------------

@pytest.mark.parametrize("paginated", [True, False])
def test_list_voices(http_server, paginated):
    voices = [{"id": "v1", "name": "Katie", "gender": "feminine", "description": "Friendly", "language": "en"},
              {"id": "v2", "name": "", "gender": None, "description": None, "language": "de"}]
    http_server.routes[("GET", "/voices?limit=100")] = (200, {"data": voices, "has_more": False} if paginated else voices)
    assert cartesia_engine.list_voices(KEY, None) == [
        {"name": "Katie", "gender": "female", "description": "Friendly", "id": "v1"},
        {"name": "v2", "gender": "", "description": "", "id": "v2"},
    ]
    req = http_server.requests[-1]
    assert req.headers["X-API-Key"] == KEY and req.headers["Cartesia-Version"] == voice_clone.CARTESIA_VERSION


@pytest.mark.parametrize("status, message", [(401, "CARTESIA_API_KEY"), (403, "CARTESIA_API_KEY"), (500, "HTTP 500")])
def test_list_voices_errors(http_server, status, message):
    http_server.routes[("GET", "/voices?limit=100")] = (status, {"error": "nope"})
    with pytest.raises(CloneError, match=message):
        cartesia_engine.list_voices(KEY, None)
