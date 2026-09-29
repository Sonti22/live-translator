"""
Desktop app for the live call translator: Transync-style window (ui/) around the engine.

  pyw -3 app.py              # normal start
  pyw -3 app.py --proxy none # override the proxy setting for this run
"""
import argparse
import asyncio
import ctypes
import datetime
import io
import json
import logging
import os
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import webview

import live_translator as lt
import meeting_notes
import netcheck
import soniox_engine
import speech_audio
import voice_clone
import winstealth

UI_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "ui"
SETTINGS_FILE = lt.APP_DIR / "settings.json"
RECORDS_DIR = lt.APP_DIR / "records"
LOG_FILE = lt.APP_DIR / "live_translator.log"
SAMPLE_FILE = lt.APP_DIR / "voice_sample"  # + original extension
REC_MAX, REC_MIN = 60.0, 30.0  # seconds of my voice: the recording stops itself at the first, Done opens at the second
INWORLD_MAX = 30.0  # seconds of a sample Inworld takes
SOUND_SETTINGS = "ms-settings:sound"
KEY_ENVS = {"openai": "OPENAI_API_KEY", "soniox": soniox_engine.KEY_ENV, "cartesia": voice_clone.KEY_ENV,
            "inworld": "INWORLD_API_KEY"}
BUILTIN_FIELDS = {"soniox": "voice_name", "cartesia": "cartesia_builtin_id", "inworld": "inworld_voice_name"}
PREVIEW_TEXT = "Hello! This is how I sound in English. Nice to meet you, and thank you for your time."
log = logging.getLogger("app")
# rough API cost per minute of session: per translated channel, plus synthesized voice for my side
# (Soniox, soniox.com/pricing: STT+translation $0.12/h; TTS ~$0.70 per hour of speech, I talk about half the call;
# the Soniox engine with Cartesia TTS: ~$2.70 per hour of speech, with Inworld TTS: ~$0.90)
PRICE_PER_MIN = {"openai": {"channel": 0.034, "voice": 0.0}, "soniox": {"channel": 0.002, "voice": 0.006},
                 "cartesia": {"channel": 0.002, "voice": 0.0225}, "inworld": {"channel": 0.002, "voice": 0.0075}}
SETTINGS_VERSION = 3

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
    "usage_seconds": 0.0, "usage_cost": 0.0, "advanced": False, "diarize": True,
    "engine_auto": True,  # engine picked by the app from the available keys, not by hand
    "provider_auto": True,  # Cartesia takes the voice once its key is there, until a provider is picked by hand
    "delivery": "balanced", "match_rate": True,  # how the voice paces itself: speed / balance / naturalness
    "soniox_region": "",  # "" (auto), "us" or "eu": where Soniox processes the audio
    # latency levers, all on by default (auto_finalize is read by the STT channel)
    "speed_boost": True, "trim_silence": True, "instant_phrases": True, "auto_finalize": True,
    # who speaks my translation in the Soniox engine: Soniox TTS, Cartesia or Inworld
    "voice_provider": "soniox", "inworld_voice_id": None, "inworld_voice_name": "Clive",
    "inworld_model": "inworld-tts-2-flash", "cartesia_builtin_id": None,
    # call mode: the windows stay out of screen sharing and recordings, the subtitles open by themselves
    "hide_from_capture": True, "overlay_auto": True, "overlay_opacity": 0.85, "overlay_click_through": False,
    "onboarding_done": False, "hints_seen": [],  # the first-run walkthrough and the one-time tips already shown
    "clone_auto_off": False,  # «мой клон» picked, but the chosen voice provider has no clone of me yet
    "settings_version": SETTINGS_VERSION,
}
ENGINE_KEYS = {"me_lang", "peer_lang", "me_on", "listen_on", "mic", "cable", "listen", "proxy",
               "engine", "voice", "voice_name", "speed", "voice_delay", "soniox_voice_id",
               "cartesia_voice_id", "keywords", "context", "diarize", "speed_boost", "trim_silence",
               "instant_phrases", "auto_finalize", "voice_provider", "inworld_voice_id",
               "inworld_voice_name", "inworld_model", "cartesia_builtin_id", "delivery", "match_rate",
               "soniox_region"}
# the OpenAI engine has no dictionary, context, speaker labels, delivery or Soniox region of its own
SONIOX_ONLY = {"keywords", "context", "diarize", "delivery", "match_rate", "soniox_region"}
# what is translated, from where, into what and where to: waiting for a pause would lose or misroute speech meanwhile
AT_ONCE = {"me_lang", "peer_lang", "me_on", "listen_on", "mic", "cable", "listen", "proxy", "engine"}
QUIET_LEVEL = 0.1    # meter level of speech (600 RMS, like AutoFinalize.LOUD): below it nobody is speaking
RESTART_QUIET = 1.5  # seconds nobody spoke before a setting changed mid-call restarts the engine...
RESTART_WAIT = 30.0  # ...but it waits no longer than this for such a pause
CLONE_DELETE_WAIT = 5.0  # closing the window waits this long for a voice clone being deleted
SETTINGS_UNSAVED = "не записаны на диск: после перезапуска вернутся прежние"
STOP_WAIT = 3.0      # seconds a stop (or the next start) waits for the engine to close its devices
NOTES_PENDING = ".notes-pending"  # next to a record whose AI notes are not made yet
LABELS = {"me_src": "Я", "me_dst": "Я → перевод", "them_src": "Собеседник", "them_dst": "Собеседник → перевод"}
MAIN_TITLE, OVERLAY_TITLE = "Live Translator", "Субтитры — Live Translator"
STEALTH_WAIT, STEALTH_STEP = 3.0, 0.1  # a new window gets its handle a moment after its event: look for it this long


def load_settings():
    settings = dict(DEFAULTS)
    saved, damaged = {}, False
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        damaged = not isinstance(saved, dict)  # valid JSON of another shape is no settings either
    except OSError:
        pass
    except ValueError:
        damaged = True
    if damaged:  # keep the damaged file for a look instead of overwriting it with defaults
        saved = {}
        log.warning("settings.json is damaged: moved to settings.json.bad, using defaults")
        try:
            SETTINGS_FILE.replace(SETTINGS_FILE.with_suffix(".json.bad"))
        except OSError:
            pass
    settings.update(saved)
    try:
        version = int(saved.get("settings_version", 1))
    except (TypeError, ValueError, OverflowError):  # edited by hand: an unreadable version is an old one
        version = 1
    if version < 3:
        if settings["speed"] == 1.1:
            settings["speed"] = 1.0  # the old default: a faster voice sounds hurried, the speed is the delivery's job
        if saved.get("voice_provider", "soniox") != "soniox":
            settings["provider_auto"] = False  # a provider picked before the automatic choice existed stays
    settings["settings_version"] = SETTINGS_VERSION
    return settings


def voice_module(provider):
    """The provider's module: list_voices everywhere; create_voice, delete_voice, speak_once in Soniox and
    Inworld (a Cartesia clone and preview go through voice_clone)."""
    if provider == "cartesia":
        import cartesia_engine  # the optional alternative voices
        return cartesia_engine
    if provider == "inworld":
        import inworld_engine
        return inworld_engine
    return soniox_engine


def checked_sample(pcm, rate):
    """The sample checks without speech_audio.prepare_sample: the recording as it is, judged by loudness alone."""
    samples = np.frombuffer(pcm, "<i2").astype(np.float32)
    rms, peak = float(np.sqrt(np.mean(samples ** 2))), float(np.abs(samples).max())
    verdict = "quiet" if rms < 500 else "clipped" if peak >= 32000 else "ok"
    return pcm, {"verdict": verdict, "speech_seconds": round(samples.size / rate, 1)}


def clip_wav(data, seconds):
    """The first `seconds` of a WAV file; anything else, or a shorter clip, comes back unchanged."""
    try:
        with wave.open(io.BytesIO(data)) as wav:
            params, keep = wav.getparams(), int(seconds * wav.getframerate())
            if wav.getnframes() <= keep:
                return data
            frames = wav.readframes(keep)
    except (wave.Error, EOFError):
        return data
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setparams(params)
        wav.writeframes(frames)
    return out.getvalue()


def resolved(value):
    """speak_once may stream over a websocket (a coroutine) or be a plain REST call."""
    return asyncio.run(value) if asyncio.iscoroutine(value) else value


def refresh_devices():
    """PortAudio's device list and defaults as Windows has them now, not as at launch (a headset plugged in since).
    live_translator.refresh_devices does nothing while an audio stream is open."""
    refresh = getattr(lt, "refresh_devices", None)
    if refresh:
        refresh()


def has_cable(devices):
    """A VB-Cable playback device (CABLE Input, CABLE-A Input, ...): where my English goes."""
    return any(lt.is_cable(d["name"]) and d["max_output_channels"] > 0 for d in devices)


def hms(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def compose_transcript(deltas):
    """[(t, kind, text[, speaker])] streaming deltas -> chronological '[mm:ss] who: phrase / → translation'."""
    phrases, open_ = {}, {}
    speakers = sorted({d[3] for d in deltas if len(d) > 3 and d[3]})
    for t, kind, text, *rest in deltas:
        speaker = None
        if kind.startswith("them") and speakers:
            speaker = (rest[0] if rest else None) or speakers[0]  # words before diarization kicked in
        key = (kind, speaker)
        cur = open_.get(key)
        if cur is None or t - cur["last"] > 1.0:
            cur = open_[key] = {"start": t, "text": "", "last": t}
            phrases.setdefault(key, []).append(cur)
        cur["text"] += text
        cur["last"] = t
        if cur["text"].rstrip().endswith((".", "?", "!", "…")):
            open_.pop(key)
    # A translation goes with the speaker's phrases that began before it (or right after: the
    # transcript of the original may lag behind), so one extra split never shifts later pairs.
    pairs = []
    voices = [("me", None, "Я")] + [("them", sp, f"Собеседник {sp}" if len(speakers) > 1 else "Собеседник")
                                   for sp in (speakers or [None])]
    for side, speaker, who in voices:
        srcs = phrases.get((f"{side}_src", speaker), [])
        dsts = phrases.get((f"{side}_dst", speaker), [])
        i = 0
        for dst in dsts:
            group = []
            while i < len(srcs) and srcs[i]["start"] <= dst["start"] + 1.0:
                group.append(srcs[i])
                i += 1
            start = min([dst["start"]] + [p["start"] for p in group])
            pairs.append((start, who, " ".join(p["text"].strip() for p in group), dst["text"].strip()))
        pairs += [(src["start"], who, src["text"].strip(), "") for src in srcs[i:]]
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
        self.loud = float("-inf")  # when someone was last heard on either side
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

    def caption(self, kind, label, text, speaker=None):
        self.record.append((time.monotonic() - self.t0, kind, text, speaker))
        self.emit(type="caption", kind=kind, text=text, **({"speaker": speaker} if speaker else {}))

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
        if max(me, them) >= QUIET_LEVEL:
            self.loud = time.monotonic()


class Api:
    """Methods callable from the page as window.pywebview.api.<name>(...)."""

    def __init__(self, cli):
        self._cli = cli
        self._bus = Bus()
        self._settings = load_settings()
        self._use_region()
        self._engine = self._loop = self._task = self._thread = None
        self._lifecycle = threading.RLock()  # pywebview runs each JS call on its own thread
        self._settings_lock = threading.Lock()
        self._unsaved = False  # the last write of settings.json failed
        self._restarting = False
        self._restart_pending = False  # a setting changed mid-sentence: the engine restarts in the next pause
        self._restarter = None
        self._reaper = None  # the thread deleting them
        self._stale_clones = []  # clones replaced during a call: deleted once the call has moved to the new one
        self._muted = False
        self._paused = False
        self._recording = None  # (stream, audio, rate) while my voice is being recorded
        self._rec_level = 0.0
        self._rec_lock = threading.Lock()
        self._started = None
        self._window = self._overlay = None
        self._overlay_lock = threading.Lock()
        self._hidden = False  # the panic hotkey has hidden the windows
        self._hotkey_ok = lt.start_hotkey(self._on_hotkey)
        self._hotkey_done_ok = lt.start_hotkey(self._on_done_hotkey, **lt.DONE_KEY)
        self._hotkey_hide_ok = lt.start_hotkey(self._on_hide_hotkey, **lt.HIDE_KEY)

    # --- state & settings ---------------------------------------------------

    def get_state(self):
        notice = self._notice()
        if not self._running():
            refresh_devices()
        wasapi = lt.wasapi_index()
        devices = lt.query_devices()  # never while PortAudio is being re-initialised
        self._adopt_cable(devices)
        return {
            "notice": notice,
            "settings": self._settings,
            "langs": LANGS,
            "has_key": self._has_engine_key(),
            "keys": {name: bool(lt.load_api_key(env)) for name, env in KEY_ENVS.items()},
            "cable_ok": has_cable(devices),
            "sample": self._sample_path() is not None,
            "running": self._running(),
            "started": self._started,
            "muted": self._muted,
            "paused": self._paused,
            "hotkey": lt.HOTKEY_NAME if self._hotkey_ok else None,
            "hotkey_done": lt.HOTKEY_DONE_NAME if self._hotkey_done_ok else None,
            "hotkey_hide": lt.HOTKEY_HIDE_NAME if self._hotkey_hide_ok else None,
            "seq": self._bus.seq,
            "system_proxy": lt.detect_proxy(None),
            "mics": [d["name"] for d in devices if d["hostapi"] == wasapi and d["max_input_channels"] > 0],
            "outputs": [d["name"] for d in devices if d["hostapi"] == wasapi and d["max_output_channels"] > 0],
            **self.default_devices(),
        }

    def get_stealth_status(self):
        """Whether the windows really are out of screen capture, read back from the OS. Never raises."""
        enabled = bool(self._settings.get("hide_from_capture"))
        try:
            return {"supported": winstealth.supported(), "enabled": enabled, "main": self._excluded(MAIN_TITLE),
                    "overlay": self._excluded(OVERLAY_TITLE), "build": winstealth.build()}
        except Exception:
            log.exception("stealth status")
            return {"supported": False, "enabled": enabled, "main": None, "overlay": None, "build": 0}

    @staticmethod
    def _excluded(title):
        """None: no such window is open; else whether the OS reports all of them excluded from capture."""
        hwnds = winstealth.find_windows(title)
        if not hwnds:
            return None
        return all(winstealth.get_affinity(hwnd) == winstealth.WDA_EXCLUDEFROMCAPTURE for hwnd in hwnds)

    def _adopt_cable(self, devices):
        """The default «CABLE Input» is not installed, another VB-Cable is (CABLE-A Input, CABLE In 16ch): that one
        becomes the cable, so the window, the pre-call check and the engine all mean the same device."""
        name = self._settings["cable"]
        outputs = [d for d in devices if d["max_output_channels"] > 0]
        if name != DEFAULTS["cable"] or any(name.lower() in d["name"].lower() for d in outputs):
            return
        wasapi = lt.wasapi_index()
        cables = sorted((d for d in outputs if lt.is_cable(d["name"])), key=lambda d: d["hostapi"] != wasapi)
        if cables:
            log.info("cable %r not found, using %r", name, cables[0]["name"])
            self.save_settings({"cable": cables[0]["name"]})

    def default_devices(self):
        """Windows default microphone and playback: what Zoom / Meet use unless told otherwise."""
        return {"default_mic": lt.default_name("input"), "default_out": lt.default_name("output")}

    def _proxy(self):
        return lt.detect_proxy(self._cli.proxy or self._settings["proxy"] or None)

    def _use_region(self):
        """Soniox's servers of the saved region (settings → Интернет), for the checks and the next engine."""
        self._region = self._settings.get("soniox_region", "")
        lt.use_soniox_region(self._region)

    def save_settings(self, patch):
        with self._lifecycle:
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
            if changed & {"hide_from_capture", "overlay_opacity", "overlay_click_through"}:
                self._restyle()
            if "soniox_region" in changed and not self._running():  # a running call moves over with its restart
                self._use_region()
            keys = ENGINE_KEYS - SONIOX_ONLY if self._settings["engine"] == "openai" else ENGINE_KEYS
            restart = bool(changed & keys) and self._running() and self._started is not None  # not a stopped call
            now = restart and (bool(changed & AT_ONCE) or self._quiet())
            log.info("settings changed: %s%s", sorted(changed),
                     " -> restart" if now else " -> restart in a pause" if restart else "")
            if now:
                self._restart()
            elif restart and not self._restart_pending:
                self._restart_pending = True
                self._restarter = threading.Thread(target=self._restart_when_quiet, daemon=True)
                self._restarter.start()
            return {"restarted": now, "pending": restart and not now}

    def _restart(self):
        """(Under _lifecycle) A new engine with the saved settings takes over the call."""
        self._restart_pending = False
        self._restarting = True  # poll keeps reporting "running" while the engine is swapped
        try:
            self._stop_engine()
            self._bus.emit(type="restarted")
            if self._unsaved:  # the UI clears its status line on "restarted": say it again
                self._bus.status("Настройки", SETTINGS_UNSAVED, False)
            self._start_engine()
        finally:
            self._restarting = False

    def _restart_when_quiet(self):
        """(Thread) The restart would cut off English mid-word or a phrase being said: it waits for a pause."""
        deadline = time.monotonic() + RESTART_WAIT
        while self._restart_pending and not self._quiet() and time.monotonic() < deadline:
            time.sleep(0.1)
        with self._lifecycle:
            if self._restart_pending and self._running():
                self._restart()

    def _quiet(self):
        """Nothing would be cut off now: no English playing or waiting to be spoken, no phrase of mine still being
        recognized, nobody heard for RESTART_QUIET s."""
        if time.monotonic() - self._bus.loud < RESTART_QUIET:
            return False
        engine = self._engine
        voice = getattr(engine, "voice", None)
        finalizer = getattr(getattr(engine, "me_channel", None), "finalizer", None)
        try:
            return not (any(p.busy for p in getattr(engine, "players", ()))
                        or (hasattr(voice, "queued_seconds") and voice.queued_seconds() > 0.05)
                        or getattr(finalizer, "pending", False))
        except RuntimeError:  # the engine loop changed its streams while they were counted
            return False

    def _write_settings(self):
        """Atomic: a crash or a second writer never leaves a half-written settings.json. A disk that refuses it
        (full, read-only, locked) is reported, never raised: the settings stay in memory and the call goes on."""
        with self._settings_lock:
            tmp = SETTINGS_FILE.with_suffix(".json.tmp")
            try:
                tmp.write_text(json.dumps(dict(self._settings), ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, SETTINGS_FILE)
                self._unsaved = False
                return True
            except OSError as e:
                log.warning("settings.json not saved: %s", e)
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
        self._unsaved = True
        self._bus.status("Настройки", SETTINGS_UNSAVED, False)
        return False

    def set_key(self, key, provider="openai"):
        key = (key or "").strip()
        if not key or provider not in KEY_ENVS:
            return {"ok": False}
        try:
            lt.save_api_key(key, KEY_ENVS[provider])
        except ValueError as e:  # the messages never contain the key
            return {"ok": False, "error": str(e)}
        except OSError as e:
            log.warning(".env not written: %s", e)
            return {"ok": False, "error": f"Ключ не записан на диск: {e.strerror or type(e).__name__}."}
        return {"ok": True, "notice": self._notice(), "engine": self._settings["engine"],
                "settings": self._settings}

    def _has_engine_key(self):
        return bool(lt.load_api_key(KEY_ENVS[self._settings["engine"]]))

    def _provider(self):
        """Who synthesizes my voice: Soniox, Cartesia or Inworld in the Soniox engine, Cartesia in the OpenAI one."""
        s = self._settings
        if s["engine"] != "soniox":
            return "cartesia"
        return lt.voice_provider(argparse.Namespace(voice_provider=s.get("voice_provider")))

    def _clone_provider(self):
        """Where a new clone of my voice is made: at Cartesia while it is the automatic choice and has a key."""
        provider = self._provider()
        if provider == "soniox" and self._settings.get("provider_auto", True) and lt.load_api_key(KEY_ENVS["cartesia"]):
            return "cartesia"
        return provider

    def check_connection(self):
        """Settings → «Проверить связь»: where the VPN exits and how fast the speech services answer."""
        try:
            proxy = self._proxy()
        except lt.Fatal as e:
            return {"ok": False, "error": str(e)}
        keys = {name: lt.load_api_key(env) for name, env in KEY_ENVS.items()}
        if not (self._running() or self._restarting):  # a running call keeps the servers it started on
            self._use_region()
        eu = self._region == "eu"
        name = "Soniox EU" if eu else "Soniox"
        probes = [("soniox_stt", f"{name} (распознавание)", soniox_engine.STT_URL, None),
                  ("soniox_tts", f"{name} (голос)", soniox_engine.TTS_URL, None)]
        if not eu:  # the EU region is what the two above already measure
            probes.append(("soniox_eu", "Soniox EU", netcheck.SONIOX_EU_STT, None))
        optional = [("openai", "OpenAI", lt.URL, {"Authorization": f"Bearer {keys['openai']}"}),
                    ("cartesia", "Cartesia", voice_clone.TTS_URL, {"X-API-Key": keys["cartesia"]}),
                    ("inworld", "Inworld", netcheck.INWORLD_TTS, {"Authorization": f"Basic {keys['inworld']}"})]
        probes += [probe for probe in optional if keys[probe[0]]]  # only services I have a key for
        result = asyncio.run(netcheck.check(probes, proxy))
        result["hint"] = netcheck.hint(result)
        log.info("connection check: %s", {p["id"]: (p["ping_ms"], p["error"]) for p in result["probes"]})
        return {"ok": True, **result}

    def _notice(self):
        """The automatic choices (engine, then voice provider) made now, as one message; None when nothing changed."""
        notices = [notice for notice in (self._auto_engine(), self._auto_provider()) if notice]
        return " ".join(notices) or None

    def _auto_provider(self):
        """Cartesia speaks for me once its key is there: the fastest voice that stays close to mine.

        Never mid-call (the restart would change the voice the call hears) and never while «мой клон» is picked but
        Cartesia has no clone of me: the call would fall back to a stock voice. A provider picked by hand stays.
        Returns a notice."""
        if self._running() or self._restarting:
            return None
        s = self._settings
        if (s["engine"] != "soniox" or not s.get("provider_auto", True) or s.get("voice_provider") != "soniox"
                or not lt.load_api_key(KEY_ENVS["cartesia"])):
            return None
        clone = s.get("cartesia_voice_id")
        if s["voice"] == "clone" and not clone:
            return None
        patch = {"voice_provider": "cartesia"}
        if clone and s.get("clone_auto_off"):
            patch.update(voice="clone", clone_auto_off=False)
        self.save_settings(patch)
        log.info("voice provider switched automatically to cartesia")
        return "Голос теперь синтезирует Cartesia — самый быстрый и похожий на вас."

    def _auto_engine(self):
        """Use the engine that has a key: OpenAI until a Soniox key appears, then Soniox with the voice clone.

        An engine picked by hand stays, unless it has no key while the other one has. Never mid-call (a key
        saved during the call): the restart would change the voice the call hears. Returns a notice."""
        if self._running() or self._restarting:  # mid-restart: the old engine is gone, the new one not started yet
            return None
        s = self._settings
        has = {name: bool(lt.load_api_key(KEY_ENVS[name])) for name in ("soniox", "openai")}
        if not has[s["engine"]]:
            other = "openai" if s["engine"] == "soniox" else "soniox"
            if not has[other]:
                return None
            target = other
        elif s["engine"] == "openai" and s.get("engine_auto", True) and has["soniox"]:
            target = "soniox"  # the Soniox key arrived: switch to the engine with the cloned voice
        else:
            return None
        self.save_settings({"engine": target, "engine_auto": True})
        log.info("engine switched automatically to %s", target)
        if target == "openai":
            return ("Ключа Soniox пока нет — перевожу через OpenAI: голос перевода подстраивается "
                    "под вашу интонацию. Клон голоса заработает с ключом Soniox.")
        if s.get("soniox_voice_id"):
            return "Включён Soniox: собеседник слышит ваш клонированный голос."
        return "Включён Soniox. Запишите свой голос: 🔊 → «Записать голос» → «Создать клон»."

    # --- voice: sample, clone, preview ----------------------------------------

    def _sample_path(self):
        found = sorted(lt.APP_DIR.glob(SAMPLE_FILE.name + ".*"))
        return found[0] if found else None

    def start_recording(self, mic=None):
        """Start recording my voice for cloning at the microphone's own rate (the page's timer stops it; REC_MAX
        is the most it keeps). `mic`: the one picked in the recorder, else the call's."""
        self.cancel_recording()
        name = mic if mic is not None else self._settings["mic"]
        audio = bytearray()
        try:
            rate = lt.native_rate(name)
            stream = lt.open_input(name, self._rec_feed(audio, rate), samplerate=rate)
            try:
                stream.start()
            except Exception:
                stream.close()
                raise
        except Exception as e:  # mic unplugged, or Windows privacy settings block it
            log.warning("recording failed: %s", e)
            return {"ok": False, "error": f"Микрофон недоступен: {e}"}
        with self._rec_lock:
            self._recording = (stream, audio, rate)
        return {"ok": True, "rate": rate, "max": REC_MAX, "min": REC_MIN}

    def _rec_feed(self, audio, rate):
        limit = int(REC_MAX * rate) * 2

        def feed(data, *_):
            chunk = bytes(data)
            audio.extend(chunk[:max(0, limit - len(audio))])
            samples = np.frombuffer(chunk[:len(chunk) // 2 * 2], "<i2").astype(np.float32)
            if samples.size:
                self._rec_level = min(1.0, float(np.sqrt(np.mean(samples ** 2))) / 6000)
        return feed

    def _take_recording(self):
        with self._rec_lock:
            recording, self._recording, self._rec_level = self._recording, None, 0.0
        if recording:
            try:
                recording[0].close()
            except Exception as e:
                log.warning("closing the recording failed: %s", e)
        return recording

    def stop_recording(self):
        """Stop recording, trim and level it, keep it as my voice sample; returns the checks (verdict, speech_seconds)."""
        recording = self._take_recording()
        if recording is None:
            return {"ok": False, "error": "Запись не идёт."}
        _, audio, rate = recording
        pcm = bytes(audio[:len(audio) // 2 * 2])
        if not pcm:
            return {"ok": False, "error": "Микрофон не дал звука."}
        prepared, report = (getattr(speech_audio, "prepare_sample", None) or checked_sample)(pcm, rate)
        saved = report["verdict"] != "short" and report["speech_seconds"] > 0  # no speech found: the old sample stays
        if saved:
            self._keep_sample(prepared, rate)
        return {"ok": True, "seconds": round(len(pcm) / 2 / rate, 1), "saved": saved, **report}

    def cancel_recording(self):
        """Throw away a recording in progress (the recorder was closed)."""
        self._take_recording()

    def _drop_samples(self):
        for old in lt.APP_DIR.glob(SAMPLE_FILE.name + ".*"):
            old.unlink()

    def _keep_sample(self, pcm, rate):
        self._drop_samples()
        with wave.open(str(SAMPLE_FILE.with_suffix(".wav")), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(pcm)

    def open_sound_settings(self):
        """Windows' sound settings: the microphone's input level is there."""
        os.startfile(SOUND_SETTINGS)

    def import_sample(self):
        """Pick an existing recording of my voice (wav/mp3/m4a/ogg/flac)."""
        picked = self._window.create_file_dialog(
            webview.FileDialog.OPEN, file_types=("Аудио (*.wav;*.mp3;*.m4a;*.ogg;*.flac;*.webm)",))
        if not picked:
            return {"ok": False}
        source = Path(picked[0] if not isinstance(picked, str) else picked)
        if not source.suffix:  # kept as voice_sample.<ext>: without one the sample would never be found again
            return self._import_failed("у файла нет расширения (.wav, .mp3, …)")
        tmp = SAMPLE_FILE.with_name(SAMPLE_FILE.name + "-import")  # not voice_sample.*: _drop_samples leaves it
        try:  # read and write the new one first: the sample from before goes only when its replacement is safe
            tmp.write_bytes(source.read_bytes())
            self._drop_samples()
            os.replace(tmp, SAMPLE_FILE.with_suffix(source.suffix.lower()))
        except OSError as e:
            tmp.unlink(missing_ok=True)
            return self._import_failed(e.strerror or str(e))
        return {"ok": True, "name": source.name}

    def _import_failed(self, why):
        error = f"Файл не подошёл: {why}. Прежний образец голоса остался."
        self._bus.status("Голос", error, False)
        return {"ok": False, "error": error}

    def create_clone(self):
        """Upload the sample to the current voice provider and wait until the clone is ready."""
        sample = self._sample_path()
        if sample is None:
            return {"ok": False, "error": "Сначала запиши голос или выбери файл."}
        provider = self._clone_provider()
        result = self._clone_at(provider, sample)
        home = self._provider()
        if result["ok"] or provider == home or not lt.load_api_key(KEY_ENVS[home]):
            return result
        # the clone the automatic choice sent to Cartesia failed (a plan without cloning, no credit): make it where
        # I speak now; nothing was saved by the failed one, so the provider and the voice stay as they were
        log.warning("clone at %s failed, trying %s", provider, home)
        first, names = result["error"], lt.PROVIDER_NAMES
        result = self._clone_at(home, sample)
        if result["ok"]:
            result["note"] = f"Клон в {names[provider]} не получился ({first}) — голос создан в {names[home]}."
        else:
            result["error"] = (f"Клон в {names[provider]} не получился ({first}). "
                               f"В {names[home]} тоже: {result['error']}")
        return result

    def _clone_at(self, provider, sample):
        """Make my clone at `provider` from the sample file; on success it becomes the voice."""
        key = lt.load_api_key(KEY_ENVS[provider])
        if not key:
            return {"ok": False, "error": f"Нужен ключ {lt.PROVIDER_NAMES[provider]} (⚙ Настройки)."}
        field = f"{provider}_voice_id"
        old = self._settings.get(field)
        delete = voice_clone.delete_clone if provider == "cartesia" else voice_module(provider).delete_voice
        try:
            proxy = self._proxy()
            if provider == "inworld":  # ready right away, nothing to wait for
                voice_id = voice_module(provider).create_voice(
                    key, clip_wav(sample.read_bytes(), INWORLD_MAX), proxy, sample.name)
            elif provider == "soniox":
                voice_id = soniox_engine.create_voice(key, sample.read_bytes(), proxy, sample.name)
                log.info("voice clone uploaded (soniox): %s", voice_id)
                status = "processing"
                try:
                    for _ in range(40):  # usually ready within seconds
                        status = soniox_engine.voice_status(key, voice_id, proxy)
                        if status != "processing":
                            break
                        time.sleep(1.5)
                except Exception:  # a failed check (a VPN hiccup) leaves the upload on the account too
                    self._delete_clone(delete, key, voice_id, proxy)
                    raise
                if status != "ready":
                    self._delete_clone(delete, key, voice_id, proxy)  # don't leave an unusable copy behind
                    return {"ok": False, "error": f"Soniox не подготовил голос: {status}"}
            else:
                voice_id = voice_clone.create_clone(key, sample.read_bytes(), "Live Translator",
                                                    self._settings["me_lang"], proxy)
        except (voice_clone.CloneError, lt.Fatal) as e:
            log.warning("clone failed: %s", e)
            return {"ok": False, "error": str(e)}
        log.info("voice clone created (%s): %s", provider, voice_id)
        patch = {field: voice_id, "voice": "clone"}
        if self._settings["engine"] == "soniox":
            patch["voice_provider"] = provider  # the clone is spoken by the provider that holds it
        replaced = bool(old and old != voice_id)  # the provider keeps a copy of my voice for every clone made
        with self._lifecycle:  # a restart must not take the lock between the save and the queueing
            moving = self.save_settings(patch)["pending"]  # a running call moves to the new clone in its next pause
            if replaced and moving:  # ...until then it still speaks with the old one
                self._stale_clones.append((delete, key, old, proxy))
        if replaced and not moving:
            self._delete_clone(delete, key, old, proxy)
        return {"ok": True, "provider": provider}

    def _delete_clone(self, delete, key, voice_id, proxy):
        try:
            delete(key, voice_id, proxy)
            log.info("old voice clone deleted: %s", voice_id)
        except Exception as e:  # cleanup only: never a reason to fail what called it
            log.warning("could not delete voice clone %s: %s", voice_id, e)

    def _drop_stale_clones(self):
        """(Under _lifecycle) The engine has moved on: delete the clones it was speaking with when they were
        replaced. In a thread, the provider is a network call away."""
        stale, self._stale_clones = self._stale_clones, []
        if stale:
            self._reaper = threading.Thread(target=lambda: [self._delete_clone(*clone) for clone in stale],
                                            daemon=True)
            self._reaper.start()

    def preview_voice(self, voice=None):
        """Say a test phrase in the chosen voice into the headphones (never into the call)."""
        try:
            return self._preview(voice)
        except Exception as e:  # network, key or audio device: the button must come back either way
            log.warning("voice preview failed: %s", e)
            return {"ok": False, "error": str(e) or type(e).__name__}

    def _preview(self, voice):
        s = self._settings
        self._headphones()  # refused before anything is synthesized
        proxy = self._proxy()
        provider = self._provider()
        key = lt.load_api_key(KEY_ENVS[provider])
        if not key:
            return {"ok": False, "error": f"Нужен ключ {lt.PROVIDER_NAMES[provider]} (⚙ Настройки)."}
        openai = s["engine"] != "soniox"
        if voice is None:
            clone = s["voice"] == "clone" or openai  # the OpenAI engine: only a Cartesia clone
            voice = s[f"{provider}_voice_id"] if clone else s[BUILTIN_FIELDS[provider]]
            if not voice and not clone and provider == "cartesia":  # what the call speaks with (_make_voice)
                voice = voice_module(provider).default_voice(key, proxy)
        if not voice:
            return {"ok": False, "error": f"Выберите голос {lt.PROVIDER_NAMES[provider]} или запишите свой (🔊)."}
        speed = float(s["speed"])
        if openai:  # its clone is voice_clone.CloneVoice, which has no speed
            pcm = asyncio.run(voice_clone.speak_once(key, voice, s["peer_lang"], PREVIEW_TEXT, proxy))
        elif provider == "cartesia":  # the call's CartesiaVoice: its wire format and speed
            # one whole phrase: what the delivery decides (clause or sentence, trimmed seams, pace) shows only in a call
            cartesia = voice_module(provider).CartesiaVoice(key, voice, s["peer_lang"], None, proxy, None, speed=speed)
            pcm = asyncio.run(soniox_engine.render_once(cartesia, PREVIEW_TEXT))
        else:
            extra = {"model": s["inworld_model"]} if provider == "inworld" else {}
            pcm = resolved(voice_module(provider).speak_once(key, voice, s["peer_lang"], PREVIEW_TEXT, proxy,
                                                             speed=speed, **extra))
        player = lt.open_headphones(s["listen"])  # picked again: a device plugged in meanwhile moves the indices
        player.gain = float(s["volume"])
        player.feed(pcm)
        with player.stream:
            time.sleep(len(pcm) / 2 / lt.RATE + 0.4)
        return {"ok": True}

    def _headphones(self):
        """The preview's device in the current device list: my headphones, the call must never hear a preview."""
        device = lt.pick_device(self._settings["listen"], "output")
        name = lt.device_name(device)
        if lt.is_cable(name):
            raise lt.Fatal(f"Прослушивание звучит только в наушниках, а выбран «{name}». "
                           "Источник звука → «Звук компьютера» → выберите наушники.")
        return device

    def list_voices(self):
        """Built-in voices of the current voice provider for the voice picker: [{name, gender, description, id?}]."""
        provider = self._provider()
        key = lt.load_api_key(KEY_ENVS[provider])
        if not key:
            return {"ok": False, "error": f"Нужен ключ {lt.PROVIDER_NAMES[provider]} (⚙ Настройки)."}
        try:
            return {"ok": True, "provider": provider, "voices": voice_module(provider).list_voices(key, self._proxy())}
        except (voice_clone.CloneError, lt.Fatal) as e:
            return {"ok": False, "error": str(e)}

    # --- engine -------------------------------------------------------------

    def _args(self):
        s = self._settings
        provider = self._provider()
        return argparse.Namespace(
            lang=s["peer_lang"], their_lang=s["me_lang"], inp=s["mic"], out=s["cable"],
            listen=s["listen"], no_listen=not s["listen_on"], no_me=not s["me_on"],
            monitor=s["monitor"], monitor_device=None,
            proxy=self._cli.proxy or s["proxy"] or None,
            passthrough=False,  # never from the window: the call would hear my Russian
            engine=s["engine"], voice=s["voice"], speed=float(s["speed"]),
            voice_delay=s["voice_delay"], keywords=s["keywords"], context=s["context"], diarize=s["diarize"],
            voice_provider=provider, inworld_model=s["inworld_model"],
            voice_name=s[BUILTIN_FIELDS[provider]], voice_id=s[f"{provider}_voice_id"],
            speed_boost=bool(s["speed_boost"]), trim_silence=bool(s["trim_silence"]),
            instant_phrases=bool(s["instant_phrases"]), auto_finalize=bool(s["auto_finalize"]),
            delivery=s["delivery"] if s["delivery"] in lt.DELIVERIES else DEFAULTS["delivery"],
            match_rate=bool(s["match_rate"]))

    def _running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        with self._lifecycle:
            log.info("start requested (running=%s)", self._running())
            if self._running() and self._started is not None:
                return {"ok": True, "started": self._started}
            if self._running():  # stopped, but the last call is still closing its devices
                self._thread.join(timeout=STOP_WAIT)
                if self._running():
                    return {"ok": False, "error": "stopping"}
            notice = self._notice()
            if not self._has_engine_key():
                return {"ok": False, "error": "no_key"}
            devices = lt.query_devices()
            if not has_cable(devices):
                return {"ok": False, "error": "no_cable"}
            self._adopt_cable(devices)
            self._bus.record = []
            self._bus.t0 = time.monotonic()
            self._paused = False
            self._started = time.time()
            self._start_engine()
            result = {"ok": True, "started": self._started, "engine": self._settings["engine"], "notice": notice,
                      "settings": self._settings}
        self._auto_overlay()
        return result

    def _start_engine(self):
        self._use_region()
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
        self._drop_stale_clones()

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
            lt.close_loop(loop)
            if task is self._task:
                self._bus.emit(type="running", value=False)

    def _stop_engine(self):
        task, self._task = self._task, None  # detached first, so its thread won't report a stop itself
        if self._running():
            if task is not None:  # None: an earlier stop cancelled it and the engine is still closing its devices
                try:
                    self._loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:  # loop already closed
                    pass
            self._thread.join(timeout=STOP_WAIT)
        self._engine = None

    def stop(self):
        with self._lifecycle:  # a second stop (double click, engine error + button) finds nothing to save
            log.info("stop requested (running=%s)", self._running())
            self._restart_pending = False  # the next call starts with the saved settings anyway
            self._stop_engine()
            self._drop_stale_clones()
            started, self._started = self._started, None
            record = self._save_record(started)
            self._bus.emit(type="running", value=False)
            return record

    def _save_record(self, started):
        if not started:
            return None
        duration = time.time() - started
        s = self._settings
        channels = int(s["me_on"]) + int(s["listen_on"])
        plan = self._provider() if s["engine"] == "soniox" else s["engine"]  # the Soniox engine pays for its voice
        price = PRICE_PER_MIN.get(plan, PRICE_PER_MIN["openai"])
        voiced = s["me_on"] and s["voice"] not in ("off", "model")
        s["usage_seconds"] = s.get("usage_seconds", 0) + duration * channels
        s["usage_cost"] = s.get("usage_cost", 0) + duration / 60 * (price["channel"] * channels + price["voice"] * voiced)
        self._write_settings()
        lines = compose_transcript(self._bus.record)
        if not lines:
            return None
        RECORDS_DIR.mkdir(exist_ok=True)
        start = datetime.datetime.fromtimestamp(started)
        path = RECORDS_DIR / f"{start:%Y-%m-%d_%H-%M-%S}.txt"
        header = [f"Live Translator — {start:%d.%m.%Y %H:%M}", f"Длительность: {hms(duration)}", ""]
        path.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
        if lt.load_api_key():
            self._pending_path(path.name).touch()  # closing the window cuts the notes off: the next launch makes them
            self._notes_later(path.name)
        return path.name

    # --- AI meeting notes -------------------------------------------------------

    def _notes_path(self, name):
        return RECORDS_DIR / (Path(name).stem + ".json")

    def _pending_path(self, name):
        return RECORDS_DIR / (Path(name).stem + NOTES_PENDING)

    def _notes_later(self, name):
        """A daemon: a closed window never waits for the notes (the process would keep the hotkeys and the exe)."""
        threading.Thread(target=self._auto_notes, args=(name,), daemon=True).start()

    def _resume_notes(self):
        """(Launch) AI notes that closing the window cut off last time."""
        for marker in RECORDS_DIR.glob("*" + NOTES_PENDING):
            name = marker.name[:-len(NOTES_PENDING)] + ".txt"
            if lt.load_api_key() and (RECORDS_DIR / name).exists():
                self._notes_later(name)
            else:
                marker.unlink(missing_ok=True)

    def _make_notes(self, name):
        key = lt.load_api_key()
        if not key:
            raise voice_clone.CloneError("Для протокола нужен ключ OpenAI (⚙ Настройки).")
        text = (RECORDS_DIR / Path(name).name).read_text(encoding="utf-8")
        notes = meeting_notes.summarize(key, text, self._proxy())
        self._notes_path(name).write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
        return notes

    def _auto_notes(self, name):
        try:
            notes = self._make_notes(name)
            self._bus.emit(type="notes", name=name, title=notes.get("title", ""))
        except (voice_clone.CloneError, lt.Fatal, OSError, ValueError) as e:
            log.warning("meeting notes failed: %s", e)
            self._bus.emit(type="notes_error", text=str(e))
        finally:
            self._pending_path(name).unlink(missing_ok=True)

    def get_record(self, name):
        path = RECORDS_DIR / Path(name).name
        notes_path = self._notes_path(name)
        try:
            notes = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.exists() else None
        except (OSError, ValueError):  # half-written or damaged: the transcript still opens, the notes can be remade
            log.warning("notes file %s is unreadable", notes_path.name)
            notes = None
        if notes is not None and not isinstance(notes, dict):
            notes = None
        return {"name": path.name, "text": path.read_text(encoding="utf-8") if path.exists() else "",
                "notes": notes, "can_summarize": bool(lt.load_api_key())}

    def save_record(self, name, body):
        """Save my edits of a transcript (header lines are kept), e.g. before regenerating the notes."""
        path = RECORDS_DIR / Path(name).name
        if not path.exists():
            return False
        header = path.read_text(encoding="utf-8").splitlines()[:3]
        path.write_text("\n".join(header + body.strip().splitlines()) + "\n", encoding="utf-8")
        return True

    def summarize_record(self, name):
        try:
            return {"ok": True, "notes": self._make_notes(name)}
        except (voice_clone.CloneError, lt.Fatal, OSError, ValueError) as e:
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

    def _on_done_hotkey(self):
        engine = self._engine
        if engine:
            engine.finish_turn()

    def _on_hide_hotkey(self):
        """Panic switch: every window of the app vanishes (or comes back) at once, e.g. when asked to share the screen."""
        self._hidden = not self._hidden
        for title in (MAIN_TITLE, OVERLAY_TITLE):
            for hwnd in winstealth.find_windows(title):
                winstealth.set_visible(hwnd, not self._hidden)

    def log_js(self, message):
        log.error("ui: %s", message)

    def poll(self, since):
        me, them = self._bus.levels if self._running() else (0.0, 0.0)
        return {"events": self._bus.since(since), "me": me, "them": them,
                "running": self._running() or self._restarting, "muted": self._muted, "paused": self._paused,
                "rec": round(self._rec_level, 3) if self._recording else 0.0}

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
        with self._overlay_lock:
            if self._overlay is not None:
                self._overlay.destroy()
                return False
            self._open_overlay()
            return True

    def _auto_overlay(self):
        """The subtitles open with a fresh call (start returns early for a running one), never twice."""
        if not (self._settings.get("overlay_auto") and self._window):
            return
        with self._overlay_lock:
            if self._overlay is not None:
                return
            try:
                self._open_overlay()
            except Exception:
                log.exception("the subtitles did not open")
                return
        self._bus.emit(type="overlay", value=True)

    def _open_overlay(self):
        geom = self._settings.get("overlay_geom") or {}
        x, y = geom.get("x"), geom.get("y")
        if not on_screen(x, y, geom.get("w", 780), geom.get("h", 180)):
            x = y = None  # that monitor is gone: open centered instead of off-screen
        self._overlay = webview.create_window(
            OVERLAY_TITLE, url=str(UI_DIR / "overlay.html"), js_api=self,
            width=geom.get("w", 780), height=geom.get("h", 180), x=x, y=y,
            min_size=(360, 110), frameless=True, easy_drag=True, on_top=True, background_color="#161616",
            focus=False,  # never takes the keyboard from the call app
            hidden=True)  # shown by _dress_overlay once it is out of capture: no frame of it reaches a screen share
        self._overlay.events.closed += self._overlay_closed
        self._overlay.events.moved += self._overlay_moved
        self._overlay.events.resized += self._overlay_resized
        self._dress_overlay(self._overlay)

    def _overlay_moved(self, x, y):
        if x > -32000 and y > -32000:  # Windows moves minimized windows to -32000
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
        set_dark_title_bar(MAIN_TITLE)
        if self._settings.get("on_top"):
            self._window.on_top = True
        self._dress_main()

    def _opacity(self):
        try:
            return float(self._settings["overlay_opacity"])
        except (TypeError, ValueError):
            return DEFAULTS["overlay_opacity"]

    def _style_main(self, hwnds):
        for hwnd in hwnds:
            winstealth.hide_from_capture(hwnd, bool(self._settings["hide_from_capture"]))

    def _style_overlay(self, hwnds):
        for hwnd in hwnds:
            winstealth.set_tool_window(hwnd, True)  # no taskbar button, no Alt+Tab entry
            winstealth.hide_from_capture(hwnd, bool(self._settings["hide_from_capture"]))
            winstealth.set_opacity(hwnd, self._opacity())
            winstealth.set_click_through(hwnd, bool(self._settings["overlay_click_through"]))

    def _restyle(self):
        """The saved call-mode settings, applied to the windows that are open now."""
        self._style_main(winstealth.find_windows(MAIN_TITLE))
        self._style_overlay(winstealth.find_windows(OVERLAY_TITLE))

    def _wait_for_window(self, title):
        deadline = time.monotonic() + STEALTH_WAIT
        while True:
            hwnds = winstealth.find_windows(title)
            if hwnds or time.monotonic() >= deadline:
                return hwnds
            time.sleep(STEALTH_STEP)

    def _dress_main(self):
        def worker():
            try:
                self._style_main(self._wait_for_window(MAIN_TITLE))
            except Exception:
                log.exception("stealth flags of the main window")

        threading.Thread(target=worker, daemon=True).start()

    def _dress_overlay(self, window):
        """The subtitles are created hidden: they appear once the stealth flags are on (or without them, if the
        window cannot be found, so they are never lost)."""
        def worker():
            shown = False
            try:
                hwnds = self._wait_for_window(OVERLAY_TITLE)
                self._style_overlay(hwnds)
                if not self._hidden:
                    shown = any([winstealth.set_visible(hwnd, True) for hwnd in hwnds])
            except Exception:
                log.exception("stealth flags of the subtitles")
            if not shown and not self._hidden:
                try:
                    window.show()
                except Exception:
                    log.warning("the subtitles could not be shown", exc_info=True)

        threading.Thread(target=worker, daemon=True).start()

    def _shutdown(self):
        self.cancel_recording()
        self.stop()
        self.close_overlay()

    def _exit(self):
        """(After the window closed) The call is saved and the process ends at once: a windowless one would keep
        the hotkeys (a new launch could not get them) and lock the exe for install.bat. JS calls still running
        (a clone upload, a connection check) end with it; unfinished AI notes are made at the next launch."""
        try:
            self.stop()  # waits for the window's own stop, or makes it
            self._write_settings()  # e.g. where the floating subtitles were
            if self._reaper:  # a replaced voice clone still being deleted at the provider
                self._reaper.join(CLONE_DELETE_WAIT)
        finally:
            logging.shutdown()
            os._exit(0)


def on_screen(x, y, width, height):
    """Whether a window at x, y would be visible on one of the connected monitors."""
    if x is None or y is None:
        return False
    for screen in getattr(webview, "screens", None) or ():
        if (x < screen.x + screen.width - 40 and x + width > screen.x + 40
                and screen.y <= y < screen.y + screen.height - 40):
            return True
    return False


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
    api._resume_notes()
    window = webview.create_window(
        MAIN_TITLE, url=str(UI_DIR / "index.html"), js_api=api,
        width=1240, height=780, min_size=(900, 560), background_color="#1B1B1B")
    api._window = window
    window.events.shown += api._on_shown
    window.events.closed += api._shutdown
    webview.start(private_mode=True)
    api._exit()


if __name__ == "__main__":
    main()
