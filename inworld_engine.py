"""
Inworld TTS as an alternative voice for the Soniox engine (off by default).

One websocket, a context per clause. There is no warm stream: create, text and close go out back
to back without waiting for contextCreated, and the server processes them in order, so a clause
costs one round trip. Voices: Inworld's built-in ones (e.g. "Clive") or an instant clone of mine.
"""
import base64
import json
import os
import re
import time

from python_socks import ProxyError
from websockets.asyncio.client import connect

from soniox_engine import SonioxVoice, render_once
from voice_clone import CloneError, https_request

TTS_URL = os.environ.get("LIVE_TRANSLATOR_INWORLD_TTS", "wss://api.inworld.ai/tts/v1/voice:streamBidirectional")
API_URL = os.environ.get("LIVE_TRANSLATOR_INWORLD_API", "https://api.inworld.ai")
KEY_ENV = "INWORLD_API_KEY"
DEFAULT_MODEL = "inworld-tts-2-flash"
DEFAULT_VOICE = "Clive"

# gRPC status code -> (the HTTP-like code the voice core understands, error type)
STATUS = {16: (401, "unauthenticated"), 7: (403, "permission_denied"), 5: (400, "voice_not_found"),
          8: (429, "resource_exhausted"), 4: (408, "request_timeout"),
          14: (503, "unavailable"), 13: (500, "internal"), 10: (503, "aborted")}
QUOTA = re.compile(r"quota|credit|billing|balance|payment", re.I)  # 8 as "out of money", not "busy"
LOCALES = {"en": "en-US", "pt": "pt-BR", "zh": "zh-CN", "ja": "ja-JP", "ko": "ko-KR", "hi": "hi-IN"}


def locale(language):
    """BCP-47 tag for a bare language code: "en" -> "en-US", "de" -> "de-DE"."""
    return language if "-" in language else LOCALES.get(language, f"{language}-{language.upper()}")


class InworldVoice(SonioxVoice):
    """My voice synthesized by Inworld TTS; everything but the wire format is SonioxVoice's."""

    PROVIDER, KEY_ENV, FATAL = "Inworld", KEY_ENV, CloneError
    WARM = False

    def __init__(self, *args, model=DEFAULT_MODEL, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = model

    def _connect(self):
        return connect(TTS_URL, additional_headers={"Authorization": f"Basic {self.api_key}"}, max_size=None,
                       proxy=self.proxy, compression=None, ping_interval=5, ping_timeout=5)

    def _open_msgs(self, st):
        audio = {"audio_encoding": "PCM", "sample_rate_hertz": 24000}
        if st.speed != 1.0:
            audio["speaking_rate"] = st.speed
        return [{"context_id": st.sid, "create": {"voice_id": self.voice, "model_id": self.model,
                                                  "audio_config": audio, "language": locale(self.language)}}]

    def _text_msgs(self, st, text, end):
        msgs = []
        if text:
            send = {"text": text, "flush_context": {}} if end else {"text": text}
            msgs.append({"context_id": st.sid, "send_text": send})
        elif end:
            msgs.append({"context_id": st.sid, "flush_context": {}})
        if end:
            msgs.append({"context_id": st.sid, "close_context": {}})
        return msgs

    def _cancel_msgs(self, st):
        return [] if st.end_sent else [{"context_id": st.sid, "close_context": {}}]

    def _keepalive_msg(self):
        return None

    def _normalize(self, msg):
        result, error = msg.get("result") or {}, msg.get("error") or {}
        status = error or result.get("status") or {}
        out = {"stream_id": result.get("contextId") or msg.get("contextId") or error.get("contextId")}
        if status.get("code"):
            code, kind = STATUS.get(status["code"], (400, ""))
            text = status.get("message") or str(status)
            if code == 429 and QUOTA.search(text):
                code, kind = 402, "quota_exceeded"  # no retry helps: fatal, like a rejected key
            out.update(error_code=code, error_type=kind, error_message=text,
                       audio_end=True, terminated=True)  # a failed context is over
        chunk = result.get("audioChunk") or {}
        audio = chunk.get("audioContent") or result.get("audioContent")
        if audio:
            out["audio"] = audio
        if "contextClosed" in result:
            out["audio_end"] = out["terminated"] = True
        return out


async def speak_once(api_key, voice, language, text, proxy, model=DEFAULT_MODEL, speed=1.0):
    """Generate one phrase (voice preview) and return PCM16 24 kHz audio."""
    return await render_once(InworldVoice(api_key, voice, language, None, proxy, None, speed=speed, model=model), text)


# --- REST: voices ---------------------------------------------------------------

def _rest(method, path, api_key, proxy, body=None):
    headers = {"Authorization": f"Basic {api_key}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body).encode()
    try:
        status, data = https_request(method, f"{API_URL}{path}", headers, body, proxy)
    except (OSError, ProxyError) as e:
        raise CloneError(f"Нет связи с Inworld ({e}). Включи VPN.") from e
    if status in (401, 403):
        raise CloneError("Inworld отклонил ключ INWORLD_API_KEY.")
    if status == 404 and method == "DELETE":
        return {}  # already gone
    if status >= 300:
        raise CloneError(f"Inworld: HTTP {status} {data[:300].decode('utf-8', 'replace')}")
    return json.loads(data) if data else {}


def create_voice(api_key, audio_bytes, proxy, filename="voice.wav"):
    """Instant clone of my voice from a 3-30 s sample; returns the voice id, ready at once.
    Inworld detects the format (WAV / MP3) and the language from the audio itself."""
    body = {"displayName": f"Live Translator {time.strftime('%Y-%m-%d %H-%M-%S')}",
            "description": "Live Translator voice",
            "voiceSamples": [{"audioData": base64.b64encode(audio_bytes).decode()}]}
    data = _rest("POST", "/voices/v1/voices:clone", api_key, proxy, body)
    voice_id = (data.get("voice") or {}).get("voiceId")
    if not voice_id:
        problems = [e.get("text", "") for s in data.get("audioSamplesValidated", ()) for e in s.get("errors", ())]
        raise CloneError("Inworld не принял образец голоса: " + ("; ".join(filter(None, problems)) or str(data)[:300]))
    return voice_id


def delete_voice(api_key, voice_id, proxy):
    """Remove a clone I no longer use."""
    _rest("DELETE", f"/voices/v1/voices/{voice_id}", api_key, proxy)


def list_voices(api_key, proxy):
    """Built-in and my own voices: [{name, gender, description, id}]."""
    voices = _rest("GET", "/voices/v1/voices?pageSize=1000", api_key, proxy).get("voices", ())
    return [{"name": v.get("displayName") or v["voiceId"], "gender": v.get("gender", ""),
             "description": v.get("description", ""), "id": v["voiceId"]} for v in voices]
