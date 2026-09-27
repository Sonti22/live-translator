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
In progress: mock test suite on branch `tests/mock-suite` (helper agent) — don't start a second one.

## Open tasks for cloud agents
1. **Speaker separation** (Transync beta): enable `enable_speaker_diarization` for the other side's Soniox
   channel, show "Собеседник 1/2" labels in `ui/app.js` entries and in saved transcripts. Files:
   `soniox_engine.py` (config + token `speaker` field -> Sink), `app.py` (`compose_transcript`), `ui/app.js`.
2. **Transcript editing before notes** (Transync v2.2): in the record view let the user edit lines of the
   saved transcript, save back to `records/<name>.txt`, then regenerate notes. Files: `app.py` records
   section, `ui/index.html` + `ui/app.js` record view only.
3. **UI localisation check**: every user-facing string in Russian, no truncated labels at 1024x640 window;
   fix CSS in `ui/app.css` only.
