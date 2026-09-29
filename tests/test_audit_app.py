"""Regression tests for the app.py audit findings (track B1): settings I/O, stop, samples, clones, records."""
import asyncio
import json
import threading
import time
import types

import pytest

import app
import live_translator as lt
import test_robustness as base

# fixtures of the robustness suite: an Api with a do-nothing engine, a fake microphone, a sample file
live_api = base.live_api
recorder = base.recorder
sample = base.sample
StubEngine = base.StubEngine
RecStream = base.RecStream


@pytest.fixture(autouse=True)
def _portaudio_is_never_restarted(monkeypatch):
    monkeypatch.setattr(lt, "refresh_devices", lambda: False)


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def deletes(server):
    return [r.path for r in server.requests if r.method == "DELETE"]


# --- settings.json cannot be written (ids 1, 11) ---------------------------------------------

def test_a_settings_file_that_cannot_be_written_does_not_break_the_call(live_api, monkeypatch, tmp_path):
    """A full or read-only disk: the change still applies to the running call and the user is told."""
    assert live_api.start()["ok"]
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "gone" / "settings.json")  # its folder does not exist
    seq = live_api._bus.seq
    assert live_api.save_settings({"me_lang": "en"}) == {"restarted": True, "pending": False}
    assert live_api._settings["me_lang"] == "en"
    assert len(StubEngine.made) == 2 and StubEngine.made[1].args.their_lang == "en"  # the restart was not skipped
    warned = [e for e in live_api._bus.since(seq) if e["type"] == "status" and not e["ok"]]
    assert {e["label"] for e in warned} == {"Настройки"}  # again after the restart: see the next test


def test_the_unsaved_settings_warning_comes_after_the_restart_that_clears_the_statuses(live_api, monkeypatch, tmp_path):
    """The UI wipes its status line on "restarted": a warning sent before it would vanish at once."""
    assert live_api.start()["ok"]
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "gone" / "settings.json")
    seq = live_api._bus.seq
    live_api.save_settings({"me_lang": "en"})
    kinds = [(e["type"], e.get("label")) for e in live_api._bus.since(seq) if e["type"] in ("restarted", "status")]
    assert kinds.index(("restarted", None)) < max(i for i, k in enumerate(kinds) if k == ("status", "Настройки"))
    seq = live_api._bus.seq
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "settings.json")  # the disk is back
    live_api.save_settings({"me_lang": "ru"})
    assert ("status", "Настройки") not in [(e["type"], e.get("label")) for e in live_api._bus.since(seq)]


@pytest.mark.parametrize("key", ["abc\ndef", "abc def"])
def test_a_key_that_cannot_be_stored_is_answered_not_raised(live_api, monkeypatch, tmp_path, key):
    """pywebview turns a raised error into an opaque rejection: the field gets the reason as an answer instead."""
    monkeypatch.setattr(lt, "ENV_FILE", tmp_path / ".env")
    result = live_api.set_key(key, "soniox")
    assert result["ok"] is False and result["error"] and "abc" not in result["error"]
    assert not (tmp_path / ".env").exists()


def test_a_key_that_cannot_be_written_to_disk_is_answered_not_raised(live_api, monkeypatch, tmp_path):
    monkeypatch.setattr(lt, "ENV_FILE", tmp_path / "gone" / ".env")  # its folder does not exist
    result = live_api.set_key("soniox-key-1234", "soniox")
    assert result["ok"] is False and result["error"] and "soniox-key-1234" not in result["error"]


def test_stopping_a_call_with_an_unwritable_settings_file_still_stops(live_api, monkeypatch, tmp_path):
    assert live_api.start()["ok"]
    live_api._started = time.time() - 60
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "gone" / "settings.json")
    live_api.stop()
    assert not live_api._running() and live_api._started is None
    assert live_api._settings["usage_seconds"] > 0  # counted in memory


# --- stopping an engine that is still closing (ids 4, 12) -------------------------------------

def test_a_second_stop_while_the_engine_is_still_closing_does_not_raise(live_api, monkeypatch):
    monkeypatch.setattr(app, "STOP_WAIT", 0.1)
    release = threading.Event()

    class SlowToClose(StubEngine):
        async def run(self):
            try:
                await asyncio.Event().wait()
            finally:
                release.wait(5)  # a WASAPI stop that hangs

    monkeypatch.setattr(lt, "Engine", SlowToClose)
    try:
        assert live_api.start()["ok"]
        live_api.stop()
        assert live_api._running()  # the first stop gave up waiting
        live_api.stop()  # ■ again, or a settings restart: nothing left to cancel, must not raise
        live_api._stop_engine()
        assert live_api._running()
    finally:
        release.set()
    live_api._thread.join(5)
    assert not live_api._running()


def test_a_settings_change_after_a_stop_that_timed_out_starts_no_engine(live_api, monkeypatch):
    """The old engine still closes its devices after the stop: a settings restart must not start a phantom call."""
    monkeypatch.setattr(app, "STOP_WAIT", 0.1)
    release = threading.Event()

    class SlowToClose(StubEngine):
        async def run(self):
            try:
                await asyncio.Event().wait()
            finally:
                release.wait(5)

    monkeypatch.setattr(lt, "Engine", SlowToClose)
    try:
        assert live_api.start()["ok"]
        live_api.stop()
        assert live_api._running() and live_api._started is None
        made = len(StubEngine.made)
        assert live_api.save_settings({"me_lang": "en"}) == {"restarted": False, "pending": False}
        assert live_api._settings["me_lang"] == "en"  # saved for the next call
        assert len(StubEngine.made) == made and live_api._engine is None and not live_api._restart_pending
    finally:
        release.set()
    live_api._thread.join(5)
    assert not live_api._running()


# --- a recording without speech does not replace the sample (ids 9, 26) -------------------------

@pytest.mark.parametrize("verdict", ["quiet", "short"])
def test_a_recording_with_no_speech_found_keeps_the_old_sample(recorder, tmp_path, monkeypatch, verdict):
    (tmp_path / "voice_sample.mp3").write_bytes(b"old")
    monkeypatch.setattr(app.speech_audio, "prepare_sample", lambda pcm, rate: (pcm, {
        "verdict": verdict, "speech_seconds": 0.0}), raising=False)  # a muted microphone: no speech at all
    recorder.start_recording()
    base.say(RecStream.made[-1])
    result = recorder.stop_recording()
    assert result["verdict"] == verdict and result["saved"] is False
    assert (tmp_path / "voice_sample.mp3").read_bytes() == b"old" and not (tmp_path / "voice_sample.wav").exists()


def test_a_quiet_recording_with_speech_in_it_is_still_kept(recorder, tmp_path, monkeypatch):
    (tmp_path / "voice_sample.mp3").write_bytes(b"old")
    monkeypatch.setattr(app.speech_audio, "prepare_sample", lambda pcm, rate: (pcm, {
        "verdict": "quiet", "speech_seconds": 12.0}), raising=False)
    recorder.start_recording()
    base.say(RecStream.made[-1])
    result = recorder.stop_recording()
    assert result["verdict"] == "quiet" and result["saved"] is True
    assert not (tmp_path / "voice_sample.mp3").exists() and (tmp_path / "voice_sample.wav").exists()


# --- importing a sample never loses the current one (ids 13, 18, 24) ----------------------------

@pytest.fixture
def picker(live_api, sample, tmp_path):
    """import_sample with the file dialog answering with whatever `picker.path` is."""
    state = types.SimpleNamespace(path=None)
    live_api._window = types.SimpleNamespace(create_file_dialog=lambda *a, **kw: [str(state.path)])
    live_api.state = state
    return live_api


def test_importing_a_file_that_cannot_be_read_keeps_the_old_sample(picker, tmp_path):
    picker.state.path = tmp_path / "moved-away.mp3"
    result = picker.import_sample()
    assert result["ok"] is False and result["error"]
    assert (tmp_path / "voice_sample.wav").read_bytes() == b"RIFF....WAVE"


def test_importing_the_current_sample_itself_keeps_it(picker, tmp_path):
    picker.state.path = tmp_path / "voice_sample.wav"  # the dialog can open in the app's own folder
    assert picker.import_sample() == {"ok": True, "name": "voice_sample.wav"}
    assert (tmp_path / "voice_sample.wav").read_bytes() == b"RIFF....WAVE"


def test_importing_a_file_without_an_extension_is_refused(picker, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    picked = tmp_path / "elsewhere" / "my-voice"
    picked.write_bytes(b"audio")
    picker.state.path = picked
    assert picker.import_sample()["ok"] is False
    assert (tmp_path / "voice_sample.wav").read_bytes() == b"RIFF....WAVE" and picker._sample_path().suffix == ".wav"


def test_importing_a_file_replaces_the_old_sample(picker, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    picked = tmp_path / "elsewhere" / "Me.MP3"
    picked.write_bytes(b"new audio")
    picker.state.path = picked
    assert picker.import_sample() == {"ok": True, "name": "Me.MP3"}
    assert sorted(p.name for p in tmp_path.glob("voice_sample*")) == ["voice_sample.mp3"]
    assert (tmp_path / "voice_sample.mp3").read_bytes() == b"new audio"


# --- settings.json of the wrong shape (ids 14, 21) ------------------------------------------------

@pytest.mark.parametrize("content", ['[1, 2]', '"text"', 'null', '42'])
def test_a_settings_file_that_is_not_an_object_is_set_aside(monkeypatch, tmp_path, content):
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "settings.json")
    app.SETTINGS_FILE.write_text(content, encoding="utf-8")
    settings = app.load_settings()
    assert settings == {**app.DEFAULTS, "settings_version": app.SETTINGS_VERSION}
    assert (tmp_path / "settings.json.bad").read_text(encoding="utf-8") == content and not app.SETTINGS_FILE.exists()


@pytest.mark.parametrize("version, speed", [
    ("2", 1.0), ("abc", 1.0), (None, 1.0), ([3], 1.0), (True, 1.0),  # unreadable or old: migrated like v1/v2
    ("3", 1.1), (3.0, 1.1),                                           # a number in disguise: current
    (float("inf"), 1.0),
])
def test_a_settings_version_that_is_not_a_number_does_not_crash_the_start(monkeypatch, tmp_path, version, speed):
    settings = base.saved_settings(monkeypatch, tmp_path, {"speed": 1.1, "settings_version": version})
    assert settings["speed"] == speed and settings["settings_version"] == app.SETTINGS_VERSION


# --- a damaged notes file (id 15) --------------------------------------------------------------------

@pytest.mark.parametrize("content", [b'{"title": "half a fi', b"[1, 2]", b"\xff\xfe not utf-8"])
def test_a_damaged_notes_file_still_opens_the_record(live_api, content):
    app.RECORDS_DIR.mkdir()
    (app.RECORDS_DIR / "call.txt").write_text("line one\n", encoding="utf-8")
    (app.RECORDS_DIR / "call.json").write_bytes(content)
    record = live_api.get_record("call.txt")
    assert record["notes"] is None and record["text"] == "line one\n"


def test_a_good_notes_file_is_returned(live_api):
    app.RECORDS_DIR.mkdir()
    (app.RECORDS_DIR / "call.txt").write_text("line one\n", encoding="utf-8")
    (app.RECORDS_DIR / "call.json").write_text(json.dumps({"title": "Call"}), encoding="utf-8")
    assert live_api.get_record("call.txt")["notes"] == {"title": "Call"}


# --- clones: nothing left behind, nothing deleted while a call speaks with it (ids 20, 27, 28) ---------

def soniox_routes(server, new="new-voice", old="old-voice"):
    server.routes[("POST", "/v1/voices")] = (201, {"id": new})
    server.routes[("GET", f"/v1/voices/{new}")] = (200, {"models": [{"model": "tts-rt-v2", "status": "ready"}]})
    server.routes[("DELETE", f"/v1/voices/{old}")] = (204, b"")
    server.routes[("DELETE", f"/v1/voices/{new}")] = (204, b"")


def test_a_clone_whose_status_check_fails_is_deleted_again(live_api, http_server, sample):
    http_server.routes[("POST", "/v1/voices")] = (201, {"id": "flaky-voice"})
    http_server.routes[("GET", "/v1/voices/flaky-voice")] = (502, b"bad gateway")  # a VPN hiccup while polling
    http_server.routes[("DELETE", "/v1/voices/flaky-voice")] = (204, b"")
    result = live_api.create_clone()
    assert result["ok"] is False
    assert deletes(http_server) == ["/v1/voices/flaky-voice"]
    assert live_api._settings["soniox_voice_id"] is None


def call_with_a_phrase_in_progress(api, monkeypatch):
    """A running call that is not quiet: a settings restart has to wait for the English to end."""
    monkeypatch.setattr(app, "RESTART_QUIET", 0.1)
    assert api.start()["ok"]
    speaking = types.SimpleNamespace(busy=True)
    api._engine.players = [speaking]
    return speaking


def test_the_old_clone_is_deleted_only_after_the_call_moved_to_the_new_one(live_api, http_server, sample,
                                                                          monkeypatch):
    live_api._settings["soniox_voice_id"] = "old-voice"
    soniox_routes(http_server)
    speaking = call_with_a_phrase_in_progress(live_api, monkeypatch)
    assert live_api.create_clone() == {"ok": True, "provider": "soniox"}
    assert live_api._restart_pending and len(StubEngine.made) == 1  # the call still speaks with the old clone
    time.sleep(0.3)
    assert deletes(http_server) == []
    speaking.busy = False  # the English is over: the restart takes the new voice
    live_api._restarter.join(5)
    assert len(StubEngine.made) == 2 and StubEngine.made[1].args.voice_id == "new-voice"
    wait_for(lambda: deletes(http_server) == ["/v1/voices/old-voice"])


def test_stopping_the_call_deletes_the_clone_it_was_still_using(live_api, http_server, sample, monkeypatch):
    live_api._settings["soniox_voice_id"] = "old-voice"
    soniox_routes(http_server)
    call_with_a_phrase_in_progress(live_api, monkeypatch)
    assert live_api.create_clone()["ok"]
    assert deletes(http_server) == []
    live_api.stop()
    wait_for(lambda: deletes(http_server) == ["/v1/voices/old-voice"])


def test_a_restart_taking_the_lock_right_after_the_save_still_deletes_the_old_clone(live_api, http_server, sample,
                                                                                    monkeypatch):
    """The restart thread may get _lifecycle the moment the save lets go of it: the old clone has to be queued by
    then, or it waits for the next start or stop of a call."""
    live_api._settings["soniox_voice_id"] = "old-voice"
    soniox_routes(http_server)
    call_with_a_phrase_in_progress(live_api, monkeypatch)
    save, racers = live_api.save_settings, []

    def save_then_restart(patch):
        result = save(patch)

        def restart():
            with live_api._lifecycle:
                live_api._drop_stale_clones()  # what _start_engine does at the end of a restart

        racers.append(threading.Thread(target=restart))
        racers[0].start()
        time.sleep(0.2)  # long enough for it to win the lock, if the clone code lets it
        return result

    monkeypatch.setattr(live_api, "save_settings", save_then_restart)
    assert live_api.create_clone()["ok"]
    racers[0].join(5)
    wait_for(lambda: deletes(http_server) == ["/v1/voices/old-voice"], timeout=3)
