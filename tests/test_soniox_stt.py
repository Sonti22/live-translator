"""Soniox STT + translation: build_context and run_stt_channel against a mock Soniox websocket."""
import asyncio
import json

import numpy as np
import pytest

import live_translator as lt
import soniox_engine
import voice_clone
from mocks import FakeSink, FakeVoice, stop, until

KEY = "soniox-test-key"
END = json.dumps({"tokens": [{"text": "<end>", "is_final": True}]})
ACK = json.dumps({"tokens": [], "final_audio_proc_ms": 0, "total_audio_proc_ms": 0})  # the reply to a config


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
        await ws.send(ACK)
        while len(seen["audio"]) < soniox_engine.RECENT + len(frames):
            seen["audio"].append(await ws.recv())
        await ws.send(tokens(("Привет", True, "original"), (" как", False, "original"),
                             ("Hello", True, "translation"), (" how", False, "translation")))
        await ws.send(tokens((" мир", True, "original"), (" world", True, "translation")))
        await ws.send(END)
        await ws.wait_closed()

    ws_server.handler = handler
    ch, sink, voice = channel(), FakeSink(), FakeVoice()
    said_while_connecting = [bytes([i % 250 + 3, 0]) * 480 for i in range(soniox_engine.RECENT + 50)]
    for pcm in said_while_connecting:
        await ch.queue.put(pcm)
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
    # raw binary PCM frames; the last 2 s said while connecting are translated too, older ones dropped
    assert seen["audio"] == said_while_connecting[-soniox_engine.RECENT:] + frames
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


# --- one chunk per server message, markers, manual finalization --------------------------

async def run_messages(ws_server, messages, finalizer=None, audio=()):
    """Serve `messages` after the config; returns (voice, sink, what the server received after the config)."""
    received = []

    async def handler(ws):
        await ws.recv()
        await ws.send(ACK)
        for message in messages:
            await ws.send(message)
        async for frame in ws:
            received.append(frame)

    ws_server.handler = handler
    ch, sink, voice = channel(), FakeSink(), FakeVoice()
    ch.finalizer = finalizer
    task = start(ch, sink, voice)
    try:
        await until(lambda: sink.statuses, what="connected")
        if finalizer:  # the mic streams all the time; here audio is fed once the words are pending
            await until(lambda: finalizer.pending, what="pending words")
        for pcm in audio:
            await ch.queue.put(pcm)
        await asyncio.sleep(0.3)
    finally:
        await stop(task)
    return voice, sink, received


async def test_a_clause_is_spoken_as_one_chunk_and_closed_by_its_punctuation(ws_server):
    voice, _, _ = await run_messages(ws_server, [
        tokens(("My", True, "translation"), (" name", True, "translation"), (" is", True, "translation"),
               (" Suren,", True, "translation"), (" я", False, "original")),
        tokens((" I'm", True, "translation"), (" a", True, "translation")),
    ])
    assert voice.said == ["My name is Suren,", " I'm a"]
    assert voice.ends == 1  # the comma closed the first clause; the second waits for more text


async def test_manual_finalize_marker_ends_the_utterance_and_is_not_captioned(ws_server):
    fin = json.dumps({"tokens": [{"text": "<fin>", "is_final": True}]})
    voice, sink, _ = await run_messages(ws_server, [
        tokens(("Привет", True, "original"), ("Hello", True, "translation")), fin,
    ])
    assert voice.said == ["Hello"] and voice.ends == 1
    assert sink.captions == [("me_src", "Я", "Привет"), ("me_dst", "Я → EN", "Hello")]


async def test_pause_after_speech_sends_finalize_while_words_are_pending(ws_server):
    loud, quiet = b"\x00\x10" * 480, bytes(960)
    finalizer = soniox_engine.AutoFinalize()
    _, _, received = await run_messages(
        ws_server, [tokens(("Меня", False, "original"))], finalizer, [loud] * 10 + [quiet] * 20)
    texts = [frame for frame in received if isinstance(frame, str)]
    assert texts == [json.dumps({"type": "finalize"})]
    assert received.index(texts[0]) == 10 + soniox_engine.AutoFinalize.PAUSE  # right after the pause frame


def feed_all(finalizer, frames):
    return [message for pcm in frames for message in finalizer.feed(pcm)]


LOUD, QUIET = b"\x00\x10" * 480, bytes(960)


def test_auto_finalize_needs_pending_words_speech_and_a_long_enough_pause():
    idle = soniox_engine.AutoFinalize()
    assert feed_all(idle, [LOUD] * 10 + [QUIET] * 30) == []  # nothing pending: Soniox already finalized
    idle.pending = True
    assert feed_all(idle, [QUIET]) == [soniox_engine.AutoFinalize.MESSAGE]  # words arrived late, still paused
    finalizer = soniox_engine.AutoFinalize()
    finalizer.pending = True
    assert feed_all(finalizer, [QUIET] * 30) == []  # a pause without speech before it
    assert feed_all(finalizer, [LOUD] * 10 + [QUIET] * (soniox_engine.AutoFinalize.PAUSE - 1)) == []
    assert feed_all(finalizer, [QUIET]) == [soniox_engine.AutoFinalize.MESSAGE]


def test_auto_finalize_is_rate_limited_and_skipped_under_backlog():
    finalizer = soniox_engine.AutoFinalize(backlog=lambda: 0.0)
    finalizer.pending = True
    assert feed_all(finalizer, [LOUD] * 10 + [QUIET] * 20) == [soniox_engine.AutoFinalize.MESSAGE]
    finalizer.pending = True
    assert feed_all(finalizer, [LOUD] * 10 + [QUIET] * 20) == []  # within 1.5 s of the last one
    queued = soniox_engine.AutoFinalize(backlog=lambda: 2.0)
    queued.pending = True
    assert feed_all(queued, [LOUD] * 10 + [QUIET] * 20) == []  # my speech is queued anyway
    off = soniox_engine.AutoFinalize(enabled=False)
    off.pending = True
    assert feed_all(off, [LOUD] * 10 + [QUIET] * 20) == []


def test_hotkey_finalizes_at_once_after_200_ms_of_silence():
    finalizer = soniox_engine.AutoFinalize(enabled=False)  # the hotkey works with auto finalize off
    finalizer.force()
    extra = finalizer.feed(LOUD)
    assert extra == [bytes(960)] * soniox_engine.AutoFinalize.SILENCE + [soniox_engine.AutoFinalize.MESSAGE]
    finalizer.force()  # pressed again within a second: ignored
    assert finalizer.feed(LOUD) == []


def test_keep_recent_keeps_the_last_frames_and_drops_control_messages():
    queue = asyncio.Queue()
    for i in range(5):
        queue.put_nowait(bytes([i]))
    queue.put_nowait(soniox_engine.AutoFinalize.MESSAGE)
    soniox_engine.keep_recent(queue, 3)
    assert [queue.get_nowait() for _ in range(queue.qsize())] == [bytes([2]), bytes([3]), bytes([4])]


# --- prosody: how I sound, for the voice to repeat ----------------------------------------

def frame(amp):
    return np.full(480, amp, "<i2").tobytes()  # 20 ms at 24 kHz


def said(prosody, seconds, amp=3000, text="ла" * 10):
    """What Soniox and my microphone give for `seconds` of me saying `text` (ten vowels) at loudness `amp`."""
    start = prosody.samples * 1000 // soniox_engine.RATE
    for _ in range(round(seconds * 50)):
        prosody.audio(frame(amp))
    prosody.source({"text": text, "start_ms": start, "end_ms": prosody.samples * 1000 // soniox_engine.RATE})
    return prosody.chunk()


def steady():
    """A speaker heard for three chunks (a second each, ten vowels, loudness 3000): the warm-up is over."""
    prosody = soniox_engine.Prosody()
    for _ in range(soniox_engine.Prosody.WARMUP):
        assert said(prosody, 1.0) == {"rate": 1.0, "volume": 1.0}
    return prosody


def heard(start, end, translation):
    """One Soniox message: my words with their time, and the translation."""
    return json.dumps({"tokens": [
        {"text": "ла" * 10, "is_final": True, "translation_status": "original", "start_ms": start, "end_ms": end},
        {"text": translation, "is_final": True, "translation_status": "translation"}]})


class Listener(FakeVoice):
    """A voice that matches my pace, as SonioxVoice does: it takes the prosody of what it says."""

    match_rate = True

    def __init__(self):
        super().__init__()
        self.tones = []

    async def say(self, text, end=False, prosody=None):
        await super().say(text, end)
        self.tones.append(prosody)


class Meter:
    def __init__(self, tone):
        self.tone, self.measured = tone, 0

    def chunk(self):
        self.measured += 1
        return self.tone


def test_the_first_chunks_only_set_what_is_usual():
    prosody = soniox_engine.Prosody()
    first = [said(prosody, seconds, amp) for seconds, amp in ((1.0, 3000), (0.5, 9000), (2.0, 1000))]
    assert first == [{"rate": 1.0, "volume": 1.0}] * 3


@pytest.mark.parametrize("seconds, rate", [(0.9, 1.06), (1.1, 0.95), (0.4, 1.1), (3.0, 0.92)])
def test_half_of_how_much_faster_or_slower_I_speak_is_repeated_within_bounds(seconds, rate):
    assert said(steady(), seconds)["rate"] == pytest.approx(rate, abs=0.005)


@pytest.mark.parametrize("amp, volume", [(4000, 1.06), (2250, 0.94), (12000, 1.15), (700, 0.85)])
def test_how_much_louder_or_quieter_I_speak_is_repeated_within_bounds(amp, volume):
    chunk = said(steady(), 1.0, amp)
    assert chunk["volume"] == pytest.approx(volume, abs=0.005) and chunk["rate"] == 1.0


def test_pace_and_loudness_are_measured_apart():
    assert said(steady(), 0.9, 4000) == {"rate": 1.06, "volume": 1.06}


def test_what_cannot_be_measured_stays_as_usual():
    prosody = steady()
    assert said(prosody, 1.0, 4000, text="ммм") == {"rate": 1.0, "volume": 1.06}  # no vowels to count
    assert said(prosody, 0.9, 300) == {"rate": 1.06, "volume": 1.0}               # too quiet to tell from a breath


def test_when_nothing_can_be_measured_there_is_no_prosody():
    prosody = steady()
    assert said(prosody, 0.2, 100) is None  # too short for a pace, too quiet for a level
    prosody.source({"text": "ла" * 10})     # a word Soniox gave no time for
    assert prosody.chunk() is None
    assert said(prosody, 1.0, 100, text="ммм") is None


def test_a_chunk_uses_up_the_words_it_measured():
    prosody = steady()
    said(prosody, 0.9)
    assert prosody.chunk() is None


def test_a_new_connection_forgets_the_time_but_not_how_I_sound():
    prosody = steady()
    prosody.source({"text": "ла" * 10, "start_ms": 0, "end_ms": 1000})
    prosody.restart()
    assert prosody.chunk() is None  # those words are in the old connection's time
    assert said(prosody, 0.9) == {"rate": 1.06, "volume": 1.0}  # no warm-up again


def test_only_the_last_30_seconds_of_frames_are_kept():
    prosody = soniox_engine.Prosody()
    for _ in range(50 * 40):
        prosody.audio(frame(3000))
    assert len(prosody.frames) <= 50 * 30 + 1 and prosody.frames[-1][1] == 40_000


async def test_the_silence_sent_before_a_finalize_is_audio_too():
    class Wire:
        def __init__(self):
            self.sent = []

        async def send(self, message):
            self.sent.append(message)

    ws, queue, prosody, finalizer = Wire(), asyncio.Queue(), soniox_engine.Prosody(), soniox_engine.AutoFinalize()
    finalizer.force()
    queue.put_nowait(frame(3000))
    pump = asyncio.create_task(soniox_engine._pump(ws, queue, finalizer, prosody))
    try:
        await until(lambda: len(ws.sent) == 1 + finalizer.SILENCE + 1, what="the frame, the silence and the finalize")
    finally:
        await stop(pump)
    assert ws.sent[-1] == finalizer.MESSAGE
    assert prosody.samples == 480 * (1 + finalizer.SILENCE)  # the message is no audio
    assert prosody.frames[-1][2] == 0.0


async def test_speak_passes_the_prosody_on_only_when_there_is_one():
    voice, tone = Listener(), {"rate": 1.05, "volume": 0.9}
    await soniox_engine._speak(voice, "Hello.", False, None, Meter(tone))
    await soniox_engine._speak(voice, "Hello.", False, None, Meter(None))
    await soniox_engine._speak(voice, "Hello.", False, None)
    assert voice.tones == [tone, None, None]
    plain = FakeVoice()  # knows nothing of prosody: it must never be asked to take it
    await soniox_engine._speak(plain, "Hello.", False, None, Meter(None))
    assert plain.said == ["Hello."]


async def test_speak_measures_a_muted_chunk_but_never_an_empty_one():
    voice, meter = Listener(), Meter({"rate": 1.05, "volume": 1.0})
    await soniox_engine._speak(voice, "Hello.", False, lambda: True, meter)
    await soniox_engine._speak(voice, "", True, None, meter)
    assert (voice.said, voice.ends, meter.measured) == ([], 1, 1)  # what I said while muted is used up


async def test_the_engine_hands_the_voice_how_I_sounded_saying_each_chunk(ws_server):
    speech = [(1000, 3000)] * 3 + [(900, 3000)]  # three usual chunks, then a faster one: (ms, loudness)
    total = sum(ms // 20 for ms, _ in speech) + 1  # and one more frame, so the last one is surely counted

    async def handler(ws):
        await ws.recv()
        await ws.send(ACK)
        for _ in range(total):
            await ws.recv()
        start = 0
        for ms, _ in speech:
            await ws.send(heard(start, start + ms, "Hello."))
            start += ms
        await ws.send(END)
        await ws.wait_closed()

    ws_server.handler = handler
    ch, sink, voice = channel(), FakeSink(), Listener()
    task = start(ch, sink, voice)
    try:
        await until(lambda: sink.statuses, what="connected status")
        for ms, amp in speech:
            for _ in range(ms // 20):
                await ch.queue.put(frame(amp))
        await ch.queue.put(frame(0))
        await until(lambda: len(voice.tones) == 4, what="four chunks")
    finally:
        await stop(task)
    assert voice.tones == [{"rate": 1.0, "volume": 1.0}] * 3 + [{"rate": 1.06, "volume": 1.0}]


async def test_a_voice_that_does_not_match_my_pace_is_not_sent_prosody(ws_server):
    async def handler(ws):
        await ws.recv()
        await ws.send(ACK)
        await ws.send(heard(0, 1000, "Hello."))
        await ws.send(END)
        await ws.wait_closed()

    ws_server.handler = handler
    ch, sink, voice = channel(), FakeSink(), FakeVoice()  # its say() takes no prosody
    task = start(ch, sink, voice)
    try:
        await until(lambda: voice.ends, what="end of utterance")
    finally:
        await stop(task)
    assert voice.said == ["Hello."]
