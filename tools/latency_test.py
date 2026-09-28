"""
Real end-to-end latency check for the Soniox engine (needs a Soniox key).

A Russian phrase spoken by the Windows voice "Microsoft Irina" is streamed in real time into Soniox
STT + translation, and the English text goes into Soniox TTS (my clone or a built-in voice), exactly
like during a call. Prints how long the other person waits to hear English and saves what they
would hear to latency_test_en.wav.

  py -3 tools/latency_test.py                  # key/voice from the installed app (or .env here)
  py -3 tools/latency_test.py --voice Adrian   # force a built-in voice
  py -3 tools/latency_test.py --text "Своя фраза для проверки."
"""
import argparse
import asyncio
import json
import os
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
import soniox_engine as se  # noqa: E402

INSTALLED = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Live Translator"
PHRASE = ("Здравствуйте! Меня зовут Сурен, я Python-разработчик, у меня больше семи лет опыта "
          "в бэкенде и инфраструктуре.")
FRAME = lt.BLOCK * 2  # 20 ms of PCM16


def find_key():
    key = lt.load_api_key(se.KEY_ENV)
    installed_env = INSTALLED / ".env"
    if not key and installed_env.exists():
        for line in installed_env.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == se.KEY_ENV:
                key = value.strip().strip('"').strip("'")
    return key


def installed_settings():
    try:
        return json.loads((INSTALLED / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def synthesize(text):
    """Russian speech from Windows TTS as PCM16 mono 24 kHz."""
    wav = Path(tempfile.gettempdir()) / "latency_test_ru.wav"
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ru-RU' } | Select-Object -First 1;"
        "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) } else { exit 3 };"
        "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000, 'Sixteen', 'Mono');"
        f"$s.SetOutputToWaveFile('{wav}', $fmt);"
        "$s.Speak([Console]::In.ReadToEnd()); $s.Dispose()"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", script], input=text.encode("utf-8"),
                            capture_output=True)
    if result.returncode == 3:
        sys.exit("Нет русского голоса Windows (Параметры → Время и язык → Речь → добавить голос).")
    with wave.open(str(wav)) as w:
        return w.readframes(w.getnframes())


class Probe(lt.Sink):
    def __init__(self):
        self.marks, self.src, self.dst, self.statuses, self.notes = {}, "", "", [], []

    def caption(self, kind, label, text, speaker=None):
        now = time.monotonic()
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


async def run(args):
    key = find_key()
    if not key:
        sys.exit("Нет ключа Soniox: вставьте его в программе (⚙ Настройки → Soniox) или в .env.")
    settings = installed_settings()
    voice = args.voice or (settings.get("soniox_voice_id") if settings.get("voice") == "clone"
                           else settings.get("voice_name")) or se.DEFAULT_VOICE
    proxy = lt.detect_proxy(args.proxy)
    speech = synthesize(args.text)
    samples = np.frombuffer(speech, "<i2")
    loud = np.nonzero(np.abs(samples) > 800)[0]
    speech_begin, speech_finish = loud[0] / lt.RATE, loud[-1] / lt.RATE
    stream = speech + bytes(lt.RATE * 2 * 4)  # 4 s of silence after the phrase

    sink, audio, marks = Probe(), bytearray(), {}

    def play(pcm):
        marks.setdefault("first_audio", time.monotonic())
        audio.extend(pcm)

    queue = asyncio.Queue()
    channel = lt.Channel("Я", "en", queue, [], "me")
    tts = se.SonioxVoice(key, voice, "en", play, proxy, sink)
    context = se.build_context(settings.get("keywords") or [], settings.get("context") or "")
    tasks = [asyncio.create_task(tts.run()),
             asyncio.create_task(se.run_stt_channel(channel, key, proxy, sink, "en", ["ru"], context, tts))]
    try:
        for _ in range(100):
            if len([s for s in sink.statuses if s[2]]) >= 2:
                break
            await asyncio.sleep(0.1)
        else:
            sys.exit(f"Не удалось подключиться к Soniox: {sink.statuses or sink.notes}")
        print(f"Голос: {voice} · прокси: {proxy or 'нет'} · фраза {speech_finish - speech_begin:.1f} с")
        t0 = time.monotonic()
        for i in range(0, len(stream), FRAME):  # real-time pace, like a microphone
            await queue.put(stream[i:i + FRAME])
            delay = t0 + (i + FRAME) / 2 / lt.RATE - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        await asyncio.sleep(1.5)
    finally:
        for task in tasks:
            task.cancel()
        for fatal in sink.notes:
            print("  !", fatal)

    begin, finish = t0 + speech_begin, t0 + speech_finish
    marks.update(sink.marks)

    def since(mark, origin):
        return f"{marks[mark] - origin:+.2f} с" if mark in marks else "—"

    print(f"Распознано: {sink.src.strip()}")
    print(f"Перевод:    {sink.dst.strip()}")
    print(f"Первое слово распознано:      {since('first_transcript', begin)} от начала речи")
    print(f"Первое слово перевода:        {since('first_translation', begin)} от начала речи")
    print(f"Собеседник слышит английский: {since('first_audio', begin)} от начала речи, "
          f"{since('first_audio', finish)} от конца фразы")
    if audio:
        out = Path(args.out).resolve()
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(lt.RATE)
            w.writeframes(bytes(audio))
        print(f"Что услышит собеседник: {out} ({len(audio) / 2 / lt.RATE:.1f} с)")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Real latency check of the Soniox engine")
    ap.add_argument("--text", default=PHRASE, help="Russian phrase to speak")
    ap.add_argument("--voice", help="Soniox voice name or clone id (default: from the installed app)")
    ap.add_argument("--proxy", help="proxy URL or 'none' (default: system proxy)")
    ap.add_argument("--out", default="latency_test_en.wav", help="where to save the English audio")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
