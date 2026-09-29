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
- `netcheck.py` — «Проверить связь»: websocket open/ping times, VPN exit (Cloudflare trace), advice.
- `tools/latency_test.py` — real end-to-end latency per clause through the voice's `trace` hook.
- My voice in the Soniox engine comes from `voice_provider`: Soniox TTS, Cartesia (`cartesia_engine`) or
  Inworld (`inworld_engine`); `live_translator.voice_class()` imports the optional ones lazily.
- Delivery: settings `delivery` (`fast` per clause, `balanced` whole sentences, `natural` no speeding up or
  trimming; unknown values are balanced) and `match_rate` (copy my pace and loudness) go through
  `Engine._make_voice` to the voice; `soniox_region` (`""` US / `"eu"`) is applied with
  `soniox_engine.use_region` before every engine starts and never overrides the `LIVE_TRANSLATOR_SONIOX_*`
  URLs. A Soniox clone belongs to one region. Changing them mid-call restarts the engine in a pause.
- Stealth: only synthesized English may reach the cable (`device_problems`, preview only in headphones,
  pre-call check `#callCheck`); never add fillers or pass Russian audio through.
- `ui/` — `index.html`, `app.css` (dark Transync-like theme), `app.js`, `overlay.html` (floating subtitles).
- Onboarding and hints (`ui/`): `#onboarding` (8-step first-run wizard, `settings.onboarding_done`), `#help` («?» button),
  `data-tip` tooltips (textContent only), coach marks remembered in `settings.hints_seen` (union, never erase other ids),
  settings section «Режим звонка» (`hide_from_capture`, `overlay_*`; status from `api.get_stealth_status` when the backend
  has it). Backend calls new to the UI are feature-detected (`typeof api.x === "function"`); no external requests.

## Rules
- UI text in Russian; code, comments, commits in English. Match the existing style; no new frameworks.
- Never commit `.env`, `settings.json`, `records/`, `dist/`, `build/`, logs.
- Audio devices, VB-Cable and WASAPI loopback exist only on the user's Windows PC. In the cloud, test with
  mocks: local websocket servers standing in for OpenAI/Soniox/Cartesia (URLs are overridable through
  `LIVE_TRANSLATOR_URL`, `LIVE_TRANSLATOR_TTS_URL`, `LIVE_TRANSLATOR_TTS_API`, `LIVE_TRANSLATOR_SONIOX_STT`,
  `LIVE_TRANSLATOR_SONIOX_TTS`, `LIVE_TRANSLATOR_SONIOX_API`, `LIVE_TRANSLATOR_OPENAI_API`,
  `LIVE_TRANSLATOR_SONIOX_EU_STT`, `LIVE_TRANSLATOR_TRACE_URL`, `LIVE_TRANSLATOR_INWORLD_TTS`,
  `LIVE_TRANSLATOR_INWORLD_API`) and a mock
  `pywebview.api` object for the UI.
- One task = one branch = one PR against `main`. Don't touch files owned by another task.

## Status
Done (local session): Soniox engine (default) with cloned voice and AI-assistant context, OpenAI engine
(+ Cartesia clone), voice recorder/clone/preview UI, AI assistant modal, JotMe-style modes, VB-CABLE check
and wizard, AI meeting notes (`meeting_notes.py`, gpt-6-luna), floating subtitles pause/resume + saved
position, simple/advanced settings. Natural-voice rework: recorder for free speech (30–60 s, no script), delivery
switch (Скорость / Баланс / Естественность) and pace matching, Cartesia as the automatic voice once its key is
there, Soniox EU region setting, default speed 1.0 (settings_version 3).
Latency baseline, measured for real (tools/latency_test.py, its default phrase, Cartesia voice, VPN exit in
the Netherlands): «Собеседник слышит английский» +1.9 s from the start of my speech, «Последнее английское
слово» +2.9 s after the Russian phrase ends (before the latency rework: 3.3 / 4.9 s; the OpenAI engine was
~6 s and is not re-measured). Clearly slower than this is a regression; compare with `--delivery fast` (the
old per-clause delivery, what the baseline is): «Баланс» and «Естественность» wait for whole sentences on purpose. A global audit
(lifecycle races, reconnects, proxies, settings, UI) is fixed; regressions live in tests/test_robustness.py.
A second full recheck (2026-09-29: 9 module reviews, 2 cross-cutting audits, every finding
tried to be refuted) confirmed 30 low/medium issues, all fixed; regressions live in tests/test_audit_*.py.
Known and left as is: a hotkey force-finalize can drop a phrase in progress; `_preview` ignores delivery.
Tests: `py -3 -m pytest` — suite with local mock servers (`tests/`), no keys, network or audio devices
needed; the Windows system proxy is ignored, and a test stuck for `test_timeout` (pytest.ini, 60 s) stops
the run with every thread's traceback. `tests/test_ui.py` runs `ui/app.js` in node with a stand-in DOM and
api (skipped without node); `tests/test_onboarding_ui.py` does the same for the wizard, hints and call-mode
controls with events and timers recorded. Keep them green; add tests next to the module you change.

## Open tasks for cloud agents
None right now. Also done: speaker separation («Собеседник 1 / 2»), transcript editing before notes,
1024x640 layout, a warm Soniox TTS stream kept ready before every utterance.
Next ideas (ask the user first): multi-target translation, presentation mode with a share link.
