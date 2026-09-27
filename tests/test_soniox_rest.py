"""Soniox REST calls (create_voice, voice_status, list_voices) against a local HTTP mock."""
import re

import pytest

import soniox_engine
from mocks import form_fields, free_port
from voice_clone import CloneError

KEY = "soniox-test-key"
CALLS = {
    "create_voice": (("POST", "/v1/voices"), lambda: soniox_engine.create_voice(KEY, b"RIFF", None)),
    "voice_status": (("GET", "/v1/voices/v1"), lambda: soniox_engine.voice_status(KEY, "v1", None)),
    "list_voices": (("GET", "/v1/tts-models"), lambda: soniox_engine.list_voices(KEY, None)),
}


def test_create_voice_uploads_name_and_file(http_server):
    http_server.routes[("POST", "/v1/voices")] = (201, {"id": "voice-123"})
    wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(range(256)) + b"\r\n\r\n--tail"
    assert soniox_engine.create_voice(KEY, wav, None) == "voice-123"

    [req] = http_server.requests
    assert (req.method, req.path) == ("POST", "/v1/voices")
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    assert req.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    fields = form_fields(req.headers["Content-Type"], req.body)
    assert list(fields) == ["name", "file"]
    assert re.fullmatch(r"Live Translator \d{4}-\d\d-\d\d \d\d-\d\d-\d\d", fields["name"][1].decode())
    head, data = fields["file"]
    assert 'filename="voice.wav"' in head
    assert re.search(r"\r\nContent-Type: audio/(x-)?wav$", head)  # the registry may say either
    assert data == wav


def test_create_voice_keeps_the_sample_format(http_server):
    http_server.routes[("POST", "/v1/voices")] = (201, {"id": "voice-456"})
    mp3 = b"ID3\x04\x00" + bytes(range(64))
    assert soniox_engine.create_voice(KEY, mp3, None, "voice_sample.mp3") == "voice-456"
    [req] = http_server.requests
    head, data = form_fields(req.headers["Content-Type"], req.body)["file"]
    assert 'filename="voice_sample.mp3"' in head and head.endswith("\r\nContent-Type: audio/mpeg")
    assert data == mp3


@pytest.mark.parametrize("models, expected", [
    ([{"model": "tts-rt-v1", "status": "failed"}, {"model": "tts-rt-v2", "status": "ready"}], "ready"),
    ([{"model": "tts-rt-v2", "status": "processing"}], "processing"),
    ([{"model": "tts-rt-v2", "status": "failed", "error_message": "Sample too short."}], "failed: Sample too short."),
    ([{"model": "tts-rt-v2", "status": "failed", "error_type": "invalid_audio"}], "failed: invalid_audio"),
    ([{"model": "tts-rt-v1", "status": "ready"}], "processing"),  # not computed for our model yet
    ([], "processing"),
])
def test_voice_status(http_server, models, expected):
    http_server.routes[("GET", "/v1/voices/voice-123")] = (200, {"id": "voice-123", "models": models})
    assert soniox_engine.voice_status(KEY, "voice-123", None) == expected
    assert http_server.requests[-1].headers["Authorization"] == f"Bearer {KEY}"


def test_list_voices(http_server):
    http_server.routes[("GET", "/v1/tts-models")] = (200, {"models": [
        {"id": "tts-rt-v1", "voices": [{"id": "old", "name": "Old"}]},
        {"id": "tts-rt-v2-2026-05", "aliased_model_id": "tts-rt-v2", "voices": [
            {"id": "adrian", "name": "Adrian", "gender": "male", "description": "Warm, calm"},
            {"id": "nova"},
        ]},
    ]})
    assert soniox_engine.list_voices(KEY, None) == [
        {"name": "Adrian", "gender": "male", "description": "Warm, calm"},
        {"name": "nova", "gender": "", "description": ""},
    ]
    assert http_server.requests[-1].headers["Authorization"] == f"Bearer {KEY}"


def test_list_voices_without_our_model(http_server):
    http_server.routes[("GET", "/v1/tts-models")] = (200, {"models": [{"id": "tts-rt-v1", "voices": [{"id": "x"}]}]})
    assert soniox_engine.list_voices(KEY, None) == []


@pytest.mark.parametrize("status", [401, 402, 403])
@pytest.mark.parametrize("call", CALLS)
def test_rejected_key(http_server, call, status):
    route, run = CALLS[call]
    http_server.routes[route] = (status, {"error": "unauthorized"})
    with pytest.raises(CloneError, match="SONIOX_API_KEY"):
        run()


def test_server_error_is_reported(http_server):
    http_server.routes[("POST", "/v1/voices")] = (500, b"upstream exploded")
    with pytest.raises(CloneError, match="HTTP 500 upstream exploded"):
        soniox_engine.create_voice(KEY, b"RIFF", None)


def test_no_connection(monkeypatch):
    monkeypatch.setattr(soniox_engine, "API_URL", f"http://127.0.0.1:{free_port()}")  # nothing listens
    with pytest.raises(CloneError, match="Нет связи с Soniox"):
        soniox_engine.list_voices(KEY, None)
