# Live Translator — notes for Claude agents

Windows desktop app: the user speaks Russian, the call (Zoom / Meet / Telegram) hears English in the
user's cloned voice through the VB-Cable virtual microphone; the other side's speech is shown as
Russian subtitles. Modeled on Transync AI (UI and features), with ideas from JotMe, Sokuji, LiveTranslate.

## Layout
- `live_translator.py` — engine (`Engine`, `Channel`, `Player`, `LagMeter`, `Sink`, loopback capture,
  proxy detection) + console mode. Engine reports through a `Sink` (captions, notes, status, lag, level).
- `voice_clone.py` — Cartesia cloned-voice streaming TTS (`CloneVoice`), `create_clone`, `https_request`
  (HTTPS through the user's SOCKS VPN proxy).
- `soniox_engine.py` — (in progress, owned by the local session) Soniox STT+translation and TTS.
- `app.py` — pywebview window; `Api` = methods the page calls (`window.pywebview.api.*`), `Bus` = event
  queue polled by the page every 100 ms. Settings in `settings.json`, keys in `.env`, transcripts in `records/`.
- `ui/` — `index.html`, `app.css` (dark Transync-like theme), `app.js`, `overlay.html` (floating subtitles).

## Rules
- UI text in Russian; code, comments, commits in English. Match the existing style; no new frameworks.
- Never commit `.env`, `settings.json`, `records/`, `dist/`, `build/`, logs.
- Audio devices, VB-Cable and WASAPI loopback exist only on the user's Windows PC. In the cloud, test with
  mocks: local websocket servers standing in for OpenAI/Soniox/Cartesia (URLs are overridable through
  `LIVE_TRANSLATOR_URL`, `LIVE_TRANSLATOR_TTS_URL`, `LIVE_TRANSLATOR_TTS_API`) and a mock `pywebview.api`
  object for the UI.
- One task = one branch = one PR against `main`. Don't touch files owned by another task.

## Open tasks for cloud agents
1. **AI meeting notes** (Transync "AI meeting notes"): after ■, if an OpenAI key is set, send the saved
   transcript to OpenAI chat and store title, short summary, decisions/action items next to the record
   (`records/<name>.json`); show the title in the records drawer and a "Протокол" view; export .md.
   Check the current recommended small chat model in OpenAI docs. Files: `app.py` (records section),
   `ui/app.js` + `ui/index.html` (drawer only).
2. **Floating subtitles**: pause/resume translation from `ui/overlay.html`, remember window position and
   size between runs (pywebview window events → `settings.json`). Files: `ui/overlay.html`,
   `app.py` (`toggle_overlay` and new small API methods only).
3. **Test suite**: `tests/` with pytest — mock servers for OpenAI translate and Cartesia (see the protocol
   in `live_translator.run_session` and `voice_clone.CloneVoice`), unit tests for `compose_transcript`,
   `LagMeter`, `Bus.since`, `detect_proxy`. Must run without audio hardware (skip device tests if no
   `CABLE` device). No production code changes except tiny testability hooks.
