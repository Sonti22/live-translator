"""
Silence trimming for synthesized speech (PCM16 mono 24 kHz).

A TTS stream starts with 64-92 ms and ends with 124-156 ms of silence. Between clauses that is dead
air the other person hears, so the voice cuts it: the lead of every stream, and the tail at a seam
where the next clause is already waiting (keeping a short, natural pause).

How hard it cuts depends on the delivery (Trim): "fast" cuts as above, "balanced" is gentler (quieter
threshold, longer pauses kept), "natural" only drops the silence a stream starts with.

prepare_sample() is the other job of this module: a recording of my voice, made for a voice clone, is
trimmed and normalized before it is uploaded, and judged (too quiet, clipped, noisy, too short).
"""
from collections import namedtuple

import numpy as np

RATE = 24_000
THRESHOLD = 300   # |sample| above this is sound
PREROLL = 0.020   # kept before the first sound
FADE = 0.005
GIVE_UP = 0.400   # no sound this long: it is quiet speech, not a silent lead
SENTENCE_END = (".", "!", "?", "…")
CLAUSE_KEEP = 0.080  # silence kept after , ; :
SPLIT_KEEP = 0.050   # after a clause split with no punctuation
HOLD = 0.160         # the end of the stream being heard is held back, so a seam can still cut it...
HOLD_MIN = 0.300     # ...but only while the player has this much queued: holding must never cause a gap

# threshold, fade (s), silence kept after , ; : and after an unpunctuated split (s; None: seams are not cut)
Trim = namedtuple("Trim", "threshold fade clause_keep split_keep")
FAST = Trim(THRESHOLD, FADE, CLAUSE_KEEP, SPLIT_KEEP)
TRIMS = {"fast": FAST, "balanced": Trim(150, 0.010, 0.150, 0.100), "natural": Trim(150, 0.010, None, None)}


def _samples(pcm):
    return np.frombuffer(pcm, "<i2", count=len(pcm) // 2)


def _loud(pcm, cut=FAST):
    return np.flatnonzero(np.abs(_samples(pcm).astype(np.int32)) > cut.threshold)


def _fade(pcm, rising, cut=FAST):
    x = _samples(pcm).astype(np.float32)
    n = min(len(x), int(cut.fade * RATE))
    if n:
        ramp = np.arange(n, dtype=np.float32) / n
        if rising:
            x[:n] *= ramp
        else:
            x[len(x) - n:] *= ramp[::-1]
    return x.astype("<i2").tobytes() + pcm[len(x) * 2:]


def trailing_quiet(pcm, cut=FAST):
    """Samples of silence the audio ends with (all of them if it has no sound)."""
    loud = _loud(pcm, cut)
    return len(pcm) // 2 - (int(loud[-1]) + 1 if loud.size else 0)


def quiet_after(pcm, before=0, cut=FAST):
    """Trailing silence of a stream that ended with `before` silent samples and then got `pcm`."""
    quiet = trailing_quiet(pcm, cut)
    return before + quiet if quiet == len(pcm) // 2 else quiet


def trim_lead(pcm, cut=FAST):
    """A whole clip without the silence it starts with."""
    loud = _loud(pcm, cut)
    if not loud.size:
        return b""
    start = max(0, int(loud[0]) - int(PREROLL * RATE))
    return _fade(pcm[start * 2:], True, cut) if start else pcm


class LeadTrimmer:
    """Drops the silence a stream starts with, fed chunk by chunk; everything after the first sound passes."""

    def __init__(self, enabled=True, cut=FAST):
        self.held = b""
        self.done = not enabled
        self.cut = cut

    def feed(self, pcm):
        if self.done:
            return pcm
        data = self.held + pcm
        if _loud(data, self.cut).size:
            self.done, self.held = True, b""
            return trim_lead(data, self.cut)
        if len(data) // 2 >= GIVE_UP * RATE:
            self.done, self.held = True, b""
            return data
        self.held = data
        return b""


def tail_keep(text, cut=FAST):
    """Silence to keep after a clause, by how it ends; None after a sentence (its pause stays) or when
    the delivery does not cut seams."""
    mark = text.rstrip().rstrip("\"')»”’")[-1:]
    if mark in SENTENCE_END:
        return None
    return cut.clause_keep if mark in (",", ";", ":") else cut.split_keep


def cut_tail(tail, quiet, keep, cut=FAST):
    """The unplayed end of a stream, cut `keep` s after its last sound.

    quiet: the stream's trailing silence in samples, including any already played before `tail`."""
    n = len(tail) // 2
    kept = min(n, max(0, n - quiet + int(keep * RATE)))
    return tail if kept == n else _fade(tail[:kept * 2], False, cut)


# --- my voice sample (any sample rate) ---------------------------------------------------

SAMPLE_PEAK = -3.0     # dBFS the prepared sample peaks at
SAMPLE_PAD = 0.250     # silence kept before the first and after the last speech
SAMPLE_FRAME = 0.020   # analysis window
SPEECH_OVER = 12.0     # dB over the pause level: a frame with speech in it...
SPEECH_FLOOR = -60.0   # ...never below this, even when the pauses are digital silence
SPEECH_RUN = 3         # frames in a row: a click is no speech
MIN_SPEECH = 20.0      # seconds of speech a clone needs
QUIET_PEAK = -20.0     # dBFS: a recording peaking lower is too quiet
CLIPPED_SHARE = 0.001  # samples at full scale
NOISY = -45.0          # dBFS of the pauses once the sample is normalized


def _dbfs(x):
    return 20 * np.log10(np.maximum(x, 1e-6) / 32768)  # floor: -120 dBFS


def prepare_sample(pcm, rate):
    """A recording of my voice (PCM16 mono at `rate`) for cloning: silence trimmed at both ends, peak at -3 dBFS.

    Returns (pcm, report): report has speech_seconds, peak_dbfs (of the recording), noise_dbfs (the pauses of the
    prepared sample) and verdict: "ok", "quiet", "clipped", "noisy" or "short" (the first that applies)."""
    x = _samples(pcm).astype(np.float32)
    peak = float(_dbfs(np.abs(x).max())) if x.size else -120.0
    report = {"speech_seconds": 0.0, "peak_dbfs": peak, "noise_dbfs": -120.0,
              "verdict": "quiet" if peak < QUIET_PEAK else "short"}  # what it stays if no speech is found
    frame = max(1, int(SAMPLE_FRAME * rate))
    frames = len(x) // frame
    if not frames:
        return pcm, report
    levels = _dbfs(np.sqrt(np.mean(x[:frames * frame].reshape(frames, frame) ** 2, axis=1)))
    pause = float(np.percentile(levels, 10))
    speech = levels >= max(pause + SPEECH_OVER, SPEECH_FLOOR)
    starts = np.flatnonzero(np.convolve(speech, np.ones(SPEECH_RUN, int), "valid") == SPEECH_RUN)
    if not starts.size:
        return pcm, report
    first, last = int(starts[0]), int(starts[-1]) + SPEECH_RUN
    pad = int(SAMPLE_PAD * rate)
    cut = x[max(0, first * frame - pad):min(len(x), last * frame + pad)]
    top = float(np.abs(cut).max())
    gain = 10 ** (SAMPLE_PEAK / 20) * 32768 / top
    speech_seconds = float(speech[first:last].sum()) * SAMPLE_FRAME
    noise = min(0.0, max(-120.0, pause + 20 * float(np.log10(gain))))
    clipped = float(np.mean(np.abs(cut) >= 32767)) > CLIPPED_SHARE
    verdict = ("quiet" if peak < QUIET_PEAK else "clipped" if clipped else "noisy" if noise > NOISY
               else "short" if speech_seconds < MIN_SPEECH else "ok")
    report.update(speech_seconds=round(speech_seconds, 2), noise_dbfs=round(noise, 1), verdict=verdict)
    return np.clip(cut * gain, -32768, 32767).astype("<i2").tobytes(), report
