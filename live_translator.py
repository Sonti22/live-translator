"""
Live call translator (Zoom, Telegram, WhatsApp, Discord, Meet, Teams).

  You:  microphone -> gpt-realtime-translate -> English voice -> VB-Cable -> the call hears English
  Them: what plays in your headphones -> gpt-realtime-translate -> Russian subtitles in this window

In the call app pick microphone "CABLE Output (VB-Audio Virtual Cable)".

Usage:
  py -3 live_translator.py                 # both directions
  py -3 live_translator.py --no-listen     # only your voice -> English
  py -3 live_translator.py --monitor       # also hear your translation in the headphones
  py -3 live_translator.py --passthrough   # no API: mic straight into the cable (routing test)
  py -3 live_translator.py --list          # list audio devices
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
from pathlib import Path
from queue import SimpleQueue

import numpy as np
import sounddevice as sd
# Import order matters: sounddevice puts the main thread in a COM STA first, which soundcard tolerates
import soundcard as sc
from python_socks import ProxyError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

URL = "wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate"
RATE = 24_000  # API requires mono PCM16 at 24 kHz
BLOCK = 480    # 20 ms per chunk

FATAL_ERRORS = {  # API error codes that reconnecting won't fix
    "invalid_api_key": "Проверь OPENAI_API_KEY.",
    "insufficient_quota": "Пополни баланс: platform.openai.com/settings/organization/billing.",
    "unsupported_country_region_territory": "OpenAI блокирует твой регион: включи VPN.",
    "model_not_found": "У аккаунта API нет доступа к gpt-realtime-translate.",
}

DIM, CYAN, YELLOW, RESET = "\033[90m", "\033[96m", "\033[93;1m", "\033[0m"


class Fatal(Exception):
    pass


def load_api_key():
    key = os.environ.get("OPENAI_API_KEY")
    env_file = Path(__file__).with_name(".env")
    if not key and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "OPENAI_API_KEY":
                key = value.strip().strip('"').strip("'")
    return key


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
        sys.exit(f"Аудиоустройство не найдено: {name!r}. Запусти с --list.")
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


class Captions:
    """Collects streaming transcript deltas per stream and prints whole phrases."""

    IDLE = 0.8

    def __init__(self):
        self.buf = {}  # label -> [text, style, last_update]

    def put(self, label, style, text):
        entry = self.buf.setdefault(label, ["", style, 0.0])
        entry[0] += text
        entry[2] = time.monotonic()
        if entry[0].rstrip().endswith((".", "?", "!", "…")):
            self.flush(label)

    def flush(self, label):
        text, style, _ = self.buf.pop(label)
        if text.strip():
            print(f"{style}{label:>8}: {text.strip()}{RESET}", flush=True)

    def note(self, text):
        print(f"{DIM}{'':>8}  {text}{RESET}", flush=True)

    async def run(self):
        while True:
            await asyncio.sleep(0.2)
            now = time.monotonic()
            for label in [k for k, v in self.buf.items() if now - v[2] > self.IDLE]:
                self.flush(label)


class Channel:
    """One direction: audio queue -> gpt-realtime-translate -> audio to players and/or subtitles."""

    def __init__(self, name, lang, queue, players, src_style, dst_style, lag=None):
        self.name, self.lang, self.queue, self.players, self.lag = name, lang, queue, players, lag
        self.src_label, self.dst_label = name, f"{name} → {lang.upper()}"
        self.src_style, self.dst_style = src_style, dst_style


async def pump_audio(ws, queue):
    while True:
        pcm = await queue.get()
        await ws.send(json.dumps({
            "type": "session.input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode(),
        }))


async def run_session(ch, key, proxy, captions):
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
                    if not ch.players:
                        continue
                    lag = ch.lag.on_output() if ch.lag else None
                    if lag is not None:
                        captions.note(f"задержка ≈ {lag:.1f} с")
                    pcm = base64.b64decode(event["delta"])
                    for p in ch.players:
                        p.feed(pcm)
                elif kind == "session.input_transcript.delta":
                    captions.put(ch.src_label, ch.src_style, event["delta"])
                elif kind == "session.output_transcript.delta":
                    captions.put(ch.dst_label, ch.dst_style, event["delta"])
                elif kind == "session.updated":
                    print(f"{DIM}[{ch.dst_label}] подключено{RESET}", flush=True)
                elif kind == "error":
                    err = event.get("error") or {}
                    if err.get("code") in FATAL_ERRORS:
                        raise Fatal(f"[API error] {err.get('message')}\n{FATAL_ERRORS[err['code']]}")
                    print(f"\n[API error] {err or event}", flush=True)
        finally:
            sender.cancel()


async def run_channel(ch, key, proxy, captions):
    while True:
        try:
            await run_session(ch, key, proxy, captions)
        except InvalidStatus as e:
            code = e.response.status_code
            if code in (401, 403):
                raise Fatal(f"API отклонил запрос (HTTP {code}): неверный ключ, нет оплаты "
                            "или регион заблокирован — включи VPN.")
            print(f"[{ch.dst_label}] HTTP {code}, переподключаюсь...", flush=True)
        except (ConnectionClosed, OSError, ProxyError) as e:
            print(f"[{ch.dst_label}] связь потеряна: {e} — переподключаюсь (VPN включён?)", flush=True)
        await asyncio.sleep(2)


def start_loopback(name, loop, queue, gate):
    """Capture what plays in the headphones (the other person) on a background thread."""
    started = SimpleQueue()

    def worker():
        # The main thread is a COM STA (PortAudio), so this thread joins the MTA itself
        ctypes.windll.ole32.CoInitializeEx(None, 0)
        try:
            warnings.filterwarnings("ignore", category=getattr(sc, "SoundcardRuntimeWarning", RuntimeWarning))
            speaker = sc.default_speaker() if name is None else sc.get_speaker(name)
            source = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            rec = source.recorder(samplerate=RATE, channels=1, blocksize=BLOCK).__enter__()
        except Exception as e:
            started.put(e)
            return
        started.put(speaker.name)
        while True:
            data = rec.record(numframes=BLOCK)[:, 0]
            if gate is not None and gate.busy:  # don't subtitle our own translation
                data = np.zeros_like(data)
            pcm = (np.clip(data, -1, 1) * 32767).astype("<i2").tobytes()
            try:
                loop.call_soon_threadsafe(queue.put_nowait, pcm)
            except RuntimeError:  # event loop closed on exit
                return

    threading.Thread(target=worker, daemon=True).start()
    result = started.get()
    if isinstance(result, Exception):
        sys.exit(f"Не удалось слушать собеседника ({result}). Запусти с --no-listen или укажи --listen.")
    return result


async def main_async(args):
    loop = asyncio.get_running_loop()
    mic_q = asyncio.Queue()
    lag = LagMeter()

    out_dev = pick_device(args.out, "output")
    players = [Player(out_dev)]
    monitor = None
    if args.monitor:
        monitor = Player(pick_device(args.monitor_device, "output"))
        players.append(monitor)

    def on_mic(indata, frames, time_info, status):
        pcm = bytes(indata)
        if args.passthrough:
            players[0].feed(pcm)
            return
        lag.on_input(pcm)
        loop.call_soon_threadsafe(mic_q.put_nowait, pcm)

    in_dev = pick_device(args.inp, "input")
    mic = sd.RawInputStream(callback=on_mic, **stream_kwargs(in_dev))
    print(f"Микрофон:     {sd.query_devices(in_dev)['name']}")
    print(f"Для звонка:   {sd.query_devices(out_dev)['name']}")

    for p in players:
        p.stream.start()
    mic.start()
    try:
        if args.passthrough:
            print("Проверка: твой голос без перевода идёт в кабель. Ctrl+C — выход.")
            await asyncio.Event().wait()

        key = load_api_key()
        if not key:
            sys.exit("Не задан OPENAI_API_KEY (переменная окружения или файл .env рядом со скриптом).")
        proxy = detect_proxy(args.proxy)
        print(f"Прокси:       {proxy or 'нет'}")

        captions = Captions()
        channels = [Channel("Я", args.lang, mic_q, players, DIM, CYAN, lag)]
        if not args.no_listen:
            their_q = asyncio.Queue()
            heard = start_loopback(args.listen, loop, their_q, monitor)
            print(f"Собеседник:   {heard}")
            channels.append(Channel("Он", args.their_lang, their_q, [], DIM, YELLOW))
        print("Говори по-русски — собеседник слышит английский. Его речь — текстом ниже. Ctrl+C — выход.\n")

        tasks = [asyncio.create_task(run_channel(ch, key, proxy, captions)) for ch in channels]
        tasks.append(asyncio.create_task(captions.run()))
        try:
            await asyncio.gather(*tasks)
        except Fatal as e:
            sys.exit(f"\n{e}")
        finally:
            for t in tasks:
                t.cancel()
    finally:
        mic.stop()
        for p in players:
            p.stream.stop()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.system("")  # enable ANSI colors in the Windows console
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
    args = ap.parse_args()

    if args.list:
        print(sd.query_devices())
        return
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
