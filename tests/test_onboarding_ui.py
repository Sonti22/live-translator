"""Onboarding, hints, help and the call-mode controls of ui/app.js: node with a stand-in DOM that also records events."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
UI = Path(__file__).resolve().parent.parent / "ui"
APP_JS = UI / "app.js"
HTML = (UI / "index.html").read_text(encoding="utf-8")
JS = APP_JS.read_text(encoding="utf-8")
HOSTILE = "<img src=x onerror=alert(1)>"

# Unlike the stand-in of test_ui.py this one keeps parents, attributes, event handlers and timers: what the wizard,
# the tooltips and the coach marks do is a matter of events and focus.
HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const els = {}, all = {}, timers = [], docHandlers = {}, winHandlers = {}, innerHTMLWrites = [];
let activeEl = null;
function el(tag) {
  const classes = new Set(), attrs = {}, byQuery = {}, byQueryAll = {};
  let inner = "", parentEl = null;
  const adopt = (c) => { if (typeof c === "string") return { text: c }; if (c && c.tag !== undefined) c._parent = e; return c; };
  const e = {
    tag, hidden: false, disabled: false, checked: false, value: "", textContent: "", title: "", scrollTop: 0,
    offsetWidth: 120, offsetHeight: 30, offsetParent: {}, style: { setProperty() {} }, dataset: {}, children: [],
    _parent: null, _qa: byQueryAll,
    get innerHTML() { return inner; },
    set innerHTML(v) { innerHTMLWrites.push(String(v)); inner = String(v); },
    get parentElement() { return parentEl || (parentEl = el()); },
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
                 toggle: (c, on = !classes.has(c)) => { if (on) classes.add(c); else classes.delete(c); return on; } },
    get className() { return [...classes].join(" "); },
    set className(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
    append: (...c) => e.children.push(...c.map(adopt)), appendChild: (c) => e.children.push(adopt(c)),
    replaceChildren: (...c) => { e.children = c.map(adopt); },
    querySelector: (sel) => byQuery[sel] || (byQuery[sel] = el()), querySelectorAll: (sel) => byQueryAll[sel] || [],
    setAttribute: (k, v) => { attrs[k] = String(v); }, removeAttribute: (k) => { delete attrs[k]; },
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    closest(sel) { for (let n = e; n; n = n._parent) if (sel === "[data-tip]" && n.dataset.tip) return n; return null; },
    contains: (x) => x === e || e.children.some((c) => c.contains && c.contains(x)),
    matches: (sel) => sel === ":focus-visible" && e._keyboard === true,
    getBoundingClientRect: () => e._rect || { left: 200, top: 10, right: 320, bottom: 42, width: 120, height: 32 },
    focus() { activeEl = e; }, scrollIntoView() {},
  };
  return e;
}
const document = {
  querySelector: (sel) => els[sel] || (els[sel] = el()),
  querySelectorAll: (sel) => all[sel] || [],
  createElement: (tag) => el(tag), createTextNode: (text) => ({ text }),
  addEventListener: (type, fn) => (docHandlers[type] = docHandlers[type] || []).push(fn),
  get activeElement() { return activeEl; },
  body: el("body"), documentElement: el("html"),
};
Object.defineProperty(all, ".coach-target", { get: () => Object.values(els).filter((x) => x.classList.contains("coach-target")) });
const window = { addEventListener: (type, fn) => { winHandlers[type] = fn; }, innerWidth: 1240, innerHeight: 780 };
const ctx = vm.createContext({
  window, document, els, all, el, timers, docHandlers, winHandlers, innerHTMLWrites, console, performance,
  setTimeout: (fn, ms) => timers.push({ fn, ms, live: true }), clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].live = false; },
  setInterval: () => 0, clearInterval() {}, requestAnimationFrame: () => 0, settle: () => new Promise((r) => setImmediate(r)),
});
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx, { filename: "app.js" });
vm.runInContext(`(async () => {\n${fs.readFileSync(0, "utf8")}\n})()`, ctx, { filename: "scenario.js" }).then(
  (r) => process.stdout.write(JSON.stringify(r === undefined ? null : r)),
  (e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""

# What the backend reports and what the tests may change: `world` is get_state(), `reply` the answers of the windows.
BASE = r"""
const clone = (x) => JSON.parse(JSON.stringify(x));
const saved = [], calls = [];
const world = {
  keys: { soniox: true, openai: false, cartesia: true, inworld: false }, cable_ok: true, sample: true,
  hotkey: "Ctrl+Alt+M", hotkey_done: "Ctrl+Alt+Space", hotkey_hide: "Ctrl+Alt+H",
  default_mic: "Microphone (USB)", default_out: "Headphones",
  mics: ["Microphone (USB)", "CABLE Output (VB-Audio Virtual Cable)"],
  outputs: ["Headphones", "CABLE Input (VB-Audio Virtual Cable)"],
  running: false, paused: false, muted: false, seq: 0, started: 0, system_proxy: "", notice: "",
  ...cfg.state,
  settings: {
    engine: "soniox", voice: "builtin", voice_provider: "soniox", voice_name: "Adrian", soniox_voice_id: null,
    cartesia_voice_id: null, cartesia_builtin_id: null, inworld_voice_name: "Clive", inworld_voice_id: null,
    voice_delay: "balanced", volume: 1, speed: 1.0, me_on: true, listen_on: true, voice_out: true,
    me_lang: "ru", peer_lang: "en", font: 16, region: "", proxy: "",
    hide_from_capture: true, overlay_auto: true, overlay_opacity: 0.85, overlay_click_through: false,
    onboarding_done: false, hints_seen: [],
    ...cfg.settings,
  },
};
world.has_key = !!world.keys[world.settings.engine];
const reply = { overlay: true, stealth: { supported: true, enabled: true, main: true, overlay: null, build: 22631 } };
const theApi = {
  get_state: async () => { calls.push(["get_state"]); return clone(world); },
  save_settings: async (patch) => { saved.push(patch); return { restarted: false }; },
  poll: () => new Promise(() => {}), list_voices: async () => ({ ok: false }), default_devices: async () => ({}),
  toggle_overlay: async () => reply.overlay,
  get_stealth_status: async () => { calls.push(["stealth"]); return clone(reply.stealth); },
  set_key: async (value, provider) => { calls.push(["set_key", provider, value]); return { ok: true, engine: "soniox", settings: {} }; },
  check_connection: async () => ({ ok: true, exit: { loc: "NL", colo: "AMS", ip: "203.0.113.7" },
                                   probes: [{ label: "Soniox", ping_ms: 120, open_ms: 300 }], hint: "Связь в порядке." }),
  log_js() {},
};
for (const id of ["#settings", "#drawer", "#assistant", "#cableWizard", "#recordView", "#callCheck", "#help", "#recorder",
                  "#onboarding", "#coach", "#tip"]) $(id).hidden = true;
all["#onboarding [data-ob-step]"] = Array.from({ length: 8 }, (_, i) => {
  const box = el("section");
  box.dataset.obStep = String(i);
  box.hidden = true;
  return box;
});
for (const p of ["soniox", "cartesia", "openai", "inworld"]) {
  const input = el("input"), button = el("button");
  input.dataset.obKeyInput = p;
  button.dataset.obKeySave = p;
  els[`[data-ob-key-input="${p}"]`] = input;
  (all["[data-ob-key-input]"] = all["[data-ob-key-input]"] || []).push(input);
  (all["[data-ob-key-save]"] = all["[data-ob-key-save]"] || []).push(button);
}
const deepText = (n) => n.text !== undefined ? n.text : [n.textContent, ...n.children.map(deepText)].filter(Boolean).join("\n");
const rows = (box) => box.children.map(deepText);
const visibleStep = () => all["#onboarding [data-ob-step]"].findIndex((b) => !b.hidden);
const fire = (type, ev = {}) => {
  const e = { target: el(), preventDefault() { e.prevented = true; }, ...ev };
  for (const f of docHandlers[type] || []) f(e);
  return e;
};
const press = (key, extra) => fire("keydown", { key, ...extra });
const run = (ms) => { for (const t of timers.filter((x) => x.live && (ms == null || x.ms === ms))) { t.live = false; t.fn(); } };
const toasted = () => [$("#toast").textContent, $("#toast").classList.contains("bad")];
const start = async () => { window.pywebview = { api: theApi }; await winHandlers.pywebviewready(); await settle(); };
"""
BOOT = {
    "init": "",  # the scenario calls start(): init() reads everything from get_state()
    "ready": "api = theApi; state = clone(world); S = state.settings; bindUi();",
}


def run_js(scenario, state=None, settings=None, boot="ready"):
    """Run `scenario` (an async function body) against ui/app.js; returns what it returns."""
    if not NODE:
        pytest.skip("node runs ui/app.js")
    cfg = f"const cfg = {json.dumps({'state': state or {}, 'settings': settings or {}})};\n"
    run = subprocess.run([NODE, "-e", HARNESS, str(APP_JS)], input=cfg + BASE + BOOT[boot] + "\n" + scenario,
                         capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


NO_KEYS = {"soniox": False, "openai": False, "cartesia": False, "inworld": False}


# --- the wizard opens at launch -----------------------------------------------

@pytest.mark.parametrize("done, keys, running, wizard, settings", [
    (False, None, False, True, False),      # first launch
    (False, NO_KEYS, False, True, False),   # ... without a key: the wizard asks for it, the settings do not open over it
    (True, None, False, False, False),      # seen once: never again by itself
    (True, NO_KEYS, False, False, True),    # seen, no key: the settings open as before
    (False, None, True, False, False),      # a call is running: no wizard over it
])
def test_the_wizard_opens_at_launch_only_when_it_has_not_been_done(done, keys, running, wizard, settings):
    state = {"running": running, "started": 1700000000}
    if keys:
        state["keys"] = keys
    result = run_js(r"""
    await start();
    return [obStep >= 0, !$("#onboarding").hidden, !$("#settings").hidden];
    """, state=state, settings={"onboarding_done": done}, boot="init")
    assert result == [wizard, wizard, settings]


def test_the_wizard_starts_on_the_first_step_with_back_disabled():
    result = run_js(r"""
    await start();
    return [obStep, visibleStep(), $("#obCount").textContent, $("#obBack").disabled, $("#obNext").textContent];
    """, boot="init")
    assert result == [0, 0, "Шаг 1 из 8 · Как это работает", True, "Далее"]


def test_next_and_back_walk_all_eight_steps():
    result = run_js(r"""
    await start();
    const forward = [];
    for (let i = 0; i < 7; i++) {
      await $("#obNext").onclick();
      forward.push([obStep, visibleStep(), $("#obCount").textContent, $("#obNext").textContent, $("#obBack").disabled]);
    }
    await $("#obBack").onclick();
    return [forward, [obStep, visibleStep()], $("#obDots").children.map((d) => d.className), saved];
    """, boot="init")
    forward, back, dots, saved = result
    assert forward[0] == [1, 1, "Шаг 2 из 8 · Ключи", "Далее", False]
    assert forward[5][2] == "Шаг 7 из 8 · Скрытие от демонстрации экрана"
    assert forward[6] == [7, 7, "Шаг 8 из 8 · Проверка", "Готово", False]
    assert back == [6, 6]
    assert dots == ["ob-dot done"] * 6 + ["ob-dot active", "ob-dot"]
    assert saved == []  # walking through the steps changes no setting


def test_the_wizard_cannot_go_past_its_ends():
    result = run_js(r"""
    await start();
    await $("#obBack").onclick();
    const first = obStep;
    await openOnboarding(99);
    return [first, obStep, visibleStep()];
    """, boot="init")
    assert result == [0, 7, 7]


def test_skipping_remembers_that_the_wizard_was_seen():
    result = run_js(r"""
    await start();
    await openOnboarding(3);
    await $("#obSkip").onclick();
    return [obStep, $("#onboarding").hidden, saved, toasted(), S.onboarding_done];
    """, boot="init")
    closed, hidden, saved, (text, bad), done = result
    assert (closed, hidden, saved, bad, done) == (-1, True, [{"onboarding_done": True}], False, True)
    assert "Обучение пропущено" in text and "Пройти обучение заново" in text


def test_finishing_remembers_it_too_and_says_what_to_press():
    result = run_js(r"""
    await start();
    await openOnboarding(7);
    await $("#obNext").onclick();
    return [obStep, $("#onboarding").hidden, saved, toasted()[0]];
    """, boot="init")
    assert result[:3] == [-1, True, [{"onboarding_done": True}]]
    assert result[3].startswith("Готово.")


def test_escape_closes_what_lies_over_the_wizard_before_the_wizard_itself():
    result = run_js(r"""
    await start();
    await openOnboarding(2);
    $("#cableWizard").hidden = false;
    press("Escape");
    const over = [obStep, $("#cableWizard").hidden, $("#onboarding").hidden, saved.length];
    press("Escape");
    await settle();
    return [over, obStep, $("#onboarding").hidden, saved];
    """, boot="init")
    assert result == [[2, True, False, 0], -1, True, [{"onboarding_done": True}]]


def test_escape_in_the_main_window_saves_nothing():
    result = run_js(r"""
    press("Escape");
    await settle();
    return [obStep, saved];
    """, settings={"onboarding_done": True})
    assert result == [-1, []]


def test_tab_stays_inside_the_wizard():
    result = run_js(r"""
    await start();
    const [a, b, c] = [el("button"), el("button"), el("button")];
    els["#onboarding"].append(a, b, c);
    els["#onboarding"]._qa["button, input, [tabindex]:not([tabindex='-1'])"] = [a, b, c];
    const at = (x) => document.activeElement === x;
    c.focus();
    const forward = [press("Tab").prevented, at(a)];
    a.focus();
    const backward = [press("Tab", { shiftKey: true }).prevented, at(c)];
    b.focus();
    const middle = [press("Tab").prevented === true, at(b)];
    const outside = el("button");
    outside.focus();
    const lost = [press("Tab").prevented, at(a)];
    c.offsetParent = null;  // hidden: the last one to reach is b
    b.focus();
    const skipsHidden = [press("Tab").prevented, at(a)];
    c.offsetParent = {};
    $("#settings").hidden = false;  // a dialog over the wizard has its own focus
    c.focus();
    const over = press("Tab").prevented === true;
    return [forward, backward, middle, lost, skipsHidden, over];
    """, boot="init")
    assert result == [[True, True], [True, True], [False, True], [True, True], [True, True], False]


def test_tab_is_left_alone_once_the_wizard_is_closed():
    result = run_js(r"""
    const a = el("button");
    els["#onboarding"].append(a);
    els["#onboarding"]._qa["button, input, [tabindex]:not([tabindex='-1'])"] = [a];
    a.focus();
    return press("Tab").prevented === true;
    """, settings={"onboarding_done": True})
    assert result is False


def test_entering_a_step_moves_the_focus_to_its_heading():
    result = run_js(r"""
    await start();
    await openOnboarding(3);
    const boxes = all["#onboarding [data-ob-step]"];
    return [document.activeElement === boxes[3].querySelector("h3"), document.activeElement === boxes[0].querySelector("h3")];
    """, boot="init")
    assert result == [True, False]


# --- steps show what the machine has right now -------------------------------------

def test_a_live_step_asks_the_machine_again_and_keeps_my_settings():
    result = run_js(r"""
    await start();
    S.mic = "Microphone (USB)";
    await openOnboarding(2);
    const before = [deepText($("#obCable")), $("#obCable").className, $("#obCableBtn").textContent];
    world.cable_ok = true;
    await $("#obCableRecheck").onclick();
    const after = [deepText($("#obCable")), $("#obCable").className, $("#obCableBtn").textContent];
    return [before, after, S.mic, S.onboarding_done, $("#obCableRecheck").disabled];
    """, state={"cable_ok": False}, boot="init")
    before, after, mic, done, busy = result
    assert before[1] == "ob-status bad" and "не найден" in before[0] and before[2] == "Как установить"
    assert after[1] == "ob-status" and "установлен" in after[0] and after[2] == "Инструкция"
    assert (mic, done, busy) == ("Microphone (USB)", False, False)


def test_a_refresh_merges_the_machine_state_and_leaves_the_settings():
    result = run_js(r"""
    theApi.get_state = async () => ({ keys: { soniox: false, cartesia: false }, hotkey: null, settings: { engine: "openai" } });
    await refreshState();
    return [state.keys, state.hotkey, state.hotkey_done, state.cable_ok, state.has_key, S.engine, state.settings.engine];
    """)
    assert result == [{"soniox": False, "cartesia": False}, None, "Ctrl+Alt+Space", True, False, "soniox", "soniox"]


def test_a_refresh_that_fails_or_is_missing_changes_nothing():
    result = run_js(r"""
    const before = JSON.stringify(state);
    theApi.get_state = async () => { throw new Error("gone"); };
    await refreshState();
    const failed = JSON.stringify(state) === before;
    delete theApi.get_state;
    await refreshState();
    return [failed, JSON.stringify(state) === before];
    """)
    assert result == [True, True]


def test_the_key_step_shows_which_keys_are_saved():
    result = run_js(r"""
    await start();
    await openOnboarding(1);
    return ["soniox", "cartesia", "openai", "inworld"].map((p) => {
      const mark = els[`[data-ob-key-state="${p}"]`];
      return [mark.textContent, mark.className];
    });
    """, boot="init")
    assert result == [["✓", "key-state ok"], ["✓", "key-state ok"], ["—", "key-state miss"], ["—", "key-state miss"]]


def test_a_key_saved_in_the_wizard_goes_through_set_key_and_only_there():
    result = run_js(r"""
    await start();
    await openOnboarding(1);
    const input = els['[data-ob-key-input="openai"]'];
    input.value = "sk-secret";
    await all["[data-ob-key-save]"].find((b) => b.dataset.obKeySave === "openai").onclick();
    const mark = els['[data-ob-key-state="openai"]'];
    const enter = els['[data-ob-key-input="inworld"]'];
    enter.value = "iw-secret";
    await enter.onkeydown({ key: "Enter" });
    await settle();
    return [calls.filter((c) => c[0] === "set_key"), input.value, mark.textContent, mark.className,
            JSON.stringify(saved).includes("secret"), toasted()[0], world.keys.openai];
    """, boot="init")
    assert result == [[["set_key", "openai", "sk-secret"], ["set_key", "inworld", "iw-secret"]], "", "✓", "key-state ok",
                      False, "Ключ сохранён", False]


def test_a_refused_key_shows_the_reason_and_stays_in_the_field():
    result = run_js(r"""
    await start();
    await openOnboarding(1);
    theApi.set_key = async () => ({ ok: false, error: "Ключ не подошёл" });
    const input = els['[data-ob-key-input="soniox"]'];
    input.value = "wrong";
    await all["[data-ob-key-save]"][0].onclick();
    return [input.value, toasted()];
    """, boot="init")
    assert result == ["wrong", ["Ключ не подошёл", True]]


def test_the_microphone_step_lists_real_devices_and_saves_the_pick():
    result = run_js(r"""
    await start();
    await openOnboarding(3);
    const mics = rows($("#obMics")), outs = rows($("#obOuts"));
    await $("#obMics").children[1].onclick();
    await $("#obOuts").children[1].onclick();
    await $("#obMics").children[0].onclick();
    return [mics, outs, saved, deepText($("#obDevices"))];
    """, boot="init")
    mics, outs, saved, status = result
    assert mics == ["Микрофон по умолчанию", "Microphone (USB)"]  # the cable is not a microphone to pick
    assert outs == ["Динамик по умолчанию", "Headphones"]
    assert saved == [{"mic": "Microphone (USB)"}, {"listen": "Headphones"}, {"mic": None}]
    assert "Microphone (USB)" in status and "в наушниках" in status


@pytest.mark.parametrize("state, level, word", [
    ({}, "ob-status", "готово"),
    ({"mics": []}, "ob-status bad", "нет микрофона"),
    ({"default_mic": "CABLE Output (VB-Audio Virtual Cable)"}, "ob-status warn", "кабель"),
    ({"default_out": "CABLE Input (VB-Audio Virtual Cable)"}, "ob-status warn", "кабель"),
])
def test_the_microphone_step_warns_about_what_would_leak_or_be_silent(state, level, word):
    result = run_js(r"""
    await start();
    await openOnboarding(3);
    return [$("#obDevices").className, deepText($("#obDevices"))];
    """, state=state, boot="init")
    assert result[0] == level and word in result[1]


def test_the_voice_step_follows_the_sample_and_the_clone():
    result = run_js(r"""
    await start();
    const seen = [];
    for (const [sample, clone] of [[false, null], [true, null], [true, "clone-1"]]) {
      world.sample = sample;
      world.settings.soniox_voice_id = clone;
      state.sample = sample;
      S.soniox_voice_id = clone;
      await openOnboarding(4);
      seen.push([deepText($("#obVoice")), $("#obRecord").textContent]);
    }
    return seen;
    """, boot="init")
    assert [("не записан" in t, b) for t, b in result] == [(True, "Записать голос"), (False, "Записать заново"), (False, "Записать заново")]
    assert "записан" in result[1][0] and "клон готов" in result[2][0]


def test_a_recording_made_over_the_wizard_updates_the_voice_step_and_the_empty_feed():
    result = run_js(r"""
    await start();
    await openOnboarding(4);
    const before = [deepText($("#obVoice")), $("#phNextTitle").textContent];
    world.sample = true;
    $("#obRecord").onclick();
    const opened = !$("#recorder").hidden;
    closeRecorder();
    await settle();
    return [before, opened, $("#recorder").hidden, deepText($("#obVoice")), $("#phNextTitle").textContent];
    """, state={"sample": False}, boot="init")
    before, opened, closed, voice, title = result
    assert "не записан" in before[0] and before[1] == "Запишите голос"
    assert (opened, closed) == (True, True) and "записан" in voice and "не записан" not in voice
    assert title == "Нажмите Старт"


def test_the_apps_step_names_the_hotkey_or_the_button():
    result = run_js(r"""
    await start();
    await openOnboarding(5);
    return $("#obMuteKey").textContent;
    """, state={"hotkey": None}, boot="init")
    assert result == "кнопка «Микрофон»"
    result = run_js(r"""
    await start();
    await openOnboarding(5);
    return $("#obMuteKey").textContent;
    """, boot="init")
    assert result == "Ctrl+Alt+M"


def test_the_last_step_sums_up_what_is_still_missing():
    result = run_js(r"""
    await start();
    await openOnboarding(7);
    return $("#obSummary").children.map((li) => [li.children[0].textContent, deepText(li.children[1])]);
    """, state={"sample": False, "keys": {"soniox": True, "openai": False, "cartesia": False, "inworld": False}},
        settings={"hide_from_capture": False}, boot="init")
    assert [mark for mark, _ in result] == ["✓", "—", "✓", "—", "—"]
    assert result[0][1] == "Ключ Soniox"
    assert result[1][1].startswith("Ключ Cartesia:") and result[3][1].startswith("Запись голоса:")
    assert result[4][1].startswith("Скрытие от показа экрана:")


def test_the_step_buttons_open_the_existing_tools_in_their_own_place():
    result = run_js(r"""
    await start();
    await openOnboarding(2);
    $("#obCableBtn").onclick();
    await openOnboarding(4);
    $("#obRecord").onclick();
    await openOnboarding(7);
    $("#obCallCheck").onclick();
    const checkOpened = [!$("#cableWizard").hidden, !$("#recorder").hidden, !$("#callCheck").hidden];
    await $("#obNet").onclick();
    return [checkOpened, $("#obNetResult").hidden, deepText($("#obNetResult")), $("#netResult").children.length,
            $("#obNet").disabled, $("#obNet").textContent];
    """, boot="init")
    opened, hidden, net, settings_box, busy, label = result
    assert opened == [True, True, True]
    assert hidden is False and "Soniox" in net and "120 мс" in net and settings_box == 0
    assert (busy, label) == (False, "Проверить связь")


# --- what comes from outside is text -----------------------------------------------

def test_device_and_hotkey_names_reach_the_page_only_as_text():
    result = run_js(r"""
    await start();
    for (const i of [3, 5, 6, 7]) await openOnboarding(i);
    openHelp();
    openSettings();
    theApi.set_key = async () => ({ ok: false, error: "<img src=x onerror=alert(1)>" });
    await all["[data-ob-key-save]"][0].onclick();
    return [innerHTMLWrites,
            deepText($("#obMics")), deepText($("#obDevices")), deepText($("#helpKeys")), deepText($("#obFacts")),
            deepText($("#helpFacts")), $("#toast").textContent];
    """, state={"mics": [HOSTILE], "default_mic": HOSTILE, "hotkey": HOSTILE, "hotkey_done": HOSTILE, "hotkey_hide": HOSTILE},
        settings={"mic": HOSTILE}, boot="init")
    assert not [write for write in result[0] if "onerror" in write or "<img" in write]
    assert all(HOSTILE in text for text in result[1:])


def test_a_hostile_windows_build_is_shown_as_text():
    result = run_js(r"""
    reply.stealth = { supported: false, build: "<img src=x onerror=alert(1)>" };
    await openSettings();
    await settle();
    return [deepText(callBoxes[0].status), innerHTMLWrites];
    """)
    assert HOSTILE in result[0] and result[1] == []


def test_innerhtml_only_ever_takes_static_markup():
    """The DOM is built from text nodes: whatever a device, a key error or the backend says can never become markup."""
    assigned = re.findall(r"innerHTML\s*=\s*(.+?);", JS)
    assert assigned
    for rhs in assigned:
        assert rhs.startswith("'") and rhs.endswith("'") and "${" not in rhs, rhs
    assert not re.search(r"insertAdjacentHTML|outerHTML|document\.write|\beval\(|new Function", JS)


def test_the_page_calls_out_to_nobody():
    for name in ("index.html", "app.css", "overlay.html"):
        page = (UI / name).read_text(encoding="utf-8")
        page = page.replace('<link rel="stylesheet" href="app.css">', "").replace('<script src="app.js"></script>', "")
        assert not re.search(r"https?://|@import|<link|<script[^>]*\ssrc=|url\(\s*['\"]?(?!data:|#)", page), name
    assert not re.search(r"\bfetch\(|XMLHttpRequest|WebSocket|sendBeacon|\bimport\(", JS)
    assert re.findall(r"https?://[^\s\"'`]+", JS) == ["https://vb-audio.com/Cable/"]  # opened by the app, not by the page


# --- call mode ----------------------------------------------------------------------

def test_the_call_mode_controls_show_the_saved_settings_and_their_defaults():
    result = run_js(r"""
    renderCallMode();
    const ui = callBoxes[0];
    const shown = () => [ui.hide.checked, ui.auto.checked, ui.click.checked, ui.opacity.value, ui.opacityVal.textContent];
    const fresh = shown();
    for (const k of ["hide_from_capture", "overlay_auto", "overlay_click_through", "overlay_opacity"]) delete S[k];
    renderCallMode();
    const missing = shown();
    Object.assign(S, { hide_from_capture: false, overlay_auto: false, overlay_click_through: true, overlay_opacity: 0.5 });
    renderCallMode();
    const changed = shown();
    const odd = [];
    for (const v of ["abc", 5, 0.1, null]) { S.overlay_opacity = v; renderCallMode(); odd.push(ui.opacity.value); }
    return [fresh, missing, changed, odd, ui.open.textContent];
    """)
    assert result == [[True, True, False, "85", "85%"], [True, True, False, "85", "85%"],
                      [False, False, True, "50", "50%"], ["85", "100", "30", "85"], "Открыть окно субтитров"]


def test_each_call_mode_toggle_saves_its_own_setting_and_both_boxes_follow():
    result = run_js(r"""
    renderCallMode();
    const [settings_box, wizard_box] = callBoxes;
    settings_box.hide.checked = false; await settings_box.hide.onchange();
    wizard_box.auto.checked = false; await wizard_box.auto.onchange();
    settings_box.click.checked = true; await settings_box.click.onchange();
    await settle();
    return [saved, callBoxes.length, [wizard_box.hide.checked, wizard_box.auto.checked, wizard_box.click.checked],
            [settings_box.hide.checked, settings_box.auto.checked, settings_box.click.checked],
            [S.hide_from_capture, S.overlay_auto, S.overlay_click_through]];
    """)
    assert result == [[{"hide_from_capture": False}, {"overlay_auto": False}, {"overlay_click_through": True}], 2,
                      [False, False, True], [False, False, True], [False, False, True]]


@pytest.mark.parametrize("dragged, expected", [("55", 0.55), ("30", 0.3), ("10", 0.3), ("100", 1), ("250", 1), ("abc", None)])
def test_the_opacity_slider_saves_a_number_between_0_3_and_1(dragged, expected):
    result = run_js(r"""
    renderCallMode();
    const ui = callBoxes[0];
    ui.opacity.value = %s;
    ui.opacity.oninput();
    const label = ui.opacityVal.textContent;
    await ui.opacity.onchange();
    await settle();
    return [saved.map((p) => [Object.keys(p), typeof p.overlay_opacity, p.overlay_opacity]), label];
    """ % json.dumps(dragged))
    saves, label = result
    assert saves == ([] if expected is None else [[["overlay_opacity"], "number", expected]])
    assert label == f"{dragged}%"


def test_the_slider_under_my_hand_is_not_moved_back():
    result = run_js(r"""
    renderCallMode();
    const ui = callBoxes[0];
    S.overlay_opacity = 0.5;
    ui.opacity.focus();
    ui.opacity.value = "70";
    renderCallMode();
    const dragging = ui.opacity.value;
    el().focus();
    renderCallMode();
    return [dragging, ui.opacity.value];
    """)
    assert result == ["70", "50"]


STATUS_CASES = [
    ("missing", "delete theApi.get_stealth_status;", [["warn", "не сообщает"]]),
    ("unsupported", "reply.stealth = { supported: false, enabled: false, main: null, overlay: null, build: 18363 };",
     [["warn", "19041"]]),
    ("throws", 'theApi.get_stealth_status = async () => { throw new Error("boom"); };', [["warn", "Не удалось узнать"]]),
    ("empty", "theApi.get_stealth_status = async () => null;", [["warn", "Не удалось узнать"]]),
    ("both", "reply.stealth = { supported: true, enabled: true, main: true, overlay: true, build: 22631 };",
     [["ok", "Скрытие включено"], ["ok", "Это окно: скрыто"], ["ok", "Окно субтитров: скрыто"]]),
    ("overlay closed", "", [["ok", "Скрытие включено"], ["ok", "Это окно: скрыто"], ["muted", "сейчас закрыто"]]),
    ("refused", "reply.stealth = { supported: true, enabled: true, main: false, overlay: null, build: 22631 };",
     [["ok", "Скрытие включено"], ["bad", "не применила скрытие"], ["muted", "сейчас закрыто"]]),
    ("switched off", "reply.stealth = { supported: true, enabled: false, main: null, overlay: null, build: 22631 };",
     [["warn", "Скрытие выключено"]]),
]


@pytest.mark.parametrize("setup, lines", [c[1:] for c in STATUS_CASES], ids=[c[0] for c in STATUS_CASES])
def test_step_seven_and_the_settings_say_what_the_backend_knows_about_the_hiding(setup, lines):
    result = run_js(setup + r"""
    await openOnboarding(6);
    await settle();
    return callBoxes.map((ui) => ui.status.children.map((c) => [c.className, c.textContent]));
    """)
    assert len(result) == 2 and result[0] == result[1]
    assert [[cls, next(t for t in [text] if part in t)] for (cls, text), (_, part) in zip(result[0], lines)] == \
           [[cls, text] for (cls, text) in result[0]]
    assert [cls for cls, _ in result[0]] == [cls for cls, _ in lines]
    if setup.startswith("reply.stealth = { supported: false"):
        assert "18363" in result[0][0][1]


def test_an_answer_of_the_backend_that_arrives_late_does_not_overwrite_a_newer_one():
    result = run_js(r"""
    const answers = [];
    theApi.get_stealth_status = () => new Promise((resolve) => answers.push(resolve));
    const first = refreshStealth();
    const second = refreshStealth();
    answers[1]({ supported: false, build: 18000 });
    await second;
    answers[0]({ supported: true, enabled: true, main: true, overlay: null });
    await first;
    return stealth.kind;
    """)
    assert result == "unsupported"


def test_the_hiding_switch_asks_the_backend_again_and_the_other_switches_do_not():
    result = run_js(r"""
    renderCallMode();
    const asked = () => calls.filter((c) => c[0] === "stealth").length;
    callBoxes[0].auto.checked = false; await callBoxes[0].auto.onchange();
    await settle();
    const other = asked();
    callBoxes[0].hide.checked = false; await callBoxes[0].hide.onchange();
    await settle();
    return [other, asked()];
    """)
    assert result == [0, 1]


def test_the_settings_drawer_has_the_call_mode_section_with_a_status():
    result = run_js(r"""
    openSettings();
    await settle();
    return [$("#settings").hidden, els["#setCallMode"].children.length, callBoxes[0].box === els["#setCallMode"],
            deepText(callBoxes[0].status), calls.filter((c) => c[0] === "stealth").length];
    """)
    assert result[:3] == [False, 6, True]
    assert result[3].startswith("Скрытие включено") and result[4] == 1


def test_the_subtitles_window_button_shows_and_follows_the_window():
    result = run_js(r"""
    renderCallMode();
    const opened = async (on) => { reply.overlay = on; await $("#overlayBtn").onclick(); await settle(); };
    await opened(true);
    const on = [$("#overlayBtn").classList.contains("on"), callBoxes[0].open.textContent];
    const recheck = timers.some((t) => t.live && t.ms === 600);
    run(600);
    await settle();
    const asked = calls.filter((c) => c[0] === "stealth").length;
    await opened(false);
    const off = [$("#overlayBtn").classList.contains("on"), callBoxes[1].open.textContent];
    handle({ type: "overlay", value: true });
    await callBoxes[0].open.onclick();
    return [on, recheck, asked, off, $("#overlayBtn").classList.contains("on")];
    """)
    assert result == [[True, "Закрыть окно субтитров"], True, 2, [False, "Открыть окно субтитров"], False]


def test_step_seven_states_what_is_hidden_the_hotkey_and_the_limits():
    result = run_js(r"""
    await start();
    await openOnboarding(6);
    return [rows($("#obFacts")), rows($("#obLimits"))];
    """, boot="init")
    facts, limits = result
    assert facts[-1] == "Ctrl+Alt+H — спрятать или показать окна программы." and len(facts) == 4
    assert len(limits) == 4
    for part in ("карта захвата", "телефон", "прокторинг", "процессы", "CABLE", "тестовой встрече"):
        assert any(part in line for line in limits), part
    result = run_js(r"""
    await start();
    await openOnboarding(6);
    return rows($("#obFacts"));
    """, state={"hotkey_hide": None}, boot="init")
    assert len(result) == 3 and not any("Alt+H" in line or "None" in line or "null" in line for line in result)


# --- tooltips -----------------------------------------------------------------------

def test_a_tooltip_appears_after_a_short_hover_and_is_plain_text():
    result = run_js(r"""
    const button = el("button");
    button.dataset.tip = "Начать <b>перевод</b>";
    fire("mouseover", { target: button });
    const early = $("#tip").hidden;
    run(350);
    const shown = [$("#tip").hidden, $("#tip").textContent, button.getAttribute("aria-describedby")];
    fire("mouseout", { target: button });
    return [early, shown, [$("#tip").hidden, button.getAttribute("aria-describedby")], innerHTMLWrites];
    """)
    assert result == [True, [False, "Начать <b>перевод</b>", "tip"], [True, None], []]


def test_a_tooltip_stays_while_the_pointer_moves_inside_its_element_and_leaves_with_it():
    result = run_js(r"""
    const button = el("button"), icon = el("svg");
    button.dataset.tip = "Подсказка";
    button.append(icon);
    fire("mouseover", { target: icon });
    run(350);
    const shown = !$("#tip").hidden;
    fire("mouseout", { target: icon, relatedTarget: button });
    const inside = !$("#tip").hidden;
    fire("mouseout", { target: icon, relatedTarget: el() });
    return [shown, inside, $("#tip").hidden];
    """)
    assert result == [True, True, True]


def test_a_tip_is_not_shown_for_a_hover_that_was_only_passing_by():
    result = run_js(r"""
    const button = el("button");
    button.dataset.tip = "Подсказка";
    fire("mouseover", { target: button });
    fire("mouseout", { target: button });
    run(350);
    fire("mouseover", { target: el("div") });  // nothing to say here
    run(350);
    return $("#tip").hidden;
    """)
    assert result is True


def test_keyboard_focus_shows_the_tip_at_once_and_a_click_focus_does_not():
    result = run_js(r"""
    const button = el("button"), clicked = el("button");
    button.dataset.tip = "С клавиатуры";
    clicked.dataset.tip = "После щелчка";
    button._keyboard = true;
    fire("focusin", { target: button });
    const keyboard = [$("#tip").hidden, $("#tip").textContent];
    fire("focusout", { target: button });
    const left = $("#tip").hidden;
    fire("focusin", { target: clicked });
    return [keyboard, left, $("#tip").hidden];
    """)
    assert result == [[False, "С клавиатуры"], True, True]


def test_pressing_the_mouse_hides_the_tip():
    result = run_js(r"""
    const button = el("button");
    button.dataset.tip = "Подсказка";
    fire("mouseover", { target: button });
    run(350);
    fire("mousedown", { target: button });
    return $("#tip").hidden;
    """)
    assert result is True


def test_a_tip_stays_inside_the_window():
    result = run_js(r"""
    const button = el("button");
    button.dataset.tip = "Подсказка";
    const at = (rect) => {
      button._rect = rect;
      fire("mouseover", { target: button });
      run(350);
      const t = $("#tip");
      const out = [t.style.left, t.style.top];
      fire("mouseout", { target: button });
      return out;
    };
    const centred = at({ left: 200, top: 10, bottom: 42, width: 120, height: 32 });
    const right = at({ left: 1200, top: 10, bottom: 42, width: 30, height: 32 });
    const left = at({ left: 0, top: 10, bottom: 42, width: 20, height: 32 });
    const low = at({ left: 200, top: 740, bottom: 772, width: 120, height: 32 });
    return [centred, right, left, low];
    """)
    assert result == [["200px", "50px"], ["1112px", "50px"], ["8px", "50px"], ["200px", "702px"]]


# --- coach marks --------------------------------------------------------------------

DONE = {"onboarding_done": True}


def test_coach_marks_come_one_at_a_time_and_are_remembered():
    result = run_js(r"""
    const seen = [];
    for (let i = 0; i < 4; i++) {
      maybeCoach();
      seen.push([$("#coach").hidden, $("#coachText").textContent.slice(0, 12), coachId]);
      if (!$("#coach").hidden) await $("#coachOk").onclick();
    }
    return [seen, saved, S.hints_seen, all[".coach-target"].length];
    """, settings=DONE)
    seen, saved, hints, targets = result
    assert [ids for _, _, ids in seen] == ["start", "voice", "subs", None]
    assert [hidden for hidden, _, _ in seen] == [False, False, False, True]
    assert saved == [{"hints_seen": ["start"]}, {"hints_seen": ["start", "voice"]}, {"hints_seen": ["start", "voice", "subs"]}]
    assert hints == ["start", "voice", "subs"] and targets == 0


def test_a_mark_points_at_its_button():
    result = run_js(r"""
    maybeCoach();
    const first = [$("#playBtn").classList.contains("coach-target"), $("#voiceBtn").classList.contains("coach-target")];
    await dismissCoach();
    maybeCoach();
    return [first, [$("#playBtn").classList.contains("coach-target"), $("#voiceBtn").classList.contains("coach-target")],
            $("#coach").style.top];
    """, settings=DONE)
    assert result == [[True, False], [False, True], "54px"]


def test_a_mark_never_erases_the_ids_of_other_versions():
    result = run_js(r"""
    maybeCoach();
    await dismissCoach();
    return [saved, S.hints_seen];
    """, settings={**DONE, "hints_seen": ["from-the-future", "start"]})
    assert result == [[{"hints_seen": ["from-the-future", "start", "voice"]}], ["from-the-future", "start", "voice"]]


def test_a_missing_list_of_seen_marks_starts_from_nothing():
    result = run_js(r"""
    delete S.hints_seen;
    maybeCoach();
    await dismissCoach();
    return saved;
    """, settings=DONE)
    assert result == [{"hints_seen": ["start"]}]


@pytest.mark.parametrize("hindrance", [
    "await openOnboarding();",
    "$('#settings').hidden = false;",
    "$('#help').hidden = false;",
    "$('#recorder').hidden = false;",
    "const pop = el(); all['.pop'] = [pop];",
    "S.onboarding_done = false;",
])
def test_no_coach_mark_over_a_dialog_a_menu_or_the_wizard(hindrance):
    result = run_js(hindrance + r"""
    maybeCoach();
    return [$("#coach").hidden, coachId];
    """, settings=DONE)
    assert result == [True, None]


def test_the_start_mark_gives_way_when_the_translation_runs():
    result = run_js(r"""
    running = true;
    maybeCoach();
    const whileRunning = coachId;
    running = false;
    await dismissCoach();
    maybeCoach();
    const idle = coachId;
    running = true;
    maybeCoach();
    return [whileRunning, idle, coachId, $("#coach").hidden];
    """, settings={**DONE, "hints_seen": ["voice", "subs"]})
    assert result == [None, "start", None, True]


def test_the_poll_loop_brings_the_marks_after_the_wizard_is_done():
    result = run_js(r"""
    theApi.poll = async () => ({ events: [], me: 0, them: 0, muted: false, paused: false, running: false });
    await start();
    const during = coachId;
    await $("#obSkip").onclick();
    run(100);
    await settle();
    return [during, coachId, $("#coach").hidden];
    """, boot="init")
    assert result == [None, "start", False]


# --- the empty feed says what to do next ---------------------------------------------

@pytest.mark.parametrize("state, settings, title", [
    ({"keys": {**NO_KEYS, "openai": True}}, {}, "Добавьте ключ Soniox"),
    ({"keys": {**NO_KEYS, "soniox": True}}, {"engine": "openai"}, "Добавьте ключ OpenAI"),
    ({"cable_ok": False}, {}, "Установите виртуальный кабель"),
    ({"sample": False}, {}, "Запишите голос"),
    ({}, {}, "Нажмите Старт"),
    ({"keys": NO_KEYS, "cable_ok": False, "sample": False}, {}, "Добавьте ключ Soniox"),
    ({"cable_ok": False, "sample": False}, {}, "Установите виртуальный кабель"),
])
def test_the_empty_feed_names_the_next_step(state, settings, title):
    result = run_js(r"""
    renderPlaceholder();
    return [$("#phNextTitle").textContent, $("#phNextHint").textContent];
    """, state=state, settings=settings)
    assert result[0] == title and result[1]


def test_the_empty_feed_moves_on_when_a_key_is_saved():
    result = run_js(r"""
    state.keys.soniox = false;
    renderEngine();
    const before = $("#phNextTitle").textContent;
    await saveKey("soniox");
    return [before, $("#phNextTitle").textContent];
    """)
    assert result == ["Добавьте ключ Soniox", "Нажмите Старт"]


# --- help ----------------------------------------------------------------------------

def test_the_help_button_opens_the_help_with_the_hotkeys():
    result = run_js(r"""
    all[".pop"] = [el()];
    all[".pop"][0].hidden = false;
    await $("#helpBtn").onclick();
    return [!$("#help").hidden, all[".pop"][0].hidden, document.activeElement === $("#helpDone"),
            $("#helpKeys").children.map((li) => [li.children[0].textContent, deepText(li)]),
            rows($("#helpLimits")).length, rows($("#helpFacts")).length];
    """)
    opened, pops, focused, keys, limits, facts = result
    assert opened and pops and focused
    assert [combo for combo, _ in keys] == ["Ctrl+Alt+M", "Ctrl+Alt+Space", "Ctrl+Alt+H"]
    assert "микрофон" in keys[0][1] and "я закончил" in keys[1][1] and "спрятать или показать" in keys[2][1]
    assert (limits, facts) == (4, 4)


def test_the_help_leaves_out_a_hide_hotkey_the_app_did_not_get():
    result = run_js(r"""
    openHelp();
    return [$("#helpKeys").children.map((li) => deepText(li)), rows($("#helpFacts"))];
    """, state={"hotkey_hide": None})
    keys, facts = result
    assert len(keys) == 2 and len(facts) == 3
    assert not any("Alt+H" in text or "None" in text or "null" in text for text in keys + facts)


def test_the_help_says_which_hotkeys_are_taken_by_another_program():
    result = run_js(r"""
    openHelp();
    return $("#helpKeys").children.map((li) => deepText(li));
    """, state={"hotkey": None, "hotkey_done": None, "hotkey_hide": None})
    assert len(result) == 2
    assert all("занята другой программой" in text for text in result)


def test_the_help_closes_by_its_buttons_its_backdrop_and_escape():
    result = run_js(r"""
    const isOpen = () => !$("#help").hidden;
    const closed = [];
    for (const how of [() => $("#helpClose").onclick(), () => $("#helpDone").onclick(),
                       () => $("#help").onclick({ target: { id: "help" } }), () => press("Escape")]) {
      openHelp();
      how();
      closed.push(!isOpen());
    }
    openHelp();
    $("#help").onclick({ target: { id: "helpTitle" } });  // a click inside the card stays
    return [closed, isOpen()];
    """)
    assert result == [[True, True, True, True], True]


def test_the_help_replays_the_wizard():
    result = run_js(r"""
    openHelp();
    await $("#helpReplay").onclick();
    return [$("#help").hidden, saved, S.onboarding_done, obStep, $("#onboarding").hidden, visibleStep()];
    """, settings=DONE)
    assert result == [True, [{"onboarding_done": False}], False, 0, False, 0]


def test_the_wizard_is_not_replayed_over_a_running_call():
    result = run_js(r"""
    running = true;
    openHelp();
    await $("#helpReplay").onclick();
    return [saved, obStep, $("#onboarding").hidden, toasted()[1]];
    """, settings=DONE)
    assert result == [[], -1, True, True]


# --- the page itself -----------------------------------------------------------------

def tag(marker):
    found = re.search(rf"<[^<>]*{marker}[^<>]*>", HTML)
    assert found, marker
    return found.group(0)


def test_the_wizard_has_eight_steps_with_a_focusable_heading_each():
    steps = re.findall(r'<div class="ob-step"[^>]*data-ob-step="(\d)"[^>]*>\s*<h3 tabindex="-1">', HTML)
    assert steps == [str(i) for i in range(8)]
    dialog = tag('id="onboarding"')
    assert 'role="dialog"' in dialog and 'aria-modal="true"' in dialog and 'aria-labelledby="obTitle"' in dialog
    assert 'id="obTitle"' in HTML


def test_dialogs_open_over_the_wizard_and_the_help():
    order = {name: HTML.index(f'id="{name}"') for name in
             ("onboarding", "help", "settings", "recorder", "assistant", "cableWizard", "callCheck", "recordView")}
    for lower in ("onboarding", "help"):
        for over in ("settings", "recorder", "assistant", "cableWizard", "callCheck", "recordView"):
            assert order[lower] < order[over], (lower, over)  # later in the page stacks above, at the same z-index
    assert order["onboarding"] < order["help"]


@pytest.mark.parametrize("marker", [
    'id="playBtn"', 'id="assistBtn"', 'id="muteBtn"', 'id="overlayBtn"', 'id="pinBtn"', 'id="recordsBtn"',
    'id="settingsBtn"', 'id="helpBtn"', 'id="sourceBtn"', 'id="langBtn"', 'id="voiceBtn"', 'id="moreBtn"',
    'id="voiceOut"', 'id="monitor"', 'id="speed"', 'id="volume"', 'id="netCheck"',
    'data-mode="two"', 'data-mode="speak"', 'data-mode="listen"',
    'data-provider="soniox"', 'data-provider="cartesia"', 'data-provider="inworld"',
    'data-delivery="fast"', 'data-delivery="balanced"', 'data-delivery="natural"',
    'data-delay="instant"', 'data-delay="balanced"', 'data-delay="smooth"',
    'data-region=""', 'data-region="eu"', 'data-engine="soniox"', 'data-engine="openai"',
])
def test_the_controls_the_hints_are_about_carry_a_tip(marker):
    assert re.search(r'data-tip="[^"]{8,}"', tag(marker))


@pytest.mark.parametrize("box", ["matchRate", "speedBoost", "trimSilence", "instantPhrases", "autoFinalize", "diarize"])
def test_the_switches_carry_a_tip_on_their_label(box):
    assert re.search(rf'<label class="radio" data-tip="[^"]{{8,}}"><input type="checkbox" id="{box}">', HTML)


def test_no_tip_is_left_as_a_native_title_and_icon_buttons_keep_a_name():
    assert not re.search(r'\stitle="', re.sub(r"<title>.*?</title>", "", HTML))
    for button in re.findall(r"<button\b([^>]*)>(.*?)</button>", HTML, re.S):
        attrs, inner = button
        if "data-tip=" in attrs and "<svg" in inner and not re.sub(r"<[^>]*>", "", inner).strip():
            assert "aria-label=" in attrs, attrs


def test_the_help_lists_the_common_problems_and_the_hint_layers_exist():
    for name in ("helpKeys", "helpFacts", "helpLimits", "helpReplay", "helpDone", "coach", "coachText", "coachOk", "phNext",
                 "callModeSection", "setCallMode", "obCallMode"):
        assert f'id="{name}"' in HTML, name
    help_card = HTML[HTML.index('id="help"'):HTML.index('id="settings"')]
    assert len(re.findall(r'<details class="opt"', help_card)) == 5
    assert 'role="tooltip"' in tag('id="tip"')
