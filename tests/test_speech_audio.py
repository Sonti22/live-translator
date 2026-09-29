"""Silence trimming of synthesized speech (speech_audio)."""
import numpy as np
import pytest

import speech_audio as sa

MS = sa.RATE // 1000  # samples per millisecond


def tone(ms, amp=5000):
    return np.full(ms * MS, amp, "<i2").tobytes()


def silence(ms, amp=0):
    return np.full(ms * MS, amp, "<i2").tobytes()


def samples(pcm):
    return np.frombuffer(pcm, "<i2")


def test_lead_trimmer_drops_leading_silence_keeping_a_faded_preroll():
    trimmer = sa.LeadTrimmer()
    assert trimmer.feed(silence(40, amp=200)) == b""  # below the threshold: still silence
    out = trimmer.feed(silence(40) + tone(30))
    assert len(out) == (20 + 30) * MS * 2  # 20 ms pre-roll + the sound
    x = samples(out)
    assert x[0] == 0 and x[20 * MS] == 5000  # fade-in within the pre-roll, sound untouched
    assert trimmer.feed(silence(10)) == silence(10)  # after the first sound everything passes


def test_lead_trimmer_keeps_audio_that_starts_with_sound():
    trimmer = sa.LeadTrimmer()
    pcm = silence(10) + tone(20)  # sound within the pre-roll: nothing to cut, no fade
    assert trimmer.feed(pcm) == pcm


def test_lead_trimmer_gives_up_on_quiet_speech():
    trimmer = sa.LeadTrimmer()
    assert trimmer.feed(silence(300, amp=100)) == b""
    out = trimmer.feed(silence(150, amp=100))  # 450 ms without sound: quiet speech, not a silent lead
    assert out == silence(450, amp=100)
    assert trimmer.feed(silence(10)) == silence(10)


def test_lead_trimmer_disabled():
    trimmer = sa.LeadTrimmer(enabled=False)
    assert trimmer.feed(silence(50)) == silence(50)


def test_trim_lead_of_a_whole_clip():
    assert sa.trim_lead(silence(100)) == b""
    assert len(sa.trim_lead(silence(100) + tone(50))) == (20 + 50) * MS * 2


@pytest.mark.parametrize("text, keep", [
    ("Hello.", None), ("Really?", None), ("Wait!", None), ("Well…", None), ('He said "yes."', None),
    ("First,", sa.CLAUSE_KEEP), ("Note:", sa.CLAUSE_KEEP), ("One; ", sa.CLAUSE_KEEP),
    ("and then", sa.SPLIT_KEEP), ("", sa.SPLIT_KEEP),
])
def test_tail_keep(text, keep):
    assert sa.tail_keep(text) == keep


def test_quiet_after_counts_silence_across_chunks():
    quiet = sa.quiet_after(tone(10) + silence(30))
    assert quiet == 30 * MS
    quiet = sa.quiet_after(silence(20), quiet)
    assert quiet == 50 * MS
    assert sa.quiet_after(silence(5) + tone(1), quiet) == 0


def test_cut_tail_keeps_a_short_pause_after_the_last_sound():
    tail = tone(20) + silence(140)
    out = sa.cut_tail(tail, 140 * MS, 0.050)
    assert len(out) == (20 + 50) * MS * 2
    assert samples(out)[-1] == 0 and samples(out)[0] == 5000


def test_cut_tail_counts_silence_that_was_already_played():
    tail = silence(100)  # 60 ms of the 160 ms silence were played before this tail
    out = sa.cut_tail(tail, 160 * MS, 0.080)
    assert len(out) == 20 * MS * 2  # 60 played + 20 = the 80 ms pause
    assert sa.cut_tail(tail, 300 * MS, 0.080) == b""  # the pause is already longer than wanted


def test_cut_tail_leaves_a_short_tail_alone():
    tail = tone(20) + silence(30)
    assert sa.cut_tail(tail, 30 * MS, 0.050) == tail


# --- trim profiles: fast / balanced / natural ------------------------------------------

BALANCED, NATURAL = sa.TRIMS["balanced"], sa.TRIMS["natural"]


def test_fast_profile_is_todays_constants():
    assert sa.TRIMS["fast"] == sa.FAST == (sa.THRESHOLD, sa.FADE, sa.CLAUSE_KEEP, sa.SPLIT_KEEP)
    assert set(sa.TRIMS) == {"fast", "balanced", "natural"}


def test_a_quiet_sound_is_silence_for_fast_but_speech_for_balanced():
    quiet = silence(40, amp=200)  # above balanced's threshold (150), below fast's (300)
    assert sa.LeadTrimmer().feed(quiet) == b""
    assert sa.LeadTrimmer(cut=BALANCED).feed(quiet) == quiet
    assert sa.LeadTrimmer(cut=NATURAL).feed(quiet) == quiet
    assert sa.trim_lead(quiet) == b"" and sa.trim_lead(quiet, BALANCED) == quiet
    assert sa.trailing_quiet(tone(10) + silence(20, amp=200)) == 20 * MS
    assert sa.trailing_quiet(tone(10) + silence(20, amp=200), BALANCED) == 0
    assert sa.quiet_after(silence(20, amp=200), 5, BALANCED) == 0


def test_balanced_lead_fades_in_over_10_ms():
    clip = silence(100, amp=100) + tone(50)  # a noise floor below both thresholds is what the fade shapes
    fast, balanced = samples(sa.trim_lead(clip)), samples(sa.trim_lead(clip, BALANCED))
    assert len(fast) == len(balanced) == (20 + 50) * MS
    assert fast[5 * MS] == 100 and balanced[5 * MS] == 50  # a 5 ms ramp is over, a 10 ms one is half way
    assert balanced[0] == 0 and balanced[10 * MS] == 100
    trimmer = sa.LeadTrimmer(cut=BALANCED)
    assert samples(trimmer.feed(silence(40, amp=100) + tone(30)))[5 * MS] == 50


@pytest.mark.parametrize("text, keep", [
    ("Hello.", None), ("Wait!", None), ("Well…", None),
    ("First,", 0.150), ("Note:", 0.150), ("One; ", 0.150),
    ("and then", 0.100), ("", 0.100),
])
def test_tail_keep_balanced(text, keep):
    assert sa.tail_keep(text, BALANCED) == keep


@pytest.mark.parametrize("text", ["Hello.", "First,", "Note:", "and then", ""])
def test_natural_never_cuts_a_seam(text):
    assert sa.tail_keep(text, NATURAL) is None


def test_cut_tail_fades_out_by_the_profile():
    tail = tone(50)  # all of it counts as trailing silence: cut 20 ms after the "last sound"
    fast, balanced = (sa.cut_tail(tail, 50 * MS, 0.020, cut) for cut in (sa.FAST, BALANCED))
    assert len(fast) == len(balanced) == 20 * MS * 2
    assert samples(fast)[-5 * MS - 1] == 5000 and samples(balanced)[-5 * MS - 1] < 5000
    assert samples(fast)[-1] == samples(balanced)[-1] == 0


# --- prepare_sample: a voice recording for cloning ---------------------------------------

def hum(seconds, rate, amp, seed=1):
    """Room noise: uniform random samples in +-amp."""
    return np.random.default_rng(seed).integers(-amp, amp + 1, int(seconds * rate)).astype("<i2")


def talk(seconds, rate, amp=8000):
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * 200 * t)).astype("<i2")


def recording(rate, lead=2.0, speech=25.0, tail=2.0, amp=8000, noise=20):
    parts = [hum(lead, rate, noise, 1), talk(speech, rate, amp), hum(tail, rate, noise, 2)]
    return np.concatenate(parts).tobytes()


def peak_dbfs(pcm):
    return 20 * np.log10(np.abs(samples(pcm).astype(np.int32)).max() / 32768)


def test_prepare_sample_trims_silence_keeping_a_quarter_second_and_peaks_at_minus_3_dbfs():
    out, report = sa.prepare_sample(recording(48000), 48000)
    assert len(out) == int((0.25 + 25.0 + 0.25) * 48000) * 2
    assert abs(peak_dbfs(out) + 3.0) < 0.1
    assert report["verdict"] == "ok"
    assert report["speech_seconds"] == pytest.approx(25.0, abs=0.1)
    assert report["peak_dbfs"] == pytest.approx(20 * np.log10(8000 / 32768), abs=0.1)  # of the recording
    assert report["noise_dbfs"] < sa.NOISY


@pytest.mark.parametrize("rate", [16000, 44100, 48000])
def test_prepare_sample_keeps_the_sample_rate(rate):
    out, report = sa.prepare_sample(recording(rate), rate)
    assert len(out) // 2 == pytest.approx(25.5 * rate, abs=rate * 0.02)
    assert report["verdict"] == "ok"


def test_prepare_sample_keeps_what_is_less_than_a_pad_of_silence():
    rate = 48000
    pcm = np.concatenate([hum(0.1, rate, 20, 1), talk(5.0, rate), hum(5.0, rate, 20, 2), talk(20.0, rate),
                          hum(0.05, rate, 20, 3)]).tobytes()
    out, _ = sa.prepare_sample(pcm, rate)
    assert len(out) == len(pcm)  # nothing to cut at the edges


def test_prepare_sample_does_not_take_a_click_for_speech():
    rate = 48000
    click = np.concatenate([hum(1.0, rate, 20, 3), talk(0.02, rate), hum(1.0, rate, 20, 4)]).tobytes()
    out, _ = sa.prepare_sample(click + recording(rate), rate)
    assert len(out) == int(25.5 * rate) * 2  # cut at the speech, not at the click


def test_prepare_sample_needs_no_noise_at_all():
    rate = 48000
    pcm = np.concatenate([np.zeros(2 * rate, "<i2"), talk(25.0, rate), np.zeros(2 * rate, "<i2")]).tobytes()
    out, report = sa.prepare_sample(pcm, rate)
    assert len(out) == int(25.5 * rate) * 2 and report["verdict"] == "ok"
    assert report["noise_dbfs"] < -100


def test_prepare_sample_scales_a_loud_recording_down_too():
    rate = 48000
    out, _ = sa.prepare_sample(recording(rate, amp=30000), rate)
    assert abs(peak_dbfs(out) + 3.0) < 0.1


def test_prepare_sample_verdict_short():
    rate = 48000
    out, report = sa.prepare_sample(recording(rate, lead=1.0, speech=5.0, tail=1.0), rate)
    assert report["verdict"] == "short"
    assert report["speech_seconds"] == pytest.approx(5.0, abs=0.1)
    assert len(out) == int(5.5 * rate) * 2  # still trimmed and normalized


def test_prepare_sample_verdict_quiet():
    _, report = sa.prepare_sample(recording(48000, amp=2000, noise=2), 48000)  # peaks at -24 dBFS
    assert report["verdict"] == "quiet" and report["peak_dbfs"] < sa.QUIET_PEAK


def test_prepare_sample_verdict_clipped():
    rate = 48000
    square = np.where(talk(25.0, rate) >= 0, 32767, -32768).astype("<i2")
    pcm = np.concatenate([hum(2.0, rate, 20), square, hum(2.0, rate, 20)]).tobytes()
    out, report = sa.prepare_sample(pcm, rate)
    assert report["verdict"] == "clipped"
    assert abs(peak_dbfs(out) + 3.0) < 0.1


def test_prepare_sample_verdict_noisy():
    _, report = sa.prepare_sample(recording(48000, noise=870), 48000)  # room noise at about -36 dBFS
    assert report["verdict"] == "noisy" and report["noise_dbfs"] > sa.NOISY


def test_prepare_sample_reports_the_first_problem_it_finds():
    rate = 48000
    _, report = sa.prepare_sample(recording(rate, speech=5.0, amp=2000, noise=200), rate)  # quiet, noisy and short
    assert report["verdict"] == "quiet"


@pytest.mark.parametrize("pcm, verdict", [
    (b"", "quiet"),
    (b"\x01", "quiet"),  # half a sample
    (silence(500), "quiet"),  # nothing recorded
    (hum(1.0, 48000, 20).tobytes(), "quiet"),  # only room noise
    (talk(0.01, 48000).tobytes(), "short"),  # less than a frame of a loud sound
    (np.full(48000, 20000, "<i2").tobytes(), "short"),  # a constant level is no speech
], ids=["empty", "half-sample", "silence", "room-noise", "under-a-frame", "constant-level"])
def test_prepare_sample_without_speech_returns_the_input(pcm, verdict):
    out, report = sa.prepare_sample(pcm, 48000)
    assert out == pcm
    assert report["verdict"] == verdict and report["speech_seconds"] == 0.0
    assert set(report) == {"speech_seconds", "peak_dbfs", "noise_dbfs", "verdict"}


def test_prepare_sample_does_not_change_its_input():
    pcm = recording(16000)
    copy = bytes(pcm)
    sa.prepare_sample(pcm, 16000)
    assert pcm == copy
