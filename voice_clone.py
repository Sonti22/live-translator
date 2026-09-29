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


def json_frame(raw):
    """A server frame as a dict; None for anything else (text that is not JSON, a binary frame, a list, a number).
    The frame is never logged: it may carry what I said."""
    try:
        msg = json.loads(raw)
    except (ValueError, RecursionError):  # JSONDecodeError, UnicodeDecodeError
        return None
    return msg if isinstance(msg, dict) else None


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
            # python_socks knows socks5 and socks4 only; "h" / "a" (the proxy resolves the name) is its rdns flag
            plain = {"socks5h": "socks5", "socks4a": "socks4"}.get(scheme, scheme)
            try:
                sock = Proxy.from_url(plain + proxy[len(scheme):], rdns=scheme in ("socks5h", "socks4a")).connect(
                    dest_host=u.hostname, dest_port=port, timeout=30)
            except ValueError as e:  # no port, no host: the address is unusable, and it may hold a password
                raise CloneError(f"Неверный адрес прокси: {proxy.rpartition('@')[2]}") from e
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
    """One Cartesia connection; each translated phrase is a context, played strictly in order. Text said while the
    connection is down waits for it, and a reconnect keeps every phrase not heard yet."""

    IDLE = 0.7  # close a phrase after this long without new text
    TTL = 10.0  # a phrase that waited offline longer than this is dropped at the reconnect

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
        self.said = {}            # context -> [its text so far, when it began]
        self.ended = set()        # contexts whose phrase is over
        self.unsent = set()       # contexts the connection doesn't have: sent in full once it is up

    async def run(self):
        while True:
            try:
                async with connect(TTS_URL, additional_headers={"X-API-Key": self.api_key}, max_size=None,
                                   proxy=self.proxy, compression=None) as ws:
                    self.ws = ws
                    await self._resend()
                    self.sink.status("Мой голос", "подключено", True)
                    async for raw in ws:
                        msg = json_frame(raw)
                        if msg is not None:  # a frame that is not an object is skipped, the session goes on
                            self._on_message(msg)
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
        if cid is not None and not isinstance(cid, str):
            return
        if kind == "chunk" and cid in self.pending:
            try:
                pcm = base64.b64decode(msg["data"])
            except (KeyError, TypeError, ValueError):  # no audio, or not base64: this frame is lost, the phrase is not
                return
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
        """Play what the head phrase buffered; once it finished, move on to the next one."""
        while self.order:
            head = self.order[0]
            for pcm in self.pending[head]:
                self.play(pcm)
            self.pending[head] = []
            if head not in self.finished:
                return
            self._forget(head)

    def _forget(self, cid):
        if cid in self.order:
            self.order.remove(cid)
        self.pending.pop(cid, None)
        self.said.pop(cid, None)
        for contexts in (self.finished, self.heard, self.ended, self.unsent):
            contexts.discard(cid)
        if self.context == cid:
            self.context, self.text = None, ""

    def _reset(self):
        """The connection is gone: the phrase cut off while heard is dropped, one whose audio arrived in full plays in
        its turn, the others go again in full after the reconnect."""
        head = self.order[0] if self.order else None
        for cid in list(self.order):
            if cid == head and cid in self.heard:
                self._forget(cid)
            elif cid not in self.finished:
                self.pending[cid] = []
                self.heard.discard(cid)
                self.unsent.add(cid)
        self._advance()

    async def _resend(self):
        """Phrases said while the connection was down go out now, in speaking order; stale ones are dropped."""
        now = time.monotonic()
        for cid in list(self.order):
            if now - self.said[cid][1] > self.TTL:
                self.sink.note(f"[Мой голос] не озвучено (не было связи): {self.said[cid][0].strip()}")
                self._forget(cid)
        self._advance()
        for cid in list(self.order):
            if cid in self.unsent:
                self.unsent.discard(cid)  # before the await: what is said meanwhile follows it
                await self._send(cid, self.said[cid][0], cid not in self.ended)

    def _request(self, cid, transcript, cont):
        return json.dumps({
            "model_id": TTS_MODEL, "transcript": transcript, "voice": self.voice_id,
            "language": self.language, "context_id": cid,
            "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000},
            "continue": cont, "max_buffer_delay_ms": self.buffer_ms,
        })

    async def _send(self, cid, transcript, cont):
        if self.ws is not None:
            try:
                await self.ws.send(self._request(cid, transcript, cont))
            except ConnectionClosed:
                pass  # run() notices: the phrase goes again after the reconnect unless it was heard in part

    async def say(self, delta, end=False):
        """Feed a translated text delta; speech starts as soon as Cartesia has enough of it."""
        if not delta:
            return
        if self.context is None:
            self.context = uuid.uuid4().hex
            self.order.append(self.context)
            self.pending[self.context] = []
            self.said[self.context] = ["", time.monotonic()]
            if self.ws is None:
                self.unsent.add(self.context)  # the connection is being made again: it goes out once it is up
        cid = self.context
        self.said[cid][0] += delta
        self.text += delta
        self.last_text = time.monotonic()
        if cid not in self.unsent:
            await self._send(cid, delta, True)
        if end or self.text.rstrip().endswith(SENTENCE_END):
            await self.end_phrase()

    async def end_phrase(self):
        cid, self.context, self.text = self.context, None, ""
        if cid is None:
            return
        self.ended.add(cid)
        if cid not in self.unsent:
            await self._send(cid, "", False)

    async def cancel_all(self):
        """Mute: stop everything that is queued or being generated."""
        if self.ws is not None:
            for cid in list(self.order):
                try:
                    await self.ws.send(json.dumps({"context_id": cid, "cancel": True}))
                except ConnectionClosed:
                    break
        self.context, self.text = None, ""
        for contexts in (self.order, self.pending, self.finished, self.heard, self.said, self.ended, self.unsent):
            contexts.clear()

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
