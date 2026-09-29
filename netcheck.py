"""
Connection check («Проверить связь»): how fast the VPN route reaches the speech services.

  ws_rtt        opens a websocket and times ping/pong round trips (what every spoken clause pays twice)
  exit_location asks Cloudflare where the VPN comes out to the internet
  hint          turns the numbers into advice in Russian

Nothing here raises on a network failure: a probe that fails reports it in its "error" field.
"""
import asyncio
import os
import statistics
import threading
import time

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from voice_clone import https_request

TRACE_URL = os.environ.get("LIVE_TRANSLATOR_TRACE_URL", "https://www.cloudflare.com/cdn-cgi/trace")
SONIOX_EU_STT = os.environ.get("LIVE_TRANSLATOR_SONIOX_EU_STT", "wss://stt-rt.eu.soniox.com/transcribe-websocket")
SONIOX_EU_TTS = "wss://tts-rt.eu.soniox.com/tts-websocket"
INWORLD_TTS = os.environ.get("LIVE_TRANSLATOR_INWORLD_TTS", "wss://api.inworld.ai/tts/v1/voice:streamBidirectional")
TIMEOUT = 10.0  # seconds for one probe: opening the connection and all its pings
SLOW_MS = 150   # round trip above this: a VPN server closer to the services is worth it
EU_GAIN_MS = 20  # the EU region has to win by at least this much to be worth mentioning
OPTIONAL = {"soniox_eu"}  # probes whose failure is not a problem for the app
AUTH_CODES = (401, 403)  # the service answered through the VPN and refused the key it was sent


def _ms(start):
    return round((time.perf_counter() - start) * 1000)


def _describe(error):
    if isinstance(error, InvalidStatus):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):  # two classes before Python 3.11
        return "нет ответа"
    return (str(error) or type(error).__name__)[:160]


async def _pings(url, proxy, headers, pings, result):
    start = time.perf_counter()
    async with connect(url, additional_headers=headers, proxy=proxy, compression=None) as ws:
        result["open_ms"] = _ms(start)
        rtts = []
        for _ in range(pings):
            start = time.perf_counter()
            await (await ws.ping())
            rtts.append(_ms(start))
    return rtts


async def ws_rtt(url, proxy, headers=None, pings=3):
    """{"open_ms", "ping_ms" (median), "error", "rejected"}: an unmeasured value is None, a failure is in
    "error", "rejected" is the HTTP status (AUTH_CODES) of a service that answered but refused the key.
    A probe sent without a key (headers None) is never "rejected": its 401/403 is a geo block or a WAF."""
    result = {"open_ms": None, "ping_ms": None, "error": None, "rejected": None}
    try:  # wait_for, not asyncio.timeout: start.bat runs any Python 3.10+
        rtts = await asyncio.wait_for(_pings(url, proxy, headers, pings, result), TIMEOUT)
        result["ping_ms"] = round(statistics.median(rtts))
    except Exception as e:  # a diagnostic: whatever breaks is the answer
        if headers and isinstance(e, InvalidStatus) and e.response.status_code in AUTH_CODES:
            result["rejected"] = e.response.status_code
        result["error"] = (f"ключ отклонён — вставьте новый (HTTP {result['rejected']})" if result["rejected"]
                           else _describe(e))
    return result


def exit_location(proxy):
    """Where the VPN exits: {"loc": "DE", "colo": "FRA", "ip": "..."} or {"error": "..."}."""
    try:
        status, data = https_request("GET", TRACE_URL, {}, None, proxy)
    except Exception as e:
        return {"error": _describe(e)}
    if status != 200:
        return {"error": f"HTTP {status}"}
    fields = dict(line.partition("=")[::2] for line in data.decode("utf-8", "replace").splitlines())
    return {name: fields.get(name, "") for name in ("loc", "colo", "ip")}


async def _exit_location(proxy):
    """exit_location on a daemon thread: its request may hang for a minute, the check waits TIMEOUT."""
    loop = asyncio.get_running_loop()
    answer = loop.create_future()

    def work():
        result = exit_location(proxy)
        try:
            loop.call_soon_threadsafe(lambda: answer.done() or answer.set_result(result))
        except RuntimeError:  # the check is over and its loop closed
            pass

    threading.Thread(target=work, daemon=True).start()
    try:
        return await asyncio.wait_for(answer, TIMEOUT)
    except asyncio.TimeoutError:
        return {"error": "нет ответа"}


def _region(pid, result):
    """OpenAI answers 403 to a country it does not serve (Russia without a VPN), whatever the key."""
    if pid == "openai" and result["rejected"] == 403:
        return {**result, "error": "недоступен из этой страны — включите VPN (HTTP 403)"}
    return result


async def check(probes, proxy):
    """probes: [(id, label, url, headers)] -> {"exit": exit_location,
    "probes": [{id, label, open_ms, ping_ms, error, rejected}]}."""
    results = await asyncio.gather(_exit_location(proxy),
                                   *(ws_rtt(url, proxy, headers) for _, _, url, headers in probes))
    return {"exit": results[0],
            "probes": [{"id": pid, "label": label, **_region(pid, r)}
                       for (pid, label, _, _), r in zip(probes, results[1:])]}


def hint(result):
    """Advice for the settings page, from the result of check()."""
    probes = {p["id"]: p for p in result["probes"]}
    ok = [p for p in result["probes"] if p["ping_ms"] is not None]
    if not ok:
        return "Нет связи с сервисами перевода: включите VPN или проверьте прокси."
    main = probes["soniox_stt"] if probes.get("soniox_stt", {}).get("ping_ms") is not None else ok[0]
    rtt = main["ping_ms"]
    if rtt > SLOW_MS:
        where = result.get("exit", {}).get("loc")
        text = (f"Задержка до {main['label']} {rtt} мс — это много{f' (VPN выходит в {where})' if where else ''}: "
                "выберите сервер VPN в Европе (Германия, Нидерланды, Финляндия).")
    else:
        text = f"Связь хорошая: {rtt} мс до {main['label']}."
    eu = probes.get("soniox_eu", {}).get("ping_ms")
    if main["id"] == "soniox_stt" and eu is not None and rtt - eu >= EU_GAIN_MS:
        text += (f" Soniox EU быстрее на {rtt - eu} мс: ⚙ Настройки → Интернет → «Регион Soniox» → Европа "
                 "(нужен проект Soniox в регионе EU).")
    failed = [p["label"] for p in result["probes"]
              if p["ping_ms"] is None and not p.get("rejected") and p["id"] not in OPTIONAL]
    if failed:
        text += f" Не отвечают: {', '.join(failed)}."
    for p in result["probes"]:
        if p.get("rejected"):  # answered through the VPN: the key or the country is the problem
            text += f" {p['label']}: {p['error']}."
    return text
