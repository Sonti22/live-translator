"""Soniox TTS: SonioxVoice and speak_once against a mock Soniox TTS websocket."""
import asyncio
import json
import threading
import time

import numpy as np
import pytest

import phrases
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
    kwargs.setdefault("delivery", "fast")  # the tests below are of how it speaks fast; deliveries have their own
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


# --- say flow and reliability (plan 3.1, 3.2) ----------------------------------------

def text_ends(msgs):
    return sum(bool(m.get("text_end")) for m in msgs)


def busy(sid):
    return json.dumps({"stream_id": sid, "error_code": 429, "error_type": "too_many_requests",
                       "error_message": "Too many concurrent streams."})


@pytest.mark.parametrize("said, spoken", [
    ("My name is Сурен.", "My name is."), (" Сурен,", ""), ("Сурен", ""), ("Pythonа developer", "Python developer"),
    ("Hello", "Hello"), (" there", " there"), ("?", "?"), ("", ""),
])
def test_speakable_strips_cyrillic(said, spoken):
    assert soniox_engine.speakable(said) == spoken


async def test_russian_never_reaches_tts_but_the_clause_still_closes(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 2)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        first = voice.current
        await voice.say("My name is")
        await voice.say(" Сурен", end=True)  # nothing speakable left: the clause just closes
        await voice.say("I live in Москва.", end=True)
        await until(lambda: text_ends(msgs) == 2, what="both clauses")
    finally:
        await stop(task)
    second = configs(msgs)[1]["stream_id"]
    assert [m for m in msgs if "text" in m] == [text(first, "My name is"), text(first, "", end=True),
                                               text(second, "I live in.", end=True)]
    assert soniox_engine.SonioxVoice.FLUSH == 0.1


async def test_a_russian_word_at_the_end_of_a_clause_does_not_stop_the_flush(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 1)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        first = voice.current
        await voice.say("I live in")
        await voice.say(" Москве")  # a name left untranslated: nothing to speak, the clause still closes
        await until(lambda: text_ends(msgs) == 1, timeout=0.5, what="the clause closed by FLUSH")
    finally:
        await stop(task)
    assert msgs[1:] == [text(first, "I live in"), text(first, "", end=True)]


def test_connections_notice_a_dead_vpn_within_seconds(monkeypatch):
    seen = {}
    monkeypatch.setattr(soniox_engine, "connect", lambda url, **kw: seen.update(kw, url=url))
    make_voice(FakeSink(), [])._connect()
    assert (seen["url"], seen["ping_interval"], seen["ping_timeout"]) == (soniox_engine.TTS_URL, 5, 5)


async def test_playback_moves_on_at_audio_end_not_at_terminated(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 2)
        first, second = [c["stream_id"] for c in configs(msgs)][:2]
        await ws.send(audio(second, b"B1"))
        await ws.send(audio(first, b"A1", end=True))  # no terminated for the first stream yet
        await ws.wait_closed()

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
    assert played == [b"A1", b"B1"]


async def test_a_clause_waits_for_a_free_slot(ws_server):
    msgs, release = [], threading.Event()

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 3)
        await until(release.is_set, what="release")
        first = msgs[0]["stream_id"]
        await ws.send(audio(first, b"A1", end=True))
        await ws.send(terminated(first))  # frees a slot
        await read_until(ws, msgs, lambda m: text_ends(m) == 4)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        for clause in ("One.", "Two.", "Three.", "Four."):
            await voice.say(clause, end=True)
        await until(lambda: text_ends(msgs) == 3, what="three clauses")
        await asyncio.sleep(0.1)
        assert len(configs(msgs)) == 3 and len(voice.live) == voice.MAX_STREAMS
        fourth = voice.order[-1]
        assert voice.streams[fourth].text == "Four." and fourth not in voice.live
        release.set()
        await until(lambda: text_ends(msgs) == 4, what="the fourth clause")
    finally:
        await stop(task)
    assert msgs[-2:] == [config(fourth), text(fourth, "Four.", end=True)]  # one message once a slot is free


async def test_a_refused_stream_keeps_its_place_in_line(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 2)
        first, second = [c["stream_id"] for c in configs(msgs)][:2]
        await ws.send(json.dumps({"stream_id": first, "error_code": 429, "error_type": "too_many_requests",
                                  "error_message": "Too many concurrent streams."}))
        await ws.send(terminated(first))
        await ws.send(audio(second, b"B1", end=True))
        await ws.send(terminated(second))
        await read_until(ws, msgs, lambda m: text_ends(m) == 3)  # the first clause, again
        await ws.send(audio(msgs[-1]["stream_id"], b"A1", end=True))
        await ws.wait_closed()

    monkeypatch.setattr(soniox_engine.SonioxVoice, "RETRY", 0.05)
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
    retried = msgs[-1]["stream_id"]
    assert retried not in [c["stream_id"] for c in configs(msgs)[:3]]
    assert msgs[-2:] == [config(retried), text(retried, "One.", end=True)]
    assert played == [b"A1", b"B1"]  # still in the order it was said
    assert sink.notes == []


async def test_a_refused_clause_takes_the_slot_of_the_unused_warm_stream(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(configs(m)) == 2)  # the clause, then the next warm stream
        first = msgs[0]["stream_id"]
        await ws.send(busy(first))
        await ws.send(terminated(first))
        await read_until(ws, msgs, lambda m: text_ends(m) == 2)  # no other stream ends meanwhile
        await ws.wait_closed()

    monkeypatch.setattr(soniox_engine.SonioxVoice, "RETRY", 0.05)
    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.", end=True)
        await until(lambda: text_ends(msgs) == 2, what="the clause, again")
    finally:
        await stop(task)
    first, warm, retried = [c["stream_id"] for c in configs(msgs)]
    assert voice.limit == 1  # the server took one stream at a time...
    assert msgs[-3:] == [{"stream_id": warm, "cancel": True}, config(retried), text(retried, "One.", end=True)]


async def test_after_a_refusal_fewer_streams_go_at_a_time(ws_server, monkeypatch):
    msgs, release = [], threading.Event()

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(configs(m)) == 3)  # two clauses and the warm stream
        await ws.send(busy(msgs[-1]["stream_id"]))  # a third stream at once is one too many
        await ws.send(terminated(msgs[-1]["stream_id"]))
        await until(release.is_set, what="release")
        await ws.send(audio(msgs[0]["stream_id"], b"A1", end=True))
        await ws.send(terminated(msgs[0]["stream_id"]))
        await read_until(ws, msgs, lambda m: text_ends(m) == 3)
        await ws.wait_closed()

    monkeypatch.setattr(soniox_engine.SonioxVoice, "RETRY", 0.02)
    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.", end=True)
        await voice.say("Two.", end=True)
        await until(lambda: len(voice.live) == 2 and voice.current is None, what="the refusal")
        await voice.say("Three.", end=True)
        third = voice.order[-1]
        await asyncio.sleep(0.2)
        assert text_ends(msgs) == 2 and third not in voice.live  # it waits although MAX_STREAMS is 3
        release.set()
        await until(lambda: text_ends(msgs) == 3, what="the third clause")
    finally:
        await stop(task)
    assert msgs[-2:] == [config(third), text(third, "Three.", end=True)]  # once a stream terminated


def test_the_lowered_stream_limit_is_lifted_after_a_while(monkeypatch):
    voice = make_voice(FakeSink(), [])
    voice.ws, voice.live = object(), {"a", "b"}
    voice._refused("c")
    assert voice.limit == 2 and not voice._free_slot()
    later = time.monotonic() + voice.RELIMIT
    monkeypatch.setattr(soniox_engine.time, "monotonic", lambda: later)
    assert voice._free_slot()  # a busy moment on the server does not cost a slot for the whole call


async def test_a_clause_the_server_keeps_refusing_is_skipped_and_reported(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            msgs.append(msg)
            if msg.get("text") == "One.":  # refused, every time
                await ws.send(busy(msg["stream_id"]))
                await ws.send(terminated(msg["stream_id"]))
            elif msg.get("text") == "Two.":
                await ws.send(audio(msg["stream_id"], b"B1", end=True))
                await ws.send(terminated(msg["stream_id"]))

    monkeypatch.setattr(soniox_engine.SonioxVoice, "RETRY", 0.02)
    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.", end=True)
        await until(lambda: sink.notes, what="the clause given up")
        await voice.say("Two.", end=True)
        await until(lambda: played, what="the next clause")
    finally:
        await stop(task)
    assert sum(m.get("text") == "One." for m in msgs) == 1 + voice.RETRIES
    assert sink.notes == ["[Мой голос] не озвучено (сервер занят): One."]
    assert sink.statuses == [CONNECTED, ("Мой голос", "сервер перегружен — фраза пропущена", False), CONNECTED]
    assert played == [b"B1"]  # the next clause is not stuck behind it


async def test_a_warm_stream_that_expires_as_its_text_arrives_is_sent_again(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 1)
        warm = msgs[0]["stream_id"]  # it timed out on the server while the text was on its way
        await ws.send(json.dumps({"stream_id": warm, "error_code": 408, "error_type": "request_timeout",
                                  "error_message": "Request timeout."}))
        await ws.send(terminated(warm))
        await read_until(ws, msgs, lambda m: text_ends(m) == 2)
        await ws.send(audio(msgs[-1]["stream_id"], b"A1", end=True))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        warm = voice.current
        await voice.say("Hello.", end=True)
        await until(lambda: played, what="the clause heard")
    finally:
        await stop(task)
    retried = msgs[-1]["stream_id"]
    assert retried != warm and msgs[-2:] == [config(retried), text(retried, "Hello.", end=True)]
    assert played == [b"A1"] and sink.notes == [] and voice.limit == voice.MAX_STREAMS  # not a busy server


@pytest.mark.parametrize("code", [500, 503])
async def test_a_server_error_before_the_clause_was_heard_sends_it_again(ws_server, monkeypatch, code):
    msgs, arrived = [], {}

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 1)
        first = msgs[1]["stream_id"]
        arrived["error"] = time.monotonic()
        await ws.send(json.dumps({"stream_id": first, "error_code": code, "error_type": "internal_error",
                                  "error_message": "Service unavailable."}))
        await ws.send(terminated(first))
        while text_ends(msgs) < 2:
            msgs.append(json.loads(await ws.recv()))
            arrived.setdefault(msgs[-1]["stream_id"], time.monotonic())
        await ws.send(audio(msgs[-1]["stream_id"], b"A1", end=True))
        await ws.wait_closed()

    monkeypatch.setattr(soniox_engine.SonioxVoice, "RETRY", 0.2)
    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        first = voice.current
        await voice.say("I have five years of experience.", end=True)
        await until(lambda: played, what="the clause heard after the error")
    finally:
        await stop(task)
    retried = msgs[-1]["stream_id"]
    assert retried != first and msgs[-2:] == [config(retried), text(retried, "I have five years of experience.",
                                                                     end=True)]
    assert arrived[retried] - arrived["error"] >= voice.RETRY  # not all retries spent within one short outage
    assert played == [b"A1"] and sink.notes == ["[Мой голос] Service unavailable."]  # the server's reason


async def test_the_rest_of_a_clause_the_server_goes_silent_on_is_skipped(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 2)
        first, second = [c["stream_id"] for c in configs(msgs)][:2]
        await ws.send(audio(first, b"A1"))  # heard in part...
        await ws.send(json.dumps({"error_code": 500, "error_type": "internal_error",
                                  "error_message": "Internal error."}))  # ...then an error that names no stream
        await ws.send(audio(second, b"B1", end=True))
        await ws.send(terminated(second))
        await read_until(ws, msgs, lambda m: {"stream_id": first, "cancel": True} in m)
        await ws.wait_closed()

    for name, value in (("STALL", 0.3), ("TICK", 0.05)):
        monkeypatch.setattr(soniox_engine.SonioxVoice, name, value)
    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.", end=True)
        await voice.say("Two.", end=True)
        await until(lambda: len(played) == 2, what="the next clause")
        await until(lambda: any("cancel" in m for m in msgs), what="the lost stream cancelled")
    finally:
        await stop(task)
    assert played == [b"A1", b"B1"]
    assert [m for m in msgs if "cancel" in m] == [{"stream_id": configs(msgs)[0]["stream_id"], "cancel": True}]
    assert sink.notes == ["[Мой голос] Internal error.", "[Мой голос] не озвучено до конца (сервер не ответил): One."]


async def test_a_lost_clause_does_not_hold_back_the_answer_while_i_keep_talking(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        lost = None
        async for raw in ws:
            msg = json.loads(raw)
            msgs.append(msg)
            if msg.get("text_end") and lost is None:
                lost = msg["stream_id"]
                await ws.send(json.dumps({"error_code": 500, "error_type": "internal_error",
                                          "error_message": "Internal error."}))  # names no stream
            elif msg.get("text_end"):
                await ws.send(audio(msg["stream_id"], b"A1" if msg["text"] == "One." else b"B1", end=True))
                await ws.send(terminated(msg["stream_id"]))

    for name, value in (("STALL", 0.3), ("TICK", 0.05)):
        monkeypatch.setattr(soniox_engine.SonioxVoice, name, value)
    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        start = time.monotonic()
        await voice.say("One.", end=True)
        while not played and time.monotonic() - start < 3.0:  # later clauses keep getting their audio meanwhile
            await asyncio.sleep(0.1)
            await voice.say("And more.", end=True)
        heard = time.monotonic() - start
    finally:
        await stop(task)
    assert played[:1] == [b"A1"] and heard < 3.0, (played, heard)  # went again while I was still talking


async def test_text_said_offline_goes_out_first_after_connecting(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 3)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    await voice.say("Hello.", end=True)  # the socket is not up yet
    queued = voice.order[0]
    task = await run_voice(voice, sink)
    try:
        await until(lambda: len(msgs) == 3, what="queued text and a warm stream")
    finally:
        await stop(task)
    assert msgs[:2] == [config(queued), text(queued, "Hello.", end=True)]
    assert msgs[2] == config(msgs[2]["stream_id"])  # the warm stream comes after the queued text


async def test_text_queued_offline_expires(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 1)
        await ws.wait_closed()

    monkeypatch.setattr(soniox_engine.SonioxVoice, "TTL", 0.05)
    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    await voice.say("Old news.", end=True)
    await asyncio.sleep(0.1)
    task = await run_voice(voice, sink)
    try:
        await until(lambda: msgs, what="the warm stream")
    finally:
        await stop(task)
    assert configs(msgs) == msgs and len(voice.order) <= 1
    assert sink.notes == ["[Мой голос] не озвучено (не было связи): Old news."]


async def test_a_clause_nobody_heard_is_sent_again_after_a_reconnect(ws_server):
    first_conn, second_conn = [], []

    async def handler(ws):
        if not first_conn:
            await read_until(ws, first_conn, lambda m: text_ends(m) == 1)
            await ws.close()  # the VPN drops before any audio
            return
        await read_until(ws, second_conn, lambda m: text_ends(m) == 1)
        await ws.send(audio(second_conn[-1]["stream_id"], b"A1", end=True))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        await voice.say("Hello.", end=True)
        await until(lambda: played, timeout=8, what="audio after the reconnect")
    finally:
        await stop(task)
    sid = first_conn[1]["stream_id"]
    assert first_conn[1] == text(sid, "Hello.", end=True)
    assert second_conn[:2] == [config(sid), text(sid, "Hello.", end=True)]
    assert played == [b"A1"]


async def test_audio_that_arrived_in_full_plays_without_waiting_for_the_reconnect(ws_server):
    msgs, connections = [], []

    async def handler(ws):
        connections.append(ws)
        if len(connections) > 1:
            await ws.wait_closed()
            return
        await read_until(ws, msgs, lambda m: text_ends(m) == 2)
        first, second = [c["stream_id"] for c in configs(msgs)][:2]
        await ws.send(audio(first, tone(50)))  # heard in part...
        await ws.send(audio(second, tone(40), end=True))  # ...while the next clause arrived in full
        await ws.close()  # the VPN drops

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.", end=True)
        await voice.say("Two.", end=True)
        await until(lambda: ms(played) == 50 + 40, timeout=0.8, what="the second clause")  # reconnecting takes 1 s+
        assert len(connections) == 1
    finally:
        await stop(task)


async def test_audio_that_waited_through_a_long_outage_is_dropped(ws_server, monkeypatch):
    connections = []

    async def handler(ws):
        connections.append(ws)
        msgs = []
        if len(connections) > 1:
            await ws.wait_closed()
            return
        await read_until(ws, msgs, lambda m: text_ends(m) == 2)
        await ws.send(audio(configs(msgs)[1]["stream_id"], tone(40), end=True))  # behind a clause not heard yet
        await ws.close()

    monkeypatch.setattr(soniox_engine.SonioxVoice, "TTL", 0.5)
    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played)
    task = await run_voice(voice, sink)
    try:
        await voice.say("One.", end=True)
        await voice.say("Two.", end=True)
        await until(lambda: len(sink.notes) == 2, timeout=8, what="both clauses dropped at the reconnect")
    finally:
        await stop(task)
    assert played == [] and not any(voice.streams[sid].text for sid in voice.order)  # a new warm stream at most
    assert sink.notes == ["[Мой голос] не озвучено (не было связи): One.",
                          "[Мой голос] не озвучено (не было связи): Two."]


async def test_a_short_answer_keeps_its_place_through_a_short_outage(cache):
    played = []
    voice = make_voice(FakeSink(), played, phrases=cache)
    head = voice._new_stream(1.0)  # a clause on its way, not heard yet
    head.text, head.ended, head.born = "Good question.", True, time.monotonic()
    voice.live.add(head.sid)
    voice.current = None
    await voice.say("Sure.", end=True)
    voice._reset()
    voice._drop_stale()
    assert [voice.streams[sid].text for sid in voice.order] == ["Good question.", "Sure."] and played == []


async def test_an_idle_connection_is_renewed_quietly(ws_server, monkeypatch):
    connections = []

    async def handler(ws):
        connections.append(time.monotonic())
        await ws.recv()  # the warm stream
        await ws.wait_closed()

    for name, value in (("RECYCLE", 0.2), ("TICK", 0.05), ("QUIET", 0.0)):
        monkeypatch.setattr(soniox_engine.SonioxVoice, name, value)
    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        await until(lambda: len(connections) == 2 and len(sink.statuses) == 2, what="a renewed connection")
    finally:
        await stop(task)
    assert connections[1] - connections[0] < 0.9  # no reconnect pause
    assert sink.statuses == [CONNECTED, CONNECTED] and sink.notes == []


# --- speed (plan 3.3) ----------------------------------------------------------------

def test_queued_seconds_counts_the_player_held_audio_and_text_not_voiced():
    voice = make_voice(FakeSink(), [], backlog=lambda: 0.4)
    heard = voice._new_stream(1.0)
    heard.text, heard.heard, heard.buf = "Hello.", True, bytes(4800)  # 0.1 s of audio held back
    waiting = voice._new_stream(1.25)
    waiting.text = "x" * 35  # 35 / (14 cps * 1.25) = 2 s
    assert voice.queued_seconds() == pytest.approx(0.4 + 0.1 + 2.0)


def test_speed_boost_switches_on_and_off_with_hysteresis():
    behind = [0.0]
    voice = make_voice(FakeSink(), [], speed=1.1, backlog=lambda: behind[0])
    speeds = []
    for seconds in (0.0, 1.6, 1.0, 0.6, 0.4, 1.4):
        behind[0] = seconds
        speeds.append(voice._clause_speed())
    assert speeds == [1.1, 1.25, 1.25, 1.25, 1.1, 1.1]
    assert make_voice(FakeSink(), [], speed=1.3, backlog=lambda: 5.0)._clause_speed() == 1.3  # capped
    assert make_voice(FakeSink(), [], speed_boost=False, backlog=lambda: 5.0)._clause_speed() == 1.0


async def test_far_behind_the_warm_stream_gives_way_to_a_faster_one(ws_server):
    msgs, behind = [], [0.0]

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(configs(m)) == 4)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [], speed=1.1, backlog=lambda: behind[0])
    task = await run_voice(voice, sink)
    try:
        warm = voice.current  # opened while I was on time
        behind[0] = 2.0
        await voice.say("I have a lot to say.", end=True)
        await until(lambda: len(configs(msgs)) == 3, what="the next warm stream")
        rewarmed = voice.current
        await voice.say("And more.", end=True)
        await until(lambda: len(configs(msgs)) == 4, what="a warm stream after the second clause")
    finally:
        await stop(task)
    boosted = configs(msgs)[1]["stream_id"]
    assert msgs[:6] == [{**config(warm), "speed": 1.1}, {"stream_id": warm, "cancel": True},
                        {**config(boosted), "speed": 1.25}, text(boosted, "I have a lot to say.", end=True),
                        {**config(rewarmed), "speed": 1.25}, text(rewarmed, "And more.", end=True)]
    assert configs(msgs)[3]["speed"] == 1.25  # while behind, warm streams are opened fast and used, not replaced


def answering(msgs):
    """A mock Soniox TTS that voices every clause at once."""
    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            msgs.append(msg)
            if msg.get("text_end"):
                await ws.send(audio(msg["stream_id"], b"A1", end=True))
                await ws.send(terminated(msg["stream_id"]))
    return handler


async def test_on_time_every_clause_uses_a_warm_stream_at_the_base_speed(ws_server):
    msgs = []
    ws_server.handler = answering(msgs)
    sink, played = FakeSink(), []
    voice = make_voice(sink, played, speed=1.1)  # nothing queued in the player
    task = await run_voice(voice, sink)
    try:
        await voice.say("I have worked there for three years.", end=True)  # unheard for a moment after its end
        await until(lambda: len(played) == 1, what="the first clause")
        await voice.say("And I liked it.", end=True)
        await until(lambda: len(played) == 2 and len(configs(msgs)) == 3, what="the second clause")
    finally:
        await stop(task)
    assert [c["speed"] for c in configs(msgs)] == [1.1] * 3 and not any("cancel" in m for m in msgs)


async def test_caught_up_a_fast_warm_stream_gives_way_to_one_at_the_base_speed(ws_server, monkeypatch):
    msgs, behind = [], [2.0]
    monkeypatch.setattr(soniox_engine.SonioxVoice, "TICK", 0.05)
    ws_server.handler = answering(msgs)
    sink, played = FakeSink(), []
    voice = make_voice(sink, played, speed=1.1, backlog=lambda: behind[0])
    task = await run_voice(voice, sink)
    try:
        await voice.say("I have a lot to say.", end=True)  # far behind: faster
        await until(lambda: len(configs(msgs)) == 3, what="a fast warm stream")
        behind[0] = 0.0  # caught up while I was silent
        await until(lambda: len(configs(msgs)) == 4, what="a warm stream at the base speed")
        await voice.say("Next.", end=True)
        await until(lambda: len(played) == 2, what="the next clause")
    finally:
        await stop(task)
    first, _, fast, base = [c["stream_id"] for c in configs(msgs)][:4]
    assert [c["speed"] for c in configs(msgs)][:4] == [1.1, 1.25, 1.25, 1.1]
    assert [m["stream_id"] for m in msgs if "cancel" in m] == [first, fast]
    assert text(base, "Next.", end=True) in msgs  # the next answer found it ready


async def test_speak_once_speed(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 2)
        await ws.send(audio(msgs[0]["stream_id"], b"ab", end=True))
        await ws.wait_closed()

    ws_server.handler = handler
    assert await asyncio.wait_for(soniox_engine.speak_once(KEY, "Adrian", "en", "Hi", None, speed=1.2), 5) == b"ab"
    assert msgs[0]["speed"] == 1.2


# --- silence trimming (plan 3.4) -----------------------------------------------------

def tone(ms, amp=5000):
    return np.full(ms * 24, amp, "<i2").tobytes()


def silence(ms):
    return bytes(ms * 48)


def ms(played):
    return sum(len(p) for p in played) // 48


def audio_end(sid):
    return json.dumps({"stream_id": sid, "audio_end": True})


async def test_the_silent_lead_of_a_clause_is_cut(ws_server):
    msgs, go_on = [], threading.Event()

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 1)
        sid = msgs[0]["stream_id"]
        await ws.send(audio(sid, silence(60)))
        await until(go_on.is_set, what="go on")
        await ws.send(audio(sid, silence(20) + tone(50), end=True))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played, first_audio, events = FakeSink(), [], [], []
    voice = make_voice(sink, played, on_first_audio=lambda: first_audio.append(1))
    voice.trace = lambda event, sid, **info: events.append(event)
    task = await run_voice(voice, sink)
    try:
        await voice.say("Hi.", end=True)
        await until(lambda: "first_audio" in events, what="the silent start")
        assert played == [] and first_audio == []  # nothing audible yet: the lag meter waits too
        go_on.set()
        await until(lambda: played, what="the sound")
    finally:
        await stop(task)
    assert ms(played) == 20 + 50  # 20 ms pre-roll before the sound
    assert first_audio == [1] and events.index("first_audio") < events.index("first_audible")


@pytest.mark.parametrize("clause, backlog, trim, heard", [
    ("Hello,", 0.5, True, 100 + 80),   # a comma keeps 80 ms of the pause
    ("Hello", 0.5, True, 100 + 50),    # a split without punctuation keeps 50 ms
    ("Hello.", 0.5, True, 250),        # a sentence keeps its pause
    ("Hello,", 0.1, True, 250),        # player nearly empty: nothing is held back, the pause already played
    ("Hello,", 0.5, False, 250),       # trimming switched off
])
async def test_the_pause_at_a_seam_is_shortened(ws_server, clause, backlog, trim, heard):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 2)
        first, second = [c["stream_id"] for c in configs(msgs)][:2]
        await ws.send(audio(first, tone(100) + silence(150)))
        await ws.send(audio_end(first))
        await ws.send(audio(second, tone(40), end=True))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played, backlog=lambda: backlog, trim=trim)
    task = await run_voice(voice, sink)
    try:
        await voice.say(clause, end=True)
        await voice.say("world.", end=True)
        await until(lambda: ms(played) >= heard + 40, what="both clauses")
        await asyncio.sleep(0.05)
    finally:
        await stop(task)
    assert ms(played) == heard + 40


async def test_a_late_period_ends_the_closed_clause_instead_of_opening_a_stream(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 2)
        first, second = [m["stream_id"] for m in msgs if m.get("text_end")]
        await ws.send(audio(first, tone(100) + silence(150)))
        await ws.send(audio_end(first))
        await ws.send(audio(second, tone(40), end=True))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played, backlog=lambda: 0.5)
    task = await run_voice(voice, sink)
    try:
        await voice.say("I work at Yandex")
        await until(lambda: text_ends(msgs) == 1, what="the clause closed by FLUSH")
        await voice.say(".", end=True)  # the period comes on its own, with the endpoint
        await voice.say("Next one.", end=True)
        await until(lambda: ms(played) >= 250 + 40, what="both clauses")
        await asyncio.sleep(0.05)
    finally:
        await stop(task)
    assert [m["text"] for m in msgs if "text" in m] == ["I work at Yandex", "", "Next one."]
    assert ms(played) == 250 + 40  # a sentence keeps its pause at the seam


async def test_a_late_symbol_the_voice_speaks_is_not_dropped(ws_server):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 2)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [])
    task = await run_voice(voice, sink)
    try:
        await voice.say("It grew by 50")
        await until(lambda: text_ends(msgs) == 1, what="the clause closed by FLUSH")
        await voice.say("%", end=True)  # "percent": a word without letters
        await until(lambda: text_ends(msgs) == 2, what="the symbol spoken")
    finally:
        await stop(task)
    assert [m["text"] for m in msgs if "text" in m] == ["It grew by 50", "", "%"]


async def test_nothing_is_held_back_while_the_clause_is_still_open(ws_server, monkeypatch):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 2)
        await ws.send(audio(msgs[0]["stream_id"], tone(300)))  # what it has so far; then it waits for more text
        await ws.wait_closed()

    monkeypatch.setattr(soniox_engine.SonioxVoice, "FLUSH", 10)  # the clause stays open
    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played, backlog=lambda: 0.5)
    task = await run_voice(voice, sink)
    try:
        await voice.say("I have been working")
        await until(lambda: ms(played) == 300, what="all audio so far")  # no gap moved into a word
    finally:
        await stop(task)


# --- stock phrases (plan 7) ----------------------------------------------------------

@pytest.fixture
def cache(tmp_path):
    ready = phrases.PhraseCache(tmp_path, "soniox|tts-rt-v2|Adrian|en|1.0")
    ready.store("Sure.", 0, silence(50) + tone(100))
    return ready


async def test_a_ready_short_answer_plays_at_once(ws_server, cache, monkeypatch):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == 1)
        await ws.wait_closed()

    monkeypatch.setattr(soniox_engine.SonioxVoice, "TICK", 10)  # no background rendering here
    ws_server.handler = handler
    sink, played, first_audio, events = FakeSink(), [], [], []
    voice = make_voice(sink, played, on_first_audio=lambda: first_audio.append(1), phrases=cache)
    voice.trace = lambda event, sid, **info: events.append(event)
    task = await run_voice(voice, sink)
    try:
        warm = voice.current
        await voice.say("Sure!", end=True)
        assert ms(played) == 20 + 100 and first_audio == [1]  # its silent lead is cut too
        assert voice.current == warm and list(voice.order) == [warm]  # the warm stream still waits
        await asyncio.sleep(0.1)
    finally:
        await stop(task)
    assert msgs == [config(warm)]  # no TTS request at all
    assert events == ["open", "clip", "first_audible"]


async def test_a_short_answer_waits_for_the_clause_before_it(ws_server, cache, monkeypatch):
    msgs, release = [], threading.Event()

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(configs(m)) == 2)
        await until(release.is_set, what="release")
        await ws.send(audio(msgs[0]["stream_id"], tone(30), end=True))
        await ws.wait_closed()

    monkeypatch.setattr(soniox_engine.SonioxVoice, "TICK", 10)
    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played, phrases=cache)
    task = await run_voice(voice, sink)
    try:
        await voice.say("Good question.", end=True)
        await voice.say("Sure.", end=True)
        assert played == [] and voice.streams[voice.order[1]].sid.startswith("clip:")
        assert voice.order[2] == voice.current  # before the unused warm stream
        release.set()
        await until(lambda: len(played) == 2, what="the clause, then the answer")
    finally:
        await stop(task)
    assert [ms([p]) for p in played] == [30, 120]


async def test_short_answers_only_as_a_whole_sentence_with_nothing_waiting(cache):
    played = []
    voice = make_voice(FakeSink(), played, phrases=cache)  # offline: a clip is local, it plays anyway
    await voice.say("Sure.", end=True)
    assert len(played) == 1
    await voice.say("Sure,", end=True)  # the sentence goes on
    await voice.say("Sure.", end=True)  # something is waiting in line
    assert [voice.streams[sid].text for sid in voice.order] == ["Sure,", "Sure."]
    await voice.cancel_all()
    await voice.say("I am")
    await voice.say(" sure.", end=True)  # the end of a longer utterance
    await voice.cancel_all()
    await voice.say("Sure.")  # not closed yet: more text may follow
    await voice.cancel_all()
    assert len(played) == 1


async def test_stock_phrases_are_rendered_in_the_background_while_idle(ws_server, cache, monkeypatch):
    msgs = []

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            msgs.append(msg)
            if not msg.get("text_end"):
                continue
            sid = msg["stream_id"]
            if msg["text"] == "Thanks.":
                await ws.send(json.dumps({"stream_id": sid, "error_code": 500, "error_type": "internal",
                                          "error_message": "Oops."}))
            else:
                await ws.send(audio(sid, b"Y1"))
                await ws.send(audio(sid, b"Y2", end=True))
            await ws.send(terminated(sid))

    monkeypatch.setattr(phrases, "PHRASES", {"Sure.": 1, "Yes.": 1, "Thanks.": 1})
    monkeypatch.setattr(soniox_engine.SonioxVoice, "TICK", 0.05)
    monkeypatch.setattr(soniox_engine.SonioxVoice, "QUIET", 0.0)
    ws_server.handler = handler
    sink, played, events = FakeSink(), [], []
    voice = make_voice(sink, played, phrases=cache)
    voice.trace = lambda event, sid, **info: events.append(event)
    task = await run_voice(voice, sink)
    try:
        await until(lambda: cache.next_missing() is None and not voice.renders, what="all phrases rendered")
    finally:
        await stop(task)
    assert cache.path("Yes.").read_bytes() == b"Y1Y2"
    assert ("Thanks.", 0) in cache.skipped  # a failed render is not retried in a loop
    rendered = [m["text"] for m in msgs if m.get("text_end")]
    assert rendered == ["Yes.", "Thanks."]  # one at a time, "Sure." was on disk already
    assert played == [] and sink.notes == [] and events == ["open"]  # silent, and invisible to the trace


async def test_a_render_the_server_refuses_is_not_resent_at_every_tick(ws_server, cache, monkeypatch):
    msgs = []

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            msgs.append(msg)
            if msg.get("text_end"):
                await ws.send(busy(msg["stream_id"]))
                await ws.send(terminated(msg["stream_id"]))

    monkeypatch.setattr(phrases, "PHRASES", {"Sure.": 1, "Yes.": 1})
    for name, value in (("TICK", 0.05), ("QUIET", 0.0), ("RETRY", 0.05)):
        monkeypatch.setattr(soniox_engine.SonioxVoice, name, value)
    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [], phrases=cache)
    task = await run_voice(voice, sink)
    try:
        await until(lambda: text_ends(msgs) == 1, what="a render")
        await asyncio.sleep(0.4)
    finally:
        await stop(task)
    assert text_ends(msgs) == 1  # the server is busy: not again for a while...
    assert cache.next_missing() == ("Yes.", 0) and sink.notes == []  # ...but the phrase is not given up


async def test_no_background_rendering_for_other_languages(ws_server, cache, monkeypatch):
    msgs = []

    async def handler(ws):
        async for raw in ws:
            msgs.append(json.loads(raw))

    monkeypatch.setattr(soniox_engine.SonioxVoice, "TICK", 0.05)
    monkeypatch.setattr(soniox_engine.SonioxVoice, "QUIET", 0.0)
    ws_server.handler = handler
    sink = FakeSink()
    voice = soniox_engine.SonioxVoice(KEY, "Adrian", "de", [].append, None, sink, phrases=cache)
    task = await run_voice(voice, sink)
    try:
        await asyncio.sleep(0.3)
    finally:
        await stop(task)
    assert len(msgs) == 1 and "model" in msgs[0]


# --- deliveries: fast, balanced, natural ---------------------------------------------

class Said:
    """A voice as _speak sees it: what it is told to say."""

    def __init__(self, **attrs):
        self.said = []
        self.__dict__.update(attrs)

    async def say(self, text, end=False):
        self.said.append((text, end))

    async def end_utterance(self):
        self.said.append(("", True))


def test_a_delivery_sets_how_eagerly_the_voice_speaks():
    assert soniox_engine.DELIVERIES == ("fast", "balanced", "natural")
    wanted = {"fast": (0.1, 1.25, 1.5, True), "balanced": (0.35, 1.1, 2.0, True), "natural": (0.6, None, None, False)}
    for delivery, (flush, boost, boost_on, boosts) in wanted.items():
        voice = make_voice(FakeSink(), [], delivery=delivery)
        assert voice.delivery == delivery and voice.FLUSH == flush and voice.speed_boost is boosts
        assert voice.closers == soniox_engine.CLOSERS[delivery]
        if boost:
            assert (voice.BOOST, voice.BOOST_ON, voice.BOOST_OFF) == (boost, boost_on, 0.5)
    cls = soniox_engine.SonioxVoice
    assert (cls.FLUSH, cls.BOOST, cls.BOOST_ON) == (0.1, 1.25, 1.5)  # the class keeps the fast constants


def test_the_delivery_is_balanced_unless_told_otherwise():
    plain = soniox_engine.SonioxVoice(KEY, "Adrian", "en", None, None, None)
    assert plain.delivery == "balanced" and plain.FLUSH == 0.35
    odd = soniox_engine.SonioxVoice(KEY, "Adrian", "en", None, None, None, delivery="loud")
    assert odd.delivery == "balanced"  # a bad setting must not break a call


@pytest.mark.parametrize("chunk, fast, patient", [
    ("Hello,", True, False), ("Hello;", True, False), ("Hello:", True, False),
    ("Hello.", True, True), ("Hello!", True, True), ("Hello?", True, True), ("Hello…", True, True),
    ("Hello", False, False), (" world ", False, False),
], ids=repr)
async def test_a_chunk_closes_the_clause_by_the_delivery(chunk, fast, patient):
    for delivery, closes in (("fast", fast), ("balanced", patient), ("natural", patient)):
        voice = Said(closers=make_voice(FakeSink(), [], delivery=delivery).closers)
        await soniox_engine._speak(voice, chunk, False, None)
        assert voice.said == [(chunk, closes)], delivery


async def test_the_endpoint_closes_the_clause_whatever_the_delivery():
    for delivery in soniox_engine.DELIVERIES:
        voice = Said(closers=make_voice(FakeSink(), [], delivery=delivery).closers)
        await soniox_engine._speak(voice, "Hello", True, None)
        assert voice.said == [("Hello", True)]
    plain = Said()  # a voice that knows no delivery closes as the fast one does
    await soniox_engine._speak(plain, "Hello,", False, None)
    assert plain.said == [("Hello,", True)]


@pytest.mark.parametrize("delivery, streams", [("fast", 2), ("balanced", 1), ("natural", 1)])
async def test_a_comma_splits_the_speech_only_when_fast(ws_server, delivery, streams):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == streams)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [], delivery=delivery)
    task = await run_voice(voice, sink)
    try:
        await soniox_engine._speak(voice, "Hello,", False, None)
        await soniox_engine._speak(voice, " world.", False, None)
        await until(lambda: text_ends(msgs) == streams, what="the end of the speech")
    finally:
        await stop(task)
    first = configs(msgs)[0]["stream_id"]
    if streams == 2:
        second = configs(msgs)[1]["stream_id"]  # the next warm stream
        assert [m for m in msgs if "text" in m] == [text(first, "Hello,", True), text(second, " world.", True)]
    else:
        assert [m for m in msgs if "text" in m] == [text(first, "Hello,"), text(first, " world.", True)]


@pytest.mark.parametrize("delivery, waited", [("fast", 0.1), ("balanced", 0.35), ("natural", 0.6)])
async def test_a_chunk_that_does_not_close_waits_for_more_by_the_delivery(ws_server, delivery, waited):
    at = []

    async def handler(ws):
        async for raw in ws:
            if json.loads(raw).get("text_end"):
                at.append(time.monotonic())

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [], delivery=delivery)
    task = await run_voice(voice, sink)
    try:
        start = time.monotonic()
        await voice.say("I work at")
        await until(lambda: at, what="the clause closed by FLUSH")
    finally:
        await stop(task)
    assert at[0] - start >= waited - 0.03  # the timer's resolution on Windows is ~16 ms


def test_balanced_speeds_up_later_and_less_and_natural_never():
    behind = [0.0]
    balanced = make_voice(FakeSink(), [], delivery="balanced", backlog=lambda: behind[0])
    speeds = []
    for seconds in (0.0, 1.6, 2.1, 1.0, 0.6, 0.4, 2.5):
        behind[0] = seconds
        speeds.append(balanced._clause_speed())
    assert speeds == [1.0, 1.0, 1.1, 1.1, 1.1, 1.0, 1.1]
    natural = make_voice(FakeSink(), [], delivery="natural", speed=1.05, backlog=lambda: 9.0)
    assert natural._clause_speed() == 1.05 and not natural.boosting  # only the speed I chose


@pytest.mark.parametrize("delivery, clause, backlog, heard", [
    ("balanced", "Hello,", 0.5, 100 + 150),  # a comma keeps 150 ms of the pause
    ("balanced", "Hello", 0.5, 100 + 100),   # a split without punctuation keeps 100 ms
    ("balanced", "Hello.", 0.5, 350),        # a sentence keeps its pause
    ("balanced", "Hello,", 0.1, 350),        # player nearly empty: nothing held back, the pause already played
    ("natural", "Hello,", 0.5, 350),         # never cuts a seam
    ("natural", "Hello", 0.5, 350),
])
async def test_the_pause_at_a_seam_is_cut_by_the_delivery(ws_server, delivery, clause, backlog, heard):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 2)
        first, second = [c["stream_id"] for c in configs(msgs)][:2]
        await ws.send(audio(first, tone(100) + silence(250)))
        await ws.send(audio_end(first))
        await ws.send(audio(second, tone(40), end=True))
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played, delivery=delivery, backlog=lambda: backlog)
    task = await run_voice(voice, sink)
    try:
        await voice.say(clause, end=True)
        await voice.say("world.", end=True)
        await until(lambda: ms(played) >= heard + 40, what="both clauses")
        await asyncio.sleep(0.05)
    finally:
        await stop(task)
    assert ms(played) == heard + 40


@pytest.mark.parametrize("delivery, heard", [("fast", 140), ("balanced", 140), ("natural", 300)])
async def test_only_a_delivery_that_cuts_seams_holds_the_end_back(ws_server, delivery, heard):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: text_ends(m) == 1)
        await ws.send(audio(msgs[0]["stream_id"], tone(300)))  # the server has all the text; audio_end is yet to come
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played, delivery=delivery, backlog=lambda: 0.5)
    task = await run_voice(voice, sink)
    try:
        await voice.say("Hello there", end=True)
        await until(lambda: played, what="the audio")
        await asyncio.sleep(0.1)
    finally:
        await stop(task)
    assert ms(played) == heard  # a seam could still cut the last 160 ms


@pytest.mark.parametrize("delivery, heard", [("fast", 20 + 50), ("balanced", 60 + 50), ("natural", 60 + 50)])
async def test_a_faint_start_is_silence_only_to_the_fast_delivery(ws_server, delivery, heard):
    async def handler(ws):
        msgs = []
        await read_until(ws, msgs, lambda m: text_ends(m) == 1)
        await ws.send(audio(msgs[0]["stream_id"], tone(60, 200) + tone(50), end=True))  # a breath, then the speech
        await ws.wait_closed()

    ws_server.handler = handler
    sink, played = FakeSink(), []
    voice = make_voice(sink, played, delivery=delivery)
    task = await run_voice(voice, sink)
    try:
        await voice.say("Hi.", end=True)
        await until(lambda: ms(played) >= heard, what="the clause")
        await asyncio.sleep(0.05)
    finally:
        await stop(task)
    assert ms(played) == heard


@pytest.mark.parametrize("delivery, heard", [("fast", 20 + 100), ("balanced", 50 + 100), ("natural", 50 + 100)])
async def test_a_stock_phrase_is_trimmed_by_the_delivery(tmp_path, delivery, heard):
    ready = phrases.PhraseCache(tmp_path, "soniox|tts-rt-v2|Adrian|en|1.0")
    ready.store("Sure.", 0, tone(50, 200) + tone(100))  # what was rendered does not depend on the delivery
    played = []
    voice = make_voice(FakeSink(), played, delivery=delivery, phrases=ready)
    await voice.say("Sure!", end=True)
    assert ms(played) == heard


# --- matching my pace and loudness ------------------------------------------------------

def test_only_balanced_and_natural_match_my_pace():
    for delivery, matches in {"fast": False, "balanced": True, "natural": True}.items():
        assert make_voice(FakeSink(), [], delivery=delivery).match_rate is matches
        assert not make_voice(FakeSink(), [], delivery=delivery, match_rate=False).match_rate
    assert soniox_engine.SonioxVoice(KEY, "Adrian", "en", None, None, None).match_rate  # balanced is the default


@pytest.mark.parametrize("speed, rate, tempo", [
    (1.0, 1.0, 1.0), (1.05, 1.0, 1.05), (1.0, 1.1, 1.1), (1.0, 0.92, 0.92),
    (1.2, 1.1, 1.3),  # never faster than MAX_SPEED...
    (1.4, 1.1, 1.4),  # ...or than the speed I chose myself
])
def test_a_stream_speaks_at_its_speed_times_my_pace(speed, rate, tempo):
    voice = make_voice(FakeSink(), [], delivery="balanced")
    st = soniox_engine._Stream("s1", speed)
    st.tone = (rate, 1.0)
    assert voice._tempo(st) == tempo


@pytest.mark.parametrize("delivery, match, tone", [
    ("balanced", True, (1.1, 0.85)),  # asked for 2.0 and 0.1: kept within bounds
    ("natural", True, (1.1, 0.85)),
    ("balanced", False, (1.0, 1.0)),
    ("fast", True, (1.0, 1.0)),       # fast is as it always was
])
async def test_prosody_sets_the_tone_of_what_follows_only_when_matching(delivery, match, tone):
    voice = make_voice(FakeSink(), [], delivery=delivery, match_rate=match)
    await voice.say("Hello.", end=True, prosody={"rate": 2.0, "volume": 0.1})
    assert voice.tone == tone
    assert [st.tone for st in voice.streams.values()] == [tone]


async def test_a_chunk_without_prosody_keeps_the_last_tone():
    voice = make_voice(FakeSink(), [], delivery="balanced")
    await voice.say("Hello.", end=True, prosody={"rate": 1.05, "volume": 1.0})
    await voice.say("Bye.", end=True)
    assert [st.tone for st in voice.streams.values()] == [(1.05, 1.0)] * 2


async def test_a_stream_sent_again_keeps_its_tone():
    voice = make_voice(FakeSink(), [], delivery="balanced")
    await voice.say("Hello.", end=True, prosody={"rate": 1.05, "volume": 1.0})
    (old,) = voice.order
    voice._retry(voice.streams[old])
    (fresh,) = voice.order
    assert fresh != old and voice.streams[fresh].tone == (1.05, 1.0)
    await asyncio.wait_for(asyncio.gather(*voice.tasks), 1)


@pytest.mark.parametrize("delivery, match, speeds", [
    ("balanced", True, [None, 1.1]), ("natural", True, [None, 1.1]),
    ("balanced", False, [None, None]), ("fast", True, [None, None]),
])
async def test_the_stream_opened_for_the_next_clause_speaks_at_my_pace(ws_server, delivery, match, speeds):
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(configs(m)) == 2)
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    voice = make_voice(sink, [], delivery=delivery, match_rate=match)
    task = await run_voice(voice, sink)
    try:
        await voice.say("Hello.", end=True, prosody={"rate": 1.1, "volume": 1.1})
        await until(lambda: len(configs(msgs)) == 2, what="the next stream")
    finally:
        await stop(task)
    # the speed of a Soniox stream is set when it opens: the one that was already warm keeps the old pace
    assert [c.get("speed") for c in configs(msgs)] == speeds
    assert all("volume" not in c for c in configs(msgs))  # Soniox has no volume
