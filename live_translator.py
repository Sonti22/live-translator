"""
Live call translator engine + console mode (Zoom, Telegram, WhatsApp, Discord, Meet, Teams).

  You:  microphone -> gpt-realtime-translate -> English voice -> VB-Cable -> the call hears English
  Them: what plays in your headphones -> gpt-realtime-translate -> Russian subtitles

In the call app pick microphone "CABLE Output (VB-Audio Virtual Cable)".
The window version is gui.py; this file runs in the console:
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


def load_api_key():
    key = os.environ.get("OPENAI_API_KEY")
    if not key and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "OPENAI_API_KEY":
                key = value.strip().strip('"').strip("'")
    return key


def save_api_key(key):
    lines = []
    if ENV_FILE.exists():
        lines = [line for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
                 if line.partition("=")[0].strip() != "OPENAI_API_KEY"]
    lines.append(f"OPENAI_API_KEY={key}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["OPENAI_API_KEY"] = key


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

    def on_input(self, pcm):
        now = time.monotonic()
        rms = np.sqrt(np.mean(np.frombuffer(pcm, "<i2").astype(np.float32) ** 2))
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

    def caption(self, kind, label, text): pass
    def note(self, text): pass
    def status(self, label, text, ok): pass
    def lag(self, seconds): pass
    async def run(self): pass


class ConsoleSink(Sink):
    """Prints whole phrases: collects streaming deltas until punctuation or a pause."""

    IDLE = 0.8
    STYLES = {"me_src": "\033[90m", "me_dst": "\033[96m", "them_src": "\033[90m", "them_dst": "\033[93;1m"}
    DIM, RESET = "\033[90m", "\033[0m"

    def __init__(self):
        self.buf = {}  # label -> [text, kind, last_update]

    def caption(self, kind, label, text):
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

    def __init__(self, name, lang, queue, players, kind, lag=None, gate_out=None):
        self.name, self.lang, self.queue, self.players = name, lang, queue, players
        self.kind, self.lag, self.gate_out = kind, lag, gate_out
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
                    if not ch.players or (ch.gate_out and ch.gate_out()):
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


def start_loopback(name, loop, queue, gate):
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
                if gate is not None and gate.busy:  # don't subtitle our own translation
                    data = np.zeros_like(data)
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
        self.players = []

    def set_muted(self, muted):
        self.muted = muted
        if muted:  # cut off translation that is still playing
            for p in self.players:
                p.clear()

    async def run(self):
        args, sink = self.args, self.sink
        loop = asyncio.get_running_loop()
        mic_q = asyncio.Queue()
        lag = LagMeter()

        out_dev = pick_device(args.out, "output")
        self.players = [Player(out_dev)]
        monitor = None
        if args.monitor:
            monitor = Player(pick_device(args.monitor_device, "output"))
            self.players.append(monitor)

        def on_mic(indata, frames, time_info, status):
            pcm = bytes(indata)
            if self.muted:
                pcm = bytes(len(pcm))  # the API expects a continuous stream, so send silence
            elif not args.passthrough:
                lag.on_input(pcm)
            if args.passthrough:
                self.players[0].feed(pcm)
            else:
                loop.call_soon_threadsafe(mic_q.put_nowait, pcm)

        in_dev = pick_device(args.inp, "input")
        mic = sd.RawInputStream(callback=on_mic, **stream_kwargs(in_dev))
        sink.note(f"Микрофон: {sd.query_devices(in_dev)['name']}")
        sink.note(f"Для звонка: {sd.query_devices(out_dev)['name']}")

        for p in self.players:
            p.stream.start()
        mic.start()
        stop_loopback = None
        try:
            if args.passthrough:
                sink.status("Проверка", "голос без перевода идёт в кабель", True)
                await asyncio.Event().wait()

            key = load_api_key()
            if not key:
                raise Fatal("Не задан OPENAI_API_KEY (переменная окружения или файл .env рядом с программой).")
            proxy = detect_proxy(args.proxy)
            sink.note(f"Прокси: {proxy or 'нет'}")

            channels = [Channel("Я", args.lang, mic_q, self.players, "me", lag, gate_out=lambda: self.muted)]
            if not args.no_listen:
                their_q = asyncio.Queue()
                heard, stop_loopback = start_loopback(args.listen, loop, their_q, monitor)
                sink.note(f"Собеседник: {heard}")
                channels.append(Channel("Он", args.their_lang, their_q, [], "them"))
            sink.note("Говори по-русски — собеседник слышит английский.")

            tasks = [asyncio.create_task(run_channel(ch, key, proxy, sink)) for ch in channels]
            tasks.append(asyncio.create_task(sink.run()))
            try:
                await asyncio.gather(*tasks)
            finally:
                for t in tasks:
                    t.cancel()
        finally:
            if stop_loopback:
                stop_loopback.set()
            mic.stop()
            mic.close()
            for p in self.players:
                p.stream.stop()
                p.stream.close()


def build_parser():
    ap = argparse.ArgumentParser(description="Live call translator: your voice RU->EN, their speech EN->RU subtitles")
    ap.add_argument("--lang", default="en", help="language the other person hears (default: en)")
    ap.add_argument("--their-lang", default="ru", help="language of their subtitles (default: ru)")
    ap.add_argument("--in", dest="inp", help="microphone name substring or index (default: system mic)")
    ap.add_argument("--out", default="CABLE Input", help="virtual cable playback device (default: CABLE Input)")
    ap.add_argument("--listen", help="speakers/headphones the call plays through (default: system output)")
    ap.add_argument("--no-listen", action="store_true", help="don't subtitle the other person")
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
