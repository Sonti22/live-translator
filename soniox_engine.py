"""
Soniox engine — the default "my voice" pipeline.

  mic -> Soniox STT (stt-rt-v5) with one-way translation: translated words stream mid-sentence
      -> Soniox TTS (tts-rt-v2) in my cloned voice, one stream per utterance, played in order
      -> VB-Cable

The same STT (without TTS) subtitles the other person. The AI assistant's keywords and context
go into Soniox `context` (terms / translation_terms / text), which steers recognition and translation.
"""
import asyncio
import base64
import json
import mimetypes
import os
import time
import uuid
from collections import deque

from python_socks import ProxyError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from voice_clone import CloneError, https_request

STT_URL = os.environ.get("LIVE_TRANSLATOR_SONIOX_STT", "wss://stt-rt.soniox.com/transcribe-websocket")
TTS_URL = os.environ.get("LIVE_TRANSLATOR_SONIOX_TTS", "wss://tts-rt.soniox.com/tts-websocket")
API_URL = os.environ.get("LIVE_TRANSLATOR_SONIOX_API", "https://api.soniox.com")
STT_MODEL = "stt-rt-v5"
TTS_MODEL = "tts-rt-v2"
KEY_ENV = "SONIOX_API_KEY"
DEFAULT_VOICE = "Adrian"
AUTH_CODES = (401, 402, 403)


def build_context(keywords, context_text, reverse=False):
    """Transync-style AI assistant -> Soniox context.

    keywords: list of "term" or "source = target" (source in my language); reverse=True for the
    other direction (their speech is translated into my language)."""
    terms, pairs = [], []
    for item in keywords or ():
        source, sep, target = item.partition("=")
        source, target = source.strip(), target.strip()
        if sep and source and target:
            if reverse:
                source, target = target, source
            pairs.append({"source": source, "target": target})
            terms.append(source)
        elif source:
            terms.append(source)
    context = {}
    if terms:
        context["terms"] = terms
    if pairs:
        context["translation_terms"] = pairs
    if (context_text or "").strip():
        context["text"] = context_text.strip()
    return context or None


def stt_config(api_key, target, hints, context, diarize=False):
    config = {
        "api_key": api_key, "model": STT_MODEL,
        "audio_format": "pcm_s16le", "sample_rate": 24000, "num_channels": 1,
        "language_hints": hints,
        "enable_endpoint_detection": True, "max_endpoint_delay_ms": 500,
        "translation": {"type": "one_way", "target_language": target},
    }
    if context:
        config["context"] = context
    if diarize:
        config["enable_speaker_diarization"] = True  # "Собеседник 1 / 2" when several people talk
    return config


class SonioxFatal(CloneError):
    pass


async def _pump(ws, queue):
    while True:
        await ws.send(await queue.get())  # binary PCM frames


async def run_stt_channel(ch, api_key, proxy, sink, target, hints, context, voice=None, diarize=False):
    """Transcribe + translate one audio source; final translated words go to captions and TTS."""
    speaker = None  # last speaker heard; translation tokens may come without one
    while True:
        try:
            async with connect(STT_URL, max_size=None, proxy=proxy, compression=None) as ws:
                await ws.send(json.dumps(stt_config(api_key, target, hints, context, diarize)))
                while not ch.queue.empty():  # drop audio captured while (re)connecting
                    ch.queue.get_nowait()
                sender = asyncio.create_task(_pump(ws, ch.queue))
                sink.status(ch.dst_label, "подключено", True)
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("error_code"):
                            text = f"Soniox: {msg.get('error_message', msg)}"
                            if msg["error_code"] in AUTH_CODES:
                                raise SonioxFatal(text + "\nПроверь ключ SONIOX_API_KEY и баланс.")
                            sink.note(f"[{ch.dst_label}] {text}")
                            break
                        for token in msg.get("tokens", ()):
                            if not token.get("is_final"):
                                continue
                            text = token.get("text", "")
                            if text == "<end>":
                                if voice:
                                    await voice.end_utterance()
                                continue
                            who = {}
                            if diarize:
                                speaker = token.get("speaker") or speaker
                                who = {"speaker": speaker} if speaker else {}
                            if token.get("translation_status") == "translation":
                                sink.caption(f"{ch.kind}_dst", ch.dst_label, text, **who)
                                if voice and not (ch.gate_out and ch.gate_out()):
                                    await voice.say(text)
                            else:
                                sink.caption(f"{ch.kind}_src", ch.src_label, text, **who)
                        if msg.get("finished"):
                            break
                finally:
                    sender.cancel()
        except SonioxFatal:
            raise
        except InvalidStatus as e:
            sink.status(ch.dst_label, f"HTTP {e.response.status_code}, переподключение…", False)
        except (ConnectionClosed, OSError, ProxyError) as e:
            sink.status(ch.dst_label, "нет связи, переподключение… (VPN включён?)", False)
            sink.note(f"[{ch.dst_label}] {e}")
        await asyncio.sleep(1)


class SonioxVoice:
    """Speaks translated text in a Soniox voice (built-in name or my clone's id).

    One TTS stream per utterance; the next stream opens right away (up to 5 run concurrently),
    and its audio waits until the previous utterance finished, so speech never overlaps."""

    KEEPALIVE = 20
    REWARM = 2.0  # at most one fresh warm stream per this many seconds
    FLUSH = 0.2   # translation quiet this long = a finished clause, speak it now

    def __init__(self, api_key, voice, language, play, proxy, sink, on_first_audio=None, speed=1.0):
        self.api_key, self.voice, self.language = api_key, voice, language
        self.play, self.proxy, self.sink = play, proxy, sink
        self.on_first_audio, self.speed = on_first_audio, speed
        self.ws = None
        self.current = None      # stream accepting text
        self.used = set()        # streams that received text
        self.order = deque()     # streams in speaking order
        self.pending = {}        # stream -> audio buffered behind an earlier utterance
        self.finished = set()
        self.heard = set()
        self.last_warm = 0.0
        self.flusher = None

    def _config(self, stream_id):
        config = {"api_key": self.api_key, "stream_id": stream_id, "model": TTS_MODEL, "voice": self.voice,
                  "language": self.language, "audio_format": "pcm_s16le", "sample_rate": 24000}
        if self.speed != 1.0:
            config["speed"] = self.speed
        return config

    async def _rewarm(self):
        """Keep one opened, unused stream ready so the next utterance skips stream setup."""
        wait = self.REWARM - (time.monotonic() - self.last_warm)
        if wait > 0:
            await asyncio.sleep(wait)  # throttled, not dropped: warm up again once allowed
        if self.ws is None or self.current is not None:
            return
        try:
            await self._open()
        except ConnectionClosed:
            pass

    async def _open(self):
        self.last_warm = time.monotonic()
        stream_id = uuid.uuid4().hex
        self.current = stream_id  # claimed before the await, so a concurrent warm-up won't open a second one
        self.order.append(stream_id)
        self.pending[stream_id] = []
        await self.ws.send(json.dumps(self._config(stream_id)))

    async def run(self):
        while True:
            try:
                async with connect(TTS_URL, max_size=None, proxy=self.proxy, compression=None) as ws:
                    self.ws = ws
                    await self._open()  # pre-warm: the first utterance skips stream setup
                    self.sink.status("Мой голос", "подключено", True)
                    keepalive = asyncio.create_task(self._keepalive())
                    try:
                        async for raw in ws:
                            self._on_message(json.loads(raw))
                    finally:
                        keepalive.cancel()
            except SonioxFatal:
                raise
            except InvalidStatus as e:
                self.sink.status("Мой голос", f"HTTP {e.response.status_code}, переподключение…", False)
            except (ConnectionClosed, OSError, ProxyError) as e:
                self.sink.status("Мой голос", "нет связи, переподключение…", False)
                self.sink.note(f"[Мой голос] {e}")
            finally:
                self.ws = None
                self._reset()
            await asyncio.sleep(1)

    async def _keepalive(self):
        while True:
            await asyncio.sleep(self.KEEPALIVE)
            await self.ws.send(json.dumps({"keep_alive": True}))

    def _on_message(self, msg):
        sid = msg.get("stream_id")
        if msg.get("error_code"):
            kind = msg.get("error_type", "")
            if msg["error_code"] in AUTH_CODES:
                raise SonioxFatal(f"Soniox TTS: {msg.get('error_message')}\nПроверь ключ SONIOX_API_KEY и баланс.")
            if kind == "request_timeout" and sid not in self.used:
                pass  # an idle pre-warmed stream expired: nothing was lost
            elif kind.startswith("voice_"):
                self.sink.status("Мой голос", "клон недоступен — запиши голос заново", False)
                self.sink.note(f"[Мой голос] {msg.get('error_message')}")
            else:
                self.sink.note(f"[Мой голос] {msg.get('error_message', msg)}")
        if msg.get("audio") and sid in self.pending:
            pcm = base64.b64decode(msg["audio"])
            if sid not in self.heard:
                self.heard.add(sid)
                if self.on_first_audio:
                    self.on_first_audio()
            if self.order and self.order[0] == sid:
                self.play(pcm)
            else:
                self.pending[sid].append(pcm)
        if msg.get("terminated") and sid in self.pending:
            self.finished.add(sid)
            if sid == self.current:
                self.current = None
                asyncio.get_running_loop().create_task(self._rewarm())  # the warm stream expired
            self._advance()

    def _advance(self):
        while self.order and self.order[0] in self.finished:
            done = self.order.popleft()
            self.pending.pop(done, None)
            self.finished.discard(done)
            self.heard.discard(done)
            self.used.discard(done)
            if self.order:
                head = self.order[0]
                for pcm in self.pending.get(head, ()):
                    self.play(pcm)
                if head in self.pending:
                    self.pending[head] = []

    def _reset(self):
        if self.flusher:
            self.flusher.cancel()
            self.flusher = None
        self.current = None
        self.order.clear()
        self.pending.clear()
        self.finished.clear()
        self.heard.clear()
        self.used.clear()

    async def say(self, text):
        if self.ws is None or not text:
            return
        if self.flusher:
            self.flusher.cancel()
            self.flusher = None
        try:
            if self.current is None:
                await self._open()
            self.used.add(self.current)
            await self.ws.send(json.dumps({"stream_id": self.current, "text": text, "text_end": False}))
        except ConnectionClosed:
            return
        self.flusher = asyncio.get_running_loop().create_task(self._flush_later())

    async def _flush_later(self):
        """Soniox TTS holds text back until it sees what follows, i.e. until the speaker pauses.
        Translation arrives in clause-sized bursts, so a burst followed by quiet is closed and
        spoken at once instead of waiting for the end of the whole sentence."""
        await asyncio.sleep(self.FLUSH)
        self.flusher = None
        await self.end_utterance()

    async def end_utterance(self):
        sid = self.current
        if self.ws is None or sid is None or sid not in self.used:
            return
        self.current = None  # before the await: text arriving meanwhile opens the next stream
        self.last_warm = 0.0
        try:
            await self.ws.send(json.dumps({"stream_id": sid, "text": "", "text_end": True}))
        except ConnectionClosed:
            return
        await self._rewarm()  # the next utterance usually follows soon

    async def cancel_all(self):
        """Mute: drop everything queued or being generated."""
        if self.ws is not None:
            for sid in list(self.order):
                try:
                    await self.ws.send(json.dumps({"stream_id": sid, "cancel": True}))
                except ConnectionClosed:
                    break
        self._reset()


# --- REST: voices ---------------------------------------------------------------

def _rest(method, path, api_key, proxy, body=None, content_type=None):
    headers = {"Authorization": f"Bearer {api_key}"}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        status, data = https_request(method, f"{API_URL}{path}", headers, body, proxy)
    except (OSError, ProxyError) as e:
        raise CloneError(f"Нет связи с Soniox ({e}). Включи VPN.") from e
    if status in AUTH_CODES:
        raise CloneError("Soniox отклонил ключ SONIOX_API_KEY (или нет баланса).")
    if status >= 300:
        raise CloneError(f"Soniox: HTTP {status} {data[:300].decode('utf-8', 'replace')}")
    return json.loads(data) if data else {}


def create_voice(api_key, audio_bytes, proxy, filename="voice.wav"):
    """Upload my voice sample; returns the new voice id (processing takes a few seconds)."""
    boundary = uuid.uuid4().hex
    name = f"Live Translator {time.strftime('%Y-%m-%d %H-%M-%S')}"
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="name"\r\n\r\n{name}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f'Content-Type: {content_type}\r\n\r\n').encode() + audio_bytes + f"\r\n--{boundary}--\r\n".encode()
    return _rest("POST", "/v1/voices", api_key, proxy, body, f"multipart/form-data; boundary={boundary}")["id"]


def voice_status(api_key, voice_id, proxy):
    """'ready' / 'processing' / 'failed: <reason>' / 'not_computed' for the TTS model we use."""
    info = _rest("GET", f"/v1/voices/{voice_id}", api_key, proxy)
    for model in info.get("models", ()):
        if model.get("model") == TTS_MODEL:
            if model.get("status") == "failed":
                return f"failed: {model.get('error_message') or model.get('error_type')}"
            return model.get("status", "processing")
    return "processing"


def list_voices(api_key, proxy):
    """Built-in voices of the TTS model: [{name, gender, description}]."""
    for model in _rest("GET", "/v1/tts-models", api_key, proxy).get("models", ()):
        if model.get("id") == TTS_MODEL or model.get("aliased_model_id") == TTS_MODEL:
            return [{"name": v.get("name") or v.get("id"), "gender": v.get("gender", ""),
                     "description": v.get("description", "")} for v in model.get("voices", ())]
    return []


async def speak_once(api_key, voice, language, text, proxy):
    """Generate one phrase (voice preview) and return PCM16 24 kHz audio."""
    audio = bytearray()
    stream_id = uuid.uuid4().hex
    async with connect(TTS_URL, max_size=None, proxy=proxy, compression=None) as ws:
        tts = SonioxVoice(api_key, voice, language, None, proxy, None)
        await ws.send(json.dumps(tts._config(stream_id)))
        await ws.send(json.dumps({"stream_id": stream_id, "text": text, "text_end": True}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("error_code"):
                raise CloneError(f"Soniox: {msg.get('error_message', msg)}")
            if msg.get("audio"):
                audio += base64.b64decode(msg["audio"])
            if msg.get("terminated"):
                break
    return bytes(audio)
