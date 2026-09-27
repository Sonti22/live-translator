"""Soniox STT + translation: build_context and run_stt_channel against a mock Soniox websocket."""
import asyncio
import json

import pytest

import live_translator as lt
import soniox_engine
import voice_clone
from mocks import FakeSink, FakeVoice, stop, until

KEY = "soniox-test-key"
END = json.dumps({"tokens": [{"text": "<end>", "is_final": True}]})


def tokens(*items):
    """(text, is_final, translation_status) triples -> one Soniox response message."""
    return json.dumps({"tokens": [{"text": t, "is_final": f, "translation_status": s} for t, f, s in items]})


def channel(kind="me", gate_out=None):
    name, lang = ("Я", "en") if kind == "me" else ("Он", "ru")
    return lt.Channel(name, lang, asyncio.Queue(), [], kind, gate_out=gate_out)


def start(ch, sink, voice=None, target="en", hints=("ru",), context=None):
    return asyncio.create_task(soniox_engine.run_stt_channel(ch, KEY, None, sink, target, list(hints), context, voice))


# --- build_context ----------------------------------------------------------------

def test_build_context_terms_and_translation_terms():
    ctx = soniox_engine.build_context(["Сурен = Suren", "Kubernetes", "   ", "Москва=Moscow"], "  Daily standup ")
    assert ctx == {
        "terms": ["Сурен", "Kubernetes", "Москва"],
        "translation_terms": [{"source": "Сурен", "target": "Suren"}, {"source": "Москва", "target": "Moscow"}],
        "text": "Daily standup",
    }


def test_build_context_reverse_direction():
    ctx = soniox_engine.build_context(["Сурен = Suren", "Kubernetes"], "", reverse=True)
    assert ctx == {"terms": ["Suren", "Kubernetes"], "translation_terms": [{"source": "Suren", "target": "Сурен"}]}


def test_build_context_half_pair_is_a_plain_term():
    assert soniox_engine.build_context(["Сурен =", "= Suren"], None) == {"terms": ["Сурен"]}


@pytest.mark.parametrize("keywords, text", [(None, None), ([], ""), (["", "  ", " = "], "   ")])
def test_build_context_empty_is_none(keywords, text):
    assert soniox_engine.build_context(keywords, text) is None


# --- run_stt_channel ----------------------------------------------------------------

async def test_config_audio_and_final_tokens(ws_server):
    seen = {"audio": []}
    frames = [b"\x01\x00" * 480, b"\x02\x00" * 480]

    async def handler(ws):
        seen["path"] = ws.request.path
        seen["config"] = json.loads(await ws.recv())
        while len(seen["audio"]) < len(frames):
            seen["audio"].append(await ws.recv())
        await ws.send(tokens(("Привет", True, "original"), (" как", False, "original"),
                             ("Hello", True, "translation"), (" how", False, "translation")))
        await ws.send(tokens((" мир", True, "original"), (" world", True, "translation")))
        await ws.send(END)
        await ws.wait_closed()

    ws_server.handler = handler
    ch, sink, voice = channel(), FakeSink(), FakeVoice()
    await ch.queue.put(b"stale")  # captured before the connection: must not be sent
    context = soniox_engine.build_context(["Сурен = Suren"], "Daily standup")
    task = start(ch, sink, voice, context=context)
    try:
        await until(lambda: sink.statuses, what="connected status")
        for pcm in frames:
            await ch.queue.put(pcm)
        await until(lambda: voice.ends, what="end of utterance")
    finally:
        await stop(task)

    assert seen["path"] == "/soniox-stt"
    assert seen["config"] == {
        "api_key": KEY, "model": "stt-rt-v5",
        "audio_format": "pcm_s16le", "sample_rate": 24000, "num_channels": 1,
        "language_hints": ["ru"],
        "enable_endpoint_detection": True, "max_endpoint_delay_ms": 500,
        "translation": {"type": "one_way", "target_language": "en"},
        "context": {"terms": ["Сурен"], "translation_terms": [{"source": "Сурен", "target": "Suren"}],
                    "text": "Daily standup"},
    }
    assert seen["audio"] == frames  # raw binary PCM frames
    assert sink.statuses == [("Я → EN", "подключено", True)]
    assert sink.captions == [("me_src", "Я", "Привет"), ("me_dst", "Я → EN", "Hello"),
                             ("me_src", "Я", " мир"), ("me_dst", "Я → EN", " world")]
    assert voice.said == ["Hello", " world"]  # final translation tokens only
    assert voice.ends == 1
    assert sink.notes == []


async def test_muted_translation_is_captioned_but_not_spoken(ws_server):
    async def handler(ws):
        await ws.recv()
        await ws.send(tokens(("Hello", True, "translation")))
        await ws.send(END)
        await ws.wait_closed()

    ws_server.handler = handler
    ch, sink, voice = channel(gate_out=lambda: True), FakeSink(), FakeVoice()
    task = start(ch, sink, voice)
    try:
        await until(lambda: voice.ends, what="end of utterance")
    finally:
        await stop(task)
    assert sink.captions == [("me_dst", "Я → EN", "Hello")]
    assert voice.said == []


async def test_their_channel_without_voice(ws_server):
    seen = {}

    async def handler(ws):
        seen["config"] = json.loads(await ws.recv())
        await ws.send(tokens(("Hi", True, "original"), ("Привет", True, "translation")))
        await ws.send(END)  # no voice: must not break anything
        await ws.send(tokens(("Bye", True, "original")))
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    task = start(channel("them"), sink, target="ru", hints=("en",))
    try:
        await until(lambda: len(sink.captions) == 3, what="captions")
    finally:
        await stop(task)
    assert sink.captions == [("them_src", "Он", "Hi"), ("them_dst", "Он → RU", "Привет"), ("them_src", "Он", "Bye")]
    assert seen["config"]["translation"] == {"type": "one_way", "target_language": "ru"}
    assert seen["config"]["language_hints"] == ["en"]
    assert "context" not in seen["config"]


@pytest.mark.parametrize("code", [401, 402, 403])
async def test_auth_error_is_fatal(ws_server, code):
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"error_code": code, "error_message": "Invalid API key."}))
        await ws.wait_closed()

    ws_server.handler = handler
    with pytest.raises(soniox_engine.SonioxFatal, match="SONIOX_API_KEY") as err:
        await asyncio.wait_for(
            soniox_engine.run_stt_channel(channel(), KEY, None, FakeSink(), "en", ["ru"], None), 5)
    assert "Invalid API key." in str(err.value)
    assert isinstance(err.value, voice_clone.CloneError)  # Engine.run turns CloneError into Fatal


async def test_other_error_is_noted_and_reconnects(ws_server):
    configs = []

    async def handler(ws):
        configs.append(json.loads(await ws.recv()))
        if len(configs) == 1:
            await ws.send(json.dumps({"error_code": 503, "error_message": "Service overloaded."}))
        await ws.wait_closed()

    ws_server.handler = handler
    sink = FakeSink()
    task = start(channel(), sink)
    try:
        await until(lambda: len(configs) == 2, what="reconnect")
    finally:
        await stop(task)
    assert sink.notes == ["[Я → EN] Soniox: Service overloaded."]
    assert configs[0] == configs[1]


async def test_http_error_on_handshake_retries(ws_server):
    ws_server.reject = 503
    sink = FakeSink()
    task = start(channel(), sink)
    try:
        await until(lambda: sink.statuses, what="status")
    finally:
        await stop(task)
    assert sink.statuses[0] == ("Я → EN", "HTTP 503, переподключение…", False)


# --- speaker separation (other side) ------------------------------------------------

async def test_diarization_labels_captions_with_speakers(ws_server):
    seen = {}

    async def handler(ws):
        seen["config"] = json.loads(await ws.recv())
        await ws.send(json.dumps({"tokens": [
            {"text": "Hello.", "is_final": True, "translation_status": "original", "speaker": "1"},
            {"text": "Привет.", "is_final": True, "translation_status": "translation"},  # no speaker: last one
            {"text": "Hi!", "is_final": True, "translation_status": "original", "speaker": "2"},
        ]}))
        await ws.wait_closed()

    ws_server.handler = handler
    ch, sink = channel("them"), FakeSink()
    task = asyncio.create_task(soniox_engine.run_stt_channel(ch, KEY, None, sink, "ru", ["en"], None, diarize=True))
    try:
        await until(lambda: len(sink.captions) == 3, what="three captions")
    finally:
        await stop(task)
    assert seen["config"]["enable_speaker_diarization"] is True
    assert sink.captions == [("them_src", "Он", "Hello.", "1"), ("them_dst", "Он → RU", "Привет.", "1"),
                             ("them_src", "Он", "Hi!", "2")]


def test_no_diarization_by_default():
    assert "enable_speaker_diarization" not in soniox_engine.stt_config(KEY, "en", ["ru"], None)
