"""
Test setup: every endpoint the engine talks to points at a local mock server, so the suite runs
without API keys, network or audio hardware.

The engine modules read their endpoint URLs from env vars at import time, so the ports are reserved
and the variables set here, before any test module imports them.
"""
import importlib
import os
import sys
import types
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path[:0] = [str(TESTS.parent), str(TESTS)]

from mocks import MockHTTP, MockWS, free_port  # noqa: E402  (imports no engine module)

WS_PORT, HTTP_PORT = free_port(), free_port()
WS_BASE, HTTP_BASE = f"ws://127.0.0.1:{WS_PORT}", f"http://127.0.0.1:{HTTP_PORT}"
os.environ.update({
    "LIVE_TRANSLATOR_URL": f"{WS_BASE}/openai?model=gpt-realtime-translate",
    "LIVE_TRANSLATOR_TTS_URL": f"{WS_BASE}/cartesia?cartesia_version=test",
    "LIVE_TRANSLATOR_TTS_API": HTTP_BASE,
    "LIVE_TRANSLATOR_SONIOX_STT": f"{WS_BASE}/soniox-stt",
    "LIVE_TRANSLATOR_SONIOX_TTS": f"{WS_BASE}/soniox-tts",
    "LIVE_TRANSLATOR_SONIOX_API": HTTP_BASE,
    "LIVE_TRANSLATOR_OPENAI_API": f"{HTTP_BASE}/v1/responses",
})

# Audio and GUI packages are only used by code the tests never run: stub them where they can't load
# (e.g. no PortAudio in a cloud container)
for _name in ("sounddevice", "soundcard", "webview"):
    try:
        importlib.import_module(_name)
    except (ImportError, OSError):
        sys.modules[_name] = types.ModuleType(_name)


@pytest.fixture(scope="session")
def _ws_server():
    server = MockWS(WS_PORT)
    yield server
    server.close()


@pytest.fixture
def ws_server(_ws_server):
    """The mock websocket server behind all ws URLs (paths /openai, /cartesia, /soniox-stt, /soniox-tts)."""
    _ws_server.reset()
    yield _ws_server
    if _ws_server.errors:
        raise _ws_server.errors[0]


@pytest.fixture(scope="session")
def _http_server():
    server = MockHTTP(HTTP_PORT)
    yield server
    server.close()


@pytest.fixture
def http_server(_http_server):
    """The mock REST server behind LIVE_TRANSLATOR_SONIOX_API, _TTS_API and _OPENAI_API."""
    _http_server.reset()
    return _http_server
