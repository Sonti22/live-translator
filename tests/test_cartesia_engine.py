"""Cartesia voice for the Soniox engine (cartesia_engine.CartesiaVoice, list_voices) against local mocks."""
import asyncio
import json
import threading

import pytest

import cartesia_engine
import soniox_engine
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


def request(cid, transcript, cont=False, speed=None, buffer=150):
    msg = {"model_id": "sonic-3.6", "transcript": transcript, "voice": {"mode": "id", "id": VOICE_ID},
           "language": "en", "context_id": cid, "output_format": cartesia_engine.FORMAT, "continue": cont,
           "max_buffer_delay_ms": buffer if cont else 0}  # partials may end mid-word; the clause end goes at once
    if speed:
        msg["generation_config"] = {"speed": speed}
    return msg


def contexts(msgs):
    return list(dict.fromkeys(m["context_id"] for m in msgs))


def make_voice(sink, played, **kwargs):
    kwargs.setdefault("delivery", "fast")
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
    assert [m["max_buffer_delay_ms"] for m in msgs] == [150, 150, 0]  # no choppy per-token generation


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


async def test_a_clause_the_server_goes_silent_on_does_not_hold_back_the_next(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 2)
        first, second = contexts(msgs)
        await ws.send(json.dumps({"type": "error", "done": True, "status_code": 500, "title": "Internal error",
                                  "message": "Something went wrong."}))  # names no context
        await ws.send(chunk(second, b"B1"))
        await ws.send(done(second))
        await read_until(ws, msgs, lambda m: sum(x.get("transcript") == "One." for x in m) == 2)  # the first, again
        await ws.send(chunk(msgs[-1]["context_id"], b"A1"))
        await ws.send(done(msgs[-1]["context_id"]))
        await ws.wait_closed()

    monkeypatch.setattr(cartesia_engine.CartesiaVoice, "STALL", 0.3)
    monkeypatch.setattr(cartesia_engine.CartesiaVoice, "TICK", 0.05)
    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.", end=True)
        await voice.say("Two.", end=True)
        await until(lambda: len(played) == 2, what="both clauses heard")
    finally:
        await stop(task)
    first = contexts(msgs)[0]
    assert played == [b"A1", b"B1"]  # in the order said, not stuck behind the lost one
    assert {"context_id": first, "cancel": True} in msgs and msgs[-1] == request(msgs[-1]["context_id"], "One.")
    assert sink.notes == ["[Мой голос] Internal error: Something went wrong."]


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
    body = {"data": voices, "has_more": False} if paginated else voices
    http_server.routes[("GET", "/voices?limit=100")] = (200, body)
    assert cartesia_engine.list_voices(KEY, None) == [
        {"name": "Katie", "gender": "female", "description": "Friendly", "id": "v1", "language": "en"},
        {"name": "v2", "gender": "", "description": "", "id": "v2", "language": "de"},
    ]
    req = http_server.requests[-1]
    assert req.headers["X-API-Key"] == KEY and req.headers["Cartesia-Version"] == voice_clone.CARTESIA_VERSION


def test_default_voice_is_a_male_english_one(http_server):
    http_server.routes[("GET", "/voices?limit=100")] = (200, [
        {"id": "f", "name": "Katie", "gender": "feminine", "language": "en"},
        {"id": "d", "name": "Klaus", "gender": "masculine", "language": "de"},
        {"id": "m", "name": "Blake", "gender": "masculine", "language": "en"}])
    assert cartesia_engine.default_voice(KEY, None) == "m"
    http_server.routes[("GET", "/voices?limit=100")] = (200, [{"id": "d", "name": "Klaus", "language": "de"}])
    assert cartesia_engine.default_voice(KEY, None) is None


@pytest.mark.parametrize("status, message", [(401, "CARTESIA_API_KEY"), (403, "CARTESIA_API_KEY"), (500, "HTTP 500")])
def test_list_voices_errors(http_server, status, message):
    http_server.routes[("GET", "/voices?limit=100")] = (status, {"error": "nope"})
    with pytest.raises(CloneError, match=message):
        cartesia_engine.list_voices(KEY, None)


# --- deliveries ----------------------------------------------------------------------

@pytest.mark.parametrize("delivery, buffer", [("fast", 150), ("balanced", 200), ("natural", 400)])
async def test_text_still_coming_in_is_buffered_as_long_as_the_delivery_is_patient(ws_server, delivery, buffer):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 3)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [], delivery=delivery)
    voice.FLUSH = 0.05
    task = await run_voice(voice, sink)
    try:
        await voice.say("My name is")
        await voice.say(" Suren,")
        await until(lambda: len(msgs) == 3, what="the end of the clause")
    finally:
        await stop(task)
    cid = msgs[0]["context_id"]
    assert msgs == [request(cid, "My name is", True, buffer=buffer), request(cid, " Suren,", True, buffer=buffer),
                    request(cid, "", False)]  # what ends the clause goes at once


@pytest.mark.parametrize("delivery, contexts_used", [("fast", 2), ("balanced", 1), ("natural", 1)])
async def test_a_comma_ends_a_context_only_when_fast(ws_server, delivery, contexts_used):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 2)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [], delivery=delivery)
    task = await run_voice(voice, sink)
    try:
        await soniox_engine._speak(voice, "Hello,", False, None)
        await soniox_engine._speak(voice, " world.", False, None)
        await until(lambda: len(msgs) == 2, what="both chunks")
    finally:
        await stop(task)
    buffer = cartesia_engine.BUFFER_MS[delivery]
    if contexts_used == 2:
        first, second = contexts(msgs)
        assert msgs == [request(first, "Hello,"), request(second, " world.")]
    else:
        cid = contexts(msgs)[0]  # one context: the voice keeps its intonation across the comma
        assert msgs == [request(cid, "Hello,", True, buffer=buffer), request(cid, " world.")]
