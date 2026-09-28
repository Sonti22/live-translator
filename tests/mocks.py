"""
Local stand-ins for the OpenAI / Soniox / Cartesia servers and for the engine's callbacks.

The servers run on their own threads (and, for websockets, their own event loop), like a remote API:
tests drive the client code in the pytest-asyncio loop and script the server side with a handler.
"""
import asyncio
import base64
import json
import re
import socket
import threading
import time
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def b64(data):
    return base64.b64encode(data).decode()


async def until(predicate, timeout=5.0, what="condition"):
    """Poll `predicate`; works both in the test loop and in the mock server's loop."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


async def stop(task):
    """Cancel a client task and re-raise anything it failed with."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def read_until(ws, msgs, done):
    """Server side: append the client's JSON messages to `msgs` until `done(msgs)`."""
    while not done(msgs):
        msgs.append(json.loads(await ws.recv()))


def form_fields(content_type, body):
    """Strict little multipart/form-data parser: field name -> (part headers, value bytes)."""
    boundary = content_type.split("boundary=", 1)[1].encode()
    assert body.endswith(b"--" + boundary + b"--\r\n")
    fields = {}
    for part in body.split(b"--" + boundary)[1:-1]:
        head, _, value = part.removeprefix(b"\r\n").partition(b"\r\n\r\n")
        assert value.endswith(b"\r\n")
        head = head.decode()
        fields[re.search(r'name="([^"]*)"', head).group(1)] = (head, value[:-2])
    return fields


class MockWS:
    """A websockets server on its own thread and event loop.

    Each test sets `handler` (`async def handler(ws)`, run for every connection) and optionally
    `reject` (an HTTP status that refuses the handshake). Handler errors land in `errors`."""

    def __init__(self, port):
        self.port = port
        self.handler = self.reject = None
        self.errors = []
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        self._thread = threading.Thread(target=self.loop.run_until_complete, args=(self._serve(ready),),
                                        daemon=True)
        self._thread.start()
        if not ready.wait(5):
            raise RuntimeError("mock websocket server did not start")

    async def _serve(self, ready):
        self._stop = asyncio.get_running_loop().create_future()
        async with serve(self._handle, "127.0.0.1", self.port, process_request=self._process_request,
                         max_size=None, compression=None):
            ready.set()
            await self._stop

    def close(self):
        self.loop.call_soon_threadsafe(self._stop.set_result, None)
        self._thread.join(5)

    def reset(self):
        self.handler = self.reject = None
        self.errors = []

    def _process_request(self, connection, request):
        if self.reject:
            return connection.respond(self.reject, "rejected by the mock\n")
        return None

    async def _handle(self, ws):
        errors, handler = self.errors, self.handler  # a late connection reports to its own test
        if handler is None:
            errors.append(AssertionError(f"unexpected connection to {ws.request.path}"))
            return
        try:
            await handler(ws)
        except ConnectionClosed:
            pass
        except Exception as e:
            errors.append(e)


Request = namedtuple("Request", "method path headers body")


class MockHTTP(ThreadingHTTPServer):
    """HTTP server on its own thread: `routes[(method, path)] = (status, body)`, requests recorded."""

    daemon_threads = True

    def __init__(self, port):
        super().__init__(("127.0.0.1", port), _HTTPHandler)
        self.routes, self.requests = {}, []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def reset(self):
        self.routes, self.requests = {}, []

    def close(self):
        self.shutdown()
        self.server_close()


class _HTTPHandler(BaseHTTPRequestHandler):
    def _reply(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.requests.append(Request(self.command, self.path, self.headers, body))
        status, payload = self.server.routes.get((self.command, self.path), (404, {"error": "no route"}))
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = do_CONNECT = _reply

    def log_message(self, format, *args):
        pass


class FakeSink:
    """Records what the engine reports (live_translator.Sink interface)."""

    def __init__(self):
        self.captions, self.notes, self.statuses, self.lags = [], [], [], []

    def caption(self, kind, label, text, speaker=None):
        self.captions.append((kind, label, text) if speaker is None else (kind, label, text, speaker))

    def note(self, text):
        self.notes.append(text)

    def status(self, label, text, ok):
        self.statuses.append((label, text, ok))

    def lag(self, seconds):
        self.lags.append(seconds)


class FakeVoice:
    """Records what a translation channel asks the TTS voice to do."""

    def __init__(self):
        self.said, self.ends = [], 0

    async def say(self, text, end=False):
        self.said.append(text)
        if end:
            self.ends += 1

    async def end_utterance(self):
        self.ends += 1


class FakePlayer:
    """Stands in for live_translator.Player (no audio device)."""

    def __init__(self):
        self.fed = []

    def feed(self, pcm):
        self.fed.append(pcm)
