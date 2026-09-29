"""Inworld TTS voice (InworldVoice, speak_once, REST voices) against local mock servers."""
import asyncio
import base64
import json
import threading

import pytest

import inworld_engine
import soniox_engine
from mocks import FakeSink, b64, read_until, stop, until
from voice_clone import CloneError

KEY = "inworld-test-key"
CONNECTED = ("Мой голос", "подключено", True)


def result(cid, **fields):
    return json.dumps({"result": {"contextId": cid, **fields, "status": {"code": 0, "message": "", "details": []}}})


def chunk(cid, pcm):
    return result(cid, audioChunk={"audioContent": b64(pcm)})


def closed(cid):
    return result(cid, contextClosed={})


def failure(cid, code, message):
    return json.dumps({"result": {"contextId": cid, "status": {"code": code, "message": message, "details": []}}})


def create(cid, speed=None, voice="Clive"):
    audio = {"audio_encoding": "PCM", "sample_rate_hertz": 24000}
    if speed:
        audio["speaking_rate"] = speed
    return {"context_id": cid, "create": {"voice_id": voice, "model_id": "inworld-tts-2-flash",
                                          "audio_config": audio, "language": "en-US"}}


def send_text(cid, text, flush=False):
    return {"context_id": cid, "send_text": {"text": text, "flush_context": {}} if flush else {"text": text}}


def close(cid):
    return {"context_id": cid, "close_context": {}}


def contexts(msgs):
    return list(dict.fromkeys(m["context_id"] for m in msgs))


def make_voice(sink, played, **kwargs):
    kwargs.setdefault("delivery", "fast")
    return inworld_engine.InworldVoice(KEY, "Clive", "en", played.append, None, sink, **kwargs)


async def run_voice(voice, sink):
    task = asyncio.create_task(voice.run())
    await until(lambda: sink.statuses, what="Inworld connected")
    return task


def test_urls_can_be_overridden():
    assert inworld_engine.TTS_URL.endswith("/inworld-tts") and inworld_engine.API_URL.startswith("http://127.0.0.1")
    assert inworld_engine.locale("en") == "en-US" and inworld_engine.locale("de") == "de-DE"
    assert inworld_engine.locale("en-GB") == "en-GB"


async def test_a_clause_is_create_text_and_close_back_to_back(ws_server):
    msgs, seen = [], {}

    async def handler(ws):
        seen["path"], seen["auth"] = ws.request.path, ws.request.headers.get("Authorization")
        await read_until(ws, msgs, lambda m: len(m) == 3)
        cid = msgs[0]["context_id"]
        await ws.send(result(cid, contextCreated={"voiceId": "Clive"}))
        await ws.send(chunk(cid, b"A1"))
        await ws.send(chunk(cid, b"A2"))
        await ws.send(result(cid, flushCompleted={}))
        await ws.send(closed(cid))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played, first_audio = FakeSink(), [], []
    voice = make_voice(sink, played, on_first_audio=lambda: first_audio.append(1))
    task = await run_voice(voice, sink)
    try:
        assert voice.current is None and not voice.live  # no warm stream: nothing to open ahead of time
        await voice.say("Hello there.", end=True)
        await until(lambda: not voice.order, what="the clause heard")
    finally:
        await stop(task)
    cid = msgs[0]["context_id"]
    assert msgs == [create(cid), send_text(cid, "Hello there.", flush=True), close(cid)]
    assert seen == {"path": "/inworld-tts", "auth": f"Basic {KEY}"}
    assert played == [b"A1", b"A2"] and first_audio == [1]
    assert sink.statuses == [CONNECTED] and sink.notes == []


async def test_streamed_text_is_flushed_and_closed_at_the_end(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 5)
        await ws.wait_closed()

    monkeypatch.setattr(inworld_engine.InworldVoice, "FLUSH", 0.05)
    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [], speed=1.1)
    task = await run_voice(voice, sink)
    try:
        await voice.say("My name is")
        await voice.say(" Suren,")
        await until(lambda: len(msgs) == 5, what="the end of the clause")
    finally:
        await stop(task)
    cid = msgs[0]["context_id"]
    assert msgs == [create(cid, speed=1.1), send_text(cid, "My name is"), send_text(cid, " Suren,"),
                    {"context_id": cid, "flush_context": {}}, close(cid)]


async def test_clauses_are_heard_in_order(ws_server):
    msgs, release = [], threading.Event()

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 6)
        first, second = contexts(msgs)
        await ws.send(chunk(second, b"B1"))
        await ws.send(chunk(first, b"A1"))
        await until(release.is_set, what="release")
        await ws.send(closed(first))
        await ws.send(chunk(second, b"B2"))
        await ws.send(closed(second))
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
        await read_until(ws, msgs, lambda m: len(m) == 6)
        first, second = contexts(msgs)
        await ws.send(failure(first, 5, "Voice not found."))
        await ws.send(chunk(second, b"B1"))
        await ws.send(closed(second))
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
    assert sink.notes == ["[Мой голос] Voice not found."]


@pytest.mark.parametrize("code, message", [
    (8, "Too many contexts."),
    (8, "Quota exceeded for requests per minute."),  # a rate limit, not an empty balance
    (14, "Service unavailable."),  # a server hiccup: the clause nobody heard yet goes again
    (13, "Internal error."),
])
async def test_a_busy_server_gets_the_clause_again(ws_server, monkeypatch, code, message):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 3)
        await ws.send(failure(msgs[0]["context_id"], code, message))
        await read_until(ws, msgs, lambda m: len(m) == 6)
        await ws.send(chunk(msgs[3]["context_id"], b"A1"))
        await ws.send(closed(msgs[3]["context_id"]))
        await ws.wait_closed()

    monkeypatch.setattr(inworld_engine.InworldVoice, "RETRY", 0.05)
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
    assert msgs[3:] == [create(again), send_text(again, "One.", flush=True), close(again)]
    assert sink.notes == ([] if code == 8 else [f"[Мой голос] {message}"])


@pytest.mark.parametrize("reply", [
    json.dumps({"error": {"code": 16, "message": "Invalid API key."}}),
    failure("x", 7, "Permission denied."),
    failure("x", 8, "Character quota exceeded for this billing period."),  # out of credits: not "busy"
])
async def test_a_rejected_key_is_fatal(ws_server, reply):
    async def handler(ws):
        await ws.recv()
        await ws.send(reply)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    await voice.say("Hi.", end=True)
    with pytest.raises(CloneError, match="INWORLD_API_KEY"):
        await asyncio.wait_for(task, 5)


async def test_a_rejected_handshake_is_fatal(ws_server):
    ws_server.reject = 401
    with pytest.raises(CloneError, match="INWORLD_API_KEY"):
        await asyncio.wait_for(make_voice(FakeSink(), []).run(), 5)


async def test_cancel_all_closes_open_contexts(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 6)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.", end=True)  # already closed: nothing more to send for it
        await voice.say("Two")
        await voice.cancel_all()
        await until(lambda: len(msgs) == 6, what="close of the open context")
    finally:
        await stop(task)
    second = contexts(msgs)[1]
    assert msgs[-1] == close(second) and not voice.order and not voice.live


async def test_speak_once(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 3)
        cid = msgs[0]["context_id"]
        await ws.send(result(cid, contextCreated={}))
        await ws.send(chunk(cid, b"ab"))
        await ws.send(chunk(cid, b"cd"))
        await ws.send(closed(cid))
        await ws.wait_closed()

    ws_server.handler = handler
    pcm = await asyncio.wait_for(inworld_engine.speak_once(KEY, "Dennis", "en", "Hello", None,
                                                           model="inworld-tts-2", speed=1.2), 5)
    assert pcm == b"abcd"
    cid = msgs[0]["context_id"]
    expected = create(cid, speed=1.2, voice="Dennis")
    expected["create"]["model_id"] = "inworld-tts-2"
    assert msgs == [expected, send_text(cid, "Hello", flush=True), close(cid)]


async def test_speak_once_error(ws_server):
    async def handler(ws):
        cid = json.loads(await ws.recv())["context_id"]
        await ws.send(failure(cid, 5, "Unknown voice."))
        await ws.wait_closed()

    ws_server.handler = handler
    with pytest.raises(CloneError, match="Unknown voice."):
        await asyncio.wait_for(inworld_engine.speak_once(KEY, "Nobody", "en", "Hello", None), 5)


# --- REST -------------------------------------------------------------------------

def test_create_voice(http_server):
    voice = {"voiceId": "ws__me", "langCode": "RU_RU"}
    http_server.routes[("POST", "/voices/v1/voices:clone")] = (200, {"voice": voice, "audioSamplesValidated": []})
    wav = b"RIFF\x24\x00\x00\x00WAVE" + bytes(range(256))
    assert inworld_engine.create_voice(KEY, wav, None) == "ws__me"
    [req] = http_server.requests
    assert req.headers["Authorization"] == f"Basic {KEY}" and req.headers["Content-Type"] == "application/json"
    body = json.loads(req.body)
    assert body["displayName"].startswith("Live Translator ") and "langCode" not in body and "languageCode" not in body
    assert [base64.b64decode(s["audioData"]) for s in body["voiceSamples"]] == [wav]


def test_create_voice_reports_a_rejected_sample(http_server):
    http_server.routes[("POST", "/voices/v1/voices:clone")] = (200, {"audioSamplesValidated": [
        {"errors": [{"text": "Audio is too short."}], "warnings": []}]})
    with pytest.raises(CloneError, match="Audio is too short."):
        inworld_engine.create_voice(KEY, b"RIFF", None)


def test_delete_voice(http_server):
    http_server.routes[("DELETE", "/voices/v1/voices/ws__me")] = (200, {})
    inworld_engine.delete_voice(KEY, "ws__me", None)
    inworld_engine.delete_voice(KEY, "gone", None)  # 404: already deleted, fine
    assert [r.path for r in http_server.requests] == ["/voices/v1/voices/ws__me", "/voices/v1/voices/gone"]


def test_list_voices(http_server):
    http_server.routes[("GET", "/voices/v1/voices?pageSize=1000")] = (200, {"voices": [
        {"voiceId": "Clive", "displayName": "Clive", "gender": "male", "description": "British, calm"},
        {"voiceId": "ws__me", "displayName": "Live Translator 2026-09-28"},
    ], "nextPageToken": ""})
    assert inworld_engine.list_voices(KEY, None) == [
        {"name": "Clive", "gender": "male", "description": "British, calm", "id": "Clive"},
        {"name": "Live Translator 2026-09-28", "gender": "", "description": "", "id": "ws__me"},
    ]
    assert http_server.requests[-1].headers["Authorization"] == f"Basic {KEY}"


@pytest.mark.parametrize("status, message", [(401, "INWORLD_API_KEY"), (403, "INWORLD_API_KEY"), (500, "HTTP 500")])
def test_rest_errors(http_server, status, message):
    http_server.routes[("GET", "/voices/v1/voices?pageSize=1000")] = (status, {"error": "nope"})
    with pytest.raises(CloneError, match=message):
        inworld_engine.list_voices(KEY, None)


# --- deliveries ----------------------------------------------------------------------

@pytest.mark.parametrize("delivery, contexts_used", [("fast", 2), ("balanced", 1), ("natural", 1)])
async def test_a_comma_ends_a_context_only_when_fast(ws_server, delivery, contexts_used):
    msgs, count = [], 6 if contexts_used == 2 else 4  # create, text, close each; or one create, two texts, close

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == count)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [], delivery=delivery)
    task = await run_voice(voice, sink)
    try:
        await soniox_engine._speak(voice, "Hello,", False, None)
        await soniox_engine._speak(voice, " world.", False, None)
        await until(lambda: len(msgs) == count, what="both chunks")
    finally:
        await stop(task)
    if contexts_used == 2:
        first, second = contexts(msgs)
        assert msgs == [create(first), send_text(first, "Hello,", flush=True), close(first),
                        create(second), send_text(second, " world.", flush=True), close(second)]
    else:
        cid = contexts(msgs)[0]
        assert msgs == [create(cid), send_text(cid, "Hello,"), send_text(cid, " world.", flush=True), close(cid)]
