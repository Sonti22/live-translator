# Live Translator — notes for Claude agents

Windows desktop app: the user speaks Russian, the call (Zoom / Meet / Telegram) hears English in the
user's cloned voice through the VB-Cable virtual microphone; the other side's speech is shown as
Russian subtitles. Modeled on Transync AI (UI and features), with ideas from JotMe, Sokuji, LiveTranslate.

## Layout
- `live_translator.py` — engine (`Engine`, `Channel`, `Player`, `LagMeter`, `Sink`, loopback capture,
  proxy detection) + console mode. Engine reports through a `Sink` (captions, notes, status, lag, level).
- `voice_clone.py` — Cartesia cloned-voice streaming TTS (`CloneVoice`), `create_clone`, `https_request`
  (HTTPS through the user's SOCKS VPN proxy).
- `soniox_engine.py` — Soniox STT+translation and streaming TTS in the cloned voice (default engine).
- `meeting_notes.py` — AI meeting notes via OpenAI Responses API.
- `app.py` — pywebview window; `Api` = methods the page calls (`window.pywebview.api.*`), `Bus` = event
  queue polled by the page every 100 ms. Settings in `settings.json`, keys in `.env`, transcripts in `records/`.
- `ui/` — `index.html`, `app.css` (dark Transync-like theme), `app.js`, `overlay.html` (floating subtitles).

## Rules
- UI text in Russian; code, comments, commits in English. Match the existing style; no new frameworks.
- Never commit `.env`, `settings.json`, `records/`, `dist/`, `build/`, logs.
- Audio devices, VB-Cable and WASAPI loopback exist only on the user's Windows PC. In the cloud, test with
  mocks: local websocket servers standing in for OpenAI/Soniox/Cartesia (URLs are overridable through
  `LIVE_TRANSLATOR_URL`, `LIVE_TRANSLATOR_TTS_URL`, `LIVE_TRANSLATOR_TTS_API`, `LIVE_TRANSLATOR_SONIOX_STT`,
  `LIVE_TRANSLATOR_SONIOX_TTS`, `LIVE_TRANSLATOR_SONIOX_API`, `LIVE_TRANSLATOR_OPENAI_API`) and a mock `pywebview.api`
  object for the UI.
- One task = one branch = one PR against `main`. Don't touch files owned by another task.

## Status
Done (local session): Soniox engine (default) with cloned voice and AI-assistant context, OpenAI engine
(+ Cartesia clone), voice recorder/clone/preview UI, AI assistant modal, JotMe-style modes, VB-CABLE check
and wizard, AI meeting notes (`meeting_notes.py`, gpt-6-luna), floating subtitles pause/resume + saved
position, simple/advanced settings.
Measured for real (tools/latency_test.py, 10.6 s Russian phrase): Soniox speaks each translated clause
~1 s after it is final; the last English word comes ~5 s after the phrase (OpenAI ~6 s). A global audit
(lifecycle races, reconnects, proxies, settings, UI) is fixed; regressions live in tests/test_robustness.py.
Tests: `py -3 -m pytest` — suite with local mock servers (`tests/`), no keys, network or audio devices
needed. Keep them green; add tests next to the module you change.

## Open tasks for cloud agents
None right now. Also done: speaker separation («Собеседник 1 / 2»), transcript editing before notes,
1024x640 layout, a warm Soniox TTS stream kept ready before every utterance.
Next ideas (ask the user first): multi-target translation, presentation mode with a share link.
