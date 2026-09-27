"""
Live call translator engine + console mode (Zoom, Telegram, WhatsApp, Discord, Meet, Teams).

  You:  microphone -> gpt-realtime-translate -> English voice -> VB-Cable -> the call hears English
  Them: what plays in your headphones -> gpt-realtime-translate -> Russian subtitles

In the call app pick microphone "CABLE Output (VB-Audio Virtual Cable)".
The window version is app.py (ui/); this file also runs in the console:
  py -3 live_translator.py                 # both directions
  py -3 live_translator.py --no-listen     # only your voice -> English
  py -3 live_translator.py --monitor       # also hear your translation in the headphones
  py -3 live_translator.py --passthrough   # no API: mic straight into the cable (routing test)
  py -3 live_translator.py --list          # list audio devices
Ctrl+Alt+M mutes/unmutes your microphone from any app.
"""
import argparse
import asyncio
import base64
import ctypes
import json
import os
import sys
import threading
import time
import urllib.request
import warnings
from ctypes import wintypes
from pathlib import Path
from queue import SimpleQueue

import numpy as np
import sounddevice as sd
# Import order matters: sounddevice puts the main thread in a COM STA first, which soundcard tolerates
import soundcard as sc
from python_socks import ProxyError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

import soniox_engine
import voice_clone

URL = os.environ.get("LIVE_TRANSLATOR_URL",
                     "wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate")
RATE = 24_000  # API requires mono PCM16 at 24 kHz
BLOCK = 480    # 20 ms per chunk

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
ENV_FILE = APP_DIR / ".env"

HOTKEY_NAME = "Ctrl+Alt+M"

FATAL_ERRORS = {  # API error codes that reconnecting won't fix
    "invalid_api_key": "Неверный ключ OPENAI_API_KEY.",
    "insufficient_quota": "Пополни баланс: platform.openai.com/settings/organization/billing.",
    "unsupported_country_region_territory": "OpenAI блокирует твой регион: включи VPN.",
    "model_not_found": "У аккаунта API нет доступа к gpt-realtime-translate.",
}


class Fatal(Exception):
    pass


def load_api_key(env="OPENAI_API_KEY"):
    key = os.environ.get(env)
    if not key and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == env:
                key = value.strip().strip('"').strip("'")
    return key


def save_api_key(key, env="OPENAI_API_KEY"):
    lines = []
    if ENV_FILE.exists():
        lines = [line for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
                 if line.partition("=")[0].strip() != env]
    lines.append(f"{env}={key}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[env] = key


def detect_proxy(explicit):
    """Explicit --proxy wins ("none" disables); else the Windows system proxy (VPN clients like v2rayN)."""
    if explicit:
        return None if explicit.lower() == "none" else explicit
    proxies = urllib.request.getproxies()
    url = proxies.get("socks") or proxies.get("https") or proxies.get("all")
    if not url:
        return None
    scheme, _, rest = url.partition("://")
    # Windows reports "socks=host:port" as socks:// or socks4://; VPN clients serve SOCKS5
    return "socks5h://" + rest if scheme.startswith("socks") else url


def wasapi_index():
    for i, api in enumerate(sd.query_hostapis()):
        if "WASAPI" in api["name"]:
            return i
    return None


def pick_device(name, kind):
    """Resolve a device by index or name substring; prefer WASAPI (lowest latency)."""
    wasapi = wasapi_index()
    if name is None:
        if wasapi is not None:
            idx = sd.query_hostapis(wasapi)[f"default_{kind}_device"]
            if idx >= 0:
                return idx
        return sd.default.device[0 if kind == "input" else 1]
    if str(name).isdigit():
        return int(name)
    channels = f"max_{kind}_channels"
    matches = [i for i, d in enumerate(sd.query_devices())
               if name.lower() in d["name"].lower() and d[channels] > 0]
    if not matches:
        raise Fatal(f"Аудиоустройство не найдено: {name!r}. Проверь, что VB-Cable установлен "
                    "(vb-audio.com/Cable), или запусти консольную версию с --list.")
    matches.sort(key=lambda i: sd.query_devices(i)["hostapi"] != wasapi)
    return matches[0]


def stream_kwargs(device):
    extra = None
    if sd.query_devices(device)["hostapi"] == wasapi_index():
        extra = sd.WasapiSettings(auto_convert=True)  # let Windows resample to/from 24 kHz
    return dict(device=device, samplerate=RATE, channels=1, dtype="int16",
                blocksize=BLOCK, latency="low", extra_settings=extra)


class Player:
    """Thread-safe PCM16 FIFO drained by an output stream callback."""

    HANG = 0.3  # seconds a player still counts as busy after its last sound

    def __init__(self, device):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._last_sound = 0.0
        self.gain = 1.0
        self.stream = sd.RawOutputStream(callback=self._callback, **stream_kwargs(device))

    def _callback(self, outdata, frames, time_info, status):
        n = len(outdata)
        with self._lock:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
        if chunk:
            self._last_sound = time.monotonic()
        outdata[:len(chunk)] = chunk
        outdata[len(chunk):] = b"\x00" * (n - len(chunk))

    def feed(self, pcm):
        if self.gain != 1.0:
            samples = np.frombuffer(pcm, "<i2").astype(np.float32) * self.gain
            pcm = np.clip(samples, -32768, 32767).astype("<i2").tobytes()
        with self._lock:
            self._buf += pcm

    def clear(self):
        with self._lock:
            self._buf.clear()

    @property
    def busy(self):
        return bool(self._buf) or time.monotonic() - self._last_sound < self.HANG


class LagMeter:
    """Rough delay from 'you started a phrase' to 'first translated sound', for tuning VPN/mic."""

    LOUD = 700  # int16 RMS treated as speech
    GAP = 0.6   # silence (s) that separates phrases
    STALE = 6.0

    def __init__(self):
        self.last_loud = 0.0
        self.speech_start = None
        self.last_out = 0.0

    def on_input(self, rms):
        now = time.monotonic()
        if rms > self.LOUD:
            stale = self.speech_start is None or now - self.speech_start > self.STALE
            if now - self.last_loud > self.GAP and stale:
                self.speech_start = now
            self.last_loud = now

    def on_output(self):
        now = time.monotonic()
        lag = None
        if now - self.last_out > self.GAP and self.speech_start is not None:
            lag = now - self.speech_start
            self.speech_start = None
        self.last_out = now
        return lag if lag is not None and lag < self.STALE else None


class Sink:
    """Where the engine reports; the console and the window implement it.

    Caption kinds: me_src, me_dst (your speech and its translation), them_src, them_dst.
    """

    def caption(self, kind, label, text, speaker=None): pass
    def note(self, text): pass
    def status(self, label, text, ok): pass
    def lag(self, seconds): pass
    def level(self, me, them): pass  # mic / call loudness 0..1, ~10 times a second
    async def run(self): pass


class ConsoleSink(Sink):
    """Prints whole phrases: collects streaming deltas until punctuation or a pause."""

    IDLE = 0.8
    STYLES = {"me_src": "\033[90m", "me_dst": "\033[96m", "them_src": "\033[90m", "them_dst": "\033[93;1m"}
    DIM, RESET = "\033[90m", "\033[0m"

    def __init__(self):
        self.buf = {}  # label -> [text, kind, last_update]

    def caption(self, kind, label, text, speaker=None):
        if speaker:
            label = f"{label} {speaker}"
        entry = self.buf.setdefault(label, ["", kind, 0.0])
        entry[0] += text
        entry[2] = time.monotonic()
        if entry[0].rstrip().endswith((".", "?", "!", "…")):
            self.flush(label)

    def flush(self, label):
        text, kind, _ = self.buf.pop(label)
        if text.strip():
            print(f"{self.STYLES[kind]}{label:>8}: {text.strip()}{self.RESET}", flush=True)

    def note(self, text):
        print(f"{self.DIM}{'':>8}  {text}{self.RESET}", flush=True)

    def status(self, label, text, ok):
        self.note(f"[{label}] {text}")

    def lag(self, seconds):
        self.note(f"задержка ≈ {seconds:.1f} с")

    async def run(self):
        while True:
            await asyncio.sleep(0.2)
            now = time.monotonic()
            for label in [k for k, v in self.buf.items() if now - v[2] > self.IDLE]:
                self.flush(label)


class Channel:
    """One direction: audio queue -> gpt-realtime-translate -> audio to players and/or captions."""

    def __init__(self, name, lang, queue, players, kind, lag=None, gate_out=None, voice=None):
        self.name, self.lang, self.queue, self.players = name, lang, queue, players
        self.kind, self.lag, self.gate_out = kind, lag, gate_out
        self.voice = voice  # CloneVoice: speak the translated text in my cloned voice
        self.src_label, self.dst_label = name, f"{name} → {lang.upper()}"


async def pump_audio(ws, queue):
    while True:
        pcm = await queue.get()
        await ws.send(json.dumps({
            "type": "session.input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode(),
        }))


async def run_session(ch, key, proxy, sink):
    headers = {"Authorization": f"Bearer {key}"}
    async with connect(URL, additional_headers=headers, max_size=None,
                       proxy=proxy, compression=None) as ws:
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {"audio": {
                "input": {
                    "transcription": {"model": "gpt-realtime-whisper"},
                    "noise_reduction": {"type": "near_field"},
                },
                "output": {"language": ch.lang},
            }},
        }))
        while not ch.queue.empty():  # drop audio captured while (re)connecting
            ch.queue.get_nowait()
        sender = asyncio.create_task(pump_audio(ws, ch.queue))
        try:
            async for raw in ws:
                event = json.loads(raw)
                kind = event.get("type")
                if kind == "session.output_audio.delta":
                    if ch.voice or not ch.players or (ch.gate_out and ch.gate_out()):
                        continue
                    lag = ch.lag.on_output() if ch.lag else None
                    if lag is not None:
                        sink.lag(lag)
                    pcm = base64.b64decode(event["delta"])
                    for p in ch.players:
                        p.feed(pcm)
                elif kind == "session.input_transcript.delta":
                    sink.caption(f"{ch.kind}_src", ch.src_label, event["delta"])
                elif kind == "session.output_transcript.delta":
                    sink.caption(f"{ch.kind}_dst", ch.dst_label, event["delta"])
                    if ch.voice and not (ch.gate_out and ch.gate_out()):
                        await ch.voice.say(event["delta"])
                elif kind == "session.updated":
                    sink.status(ch.dst_label, "подключено", True)
                elif kind == "error":
                    err = event.get("error") or {}
                    if err.get("code") in FATAL_ERRORS:
                        raise Fatal(f"{err.get('message')}\n{FATAL_ERRORS[err['code']]}")
                    sink.note(f"[API error] {err or event}")
        finally:
            sender.cancel()


async def run_channel(ch, key, proxy, sink):
    while True:
        try:
            await run_session(ch, key, proxy, sink)
        except InvalidStatus as e:
            code = e.response.status_code
            if code in (401, 403):
                raise Fatal(f"API отклонил запрос (HTTP {code}): неверный ключ, нет оплаты "
                            "или регион заблокирован — включи VPN.")
            sink.status(ch.dst_label, f"HTTP {code}, переподключение…", False)
        except (ConnectionClosed, OSError, ProxyError) as e:
            sink.status(ch.dst_label, "нет связи, переподключение… (VPN включён?)", False)
            sink.note(f"[{ch.dst_label}] {e}")
        await asyncio.sleep(2)


def start_loopback(name, loop, queue, gate, on_rms=None):
    """Capture what plays in the headphones (the other person) on a background thread.

    Returns (device name, stop event)."""
    started = SimpleQueue()
    stop = threading.Event()

    def worker():
        # The main thread is a COM STA (PortAudio), so this thread joins the MTA itself
        ctypes.windll.ole32.CoInitializeEx(None, 0)
        try:
            warnings.filterwarnings("ignore", category=getattr(sc, "SoundcardRuntimeWarning", RuntimeWarning))
            speaker = sc.default_speaker() if name is None else sc.get_speaker(name)
            source = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            recorder = source.recorder(samplerate=RATE, channels=1, blocksize=BLOCK)
            rec = recorder.__enter__()
        except Exception as e:
            started.put(e)
            return
        started.put(speaker.name)
        try:
            while not stop.is_set():
                data = rec.record(numframes=BLOCK)[:, 0]
                if gate():  # don't subtitle our own translation playing in the headphones
                    data = np.zeros_like(data)
                if on_rms:
                    on_rms(float(np.sqrt(np.mean(data ** 2))) * 32767)
                pcm = (np.clip(data, -1, 1) * 32767).astype("<i2").tobytes()
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, pcm)
                except RuntimeError:  # event loop closed
                    return
        finally:
            recorder.__exit__(None, None, None)

    threading.Thread(target=worker, daemon=True).start()
    result = started.get()
    if isinstance(result, Exception):
        raise Fatal(f"Не удалось слушать собеседника ({result}). Запусти с --no-listen или укажи --listen.")
    return result, stop


def start_hotkey(callback):
    """Call `callback` on Ctrl+Alt+M from any app. Returns False if another program owns the hotkey."""
    registered = SimpleQueue()

    def worker():
        user32 = ctypes.windll.user32
        mod_alt, mod_control, mod_norepeat, vk_m, wm_hotkey = 0x1, 0x2, 0x4000, 0x4D, 0x312
        ok = user32.RegisterHotKey(None, 1, mod_control | mod_alt | mod_norepeat, vk_m)
        registered.put(bool(ok))
        if not ok:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == wm_hotkey:
                callback()

    threading.Thread(target=worker, daemon=True).start()
    return registered.get()


class Engine:
    """Opens the audio devices and runs both translation channels until cancelled."""

    def __init__(self, args, sink):
        self.args, self.sink = args, sink
        self.muted = False
        self.paused = False    # both directions stopped (from the floating subtitles)
        self.voice_out = True  # speak my translation into the call
        self.volume = 1.0
        self.players = []
        self.monitor = None
        self.mic_rms = self.them_rms = 0.0
        self.voice = None  # CloneVoice in "my voice" mode
        self.loop = None

    def set_voice_out(self, on):
        self.voice_out = on
        if not on:
            self._cut_speech()

    def _cut_speech(self):
        for p in self.players:
            p.clear()
        if self.voice and self.loop:
            asyncio.run_coroutine_threadsafe(self.voice.cancel_all(), self.loop)

    def set_volume(self, volume):
        self.volume = volume
        for p in self.players:
            p.gain = volume

    def set_muted(self, muted):
        self.muted = muted
        if muted:  # cut off translation that is still playing
            self._cut_speech()

    def set_paused(self, paused):
        self.paused = paused
        if paused:
            self._cut_speech()

    def set_monitor(self, on):
        """Hear your own translation in the headphones; can be switched while running."""
        self.args.monitor = on
        if on and self.monitor is None and self.players:
            monitor = Player(pick_device(self.args.monitor_device, "output"))
            monitor.gain = self.volume
            monitor.stream.start()
            self.monitor = monitor
            self.players.append(monitor)
        elif not on and self.monitor is not None:
            monitor, self.monitor = self.monitor, None
            self.players.remove(monitor)
            monitor.stream.stop()
            monitor.stream.close()

    async def report_level(self):
        while True:
            await asyncio.sleep(0.1)
            self.sink.level(min(1.0, self.mic_rms / 6000), min(1.0, self.them_rms / 6000))

    def _set_them_rms(self, rms):
        self.them_rms = rms

    def _play(self, pcm):
        """Synthesized speech (cloned or built-in voice) into the call."""
        if not (self.muted or not self.voice_out):
            for p in self.players:
                p.feed(pcm)

    def _first_audio(self, lag):
        def report():
            measured = lag.on_output()
            if measured is not None:
                self.sink.lag(measured)
        return report

    def _openai_jobs(self, me, them, proxy, lag):
        args = self.args
        key = load_api_key()
        if not key:
            raise Fatal("Не задан OPENAI_API_KEY (Настройки → Ключ OpenAI).")
        jobs = []
        if me:
            if args.voice == "clone":
                cartesia = load_api_key(voice_clone.KEY_ENV)
                if not cartesia:
                    raise Fatal("Для клона голоса в движке OpenAI нужен ключ Cartesia (Настройки).")
                if not args.voice_id:
                    raise Fatal("Клон голоса ещё не создан: 🔊 → «Записать мой голос».")
                self.voice = voice_clone.CloneVoice(
                    cartesia, args.voice_id, args.lang, self._play, proxy,
                    voice_clone.BUFFER_MS.get(args.voice_delay, 500), self.sink, self._first_audio(lag))
                me.voice = self.voice
                jobs += [self.voice.run(), self.voice.watchdog()]
                self.sink.note("Движок: OpenAI · голос: мой клон (Cartesia)")
            elif args.voice == "off":
                me.players = []  # text only: the translator's own voice stays out of the call
                self.sink.note("Движок: OpenAI · голос выключен (только текст)")
            else:
                self.sink.note("Движок: OpenAI · голос модели")
            jobs.append(run_channel(me, key, proxy, self.sink))
        if them:
            jobs.append(run_channel(them, key, proxy, self.sink))
        return jobs

    def _soniox_jobs(self, me, them, proxy, lag):
        args = self.args
        key = load_api_key(soniox_engine.KEY_ENV)
        if not key:
            raise Fatal("Нужен ключ Soniox (Настройки → Ключ Soniox, console.soniox.com).")
        keywords, context = getattr(args, "keywords", None) or [], getattr(args, "context", None) or ""
        jobs = []
        if me:
            if args.voice == "clone" and not args.voice_id:
                raise Fatal("Клон голоса ещё не создан: 🔊 → «Записать мой голос».")
            if args.voice != "off":
                voice = args.voice_id if args.voice == "clone" else (args.voice_name or soniox_engine.DEFAULT_VOICE)
                self.voice = soniox_engine.SonioxVoice(key, voice, args.lang, self._play, proxy, self.sink,
                                                       self._first_audio(lag), args.speed)
                me.voice = self.voice
                jobs.append(self.voice.run())
            label = "мой клон" if args.voice == "clone" else (args.voice_name or soniox_engine.DEFAULT_VOICE)
            self.sink.note(f"Движок: Soniox · голос: {label if args.voice != 'off' else 'выключен'}")
            jobs.append(soniox_engine.run_stt_channel(
                me, key, proxy, self.sink, args.lang, [args.their_lang],
                soniox_engine.build_context(keywords, context), self.voice))
        if them:
            jobs.append(soniox_engine.run_stt_channel(
                them, key, proxy, self.sink, args.their_lang, [args.lang],
                soniox_engine.build_context(keywords, context, reverse=True),
                diarize=getattr(args, "diarize", True)))
        return jobs

    async def run(self):
        args, sink = self.args, self.sink
        if args.no_me and args.no_listen:
            raise Fatal("Выбери хотя бы один источник звука: микрофон или звук компьютера.")
        loop = self.loop = asyncio.get_running_loop()
        mic_q = asyncio.Queue()
        lag = LagMeter()

        out_dev = pick_device(args.out, "output")
        self.players = [Player(out_dev)]
        if args.monitor:
            self.monitor = Player(pick_device(args.monitor_device, "output"))
            self.players.append(self.monitor)
        for p in self.players:
            p.gain = self.volume

        def on_mic(indata, frames, time_info, status):
            pcm = bytes(indata)
            if self.muted or self.paused:
                pcm = bytes(len(pcm))  # the API expects a continuous stream, so send silence
                self.mic_rms = 0.0
            else:
                self.mic_rms = float(np.sqrt(np.mean(np.frombuffer(pcm, "<i2").astype(np.float32) ** 2)))
                if not args.passthrough:
                    lag.on_input(self.mic_rms)
            if args.passthrough:
                self.players[0].feed(pcm)
            elif not args.no_me:
                loop.call_soon_threadsafe(mic_q.put_nowait, pcm)

        in_dev = pick_device(args.inp, "input")
        mic = sd.RawInputStream(callback=on_mic, **stream_kwargs(in_dev))
        sink.note(f"Микрофон: {sd.query_devices(in_dev)['name']}")
        sink.note(f"Для звонка: {sd.query_devices(out_dev)['name']}")

        for p in self.players:
            p.stream.start()
        mic.start()
        stop_loopback = None
        level_task = asyncio.create_task(self.report_level())
        try:
            if args.passthrough:
                sink.status("Проверка", "голос без перевода идёт в кабель", True)
                await asyncio.Event().wait()

            proxy = detect_proxy(args.proxy)
            sink.note(f"Прокси: {proxy or 'нет'}")

            me = them = None
            if not args.no_me:
                me = Channel("Я", args.lang, mic_q, self.players, "me", lag,
                             gate_out=lambda: self.muted or not self.voice_out)
            if not args.no_listen:
                their_q = asyncio.Queue()
                heard, stop_loopback = start_loopback(
                    args.listen, loop, their_q,
                    lambda: self.paused or (self.monitor is not None and self.monitor.busy),
                    self._set_them_rms)
                sink.note(f"Собеседник: {heard}")
                them = Channel("Он", args.their_lang, their_q, [], "them")
            if args.engine == "soniox" and args.voice == "model":
                args.voice = "builtin"  # the translator's own voice exists only in the OpenAI engine
            jobs = (self._soniox_jobs if args.engine == "soniox" else self._openai_jobs)(me, them, proxy, lag)
            sink.note("Говори по-русски — собеседник слышит английский.")

            tasks = [asyncio.create_task(job) for job in jobs]
            tasks.append(asyncio.create_task(sink.run()))
            try:
                await asyncio.gather(*tasks)
            except voice_clone.CloneError as e:
                raise Fatal(str(e)) from e
            finally:
                for t in tasks:
                    t.cancel()
        finally:
            level_task.cancel()
            if stop_loopback:
                stop_loopback.set()
            mic.stop()
            mic.close()
            for p in self.players:
                p.stream.stop()
                p.stream.close()
            self.players, self.monitor, self.voice = [], None, None


def build_parser():
    ap = argparse.ArgumentParser(description="Live call translator: your voice RU->EN, their speech EN->RU subtitles")
    ap.add_argument("--lang", default="en", help="language the other person hears (default: en)")
    ap.add_argument("--their-lang", default="ru", help="language of their subtitles (default: ru)")
    ap.add_argument("--in", dest="inp", help="microphone name substring or index (default: system mic)")
    ap.add_argument("--out", default="CABLE Input", help="virtual cable playback device (default: CABLE Input)")
    ap.add_argument("--listen", help="speakers/headphones the call plays through (default: system output)")
    ap.add_argument("--no-listen", action="store_true", help="don't subtitle the other person")
    ap.add_argument("--no-me", action="store_true", help="don't translate your microphone")
    ap.add_argument("--no-diarize", dest="diarize", action="store_false",
                    help="don't tell apart several speakers on the other side (Soniox)")
    ap.add_argument("--engine", choices=("soniox", "openai"), default="soniox",
                    help="soniox: mid-sentence translation, cloned voice, keywords/context (default); "
                         "openai: gpt-realtime-translate")
    ap.add_argument("--voice", choices=("clone", "builtin", "model", "off"), default="builtin",
                    help="clone: my cloned voice; builtin: a Soniox voice (--voice-name); "
                         "model: OpenAI translator's own voice; off: text only")
    ap.add_argument("--voice-id", help="id of my cloned voice (Soniox, or Cartesia for --engine openai)")
    ap.add_argument("--voice-name", help=f"built-in Soniox voice (default: {soniox_engine.DEFAULT_VOICE})")
    ap.add_argument("--speed", type=float, default=1.0, help="speech speed for Soniox voices, 0.7-1.3")
    ap.add_argument("--context-file", help='JSON {"keywords": ["Сурен = Suren", ...], "context": "..."}')
    ap.add_argument("--voice-delay", choices=tuple(voice_clone.BUFFER_MS), default="balanced",
                    help="how long the cloned voice may wait for more text before speaking")
    ap.add_argument("--monitor", action="store_true", help="also play your translation to your headphones")
    ap.add_argument("--monitor-device", help="headphones name substring or index (default: system output)")
    ap.add_argument("--proxy", help="proxy URL, e.g. socks5h://127.0.0.1:10808, or 'none' (default: system proxy)")
    ap.add_argument("--passthrough", action="store_true", help="no translation: mic straight into the cable")
    ap.add_argument("--list", action="store_true", help="list audio devices and exit")
    return ap


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.system("")  # enable ANSI colors in the Windows console
    args = build_parser().parse_args()
    if args.context_file:
        assistant = json.loads(Path(args.context_file).read_text(encoding="utf-8"))
        args.keywords, args.context = assistant.get("keywords", []), assistant.get("context", "")
    if args.list:
        print(sd.query_devices())
        return

    sink = ConsoleSink()
    engine = Engine(args, sink)

    def toggle_mute():
        engine.set_muted(not engine.muted)
        sink.note("микрофон ВЫКЛЮЧЕН" if engine.muted else "микрофон включён")

    if start_hotkey(toggle_mute):
        sink.note(f"{HOTKEY_NAME} — выключить/включить микрофон. Ctrl+C — выход.")
    try:
        asyncio.run(engine.run())
    except Fatal as e:
        sys.exit(f"\n{e}")
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
