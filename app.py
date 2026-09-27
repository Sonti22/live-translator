"""
Desktop app for the live call translator: Transync-style window (ui/) around the engine.

  pyw -3 app.py              # normal start
  pyw -3 app.py --proxy none # override the proxy setting for this run
"""
import argparse
import asyncio
import ctypes
import datetime
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import webview

import live_translator as lt
import meeting_notes
import soniox_engine
import voice_clone

UI_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "ui"
SETTINGS_FILE = lt.APP_DIR / "settings.json"
RECORDS_DIR = lt.APP_DIR / "records"
LOG_FILE = lt.APP_DIR / "live_translator.log"
SAMPLE_FILE = lt.APP_DIR / "voice_sample"  # + original extension
KEY_ENVS = {"openai": "OPENAI_API_KEY", "soniox": soniox_engine.KEY_ENV, "cartesia": voice_clone.KEY_ENV}
PREVIEW_TEXT = "Hello! This is how I sound in English. Nice to meet you, and thank you for your time."
log = logging.getLogger("app")
# rough API cost per minute of session: per translated channel, plus synthesized voice for my side
PRICE_PER_MIN = {"openai": {"channel": 0.034, "voice": 0.0}, "soniox": {"channel": 0.0035, "voice": 0.006}}

# gpt-realtime-translate output languages
LANGS = [
    ("ru", "русский"), ("en", "английский"), ("zh", "китайский"), ("ja", "японский"),
    ("ko", "корейский"), ("de", "немецкий"), ("fr", "французский"), ("es", "испанский"),
    ("it", "итальянский"), ("pt", "португальский"), ("hi", "хинди"), ("id", "индонезийский"),
    ("vi", "вьетнамский"),
]
DEFAULTS = {
    "me_lang": "ru", "peer_lang": "en",
    "me_on": True, "listen_on": True,
    "mic": None, "cable": "CABLE Input", "listen": None,
    "voice_out": True, "monitor": False, "volume": 1.0,
    "engine": "soniox", "voice": "builtin", "voice_name": "Adrian", "speed": 1.0, "voice_delay": "balanced",
    "soniox_voice_id": None, "cartesia_voice_id": None, "keywords": [], "context": "",
    "proxy": "", "on_top": False,
    "font": 18, "panel": "single", "text_mode": "both", "swap": False,
    "usage_seconds": 0.0, "usage_cost": 0.0, "advanced": False,
}
ENGINE_KEYS = {"me_lang", "peer_lang", "me_on", "listen_on", "mic", "cable", "listen", "proxy",
               "engine", "voice", "voice_name", "speed", "voice_delay", "soniox_voice_id",
               "cartesia_voice_id", "keywords", "context"}
LABELS = {"me_src": "Я", "me_dst": "Я → перевод", "them_src": "Собеседник", "them_dst": "Собеседник → перевод"}


def load_settings():
    settings = dict(DEFAULTS)
    try:
        settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return settings


def hms(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def compose_transcript(deltas):
    """[(t, kind, text)] streaming deltas -> chronological '[mm:ss] who: phrase / → translation' lines."""
    phrases = {kind: [] for kind in LABELS}
    open_ = {}
    for t, kind, text in deltas:
        cur = open_.get(kind)
        if cur is None or t - cur["last"] > 1.0:
            cur = open_[kind] = {"start": t, "text": "", "last": t}
            phrases[kind].append(cur)
        cur["text"] += text
        cur["last"] = t
        if cur["text"].rstrip().endswith((".", "?", "!", "…")):
            open_.pop(kind)
    pairs = []  # the n-th phrase of a speaker goes with the n-th translation, as on screen
    for side, who in (("me", "Я"), ("them", "Собеседник")):
        srcs, dsts = phrases[f"{side}_src"], phrases[f"{side}_dst"]
        for i in range(max(len(srcs), len(dsts))):
            src = srcs[i] if i < len(srcs) else None
            dst = dsts[i] if i < len(dsts) else None
            start = min(p["start"] for p in (src, dst) if p)
            pairs.append((start, who, src["text"].strip() if src else "", dst["text"].strip() if dst else ""))
    lines = []
    for start, who, src, dst in sorted(pairs, key=lambda p: p[0]):
        stamp = hms(start) if start >= 3600 else hms(start)[3:]  # hh:mm:ss only in calls over an hour
        lines.append(f"[{stamp}] {who}: {src or '—'}")
        if dst:
            lines.append(f"        → {dst}")
    return lines


class Bus(lt.Sink):
    """Engine reports -> numbered events that every window polls with `since`."""

    MAX = 4000

    def __init__(self):
        self._lock = threading.Lock()
        self._events = []
        self.seq = 0
        self.levels = (0.0, 0.0)
        self.t0 = time.monotonic()
        self.record = []  # (seconds since session start, kind, text)

    def emit(self, **event):
        with self._lock:
            self.seq += 1
            event["seq"] = self.seq
            self._events.append(event)
            if len(self._events) > self.MAX:
                del self._events[:self.MAX // 4]

    def since(self, seq):
        with self._lock:
            if not self._events or seq >= self.seq:
                return []
            start = max(0, len(self._events) - (self.seq - seq))
            return self._events[start:]

    def caption(self, kind, label, text):
        self.record.append((time.monotonic() - self.t0, kind, text))
        self.emit(type="caption", kind=kind, text=text)

    def note(self, text):
        log.info("note: %s", text)
        self.emit(type="note", text=text)

    def status(self, label, text, ok):
        log.info("status %s: %s", label, text)
        self.emit(type="status", label=label, text=text, ok=ok)

    def lag(self, seconds):
        self.emit(type="lag", value=round(seconds, 2))

    def level(self, me, them):
        self.levels = (me, them)


class Api:
    """Methods callable from the page as window.pywebview.api.<name>(...)."""

    def __init__(self, cli):
        self._cli = cli
        self._bus = Bus()
        self._settings = load_settings()
        self._engine = self._loop = self._task = self._thread = None
        self._muted = False
        self._paused = False
        self._started = None
        self._window = self._overlay = None
        self._hotkey_ok = lt.start_hotkey(self._on_hotkey)

    # --- state & settings ---------------------------------------------------

    def get_state(self):
        wasapi = lt.wasapi_index()
        devices = sd.query_devices()
        return {
            "settings": self._settings,
            "langs": LANGS,
            "has_key": self._has_engine_key(),
            "keys": {name: bool(lt.load_api_key(env)) for name, env in KEY_ENVS.items()},
            "cable_ok": any("CABLE Input" in d["name"] for d in devices if d["max_output_channels"] > 0),
            "sample": self._sample_path() is not None,
            "running": self._running(),
            "started": self._started,
            "muted": self._muted,
            "hotkey": lt.HOTKEY_NAME if self._hotkey_ok else None,
            "seq": self._bus.seq,
            "system_proxy": lt.detect_proxy(None),
            "mics": [d["name"] for d in devices if d["hostapi"] == wasapi and d["max_input_channels"] > 0],
            "outputs": [d["name"] for d in devices if d["hostapi"] == wasapi and d["max_output_channels"] > 0],
        }

    def save_settings(self, patch):
        changed = {k for k, v in patch.items() if self._settings.get(k) != v}
        self._settings.update(patch)
        self._write_settings()
        engine = self._engine
        if engine:
            if "monitor" in changed:
                engine.set_monitor(self._settings["monitor"])
            if "voice_out" in changed:
                engine.set_voice_out(self._settings["voice_out"])
            if "volume" in changed:
                engine.set_volume(float(self._settings["volume"]))
        if "on_top" in changed and self._window:
            self._window.on_top = bool(self._settings["on_top"])
        restart = bool(changed & ENGINE_KEYS) and self._running()
        log.info("settings changed: %s%s", sorted(changed), " -> restart" if restart else "")
        if restart:
            self._stop_engine()
            self._start_engine()
        return {"restarted": restart}

    def _write_settings(self):
        SETTINGS_FILE.write_text(json.dumps(self._settings, ensure_ascii=False, indent=2), encoding="utf-8")

    def set_key(self, key, provider="openai"):
        key = (key or "").strip()
        if not key or provider not in KEY_ENVS:
            return False
        lt.save_api_key(key, KEY_ENVS[provider])
        return True

    def _has_engine_key(self):
        return bool(lt.load_api_key(KEY_ENVS[self._settings["engine"]]))

    # --- voice: sample, clone, preview ----------------------------------------

    def _sample_path(self):
        found = sorted(lt.APP_DIR.glob(SAMPLE_FILE.name + ".*"))
        return found[0] if found else None

    def record_sample(self, seconds):
        """Record my voice for cloning from the selected microphone; returns loudness checks."""
        device = lt.pick_device(self._settings["mic"], "input")
        audio = bytearray()
        stream = sd.RawInputStream(callback=lambda data, *a: audio.extend(bytes(data)), **lt.stream_kwargs(device))
        with stream:
            time.sleep(float(seconds))
        samples = np.frombuffer(bytes(audio[:len(audio) // 2 * 2]), "<i2").astype(np.float32)
        if samples.size == 0:
            return {"ok": False, "error": "Микрофон не дал звука."}
        rms, peak = float(np.sqrt(np.mean(samples ** 2))), float(np.abs(samples).max())
        for old in lt.APP_DIR.glob(SAMPLE_FILE.name + ".*"):
            old.unlink()
        import wave
        with wave.open(str(SAMPLE_FILE.with_suffix(".wav")), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(lt.RATE)
            wav.writeframes(bytes(audio))
        verdict = ("quiet" if rms < 500 else "clipped" if peak >= 32000 else "ok")
        return {"ok": True, "seconds": round(samples.size / lt.RATE, 1), "rms": round(rms), "verdict": verdict}

    def import_sample(self):
        """Pick an existing recording of my voice (wav/mp3/m4a/ogg/flac)."""
        picked = self._window.create_file_dialog(
            webview.FileDialog.OPEN, file_types=("Аудио (*.wav;*.mp3;*.m4a;*.ogg;*.flac;*.webm)",))
        if not picked:
            return {"ok": False}
        source = Path(picked[0] if not isinstance(picked, str) else picked)
        for old in lt.APP_DIR.glob(SAMPLE_FILE.name + ".*"):
            old.unlink()
        SAMPLE_FILE.with_suffix(source.suffix.lower()).write_bytes(source.read_bytes())
        return {"ok": True, "name": source.name}

    def create_clone(self):
        """Upload the sample to the current engine's voice provider and wait until the clone is ready."""
        sample = self._sample_path()
        if sample is None:
            return {"ok": False, "error": "Сначала запиши голос или выбери файл."}
        engine = self._settings["engine"]
        provider = "soniox" if engine == "soniox" else "cartesia"
        key = lt.load_api_key(KEY_ENVS[provider])
        if not key:
            return {"ok": False, "error": f"Нужен ключ {provider.capitalize()} (⚙ Настройки)."}
        proxy = lt.detect_proxy(self._cli.proxy or self._settings["proxy"] or None)
        try:
            if provider == "soniox":
                voice_id = soniox_engine.create_voice(key, sample.read_bytes(), proxy, sample.name)
                status = "processing"
                for _ in range(40):  # usually ready within seconds
                    status = soniox_engine.voice_status(key, voice_id, proxy)
                    if status != "processing":
                        break
                    time.sleep(1.5)
                if status != "ready":
                    return {"ok": False, "error": f"Soniox не подготовил голос: {status}"}
                patch = {"soniox_voice_id": voice_id, "voice": "clone"}
            else:
                voice_id = voice_clone.create_clone(key, sample.read_bytes(), "Live Translator",
                                                    self._settings["me_lang"], proxy)
                patch = {"cartesia_voice_id": voice_id, "voice": "clone"}
        except voice_clone.CloneError as e:
            log.warning("clone failed: %s", e)
            return {"ok": False, "error": str(e)}
        log.info("voice clone created (%s): %s", provider, voice_id)
        self.save_settings(patch)
        return {"ok": True, "provider": provider}

    def preview_voice(self, voice=None):
        """Say a test phrase in the chosen voice into the headphones (never into the call)."""
        s = self._settings
        proxy = lt.detect_proxy(self._cli.proxy or s["proxy"] or None)
        try:
            if s["engine"] == "soniox":
                key = lt.load_api_key(soniox_engine.KEY_ENV)
                if not key:
                    return {"ok": False, "error": "Нужен ключ Soniox (⚙ Настройки)."}
                if voice is None:
                    voice = s["soniox_voice_id"] if s["voice"] == "clone" else s["voice_name"]
                pcm = asyncio.run(soniox_engine.speak_once(key, voice, s["peer_lang"], PREVIEW_TEXT, proxy))
            else:
                key = lt.load_api_key(voice_clone.KEY_ENV)
                if not key or not s["cartesia_voice_id"]:
                    return {"ok": False, "error": "Прослушивание доступно для клона Cartesia или голосов Soniox."}
                pcm = asyncio.run(voice_clone.speak_once(key, s["cartesia_voice_id"], s["peer_lang"],
                                                         PREVIEW_TEXT, proxy))
        except (voice_clone.CloneError, OSError) as e:
            return {"ok": False, "error": str(e)}
        player = lt.Player(lt.pick_device(None, "output"))
        player.gain = float(s["volume"])
        player.feed(pcm)
        with player.stream:
            time.sleep(len(pcm) / 2 / lt.RATE + 0.4)
        return {"ok": True}

    def list_voices(self):
        """Built-in Soniox voices for the voice picker."""
        key = lt.load_api_key(soniox_engine.KEY_ENV)
        if not key:
            return {"ok": False, "error": "Нужен ключ Soniox (⚙ Настройки)."}
        try:
            proxy = lt.detect_proxy(self._cli.proxy or self._settings["proxy"] or None)
            return {"ok": True, "voices": soniox_engine.list_voices(key, proxy)}
        except voice_clone.CloneError as e:
            return {"ok": False, "error": str(e)}

    # --- engine -------------------------------------------------------------

    def _args(self):
        s = self._settings
        return argparse.Namespace(
            lang=s["peer_lang"], their_lang=s["me_lang"], inp=s["mic"], out=s["cable"],
            listen=s["listen"], no_listen=not s["listen_on"], no_me=not s["me_on"],
            monitor=s["monitor"], monitor_device=None,
            proxy=self._cli.proxy or s["proxy"] or None, passthrough=False,
            engine=s["engine"], voice=s["voice"], voice_name=s["voice_name"], speed=float(s["speed"]),
            voice_delay=s["voice_delay"], keywords=s["keywords"], context=s["context"],
            voice_id=s["soniox_voice_id"] if s["engine"] == "soniox" else s["cartesia_voice_id"])

    def _running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        log.info("start requested (running=%s)", self._running())
        if self._running():
            return {"ok": True, "started": self._started}
        if not self._has_engine_key():
            return {"ok": False, "error": "no_key"}
        if not any("CABLE" in d["name"] for d in sd.query_devices()):
            return {"ok": False, "error": "no_cable"}
        self._bus.record = []
        self._bus.t0 = time.monotonic()
        self._paused = False
        self._started = time.time()
        self._start_engine()
        return {"ok": True, "started": self._started}

    def _start_engine(self):
        engine = lt.Engine(self._args(), self._bus)
        engine.set_muted(self._muted)
        engine.set_paused(self._paused)
        engine.voice_out = bool(self._settings["voice_out"])
        engine.volume = float(self._settings["volume"])
        self._engine = engine
        self._loop = asyncio.new_event_loop()
        self._task = self._loop.create_task(engine.run())
        self._thread = threading.Thread(target=self._run_engine, args=(self._loop, self._task), daemon=True)
        self._thread.start()
        self._bus.emit(type="running", value=True)

    def _run_engine(self, loop, task):
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except lt.Fatal as e:
            log.warning("engine stopped: %s", e)
            self._bus.emit(type="fatal", text=str(e), key="OPENAI_API_KEY" in str(e))
        except Exception as e:
            log.exception("engine crashed")
            self._bus.emit(type="fatal", text=f"{type(e).__name__}: {e}", key=False)
        finally:
            loop.close()
            if task is self._task:
                self._bus.emit(type="running", value=False)

    def _stop_engine(self):
        if self._running():
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:  # loop already closed
                pass
            self._thread.join(timeout=3)
        self._engine = None

    def stop(self):
        log.info("stop requested (running=%s)", self._running())
        self._stop_engine()
        record = self._save_record()
        self._started = None
        self._bus.emit(type="running", value=False)
        return record

    def _save_record(self):
        if not self._started:
            return None
        duration = time.time() - self._started
        s = self._settings
        channels = int(s["me_on"]) + int(s["listen_on"])
        price = PRICE_PER_MIN.get(s["engine"], PRICE_PER_MIN["openai"])
        voiced = s["me_on"] and s["voice"] not in ("off", "model")
        s["usage_seconds"] = s.get("usage_seconds", 0) + duration * channels
        s["usage_cost"] = s.get("usage_cost", 0) + duration / 60 * (price["channel"] * channels + price["voice"] * voiced)
        self._write_settings()
        lines = compose_transcript(self._bus.record)
        if not lines:
            return None
        RECORDS_DIR.mkdir(exist_ok=True)
        start = datetime.datetime.fromtimestamp(self._started)
        path = RECORDS_DIR / f"{start:%Y-%m-%d_%H-%M-%S}.txt"
        header = [f"Live Translator — {start:%d.%m.%Y %H:%M}", f"Длительность: {hms(duration)}", ""]
        path.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
        if lt.load_api_key():
            threading.Thread(target=self._auto_notes, args=(path.name,), daemon=True).start()
        return path.name

    # --- AI meeting notes -------------------------------------------------------

    def _notes_path(self, name):
        return RECORDS_DIR / (Path(name).stem + ".json")

    def _make_notes(self, name):
        key = lt.load_api_key()
        if not key:
            raise voice_clone.CloneError("Для протокола нужен ключ OpenAI (⚙ Настройки).")
        text = (RECORDS_DIR / Path(name).name).read_text(encoding="utf-8")
        proxy = lt.detect_proxy(self._cli.proxy or self._settings["proxy"] or None)
        notes = meeting_notes.summarize(key, text, proxy)
        self._notes_path(name).write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
        return notes

    def _auto_notes(self, name):
        try:
            notes = self._make_notes(name)
            self._bus.emit(type="notes", name=name, title=notes.get("title", ""))
        except (voice_clone.CloneError, OSError, ValueError) as e:
            log.warning("meeting notes failed: %s", e)
            self._bus.emit(type="notes_error", text=str(e))

    def get_record(self, name):
        path = RECORDS_DIR / Path(name).name
        notes_path = self._notes_path(name)
        notes = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.exists() else None
        return {"name": path.name, "text": path.read_text(encoding="utf-8") if path.exists() else "",
                "notes": notes, "can_summarize": bool(lt.load_api_key())}

    def summarize_record(self, name):
        try:
            return {"ok": True, "notes": self._make_notes(name)}
        except (voice_clone.CloneError, OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}

    def export_record(self, name):
        record = self.get_record(name)
        target = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=Path(name).stem + ".md",
                                                 file_types=("Markdown (*.md)",))
        if not target:
            return None
        target = target if isinstance(target, str) else target[0]
        header = record["text"].splitlines()[0] if record["text"] else name
        Path(target).write_text(meeting_notes.to_markdown(record["notes"] or {}, record["text"], header),
                                encoding="utf-8")
        return target

    def set_muted(self, muted):
        self._muted = bool(muted)
        if self._engine:
            self._engine.set_muted(self._muted)
        self._bus.emit(type="muted", value=self._muted)
        return self._muted

    def set_paused(self, paused):
        self._paused = bool(paused)
        if self._engine:
            self._engine.set_paused(self._paused)
        self._bus.emit(type="paused", value=self._paused)
        return self._paused

    def _on_hotkey(self):
        self.set_muted(not self._muted)

    def log_js(self, message):
        log.error("ui: %s", message)

    def poll(self, since):
        me, them = self._bus.levels if self._running() else (0.0, 0.0)
        return {"events": self._bus.since(since), "me": me, "them": them,
                "running": self._running(), "muted": self._muted, "paused": self._paused}

    # --- records ------------------------------------------------------------

    def list_records(self):
        records = []
        for path in sorted(RECORDS_DIR.glob("*.txt"), reverse=True)[:100]:  # notes live next to them as .json
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            first = next((line.split(": ", 1)[-1] for line in lines[3:] if ": " in line), "")
            notes_path = self._notes_path(path.name)
            if notes_path.exists():
                try:
                    first = json.loads(notes_path.read_text(encoding="utf-8")).get("title") or first
                except (OSError, ValueError):
                    pass
            records.append({
                "name": path.name,
                "notes": notes_path.exists(),
                "title": (first[:60] + "…") if len(first) > 60 else first,
                "date": lines[0].split("— ", 1)[-1] if lines else path.stem,
                "duration": lines[1].split(": ", 1)[-1] if len(lines) > 1 else "",
            })
        usage = self._settings.get("usage_seconds", 0)
        return {"records": records, "usage": hms(usage), "cost": round(self._settings.get("usage_cost", 0), 2)}

    def open_record(self, name):
        path = RECORDS_DIR / Path(name).name
        if path.exists():
            os.startfile(path)

    def open_url(self, url):
        if url.startswith("https://"):
            import webbrowser
            webbrowser.open(url)

    def open_records_folder(self):
        RECORDS_DIR.mkdir(exist_ok=True)
        os.startfile(RECORDS_DIR)

    # --- windows ------------------------------------------------------------

    def toggle_overlay(self):
        if self._overlay is not None:
            self._overlay.destroy()
            return False
        geom = self._settings.get("overlay_geom") or {}
        self._overlay = webview.create_window(
            "Субтитры — Live Translator", url=str(UI_DIR / "overlay.html"), js_api=self,
            width=geom.get("w", 780), height=geom.get("h", 180), x=geom.get("x"), y=geom.get("y"),
            min_size=(360, 110), frameless=True, easy_drag=True, on_top=True, background_color="#161616")
        self._overlay.events.closed += self._overlay_closed
        self._overlay.events.moved += self._overlay_moved
        self._overlay.events.resized += self._overlay_resized
        return True

    def _overlay_moved(self, x, y):
        self._settings.setdefault("overlay_geom", {}).update(x=x, y=y)

    def _overlay_resized(self, width, height):
        self._settings.setdefault("overlay_geom", {}).update(w=width, h=height)

    def _overlay_closed(self):
        self._overlay = None
        self._write_settings()  # remember where the floating subtitles were
        self._bus.emit(type="overlay", value=False)

    def close_overlay(self):
        if self._overlay is not None:
            self._overlay.destroy()

    def _on_shown(self):
        set_dark_title_bar("Live Translator")
        if self._settings.get("on_top"):
            self._window.on_top = True

    def _shutdown(self):
        self.stop()
        self.close_overlay()


def set_dark_title_bar(title):
    """Dark native title bar (Windows 10 20H1+ / 11) to match the dark UI."""
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if hwnd:
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except (AttributeError, OSError):
        pass


def main():
    logging.basicConfig(filename=LOG_FILE, encoding="utf-8", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("app start, %s", lt.APP_DIR)
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", help="proxy URL or 'none' for this run (default: settings / system)")
    cli, _ = parser.parse_known_args()
    api = Api(cli)
    window = webview.create_window(
        "Live Translator", url=str(UI_DIR / "index.html"), js_api=api,
        width=1240, height=780, min_size=(900, 560), background_color="#1B1B1B")
    api._window = window
    window.events.shown += api._on_shown
    window.events.closed += api._shutdown
    webview.start(private_mode=True)


if __name__ == "__main__":
    main()
