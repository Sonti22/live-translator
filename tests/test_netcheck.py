"""«Проверить связь»: websocket round trips, where the VPN exits, advice, Api.check_connection."""
import argparse
import asyncio
import socket
import threading
import time

import pytest

import app
import live_translator as lt
import netcheck
import soniox_engine


async def wait_closed(ws):
    await ws.wait_closed()


@pytest.fixture
def dead_port():
    """A port that accepts TCP and hangs up at once (a refused connect takes 2 s on Windows)."""
    server = socket.create_server(("127.0.0.1", 0))

    def hang_up():
        while True:
            try:
                server.accept()[0].close()
            except OSError:  # closed by the test
                return

    threading.Thread(target=hang_up, daemon=True).start()
    yield server.getsockname()[1]
    server.close()


async def test_ping_round_trips_are_measured(ws_server):
    seen = []

    async def handler(ws):
        seen.append((ws.request.path, ws.request.headers.get("X-API-Key")))
        await ws.wait_closed()

    ws_server.handler = handler
    result = await netcheck.ws_rtt(soniox_engine.STT_URL, None, {"X-API-Key": "k"}, pings=3)
    assert result["error"] is None
    assert isinstance(result["open_ms"], int) and isinstance(result["ping_ms"], int)
    assert 0 <= result["ping_ms"] < 1000
    assert seen == [("/soniox-stt", "k")]


@pytest.mark.parametrize("status, error, rejected", [
    (401, "ключ отклонён — вставьте новый (HTTP 401)", 401),  # the service answered: the key is wrong
    (403, "ключ отклонён — вставьте новый (HTTP 403)", 403),
    (502, "HTTP 502", None),
])
async def test_refused_handshake_is_reported_not_raised(ws_server, status, error, rejected):
    ws_server.handler, ws_server.reject = wait_closed, status
    assert await netcheck.ws_rtt(soniox_engine.TTS_URL, None) == {"open_ms": None, "ping_ms": None,
                                                                  "error": error, "rejected": rejected}


async def test_openai_refusing_the_country_says_so(ws_server, http_server):
    ws_server.handler, ws_server.reject = wait_closed, 403
    result = await netcheck.check([("openai", "OpenAI", lt.URL, None),
                                   ("cartesia", "Cartesia", soniox_engine.TTS_URL, None)], None)
    openai, cartesia = result["probes"]
    assert openai["error"] == "недоступен из этой страны — включите VPN (HTTP 403)" and openai["rejected"] == 403
    assert cartesia["error"] == "ключ отклонён — вставьте новый (HTTP 403)"


async def test_a_dropped_connection_is_reported(dead_port):
    result = await netcheck.ws_rtt(f"ws://127.0.0.1:{dead_port}/", None)
    assert result["open_ms"] is None and result["ping_ms"] is None and result["error"]


async def test_a_silent_server_times_out(monkeypatch):
    async def mute(reader, writer):  # accepts TCP, never answers the handshake
        await reader.read()

    server = await asyncio.start_server(mute, "127.0.0.1", 0)
    monkeypatch.setattr(netcheck, "TIMEOUT", 0.3)
    try:
        result = await netcheck.ws_rtt(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/", None)
    finally:
        server.close()
    assert result == {"open_ms": None, "ping_ms": None, "error": "нет ответа", "rejected": None}


def test_exit_location_parses_the_cloudflare_trace(http_server):
    http_server.routes[("GET", "/cdn-cgi/trace")] = (
        200, b"fl=123f\nh=www.cloudflare.com\nip=203.0.113.7\nts=1.2\nloc=DE\ncolo=FRA\nhttp=http/1.1\n")
    assert netcheck.exit_location(None) == {"loc": "DE", "colo": "FRA", "ip": "203.0.113.7"}
    assert http_server.requests[0].path == "/cdn-cgi/trace"


def test_exit_location_failures(http_server, monkeypatch, dead_port):
    http_server.routes[("GET", "/cdn-cgi/trace")] = (503, b"busy")
    assert netcheck.exit_location(None) == {"error": "HTTP 503"}
    monkeypatch.setattr(netcheck, "TRACE_URL", f"http://127.0.0.1:{dead_port}/cdn-cgi/trace")
    assert set(netcheck.exit_location(None)) == {"error"}
    assert set(netcheck.exit_location("ftp://proxy:21")) == {"error"}  # a bad proxy is an answer too


async def test_a_hanging_exit_lookup_does_not_hold_the_check(monkeypatch):
    monkeypatch.setattr(netcheck, "TIMEOUT", 0.2)
    monkeypatch.setattr(netcheck, "exit_location", lambda proxy: __import__("time").sleep(1) or {"loc": "CL"})
    result = await asyncio.wait_for(netcheck.check([], None), 0.8)
    assert result == {"exit": {"error": "нет ответа"}, "probes": []}


async def test_the_check_runs_on_python_3_10(ws_server, monkeypatch):
    """README promises Python 3.10+, which has no asyncio.timeout: the check must still end in time."""
    async def mute(reader, writer):
        await reader.read()

    monkeypatch.delattr(asyncio, "timeout", raising=False)
    monkeypatch.setattr(netcheck, "TIMEOUT", 0.3)
    monkeypatch.setattr(netcheck, "exit_location", lambda proxy: time.sleep(1) or {"loc": "CL"})
    ws_server.handler = wait_closed
    server = await asyncio.start_server(mute, "127.0.0.1", 0)
    try:
        result = await asyncio.wait_for(netcheck.check(
            [("soniox_stt", "Soniox", soniox_engine.STT_URL, None),
             ("silent", "Silent", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/", None)], None), 0.9)
    finally:
        server.close()
    stt, silent = result["probes"]
    assert result["exit"] == {"error": "нет ответа"}
    assert stt["error"] is None and stt["ping_ms"] is not None
    assert (silent["ping_ms"], silent["error"]) == (None, "нет ответа")


async def test_check_runs_every_probe(ws_server, http_server, dead_port):
    ws_server.handler = wait_closed
    http_server.routes[("GET", "/cdn-cgi/trace")] = (200, b"ip=203.0.113.7\nloc=CL\ncolo=SCL\n")
    result = await netcheck.check([("soniox_stt", "Soniox", soniox_engine.STT_URL, None),
                                   ("dead", "Dead", f"ws://127.0.0.1:{dead_port}/", None)], None)
    assert result["exit"] == {"loc": "CL", "colo": "SCL", "ip": "203.0.113.7"}
    stt, dead = result["probes"]
    assert (stt["id"], stt["label"], stt["error"]) == ("soniox_stt", "Soniox", None) and stt["ping_ms"] is not None
    assert dead["id"] == "dead" and dead["ping_ms"] is None and dead["error"]


# --- advice ------------------------------------------------------------------------------------

def probe(pid, ping, label=None):
    return {"id": pid, "label": label or pid, "open_ms": None if ping is None else ping * 3, "ping_ms": ping,
            "error": None if ping is not None else "HTTP 403"}


@pytest.mark.parametrize("exit_, probes, expected", [
    ({"loc": "CL"}, [probe("soniox_stt", None), probe("openai", None, "OpenAI")],
     "Нет связи с сервисами перевода: включите VPN или проверьте прокси."),
    ({"loc": "CL"}, [probe("soniox_stt", 410, "Soniox"), probe("soniox_eu", 420)],
     "Задержка до Soniox 410 мс — это много (VPN выходит в CL): выберите сервер VPN в Европе "
     "(Германия, Нидерланды, Финляндия)."),
    ({"error": "нет ответа"}, [probe("soniox_stt", 160, "Soniox")],
     "Задержка до Soniox 160 мс — это много: выберите сервер VPN в Европе (Германия, Нидерланды, Финляндия)."),
    ({"loc": "DE"}, [probe("soniox_stt", 120, "Soniox"), probe("soniox_eu", 30)],
     "Связь хорошая: 120 мс до Soniox. Soniox EU быстрее на 90 мс (нужен проект Soniox в регионе EU)."),
    ({"loc": "DE"}, [probe("soniox_stt", 40, "Soniox"), probe("soniox_eu", 30)],
     "Связь хорошая: 40 мс до Soniox."),  # 10 ms is not worth a new project
    ({"loc": "DE"}, [probe("soniox_stt", 90, "Soniox"), probe("soniox_eu", None), probe("openai", None, "OpenAI")],
     "Связь хорошая: 90 мс до Soniox. Не отвечают: OpenAI."),  # the EU probe is optional
    ({"loc": "US"}, [probe("soniox_stt", None, "Soniox"), probe("openai", 200, "OpenAI")],
     "Задержка до OpenAI 200 мс — это много (VPN выходит в US): выберите сервер VPN в Европе "
     "(Германия, Нидерланды, Финляндия). Не отвечают: Soniox."),
    ({"loc": "DE"}, [probe("soniox_stt", 90, "Soniox"), probe("inworld", None, "Inworld"),
                     {**probe("cartesia", None, "Cartesia"), "error": "ключ отклонён — вставьте новый (HTTP 401)",
                      "rejected": 401}],
     "Связь хорошая: 90 мс до Soniox. Не отвечают: Inworld. Cartesia: ключ отклонён — вставьте новый (HTTP 401)."),
])
def test_hint(exit_, probes, expected):
    assert netcheck.hint({"exit": exit_, "probes": probes}) == expected


# --- Api.check_connection ----------------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(lt, "start_hotkey", lambda callback, **kw: False)
    for name in app.KEY_ENVS.values():
        monkeypatch.delenv(name, raising=False)
    return app.Api(argparse.Namespace(proxy=None))


def test_only_services_with_a_key_are_probed(api, monkeypatch):
    calls = []

    async def fake_check(probes, proxy):
        calls.append((probes, proxy))
        return {"exit": {"loc": "DE"}, "probes": [probe(pid, 50, label) for pid, label, _, _ in probes]}

    monkeypatch.setattr(netcheck, "check", fake_check)
    api._settings["proxy"] = "127.0.0.1:10808"
    result = api.check_connection()
    probes, proxy = calls[0]
    assert proxy == "socks5h://127.0.0.1:10808"  # the app's own proxy setting
    assert [p[0] for p in probes] == ["soniox_stt", "soniox_tts", "soniox_eu"]
    assert result["ok"] and result["hint"] == "Связь хорошая: 50 мс до Soniox (распознавание)."
    for provider in ("openai", "cartesia", "inworld"):
        monkeypatch.setenv(app.KEY_ENVS[provider], f"{provider}-key")
    api.check_connection()
    probes = {p[0]: p for p in calls[1][0]}
    assert list(probes) == ["soniox_stt", "soniox_tts", "soniox_eu", "openai", "cartesia", "inworld"]
    assert probes["openai"][3] == {"Authorization": "Bearer openai-key"}
    assert probes["cartesia"][3] == {"X-API-Key": "cartesia-key"}
    assert probes["inworld"][3] == {"Authorization": "Basic inworld-key"}


def test_bad_proxy_is_a_message(api):
    api._settings["proxy"] = "ftp://127.0.0.1:21"
    result = api.check_connection()
    assert result["ok"] is False and "Неверный адрес прокси" in result["error"]


def test_check_connection_against_the_mocks(api, ws_server, http_server, monkeypatch):
    paths = []

    async def handler(ws):
        paths.append(ws.request.path)
        await ws.wait_closed()

    ws_server.handler = handler
    http_server.routes[("GET", "/cdn-cgi/trace")] = (200, b"ip=203.0.113.7\nloc=NL\ncolo=AMS\n")
    monkeypatch.setattr(netcheck, "INWORLD_TTS", soniox_engine.TTS_URL.replace("soniox-tts", "inworld-tts"))
    monkeypatch.setenv(app.KEY_ENVS["inworld"], "inworld-key")
    api._settings["proxy"] = "none"  # the mocks are local, never through the VPN
    result = api.check_connection()
    assert result["ok"] and result["exit"] == {"loc": "NL", "colo": "AMS", "ip": "203.0.113.7"}
    assert all(p["ping_ms"] is not None for p in result["probes"]), result["probes"]
    assert sorted(paths) == ["/inworld-tts", "/soniox-stt", "/soniox-stt-eu", "/soniox-tts"]
    assert result["hint"].startswith("Связь хорошая")
