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
import re
import time
import uuid
from collections import deque

import numpy as np
from python_socks import ProxyError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, WebSocketException

from speech_audio import HOLD, HOLD_MIN, RATE, LeadTrimmer, cut_tail, quiet_after, tail_keep, trim_lead
from voice_clone import CloneError, https_request

STT_URL = os.environ.get("LIVE_TRANSLATOR_SONIOX_STT", "wss://stt-rt.soniox.com/transcribe-websocket")
TTS_URL = os.environ.get("LIVE_TRANSLATOR_SONIOX_TTS", "wss://tts-rt.soniox.com/tts-websocket")
API_URL = os.environ.get("LIVE_TRANSLATOR_SONIOX_API", "https://api.soniox.com")
STT_MODEL = "stt-rt-v5"
TTS_MODEL = "tts-rt-v2"
KEY_ENV = "SONIOX_API_KEY"
DEFAULT_VOICE = "Adrian"
AUTH_CODES = (401, 402, 403)
RETRY_CODES = (408, 429)  # other 4xx (bad model, bad config) will not get better by reconnecting


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


CLAUSE_END = (".", ",", "!", "?", ";", ":", "…")
UNVOICED = re.compile(r"[\s.,!?;:…\"'«»“”‘’()\[\]—–]+")  # marks no voice speaks ("%" or "$" it does speak)
MARKERS = ("<end>", "<fin>")  # Soniox endpoint and manual-finalize markers: never captioned
RECENT = 100                  # frames (2 s) of speech kept while the connection is being (re)made


def drain(queue):
    while not queue.empty():
        queue.get_nowait()


def keep_recent(queue, frames=RECENT):
    """Keep only the last `frames` audio frames: words said while connecting are still translated,
    a long outage does not replay stale speech."""
    recent = []
    while not queue.empty():
        item = queue.get_nowait()
        if isinstance(item, bytes):
            recent.append(item)
    for item in recent[-frames:]:
        queue.put_nowait(item)


class AutoFinalize:
    """Closes the phrase I'm saying at a short pause (Soniox manual finalization) instead of waiting
    for the Soniox endpoint, and at once on the «я закончил» hotkey (force).

    Measured: a 200 ms pause inside "я… Python-разработчик" split it into "I am." / "A developer.",
    350 ms did not. So: 360 ms of quiet after real speech, only while words are still pending, at most
    once per 1.5 s (Soniox may disconnect on frequent finalizes) and only when my translation is not
    queued anyway (then an early final gains nothing)."""

    LOUD = 600           # RMS of a voiced 20 ms frame
    SPEECH = 6           # voiced frames (120 ms) before a pause counts
    PAUSE = 18           # quiet frames (360 ms)
    GAP = 1.5            # seconds between automatic finalizes
    FORCE_GAP = 1.0
    MAX_BACKLOG = 1.0    # seconds of my speech still queued
    SILENCE = 10         # zero frames (200 ms) sent before a forced finalize, as Soniox asks
    MESSAGE = json.dumps({"type": "finalize"})

    def __init__(self, enabled=True, backlog=None):
        self.enabled = enabled
        self.backlog = backlog or (lambda: 0.0)
        self.pending = False     # the last STT message still had non-final words
        self.voiced = self.quiet = 0
        self.last = self.last_force = float("-inf")
        self.forced = False

    def force(self):
        now = time.monotonic()
        if now - self.last_force >= self.FORCE_GAP:
            self.last_force = now
            self.forced = True

    def feed(self, pcm):
        """Extra messages to send right after this audio frame."""
        if self.forced:
            self.forced = False
            self._fired()
            return [bytes(len(pcm))] * self.SILENCE + [self.MESSAGE]
        samples = np.frombuffer(pcm, "<i2").astype(np.float32)
        if samples.size and np.sqrt(np.mean(samples ** 2)) >= self.LOUD:
            self.voiced, self.quiet = self.voiced + 1, 0
            return []
        self.quiet += 1
        if (self.enabled and self.pending and self.voiced >= self.SPEECH and self.quiet >= self.PAUSE
                and time.monotonic() - self.last >= self.GAP and self.backlog() < self.MAX_BACKLOG):
            self._fired()
            return [self.MESSAGE]
        return []

    def _fired(self):
        self.last = time.monotonic()
        self.voiced = 0
        self.pending = False


async def _pump(ws, queue, finalizer=None):
    while True:
        pcm = await queue.get()
        await ws.send(pcm)  # binary PCM frame
        for extra in finalizer.feed(pcm) if finalizer else ():
            await ws.send(extra)


async def run_stt_channel(ch, api_key, proxy, sink, target, hints, context, voice=None, diarize=False):
    """Transcribe + translate one audio source; final translated words go to captions and TTS.

    The translated words of one server message are spoken as one chunk, closed right away when the
    clause is done (punctuation or an endpoint), so speech starts without a timer."""
    speaker = None  # last speaker heard; translation tokens may come without one
    finalizer = getattr(ch, "finalizer", None)
    delay = 1
    while True:
        try:
            async with connect(STT_URL, max_size=None, proxy=proxy, compression=None,
                               ping_interval=5, ping_timeout=5) as ws:
                await ws.send(json.dumps(stt_config(api_key, target, hints, context, diarize)))
                keep_recent(ch.queue)
                sender = asyncio.create_task(_pump(ws, ch.queue, finalizer))
                accepted = False
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        code = msg.get("error_code")
                        if code:
                            text = f"Soniox: {msg.get('error_message', msg)}"
                            if code in AUTH_CODES:
                                raise SonioxFatal(text + "\nПроверь ключ SONIOX_API_KEY и баланс.")
                            if 400 <= code < 500 and code not in RETRY_CODES:
                                raise SonioxFatal(text)
                            sink.status(ch.dst_label, f"ошибка Soniox {code}, переподключение…", False)
                            sink.note(f"[{ch.dst_label}] {text}")
                            break
                        if not accepted:  # Soniox answers the config right away (no tokens yet)
                            accepted, delay = True, 1
                            sink.status(ch.dst_label, "подключено", True)
                        tokens = msg.get("tokens", ())
                        if finalizer:
                            finalizer.pending = any(not t.get("is_final") for t in tokens)
                        chunk, marker = [], False
                        for token in tokens:
                            if not token.get("is_final"):
                                continue
                            text = token.get("text", "")
                            if text in MARKERS:
                                marker = True
                                continue
                            who = {}
                            if diarize:
                                speaker = token.get("speaker") or speaker
                                who = {"speaker": speaker} if speaker else {}
                            if token.get("translation_status") == "translation":
                                sink.caption(f"{ch.kind}_dst", ch.dst_label, text, **who)
                                chunk.append(text)
                            else:
                                sink.caption(f"{ch.kind}_src", ch.src_label, text, **who)
                        if voice:
                            await _speak(voice, "".join(chunk), marker, ch.gate_out)
                        if msg.get("finished"):
                            break
                finally:
                    sender.cancel()
        except SonioxFatal:
            raise
        except InvalidStatus as e:
            sink.status(ch.dst_label, f"HTTP {e.response.status_code}, переподключение…", False)
        except (ConnectionClosed, OSError, ProxyError, InvalidHandshake) as e:
            sink.status(ch.dst_label, "нет связи, переподключение… (VPN включён?)", False)
            sink.note(f"[{ch.dst_label}] {e}")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 16)
        keep_recent(ch.queue)


async def _speak(voice, chunk, marker, gate_out):
    if chunk and not (gate_out and gate_out()):
        await voice.say(chunk, end=marker or chunk.rstrip().endswith(CLAUSE_END))
    elif marker:
        await voice.end_utterance()


CYRILLIC = re.compile(r"[\u0400-\u04FF]+")


def speakable(text):
    """Text for TTS without Cyrillic words: the other side must never hear Russian.

    Captions keep them; "" when nothing but punctuation is left."""
    if not text or not CYRILLIC.search(text):
        return text
    text = re.sub(r" {2,}", " ", re.sub(r"\s+([,.!?;:…])", r"\1", CYRILLIC.sub("", text)))
    return text if re.search(r"[^\W_]", text) else ""


class _Stream:
    """One clause on its way to the call: planned (no connection or no free slot yet) -> opened ->
    text sent -> audio arriving -> done (audio_end). A stock phrase from the cache is a stream that
    is done from the start ("clip:<id>")."""

    def __init__(self, sid, speed, text="", trim=True):
        self.sid, self.speed, self.text = sid, speed, text
        self.sent = 0            # characters of `text` the server has
        self.ended = self.end_sent = False
        self.born = 0.0          # when its first text was said: text queued offline expires
        self.heard = False       # first audio arrived
        self.audible = False     # first sound passed the lead trimmer
        self.played = False      # some of it went to the call
        self.done = False        # all of its audio arrived
        self.failed = False
        self.tries = 0           # times the server refused it before it was heard
        self.buf = b""           # audio not played yet
        self.quiet = 0           # samples of silence it ends with so far
        self.lead = LeadTrimmer(trim)
        self.render = None       # (phrase, variant) while rendering a stock phrase for the cache

    def restart(self, trim):
        """The connection died before any of it was heard: send it again from scratch."""
        self.sent, self.end_sent = 0, False
        self.heard = self.audible = self.failed = False
        self.buf, self.quiet, self.lead = b"", 0, LeadTrimmer(trim)


class SonioxVoice:
    """Speaks translated text in a Soniox voice (built-in name or my clone's id).

    One TTS stream per clause, heard strictly in order. Up to MAX_STREAMS generate at once (an opened,
    unused "warm" stream for the next clause included); text that finds no connection or no free slot
    waits in its place. Subclasses speak other providers' wire formats by overriding _connect,
    _open_msgs, _text_msgs, _cancel_msgs, _keepalive_msg and _normalize."""

    PROVIDER, KEY_ENV, FATAL, LABEL = "Soniox", KEY_ENV, SonioxFatal, "Мой голос"
    WARM = True        # open the next clause's stream before its text arrives
    KEEPALIVE = 20
    REWARM = 2.0       # at most one fresh warm stream per this many seconds
    FLUSH = 0.1        # translation quiet this long = a finished clause, speak it now
    MAX_STREAMS = 3    # opened and not terminated yet, the warm one included
    RETRY = 0.5        # pause after the server failed a stream or refused it for too many at once...
    RELIMIT = 30.0     # ...and how long fewer streams go at a time after a refusal
    RETRIES = 4        # a clause refused again after this many new tries is skipped
    TTL = 10.0         # text said while offline is dropped when it is older than this at reconnect
    RECYCLE = 150      # reconnect when idle this long: Soniox closes a connection after 3 min without audio
    TICK = 0.5         # background work (reconnect, stock phrases) is checked this often...
    QUIET = 2.0        # ...and runs only after I've been silent this long
    CPS = 14           # characters per second of speech at speed 1.0 (text not voiced yet)
    BOOST, MAX_SPEED = 1.25, 1.3
    BOOST_ON, BOOST_OFF = 1.5, 0.5  # backlog (s) that turns the faster speech on / off

    def __init__(self, api_key, voice, language, play, proxy, sink, on_first_audio=None, speed=1.0, backlog=None,
                 speed_boost=True, trim=True, phrases=None):
        self.api_key, self.voice, self.language = api_key, voice, language
        self.play, self.proxy, self.sink = play, proxy, sink
        self.on_first_audio, self.speed = on_first_audio, speed
        self.backlog = backlog or (lambda: 0.0)  # seconds queued in the player, not heard yet
        self.speed_boost, self.trim, self.phrases = speed_boost, trim, phrases
        self.trace = None  # optional callable(event, stream_id, **info) for latency measurements
        self.ws = None
        self.streams = {}        # sid -> _Stream, everything in `order`
        self.order = deque()     # streams in speaking order
        self.current = None      # stream accepting text
        self.live = set()        # opened on the server and not terminated: each takes a slot
        self.renders = {}        # sid -> _Stream rendering a stock phrase (never played)
        self.limit = self.MAX_STREAMS  # lowered for RELIMIT s after the server refused a stream
        self.boosting = self.recycling = self.overloaded = False
        self.last_warm = self.last_audio = self.last_say = self.retry_at = self.relimit_at = 0.0
        self.flusher = None
        self.tasks = set()       # fire-and-forget refills: referenced until done

    # --- wire format (other providers override these) ---------------------------------

    def _connect(self):
        return connect(TTS_URL, max_size=None, proxy=self.proxy, compression=None, ping_interval=5, ping_timeout=5)

    def _config(self, stream_id, speed=None):
        speed = self.speed if speed is None else speed
        config = {"api_key": self.api_key, "stream_id": stream_id, "model": TTS_MODEL, "voice": self.voice,
                  "language": self.language, "audio_format": "pcm_s16le", "sample_rate": 24000}
        if speed != 1.0:
            config["speed"] = speed
        return config

    def _open_msgs(self, st):
        return [self._config(st.sid, st.speed)]

    def _text_msgs(self, st, text, end):
        return [{"stream_id": st.sid, "text": text, "text_end": end}]

    def _cancel_msgs(self, st):
        return [{"stream_id": st.sid, "cancel": True}]

    def _keepalive_msg(self):
        return {"keep_alive": True}

    def _normalize(self, msg):
        """A server message in Soniox's shape: stream_id, audio (base64), audio_end, terminated,
        error_code / error_type / error_message; None to ignore it."""
        return msg

    # --- connection -------------------------------------------------------------------

    async def run(self):
        while True:
            try:
                async with self._connect() as ws:
                    await self._connected(ws)
                    tasks = [asyncio.create_task(self._keepalive()), asyncio.create_task(self._idle_loop())]
                    try:
                        async for raw in ws:
                            self._on_message(json.loads(raw))
                    finally:
                        for task in tasks:
                            task.cancel()
            except InvalidStatus as e:
                code = e.response.status_code
                if code in AUTH_CODES:
                    raise self._fatal(f"HTTP {code}") from e
                self.sink.status(self.LABEL, f"HTTP {code}, переподключение…", False)
            except (ConnectionClosed, OSError, ProxyError, InvalidHandshake) as e:
                self.sink.status(self.LABEL, "нет связи, переподключение…", False)
                self.sink.note(f"[{self.LABEL}] {e}")
            finally:
                self.ws = None
                self._reset()
            if self.recycling:
                self.recycling = False  # a planned reconnect: no pause, no alarm
            else:
                await asyncio.sleep(1)

    async def _connected(self, ws):
        self.ws, self.limit, self.retry_at, self.overloaded = ws, self.MAX_STREAMS, 0.0, False
        self.last_audio = time.monotonic()
        self._drop_stale()
        await self._drain()  # text said while the connection was down goes first...
        await self._warm()   # ...then the first clause skips stream setup
        self.sink.status(self.LABEL, "подключено", True)

    def _fatal(self, text):
        return self.FATAL(f"{self.PROVIDER} TTS: {text}\nПроверь ключ {self.KEY_ENV} и баланс.")

    async def _send(self, msgs):
        ws = self.ws
        try:
            for msg in msgs:
                if ws is None:
                    return
                await ws.send(json.dumps(msg))
        except ConnectionClosed:
            pass  # run() notices and re-queues what was not heard

    async def _keepalive(self):
        msg = self._keepalive_msg()
        while msg:
            await asyncio.sleep(self.KEEPALIVE)
            await self._send([msg])

    async def _idle_loop(self):
        while True:
            await asyncio.sleep(self.TICK)
            if not self._idle():
                continue
            if time.monotonic() - self.last_audio > self.RECYCLE:
                self.recycling = True  # before the server drops a connection that has been silent too long
                await self.ws.close()
                return
            await self._render_next()

    def _idle(self):
        return (self.ws is not None and not any(st.text for st in self.streams.values())
                and self.backlog() < 0.05 and time.monotonic() - self.last_say > self.QUIET)

    def _reset(self):
        """The connection is gone: clauses nobody heard yet keep their place and go out again after the
        reconnect; audio that already arrived in full stays and plays when its turn comes, without waiting
        for the reconnect; a clause cut off mid-word is dropped."""
        self.live.clear()
        self.renders.clear()
        for sid in list(self.order):
            st = self.streams[sid]
            if st.done and st.buf and not st.played:
                continue
            if st.text and not st.played:
                st.restart(self.trim)
            else:
                self._forget(st)
        self._advance()

    def _drop_stale(self):
        now = time.monotonic()
        for st in list(self.streams.values()):
            if st.text and not st.played and now - st.born > self.TTL:
                self._forget(st)
                self.sink.note(f"[{self.LABEL}] не озвучено (не было связи): {st.text.strip()}")
        self._advance()

    # --- streams ----------------------------------------------------------------------

    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _trace(self, event, sid, **info):
        if self.trace and sid not in self.renders:
            self.trace(event, sid, **info)

    def _free_slot(self):
        now = time.monotonic()
        limit = self.limit if now < self.relimit_at else self.MAX_STREAMS
        return self.ws is not None and len(self.live) < limit and now >= self.retry_at

    def _new_stream(self, speed):
        st = _Stream(uuid.uuid4().hex, speed, trim=self.trim)
        self.streams[st.sid] = st
        self.order.append(st.sid)
        self.current = st.sid
        return st

    def _forget(self, st):
        self.streams.pop(st.sid, None)
        if st.sid in self.order:
            self.order.remove(st.sid)
        if self.current == st.sid:
            self.current = None

    async def _open(self, st):
        self.live.add(st.sid)  # claimed before the await, so nobody opens it twice
        self._trace("open", st.sid)
        await self._send(self._open_msgs(st))
        await self._push(st)

    async def _push(self, st):
        """Send the text of an opened stream that the server doesn't have yet, and its end."""
        text, end = st.text[st.sent:], st.ended and not st.end_sent
        if st.sid not in self.live or not (text or end):
            return
        st.sent, st.end_sent = len(st.text), st.ended  # claimed before the await: sent exactly once
        self._trace("text", st.sid, text=text, end=end)
        await self._send(self._text_msgs(st, text, end))

    async def _drain(self):
        """Open the clauses waiting for a connection or a slot, in speaking order."""
        for sid in list(self.order):
            st = self.streams.get(sid)
            if st is None or not st.text or st.done or sid in self.live:
                continue
            if not self._free_slot() and not await self._make_room():
                return
            await self._open(st)

    async def _make_room(self):
        """A clause waiting for a slot goes before a warm stream waiting for text."""
        warm = self.streams.get(self.current)
        if (self.ws is None or time.monotonic() < self.retry_at or warm is None or warm.text
                or warm.sid not in self.live):
            return False
        await self._cancel(warm)
        return self._free_slot()

    async def _warm(self):
        """Open a stream for the next clause before its text arrives: it skips stream setup."""
        if not self.WARM or self.current is not None or not self._free_slot():
            return
        self.last_warm = time.monotonic()
        await self._open(self._new_stream(self.speed))

    async def _rewarm(self):
        wait = self.REWARM - (time.monotonic() - self.last_warm)
        if wait > 0:
            await asyncio.sleep(wait)  # throttled, not dropped: warm up again once allowed
        await self._warm()

    async def _refill(self, rewarm=True):
        """A slot freed: open the clauses waiting for one, then keep a warm stream ready."""
        while time.monotonic() < self.retry_at:  # the server refused a stream: not before RETRY s
            await asyncio.sleep(max(0.01, self.retry_at - time.monotonic()))
        await self._drain()
        if rewarm:
            await self._rewarm()

    async def _cancel(self, st):
        self._forget(st)
        if st.sid in self.live:
            self.live.discard(st.sid)  # its slot is free at once; late messages for it are ignored
            await self._send(self._cancel_msgs(st))

    # --- messages ---------------------------------------------------------------------

    def _find(self, sid):
        return self.streams.get(sid) or self.renders.get(sid)

    def _on_message(self, msg):
        msg = self._normalize(msg)
        if not msg:
            return
        sid = msg.get("stream_id")
        if msg.get("error_code"):
            self._on_error(sid, msg)
        if msg.get("audio") and self._find(sid):
            self.last_audio = time.monotonic()
            self._on_audio(self._find(sid), base64.b64decode(msg["audio"]))
        if msg.get("audio_end") and self._find(sid):
            self._on_audio_end(self._find(sid))
        if msg.get("terminated"):
            self._on_terminated(sid)

    def _on_error(self, sid, msg):
        code, kind = msg["error_code"], msg.get("error_type") or ""
        text = msg.get("error_message") or str(msg)
        if code in AUTH_CODES:
            raise self._fatal(text)
        st = self._find(sid)
        if st is None:
            if sid is None:  # not about a stream: say so; a cancelled stream's error is of no interest
                self.sink.note(f"[{self.LABEL}] {text}")
            return
        if code == 429 and not st.heard:
            self._refused(st.sid)
        if st.render:
            st.failed = code != 429  # a busy server is no reason to give the phrase up
        elif kind == "request_timeout" and not st.text:
            pass  # an idle pre-warmed stream expired: nothing was lost
        elif code >= 500 and not st.heard:  # a server hiccup: the clause goes again once it may be over
            self.sink.note(f"[{self.LABEL}] {text}")
            self.retry_at = time.monotonic() + self.RETRY
            self._retry(st)
        elif code in RETRY_CODES and not st.heard:
            self._retry(st)
        else:
            st.failed = True
            if kind.startswith("voice_"):
                self.sink.status(self.LABEL, "клон недоступен — запиши голос заново", False)
            self.sink.note(f"[{self.LABEL}] {text}")

    def _refused(self, sid):
        """The server refused a stream for too many at once: none for RETRY s, fewer at a time for RELIMIT s."""
        self.live.discard(sid)
        now = time.monotonic()
        self.limit = max(1, len(self.live))
        self.retry_at, self.relimit_at = now + self.RETRY, now + self.RELIMIT

    def _retry(self, st):
        """Nobody heard the stream the server refused or let expire: its clause keeps its place in line and
        goes out again once a slot frees; one refused too often is skipped."""
        self.live.discard(st.sid)
        if st.text and st.tries >= self.RETRIES:
            self._give_up(st)
            return  # its terminated moves playback on
        if st.text:
            fresh = _Stream(uuid.uuid4().hex, st.speed, st.text, self.trim)
            fresh.ended, fresh.born, fresh.tries = st.ended, st.born, st.tries + 1
            self.order[self.order.index(st.sid)] = fresh.sid
            self.streams[fresh.sid] = fresh
            del self.streams[st.sid]
            if self.current == st.sid:
                self.current = fresh.sid
            self._trace("retry", fresh.sid, was=st.sid)
        else:
            self._forget(st)
        self._spawn(self._refill())

    def _give_up(self, st):
        st.failed = self.overloaded = True
        self.sink.status(self.LABEL, "сервер перегружен — фраза пропущена", False)
        self.sink.note(f"[{self.LABEL}] не озвучено (сервер занят): {st.text.strip()}")

    def _on_audio(self, st, pcm):
        if st.done:
            return
        if st.render:
            st.buf += pcm
            return
        if not st.heard:
            st.heard = True
            self._trace("first_audio", st.sid, pcm=pcm)
        pcm = st.lead.feed(pcm)
        if not pcm:
            return
        st.quiet = quiet_after(pcm, st.quiet)
        if not st.audible:
            st.audible = True
            self._trace("first_audible", st.sid)
            if self.on_first_audio:
                self.on_first_audio()
        st.buf += pcm
        if self.order[0] == st.sid:
            self._release(st)

    def _on_audio_end(self, st):
        if st.render:
            self._rendered(st)
        elif not st.done:
            st.done = True
            self._trace("audio_end", st.sid)
            if self.overloaded and not st.failed:  # clauses are heard again after one was skipped
                self.overloaded = False
                self.sink.status(self.LABEL, "подключено", True)
            self._advance()

    def _on_terminated(self, sid):
        """A stream is over on the server: its slot is free (playback moved on at audio_end already)."""
        self.live.discard(sid)
        self._trace("terminated", sid)
        st, rewarm = self._find(sid), True
        if st is not None:
            if self.current == sid:
                self.current = None  # the warm stream expired, or the clause failed
            rewarm = not st.failed   # a rejected voice would fail again: no warm-up loop
            if st.render:
                self._rendered(st)
                del self.renders[sid]
            elif not st.done:  # ended without audio_end (an error): skip it
                st.done = True
                self._advance()
        self._spawn(self._refill(rewarm=rewarm))

    # --- playback ---------------------------------------------------------------------

    def _advance(self):
        """Play the head stream; when all of it is out, move on to the next clause."""
        while self.order:
            st = self.streams[self.order[0]]
            self._release(st)
            if not st.done:
                return
            self._forget(st)

    def _release(self, st):
        """Play what the stream being heard has. Once the server has all of its text, its last HOLD s wait
        while the player has enough queued: at a seam with the next clause its trailing silence can still
        be cut. (Before that the audio may pause for more text: a held end would put the gap mid-word.)"""
        pcm = st.buf
        if st.done:
            keep = tail_keep(st.text) if self.trim and self._seam() else None
            if keep is not None:
                pcm = cut_tail(pcm, st.quiet, keep)
            st.buf = b""
        else:
            hold = int(HOLD * RATE) * 2 if self.trim and st.end_sent and self.backlog() >= HOLD_MIN else 0
            cut = max(0, len(pcm) - hold) // 2 * 2
            pcm, st.buf = pcm[:cut], pcm[cut:]
        if pcm:
            st.played = True
            self.play(pcm)

    def _seam(self):
        """The next clause is waiting right behind the one being heard."""
        return len(self.order) > 1 and bool(self.streams[self.order[1]].text)

    def queued_seconds(self):
        """Seconds of my speech not heard yet: the player's queue, audio held here, text not voiced yet."""
        total = self.backlog()
        for st in self.streams.values():
            total += len(st.buf) / 2 / RATE
            if st.text and not st.heard:
                total += len(st.text) / (self.CPS * st.speed)
        return total

    def _clause_speed(self):
        """Base speed, or faster while I'm far behind (on above BOOST_ON s of backlog, off below BOOST_OFF)."""
        if not self.speed_boost:
            return self.speed
        behind = self.queued_seconds()
        if behind > self.BOOST_ON:
            self.boosting = True
        elif behind < self.BOOST_OFF:
            self.boosting = False
        return min(self.MAX_SPEED, max(self.speed, self.BOOST)) if self.boosting else self.speed

    # --- stock phrases ----------------------------------------------------------------

    def _queued(self):
        return any(st.text and not st.done and sid not in self.live for sid, st in self.streams.items())

    def _play_clip(self, text):
        """A whole short answer I have ready in this voice plays at once, without a TTS round trip."""
        if not self.phrases or text.rstrip()[-1:] in (",", ";", ":") or self._queued():
            return False
        warm = self.streams.get(self.current)
        if warm is not None and warm.text:
            return False  # in the middle of an utterance
        pcm = self.phrases.match(text)
        if not pcm:
            return False
        st = _Stream(f"clip:{uuid.uuid4().hex}", self.speed, text)
        st.ended = st.end_sent = st.heard = st.audible = st.done = True
        st.born = self.last_say
        st.buf = trim_lead(pcm) if self.trim else pcm
        st.quiet = quiet_after(st.buf)
        self.streams[st.sid] = st
        if warm is not None:
            self.order.insert(self.order.index(warm.sid), st.sid)  # before the unused warm stream
        else:
            self.order.append(st.sid)
        self._trace("clip", st.sid, text=text)
        self._trace("first_audible", st.sid)
        if self.on_first_audio:
            self.on_first_audio()
        self._advance()
        return True

    async def _render_next(self):
        """Render one missing stock phrase into the cache: only while I'm silent, one at a time."""
        if not self.phrases or self.renders or self.language != "en" or not self._free_slot():
            return
        item = self.phrases.next_missing()
        if item is None:
            return
        st = _Stream(uuid.uuid4().hex, self.speed, item[0])
        st.ended, st.render = True, item
        self.renders[st.sid] = st
        await self._open(st)

    def _rendered(self, st):
        if st.done:
            return
        st.done = True
        if st.failed:
            self.phrases.skip(*st.render)
        elif st.buf:
            self.phrases.store(*st.render, st.buf)

    # --- what the engine calls --------------------------------------------------------

    async def say(self, text, end=False):
        """Speak translated text; end=True closes the clause in the same message (speech starts at once)."""
        text = speakable(text)
        if not text:
            if end:
                await self.end_utterance()
            return  # a pending flusher still closes the clause
        if UNVOICED.fullmatch(text) and not self._saying():
            self._punctuate(text)
            return
        if self.flusher:
            self.flusher.cancel()
            self.flusher = None
        self.last_say = time.monotonic()
        if end and self._play_clip(text):
            return
        st = await self._stream_for_text()
        st.text += text
        st.born = st.born or self.last_say
        if end:
            st.ended = True
            self.current = None  # before the await: text arriving meanwhile opens the next stream
            self.last_warm = 0.0
        if st.sid in self.live:
            await self._push(st)
        else:
            await self._drain()  # opens it now if it may go ahead of nothing and a slot is free
        if end:
            await self._rewarm()  # the next clause usually follows soon
        else:
            self.flusher = asyncio.get_running_loop().create_task(self._flush_later())

    def _saying(self):
        st = self.streams.get(self.current)
        return st is not None and bool(st.text)

    def _punctuate(self, text):
        """Punctuation that arrives after its clause was closed is no clause of its own (a TTS round trip for a
        click): it ends the last clause's text, so the pause after that clause stays a sentence's at a seam."""
        for sid in reversed(self.order):
            st = self.streams[sid]
            if st.text:
                st.text += text
                return

    async def _stream_for_text(self):
        st = self.streams.get(self.current)
        if st is not None and st.text:
            return st  # the clause being said
        speed = self._clause_speed()
        if st is None:
            return self._new_stream(speed)
        if st.speed != speed:  # the warm stream is at base speed and I'm far behind
            warm, st = st, self._new_stream(speed)  # current moves on before the await
            await self._cancel(warm)
        return st

    async def _flush_later(self):
        """Soniox TTS holds text back until it sees what follows, i.e. until the speaker pauses.
        Translation arrives in clause-sized bursts, so a burst followed by quiet is closed and
        spoken at once instead of waiting for the end of the whole sentence."""
        await asyncio.sleep(self.FLUSH)
        self.flusher = None
        await self.end_utterance()

    async def end_utterance(self):
        st = self.streams.get(self.current)
        if st is None or not st.text:
            return  # nothing said since the last clause: the warm stream stays unused
        self.current = None  # before the await: text arriving meanwhile opens the next stream
        self.last_warm = 0.0
        st.ended = True
        await self._push(st)
        await self._rewarm()  # the next utterance usually follows soon

    async def cancel_all(self):
        """Mute: drop everything queued or being generated."""
        if self.flusher:
            self.flusher.cancel()
            self.flusher = None
        streams = [self.streams[sid] for sid in self.order]
        self.order.clear()
        self.streams.clear()
        self.current = None
        for st in streams:
            if st.sid in self.live:
                self.live.discard(st.sid)
                await self._send(self._cancel_msgs(st))


async def render_once(voice, text):
    """One phrase over its own connection, in the voice's wire format: PCM16 24 kHz audio."""
    st = _Stream(uuid.uuid4().hex, voice.speed, text)
    audio = bytearray()
    try:
        async with voice._connect() as ws:
            for msg in voice._open_msgs(st) + voice._text_msgs(st, text, True):
                await ws.send(json.dumps(msg))
            async for raw in ws:
                msg = voice._normalize(json.loads(raw)) or {}
                if msg.get("stream_id") not in (st.sid, None):
                    continue
                if msg.get("error_code"):
                    if msg["error_code"] in AUTH_CODES:
                        raise CloneError(f"{voice.PROVIDER} отклонил ключ {voice.KEY_ENV}: {msg.get('error_message')}")
                    raise CloneError(f"{voice.PROVIDER}: {msg.get('error_message', msg)}")
                if msg.get("audio"):
                    audio += base64.b64decode(msg["audio"])
                if msg.get("audio_end") or msg.get("terminated"):
                    break
    except InvalidStatus as e:
        code = e.response.status_code
        if code in AUTH_CODES:
            raise CloneError(f"{voice.PROVIDER} отклонил ключ {voice.KEY_ENV}.") from e
        raise CloneError(f"{voice.PROVIDER}: HTTP {code}") from e
    except (OSError, ProxyError, WebSocketException) as e:
        raise CloneError(f"Нет связи с {voice.PROVIDER} ({e}). Включи VPN.") from e
    return bytes(audio)


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


def delete_voice(api_key, voice_id, proxy):
    """Remove a clone I no longer use: Soniox keeps at most 20 custom voices per organization."""
    _rest("DELETE", f"/v1/voices/{voice_id}", api_key, proxy)


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


async def speak_once(api_key, voice, language, text, proxy, speed=1.0):
    """Generate one phrase (voice preview) and return PCM16 24 kHz audio."""
    return await render_once(SonioxVoice(api_key, voice, language, None, proxy, None, speed=speed), text)
