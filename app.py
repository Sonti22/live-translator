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

import sounddevice as sd
import webview

import live_translator as lt

UI_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "ui"
SETTINGS_FILE = lt.APP_DIR / "settings.json"
RECORDS_DIR = lt.APP_DIR / "records"
LOG_FILE = lt.APP_DIR / "live_translator.log"
log = logging.getLogger("app")
PRICE_PER_MIN = 0.034  # gpt-realtime-translate, per channel

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
    "proxy": "", "on_top": False,
    "font": 18, "panel": "single", "text_mode": "both", "swap": False,
    "usage_seconds": 0.0,
}
ENGINE_KEYS = {"me_lang", "peer_lang", "me_on", "listen_on", "mic", "cable", "listen", "proxy"}
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
        lines.append(f"[{hms(start)[3:]}] {who}: {src or '—'}")
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
            "has_key": bool(lt.load_api_key()),
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

    def set_key(self, key):
        key = (key or "").strip()
        if not key:
            return False
        lt.save_api_key(key)
        return True

    # --- engine -------------------------------------------------------------

    def _args(self):
        s = self._settings
        return argparse.Namespace(
            lang=s["peer_lang"], their_lang=s["me_lang"], inp=s["mic"], out=s["cable"],
            listen=s["listen"], no_listen=not s["listen_on"], no_me=not s["me_on"],
            monitor=s["monitor"], monitor_device=None,
            proxy=self._cli.proxy or s["proxy"] or None, passthrough=False)

    def _running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        log.info("start requested (running=%s)", self._running())
        if self._running():
            return {"ok": True, "started": self._started}
        if not lt.load_api_key():
            return {"ok": False, "error": "no_key"}
        self._bus.record = []
        self._bus.t0 = time.monotonic()
        self._started = time.time()
        self._start_engine()
        return {"ok": True, "started": self._started}

    def _start_engine(self):
        engine = lt.Engine(self._args(), self._bus)
        engine.set_muted(self._muted)
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
        channels = int(self._settings["me_on"]) + int(self._settings["listen_on"])
        self._settings["usage_seconds"] = self._settings.get("usage_seconds", 0) + duration * channels
        self._write_settings()
        lines = compose_transcript(self._bus.record)
        if not lines:
            return None
        RECORDS_DIR.mkdir(exist_ok=True)
        start = datetime.datetime.fromtimestamp(self._started)
        path = RECORDS_DIR / f"{start:%Y-%m-%d_%H-%M-%S}.txt"
        header = [f"Live Translator — {start:%d.%m.%Y %H:%M}", f"Длительность: {hms(duration)}", ""]
        path.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
        return path.name

    def set_muted(self, muted):
        self._muted = bool(muted)
        if self._engine:
            self._engine.set_muted(self._muted)
        self._bus.emit(type="muted", value=self._muted)
        return self._muted

    def _on_hotkey(self):
        self.set_muted(not self._muted)

    def log_js(self, message):
        log.error("ui: %s", message)

    def poll(self, since):
        me, them = self._bus.levels if self._running() else (0.0, 0.0)
        return {"events": self._bus.since(since), "me": me, "them": them,
                "running": self._running(), "muted": self._muted}

    # --- records ------------------------------------------------------------

    def list_records(self):
        records = []
        for path in sorted(RECORDS_DIR.glob("*.txt"), reverse=True)[:100]:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            first = next((line.split(": ", 1)[-1] for line in lines[3:] if ": " in line), "")
            records.append({
                "name": path.name,
                "title": (first[:60] + "…") if len(first) > 60 else first,
                "date": lines[0].split("— ", 1)[-1] if lines else path.stem,
                "duration": lines[1].split(": ", 1)[-1] if len(lines) > 1 else "",
            })
        usage = self._settings.get("usage_seconds", 0)
        return {"records": records, "usage": hms(usage), "cost": round(usage / 60 * PRICE_PER_MIN, 2)}

    def open_record(self, name):
        path = RECORDS_DIR / Path(name).name
        if path.exists():
            os.startfile(path)

    def open_records_folder(self):
        RECORDS_DIR.mkdir(exist_ok=True)
        os.startfile(RECORDS_DIR)

    # --- windows ------------------------------------------------------------

    def toggle_overlay(self):
        if self._overlay is not None:
            self._overlay.destroy()
            return False
        self._overlay = webview.create_window(
            "Субтитры — Live Translator", url=str(UI_DIR / "overlay.html"), js_api=self,
            width=780, height=180, min_size=(360, 110), frameless=True, easy_drag=True,
            on_top=True, background_color="#161616")
        self._overlay.events.closed += self._overlay_closed
        return True

    def _overlay_closed(self):
        self._overlay = None
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
