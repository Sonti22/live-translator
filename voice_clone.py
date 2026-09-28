"""
"My voice" mode: the translated text is spoken in the user's cloned voice (Cartesia Sonic).

gpt-realtime-translate streams the English text while you are still talking; each text delta goes
straight into a Cartesia streaming context, so speech starts after a few words instead of after
the whole sentence.
"""
import asyncio
import base64
import http.client
import json
import os
import socket
import ssl
import time
import uuid
from collections import deque
from urllib.parse import unquote, urlsplit

from python_socks import ProxyError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, WebSocketException

CARTESIA_VERSION = "2026-08-14"
TTS_MODEL = "sonic-3.6"
TTS_URL = os.environ.get("LIVE_TRANSLATOR_TTS_URL",
                         f"wss://api.cartesia.ai/tts/websocket?cartesia_version={CARTESIA_VERSION}")
TTS_API = os.environ.get("LIVE_TRANSLATOR_TTS_API", "https://api.cartesia.ai")
KEY_ENV = "CARTESIA_API_KEY"
SENTENCE_END = (".", "?", "!", "…")

# "Задержка воспроизведения": how long Cartesia may wait for more text before speaking
BUFFER_MS = {"instant": 150, "balanced": 500, "smooth": 1200}


class CloneError(Exception):
    pass


def https_request(method, url, headers, body, proxy):
    """Minimal HTTP(S) request that also works through the SOCKS or HTTP proxy of a VPN client."""
    u = urlsplit(url)
    secure = u.scheme == "https"
    port = u.port or (443 if secure else 80)
    target = u.path + (f"?{u.query}" if u.query else "")
    connection = http.client.HTTPSConnection if secure else http.client.HTTPConnection
    scheme = urlsplit(proxy).scheme if proxy else ""
    if scheme == "https":
        raise CloneError("HTTPS-прокси здесь не поддерживается: укажите http:// или socks5h:// (⚙ Настройки).")
    if proxy and scheme not in ("http", "socks5h", "socks5", "socks4a", "socks4"):
        raise CloneError(f"Неверный адрес прокси: {proxy.rpartition('@')[2]}")
    if scheme == "http":
        p = urlsplit(proxy)
        conn = connection(p.hostname, p.port or 80, timeout=60)
        auth = {}
        if p.username:  # websockets sends these too, so streaming and REST behave the same
            login = f"{unquote(p.username)}:{unquote(p.password or '')}".encode()
            auth = {"Proxy-Authorization": "Basic " + base64.b64encode(login).decode()}
        if secure:
            conn.set_tunnel(u.hostname, port, headers=auth)  # CONNECT through the proxy, TLS to the real host
        else:
            target = url  # plain HTTP proxies take the absolute URL
            headers = {**headers, **auth}
    else:
        if proxy:
            from python_socks.sync import Proxy
            rdns = proxy.startswith("socks5h")
            sock = Proxy.from_url(proxy.replace("socks5h://", "socks5://"), rdns=rdns).connect(
                dest_host=u.hostname, dest_port=port, timeout=30)
        else:
            sock = socket.create_connection((u.hostname, port), timeout=30)
        sock.settimeout(60)  # connect within 30 s, but a slow answer (AI notes) may take longer
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        conn = connection(u.hostname, port, timeout=60)
        conn.sock = sock
    try:
        conn.request(method, target, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def create_clone(api_key, wav_bytes, name, language, proxy):
    """Upload a voice sample, return the new Cartesia voice id."""
    boundary = uuid.uuid4().hex
    parts = []
    for field, value in (("name", name), ("language", language), ("description", "Live Translator voice")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="clip"; filename="voice.wav"\r\n'
                 f'Content-Type: audio/wav\r\n\r\n'.encode() + wav_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    headers = {"X-API-Key": api_key, "Cartesia-Version": CARTESIA_VERSION,
               "Content-Type": f"multipart/form-data; boundary={boundary}"}
    try:
        status, data = https_request("POST", f"{TTS_API}/voices/clone", headers, b"".join(parts), proxy)
    except (OSError, ProxyError) as e:
        raise CloneError(f"Нет связи с Cartesia ({e}). Включи VPN.") from e
    if status in (401, 403):
        raise CloneError("Cartesia отклонила ключ (или нужен тариф Pro для клонирования голоса).")
    if status != 200:
        raise CloneError(f"Cartesia: HTTP {status} {data[:300].decode('utf-8', 'replace')}")
    return json.loads(data)["id"]


def delete_clone(api_key, voice_id, proxy):
    """Remove a replaced clone of my voice from the Cartesia account."""
    headers = {"X-API-Key": api_key, "Cartesia-Version": CARTESIA_VERSION}
    try:
        status, data = https_request("DELETE", f"{TTS_API}/voices/{voice_id}", headers, None, proxy)
    except (OSError, ProxyError) as e:
        raise CloneError(f"Нет связи с Cartesia ({e}).") from e
    if status >= 300 and status != 404:
        raise CloneError(f"Cartesia: HTTP {status} {data[:200].decode('utf-8', 'replace')}")


class CloneVoice:
    """One Cartesia connection; each translated phrase is a context, played strictly in order."""

    IDLE = 0.7  # close a phrase after this long without new text

    def __init__(self, api_key, voice_id, language, play, proxy, buffer_ms, sink, on_first_audio=None):
        self.api_key, self.voice_id, self.language = api_key, voice_id, language
        self.play, self.proxy, self.buffer_ms, self.sink = play, proxy, buffer_ms, sink
        self.on_first_audio = on_first_audio
        self.ws = None
        self.context = None       # context currently receiving text
        self.text = ""
        self.last_text = 0.0
        self.order = deque()      # contexts in speaking order
        self.pending = {}         # context -> buffered audio while an earlier phrase is still speaking
        self.finished = set()
        self.heard = set()        # contexts that already produced audio

    async def run(self):
        while True:
            try:
                async with connect(TTS_URL, additional_headers={"X-API-Key": self.api_key}, max_size=None,
                                   proxy=self.proxy, compression=None) as ws:
                    self.ws = ws
                    self.sink.status("Мой голос", "подключено", True)
                    async for raw in ws:
                        self._on_message(json.loads(raw))
            except InvalidStatus as e:
                code = e.response.status_code
                if code in (401, 403):
                    raise CloneError("Cartesia отклонила ключ CARTESIA_API_KEY.")
                self.sink.status("Мой голос", f"HTTP {code}, переподключение…", False)
            except (ConnectionClosed, OSError, ProxyError, InvalidHandshake) as e:
                self.sink.status("Мой голос", "нет связи, переподключение…", False)
                self.sink.note(f"[Мой голос] {e}")
            finally:
                self.ws = None
                self._reset()
            await asyncio.sleep(1)

    def _on_message(self, msg):
        kind, cid = msg.get("type"), msg.get("context_id")
        if kind == "chunk" and cid in self.pending:
            pcm = base64.b64decode(msg["data"])
            if cid not in self.heard:
                self.heard.add(cid)
                if self.on_first_audio:
                    self.on_first_audio()
            if self.order and self.order[0] == cid:
                self.play(pcm)
            else:
                self.pending[cid].append(pcm)
        elif kind == "done" and cid in self.pending:
            self.finished.add(cid)
            self._advance()
        elif kind == "error":
            self.sink.note(f"[Мой голос] {msg.get('title', '')}: {msg.get('message', msg)}")
            if msg.get("error_code") in ("voice_not_found", "invalid_voice_id"):
                self.sink.status("Мой голос", "клон не найден — запиши голос заново", False)
            if cid in self.pending:
                self.finished.add(cid)
                self._advance()

    def _advance(self):
        """The head phrase finished: start playing the next one, including audio it already buffered."""
        while self.order and self.order[0] in self.finished:
            done = self.order.popleft()
            self.pending.pop(done, None)
            self.finished.discard(done)
            self.heard.discard(done)
            if self.order:
                for pcm in self.pending.get(self.order[0], ()):
                    self.play(pcm)
                if self.order[0] in self.pending:
                    self.pending[self.order[0]] = []

    def _reset(self):
        self.context, self.text = None, ""
        self.order.clear()
        self.pending.clear()
        self.finished.clear()
        self.heard.clear()

    def _request(self, transcript, cont):
        return json.dumps({
            "model_id": TTS_MODEL, "transcript": transcript, "voice": self.voice_id,
            "language": self.language, "context_id": self.context,
            "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000},
            "continue": cont, "max_buffer_delay_ms": self.buffer_ms,
        })

    async def say(self, delta, end=False):
        """Feed a translated text delta; speech starts as soon as Cartesia has enough of it."""
        if self.ws is None or not delta:
            return
        if self.context is None:
            self.context = uuid.uuid4().hex
            self.order.append(self.context)
            self.pending[self.context] = []
        self.text += delta
        self.last_text = time.monotonic()
        try:
            await self.ws.send(self._request(delta, True))
            if end or self.text.rstrip().endswith(SENTENCE_END):
                await self.end_phrase()
        except ConnectionClosed:
            pass

    async def end_phrase(self):
        if self.ws is not None and self.context is not None:
            try:
                await self.ws.send(self._request("", False))
            except ConnectionClosed:
                pass
        self.context, self.text = None, ""

    async def cancel_all(self):
        """Mute: stop everything that is queued or being generated."""
        if self.ws is not None:
            for cid in list(self.order):
                try:
                    await self.ws.send(json.dumps({"context_id": cid, "cancel": True}))
                except ConnectionClosed:
                    break
        self._reset()

    async def watchdog(self):
        while True:
            await asyncio.sleep(0.15)
            if self.context is not None and time.monotonic() - self.last_text > self.IDLE:
                await self.end_phrase()


async def speak_once(api_key, voice_id, language, text, proxy, speed=1.0):
    """Generate one phrase (voice preview) and return its PCM16 24 kHz audio."""
    audio = bytearray()
    context = uuid.uuid4().hex
    request = {
        "model_id": TTS_MODEL, "transcript": text, "voice": voice_id, "language": language,
        "context_id": context, "continue": False,
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000},
    }
    if speed != 1.0:
        request["generation_config"] = {"speed": speed}
    try:
        async with connect(TTS_URL, additional_headers={"X-API-Key": api_key}, max_size=None,
                           proxy=proxy, compression=None) as ws:
            await ws.send(json.dumps(request))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "chunk":
                    audio += base64.b64decode(msg["data"])
                elif msg.get("type") == "done":
                    break
                elif msg.get("type") == "error":
                    raise CloneError(msg.get("message", str(msg)))
    except InvalidStatus as e:
        if e.response.status_code in (401, 403):
            raise CloneError("Cartesia отклонила ключ CARTESIA_API_KEY.") from e
        raise CloneError(f"Cartesia: HTTP {e.response.status_code}") from e
    except (OSError, ProxyError, WebSocketException) as e:
        raise CloneError(f"Нет связи с Cartesia ({e}). Включи VPN.") from e
    return bytes(audio)
