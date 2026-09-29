"""Regressions of the engine audit: broken server frames, prosody across a reconnect, the STT sender task, SOCKS4a."""
import asyncio
import gc
import json
import socket
import struct
import threading

import pytest
from websockets.exceptions import ConnectionClosedError

import cartesia_engine
import inworld_engine
import soniox_engine
import voice_clone
from mocks import FakeSink, FakeVoice, b64, read_until, stop, until

KEY = "audit-test-key"
CONNECTED = ("Мой голос", "подключено", True)
ACK = json.dumps({"tokens": [], "final_audio_proc_ms": 0, "total_audio_proc_ms": 0})
END = json.dumps({"tokens": [{"text": "<end>", "is_final": True}]})


def channel(kind="me"):
    return __import__("live_translator").Channel("Я", "en", asyncio.Queue(), [], kind)


def start(ch, sink, voice=None):
    return asyncio.create_task(soniox_engine.run_stt_channel(ch, KEY, None, sink, "en", ["ru"], None, voice))


def tokens(*items):
    return json.dumps({"tokens": [{"text": t, "is_final": f, "translation_status": s} for t, f, s in items]})


# --- a frame that breaks the protocol is skipped: STT (ids 5) --------------------------------------

BAD_STT = [
    pytest.param("SECRET, not json", id="not-json"),
    pytest.param(b"\xff\xfe\x00SECRET", id="binary-not-text"),
    pytest.param('"SECRET"', id="json-string"),
    pytest.param("[]", id="json-list"),
    pytest.param("null", id="json-null"),
    pytest.param(json.dumps({"tokens": None}), id="tokens-null"),
    pytest.param(json.dumps({"tokens": "SECRET"}), id="tokens-string"),
    pytest.param(json.dumps({"tokens": ["SECRET"]}), id="token-string"),
    pytest.param(json.dumps({"tokens": [{"text": None, "is_final": True, "translation_status": "translation"}]}),
                 id="token-text-null"),
    pytest.param(json.dumps({"error_code": "500", "error_message": "SECRET"}), id="error-code-string"),
    pytest.param(json.dumps({"error_code": [500], "error_message": "SECRET"}), id="error-code-list"),
]


@pytest.mark.parametrize("bad", BAD_STT)
async def test_a_broken_stt_frame_is_skipped_and_the_session_goes_on(ws_server, bad):
    configs = []

    async def handler(ws):
        configs.append(await ws.recv())
        await ws.send(ACK)
        await ws.send(bad)
        await ws.send(tokens(("Hello", True, "translation")))
        await ws.send(END)
        await ws.wait_closed()

    ws_server.handler = handler
    ch, sink, voice = channel(), FakeSink(), FakeVoice()
    task = start(ch, sink, voice)
    try:
        await until(lambda: voice.ends or task.done(), what="the frame after the broken one")
        assert not task.done()
    finally:
        await stop(task)
    assert voice.said == ["Hello"] and voice.ends == 1
    assert len(configs) == 1  # no reconnect
    assert sink.statuses == [("Я → EN", "подключено", True)]
    assert not any("SECRET" in str(entry) for entry in sink.notes + sink.statuses + sink.captions)  # my speech


# --- a frame that breaks the protocol is skipped: TTS voices (ids 6, 25) -------------------------------

def soniox_audio(sid, pcm, end=False):
    return [json.dumps({"audio": b64(pcm), "stream_id": sid, **({"audio_end": True} if end else {})}),
            json.dumps({"terminated": True, "stream_id": sid})]


BAD_SONIOX = [
    pytest.param("SECRET, not json", id="not-json"),
    pytest.param('["SECRET"]', id="json-list"),
    pytest.param('"SECRET"', id="json-string"),
    pytest.param("5", id="json-number"),
    pytest.param('{"stream_id": "SID", "audio": "!!!SECRET"}', id="audio-not-base64"),
    pytest.param('{"stream_id": "SID", "audio": "AAA"}', id="audio-bad-padding"),
    pytest.param('{"stream_id": "SID", "audio": 5}', id="audio-number"),
    pytest.param('{"stream_id": ["SID"], "audio": "QQ=="}', id="stream-id-list"),
    pytest.param('{"stream_id": "SID", "error_code": "500", "error_message": "SECRET"}', id="error-code-string"),
    pytest.param('{"stream_id": "SID", "error_code": [500], "error_message": "SECRET"}', id="error-code-list"),
    pytest.param('{"stream_id": "SID", "error_code": 400, "error_type": 5}', id="error-type-number"),
]

BAD_CARTESIA = [
    pytest.param("SECRET, not json", id="not-json"),
    pytest.param("[]", id="json-list"),
    pytest.param('{"type": "chunk", "context_id": "SID", "data": "!!!SECRET"}', id="audio-not-base64"),
    pytest.param('{"type": "chunk", "context_id": "SID", "data": 5}', id="audio-number"),
    pytest.param('{"type": "chunk", "context_id": ["SID"], "data": "QQ=="}', id="context-id-list"),
    pytest.param('{"type": "error", "context_id": "SID", "status_code": "500", "message": "SECRET"}',
                 id="status-code-string"),
    pytest.param('{"type": "error", "context_id": "SID", "error_code": ["x"], "message": "SECRET"}',
                 id="error-code-list"),
]

BAD_INWORLD = [
    pytest.param("SECRET, not json", id="not-json"),
    pytest.param("[]", id="json-list"),
    pytest.param('{"result": "SECRET"}', id="result-string"),
    pytest.param('{"error": "SECRET"}', id="error-string"),
    pytest.param('{"result": {"contextId": "SID", "audioChunk": {"audioContent": "!!!SECRET"}}}',
                 id="audio-not-base64"),
    pytest.param('{"result": {"contextId": "SID", "audioChunk": {"audioContent": 5}}}', id="audio-number"),
    pytest.param('{"result": {"contextId": ["SID"], "audioChunk": {"audioContent": "QQ=="}}}', id="context-id-list"),
    pytest.param('{"result": {"contextId": "SID", "status": {"code": ["x"], "message": "SECRET"}}}',
                 id="status-code-list"),
]


async def broken_frame_then_audio(ws_server, voice, sink, played, key, sent, bad, good):
    """Say a clause and answer it with a broken frame, then its audio: the audio must still be heard."""
    msgs = []

    async def handler(ws):
        await read_until(ws, msgs, lambda m: len(m) == sent)
        sid = msgs[0][key]
        await ws.send(bad.replace("SID", sid))
        for frame in good(sid):
            await ws.send(frame)
        await ws.wait_closed()

    ws_server.handler = handler
    task = asyncio.create_task(voice.run())
    try:
        await until(lambda: sink.statuses, what="connected")
        await voice.say("Hello there.", end=True)
        await until(lambda: played or task.done(), what="the audio after the broken frame")
        assert not task.done()
    finally:
        await stop(task)
    assert played == [b"A1"]
    assert sink.statuses == [CONNECTED]  # no reconnect
    assert not any("SECRET" in note for note in sink.notes)


@pytest.mark.parametrize("bad", BAD_SONIOX)
async def test_soniox_voice_skips_a_broken_frame(ws_server, bad):
    sink, played = FakeSink(), []
    voice = soniox_engine.SonioxVoice(KEY, "Adrian", "en", played.append, None, sink, delivery="fast")
    await broken_frame_then_audio(ws_server, voice, sink, played, "stream_id", 2, bad,
                                  lambda sid: soniox_audio(sid, b"A1", end=True))


@pytest.mark.parametrize("bad", BAD_CARTESIA)
async def test_cartesia_voice_skips_a_broken_frame(ws_server, bad):
    sink, played = FakeSink(), []
    voice = cartesia_engine.CartesiaVoice(KEY, "voice-abc", "en", played.append, None, sink, delivery="fast")
    await broken_frame_then_audio(
        ws_server, voice, sink, played, "context_id", 1, bad,
        lambda cid: [json.dumps({"type": "chunk", "context_id": cid, "data": b64(b"A1")}),
                     json.dumps({"type": "done", "context_id": cid})])


@pytest.mark.parametrize("bad", BAD_INWORLD)
async def test_inworld_voice_skips_a_broken_frame(ws_server, bad):
    sink, played = FakeSink(), []
    voice = inworld_engine.InworldVoice(KEY, "Clive", "en", played.append, None, sink, delivery="fast")
    status = {"code": 0, "message": "", "details": []}
    await broken_frame_then_audio(
        ws_server, voice, sink, played, "context_id", 3, bad,
        lambda cid: [json.dumps({"result": {"contextId": cid, "audioChunk": {"audioContent": b64(b"A1")},
                                            "status": status}}),
                     json.dumps({"result": {"contextId": cid, "contextClosed": {}, "status": status}})])


BAD_CLONE = [
    pytest.param("SECRET, not json", id="not-json"),
    pytest.param("[]", id="json-list"),
    pytest.param("null", id="json-null"),
    pytest.param('{"type": "chunk", "context_id": "SID"}', id="chunk-without-data"),
    pytest.param('{"type": "chunk", "context_id": "SID", "data": "!!!SECRET"}', id="audio-not-base64"),
    pytest.param('{"type": "chunk", "context_id": "SID", "data": 5}', id="audio-number"),
    pytest.param('{"type": "chunk", "context_id": ["SID"], "data": "QQ=="}', id="context-id-list"),
]


@pytest.mark.parametrize("bad", BAD_CLONE)
async def test_clone_voice_skips_a_broken_frame(ws_server, bad):
    sink, played = FakeSink(), []
    voice = voice_clone.CloneVoice(KEY, "voice-abc", "en", played.append, None, 500, sink)
    await broken_frame_then_audio(
        ws_server, voice, sink, played, "context_id", 2, bad,
        lambda cid: [json.dumps({"type": "chunk", "context_id": cid, "data": b64(b"A1")}),
                     json.dumps({"type": "done", "context_id": cid})])


# --- prosody: a reconnect starts its clock over (id 2) ---------------------------------------------------

def frame(amp):
    import numpy as np
    return np.full(480, amp, "<i2").tobytes()  # 20 ms at 24 kHz


def heard(start, end, translation):
    return json.dumps({"tokens": [
        {"text": "ла" * 10, "is_final": True, "translation_status": "original", "start_ms": start, "end_ms": end},
        {"text": translation, "is_final": True, "translation_status": "translation"}]})


class Listener(FakeVoice):
    match_rate = True

    def __init__(self):
        super().__init__()
        self.tones = []

    async def say(self, text, end=False, prosody=None):
        await super().say(text, end)
        self.tones.append(prosody)


async def test_the_frame_clock_starts_over_with_each_connection(ws_server):
    warmup = soniox_engine.Prosody.WARMUP
    configs = []

    async def handler(ws):
        configs.append(await ws.recv())
        await ws.send(ACK)
        if len(configs) == 1:  # a usual-sounding stretch (3 s, the warm-up) and the connection ends
            for _ in range(50 * warmup + 1):
                await ws.recv()
            for i in range(warmup):
                await ws.send(heard(i * 1000, (i + 1) * 1000, "Hello."))
            await ws.close()
        else:  # a second of me, three times louder, in this connection's time (from 0)
            for _ in range(51):
                await ws.recv()
            await ws.send(heard(0, 1000, "Hello."))
            await ws.wait_closed()

    ws_server.handler = handler
    ch, sink, voice = channel(), FakeSink(), Listener()
    task = start(ch, sink, voice)
    try:
        await until(lambda: sink.statuses, what="connected")
        for _ in range(50 * warmup + 1):
            await ch.queue.put(frame(3000))
        await until(lambda: len(configs) == 2 and len(sink.statuses) == 2, what="the reconnect")
        for _ in range(51):
            await ch.queue.put(frame(9000))
        await until(lambda: len(voice.tones) == warmup + 1, what="the chunk after the reconnect")
    finally:
        await stop(task)
    # measured on this connection's frames (loud), not on the old connection's (usual) that carry the same times
    assert voice.tones == [{"rate": 1.0, "volume": 1.0}] * warmup + [{"rate": 1.0, "volume": 1.15}]


# --- the STT sender task is reaped (id 7) ---------------------------------------------------------------

class Wire:
    """A stand-in websocket. `on_audio(self)` runs when a binary frame is sent; iterating it waits for `over`."""

    def __init__(self, on_audio=None):
        self.on_audio, self.over, self.sending = on_audio, asyncio.Event(), asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, message):
        if isinstance(message, bytes):
            self.sending.set()
            await self.on_audio(self)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.over.wait()
        raise ConnectionClosedError(None, None)


async def test_a_stopped_channel_waits_for_its_sender_to_finish(monkeypatch):
    reaped = []

    async def hang(wire):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # what the send still has to let go of
            reaped.append(1)
            raise

    monkeypatch.setattr(soniox_engine, "connect", lambda *args, **kwargs: Wire(hang))
    ch = channel()
    task = start(ch, FakeSink())
    await ch.queue.put(frame(1000))
    await until(lambda: ch.queue.empty(), what="the frame taken")
    await asyncio.sleep(0.05)
    await stop(task)
    assert reaped == [1]


async def test_a_sender_that_fails_while_being_stopped_leaves_no_unretrieved_error(monkeypatch):
    problems, wires = [], []

    async def fail_when_stopped(wire):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise ConnectionClosedError(None, None) from None  # the socket went away under the cancelled send

    def connect(*args, **kwargs):
        wires.append(Wire(fail_when_stopped))
        return wires[-1]

    monkeypatch.setattr(soniox_engine, "connect", connect)
    asyncio.get_running_loop().set_exception_handler(lambda loop, context: problems.append(context["message"]))
    ch, sink = channel(), FakeSink()
    task = start(ch, sink)
    try:
        await ch.queue.put(frame(1000))
        await until(lambda: wires and wires[0].sending.is_set(), what="the frame sent")
        wires[0].over.set()  # the connection ends: the reader stops and the sender is let go of
        await until(lambda: len(wires) == 2, what="the reconnect")
        gc.collect()
    finally:
        await stop(task)
    assert sink.statuses[0][1].startswith("нет связи")  # a real disconnect is still handled
    assert problems == []


# --- SOCKS4a for REST calls (id 8) -------------------------------------------------------------------------

def _recv(sock, size):
    data = b""
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise OSError("closed")
        data += part
    return data


def _cstring(sock):
    data = b""
    while (byte := _recv(sock, 1)) != b"\x00":
        data += byte
    return data


def _relay(source, sink):
    try:
        while data := source.recv(65536):
            sink.sendall(data)
        sink.shutdown(socket.SHUT_WR)
    except OSError:
        pass


class Socks4a:
    """A SOCKS4a proxy: notes the destination the client asked for and relays to `target` instead."""

    def __init__(self, target):
        self.target, self.asked = target, []
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.server.close()

    def _accept(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        with client:
            version, command, port, ip = struct.unpack("!BBH4s", _recv(client, 8))
            _cstring(client)  # the user id
            host = socket.inet_ntoa(ip)
            if ip[:3] == bytes(3) and ip[3]:  # 0.0.0.x: the client wants the proxy to resolve the name (4a)
                host = _cstring(client).decode()
            assert (version, command) == (4, 1)
            self.asked.append((host, port))
            client.sendall(b"\x00\x5a" + bytes(6))
            with socket.create_connection(("127.0.0.1", self.target)) as upstream:
                threading.Thread(target=_relay, args=(upstream, client), daemon=True).start()
                _relay(client, upstream)


def test_a_socks4a_proxy_serves_rest_calls(http_server):
    port = http_server.server_address[1]
    http_server.routes[("GET", "/voices")] = (200, {"voices": []})
    proxy = Socks4a(port)
    try:
        status, data = voice_clone.https_request("GET", f"http://voices.example.test:{port}/voices", {}, None,
                                                 f"socks4a://127.0.0.1:{proxy.port}")
    finally:
        proxy.close()
    assert (status, json.loads(data)) == (200, {"voices": []})
    assert proxy.asked == [("voices.example.test", port)]  # the proxy resolves the name (that is the "a")


@pytest.mark.parametrize("proxy", ["socks5h://127.0.0.1", "socks4://user:secret@127.0.0.1"])
def test_a_proxy_address_python_socks_rejects_is_a_clone_error(proxy):
    with pytest.raises(voice_clone.CloneError, match="127.0.0.1") as err:
        voice_clone.https_request("GET", "http://127.0.0.1:1/", {}, None, proxy)
    assert "secret" not in str(err.value)
