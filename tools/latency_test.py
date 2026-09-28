"""
Real end-to-end latency check for the Soniox engine (needs a Soniox key).

A Russian phrase spoken by the Windows voice "Microsoft Irina" (or your own recording, --wav) is
streamed in real time into Soniox STT + translation, and the English text goes into the TTS voice
(Soniox: my clone or a built-in voice; or Inworld), exactly like during a call. Prints how long the
other person waits to hear English, a table per translated clause, and saves what they would hear
to latency_test_en.wav.

  py -3 tools/latency_test.py                  # key/voice from the installed app (or .env here)
  py -3 tools/latency_test.py --voice Adrian   # force a built-in voice
  py -3 tools/latency_test.py --text "Своя фраза для проверки."
  py -3 tools/latency_test.py --wav me.wav     # my own recorded speech (any PCM WAV)
  py -3 tools/latency_test.py --repeat 10      # medians over 10 runs
  py -3 tools/latency_test.py --provider inworld --model inworld-tts-2
  py -3 tools/latency_test.py --region eu      # Soniox EU endpoints (needs a key of an EU project)
  py -3 tools/latency_test.py --done           # "I finished" (Ctrl+Alt+Space) 100 ms after the phrase
  py -3 tools/latency_test.py --engine openai  # gpt-realtime-translate with the model's own voice
"""
import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

import live_translator as lt  # noqa: E402
import netcheck  # noqa: E402
import soniox_engine as se  # noqa: E402

INSTALLED = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Live Translator"
PHRASE = ("Здравствуйте! Меня зовут Сурен, я Python-разработчик, у меня больше семи лет опыта "
          "в бэкенде и инфраструктуре.")
FRAME = lt.BLOCK * 2  # 20 ms of PCM16
AUDIBLE = 300  # |sample| above this is heard; TTS streams start and end with near-silence
DONE_AFTER = 0.1  # --done: seconds after the end of the phrase
# (key, label, what it is measured from); "wait", "lead", "tail" are per clause, in ms
METRICS = [
    ("first_transcript", "Первое слово распознано", "от начала речи"),
    ("first_translation", "Первое слово перевода", "от начала речи"),
    ("first_audio", "Первый звук синтеза", "от начала речи"),
    ("first_audible", "Собеседник слышит английский", "от начала речи"),
    ("last_word", "Последнее английское слово", "после конца русской фразы"),
    ("wait", "text_end → слышимый звук", "медиана по кускам"),
    ("lead", "Тишина в начале куска", "медиана по кускам"),
    ("tail", "Тишина в конце куска", "медиана по кускам"),
]


def find_key(env):
    key = lt.load_api_key(env)
    installed_env = INSTALLED / ".env"
    if not key and installed_env.exists():
        for line in installed_env.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == env:
                key = value.strip().strip('"').strip("'")
    return key


def installed_settings():
    try:
        return json.loads((INSTALLED / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def synthesize(text):
    """Russian speech from Windows TTS as PCM16 mono 24 kHz."""
    tmp = Path(tempfile.gettempdir())
    wav, txt = tmp / "latency_test_ru.wav", tmp / "latency_test_ru.txt"
    txt.write_text(text, encoding="utf-8")  # a file, not stdin: the console code page would garble Cyrillic
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ru-RU' } | Select-Object -First 1;"
        "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) } else { exit 3 };"
        "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000, 'Sixteen', 'Mono');"
        f"$s.SetOutputToWaveFile('{wav}', $fmt);"
        f"$s.Speak([IO.File]::ReadAllText('{txt}', [Text.Encoding]::UTF8)); $s.Dispose()"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True)
    if result.returncode == 3:
        sys.exit("Нет русского голоса Windows (Параметры → Время и язык → Речь → добавить голос).")
    return read_wav(wav)


def read_wav(path):
    """Any PCM WAV -> mono PCM16 at 24 kHz: channels averaged, resampled linearly."""
    with wave.open(str(path)) as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    if (rate, width, channels) == (lt.RATE, 2, 1):
        return raw
    if width == 1:
        samples = (np.frombuffer(raw, np.uint8) - 128.0) / 128
    elif width == 3:  # 24-bit: pad each sample to 32 bits
        padded = np.zeros((len(raw) // 3, 4), np.uint8)
        padded[:, 1:] = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        samples = padded.view("<i4").ravel() / 2 ** 31
    else:
        samples = np.frombuffer(raw, {2: "<i2", 4: "<i4"}[width]) / 2 ** (8 * width - 1)
    mono = samples.reshape(-1, channels).mean(axis=1)
    if rate != lt.RATE:
        count = int(round(len(mono) * lt.RATE / rate))
        mono = np.interp(np.arange(count) * rate / lt.RATE, np.arange(len(mono)), mono)
    return (np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes()


def loud_samples(pcm, threshold=AUDIBLE):
    return np.flatnonzero(np.abs(np.frombuffer(pcm, "<i2").astype(np.int32)) > threshold)


def silence(pcm):
    """(leading, trailing) near-silence of a clip, seconds."""
    loud, total = loud_samples(pcm), len(pcm) // 2
    if not loud.size:
        return total / lt.RATE, 0.0
    return loud[0] / lt.RATE, (total - 1 - loud[-1]) / lt.RATE


class Timeline:
    """The voice's trace hook plus a simulated call player: what the other person hears, per clause.

    Chunks play back to back like in the real player; a clause is one TTS stream (its sid)."""

    def __init__(self):
        self.voice = None     # the TTS voice: its order[0] is the stream being played
        self.clauses = {}     # sid -> timings, text and the audio played for it
        self.events = []      # (time, description) for --log
        self.playhead = 0.0
        self.audio = bytearray()
        self.first_audio = self.first_audible = self.last_audible = None
        self._playing = None

    def backlog(self):
        """Seconds of speech queued in the simulated player: what Player.buffered reports in the app."""
        return max(0.0, self.playhead - time.monotonic())

    def trace(self, event, sid, **info):
        now = time.monotonic()
        text = info.get("text", "")
        self.events.append((now, f"{event:<11} {sid[:8]} {text!r}{' +text_end' if info.get('end') else ''}"))
        if event == "text":
            clause = self.clauses.setdefault(sid, {"text": "", "final": now, "end": None, "first_audio": None,
                                                   "backlog": None, "audible": None, "pcm": bytearray()})
            clause["text"] += text
            if info.get("end"):
                clause["end"] = now
        elif event == "first_audio" and sid in self.clauses:
            self.clauses[sid].update(first_audio=now, backlog=self.backlog())

    def play(self, pcm):
        now = time.monotonic()
        start = max(self.playhead, now)
        order = getattr(self.voice, "order", None)
        if order:
            self._playing = order[0]
        clause = self.clauses.get(self._playing)
        loud = loud_samples(pcm)
        if self.first_audio is None:
            self.first_audio = now
        if loud.size:
            heard = start + loud[0] / lt.RATE
            if self.first_audible is None:
                self.first_audible = heard
            if clause is not None and clause["audible"] is None:
                clause["audible"] = heard
            self.last_audible = start + (loud[-1] + 1) / lt.RATE
        if clause is not None:
            clause["pcm"] += pcm
        self.playhead = start + len(pcm) / 2 / lt.RATE
        self.audio += pcm

    def rows(self, origin):
        """One row per clause, times in seconds from `origin` (None: didn't happen)."""
        def rel(t):
            return None if t is None else t - origin

        rows = []
        for c in sorted(self.clauses.values(), key=lambda c: c["final"]):
            lead, tail = silence(bytes(c["pcm"])) if c["pcm"] else (None, None)
            wait = c["audible"] - c["end"] if c["audible"] is not None and c["end"] is not None else None
            rows.append({"text": c["text"].strip(), "final": rel(c["final"]), "end": rel(c["end"]),
                         "first_audio": rel(c["first_audio"]), "audible": rel(c["audible"]), "wait": wait,
                         "backlog": c["backlog"], "lead": lead, "tail": tail})
        return rows


class Probe(lt.Sink):
    def __init__(self, timeline):
        self.timeline = timeline
        self.marks, self.src, self.dst, self.statuses, self.notes = {}, "", "", [], []

    def caption(self, kind, label, text, speaker=None):
        now = time.monotonic()
        self.timeline.events.append((now, f"{kind:<11} {text!r}"))
        if kind.endswith("_src"):
            self.marks.setdefault("first_transcript", now)
            self.src += text
        else:
            self.marks.setdefault("first_translation", now)
            self.dst += text

    def status(self, label, text, ok):
        self.statuses.append((label, text, ok))

    def note(self, text):
        self.notes.append(text)


class Capture:
    """Stands in for the VB-Cable player: keeps what the other person would hear."""

    def __init__(self, play):
        self.feed, self.gain, self.busy = play, 1.0, False


def press_done(channel):
    """--done: what Ctrl+Alt+Space does in the app, if the STT channel has a finalizer."""
    finalizer = getattr(channel, "finalizer", None)
    if finalizer is None:
        return False
    finalizer.force()
    return True


def metrics(timeline, sink, begin, finish):
    def since(t, origin):
        return None if t is None else t - origin

    def median(key):
        values = [r[key] for r in timeline.rows(begin) if r[key] is not None]
        return statistics.median(values) if values else None

    return {"first_transcript": since(sink.marks.get("first_transcript"), begin),
            "first_translation": since(sink.marks.get("first_translation"), begin),
            "first_audio": since(timeline.first_audio, begin),
            "first_audible": since(timeline.first_audible, begin),
            "last_word": since(timeline.last_audible, finish),
            "wait": median("wait"), "lead": median("lead"), "tail": median("tail")}


def fmt(key, value):
    if value is None:
        return "—"
    return f"{value * 1000:.0f} мс" if key in ("wait", "lead", "tail") else f"{value:+.2f} с"


def medians(runs):
    """Median of every metric over the runs that measured it."""
    result = {}
    for key, _, _ in METRICS:
        values = [run[key] for run in runs if run.get(key) is not None]
        result[key] = statistics.median(values) if values else None
    return result


def print_metrics(result):
    for key, label, origin in METRICS:
        print(f"  {label:<32} {fmt(key, result[key]):>9}  {origin}")


def print_table(rows):
    def cell(value, width=7):
        return f"{'—' if value is None else f'{value:+.2f}':>{width}}"

    def ms(value):
        return "—" if value is None else f"{value * 1000:.0f}"

    print("   #   финал text_end 1-й чанк слышно end→звук backlog тишина н/к  кусок")
    for i, r in enumerate(rows, 1):
        backlog = "—" if r["backlog"] is None else f"{r['backlog']:.2f} с"
        print(f"  {i:>2} {cell(r['final'])} {cell(r['end'], 8)} {cell(r['first_audio'], 8)} {cell(r['audible'], 6)}"
              f" {ms(r['wait']):>5} мс {backlog:>7} {ms(r['lead']):>4}/{ms(r['tail']):<4} мс  {r['text'][:60]}")


def pick_voice(args, settings):
    if args.engine == "openai":
        return "голос модели OpenAI"
    if args.voice:
        return args.voice
    clone = settings.get("voice") == "clone"
    if args.provider == "inworld":
        import inworld_engine
        return (clone and settings.get("inworld_voice_id")) or settings.get("inworld_voice_name") \
            or inworld_engine.DEFAULT_VOICE
    return (clone and settings.get("soniox_voice_id")) or settings.get("voice_name") or se.DEFAULT_VOICE


def make_voice(args, keys, voice, proxy, sink, timeline):
    options = dict(speed=args.speed, backlog=timeline.backlog, speed_boost=not args.no_boost, trim=not args.no_trim)
    if args.provider == "inworld":
        import inworld_engine
        return inworld_engine.InworldVoice(keys["inworld"], voice, "en", timeline.play, proxy, sink,
                                           model=args.model or inworld_engine.DEFAULT_MODEL, **options)
    return se.SonioxVoice(keys["soniox"], voice, "en", timeline.play, proxy, sink, **options)


async def print_rtt(args, keys, proxy):
    """Websocket ping to what this run talks to: most of every clause's delay is two such round trips."""
    if args.engine == "openai":
        probes = [("OpenAI", lt.URL, {"Authorization": f"Bearer {keys['openai']}"})]
    else:
        probes = [("Soniox STT", se.STT_URL, None), ("Soniox TTS", se.TTS_URL, None)]
        if args.provider == "inworld":
            probes[1] = ("Inworld TTS", netcheck.INWORLD_TTS, {"Authorization": f"Basic {keys['inworld']}"})
    results = await asyncio.gather(*(netcheck.ws_rtt(url, proxy, headers) for _, url, headers in probes))
    parts = [f"{name}: ping {r['ping_ms']} мс, соединение {r['open_ms']} мс" if r["ping_ms"] is not None
             else f"{name}: {r['error']}" for (name, _, _), r in zip(probes, results)]
    print("Связь: " + " · ".join(parts))


async def run_once(args, keys, voice, proxy, speech, settings):
    openai = args.engine == "openai"
    loud = loud_samples(speech, 800)
    speech_begin, speech_finish = loud[0] / lt.RATE, loud[-1] / lt.RATE
    stream = speech + bytes(lt.RATE * 2 * 4)  # 4 s of silence after the phrase
    timeline = Timeline()
    sink, queue = Probe(timeline), asyncio.Queue()
    if openai:
        channel = lt.Channel("Я", "en", queue, [Capture(timeline.play)], "me")
        tasks, needed = [asyncio.create_task(lt.run_channel(channel, keys["openai"], proxy, sink))], 1
    else:
        channel = lt.Channel("Я", "en", queue, [], "me")
        tts = timeline.voice = make_voice(args, keys, voice, proxy, sink, timeline)
        tts.trace = timeline.trace
        context = se.build_context(settings.get("keywords") or [], settings.get("context") or "")
        tasks = [asyncio.create_task(tts.run()),
                 asyncio.create_task(se.run_stt_channel(channel, keys["soniox"], proxy, sink, "en", ["ru"],
                                                        context, tts))]
        needed = 2

    def check():  # a wrong key or an empty balance ends a task: say so instead of measuring silence
        for task in tasks:
            if task.done() and not task.cancelled() and task.exception():
                sys.exit(f"Ошибка: {task.exception()}")

    try:
        for _ in range(100):
            check()
            if len([s for s in sink.statuses if s[2]]) >= needed:
                break
            await asyncio.sleep(0.1)
        else:
            sys.exit(f"Не удалось подключиться: {sink.statuses or sink.notes}")
        print(f"Голос: {voice} · прокси: {lt.redact(proxy) if proxy else 'нет'} · "
              f"фраза {speech_finish - speech_begin:.1f} с")
        done_at = speech_finish + DONE_AFTER if args.done else None
        t0 = time.monotonic()
        for i in range(0, len(stream), FRAME):  # real-time pace, like a microphone
            if i % (FRAME * 50) == 0:
                check()
            if done_at is not None and i / 2 / lt.RATE >= done_at:
                done_at = None
                timeline.events.append((time.monotonic(), "--done      finalizer.force()"))
                if not press_done(channel):
                    print("  --done: у канала нет finalizer — быстрое завершение фраз ещё не подключено")
            await queue.put(stream[i:i + FRAME])
            delay = t0 + (i + FRAME) / 2 / lt.RATE - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        await asyncio.sleep(1.5)
        check()
    finally:
        for task in tasks:
            task.cancel()
        for fatal in sink.notes:
            print("  !", fatal)

    begin, finish = t0 + speech_begin, t0 + speech_finish
    result = metrics(timeline, sink, begin, finish)
    print(f"Распознано: {sink.src.strip()}")
    print(f"Перевод:    {sink.dst.strip()}")
    print_metrics(result)
    if timeline.clauses:
        print_table(timeline.rows(begin))
    if args.log:
        print("Порядок событий (от начала речи):")
        for t, what in sorted(timeline.events):
            print(f"  {t - begin:+7.2f}  {what}")
    return timeline, result


async def run(args):
    openai = args.engine == "openai"
    keys = {"soniox": find_key(se.KEY_ENV), "openai": find_key("OPENAI_API_KEY"),
            "inworld": find_key("INWORLD_API_KEY") if args.provider == "inworld" else None}
    if not keys["openai" if openai else "soniox"]:
        sys.exit(f"Нет ключа {'OpenAI' if openai else 'Soniox'}: вставьте его в программе (⚙ Настройки → Ключи).")
    if not openai and args.provider == "inworld" and not keys["inworld"]:
        sys.exit("Нет ключа Inworld: вставьте его в программе (⚙ Настройки → Расширенные → Ключи).")
    if args.region == "eu":
        se.STT_URL, se.TTS_URL = netcheck.SONIOX_EU_STT, netcheck.SONIOX_EU_TTS
    settings = installed_settings()
    if args.speed is None:
        args.speed = float(settings.get("speed", 1.1))
    voice = pick_voice(args, settings)
    proxy = lt.detect_proxy(args.proxy)
    speech = read_wav(args.wav) if args.wav else synthesize(args.text)
    if not loud_samples(speech, 800).size:
        sys.exit("В записи не слышно речи.")
    await print_rtt(args, keys, proxy)
    runs = []
    for n in range(args.repeat):
        if args.repeat > 1:
            print(f"\n— прогон {n + 1} из {args.repeat}")
        timeline, result = await run_once(args, keys, voice, proxy, speech, settings)
        runs.append(result)
        if n + 1 < args.repeat:
            await asyncio.sleep(1)
    if args.repeat > 1:
        print(f"\nМедианы по {args.repeat} прогонам:")
        print_metrics(medians(runs))
    if timeline.audio:
        out = Path(args.out).resolve()
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(lt.RATE)
            w.writeframes(bytes(timeline.audio))
        print(f"Что услышит собеседник: {out} ({len(timeline.audio) / 2 / lt.RATE:.1f} с)")


def build_parser():
    ap = argparse.ArgumentParser(description="Real latency check of the Soniox engine")
    ap.add_argument("--engine", choices=("soniox", "openai"), default="soniox")
    ap.add_argument("--text", default=PHRASE, help="Russian phrase to speak")
    ap.add_argument("--wav", help="my own recorded Russian speech (any PCM WAV) instead of the Windows voice")
    ap.add_argument("--voice", help="voice name or clone id (default: from the installed app)")
    ap.add_argument("--provider", choices=("soniox", "inworld"), default="soniox", help="who speaks the English")
    ap.add_argument("--model", help="Inworld TTS model (default: inworld-tts-2-flash)")
    ap.add_argument("--speed", type=float, help="voice speed (default: from the installed app, else 1.1)")
    ap.add_argument("--no-boost", action="store_true", help="no automatic speed-up when the voice falls behind")
    ap.add_argument("--no-trim", action="store_true", help="keep the TTS silence around every clause")
    ap.add_argument("--region", choices=("us", "eu"), default="us",
                    help="Soniox endpoints (eu needs a key of a Soniox project in the EU region)")
    ap.add_argument("--repeat", type=int, default=1, help="number of runs; medians are printed after several")
    ap.add_argument("--done", action="store_true",
                    help="press «я закончил» (the channel's finalizer) 100 ms after the phrase")
    ap.add_argument("--log", action="store_true", help="print STT tokens and TTS stream events in order")
    ap.add_argument("--proxy", help="proxy URL or 'none' (default: system proxy)")
    ap.add_argument("--out", default="latency_test_en.wav", help="where to save the English audio")
    return ap


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
