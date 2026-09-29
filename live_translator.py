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
Ctrl+Alt+M mutes/unmutes your microphone from any app; Ctrl+Alt+Space says "I finished": the phrase
is closed and spoken at once instead of after the pause.
"""
import argparse
import asyncio
import base64
import contextlib
import ctypes
import functools
import gc
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import warnings
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlsplit
from queue import SimpleQueue

import numpy as np
import sounddevice as sd
# Import order matters: sounddevice puts the main thread in a COM STA first, which soundcard tolerates
import soundcard as sc
from python_socks import ProxyError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

import soniox_engine
import voice_clone

URL = os.environ.get("LIVE_TRANSLATOR_URL",
                     "wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate")
RATE = 24_000  # API requires mono PCM16 at 24 kHz
BLOCK = 480    # 20 ms per chunk
SILENCE = bytes(BLOCK * 2)  # one chunk of it
CANCEL_WAIT = 1.0  # seconds a stopping engine waits for its tasks to end

APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
ENV_FILE = APP_DIR / ".env"

HOTKEY_NAME = "Ctrl+Alt+M"
HOTKEY_DONE_NAME = "Ctrl+Alt+Space"
DONE_KEY = {"vk": 0x20, "ident": 2}  # Ctrl+Alt+Space for start_hotkey
PASSTHROUGH_WARNING = ("ВНИМАНИЕ: --passthrough пускает ваш настоящий голос (по-русски) прямо в звонок. "
                       "Только для проверки кабеля — не включайте во время разговора!")

FATAL_ERRORS = {  # API error codes that reconnecting won't fix
    "invalid_api_key": "Неверный ключ OPENAI_API_KEY.",
    "insufficient_quota": "Пополни баланс: platform.openai.com/settings/organization/billing.",
    "unsupported_country_region_territory": "OpenAI блокирует твой регион: включи VPN.",
    "model_not_found": "У аккаунта API нет доступа к gpt-realtime-translate.",
}
RATE_LIMIT_DELAY = 10  # seconds before reconnecting after an HTTP 429: hammering a server that asked to slow down


class Fatal(Exception):
    pass


def load_api_key(env="OPENAI_API_KEY"):
    """The key saved in the app (.env next to it) wins over an environment variable of the same name."""
    if ENV_FILE.exists():
        for line in read_env_lines("replace"):
            name, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if name.strip() == env and value:
                return value
    return os.environ.get(env)


def read_env_lines(errors):
    """The lines of .env without a BOM (Notepad adds one); undecodable bytes are replaced or kept for a rewrite."""
    return ENV_FILE.read_text(encoding="utf-8-sig", errors=errors).splitlines()


def save_api_key(key, env="OPENAI_API_KEY"):
    if "\r" in key or "\n" in key:
        raise ValueError("В ключе есть перенос строки: вставьте только сам ключ.")
    lines = []
    if ENV_FILE.exists():
        lines = [line for line in read_env_lines("surrogateescape") if line.partition("=")[0].strip() != env]
    lines.append(f"{env}={key}")
    tmp = ENV_FILE.with_name(ENV_FILE.name + ".tmp")  # a crash while writing must not leave .env half written
    try:
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", errors="surrogateescape")
        os.replace(tmp, ENV_FILE)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.environ[env] = key


PROXY_SCHEMES = ("socks5h", "socks5", "socks4a", "socks4", "http", "https")


def detect_proxy(explicit):
    """Explicit --proxy wins ("none" disables); else the Windows system proxy (VPN clients like v2rayN)."""
    if explicit:
        explicit = explicit.strip()
        if explicit.lower() == "none":
            return None
        scheme, sep, rest = explicit.partition("://")
        if not sep:  # "127.0.0.1:10808", as VPN clients show their local SOCKS port
            return "socks5h://" + explicit
        scheme = scheme.lower()
        if scheme == "socks":
            return "socks5h://" + rest
        if scheme not in PROXY_SCHEMES or not rest:
            raise Fatal(f"Неверный адрес прокси: {redact(explicit)}. Пример: socks5h://127.0.0.1:10808")
        return f"{scheme}://{rest}"
    proxies = urllib.request.getproxies()
    url = proxies.get("socks") or proxies.get("https") or proxies.get("all")
    if not url:
        return None
    scheme, _, rest = url.partition("://")
    # Windows reports "socks=host:port" as socks:// or socks4://; VPN clients serve SOCKS5
    proxy = "socks5h://" + rest if scheme.startswith("socks") else url
    return None if _local_proxy_down(proxy) else proxy


def redact(url):
    """A proxy URL without its login and password, for the log and the UI."""
    netloc = urlsplit(url).netloc
    return url.replace(netloc, "***@" + netloc.rpartition("@")[2], 1) if "@" in netloc else url


def drain(queue):
    """Drop audio captured while there is no connection: it would only pile up in memory."""
    while not queue.empty():
        queue.get_nowait()


def _local_proxy_down(url):
    """A VPN client that is switched off leaves its Windows proxy setting behind: go direct then."""
    u = urlsplit(url)
    if u.hostname not in ("127.0.0.1", "localhost", "::1") or not u.port:
        return False
    try:
        socket.create_connection((u.hostname, u.port), timeout=0.3).close()
        return False
    except OSError:
        return True


PORTAUDIO = threading.RLock()  # held while PortAudio is re-initialised, and around every use of it outside a call


def portaudio(fn):
    """fn never runs while PortAudio is being re-initialised: a query would fail, a stream being opened could crash."""
    @functools.wraps(fn)
    def locked(*args, **kwargs):
        with PORTAUDIO:
            return fn(*args, **kwargs)
    return locked


@portaudio
def wasapi_index():
    for i, api in enumerate(sd.query_hostapis()):
        if "WASAPI" in api["name"]:
            return i
    return None


@portaudio
def query_devices():
    """sd.query_devices() for callers outside the engine (the window's device lists, the cable check)."""
    return sd.query_devices()


def refresh_devices():
    """Re-initialise PortAudio, which reads the device list only when it starts, once Windows has a device plugged in
    since or no longer has one PortAudio lists. Nothing is done while an audio stream is open (it would be closed under
    its owner); a stream being opened, or another refresh, is waited for. PortAudio is used through the functions here
    (query_devices, pick_device, open_headphones, open_input...), which never overlap a refresh. Returns whether it
    refreshed."""
    with PORTAUDIO:
        try:
            if not hasattr(sd, "_terminate") or streams_open() or not _devices_changed():
                return False
            if sd._initialized:
                sd._terminate()
            sd._initialize()
            return True
        except Exception:  # PortAudio would not start again (audio service down): the next refresh tries again
            return False


def _devices_changed():
    """Windows has an audio device PortAudio's list lacks, or no longer has one it lists; True if Windows can't say.
    (A default changed in Windows needs no refresh: pick_device asks Windows for it by name.)"""
    windows = _windows(lambda: {("input", str(d.name)) for d in sc.all_microphones()}
                       | {("output", str(d.name)) for d in sc.all_speakers()})
    wasapi = wasapi_index()
    if not windows or wasapi is None:
        return True
    listed = {(kind, d["name"]) for d in sd.query_devices() if d["hostapi"] == wasapi
              for kind in ("input", "output") if d[f"max_{kind}_channels"] > 0}
    return listed != windows


def streams_open():
    """Whether a PortAudio stream of this program is open, whoever opened it (the engine, a preview, a recording)."""
    gc.collect()  # a stream object that failed to open, or was dropped, is not in the way
    stream = getattr(sd, "_StreamBase", ())
    return any(isinstance(o, stream) and _stream_open(o) for o in gc.get_objects())


def _stream_open(stream):
    try:
        return not stream.closed
    except AttributeError:  # being opened on another thread right now
        return True


@portaudio
def pick_device(name, kind):
    """Resolve a device by index or name substring; prefer WASAPI (lowest latency). No name: the Windows default."""
    wasapi = wasapi_index()
    if name is None:
        return _default_device(kind, wasapi)
    if str(name).isdigit():
        return int(name)
    matches = _devices(kind, wasapi, lambda device: name.lower() in device.lower())
    if not matches:
        raise Fatal(f"Аудиоустройство не найдено: {name!r}. Проверь, что VB-Cable установлен "
                    "(vb-audio.com/Cable), или запусти консольную версию с --list.")
    return matches[0]


def _devices(kind, wasapi, match):
    """Indexes of the "input" / "output" devices whose name matches, WASAPI ones first."""
    channels = f"max_{kind}_channels"
    matches = [i for i, d in enumerate(sd.query_devices()) if match(d["name"]) and d[channels] > 0]
    return sorted(matches, key=lambda i: sd.query_devices(i)["hostapi"] != wasapi)


NO_DEVICE = {"input": "Windows не видит ни одного микрофона: подключите его (Параметры Windows → Система → Звук).",
             "output": "Windows не видит ни одного устройства вывода звука: подключите наушники или колонки."}


def _default_device(kind, wasapi):
    """The Windows default device now; PortAudio's own default when Windows can't say or PortAudio's list, read when
    it started, doesn't have that device yet."""
    current = windows_default(kind)
    matches = _devices(kind, wasapi, lambda device: device == current) if current else []
    if matches:
        return matches[0]
    if wasapi is not None:
        idx = sd.query_hostapis(wasapi)[f"default_{kind}_device"]
        if idx >= 0:
            return idx
    idx = sd.default.device[0 if kind == "input" else 1]
    if idx < 0:
        raise Fatal(NO_DEVICE[kind])
    return idx


@portaudio
def device_name(index):
    return sd.query_devices(index)["name"]


def _windows(ask):
    """ask() of soundcard, which needs COM on this thread (pywebview calls, the engine); None if Windows can't say."""
    try:
        ole32 = ctypes.windll.ole32
        hr = ole32.CoInitializeEx(None, 0)
        try:
            return ask()
        finally:
            if hr >= 0:  # S_OK / S_FALSE; an STA thread (RPC_E_CHANGED_MODE) is left as it was
                ole32.CoUninitialize()
    except Exception:  # no default device or no COM
        return None


def windows_default(kind):
    """Name of the Windows default "input" (microphone) or "output" (playback) device now; None if Windows can't say.

    Asked on every call: PortAudio keeps the defaults it saw when it was last initialised."""
    return _windows(lambda: str((sc.default_microphone() if kind == "input" else sc.default_speaker()).name))


def default_name(kind):
    """Name of the Windows default device now; PortAudio's view when Windows can't say; None if unknown."""
    name = windows_default(kind)
    if name is not None:
        return name
    try:
        return device_name(pick_device(None, kind))
    except Exception:  # no audio devices, PortAudio errors: the caller shows "unknown"
        return None


def is_cable(name):
    return "CABLE" in (name or "")


def device_problems(mic, out, monitor):
    """Stealth check by device names: whatever would let the call hear something besides my English.

    mic: the microphone to translate; out: the Windows default output; monitor: where I hear
    myself (None when off). Returns {"mic" | "out" | "monitor": message}."""
    problems = {}
    if is_cable(mic):
        problems["mic"] = (f"Выберите настоящий микрофон: сейчас программа слушает «{mic}» — это виртуальный "
                           "кабель, и перевод пошёл бы по кругу. Источник звука → «Звук микрофона».")
    if is_cable(out):
        problems["out"] = (f"Звук Windows по умолчанию идёт в «{out}»: собеседник услышит системные звуки. "
                           "Выберите наушники: Параметры Windows → Система → Звук → Вывод.")
    if is_cable(monitor):
        problems["monitor"] = (f"«Слышать себя» пропущено: «{monitor}» — виртуальный кабель, "
                               "перевод прозвучал бы в звонке дважды.")
    return problems


@portaudio
def stream_kwargs(device, blocksize=BLOCK):
    extra = None
    if sd.query_devices(device)["hostapi"] == wasapi_index():
        extra = sd.WasapiSettings(auto_convert=True)  # let Windows resample to/from 24 kHz
    return dict(device=device, samplerate=RATE, channels=1, dtype="int16",
                blocksize=blocksize, latency="low", extra_settings=extra)


class Player:
    """Thread-safe PCM16 FIFO drained by an output stream callback."""

    HANG = 0.3  # seconds a player still counts as busy after its last sound

    def __init__(self, device):
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._last_sound = 0.0
        self.gain = 1.0
        with PORTAUDIO:  # never while PortAudio is being re-initialised (a preview opens it off the engine's thread)
            # blocksize 0: the device's own buffer size, no extra 20 ms block between speech and the call
            self.stream = sd.RawOutputStream(callback=self._callback, **stream_kwargs(device, blocksize=0))

    def _callback(self, outdata, frames, time_info, status):
        n = len(outdata)
        with self._lock:
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
        if chunk:
            self._last_sound = time.monotonic()
        outdata[:len(chunk)] = chunk
        outdata[len(chunk):] = b"\x00" * (n - len(chunk))

    def _alive(self):
        """False once the output device is gone (PortAudio stopped calling back): nothing is queued for it any more."""
        try:
            alive = getattr(self.stream, "active", True)
        except Exception:  # a closed stream raises
            alive = False
        if not alive:
            self.clear()
        return alive

    def feed(self, pcm):
        if not self._alive():
            return
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
        return self._alive() and (bool(self._buf) or time.monotonic() - self._last_sound < self.HANG)

    @property
    def buffered(self):
        """Seconds of audio queued and not yet played."""
        return len(self._buf) / 2 / RATE if self._alive() else 0.0


def open_player(device):
    """A started Player; its stream is closed again when it can't start."""
    player = Player(device)
    try:
        player.stream.start()
    except BaseException:
        player.stream.close()
        raise
    return player


HEADPHONES_ONLY = ("Прослушивание звучит только в наушниках, а выбран «{}». "
                   "Источник звука → «Звук компьютера» → выберите наушники.")


@portaudio
def open_headphones(name):
    """A started Player on my headphones `name` (the Windows default output when None), picked, checked and opened with
    no refresh in between to renumber the devices. Never the cable: the call must not hear it (a voice preview)."""
    device = pick_device(name, "output")
    label = device_name(device)
    if is_cable(label):
        raise Fatal(HEADPHONES_ONLY.format(label))
    return open_player(device)


@portaudio
def native_rate(name):
    """The rate Windows records the microphone `name` at (48000 mostly): a voice sample keeps that quality."""
    return int(sd.query_devices(pick_device(name, "input"))["default_samplerate"])


@portaudio
def open_input(name, callback, samplerate=None):
    """An input stream, not started yet, on the microphone `name` (the Windows default when None), picked, checked and
    opened with no refresh in between to renumber the devices. Never the cable: it carries our English, not my voice.
    `samplerate` None is the engine's own rate."""
    device = pick_device(name, "input")
    problem = device_problems(device_name(device), None, None).get("mic")
    if problem:
        raise Fatal(problem)
    kwargs = stream_kwargs(device)
    if samplerate:
        kwargs["samplerate"] = samplerate
    return sd.RawInputStream(callback=callback, **kwargs)


DEVICE_ERRORS = {
    "input": "Микрофон «{}» недоступен: разрешите приложениям доступ к микрофону (Параметры Windows → "
             "Конфиденциальность и защита → Микрофон) или закройте программу, которая его заняла.",
    "output": "Не удалось открыть «{}»: проверьте, что устройство включено (Параметры Windows → Система → Звук), "
              "и закройте программу, которая его заняла.",
}


@contextlib.contextmanager
def device_errors(kind, name, sink):
    """PortAudio's English error opening or starting a device becomes a Russian Fatal that says what to do."""
    try:
        yield
    except sd.PortAudioError as e:
        sink.note(f"[{name}] {e}")
        raise Fatal(DEVICE_ERRORS[kind].format(name)) from e


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
        self.finalizer = None  # set by the STT channel: .force() closes the phrase I'm saying right now
        self.src_label, self.dst_label = name, f"{name} → {lang.upper()}"


async def pump_audio(ws, queue):
    while True:
        pcm = await queue.get()
        await ws.send(json.dumps({
            "type": "session.input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode(),
        }))


DELTA_EVENTS = ("session.output_audio.delta", "session.input_transcript.delta", "session.output_transcript.delta")


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
        drain(ch.queue)  # audio captured while (re)connecting is stale
        sender = asyncio.create_task(pump_audio(ws, ch.queue))
        try:
            async for raw in ws:
                try:
                    event = json.loads(raw)
                except ValueError:  # a frame that is not JSON is skipped, not worth ending the call
                    continue
                if not isinstance(event, dict):
                    continue
                kind, delta = event.get("type"), event.get("delta")
                if kind in DELTA_EVENTS and not isinstance(delta, str):
                    continue
                if kind == "session.output_audio.delta":
                    if ch.voice or not ch.players or (ch.gate_out and ch.gate_out()):
                        continue
                    try:
                        pcm = base64.b64decode(delta)
                    except ValueError:
                        continue
                    lag = ch.lag.on_output() if ch.lag else None
                    if lag is not None:
                        sink.lag(lag)
                    for p in ch.players:
                        p.feed(pcm)
                elif kind == "session.input_transcript.delta":
                    sink.caption(f"{ch.kind}_src", ch.src_label, delta)
                elif kind == "session.output_transcript.delta":
                    sink.caption(f"{ch.kind}_dst", ch.dst_label, delta)
                    text = soniox_engine.speakable(delta)  # an untranslated Russian word stays unspoken
                    if ch.voice and not (ch.gate_out and ch.gate_out()):
                        if text:
                            await ch.voice.say(text)
                        elif delta.rstrip().endswith(voice_clone.SENTENCE_END):
                            await ch.voice.end_phrase()  # the dropped word ended the sentence
                elif kind == "session.updated":
                    sink.status(ch.dst_label, "подключено", True)
                elif kind == "error":
                    err = event.get("error")
                    err = err if isinstance(err, dict) else {}
                    code = err.get("code")
                    if isinstance(code, str) and code in FATAL_ERRORS:
                        raise Fatal(f"{err.get('message')}\n{FATAL_ERRORS[code]}")
                    sink.note(f"[API error] {err or event}")
        finally:
            sender.cancel()


async def run_channel(ch, key, proxy, sink):
    while True:
        pause = 2
        try:
            await run_session(ch, key, proxy, sink)
        except InvalidStatus as e:
            code = e.response.status_code
            if code in (401, 403):
                raise Fatal(f"API отклонил запрос (HTTP {code}): неверный ключ, нет оплаты "
                            "или регион заблокирован — включи VPN.")
            if code == 429 and b"insufficient_quota" in (e.response.body or b""):
                raise Fatal(f"API отклонил запрос (HTTP 429): нет средств. {FATAL_ERRORS['insufficient_quota']}")
            if code == 429:
                pause = RATE_LIMIT_DELAY
            sink.status(ch.dst_label, f"HTTP {code}, переподключение…", False)
        except (ConnectionClosed, OSError, ProxyError, InvalidHandshake) as e:
            sink.status(ch.dst_label, "нет связи, переподключение… (VPN включён?)", False)
            sink.note(f"[{ch.dst_label}] {e}")
        await asyncio.sleep(pause)
        drain(ch.queue)


FOLLOW = 1.0         # seconds between checks that the Windows default output is still the device being captured...
FOLLOW_SILENT = 2.0  # ...which is left for the new default only once it gave nothing but exact zeros this long


def start_loopback(name, loop, queue, gate, on_rms=None, on_status=None):
    """Capture what plays in the headphones (the other person) on a background thread.

    A device that goes away (headset unplugged, format changed) is reopened, but never the cable Windows may
    fall back to (that would subtitle my own English); on_status(text, ok) tells the UI meanwhile. Without a name
    it follows the Windows default output to another real device once the call app plays there, not here.
    Returns (device name, stop event)."""
    started = SimpleQueue()
    stop = threading.Event()

    def open_device():
        speaker = sc.default_speaker() if name is None else sc.get_speaker(name)
        problem = device_problems(None, str(speaker.name), None).get("out") if name is None else None
        if problem:
            raise Fatal(problem)
        source = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        recorder = source.recorder(samplerate=RATE, channels=1, blocksize=BLOCK)
        return speaker, recorder, recorder.__enter__()

    def report(text, ok):
        if on_status:
            try:
                loop.call_soon_threadsafe(on_status, text, ok)
            except RuntimeError:  # event loop closed
                pass

    def moved(speaker):
        """The Windows default output is another real device now (a headset connected)."""
        try:
            now = str(sc.default_speaker().name)
        except Exception:  # no answer this time
            return False
        return now != str(speaker.name) and not is_cable(now)

    def capture(speaker, rec):
        """Record into the queue; True when the call's sound moved to the new default output, False once stopped.

        Moved means the device captured gave only exact zeros for FOLLOW_SILENT s, as an endpoint nobody plays to
        does: a call app set to this device keeps it (a call's audio is rarely exact zeros, even in a pause)."""
        checked = sounded = time.monotonic()
        while not stop.is_set():
            data = rec.record(numframes=BLOCK)[:, 0]
            now = time.monotonic()
            if data.any():
                sounded = now
            if gate():  # don't subtitle our own translation playing in the headphones
                data = np.zeros_like(data)
            if on_rms:
                on_rms(float(np.sqrt(np.mean(data ** 2))) * 32767)
            pcm = (np.clip(data, -1, 1) * 32767).astype("<i2").tobytes()
            try:
                loop.call_soon_threadsafe(queue.put_nowait, pcm)
            except RuntimeError:  # event loop closed
                return False
            if name is None and now - checked >= FOLLOW:
                checked = now
                if now - sounded >= FOLLOW_SILENT and moved(speaker):
                    return True
        return False

    def idle(seconds):
        """Wait, the channel hearing silence in real time meanwhile: Soniox ends the phrase it was hearing and keeps
        the stream, which it drops after 20 s without audio. False once stopped."""
        start, sent = time.monotonic(), 0
        while not stop.wait(BLOCK / RATE):
            elapsed = time.monotonic() - start
            due = int(elapsed * RATE / BLOCK)
            try:
                for _ in range(due - sent):
                    loop.call_soon_threadsafe(queue.put_nowait, SILENCE)
            except RuntimeError:  # event loop closed
                return False
            sent = due
            if elapsed >= seconds:
                return True
        return False

    def reopen():
        """The device again, or the new default output, once it opens; None once stopped."""
        shown = None
        while idle(1):
            try:
                return open_device()
            except Fatal as e:  # the default output is the cable now
                if str(e) != shown:
                    shown = str(e)
                    report(shown, False)
            except Exception:
                pass
        return None

    def worker():
        # The main thread is a COM STA (PortAudio), so this thread joins the MTA itself
        ctypes.windll.ole32.CoInitializeEx(None, 0)
        warnings.filterwarnings("ignore", category=getattr(sc, "SoundcardRuntimeWarning", RuntimeWarning))
        try:
            speaker, recorder, rec = open_device()
        except Exception as e:
            started.put(e)
            return
        started.put(speaker.name)
        while True:
            try:
                if not capture(speaker, rec):
                    return  # stopped, or the engine's event loop is gone
                lost = None  # the call's sound moved to the new default output
            except Exception as e:  # the device went away
                lost = e
            finally:
                try:
                    recorder.__exit__(None, None, None)
                except Exception:
                    pass
            opened = None
            if lost is None:
                try:
                    opened = open_device()
                except Exception as e:  # the new default won't open yet: waited for like a lost device
                    lost = e
            if lost is not None:
                if on_rms:
                    on_rms(0.0)
                report(f"звук компьютера пропал ({lost}), жду устройство…", False)
                opened = reopen()
                if opened is None:
                    return
            speaker, recorder, rec = opened
            report(f"{'теперь' if lost is None else 'снова'} слышу: {speaker.name}", True)

    threading.Thread(target=worker, daemon=True).start()
    result = started.get()
    if isinstance(result, Exception):
        raise Fatal(f"Не удалось слушать собеседника ({result}). Запусти с --no-listen или укажи --listen.")
    return result, stop


def start_hotkey(callback, vk=0x4D, ident=1):
    """Call `callback` on Ctrl+Alt+<vk> (default M) from any app; each hotkey needs its own `ident`.

    Returns False if another program owns the hotkey."""
    registered = SimpleQueue()

    def worker():
        user32 = ctypes.windll.user32
        mod_alt, mod_control, mod_norepeat, wm_hotkey = 0x1, 0x2, 0x4000, 0x312
        ok = user32.RegisterHotKey(None, ident, mod_control | mod_alt | mod_norepeat, vk)
        registered.put(bool(ok))
        if not ok:
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == wm_hotkey:
                callback()

    threading.Thread(target=worker, daemon=True).start()
    return registered.get()


PROVIDER_NAMES = {"soniox": "Soniox", "cartesia": "Cartesia", "inworld": "Inworld"}
DELIVERIES = getattr(soniox_engine, "DELIVERIES", ("fast", "balanced", "natural"))  # how the voice paces its speech


def use_soniox_region(region):
    """Soniox's "us" (or "" / None) and "eu" hosts from now on; nothing until the engine module has regions."""
    switch = getattr(soniox_engine, "use_region", None)
    if switch:
        switch(region or "")


def voice_provider(args):
    """Who synthesizes my voice in the Soniox engine: "soniox" (default), "cartesia" or "inworld"."""
    provider = getattr(args, "voice_provider", None)
    return provider if provider in PROVIDER_NAMES else "soniox"


def voice_class(provider):
    """(voice class, key env var, default model, default built-in voice) of a voice provider.

    Cartesia and Inworld are imported only when picked: they are optional alternatives to Soniox TTS."""
    if provider == "cartesia":
        import cartesia_engine
        return cartesia_engine.CartesiaVoice, voice_clone.KEY_ENV, voice_clone.TTS_MODEL, None
    if provider == "inworld":
        import inworld_engine
        return (inworld_engine.InworldVoice, inworld_engine.KEY_ENV, inworld_engine.DEFAULT_MODEL,
                inworld_engine.DEFAULT_VOICE)
    return soniox_engine.SonioxVoice, soniox_engine.KEY_ENV, soniox_engine.TTS_MODEL, soniox_engine.DEFAULT_VOICE


async def finish(tasks, timeout=CANCEL_WAIT):
    """Cancel the tasks and wait (a little) until each has really ended: a task only asked to cancel and left
    behind a closed loop is destroyed while pending."""
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)


def close_loop(loop):
    """End an engine loop the way asyncio.run does: every task still alive is cancelled and awaited, then it closes."""
    try:
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=CANCEL_WAIT))
        loop.run_until_complete(loop.shutdown_asyncgens())  # no shutdown_default_executor: a device query may hang it
    finally:
        loop.close()


class Engine:
    """Opens the audio devices and runs both translation channels until cancelled."""

    WATCH = 2.0       # seconds between checks of the Windows default output during the call
    MIC_SILENT = 1.0  # no audio from the microphone this long: it is gone (unplugged, Bluetooth dropped)...
    MIC_START = 3.0   # ...but one just started may take this long to deliver its first audio
    OUT_LABEL, MIC_LABEL = "Вывод звука", "Микрофон"

    def __init__(self, args, sink):
        self.args, self.sink = args, sink
        self.muted = False
        self.paused = False    # both directions stopped (from the floating subtitles)
        self.voice_out = True  # speak my translation into the call
        self.out_problem = None  # the Windows default output became the cable mid-call: my voice waits
        self.volume = 1.0
        self.players = []
        self.monitor = None
        self.mic_rms = self.them_rms = 0.0
        self.mic = None
        self.mic_device = None  # the microphone the call started with (checked: not the cable)
        self.mic_started = self.mic_seen = 0.0  # when the microphone was started / last delivered audio
        self.mic_lock = threading.Lock()        # the microphone is reopened on a worker thread
        self.mic_q = None  # my channel's audio
        self.voice = None  # CloneVoice in "my voice" mode
        self.me_channel = None
        self.loop = None
        self._library_voice = None  # Cartesia's default voice, looked up by _find_default_voice ("" when there is none)

    def finish_turn(self):
        """Ctrl+Alt+Space, "I finished": close my phrase now instead of waiting for the pause."""
        finalizer = self.me_channel and self.me_channel.finalizer
        if finalizer and self.loop:
            try:
                self.loop.call_soon_threadsafe(finalizer.force)
            except RuntimeError:  # the engine just stopped
                pass

    def _backlog(self):
        """Seconds of my translated speech queued for the call and not heard yet."""
        return self.players[0].buffered if self.players else 0.0

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
            device = pick_device(self.args.monitor_device, "output")
            problem = device_problems(None, None, device_name(device)).get("monitor")
            if problem:
                self.sink.note(problem)
                return
            monitor = self._open_monitor(device)
            if monitor is not None:
                self.monitor = monitor
                self.players.append(monitor)
        elif not on and self.monitor is not None:
            monitor, self.monitor = self.monitor, None
            self.players.remove(monitor)
            monitor.stream.stop()
            monitor.stream.close()

    def _open_monitor(self, device):
        """My translation in the headphones too; one that can't be opened is only noted (the call goes on)."""
        try:
            with device_errors("output", device_name(device), self.sink):
                monitor = open_player(device)
        except Fatal as e:
            self.sink.note(f"«Слышать себя» пропущено. {e}")
            return None
        monitor.gain = self.volume
        return monitor

    async def report_level(self):
        while True:
            await asyncio.sleep(0.1)
            self.sink.level(min(1.0, self.mic_rms / 6000), min(1.0, self.them_rms / 6000))

    async def watch_output(self):
        """Windows can make the cable its default output mid-call (a headset dropped): then the call hears system
        sounds. Asked off the event loop, since Windows may take a while to answer."""
        while True:
            await asyncio.sleep(self.WATCH)
            name = await asyncio.to_thread(windows_default, "output")
            if name is not None:  # no answer this time (an endpoint changing): nothing is known to have changed
                self._on_default_output(name)

    def _on_default_output(self, name):
        """Red status and my voice paused while the default output is the cable; both undone once it is fixed."""
        problem = device_problems(None, name, None).get("out")
        if problem == self.out_problem:
            return
        self.out_problem = problem
        if problem:
            self._cut_speech()
            self.sink.status(self.OUT_LABEL, f"{problem} Мой голос на паузе, пока это не исправлено.", False)
        else:
            self.sink.status(self.OUT_LABEL, "исправлено — мой голос снова звучит", True)

    async def watch_mic(self, open_mic):
        """A microphone that stops delivering audio is reported, its meter drops to zero, and it is reopened as soon
        as it can be (off the event loop: opening a Bluetooth headset can take seconds)."""
        while True:
            await asyncio.sleep(self.MIC_SILENT / 4)
            if self._mic_alive():
                continue
            self.mic_rms = 0.0
            self.sink.status(self.MIC_LABEL, "пропал — жду устройство…", False)
            feeding = asyncio.create_task(self._feed_silence())
            try:
                name = await self._mic_back(open_mic)
            finally:
                await finish([feeding])
            self.sink.status(self.MIC_LABEL, f"снова слышу: {name}", True)

    async def _mic_back(self, open_mic):
        """Reopen the lost microphone until it delivers audio; its name. The cable, which Windows may make its default
        microphone meanwhile, is refused and shown."""
        shown = None
        while True:
            try:
                name = await asyncio.to_thread(self._reopen_mic, open_mic)
            except Fatal as e:
                name = None
                if str(e) != shown:
                    shown = str(e)
                    self.sink.status(self.MIC_LABEL, shown, False)
            if name and await self._mic_heard():
                return name
            await asyncio.sleep(self.MIC_SILENT)

    async def _feed_silence(self):
        """(The microphone is gone) my channel hears silence in real time, as when muted: Soniox speaks the phrase I was
        saying at once, and keeps the stream, which it drops after 20 s without audio."""
        finalizer = self.me_channel and self.me_channel.finalizer
        if finalizer:
            finalizer.force()
        start, sent = time.monotonic(), 0
        while self.me_channel:
            await asyncio.sleep(BLOCK / RATE)
            due = int((time.monotonic() - start) * RATE / BLOCK)
            if time.monotonic() - self.mic_seen > 2 * BLOCK / RATE:  # not while a reopened one delivers
                for _ in range(due - sent):
                    self.mic_q.put_nowait(SILENCE)
            sent = due

    def _mic_alive(self):
        """Delivering audio, or started less than MIC_START s ago and not heard from yet."""
        since, limit = (self.mic_seen, self.MIC_SILENT) if self.mic_seen else (self.mic_started, self.MIC_START)
        return self.mic.active and time.monotonic() - since < limit

    async def _mic_heard(self):
        """The microphone just started delivers audio (a slow one is not reopened again before MIC_START s)."""
        while not self.mic_seen and self._mic_alive():
            await asyncio.sleep(0.05)
        return self._mic_alive()

    def _start_mic(self):
        self.mic_seen = 0.0
        self.mic.start()
        self.mic_started = time.monotonic()

    def _reopen_mic(self, open_mic):
        """(Worker thread) The lost microphone is closed and opened anew: the one the call started with, else the one
        chosen now (by name, or the Windows default). Its name, None while none is back; Fatal if Windows made the cable
        its default microphone (my channel would hear our own English)."""
        with self.mic_lock:
            if self.mic is None:
                return None  # the engine stopped meanwhile
            self.mic.close()
            for device in self._mic_devices():
                if self._restart_mic(open_mic, device):
                    return device_name(device)
            return None

    def _mic_devices(self):
        """Where the lost microphone may be back, in order: the one the call started with, then the one chosen now if
        another. Never the cable (Fatal)."""
        if self.mic_device is not None:
            yield self.mic_device
        try:
            device = pick_device(self.args.inp, "input")
            name = device_name(device)
        except Exception:  # not back yet (a name), no microphone at all (the default)
            return
        if device == self.mic_device:
            return
        if is_cable(name):
            raise Fatal(f"Windows переключил микрофон на «{name}» — подключите настоящий микрофон.")
        yield device

    def _restart_mic(self, open_mic, device):
        """(Under mic_lock) `device` becomes my microphone, started; False if Windows won't open it (not back yet)."""
        try:
            mic = open_mic(device)
        except Exception:
            return False
        self.mic = mic
        try:
            self._start_mic()
            return True
        except Exception:
            mic.close()
            return False

    def _set_them_rms(self, rms):
        self.them_rms = rms

    def _silenced(self):
        return self.muted or self.paused or not self.voice_out or bool(self.out_problem)

    def _play(self, pcm):
        """Synthesized speech (cloned or built-in voice) into the call."""
        if not self._silenced():
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
            label = "выключен"
            if args.voice != "off":
                self.voice = self._make_voice(key, proxy, lag)
                me.voice = self.voice
                jobs.append(self.voice.run())
                provider = voice_provider(args)
                label = "мой клон" if args.voice == "clone" else (
                    "встроенный" if provider == "cartesia" else self.voice.voice)  # Cartesia: an id, not a name
                if provider != "soniox":
                    label += f" ({PROVIDER_NAMES[provider]})"
            self.sink.note(f"Движок: Soniox · голос: {label}")
            me.finalizer = soniox_engine.AutoFinalize(  # the hotkey works even with auto finalize off
                getattr(args, "auto_finalize", True), self.voice.queued_seconds if self.voice else self._backlog)
            jobs.append(soniox_engine.run_stt_channel(
                me, key, proxy, self.sink, args.lang, [args.their_lang],
                soniox_engine.build_context(keywords, context), self.voice))
        if them:
            jobs.append(soniox_engine.run_stt_channel(
                them, key, proxy, self.sink, args.their_lang, [args.lang],
                soniox_engine.build_context(keywords, context, reverse=True),
                diarize=getattr(args, "diarize", True)))
        return jobs

    async def _find_default_voice(self, proxy):
        """Cartesia with no voice picked speaks the library's default: a blocking request, so off the event loop."""
        args = self.args
        self._library_voice = None
        if voice_provider(args) != "cartesia" or args.voice in ("off", "clone") or args.voice_name:
            return
        key = load_api_key(voice_clone.KEY_ENV)
        if not (key and load_api_key(soniox_engine.KEY_ENV)):
            return  # _soniox_jobs names the missing key
        import cartesia_engine
        try:
            self._library_voice = await asyncio.to_thread(cartesia_engine.default_voice, key, proxy) or ""
        except voice_clone.CloneError as e:
            raise Fatal(str(e)) from e

    def _make_voice(self, soniox_key, proxy, lag):
        """My voice in the Soniox engine, synthesized by Soniox, Cartesia or Inworld (args.voice_provider)."""
        args = self.args
        provider = voice_provider(args)
        cls, key_env, model, default_voice = voice_class(provider)
        name = PROVIDER_NAMES[provider]
        key = soniox_key if provider == "soniox" else load_api_key(key_env)
        if not key:
            raise Fatal(f"Нужен ключ {name}: ⚙ Настройки → Расширенные → Ключи. Или выберите голос Soniox (🔊).")
        if args.voice == "clone" and not args.voice_id:
            raise Fatal(f"Клон голоса для {name} ещё не создан: 🔊 → «Записать мой голос».")
        voice = args.voice_id if args.voice == "clone" else (args.voice_name or default_voice)
        if not voice and provider == "cartesia":
            voice = self._library_voice
            if voice is None:
                import cartesia_engine
                try:
                    voice = cartesia_engine.default_voice(key, proxy)
                except voice_clone.CloneError as e:
                    raise Fatal(str(e)) from e
        if not voice:
            raise Fatal(f"Выберите голос {name} в меню 🔊 или запишите свой.")
        options = {"delivery": getattr(args, "delivery", "balanced"), "match_rate": getattr(args, "match_rate", True)}
        if provider != "soniox":
            options["model"] = getattr(args, f"{provider}_model", None) or model
        model = options.get("model", model)
        return cls(key, voice, args.lang, self._play, proxy, self.sink, self._first_audio(lag),
                   speed=args.speed, backlog=self._backlog, speed_boost=getattr(args, "speed_boost", True),
                   trim=getattr(args, "trim_silence", True), phrases=self._phrases(provider, model, voice),
                   **options)

    def _phrases(self, provider, model, voice):
        """Ready-made short English answers ("Sure.", "Thank you.") in exactly this voice, cached on disk."""
        args = self.args
        if not getattr(args, "instant_phrases", True) or args.lang != "en":
            return None
        import phrases
        return phrases.PhraseCache(APP_DIR / "phrases", f"{provider}|{model}|{voice}|{args.lang}|{args.speed}")

    def _open_devices(self, open_mic):
        """The cable, my headphones (monitor) and the microphone as Windows has them now: picked, checked, opened."""
        args, sink = self.args, self.sink
        refresh_devices()  # a headset plugged in after the program started, a default changed in Windows
        out_dev = pick_device(args.out, "output")
        want_mic = args.passthrough or not args.no_me  # listen-only leaves the microphone alone, even a missing one
        in_dev = pick_device(args.inp, "input") if want_mic else None
        monitor_dev = pick_device(args.monitor_device, "output") if args.monitor else None
        in_name, out_name = device_name(in_dev) if want_mic else None, device_name(out_dev)
        problems = device_problems(in_name, default_name("output"),
                                   None if monitor_dev is None else device_name(monitor_dev))
        if "mic" in problems:
            raise Fatal(problems["mic"])
        if "out" in problems:
            raise Fatal(problems["out"])
        if "monitor" in problems:
            sink.note(problems["monitor"])
            monitor_dev = None
        with device_errors("output", out_name, sink):  # what opened before a failure is closed by run()
            self.players = [open_player(out_dev)]
        self.players[0].gain = self.volume
        monitor = None if monitor_dev is None else self._open_monitor(monitor_dev)
        if monitor is not None:
            self.monitor = monitor
            self.players.append(monitor)
        if want_mic:
            with device_errors("input", in_name, sink):
                self.mic = open_mic(in_dev)
                self._start_mic()
            self.mic_device = in_dev
            sink.note(f"Микрофон: {in_name}")
        sink.note(f"Для звонка: {out_name}")

    async def run(self):
        args, sink = self.args, self.sink
        if args.no_me and args.no_listen:
            raise Fatal("Выбери хотя бы один источник звука: микрофон или звук компьютера.")
        loop = self.loop = asyncio.get_running_loop()
        mic_q = self.mic_q = asyncio.Queue()
        lag = LagMeter()

        def on_mic(indata, frames, time_info, status):
            self.mic_seen = time.monotonic()
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

        def open_mic(device):
            return sd.RawInputStream(callback=on_mic, **stream_kwargs(device))

        stop_loopback, watchers, tasks = None, [], []
        try:
            with PORTAUDIO:  # nobody re-initialises PortAudio while the devices are picked and opened
                self._open_devices(open_mic)
            watching = [self.report_level(), self.watch_output()]
            if args.passthrough or not args.no_me:
                watching.append(self.watch_mic(open_mic))
            watchers = [asyncio.create_task(w) for w in watching]
            if args.passthrough:
                sink.status("Проверка", "ваш голос без перевода идёт в кабель: звонок слышит русский", False)
                await asyncio.Event().wait()

            proxy = detect_proxy(args.proxy)
            sink.note(f"Прокси: {redact(proxy) if proxy else 'нет'}")

            me = them = None
            if not args.no_me:
                me = self.me_channel = Channel("Я", args.lang, mic_q, self.players, "me", lag,
                                               gate_out=self._silenced)
            if not args.no_listen:
                their_q = asyncio.Queue()
                them = Channel("Он", args.their_lang, their_q, [], "them")
                heard, stop_loopback = start_loopback(
                    args.listen, loop, their_q,
                    lambda: self.paused or (self.monitor is not None and self.monitor.busy),
                    self._set_them_rms, lambda text, ok: sink.status(them.dst_label, text, ok))
                sink.note(f"Собеседник: {heard}")
            if args.engine == "soniox" and args.voice == "model":
                args.voice = "builtin"  # the translator's own voice exists only in the OpenAI engine
            if args.engine == "soniox" and me:
                await self._find_default_voice(proxy)
            jobs = (self._soniox_jobs if args.engine == "soniox" else self._openai_jobs)(me, them, proxy, lag)
            sink.note("Говори по-русски — собеседник слышит английский.")

            tasks = [asyncio.create_task(job) for job in jobs]
            tasks.append(asyncio.create_task(sink.run()))
            try:
                await asyncio.gather(*tasks)
            except voice_clone.CloneError as e:
                raise Fatal(str(e)) from e
        finally:
            for t in tasks + watchers:
                t.cancel()
            if stop_loopback:
                stop_loopback.set()
            with self.mic_lock:  # after a reopen that is still under way, so no stream is left open
                if self.mic is not None:
                    self.mic.close()  # stops it too
                    self.mic = None
            for p in self.players:
                p.stream.stop()
                p.stream.close()
            self.players, self.monitor, self.voice, self.me_channel = [], None, None, None
            await finish(tasks + watchers)  # last: the devices are closed even if a second cancel cuts this short


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
    ap.add_argument("--voice-id", help="id of my cloned voice at the voice provider (Cartesia for --engine openai)")
    ap.add_argument("--voice-name", help=f"built-in voice: name, or id for Cartesia "
                                         f"(default: {soniox_engine.DEFAULT_VOICE}; Clive for Inworld)")
    ap.add_argument("--voice-provider", choices=tuple(PROVIDER_NAMES), default="soniox",
                    help="who speaks in the Soniox engine: Soniox TTS (default), Cartesia (CARTESIA_API_KEY) "
                         "or Inworld (INWORLD_API_KEY)")
    ap.add_argument("--inworld-model", help="Inworld TTS model (default: inworld-tts-2-flash)")
    ap.add_argument("--speed", type=float, default=1.0, help="speech speed of the voice in the Soniox engine, 0.7-1.3")
    ap.add_argument("--delivery", choices=DELIVERIES, default="balanced",
                    help="fast: English by clause, quickest; balanced: whole sentences, speeds up only when behind "
                         "(default); natural: most lively, never speeds up or trims pauses")
    ap.add_argument("--no-match-rate", dest="match_rate", action="store_false",
                    help="don't copy my speaking pace and loudness onto the voice")
    ap.add_argument("--region", choices=("us", "eu"),
                    help="Soniox region: eu is nearer to Europe but needs a Soniox project made in the EU "
                         "(console.soniox.com) and its key (default: us)")
    ap.add_argument("--no-speed-boost", dest="speed_boost", action="store_false",
                    help="don't speak faster for a while when the voice falls behind")
    ap.add_argument("--no-trim", dest="trim_silence", action="store_false",
                    help="keep the silence the TTS adds around every clause")
    ap.add_argument("--no-instant-phrases", dest="instant_phrases", action="store_false",
                    help="don't play ready-made short answers (Yes. / Sure. / Thank you.)")
    ap.add_argument("--no-auto-finalize", dest="auto_finalize", action="store_false",
                    help="close a phrase only by Soniox's own pause detection")
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
    use_soniox_region(args.region)

    if args.passthrough:
        print(f"\033[91;1m{PASSTHROUGH_WARNING}\033[0m", flush=True)
    sink = ConsoleSink()
    engine = Engine(args, sink)

    def toggle_mute():
        engine.set_muted(not engine.muted)
        sink.note("микрофон ВЫКЛЮЧЕН" if engine.muted else "микрофон включён")

    if start_hotkey(toggle_mute):
        sink.note(f"{HOTKEY_NAME} — выключить/включить микрофон. Ctrl+C — выход.")
    if start_hotkey(engine.finish_turn, **DONE_KEY):
        sink.note(f"{HOTKEY_DONE_NAME} — «я закончил»: перевод фразы звучит сразу, без паузы.")
    try:
        asyncio.run(engine.run())
    except Fatal as e:
        sys.exit(f"\n{e}")
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
