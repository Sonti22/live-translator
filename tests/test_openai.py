"""OpenAI gpt-realtime-translate path (run_session / run_channel) against a mock realtime websocket."""
import argparse
import asyncio
import base64
import json

import pytest

import live_translator as lt
from mocks import FakePlayer, FakeSink, FakeVoice, b64, stop, until

KEY = "sk-test"


def event(kind, **fields):
    return json.dumps({"type": kind, **fields})


def channel(players=(), voice=None, gate_out=None, lag=None):
    return lt.Channel("Я", "en", asyncio.Queue(), list(players), "me", lag=lag, gate_out=gate_out, voice=voice)


async def session(ch, sink):
    await asyncio.wait_for(lt.run_session(ch, KEY, None, sink), 5)


async def test_session_update_audio_and_deltas(ws_server):
    seen = {"msgs": []}
    pcm_in, pcm_out = b"\x01\x00" * 480, b"\x10\x00" * 240

    async def handler(ws):
        seen["path"], seen["auth"] = ws.request.path, ws.request.headers.get("Authorization")
        seen["msgs"].append(json.loads(await ws.recv()))  # session.update
        await ws.send(event("session.updated", session={}))
        seen["msgs"].append(json.loads(await ws.recv()))  # first audio chunk
        await ws.send(event("session.input_transcript.delta", delta="Привет"))
        await ws.send(event("session.output_transcript.delta", delta="Hello"))
        await ws.send(event("session.output_audio.delta", delta=b64(pcm_out)))
        await ws.send(event("session.output_audio.delta", delta=b64(pcm_out)))
        # returning closes the connection normally: run_session returns

    ws_server.handler = handler
    sink, player, lag = FakeSink(), FakePlayer(), lt.LagMeter()
    ch = channel([player], lag=lag)
    await ch.queue.put(b"stale")  # captured before the connection: must not be sent
    lag.on_input(5000)  # I just started a phrase

    async def speak():
        await until(lambda: sink.statuses, what="session.updated")
        await ch.queue.put(pcm_in)

    speaker = asyncio.create_task(speak())
    await session(ch, sink)
    await speaker

    assert seen["path"] == "/openai?model=gpt-realtime-translate"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["msgs"] == [
        {"type": "session.update", "session": {"audio": {
            "input": {"transcription": {"model": "gpt-realtime-whisper"}, "noise_reduction": {"type": "near_field"}},
            "output": {"language": "en"},
        }}},
        {"type": "session.input_audio_buffer.append", "audio": base64.b64encode(pcm_in).decode()},
    ]
    assert sink.statuses == [("Я → EN", "подключено", True)]
    assert sink.captions == [("me_src", "Я", "Привет"), ("me_dst", "Я → EN", "Hello")]
    assert player.fed == [pcm_out, pcm_out]
    assert len(sink.lags) == 1 and 0 <= sink.lags[0] < lt.LagMeter.STALE  # measured once per phrase


async def test_cloned_voice_gets_the_text_instead_of_model_audio(ws_server):
    async def handler(ws):
        await ws.recv()
        await ws.send(event("session.output_transcript.delta", delta="Hello"))
        await ws.send(event("session.output_audio.delta", delta=b64(b"\x01\x00")))
        await ws.send(event("session.output_transcript.delta", delta=" world."))

    ws_server.handler = handler
    player, voice = FakePlayer(), FakeVoice()
    await session(channel([player], voice=voice), FakeSink())
    assert voice.said == ["Hello", " world."]
    assert player.fed == []


async def test_muted_channel_speaks_nothing(ws_server):
    async def handler(ws):
        await ws.recv()
        await ws.send(event("session.output_transcript.delta", delta="Hello"))
        await ws.send(event("session.output_audio.delta", delta=b64(b"\x01\x00")))

    ws_server.handler = handler
    sink, player, voice = FakeSink(), FakePlayer(), FakeVoice()
    await session(channel([player], gate_out=lambda: True), sink)
    await session(channel([player], voice=voice, gate_out=lambda: True), sink)
    assert player.fed == [] and voice.said == []
    assert sink.captions == [("me_dst", "Я → EN", "Hello")] * 2  # subtitles still shown


async def test_fatal_error(ws_server):
    async def handler(ws):
        await ws.recv()
        await ws.send(event("error", error={"type": "invalid_request_error", "code": "invalid_api_key",
                                            "message": "Incorrect API key provided."}))
        await ws.wait_closed()

    ws_server.handler = handler
    with pytest.raises(lt.Fatal) as err:
        await session(channel(), FakeSink())
    assert str(err.value) == f"Incorrect API key provided.\n{lt.FATAL_ERRORS['invalid_api_key']}"


async def test_other_error_is_a_note(ws_server):
    async def handler(ws):
        await ws.recv()
        await ws.send(event("error", error={"code": "rate_limit_exceeded", "message": "Slow down."}))

    ws_server.handler = handler
    sink = FakeSink()
    await session(channel(), sink)
    assert len(sink.notes) == 1
    assert sink.notes[0].startswith("[API error]") and "rate_limit_exceeded" in sink.notes[0]


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_handshake_is_fatal(ws_server, status):
    ws_server.reject = status
    with pytest.raises(lt.Fatal, match=f"HTTP {status}"):
        await asyncio.wait_for(lt.run_channel(channel(), KEY, None, FakeSink()), 5)


async def test_other_handshake_error_retries(ws_server):
    ws_server.reject = 503
    sink = FakeSink()
    task = asyncio.create_task(lt.run_channel(channel(), KEY, None, sink))
    try:
        await until(lambda: sink.statuses, what="status")
    finally:
        await stop(task)
    assert sink.statuses == [("Я → EN", "HTTP 503, переподключение…", False)]


@pytest.mark.xfail(strict=True, reason="bug: with --engine openai --voice off (console, or voice='off' in "
                                       "settings.json) the translator's own voice still plays into the call: "
                                       "Engine._openai_jobs only special-cases 'clone'")
async def test_voice_off_puts_no_audio_into_the_call(ws_server, monkeypatch):
    async def handler(ws):
        await ws.recv()
        await ws.send(event("session.output_audio.delta", delta=b64(b"\x01\x00")))

    ws_server.handler = handler
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    engine = lt.Engine(argparse.Namespace(voice="off", voice_id=None, lang="en"), FakeSink())
    player = FakePlayer()
    me = channel([player], gate_out=lambda: engine.muted or not engine.voice_out)
    for job in engine._openai_jobs(me, None, None, lt.LagMeter()):
        job.close()  # the channel is configured; its coroutine is driven below instead
    await session(me, FakeSink())
    assert player.fed == []  # "off: text only"
