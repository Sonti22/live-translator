"""
Cartesia Sonic as the voice of the Soniox engine.

Measured through the same VPN, Cartesia's server answers in ~70 ms against Soniox TTS's ~220-300 ms,
so each clause starts ~0.2 s sooner. One websocket, a context per chunk of translation that closes:
a clause with the "fast" delivery, a whole sentence with the others (its chunks are continuations of one
context, so the voice keeps its intonation across a comma). Text that ends the chunk is generated at once
(no server-side buffering); text still streaming in (sub-word STT tokens) is buffered briefly (longer with
the patient deliveries), so Cartesia never speaks half a word. Its audio never waits for a close, so with the
patient deliveries a context stays open (OPEN_S) across the pause to the next clause: the intonation of a comma is
not reset. Soniox and Inworld speak only what a close releases and cannot wait like that. Cloning, deleting and
previews are voice_clone's (create_clone, delete_clone, speak_once).
"""
import json

from python_socks import ProxyError
from websockets.asyncio.client import connect

import voice_clone
from soniox_engine import SonioxVoice
from voice_clone import CloneError, https_request

FORMAT = {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000}
GENDERS = {"masculine": "male", "feminine": "female", "gender_neutral": "neutral"}
VOICE_ERRORS = ("voice_not_found", "invalid_voice_id")
PARTIAL_BUFFER_MS = voice_clone.BUFFER_MS["instant"]  # a clause still coming in may wait this long for more...
BUFFER_MS = {"fast": PARTIAL_BUFFER_MS, "balanced": 200, "natural": 400}  # ...by delivery
OPEN_S = {"balanced": 1.5, "natural": 2.5}  # s a context waits for more text before it is closed (its FLUSH)


class CartesiaVoice(SonioxVoice):
    """My voice synthesized by Cartesia; everything but the wire format is SonioxVoice's."""

    PROVIDER, KEY_ENV, FATAL = "Cartesia", voice_clone.KEY_ENV, CloneError
    WARM = False

    def __init__(self, *args, model=voice_clone.TTS_MODEL, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = model
        self.FLUSH = OPEN_S.get(self.delivery, self.FLUSH)

    def _connect(self):
        return connect(voice_clone.TTS_URL, additional_headers={"X-API-Key": self.api_key}, max_size=None,
                       proxy=self.proxy, compression=None, ping_interval=5, ping_timeout=5)

    def _open_msgs(self, st):
        return []  # a context starts with its first transcript

    def _text_msgs(self, st, text, end):
        msg = {"model_id": self.model, "transcript": text, "voice": {"mode": "id", "id": self.voice},
               "language": self.language, "context_id": st.sid, "output_format": FORMAT,
               "continue": not end, "max_buffer_delay_ms": 0 if end else BUFFER_MS[self.delivery]}
        config = {}
        if self._tempo(st) != 1.0:
            config["speed"] = self._tempo(st)
        if st.tone[1] != 1.0:
            config["volume"] = round(st.tone[1], 2)
        if config:
            msg["generation_config"] = config
        return [msg]

    def _cancel_msgs(self, st):
        return [{"context_id": st.sid, "cancel": True}]

    def _keepalive_msg(self):
        return None

    def _normalize(self, msg):
        kind, sid = msg.get("type"), msg.get("context_id")
        if kind == "chunk":
            return {"stream_id": sid, "audio": msg.get("data")}
        if kind == "done":
            return {"stream_id": sid, "audio_end": True, "terminated": True}
        if kind == "error":
            code = msg.get("error_code") or ""
            text = ": ".join(filter(None, (msg.get("title"), msg.get("message") or msg.get("error")))) or str(msg)
            return {"stream_id": sid, "error_code": msg.get("status_code") or 400,
                    "error_type": "voice_not_found" if code in VOICE_ERRORS else code,
                    "error_message": text, "audio_end": True, "terminated": True}
        return None  # timestamps, flush_done


def list_voices(api_key, proxy):
    """Voices on my Cartesia account and in its library: [{name, gender, description, id}]."""
    headers = {"X-API-Key": api_key, "Cartesia-Version": voice_clone.CARTESIA_VERSION}
    try:
        status, data = https_request("GET", f"{voice_clone.TTS_API}/voices?limit=100", headers, None, proxy)
    except (OSError, ProxyError) as e:
        raise CloneError(f"Нет связи с Cartesia ({e}). Включи VPN.") from e
    if status in (401, 403):
        raise CloneError("Cartesia отклонила ключ CARTESIA_API_KEY.")
    if status >= 300:
        raise CloneError(f"Cartesia: HTTP {status} {data[:300].decode('utf-8', 'replace')}")
    body = json.loads(data)
    voices = body.get("data", ()) if isinstance(body, dict) else body
    return [{"name": v.get("name") or v["id"], "gender": GENDERS.get(v.get("gender"), v.get("gender") or ""),
             "description": v.get("description") or "", "id": v["id"], "language": v.get("language") or ""}
            for v in voices]


def default_voice(api_key, proxy):
    """A male English library voice for when none was picked yet (the user speaks as a man)."""
    voices = [v for v in list_voices(api_key, proxy) if v["language"].startswith("en")]
    male = [v for v in voices if v["gender"] == "male"]
    return (male or voices or [{"id": None}])[0]["id"]
