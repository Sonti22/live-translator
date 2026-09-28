"""ui/app.js in node with a stand-in DOM and pywebview api: what the buttons do to the settings and the call."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
APP_JS = Path(__file__).resolve().parent.parent / "ui" / "app.js"
pytestmark = pytest.mark.skipif(not NODE, reason="node runs ui/app.js")

# Every element is a plain object: `els[selector]` is what $(selector) returned, `all[selector]` what $$ returns.
HARNESS = r"""
const vm = require("vm"), fs = require("fs");
const els = {}, all = {};
function el(tag) {
  const classes = new Set();
  const e = {
    tag, hidden: false, disabled: false, checked: false, value: "", textContent: "", innerHTML: "", title: "",
    style: { setProperty() {} }, dataset: {}, children: [],
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
                 toggle: (c, on = !classes.has(c)) => { if (on) classes.add(c); else classes.delete(c); return on; } },
    get className() { return [...classes].join(" "); },
    set className(v) { classes.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => classes.add(c)); },
    append: (...c) => e.children.push(...c), appendChild: (c) => e.children.push(c),
    replaceChildren: (...c) => { e.children = c; },
    querySelector: () => el(), querySelectorAll: () => [], focus() {}, scrollIntoView() {},
  };
  return e;
}
const document = {
  querySelector: (sel) => els[sel] || (els[sel] = el()),
  querySelectorAll: (sel) => all[sel] || [],
  createElement: (tag) => el(tag), addEventListener() {}, body: el("body"), documentElement: el("html"),
};
const ctx = vm.createContext({ window: { addEventListener() {} }, document, els, all, el, console, performance,
                               setTimeout: () => 0, clearTimeout() {} });
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx, { filename: "app.js" });
vm.runInContext(`(async () => {\n${fs.readFileSync(0, "utf8")}\n})()`, ctx, { filename: "scenario.js" }).then(
  (r) => process.stdout.write(JSON.stringify(r === undefined ? null : r)),
  (e) => { process.stderr.write(String(e && e.stack || e)); process.exit(1); });
"""

# What init() would have loaded: the api records every saved patch.
SETUP = r"""
const saved = [];
api = { save_settings: async (patch) => { saved.push(patch); return { restarted: running }; },
        list_voices: async () => ({ ok: false }), default_devices: async () => ({}) };
state = { keys: { soniox: true, openai: true, cartesia: true, inworld: false }, hotkey: "Ctrl+Alt+M",
          default_mic: "Microphone (USB)", default_out: "Headphones", cable_ok: true };
S = { engine: "soniox", voice: "builtin", voice_provider: "soniox", voice_name: "Adrian", soniox_voice_id: null,
      cartesia_voice_id: null, cartesia_builtin_id: null, inworld_voice_name: "Clive", inworld_voice_id: null,
      voice_delay: "balanced", volume: 1, speed: 1.1, me_on: true, listen_on: true };
bindUi();
const toasts = () => [$("#toast").textContent, $("#toast").classList.contains("bad")];
"""


def run_js(scenario):
    """Run `scenario` (the body of an async function, after SETUP) against ui/app.js; returns what it returns."""
    run = subprocess.run([NODE, "-e", HARNESS, str(APP_JS)], input=SETUP + scenario, capture_output=True,
                         text=True, encoding="utf-8", timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_switching_providers_keeps_my_clone():
    """No Cartesia clone: Cartesia speaks a stock voice, and back on Soniox the call hears my clone again."""
    result = run_js(r"""
    Object.assign(S, { voice: "clone", soniox_voice_id: "s-1" });
    const seen = [];
    for (const name of ["cartesia", "soniox"]) {
      await pickProvider(name);
      seen.push([S.voice_provider, S.voice, els["#cloneState"].textContent]);
    }
    return seen;
    """)
    assert result == [["cartesia", "builtin", "не создан"], ["soniox", "clone", "готов ✓ · Soniox"]]


def test_a_stock_voice_picked_by_hand_stays():
    result = run_js(r"""
    Object.assign(S, { voice: "clone", soniox_voice_id: "s-1" });
    renderVoice();
    await els["#voiceList"].children[1].onclick();  // Adrian, below «Мой голос (клон)»
    const badge = els["#cloneState"].textContent;
    await pickProvider("cartesia");
    await pickProvider("soniox");
    return [badge, S.voice, els["#cloneState"].textContent];
    """)
    assert result == ["готов, но не выбран", "builtin", "готов, но не выбран"]  # a clone exists, the call won't hear it


def test_the_engine_is_not_switched_mid_call():
    result = run_js(r"""
    const openai = el("button");
    openai.dataset.engine = "openai";
    all["#engineSeg [data-engine]"] = [openai];
    bindUi();
    running = true;
    await openai.onclick();
    const blocked = [saved.length, S.engine, ...toasts()];
    running = false;
    await openai.onclick();
    return [blocked, saved, S.engine];
    """)
    blocked, saved, engine = result
    assert blocked[:2] == [0, "soniox"] and "Остановите перевод" in blocked[2] and blocked[3] is True
    assert (saved, engine) == ([{"engine": "openai", "engine_auto": False}], "openai")


def test_the_call_check_cannot_be_passed_with_my_microphone_off():
    """Step 2 switches my microphone off: «Начать перевод» waits until step 3 switched it on again."""
    result = run_js(r"""
    muted = true;
    openCallCheck(() => {}, "Начать перевод");
    els["#ccDone"].checked = true;
    els["#ccDone"].onchange({ target: els["#ccDone"] });
    const whileMuted = els["#ccStart"].disabled;
    muted = false;
    renderMute();  // what poll() does when the hotkey switches it back on
    return [whileMuted, els["#ccStart"].disabled];
    """)
    assert result == [True, False]


def test_starting_with_my_microphone_off_is_flagged():
    result = run_js(r"""
    api.start = async () => ({ ok: true, started: 1 });
    muted = true;
    S.me_on = false;  // only listening: my microphone is not translated anyway
    await startRun();
    const listening = toasts();
    S.me_on = true;
    await startRun();
    return [listening, toasts()];
    """)
    listening, (text, bad) = result
    assert listening == ["", False]
    assert bad and "Микрофон программы выключен" in text and "Ctrl+Alt+M" in text


def test_a_class_on_the_body_never_picks_up_a_rule_of_a_button():
    """«Поменять местами» put .swap on <body>, and the 32 px .swap rule of the language button laid the window out."""
    css = (APP_JS.parent / "app.css").read_text(encoding="utf-8")
    on_body = set(re.findall(r'document\.body\.classList\.(?:toggle|add|remove)\("([\w-]+)"',
                             APP_JS.read_text(encoding="utf-8")))
    assert {"live", "simple", "dst-only"} <= on_body
    for name in on_body:  # `.name` alone (not body.name, not .other.name) would also match <body>
        assert not re.search(rf"(?:^|[\s,>+~])\.{re.escape(name)}(?![\w-])", css, re.M), name
    result = run_js(r"""
    Object.assign(S, { swap: true, font: 18, panel: "single", text_mode: "both" });
    applyView();
    return ["swap", "swap-order"].map((c) => document.body.classList.contains(c));
    """)
    assert result == [False, True]


def test_the_main_window_keeps_showing_the_pause():
    """❚❚ in the mini-subtitles: a status event (the idle voice reconnecting) must not paint the call green."""
    result = run_js(r"""
    running = true;
    const resumed = [];
    api.set_paused = async (value) => { resumed.push(value); return value; };
    api.poll = async () => ({ events: [{ seq: 1, type: "status", label: "Мой голос", text: "подключено", ok: true }],
                              me: 0, them: 0, running: true, muted: false, paused: true });
    const seen = () => [els["#statusText"].textContent, els["#statusDot"].className, els["#resumeBtn"].hidden];
    await poll();
    const polled = seen();
    handle({ type: "status", label: "Я → EN", text: "подключено", ok: true });
    const afterStatus = seen();
    await els["#resumeBtn"].onclick();
    handle({ type: "paused", value: false });
    return [polled, afterStatus, resumed, seen()];
    """)
    polled, after_status, resumed, resumed_seen = result
    assert polled == after_status == ["Пауза — перевод остановлен", "sdot connecting", False]
    assert resumed == [False]
    assert resumed_seen == ["Мой голос ✓   ·   Я → EN ✓", "sdot ok", True]
