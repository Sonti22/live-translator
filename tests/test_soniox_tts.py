"""Soniox TTS: SonioxVoice and speak_once against a mock Soniox TTS websocket."""
import asyncio
import json
import threading

import pytest

import soniox_engine
import voice_clone
from mocks import FakeSink, b64, read_until, stop, until

KEY = "soniox-test-key"
CONNECTED = ("Мой голос", "подключено", True)


def audio(sid, pcm, end=False):
    msg = {"audio": b64(pcm), "stream_id": sid}
    if end:
        msg["audio_end"] = True
    return json.dumps(msg)


def terminated(sid):
    return json.dumps({"terminated": True, "stream_id": sid})


def configs(msgs):
    return [m for m in msgs if "model" in m]


def config(sid, voice="Adrian"):
    return {"api_key": KEY, "stream_id": sid, "model": "tts-rt-v2", "voice": voice, "language": "en",
            "audio_format": "pcm_s16le", "sample_rate": 24000}


def text(sid, value, end=False):
    return {"stream_id": sid, "text": value, "text_end": end}


def make_voice(sink, played, **kwargs):
    return soniox_engine.SonioxVoice(KEY, "Adrian", "en", played.append, None, sink, **kwargs)


async def run_voice(voice, sink):
    task = asyncio.create_task(voice.run())
    await until(lambda: sink.statuses, what="TTS connected")
    return task


def test_config_speed_only_when_changed():
    assert soniox_engine.SonioxVoice(KEY, "Adrian", "en", None, None, None)._config("s1") == config("s1")
    fast = soniox_engine.SonioxVoice(KEY, "voice-id-1", "en", None, None, None, speed=1.2)._config("s1")
    assert fast == {**config("s1", "voice-id-1"), "speed": 1.2}


async def test_prewarmed_stream_reused_and_utterances_play_in_order(ws_server):
    msgs, release = [], threading.Event()

    async def handler(ws):
        await read_until(ws, msgs, lambda m: sum(bool(x.get("text_end")) for x in m) == 2)
        first, second = [c["stream_id"] for c in configs(msgs)][:2]
        await ws.send(audio(second, b"B1"))  # utterance 2 is ready first...
        await ws.send(audio(first, b"A1"))
        await until(release.is_set, what="release")
        await ws.send(audio(first, b"A2", end=True))
        await ws.send(terminated(first))  # ...but is heard only after utterance 1 terminated
        await ws.send(audio(second, b"B2", end=True))
        await ws.send(terminated(second))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played, first_audio = FakeSink(), [], []
    voice = make_voice(sink, played, on_first_audio=lambda: first_audio.append(1))
    task = await run_voice(voice, sink)
    try:
        prewarmed = voice.current
        assert prewarmed is not None
        await voice.say("Hello")
        await voice.say(" there")
        await voice.end_utterance()
        await voice.say("Bye")
        second = voice.current
        await voice.end_utterance()
        warm = voice.current  # a fresh stream is opened right away for the next utterance
        assert warm not in (None, prewarmed, second)
        await until(lambda: len(first_audio) == 2, what="audio of both utterances")
        assert played == [b"A1"]
        assert voice.streams[second].buf == b"B1"  # held back behind utterance 1
        release.set()
        await until(lambda: list(voice.order) == [warm], what="both utterances finished")
    finally:
        await stop(task)

    assert msgs == [
        config(prewarmed),  # opened on connect, before any text
        text(prewarmed, "Hello"), text(prewarmed, " there"), text(prewarmed, "", end=True),
        config(second), text(second, "Bye"), text(second, "", end=True),
    ]  # the mock stops recording after the 2nd text_end; the warm stream is checked above
    assert second != prewarmed
    assert played == [b"A1", b"A2", b"B1", b"B2"]
    assert not voice.streams and voice.current is None
    assert sink.statuses == [CONNECTED] and sink.notes == []


async def test_say_with_end_closes_the_clause_in_one_message(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(configs(m)) == 2)  # the clause, then the next warm stream
        await ws.send(audio(msgs[0]["stream_id"], b"A1", end=True))
        await ws.send(terminated(msgs[0]["stream_id"]))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played, events = FakeSink(), [], []
    voice = make_voice(sink, played)
    voice.trace = lambda event, sid, **info: events.append(event)
    task = await run_voice(voice, sink)
    try:
        first = voice.current
        await voice.say("My name is Suren,", end=True)
        await until(lambda: played, what="audio")
        assert voice.current == configs(msgs)[1]["stream_id"]  # the next clause finds a warm stream
    finally:
        await stop(task)
    assert msgs[1] == text(first, "My name is Suren,", end=True)  # no separate empty text_end, no FLUSH wait
    assert events[:6] == ["open", "text", "open", "first_audio", "first_audible", "audio_end"]

async def test_end_utterance_keeps_an_unused_prewarmed_stream(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 2)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        prewarmed = voice.current
        await voice.end_utterance()  # <end> without any translated text
        await voice.say("Hi")
        await until(lambda: len(msgs) == 2, what="text")
    finally:
        await stop(task)
    assert msgs == [config(prewarmed), text(prewarmed, "Hi")]


async def test_cancel_all(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: sum("cancel" in x for x in m) == 2)
        first, second = [c["stream_id"] for c in configs(msgs)][:2]
        await ws.send(audio(first, b"late"))  # replies still in flight for the cancelled streams
        await ws.send(terminated(first))
        await ws.send(audio(second, b"late"))
        await ws.send(terminated(second))
        await read_until(ws, msgs, lambda m: m[-1].get("text") == "Next")
        await ws.send(audio(configs(msgs)[-1]["stream_id"], b"C1"))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        first = voice.current
        await voice.say("Hello")
        await voice.end_utterance()
        await voice.say("again")
        second = voice.current
        await voice.cancel_all()
        assert voice.current is None
        assert not (voice.order or voice.streams)
        await voice.say("Next")
        await until(lambda: played, what="audio of the next utterance")
    finally:
        await stop(task)
    assert [m for m in msgs if "cancel" in m] == [{"stream_id": first, "cancel": True},
                                                 {"stream_id": second, "cancel": True}]
    third = configs(msgs)[-1]["stream_id"]
    assert third not in (first, second)
    assert played == [b"C1"]


async def test_idle_prewarmed_stream_timeout_is_dropped_silently(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 1)
        expired = msgs[0]["stream_id"]
        await ws.send(json.dumps({"stream_id": expired, "error_code": 408, "error_type": "request_timeout",
                                  "error_message": "Request timeout."}))
        await ws.send(terminated(expired))  # Soniox terminates a stream after its error
        await read_until(ws, msgs, lambda m: len(m) == 3)
        await ws.send(audio(msgs[1]["stream_id"], b"N1"))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        expired = voice.current
        await until(lambda: voice.current is None and not voice.order, what="expired stream dropped")
        await voice.say("Hi")
        await until(lambda: played, what="audio")
    finally:
        await stop(task)
    fresh = msgs[1]["stream_id"]
    assert fresh != expired
    assert msgs[1:] == [config(fresh), text(fresh, "Hi")]
    assert played == [b"N1"]  # not stuck behind the expired stream
    assert sink.notes == [] and sink.statuses == [CONNECTED]


async def test_voice_error_on_a_used_stream_is_reported(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 2)
        sid = msgs[0]["stream_id"]
        await ws.send(json.dumps({"stream_id": sid, "error_code": 400, "error_type": "voice_not_found",
                                  "error_message": "Voice not found."}))
        await ws.send(terminated(sid))
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        await voice.say("Hello")
        await until(lambda: not voice.order, what="failed stream dropped")
    finally:
        await stop(task)
    assert sink.statuses == [CONNECTED, ("Мой голос", "клон недоступен — запиши голос заново", False)]
    assert sink.notes == ["[Мой голос] Voice not found."]


async def test_auth_error_is_fatal(ws_server):
    async def handler(ws):
        sid = json.loads(await ws.recv())["stream_id"]
        await ws.send(json.dumps({"stream_id": sid, "error_code": 401, "error_type": "unauthenticated",
                                  "error_message": "Invalid API key."}))
        await ws.wait_closed()

    ws_server.handler = handler
    voice = make_voice(FakeSink(), [])
    with pytest.raises(soniox_engine.SonioxFatal, match="SONIOX_API_KEY"):
        await asyncio.wait_for(voice.run(), 5)
    assert voice.ws is None and not voice.order


async def test_keepalive(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 3)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    voice.KEEPALIVE = 0.05
    task = await run_voice(voice, sink)
    try:
        await until(lambda: len(msgs) == 3, what="keepalives")
    finally:
        await stop(task)
    assert msgs[1:] == [{"keep_alive": True}] * 2


async def test_speak_once(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 2)
        sid = msgs[0]["stream_id"]
        await ws.send(audio(sid, b"ab"))
        await ws.send(audio(sid, b"cd", end=True))
        await ws.send(terminated(sid))
        await ws.wait_closed()

    ws_server.handler = handler
    pcm = await asyncio.wait_for(soniox_engine.speak_once(KEY, "Adrian", "en", "Hello", None), 5)
    assert pcm == b"abcd"
    sid = msgs[0]["stream_id"]
    assert msgs == [config(sid), text(sid, "Hello", end=True)]


async def test_speak_once_error(ws_server):
    async def handler(ws):
        sid = json.loads(await ws.recv())["stream_id"]
        await ws.send(json.dumps({"stream_id": sid, "error_code": 400, "error_type": "invalid_voice",
                                  "error_message": "Unknown voice."}))
        await ws.wait_closed()

    ws_server.handler = handler
    with pytest.raises(voice_clone.CloneError, match="Unknown voice."):
        await asyncio.wait_for(soniox_engine.speak_once(KEY, "Nobody", "en", "Hello", None), 5)


async def test_expired_warm_stream_is_replaced_but_throttled(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            msgs.append(msg)
            if "model" in msg:  # every warm stream expires right away (no text within the timeout)
                await ws.send(json.dumps({"stream_id": msg["stream_id"], "error_code": 408,
                                          "error_type": "request_timeout", "error_message": "timeout"}))
                await ws.send(terminated(msg["stream_id"]))

    monkeypatch.setattr(soniox_engine.SonioxVoice, "REWARM", 0.3)
    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        await asyncio.sleep(1.0)
    finally:
        await stop(task)
    opened = len(configs(msgs))
    assert 2 <= opened <= 5, opened  # re-warmed after expiry, at most one per REWARM seconds
    assert sink.notes == []  # expired unused streams are silent
