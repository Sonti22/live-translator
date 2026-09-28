"use strict";
// Transync-style UI for the live call translator. Talks to Python through window.pywebview.api.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const SENTENCE_END = /[.?!…]["»”)]?\s*$/;
const IDLE_MS = 900;
const DST_IDLE_MS = 2500;        // a translation may pause mid-sentence longer than the original
const STALE_MS = 12000;
const ACCENT = "#3fbf45";
const SLOW_MS = 150;            // round trip to a speech service that is worth a closer VPN server
const LEVERS = { speedBoost: "speed_boost", trimSilence: "trim_silence",
                 instantPhrases: "instant_phrases", autoFinalize: "auto_finalize" };
const PROVIDERS = { soniox: "Soniox", cartesia: "Cartesia", inworld: "Inworld" };
const BUILTIN_FIELDS = { soniox: "voice_name", cartesia: "cartesia_builtin_id", inworld: "inworld_voice_name" };

let api, state, S;              // bridge, initial state, settings
let seq = 0;
let running = false, startedAt = 0, frozen = 0, muted = false;
let statuses = {};
let callCheck = { done: false, mic: null };  // the pre-call check of this launch, for this default mic
let afterCheck = null;          // what passing the pre-call check goes on with: start, or my mic switched on
const levels = [];              // mic/call loudness history for the ruler
const entries = [];
const chans = { me: newChan(), them: newChan() };
const themSpeakers = new Set();  // Soniox speaker labels of the other side ("1", "2", ...)

function newChan() {
  return { src: null, srcClosed: true, srcLast: 0, dst: null, dstClosed: true, dstLast: 0, pending: [] };
}

window.addEventListener("pywebviewready", init);
window.addEventListener("error", (e) => api && api.log_js(`${e.message} @ ${e.filename}:${e.lineno}`));
window.addEventListener("unhandledrejection", (e) => api && api.log_js(`promise: ${e.reason}`));

async function init() {
  api = window.pywebview.api;
  state = await api.get_state();
  S = state.settings;
  seq = state.seq;
  muted = state.muted;
  buildTicks($("#meterMix"), 16);
  buildTicks($("#meterMe"), 14);
  buildTicks($("#meterThem"), 14);
  bindUi();
  applyView();
  renderPair();
  renderMute();
  renderEngine();
  renderVoice();
  $("#cableBanner").hidden = state.cable_ok;
  $("#outBanner").hidden = !isCable(state.default_out);
  $("#pinBtn").classList.toggle("on", !!S.on_top);
  $("#hotkeyName").parentElement.textContent = state.hotkey
    ? `${state.hotkey} — выключить/включить микрофон из любого окна.`
    : "Ctrl+Alt+M занята другой программой — выключайте микрофон кнопкой «Микрофон вкл».";
  $("#hotkeyDone").parentElement.textContent = state.hotkey_done
    ? `${state.hotkey_done} — «я закончил»: перевод фразы звучит сразу, не дожидаясь паузы.`
    : "Ctrl+Alt+Space занята другой программой — фраза закрывается по паузе в речи.";
  if (state.notice) toast(state.notice);
  if (!state.has_key) {
    setStatus("Нужен ключ Soniox или OpenAI — откройте настройки", "bad");
    openSettings();
  }
  if (state.running) setRunning(true, state.started);
  poll();
  requestAnimationFrame(frame);
}

// --- polling --------------------------------------------------------------

async function poll() {
  try {
    const r = await api.poll(seq);
    for (const ev of r.events) {
      seq = ev.seq;
      handle(ev);
    }
    setMeters(r.me, r.them);
    if (running) levels.push(Math.max(r.me, r.them));
    if (levels.length > 600) levels.splice(0, levels.length - 600);
    if (r.muted !== muted) { muted = r.muted; renderMute(); }
    if (running && !r.running) engineStopped();
  } catch (e) {
    console.error(e);
  }
  tickChannels();
  setTimeout(poll, 100);
}

function handle(ev) {
  switch (ev.type) {
    case "caption": onCaption(ev.kind, ev.text, ev.speaker); break;
    case "status":
      statuses[ev.label] = ev;
      renderStatus();
      break;
    case "lag": $("#lag").textContent = `· задержка ≈ ${ev.value.toFixed(1)} с`; break;
    case "note": $("#statusText").title = ($("#statusText").title + "\n" + ev.text).trim(); break;
    case "fatal":
      setStatus("Остановлено: ошибка", "bad");
      toast(ev.text, true);
      if (ev.key) openSettings();
      break;
    case "muted": muted = ev.value; renderMute(); break;
    case "paused":
      if (ev.value) setStatus("Пауза — перевод остановлен (продолжить: ▶ в мини-субтитрах)", "connecting");
      else if (running) renderStatus();
      else setStatus("Готов к работе", "");
      break;
    case "running": if (!ev.value && running) engineStopped(); break;
    case "overlay": $("#overlayBtn").classList.toggle("on", ev.value); break;
    case "notes": toast(`ИИ-протокол готов: ${ev.title}`); break;
    case "notes_error": toast(`Протокол не создан: ${ev.text}`, true); break;
  }
}

// --- start / stop ---------------------------------------------------------

async function toggleRun() {
  if (running) {
    setRunning(false);  // first: the engine's own "stopped" event must not stop it a second time
    statuses = {};
    setStatus("Остановлено", "");
    const record = await api.stop();
    if (record) toast(`Запись сохранена: ${record}`);
    return;
  }
  if (!state.has_key) { openSettings(); return; }
  if (await needsCallCheck(S.me_on)) {
    openCallCheck(startRun, "Начать перевод");  // starts the call itself once checked
    return;
  }
  await startRun();
}

// Asks Windows for its default devices again; true when my microphone is to be translated and the check is due.
async function needsCallCheck(meOn) {
  Object.assign(state, await api.default_devices());
  $("#outBanner").hidden = !isCable(state.default_out);
  return state.cable_ok && meOn && (!callCheck.done || callCheck.mic !== state.default_mic);
}

// Switching my microphone on mid-call puts its translation into the call: the pre-call check comes first.
async function saveMe(patch, render) {
  const apply = () => { const r = save(patch); render(); return r; };
  if (running && patch.me_on && !S.me_on && await needsCallCheck(true)) {
    openCallCheck(apply, "Включить перевод микрофона");
    render();  // stays as it was until checked
    return;
  }
  await apply();
}

async function startRun() {
  const r = await api.start();
  if (!r.ok) {
    if (r.error === "no_cable") $("#cableWizard").hidden = false;
    else openSettings();
    return;
  }
  if (r.notice) {
    S.engine = r.engine;
    voiceCache = null;
    renderEngine();
    renderVoice();
    toast(r.notice);
  }
  clearFeed();
  statuses = {};
  $("#lag").textContent = "";
  $("#statusText").title = "";
  setStatus("Подключение…", "connecting");
  setRunning(true, r.started);
}

async function engineStopped() {
  if (!running) return;
  setRunning(false);  // before the await: one poll may deliver two stop signals
  statuses = {};
  await api.stop();  // saves the record
}

function setRunning(on, started) {
  running = on;
  document.body.classList.toggle("live", on);
  if (on) startedAt = started * 1000;
  else frozen = elapsed();
}

function elapsed() {
  return running ? (Date.now() - startedAt) / 1000 : frozen;
}

function isCable(name) {
  return /CABLE/.test(name || "");
}

// The call must hear only the translation: once per launch (and when the default mic changes)
// the user checks that the call app's microphone is the cable, not the real microphone.
function openCallCheck(then, action) {
  afterCheck = then;
  $("#ccStart").textContent = action;
  $("#ccMic").textContent = state.default_mic || "не найден";
  const warn = [];
  if (isCable(state.default_mic) && !S.mic) {
    warn.push("Программа слушает микрофон Windows по умолчанию, а это кабель: выберите настоящий микрофон — «Источник звука» → «Звук микрофона».");
  }
  if (isCable(state.default_out)) {
    warn.push("Звук Windows по умолчанию идёт в кабель: собеседник услышит системные звуки. Выберите наушники: Параметры → Система → Звук → Вывод.");
  }
  $("#ccWarn").textContent = warn.join("\n");
  $("#ccWarn").hidden = !warn.length;
  $$(".cc-key").forEach((b) => (b.textContent = state.hotkey || "кнопку «Микрофон»"));
  $("#ccDone").checked = false;
  $("#ccStart").disabled = true;
  renderMute();
  $("#callCheck").hidden = false;
}

function passCallCheck() {
  callCheck = { done: true, mic: state.default_mic };
  $("#callCheck").hidden = true;
  afterCheck();
}

// --- captions -> entries ----------------------------------------------------

function onCaption(kind, text, speaker) {
  const side = kind.startsWith("me") ? "me" : "them";
  if (side === "them" && speaker && !themSpeakers.has(speaker)) {
    themSpeakers.add(speaker);
    document.body.classList.toggle("multi-speaker", themSpeakers.size > 1);
    for (const e of entries) if (e.side === "them") e.whoEl.textContent = whoLabel(e);  // "Собеседник 1 / 2"
  }
  const key = side === "them" && speaker ? `them:${speaker}` : side;
  const ch = chans[key] || (chans[key] = newChan());
  const now = performance.now();
  if (kind.endsWith("_src")) {
    if (!ch.src || ch.srcClosed) {
      ch.src = newEntry(side, speaker);
      ch.srcClosed = false;
      ch.pending.push(ch.src);
    }
    write(ch.src, "src", text);
    ch.srcLast = ch.src.srcLast = now;
    if (SENTENCE_END.test(ch.src.srcText)) close(ch, "src");
  } else {
    if (!ch.dst || ch.dstClosed) {
      const unfinished = ch.dst && !SENTENCE_END.test(ch.dst.dstText) && now - ch.dstLast < 4000;
      if (!unfinished) {  // else: the rest of a sentence whose translation paused midway
        // translation of the oldest phrase still waiting for one; a phrase that never got its
        // translation (e.g. "Угу.") is skipped once a newer one waits too, so pairs don't shift
        ch.pending = ch.pending.filter((e, i, all) => !e.hasDst && now - e.created < STALE_MS
                                                      && !(i < all.length - 1 && now - e.srcLast > 5000));
        ch.dst = ch.pending.shift() || newEntry(side, speaker);
        ch.dst.hasDst = true;
      }
      ch.dstClosed = false;
    }
    write(ch.dst, "dst", text);
    ch.dstLast = now;
    if (SENTENCE_END.test(ch.dst.dstText)) close(ch, "dst");
  }
}

function tickChannels() {
  const now = performance.now();
  for (const ch of Object.values(chans)) {
    if (!ch.srcClosed && now - ch.srcLast > IDLE_MS) close(ch, "src");
    const dstIdle = ch.dst && SENTENCE_END.test(ch.dst.dstText) ? IDLE_MS : DST_IDLE_MS;
    if (!ch.dstClosed && now - ch.dstLast > dstIdle) close(ch, "dst");
  }
}

function close(ch, part) {
  ch[part + "Closed"] = true;
  if (ch[part]) ch[part][part + "El"].classList.remove("open");
}

function whoLabel(entry) {
  if (entry.side === "me") return "Я";
  return themSpeakers.size > 1 && entry.speaker ? `Собеседник ${entry.speaker}` : "Собеседник";
}

function newEntry(side, speaker) {
  const el = document.createElement("div");
  el.className = `entry ${side}`;
  const meta = document.createElement("div");
  meta.className = "meta";
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = whoLabel({ side, speaker });
  const ts = document.createElement("span");
  ts.textContent = elapsed() >= 3600 ? clock(elapsed()) : clock(elapsed()).slice(3);
  meta.append(who, ts);
  const srcEl = document.createElement("div");
  srcEl.className = "src";
  const dstEl = document.createElement("div");
  dstEl.className = "dst";
  el.append(meta, srcEl, dstEl);
  const entry = { side, speaker, el, whoEl: who, srcEl, dstEl, srcText: "", dstText: "", hasDst: false,
                  created: performance.now(), srcLast: performance.now() };
  entries.push(entry);
  place(entry);
  $("#placeholder").hidden = true;
  return entry;
}

function place(entry) {
  const box = S.panel === "split" ? (entry.side === "me" ? $("#feedMe") : $("#feedThem")) : $("#feedSingle");
  const stick = nearBottom(box);
  box.appendChild(entry.el);
  if (stick) box.scrollTop = box.scrollHeight;
}

function write(entry, part, text) {
  const box = entry.el.parentElement;
  const stick = nearBottom(box);
  entry[part + "Text"] += text;
  const el = entry[part + "El"];
  el.textContent = entry[part + "Text"].trimStart();
  el.classList.add("open");
  if (stick) box.scrollTop = box.scrollHeight;
}

function nearBottom(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
}

function clearFeed() {
  entries.length = 0;
  for (const id of ["#feedSingle", "#feedMe", "#feedThem"]) $(id).replaceChildren();
  for (const key of Object.keys(chans)) delete chans[key];
  Object.assign(chans, { me: newChan(), them: newChan() });
  themSpeakers.clear();
  document.body.classList.remove("multi-speaker");
  $("#placeholder").hidden = false;
}

// --- rendering --------------------------------------------------------------

function applyView() {
  document.documentElement.style.setProperty("--font", `${S.font}px`);
  document.body.classList.toggle("swap", !!S.swap);
  document.body.classList.toggle("simple", !S.advanced);
  $("#advanced").checked = !!S.advanced;
  $("#diarize").checked = S.diarize !== false;
  for (const [id, key] of Object.entries(LEVERS)) $("#" + id).checked = S[key] !== false;
  document.body.classList.toggle("dst-only", S.text_mode === "dst");
  const split = S.panel === "split";
  $("#feedSingle").hidden = split;
  $("#feedSplit").hidden = !split;
  for (const e of entries) place(e);
  $$("#panelSeg [data-panel]").forEach((b) => b.classList.toggle("active", b.dataset.panel === S.panel));
  $$("#textSeg [data-text]").forEach((b) => b.classList.toggle("active", b.dataset.text === S.text_mode));
  $("#swapOrder").classList.toggle("active", !!S.swap);
  $("#textHint").textContent = S.text_mode === "dst" ? "Показать только перевод" : "Оригинал и перевод";
  const label = S.me_on && S.listen_on ? "Микс" : S.me_on ? "Микрофон" : S.listen_on ? "Компьютер" : "Нет звука";
  $("#sourceLabel").textContent = label;
}

function renderPair() {
  $("#pairMe").textContent = S.me_lang;
  $("#pairPeer").textContent = S.peer_lang;
  $("#phPair").textContent = `${S.me_lang} ⇄ ${S.peer_lang}`;
  $("#voiceLang").textContent = S.peer_lang;
}

function renderMute() {
  $("#muteBtn").classList.toggle("off", muted);
  $("#muteText").textContent = muted ? "Микрофон выкл" : "Микрофон вкл";
  $("#muteBtn").title = `Выключить/включить микрофон${state && state.hotkey ? " (" + state.hotkey + ")" : ""}`;
  $("#ccMute").textContent = muted ? "сейчас выключен" : "сейчас включён";
  $("#ccMute").className = "badge" + (muted ? "" : " ok");
}

function renderVoice() {
  $("#voiceBtn").classList.toggle("on", !!S.voice_out);
  $("#voiceOut").classList.toggle("on", !!S.voice_out);
  $("#monitor").classList.toggle("on", !!S.monitor);
  $("#volume").value = S.volume;
  $("#volumeVal").textContent = `${Math.round(S.volume * 100)}%`;
  $("#speed").value = S.speed;
  $("#speedVal").textContent = `${Number(S.speed).toFixed(2)}×`;
  $("#speedField").hidden = S.engine !== "soniox";
  // the provider choice appears once a second provider has a key
  const offered = Object.keys(PROVIDERS).filter((p) => p === "soniox" || state.keys[p] || p === provider());
  $("#providerRow").hidden = S.engine !== "soniox" || offered.length < 2;
  $$("#providerSeg [data-provider]").forEach((b) => {
    b.hidden = !offered.includes(b.dataset.provider);
    b.classList.toggle("active", b.dataset.provider === provider());
  });
  const inUse = cloneId() && S.voice === "clone";  // the call hears my clone only when it is picked
  const badge = $("#cloneState");
  badge.textContent = inUse ? `готов ✓ · ${PROVIDERS[provider()]}` : cloneId() ? "готов, но не выбран" : "не создан";
  badge.className = "badge" + (inUse ? " ok" : "");
  const cartesiaClone = S.engine === "openai" && S.voice === "clone";
  $("#delaySeg").hidden = !cartesiaClone;
  $$("#delaySeg [data-delay]").forEach((b) => b.classList.toggle("active", b.dataset.delay === S.voice_delay));
  $("#delayHint").textContent = cartesiaClone
    ? "Сколько клон может ждать продолжения фразы: быстрее — «Мгновенно», естественнее — «Плавно»."
    : "Мгновенно — фразы озвучиваются по мере перевода, не дожидаясь конца предложения.";
  renderVoiceList();
  loadVoices();
}

function renderStatus() {
  const list = Object.values(statuses);
  const ok = list.length > 0 && list.every((s) => s.ok);
  const text = list.map((s) => (s.ok ? `${s.label} ✓` : `${s.label}: ${s.text}`)).join("   ·   ");
  setStatus(text, ok ? "ok" : "bad");
}

function setStatus(text, cls) {
  $("#statusText").textContent = text;
  $("#statusDot").className = "sdot " + (cls || "");
}

function buildTicks(box, n) {
  box.replaceChildren(...Array.from({ length: n }, () => document.createElement("i")));
}

function setMeters(me, them) {
  lightTicks($("#meterMix"), Math.max(me, them));
  lightTicks($("#meterMe"), me);
  lightTicks($("#meterThem"), them);
}

function lightTicks(box, value) {
  const lit = Math.round(Math.min(1, value * 1.6) * box.children.length);
  [...box.children].forEach((t, i) => t.classList.toggle("lit", i < lit));
}

function clock(sec) {
  sec = Math.floor(sec);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(Math.floor(sec / 3600))}:${p(Math.floor(sec / 60) % 60)}:${p(sec % 60)}`;
}

// timer + scrolling ruler with the loudness history left of the playhead
function frame() {
  const t = elapsed();
  $("#timer").textContent = clock(t);
  const canvas = $("#ruler");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const mid = w / 2, pxPerSec = 20, step = 8;
  const offset = (t * pxPerSec) % (step * 5);
  for (let x = mid - offset - step * 5 * Math.ceil(mid / (step * 5)); x < w; x += step) {
    const n = Math.round((x - mid + offset) / step);
    const major = n % 5 === 0;
    const fade = 1 - Math.min(1, Math.abs(x - mid) / mid) * 0.75;
    ctx.fillStyle = `rgba(255,255,255,${(major ? 0.45 : 0.22) * fade})`;
    const th = major ? 16 : 7;
    ctx.fillRect(Math.round(x), h - th - 4, 1, th);
  }
  const barW = pxPerSec / 10;  // one sample per poll (100 ms)
  for (let i = levels.length - 1, x = mid - barW; i >= 0 && x > 0; i--, x -= barW) {
    const bh = Math.max(1, levels[i] * (h - 10));
    ctx.fillStyle = "rgba(63,191,69,.55)";
    ctx.fillRect(x, h - 4 - bh, Math.max(1, barW - 0.6), bh);
  }
  ctx.fillStyle = ACCENT;
  ctx.fillRect(Math.round(mid) - 1, 4, 2, h - 8);
  requestAnimationFrame(frame);
}

// --- controls ---------------------------------------------------------------

async function save(patch) {
  Object.assign(S, patch);
  const r = await api.save_settings(patch);
  if (r && r.restarted) {
    statuses = {};
    setStatus("Перезапуск с новыми настройками…", "connecting");
  }
  return r;
}

function bindUi() {
  $("#playBtn").onclick = toggleRun;
  $("#muteBtn").onclick = () => api.set_muted(!muted);
  $("#overlayBtn").onclick = async () => $("#overlayBtn").classList.toggle("on", await api.toggle_overlay());
  $("#pinBtn").onclick = () => { save({ on_top: !S.on_top }); $("#pinBtn").classList.toggle("on", S.on_top); };
  $("#settingsBtn").onclick = openSettings;
  $("#settingsClose").onclick = () => ($("#settings").hidden = true);
  $("#settings").onclick = (e) => { if (e.target.id === "settings") $("#settings").hidden = true; };
  $("#recordsBtn").onclick = openRecords;
  $("#drawerClose").onclick = () => ($("#drawer").hidden = true);
  $("#openFolder").onclick = () => api.open_records_folder();

  $("#sourceBtn").onclick = (e) => togglePop("#sourcePop", e.currentTarget, renderSources);
  $("#langBtn").onclick = (e) => togglePop("#langPop", e.currentTarget, openLangs);
  $("#voiceBtn").onclick = (e) => togglePop("#voicePop", e.currentTarget, renderVoice, true);
  $("#moreBtn").onclick = (e) => togglePop("#morePop", e.currentTarget, null, true);
  document.addEventListener("mousedown", (e) => {
    if (!e.target.closest(".pop, #sourceBtn, #langBtn, #voiceBtn, #moreBtn")) closePops();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closePops();
      for (const id of ["#settings", "#drawer", "#assistant", "#cableWizard", "#recordView", "#callCheck"]) $(id).hidden = true;
      if (!recording) $("#recorder").hidden = true;
    }
  });

  // sources
  $("#listenOn").onchange = (e) => { save({ listen_on: e.target.checked }); applyView(); };
  $("#meOn").onchange = (e) => saveMe({ me_on: e.target.checked }, () => { $("#meOn").checked = !!S.me_on; applyView(); });
  $("#vmicHelp").onclick = () => { closePops(); openSettings(); $("#howto").scrollIntoView(); };

  // languages
  $("#langSwap").onclick = () => { [draft.me_lang, draft.peer_lang] = [draft.peer_lang, draft.me_lang]; renderLangLists(); };
  $$("#modeSeg [data-mode]").forEach((b) => (b.onclick = () => { draft.mode = b.dataset.mode; renderLangLists(); }));
  $("#langOk").onclick = () => {
    closePops();
    saveMe({ me_lang: draft.me_lang, peer_lang: draft.peer_lang,
             me_on: draft.mode !== "listen", listen_on: draft.mode !== "speak" }, () => { renderPair(); applyView(); });
  };

  // voice
  $("#voiceOut").onclick = () => { save({ voice_out: !S.voice_out }); renderVoice(); };
  $("#monitor").onclick = () => { save({ monitor: !S.monitor }); renderVoice(); };
  $("#volume").oninput = (e) => { $("#volumeVal").textContent = `${Math.round(e.target.value * 100)}%`; };
  $("#volume").onchange = (e) => save({ volume: parseFloat(e.target.value) });

  // view
  $$("#morePop [data-font]").forEach((b) => (b.onclick = () => {
    save({ font: Math.max(12, Math.min(36, S.font + 2 * Number(b.dataset.font))) });
    applyView();
  }));
  $("#swapOrder").onclick = () => { save({ swap: !S.swap }); applyView(); };
  $$("#panelSeg [data-panel]").forEach((b) => (b.onclick = () => { save({ panel: b.dataset.panel }); applyView(); }));
  $$("#textSeg [data-text]").forEach((b) => (b.onclick = () => { save({ text_mode: b.dataset.text }); applyView(); }));

  // voice clone & picker
  $("#speed").oninput = (e) => { $("#speedVal").textContent = `${Number(e.target.value).toFixed(2)}×`; };
  $("#speed").onchange = (e) => save({ speed: parseFloat(e.target.value) });
  $$("#delaySeg [data-delay]").forEach((b) => (b.onclick = () => { save({ voice_delay: b.dataset.delay }); renderVoice(); }));
  $$("#providerSeg [data-provider]").forEach((b) => (b.onclick = () => pickProvider(b.dataset.provider)));
  $("#recordBtn").onclick = openRecorder;
  $("#importBtn").onclick = async () => { if ((await api.import_sample()).ok) createClone(); };
  $("#previewBtn").onclick = () => preview(null, $("#previewBtn"));
  $("#recStart").onclick = startRecording;
  $("#recRetry").onclick = startRecording;
  $("#recCreate").onclick = createClone;
  $("#recClose").onclick = () => { if (!recording) $("#recorder").hidden = true; };

  // AI assistant
  $("#assistBtn").onclick = openAssistant;
  $("#assistClose").onclick = () => ($("#assistant").hidden = true);
  $("#kwAdd").onclick = () => { addKeywords($("#kwInput").value); $("#kwInput").value = ""; };
  $("#kwInput").onkeydown = (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addKeywords($("#kwInput").value);
      $("#kwInput").value = "";
    }
  };
  $("#kwInput").onpaste = (e) => {
    const text = e.clipboardData.getData("text");
    if (/[,;\n]/.test(text)) { e.preventDefault(); addKeywords(text); }
  };
  $("#ctxInput").oninput = updateCounts;
  $("#assistSave").onclick = saveAssistant;

  // virtual cable
  $("#cableHelp").onclick = () => ($("#cableWizard").hidden = false);
  $("#cableClose").onclick = () => ($("#cableWizard").hidden = true);
  $("#cableDownload").onclick = () => api.open_url("https://vb-audio.com/Cable/");

  // pre-call check
  $("#ccClose").onclick = () => ($("#callCheck").hidden = true);
  $("#ccDone").onchange = (e) => ($("#ccStart").disabled = !e.target.checked);
  $("#ccStart").onclick = passCallCheck;

  // settings
  $$("#engineSeg [data-engine]").forEach((b) => (b.onclick = async () => {
    await save({ engine: b.dataset.engine, engine_auto: false });
    voiceCache = null;
    renderEngine();
    renderVoice();
  }));
  $$("[data-key-save]").forEach((b) => (b.onclick = () => saveKey(b.dataset.keySave)));
  $("#advanced").onchange = (e) => { save({ advanced: e.target.checked }); applyView(); };
  $("#diarize").onchange = (e) => save({ diarize: e.target.checked });
  for (const [id, key] of Object.entries(LEVERS)) $("#" + id).onchange = (e) => save({ [key]: e.target.checked });
  $("#netCheck").onclick = checkConnection;
  $$("input[name=proxy]").forEach((r) => (r.onchange = saveProxy));
  $("#proxyInput").onchange = saveProxy;
  $("#proxyInput").onfocus = () => { $("input[name=proxy][value=custom]").checked = true; };
}

// --- popovers -------------------------------------------------------------

let draft = {};

function togglePop(sel, anchor, onOpen, alignRight) {
  const pop = $(sel);
  const wasOpen = !pop.hidden;
  closePops();
  if (wasOpen) return;
  if (onOpen) onOpen();
  pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth;
  let left = alignRight ? r.right - w : r.left;
  left = Math.max(12, Math.min(left, window.innerWidth - w - 12));
  pop.style.left = `${left}px`;
  pop.style.top = `${r.bottom + 8}px`;
  pop.style.maxHeight = `${window.innerHeight - r.bottom - 20}px`;  // scrolls instead of running off a small window
}

function closePops() {
  $$(".pop").forEach((p) => (p.hidden = true));
}

function renderSources() {
  $("#listenOn").checked = !!S.listen_on;
  $("#meOn").checked = !!S.me_on;
  const outs = state.outputs;
  const notCable = (n) => !/CABLE/i.test(n);
  devList($("#listenList"), [[null, "Динамик по умолчанию"], ...outs.filter(notCable).map((n) => [n, n])],
    S.listen, (v) => save({ listen: v }));
  devList($("#micList"), [[null, "Микрофон по умолчанию"], ...state.mics.filter(notCable).map((n) => [n, n])],
    S.mic, (v) => save({ mic: v }));
  const cables = [...outs.filter((n) => !notCable(n)), ...outs.filter(notCable)];
  devList($("#cableList"), cables.map((n) => [n, n]), S.cable, (v) => save({ cable: v }));
}

function devList(ul, items, selected, onPick) {
  // the engine finds a device by a part of its name ("CABLE Input"), so mark it the same way
  const part = selected == null ? null : String(selected).toLowerCase();
  const match = items.find(([v]) => v === selected) || (part && items.find(([v]) => v && v.toLowerCase().includes(part)));
  ul.replaceChildren(...items.map(([value, label]) => {
    const li = document.createElement("li");
    li.innerHTML = '<svg class="i"><use href="#i-check"/></svg>';
    li.append(document.createTextNode(label));
    li.classList.toggle("sel", !!match && value === match[0]);
    li.onclick = () => { onPick(value); ul.querySelectorAll("li").forEach((x) => x.classList.remove("sel")); li.classList.add("sel"); };
    return li;
  }));
}

function openLangs() {
  draft = { me_lang: S.me_lang, peer_lang: S.peer_lang,
            mode: S.me_on && S.listen_on ? "two" : S.me_on ? "speak" : "listen" };
  renderLangLists();
}

function renderLangLists() {
  $$("#modeSeg [data-mode]").forEach((b) => b.classList.toggle("active", b.dataset.mode === draft.mode));
  langList($("#meLangs"), "me_lang", "peer_lang");
  langList($("#peerLangs"), "peer_lang", "me_lang");
}

function langList(ul, key, other) {
  ul.replaceChildren(...state.langs.map(([code, name]) => {
    const li = document.createElement("li");
    li.innerHTML = '<svg class="i"><use href="#i-check"/></svg>';
    const label = document.createElement("span");
    label.textContent = name + " ";
    const c = document.createElement("span");
    c.className = "code";
    c.textContent = code;
    label.append(c);
    li.append(label);
    li.classList.toggle("sel", draft[key] === code);
    li.classList.toggle("off", draft[other] === code);
    li.onclick = () => { draft[key] = code; renderLangLists(); };
    return li;
  }));
}

// --- records --------------------------------------------------------------

async function openRecords() {
  const data = await api.list_records();
  $("#usageTime").textContent = data.usage;
  $("#usageCost").textContent = `$${data.cost.toFixed(2)}`;
  const ul = $("#records");
  if (!data.records.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "Пока нет записей. Они сохраняются автоматически после каждой сессии.";
    ul.replaceChildren(li);
  } else {
    ul.replaceChildren(...data.records.map((r) => {
      const li = document.createElement("li");
      li.innerHTML = '<svg class="i"><use href="#i-records"/></svg><div class="rec-main"><div class="rec-title"></div><div class="rec-sub"></div></div><span class="link">Просмотр</span>';
      li.querySelector(".rec-title").textContent = r.title || "Без текста";
      li.querySelector(".rec-sub").textContent = `${r.date} · ${r.duration}`;
      li.onclick = () => openRecord(r.name);
      return li;
    }));
  }
  $("#drawer").hidden = false;
}

let currentRecord = null;

async function openRecord(name) {
  currentRecord = name;
  const r = await api.get_record(name);
  const lines = r.text.split("\n");
  $("#rvTitle").textContent = (r.notes && r.notes.title) || "Запись";
  $("#rvSub").textContent = `${lines[0] || name} · ${(lines[1] || "").replace("Длительность: ", "")}`;
  renderNotes(r.notes, r.can_summarize);
  $("#rvOpen").onclick = () => api.open_record(name);
  $("#rvExport").onclick = async () => { const path = await api.export_record(name); if (path) toast(`Сохранено: ${path}`); };
  $("#rvSummarize").hidden = !r.can_summarize;
  $("#rvSummarize").disabled = false;
  $("#rvSummarize").textContent = r.notes ? "Обновить протокол" : "Создать протокол";
  $("#rvSummarize").onclick = async () => {
    const btn = $("#rvSummarize");
    btn.disabled = true;
    btn.textContent = "Создаю…";
    let res;
    try {
      res = await api.summarize_record(name);
    } catch (e) {
      res = { ok: false, error: String(e.message || e) };
    }
    if (currentRecord !== name || $("#recordView").hidden) {  // another record is open by now
      if (res.ok) toast(`ИИ-протокол готов: ${res.notes.title || name}`);
      return;
    }
    btn.disabled = false;
    btn.textContent = "Обновить протокол";
    if (!res.ok) { toast(res.error, true); return; }
    $("#rvTitle").textContent = res.notes.title || "Запись";
    renderNotes(res.notes, true);
  };
  $("#rvEdit").textContent = "Редактировать";
  $("#rvEditor").hidden = true;
  $("#rvNotes").hidden = false;
  $("#rvEdit").onclick = async () => {
    const editor = $("#rvEditor");
    if (editor.hidden) {  // open the editor with the transcript body (header lines stay untouched)
      editor.value = r.text.split("\n").slice(3).join("\n").trim();
      editor.hidden = false;
      $("#rvNotes").hidden = true;
      $("#rvEdit").textContent = "Сохранить правки";
      editor.focus();
      return;
    }
    if (!(await api.save_record(name, editor.value))) { toast("Не удалось сохранить", true); return; }
    r.text = r.text.split("\n").slice(0, 3).join("\n") + "\n" + editor.value;
    editor.hidden = true;
    $("#rvNotes").hidden = false;
    $("#rvEdit").textContent = "Редактировать";
    toast(r.can_summarize ? "Сохранено. Нажмите «Обновить протокол», чтобы учесть правки." : "Сохранено");
  };
  $("#rvClose").onclick = () => ($("#recordView").hidden = true);
  $("#recordView").hidden = false;
}

function renderNotes(notes, canSummarize) {
  const box = $("#rvNotes");
  box.replaceChildren();
  if (!notes) {
    const p = document.createElement("p");
    p.className = "desc";
    p.textContent = canSummarize
      ? "ИИ-протокола ещё нет — нажмите «Создать протокол»."
      : "ИИ-протокол создаётся автоматически, если в ⚙ Настройках задан ключ OpenAI.";
    box.append(p);
    return;
  }
  const add = (title, items) => {
    if (!items || !items.length) return;
    const section = document.createElement("section");
    const h = document.createElement("h3");
    h.textContent = title;
    section.append(h);
    if (typeof items === "string") {
      const p = document.createElement("p");
      p.textContent = items;
      section.append(p);
    } else {
      const ul = document.createElement("ul");
      for (const item of items) {
        const li = document.createElement("li");
        li.textContent = item;
        ul.append(li);
      }
      section.append(ul);
    }
    box.append(section);
  };
  add("Кратко", notes.summary);
  add("Решения", notes.decisions);
  add("Задачи", notes.action_items);
}

// --- settings -------------------------------------------------------------

function openSettings() {
  closePops();
  renderKeys();
  renderEngine();
  const proxy = S.proxy || "";
  const kind = proxy === "" ? "" : proxy === "none" ? "none" : "custom";
  $(`input[name=proxy][value="${kind}"]`).checked = true;
  $("#proxyInput").value = kind === "custom" ? proxy : "";
  $("#sysProxy").textContent = state.system_proxy ? `(сейчас: ${state.system_proxy})` : "(не найден)";
  $("#settings").hidden = false;
  if (!state.has_key) $(`[data-key-input="${S.engine}"]`).focus();
}

// --- engine & keys ----------------------------------------------------------

const ENGINES = { soniox: "Soniox", openai: "OpenAI" };

function engineName() {
  return ENGINES[S.engine] || S.engine;
}

function renderEngine() {
  const tts = { soniox: "tts-rt-v2", cartesia: "Cartesia sonic-3.6", inworld: `Inworld ${S.inworld_model}` }[provider()];
  const model = S.engine === "soniox" ? `Soniox · stt-rt-v5 + ${tts}` : "OpenAI · gpt-realtime-translate";
  $("#modelChip").textContent = model;
  $("#phModel").textContent = model;
  $$("#engineSeg [data-engine]").forEach((b) => b.classList.toggle("active", b.dataset.engine === S.engine));
  $("#engineHint").textContent = S.engine === "soniox"
    ? "Переводит посреди фразы, говорит вашим клонированным голосом, учитывает ключевые слова и контекст. Нужен ключ Soniox."
    : "Синхронный перевод OpenAI: голос модели или ваш клон через Cartesia. Ключевые слова этот движок не поддерживает. Голос модели менее скрытный: он может произнести русское слово, отфильтровать его нельзя.";
  state.has_key = !!state.keys[S.engine];
}

function renderKeys() {
  for (const [name, ok] of Object.entries(state.keys)) {
    const el = $(`[data-key-state="${name}"]`);
    el.textContent = ok ? "✓" : "—";
    el.className = "key-state " + (ok ? "ok" : "miss");
    el.title = ok ? "Ключ сохранён — можно заменить новым" : "Ключ не задан";
  }
}

async function saveKey(provider) {
  const input = $(`[data-key-input="${provider}"]`);
  const r = await api.set_key(input.value, provider);
  if (!r || !r.ok) return;
  input.value = "";
  state.keys[provider] = true;
  S.engine = r.engine;
  voiceCache = null;
  renderKeys();
  renderEngine();
  renderVoice();
  if (state.has_key && !running) setStatus("Готов к работе", "");
  toast(r.notice || "Ключ сохранён");
}

// --- voice: picker, clone, preview ----------------------------------------------

const FALLBACK_VOICES = { soniox: [{ name: "Adrian" }, { name: "Daniel" }, { name: "Maya" }], cartesia: [],
                          inworld: [{ name: "Clive" }] };
let voiceCache = null;
let recording = false;

// who synthesizes my voice: Soniox, Cartesia or Inworld in the Soniox engine, Cartesia in the OpenAI one
function provider() {
  if (S.engine !== "soniox") return "cartesia";
  return PROVIDERS[S.voice_provider] ? S.voice_provider : "soniox";
}

function cloneId(p = provider()) {
  return S[p + "_voice_id"];
}

async function pickProvider(name) {
  const clone = S.voice === "clone";
  // mid-call the engine restarts at once: a stock voice instead of my clone, or no voice at all, gives me away
  if (running && !(clone ? cloneId(name) : S[BUILTIN_FIELDS[name]] || FALLBACK_VOICES[name].length)) {
    toast(`Во время перевода голос не переключить на ${PROVIDERS[name]}: ${clone ? "там нет вашего клона" : "не выбран голос"}. `
          + "Остановите перевод, выберите голос и начните снова.", true);
    return;
  }
  // «мой клон» stays wanted: a provider without my clone speaks a stock voice until one with it is picked again
  const patch = { voice_provider: name };
  if (clone || S.clone_auto_off) {
    Object.assign(patch, cloneId(name) ? { voice: "clone", clone_auto_off: false }
                                       : { voice: "builtin", clone_auto_off: true });
  }
  await save(patch);
  voiceCache = null;
  renderEngine();
  renderVoice();
}

async function loadVoices() {
  const p = provider();
  if (voiceCache || S.engine !== "soniox" || !state.keys[p]) return;
  let r;
  try {
    r = await api.list_voices();
  } catch (e) {
    return;  // the built-in fallback list stays
  }
  if (r.ok && r.voices.length && r.provider === provider()) {
    voiceCache = r.voices;
    renderVoiceList();
  }
}

function renderVoiceList() {
  const p = provider(), openai = S.engine !== "soniox";
  const items = [{ key: "clone", label: "Мой голос (клон)", desc: cloneId() ? "говорит как вы" : "сначала запишите голос",
                   off: !cloneId(), voice: cloneId() }];
  if (openai) {
    items.push({ key: "model", label: "Голос модели", desc: "подстраивается под ваш тон, быстрее всего" });
  } else {
    for (const v of voiceCache || FALLBACK_VOICES[p]) {  // a Cartesia voice is kept by its id
      items.push({ key: "builtin:" + (v.id || v.name), label: v.name, voice: v.id || v.name,
                   desc: [v.gender, v.description].filter(Boolean).join(" · ") });
    }
  }
  const builtin = S[BUILTIN_FIELDS[p]] || (FALLBACK_VOICES[p][0] || {}).name;
  const current = S.voice === "clone" ? "clone" : openai ? "model" : "builtin:" + builtin;
  $("#voiceList").replaceChildren(...items.map((item) => {
    const li = document.createElement("li");
    li.innerHTML = '<svg class="i"><use href="#i-check"/></svg><span class="vname"></span><span class="vdesc"></span>';
    li.querySelector(".vname").textContent = item.label;
    li.querySelector(".vdesc").textContent = item.desc;
    li.classList.toggle("sel", item.key === current);
    li.classList.toggle("off", !!item.off);
    if (item.voice && !openai) {
      const play = document.createElement("button");
      play.className = "vplay";
      play.textContent = "▶";
      play.title = "Прослушать";
      play.onclick = (e) => { e.stopPropagation(); preview(item.voice, play); };
      li.append(play);
    }
    li.onclick = async () => {
      if (item.off) return;
      const picked = { clone_auto_off: false };  // picked by hand: switching providers keeps it
      if (item.key === "clone") await save({ ...picked, voice: "clone" });
      else if (item.key === "model") await save({ ...picked, voice: "model" });
      else await save({ ...picked, voice: "builtin", [BUILTIN_FIELDS[p]]: item.voice });
      renderVoice();
    };
    return li;
  }));
}

async function preview(voice, button) {
  button.disabled = true;
  try {
    const r = await api.preview_voice(voice);
    if (!r.ok) toast(r.error, true);
  } catch (e) {
    toast(String(e.message || e), true);
  } finally {
    button.disabled = false;
  }
}

const REC_SECONDS = 25;

function openRecorder() {
  closePops();
  $("#recTime").textContent = clock(REC_SECONDS).slice(3);
  $("#recHint").textContent = `Нажмите красную кнопку и читайте текст (${REC_SECONDS} секунд)`;
  $("#recRetry").hidden = $("#recCreate").hidden = true;
  $("#recorder").hidden = false;
}

async function startRecording() {
  if (recording) return;
  recording = true;
  const btn = $("#recStart");
  btn.disabled = true;
  btn.classList.add("live");
  $("#recRetry").hidden = $("#recCreate").hidden = true;
  $("#recHint").textContent = "Идёт запись — читайте текст спокойно и естественно";
  const until = Date.now() + REC_SECONDS * 1000;
  const timer = setInterval(() => {
    $("#recTime").textContent = clock(Math.max(0, (until - Date.now()) / 1000) + 0.99).slice(3);
  }, 200);
  let r;
  try {
    r = await api.record_sample(REC_SECONDS);
  } catch (e) {
    r = { ok: false, error: `Не удалось записать: ${e.message || e}` };
  } finally {  // whatever happens, the recorder window must stay closable
    clearInterval(timer);
    recording = false;
    btn.disabled = false;
    btn.classList.remove("live");
    $("#recTime").textContent = "00:00";
  }
  if (!r.ok) { $("#recHint").textContent = r.error; return; }
  $("#recHint").textContent = {
    ok: `Записано ${r.seconds} с — громкость в норме. Можно создавать клон.`,
    quiet: "Очень тихо — говорите громче или ближе к микрофону. Лучше перезаписать.",
    clipped: "Слишком громко, звук искажён — отодвиньтесь от микрофона и перезапишите.",
  }[r.verdict];
  $("#recRetry").hidden = false;
  $("#recCreate").hidden = false;
}

async function createClone() {
  const btn = $("#recCreate");
  btn.disabled = true;
  btn.textContent = "Создаю клон…";
  $("#cloneState").textContent = "создаётся…";
  $("#cloneState").className = "badge busy";
  let r;
  try {
    r = await api.create_clone();
  } catch (e) {
    r = { ok: false, error: String(e.message || e) };
  } finally {
    btn.disabled = false;
    btn.textContent = "Создать клон";
  }
  if (!r.ok) {
    toast(r.error, true);
    renderVoice();
    return;
  }
  state = await api.get_state();
  S = state.settings;
  voiceCache = null;
  renderVoice();
  $("#recorder").hidden = true;
  toast("Клон готов — собеседник услышит ваш голос. Нажмите ▶ Прослушать в меню голоса.");
}

// --- AI assistant -------------------------------------------------------------

const KW_LIMIT = 2000;
let kwDraft = [];

function openAssistant() {
  kwDraft = [...(S.keywords || [])];
  $("#ctxInput").value = S.context || "";
  $("#assistNote").textContent = S.engine === "soniox"
    ? "Имена, компании и термины с переводом через «=» — Soniox распознаёт и переводит их точно. Контекст помогает с тоном и терминологией."
    : "Движок OpenAI не поддерживает ключевые слова и контекст — они заработают после переключения на Soniox (⚙ Настройки).";
  renderChips();
  $("#assistant").hidden = false;
  $("#kwInput").focus();
}

function addKeywords(text) {
  for (const part of text.split(/[,;\n]/)) {
    const item = part.replace(/\s*=\s*/, " = ").trim();
    if (!item || kwDraft.includes(item)) continue;
    if ([...kwDraft, item].join(", ").length > KW_LIMIT) { toast(`Лимит ${KW_LIMIT} символов`, true); break; }
    kwDraft.push(item);
  }
  renderChips();
}

function renderChips() {
  $("#chips").replaceChildren(...kwDraft.map((kw, i) => {
    const chip = document.createElement("span");
    chip.className = "kw";
    chip.textContent = kw;
    const x = document.createElement("button");
    x.textContent = "×";
    x.title = "Удалить";
    x.onclick = () => { kwDraft.splice(i, 1); renderChips(); };
    chip.append(x);
    return chip;
  }));
  updateCounts();
}

function updateCounts() {
  $("#kwCount").textContent = `${kwDraft.join(", ").length}/${KW_LIMIT}`;
  $("#ctxCount").textContent = `${$("#ctxInput").value.length}/800`;
}

async function saveAssistant() {
  const pending = $("#kwInput").value.trim();
  if (pending) { addKeywords(pending); $("#kwInput").value = ""; }
  const r = await save({ keywords: kwDraft, context: $("#ctxInput").value.trim() });
  $("#assistant").hidden = true;
  toast(r && r.restarted ? "Сохранено — перевод перезапущен с новым словарём" : "Сохранено");
}

function saveProxy() {
  const kind = $("input[name=proxy]:checked").value;
  const value = kind === "custom" ? $("#proxyInput").value.trim() : kind;
  if (kind === "custom" && !value) return;
  save({ proxy: value });
}

async function checkConnection() {
  const btn = $("#netCheck"), box = $("#netResult");
  btn.disabled = true;
  btn.textContent = "Проверяю…";
  let r;
  try {
    r = await api.check_connection();
  } catch (e) {
    r = { ok: false, error: String(e.message || e) };
  } finally {
    btn.disabled = false;
    btn.textContent = "Проверить связь";
  }
  box.hidden = false;
  box.replaceChildren();
  const line = (label, value, cls) => {
    const row = document.createElement("div");
    row.className = "net-line " + (cls || "");
    for (const text of [label, value]) {
      const span = document.createElement("span");
      span.textContent = text;
      row.append(span);
    }
    box.append(row);
  };
  if (!r.ok) { line("Ошибка", r.error, "fail"); return; }
  const exit = r.exit || {};
  line("VPN выходит", exit.error ? `не удалось узнать: ${exit.error}` : [exit.loc, exit.colo, exit.ip].filter(Boolean).join(" · "),
       exit.error ? "fail" : "");
  for (const p of r.probes) {
    if (p.ping_ms == null) line(p.label, p.rejected ? p.error : `нет связи: ${p.error}`, "fail");  // rejected: it answered
    else line(p.label, `${p.ping_ms} мс${p.open_ms != null ? ` · соединение ${p.open_ms} мс` : ""}`, p.ping_ms > SLOW_MS ? "slow" : "");
  }
  const hint = document.createElement("div");
  hint.className = "net-hint";
  hint.textContent = r.hint;
  box.append(hint);
}

// --- toast ----------------------------------------------------------------

let toastTimer;
function toast(text, bad) {
  const el = $("#toast");
  el.textContent = text;
  el.classList.toggle("bad", !!bad);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), bad ? 9000 : 3500);
}
