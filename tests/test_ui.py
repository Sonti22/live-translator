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
  createElement: (tag) => el(tag), createTextNode: (text) => ({ text }), addEventListener() {}, body: el("body"),
  documentElement: el("html"),
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
      voice_delay: "balanced", volume: 1, speed: 1.0, me_on: true, listen_on: true, voice_out: true };
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


def test_a_provider_picked_by_hand_ends_the_automatic_choice():
    result = run_js(r"""
    S.provider_auto = true;
    await pickProvider("cartesia");
    return [saved, S.provider_auto];
    """)
    assert result == [[{"voice_provider": "cartesia", "provider_auto": False}], False]


def test_a_saved_key_shows_the_voice_the_app_chose():
    """The Cartesia key made Cartesia the voice: the provider row and the model chip show it at once."""
    result = run_js(r"""
    state.keys.cartesia = false;
    api.set_key = async () => ({ ok: true, notice: "Голос теперь синтезирует Cartesia", engine: "soniox",
                                 settings: { voice_provider: "cartesia" } });
    $('[data-key-input="cartesia"]').value = "key";
    await saveKey("cartesia");
    return [S.voice_provider, els["#modelChip"].textContent, toasts()[0]];
    """)
    assert result == ["cartesia", "Soniox · stt-rt-v5 + Cartesia sonic-3.6", "Голос теперь синтезирует Cartesia"]


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


def test_the_call_check_can_be_done_without_the_hotkey():
    """Ctrl+Alt+M taken by another program: the «Микрофон» button of the window is under the check, its own does it."""
    result = run_js(r"""
    state.hotkey = null;
    all[".cc-key"] = [el(), el()];
    const calls = [];
    api.set_muted = async (value) => { calls.push(value); muted = value; renderMute(); return value; };
    openCallCheck(() => {}, "Начать перевод");
    const keys = all[".cc-key"].map((b) => b.textContent);
    await els["#ccMute"].onclick();
    const off = [els["#ccMute"].textContent, els["#ccStart"].disabled];
    await els["#ccMute"].onclick();
    return [keys, calls, off, els["#ccMute"].textContent];
    """)
    keys, calls, off, on = result
    assert keys == ["кнопку «Микрофон» в шаге 3"] * 2
    assert calls == [True, False] and off == ["Микрофон выкл", True] and on == "Микрофон вкл"


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


def test_starting_with_the_voice_switched_off_is_flagged():
    """«Озвучка перевода в звонок» off is remembered across launches: the call would hear nothing."""
    result = run_js(r"""
    api.start = async () => ({ ok: true, started: 1 });
    S.voice_out = false;
    S.me_on = false;  // only listening: nobody is meant to hear me
    await startRun();
    const listening = toasts();
    S.me_on = true;
    await startRun();
    const warned = toasts();
    handle({ type: "status", label: "Я → EN", text: "подключено", ok: true });
    return [listening, warned, els["#statusText"].textContent, els["#statusDot"].className];
    """)
    listening, (text, bad), status, dot = result
    assert listening == ["", False]
    assert bad and "Озвучка перевода в звонок выключена" in text
    assert "Озвучка в звонок: выключена" in status and dot == "sdot bad"  # never an all-green line


def test_start_while_the_last_call_is_still_closing_says_so():
    result = run_js(r"""
    api.start = async () => ({ ok: false, error: "stopping" });
    els["#settings"].hidden = true;
    await startRun();
    return [...toasts(), els["#settings"].hidden, running];
    """)
    text, bad, settings_hidden, running = result
    assert bad and "ещё останавливается" in text
    assert settings_hidden and running is False  # no settings dialog, no timer counting from 1970


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


def test_a_restart_in_the_next_pause_is_announced():
    result = run_js(r"""
    api.save_settings = async () => ({ restarted: false, pending: true });
    running = true;
    await save({ speed: 1.2 });
    const pending = toasts();
    handle({ type: "restarted" });  // the pause came: the new engine takes over
    return [pending, els["#statusText"].textContent];
    """)
    (text, bad), status = result
    assert "в ближайшей паузе" in text and not bad
    assert status == "Перезапуск с новыми настройками…"


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


# --- recording my voice ----------------------------------------------------------------------------------------

# A fake clock and timer (`at(sec)` is that many seconds into the recording), a headset among the microphones and the
# recording api answering `stopped`; every call to it is in `calls`.
REC_SETUP = r"""
let now = 1000000, timers = 0, cleared = 0;
Date.now = () => now;
setInterval = () => ++timers;
clearInterval = () => { cleared++; };
const calls = [];
state.mics = ["Microphone Array (Realtek)", "Headset (Jabra Evolve)", "CABLE Output (VB-Audio)"];
state.default_mic = "Microphone Array (Realtek)";
S.mic = null;  // the call uses the Windows default microphone
let stopped = { ok: true, seconds: 40, speech_seconds: 35, verdict: "ok" };
api.get_state = async () => ({ mics: state.mics, default_mic: state.default_mic });
api.start_recording = async (mic) => { calls.push(["start", mic]); return { ok: true, rate: 48000, min: 30, max: 60 }; };
api.stop_recording = async () => { calls.push(["stop"]); return stopped; };
api.cancel_recording = () => { calls.push(["cancel"]); };
api.open_sound_settings = () => { calls.push(["sound"]); };
const settle = async () => { for (let i = 0; i < 10; i++) await null; };
const at = (sec) => { now = 1000000 + sec * 1000; recTick(); };
const names = () => $("#recMics").children.map((li) => li.children[0].text);
const marks = () => $("#recMics").children.map((li) => li.className);
const view = () => [$("#recTime").textContent, $("#recDone").hidden, $("#recDone").disabled, $("#recRetry").hidden,
                    $("#recCreate").hidden];
"""


def run_rec(scenario):
    return run_js(REC_SETUP + scenario)


def test_the_recorder_lists_the_microphones_without_the_cable_and_marks_a_headset():
    result = run_rec(r"""
    openRecorder();
    await settle();
    const first = [names(), marks(), $("#recorder").hidden];
    state.default_mic = "Headset (Jabra Evolve)";  // the call's own microphone is a headset
    renderRecMics();
    return [first, marks()];
    """)
    first, headset_default = result
    assert first == [["Микрофон по умолчанию", "Microphone Array (Realtek)", "Headset (Jabra Evolve)"],
                     ["sel", "", "headset"], False]
    assert headset_default == ["sel headset", "", "headset"]


def test_the_recorder_lists_the_microphone_of_the_call_once_it_is_chosen():
    result = run_rec(r"""
    S.mic = "Headset (Jabra Evolve)";
    openRecorder();
    await settle();
    return [names(), marks()];
    """)
    assert result == [["Microphone Array (Realtek)", "Headset (Jabra Evolve)"], ["", "sel headset"]]


def test_a_headset_plugged_in_after_the_window_started_shows_up_in_the_recorder():
    result = run_rec(r"""
    api.get_state = async () => ({ mics: [...state.mics, "Blue Yeti"], default_mic: state.default_mic });
    openRecorder();
    const before = names().length;
    await settle();
    return [before, names(), marks()];
    """)
    before, names, marks = result
    assert before == 3 and names[-1] == "Blue Yeti" and marks[-1] == "headset"


def test_the_picked_microphone_is_what_the_recording_uses_else_the_calls_own():
    result = run_rec(r"""
    openRecorder();
    await settle();
    await startRecording();
    const busy = $("#recMics").className;
    await finishRecording();
    const idle = $("#recMics").className;
    $("#recMics").children[2].onclick();  // the headset
    await startRecording();
    return [calls.filter((c) => c[0] === "start"), busy, idle, marks()];
    """)
    starts, busy, idle, marks = result
    assert starts == [["start", None], ["start", "Headset (Jabra Evolve)"]]
    assert "off" in busy and "off" not in idle  # no switching microphones in the middle of a recording
    assert sorted(marks[2].split()) == ["headset", "sel"]


def test_done_opens_at_thirty_seconds_and_the_recording_stops_itself_at_sixty():
    result = run_rec(r"""
    openRecorder();
    await settle();
    const idle = [$("#recDone").hidden, $("#recTime").textContent, $("#recStart").disabled];
    await startRecording();
    const hint = $("#recHint").textContent;
    const stages = [view(), (at(10), view()), (at(29.9), view()), (at(30), view())];
    const ready = $("#recHint").textContent;
    at(59.9);
    const before = [calls.filter((c) => c[0] === "stop").length, cleared];
    at(64);  // a window that slept: the clock stops at the limit
    const capped = $("#recTime").textContent;
    await settle();
    const stops = () => [calls.filter((c) => c[0] === "stop").length, cleared, timers];
    const after = stops();
    await finishRecording();  // a late click on Done: the recording is over, nothing is stopped twice
    return [idle, hint, stages, before, capped, after, stops(), view(), $("#recHint").textContent, ready];
    """)
    idle, hint, stages, before, capped, after, later, final, verdict, ready = result
    assert idle == [True, "00:00 / 01:00", False]
    assert "Идёт запись" in hint and "через 30 секунд" in hint
    assert "Можно заканчивать" in ready and "через" not in ready  # once Done is open the hint stops promising it
    assert stages == [["00:00 / 01:00", False, True, True, True], ["00:10 / 01:00", False, True, True, True],
                      ["00:29 / 01:00", False, True, True, True], ["00:30 / 01:00", False, False, True, True]]
    assert before == [0, 0] and capped == "01:00 / 01:00" and after == later == [1, 1, 1]
    assert final == ["00:00 / 01:00", True, True, False, False]  # waiting again: Retry and Create are on
    assert verdict == "Записано 40 с, речи 35 с — отлично. Можно создавать клон."


@pytest.mark.parametrize("verdict, text, can_create", [
    ("ok", "Записано 40 с, речи 35 с — отлично. Можно создавать клон.", True),
    ("quiet", "Слишком тихо — говорите громче или ближе к микрофону и перезапишите.", True),
    ("clipped", "Перегруз — звук искажён: отодвиньте микрофон и перезапишите.", True),
    ("noisy", "Шумно — запишите в тихой комнате.", True),
    ("short", "Мало речи — нужно хотя бы 20 с. Перезапишите.", False),
    ("something-new", "Записано 40 с, речи 35 с — отлично. Можно создавать клон.", True),
])
def test_the_recording_is_judged_in_words_and_only_one_without_speech_cannot_become_a_clone(verdict, text, can_create):
    result = run_rec(f"stopped.verdict = {json.dumps(verdict)};" + r"""
    openRecorder();
    await settle();
    await startRecording();
    at(35);
    await finishRecording();
    return [$("#recHint").textContent, $("#recRetry").hidden, $("#recCreate").hidden, $("#recStart").disabled];
    """)
    assert result == [text, False, not can_create, False]


@pytest.mark.parametrize("verdict, retry, create, label", [
    ("ok", "ghost", "primary", "Создать клон"),
    ("something-new", "ghost", "primary", "Создать клон"),
    ("quiet", "primary", "ghost", "Всё равно создать клон"),
    ("clipped", "primary", "ghost", "Всё равно создать клон"),
    ("noisy", "primary", "ghost", "Всё равно создать клон"),
    ("short", "primary", "ghost", "Всё равно создать клон"),  # hidden anyway: it cannot be created
])
def test_a_weak_recording_is_recorded_again_first_and_made_into_a_clone_only_reluctantly(verdict, retry, create, label):
    result = run_rec(f"stopped.verdict = {json.dumps(verdict)};" + r"""
    const buttons = () => [$("#recRetry").className, $("#recCreate").className, $("#recCreate").textContent];
    openRecorder();
    await settle();
    await startRecording();
    at(35);
    await finishRecording();
    const shown = buttons();
    api.create_clone = async () => ({ ok: false, error: "Клон не получился" });
    await createClone();  // a failed creation gives the button back the way it was
    const failed = [...buttons(), $("#recCreate").disabled, toasts()[0]];
    stopped.verdict = "ok";  // the next recording is judged on its own
    await startRecording();
    at(35);
    await finishRecording();
    return [shown, failed, buttons()];
    """)
    shown, failed, again = result
    assert shown == [retry, create, label]
    assert failed == [retry, create, label, False, "Клон не получился"]
    assert again == ["ghost", "primary", "Создать клон"]


def test_the_warning_on_a_weak_recordings_clone_button_is_its_label_and_tooltip():
    result = run_rec(r"""
    stopped.verdict = "quiet";
    openRecorder();
    await settle();
    await startRecording();
    at(35);
    await finishRecording();
    return $("#recCreate").title;
    """)
    assert "хуже" in result


def test_a_clone_made_at_the_fallback_provider_says_so():
    result = run_rec(r"""
    const note = "Клон в Cartesia не получился (нужен тариф Pro) — голос создан в Soniox.";
    api.create_clone = async () => ({ ok: true, provider: "soniox", note });
    api.get_state = async () => ({ ...state, settings: { ...S, voice: "clone", soniox_voice_id: "s-1" } });
    await createClone();
    return [toasts(), $("#recorder").hidden, S.voice, S.soniox_voice_id];
    """)
    assert result == [["Клон в Cartesia не получился (нужен тариф Pro) — голос создан в Soniox.", False], True,
                      "clone", "s-1"]


CHOSEN = r"""
const chosen = { notice: "Голос теперь синтезирует Cartesia — самый быстрый и похожий на вас.",
                 settings: { ...S, voice_provider: "cartesia" } };
"""


def test_the_recorder_shows_the_voice_the_app_chose_while_it_read_the_state():
    result = run_rec(CHOSEN + r"""
    api.get_state = async () => ({ mics: state.mics, default_mic: state.default_mic, ...chosen });
    openRecorder();
    await settle();
    return [S.voice_provider, els["#modelChip"].textContent, toasts()];
    """)
    assert result == ["cartesia", "Soniox · stt-rt-v5 + Cartesia sonic-3.6",
                      ["Голос теперь синтезирует Cartesia — самый быстрый и похожий на вас.", False]]


def test_the_choice_of_the_app_is_shown_even_when_a_recording_has_started_meanwhile():
    result = run_rec(CHOSEN + r"""
    let answer;
    api.get_state = () => new Promise((resolve) => { answer = resolve; });
    openRecorder();
    await startRecording();
    answer({ mics: [...state.mics, "Blue Yeti"], default_mic: state.default_mic, ...chosen });
    await settle();
    return [S.voice_provider, toasts()[0], names()];
    """)
    assert result[:2] == ["cartesia", "Голос теперь синтезирует Cartesia — самый быстрый и похожий на вас."]
    assert result[2] == ["Микрофон по умолчанию", "Microphone Array (Realtek)", "Headset (Jabra Evolve)"]


def test_the_settings_of_a_state_without_a_notice_are_not_taken_over_by_the_recorder():
    """Nothing was chosen by the app: what the page already shows (a switch flipped a moment ago) stays."""
    result = run_rec(CHOSEN + r"""
    api.get_state = async () => ({ mics: state.mics, default_mic: state.default_mic, settings: chosen.settings });
    openRecorder();
    await settle();
    return [S.voice_provider, toasts()[0]];
    """)
    assert result == ["soniox", ""]


def test_the_recorder_tips_tell_to_turn_the_windows_sound_enhancements_off():
    html = (APP_JS.parent / "index.html").read_text(encoding="utf-8")
    tips = re.search(r'<ul class="rec-tips">(.*?)</ul>', html, re.S).group(1)
    assert "«Улучшения звука»" in tips and "шумоподавление" in tips
    assert html.index("Улучшения звука") < html.index('id="recSoundSettings"')  # next to the button that opens them


def test_the_voice_menu_says_the_preview_is_one_phrase_whatever_the_delivery():
    html = (APP_JS.parent / "index.html").read_text(encoding="utf-8")
    field = re.search(r'<div class="field" id="deliveryField">(.*?)\n  </div>', html, re.S).group(1)
    assert "«▶ Прослушать» — одна фраза целиком" in field and "только в звонке" in field


def test_the_recorder_opens_on_the_calls_microphone_not_the_one_picked_last_time():
    result = run_rec(r"""
    openRecorder();
    await settle();
    $("#recMics").children[2].onclick();
    $("#recClose").onclick();
    openRecorder();
    await settle();
    await startRecording();
    return calls.filter((c) => c[0] === "start");
    """)
    assert result == [["start", None]]


def test_the_microphones_are_not_redrawn_under_a_recording_that_has_started():
    result = run_rec(r"""
    let answer;
    api.get_state = () => new Promise((resolve) => { answer = resolve; });
    openRecorder();
    await startRecording();
    answer({ mics: [...state.mics, "Blue Yeti"], default_mic: state.default_mic });
    await settle();
    return names();
    """)
    assert result == ["Микрофон по умолчанию", "Microphone Array (Realtek)", "Headset (Jabra Evolve)"]


def test_closing_the_recorder_while_it_is_being_stopped_shows_nothing_afterwards():
    result = run_rec(r"""
    let finish;
    api.stop_recording = () => new Promise((resolve) => { finish = resolve; });
    openRecorder();
    await settle();
    await startRecording();
    at(40);
    const stopping = finishRecording();
    $("#recClose").onclick();
    finish({ ok: true, seconds: 40, speech_seconds: 35, verdict: "ok" });
    await stopping;
    return [$("#recorder").hidden, $("#recHint").textContent, $("#recRetry").hidden, $("#recCreate").hidden];
    """)
    hidden, hint, retry_hidden, create_hidden = result
    assert hidden and "Нажмите красную кнопку" in hint and retry_hidden and create_hidden  # not a verdict of a closed window


def test_a_microphone_that_will_not_start_says_why_and_can_be_tried_again():
    result = run_rec(r"""
    api.start_recording = async () => ({ ok: false, error: "Микрофон недоступен: занят" });
    openRecorder();
    await settle();
    await startRecording();
    const failed = [$("#recHint").textContent, $("#recStart").disabled, $("#recDone").hidden, timers, $("#recMics").className];
    api.start_recording = async () => { throw new Error("bridge lost"); };
    await startRecording();
    const thrown = [$("#recHint").textContent, timers];
    api.start_recording = async () => ({ ok: true, rate: 44100, min: 30, max: 60 });
    await $("#recStart").onclick();
    return [failed, thrown, timers, $("#recDone").hidden];
    """)
    failed, thrown, timers, done_hidden = result
    assert failed == ["Микрофон недоступен: занят", False, True, 0, ""]
    assert thrown == ["Не удалось записать: bridge lost", 0]
    assert timers == 1 and done_hidden is False


def test_a_recording_that_gave_no_sound_says_so_and_offers_a_new_one():
    result = run_rec(r"""
    stopped = { ok: false, error: "Микрофон не дал звука." };
    openRecorder();
    await settle();
    await startRecording();
    at(31);
    await finishRecording();
    return [$("#recHint").textContent, $("#recStart").disabled, $("#recRetry").hidden, $("#recCreate").hidden,
            $("#recDone").hidden, timers, cleared];
    """)
    assert result == ["Микрофон не дал звука.", False, True, True, True, 1, 1]


def test_closing_the_recorder_throws_a_recording_in_progress_away():
    result = run_rec(r"""
    openRecorder();
    await settle();
    await startRecording();
    at(12);
    $("#recClose").onclick();
    const closed = [calls.filter((c) => c[0] !== "start"), cleared, $("#recorder").hidden, $("#recStart").disabled,
                    $("#recMics").className, $("#recDone").hidden, $("#recTime").textContent];
    await finishRecording();  // nothing left to stop
    openRecorder();  // and the window opens fresh
    const reopened = [$("#recorder").hidden, $("#recHint").textContent];
    $("#recClose").onclick();  // closing with nothing recorded cancels nothing
    return [closed, calls.filter((c) => c[0] === "stop" || c[0] === "cancel").length, reopened];
    """)
    closed, calls, reopened = result
    assert closed == [[["cancel"]], 1, True, False, "", True, "00:00 / 01:00"]
    assert calls == 1 and reopened[0] is False and "Нажмите красную кнопку" in reopened[1]


def test_closing_the_recorder_while_the_microphone_is_starting_leaves_no_timer_and_no_open_microphone():
    result = run_rec(r"""
    let open;
    api.start_recording = (mic) => new Promise((resolve) => { calls.push(["start", mic]); open = resolve; });
    openRecorder();
    await settle();
    const starting = startRecording();
    $("#recClose").onclick();
    open({ ok: true, rate: 48000, min: 30, max: 60 });  // the microphone opened after the window was closed
    await starting;
    return [calls.map((c) => c[0]), timers, $("#recDone").hidden, $("#recorder").hidden];
    """)
    assert result == [["start", "cancel", "cancel"], 0, True, True]  # the last cancel closes what opened late


def test_the_sound_settings_button_opens_the_windows_page_through_the_app():
    assert run_rec(r"""
    $("#recSoundSettings").onclick();
    return calls;
    """) == [["sound"]]


def test_the_recorder_meter_follows_the_level_the_app_reports():
    result = run_rec(r"""
    buildTicks($("#recMeter"), 24);
    const lit = () => $("#recMeter").children.filter((t) => t.classList.contains("lit")).length;
    const poll_with = async (extra) => {
      api.poll = async () => ({ events: [], me: 0, them: 0, muted: false, paused: false, running: false, ...extra });
      await poll();
      return lit();
    };
    return [await poll_with({ rec: 0.5 }), await poll_with({ rec: 0 }), await poll_with({})];
    """)
    assert result == [19, 0, 0]  # 0.5 * 1.6 of 24 ticks; an app without the level lights nothing


# --- delivery, pace matching, Soniox region ----------------------------------------------------------

DELIVERY_SETUP = r"""
const deliveryButtons = ["fast", "balanced", "natural"].map((name) => { const b = el("button"); b.dataset.delivery = name; return b; });
const hints = [el(), el()];  // the one of the voice menu and the one of the settings
all["[data-delivery]"] = deliveryButtons;
all[".delivery-hint"] = hints;
bindUi();
const active = () => deliveryButtons.map((b) => b.classList.contains("active"));
"""


def test_the_delivery_switch_saves_and_marks_both_of_its_places():
    result = run_js(DELIVERY_SETUP + r"""
    renderVoice();
    const before = [active(), hints[0].textContent, S.delivery];
    await deliveryButtons[2].onclick();
    return [before, saved, active(), hints.map((h) => h.textContent)];
    """)
    before, saved, active, hints = result
    assert before[0] == [False, True, False] and before[1].startswith("Целые предложения") and before[2] is None
    assert saved == [{"delivery": "natural"}] and active == [False, False, True]
    assert hints == 2 * ["Самая живая речь: без ускорения и обрезки пауз, на длинных фразах на 0,2–0,4 с позже."]


def test_an_unknown_delivery_is_shown_as_balance():
    result = run_js(DELIVERY_SETUP + r"""
    S.delivery = "sudden";
    renderVoice();
    return active();
    """)
    assert result == [False, True, False]


def test_the_fast_delivery_leans_on_the_done_hotkey_when_it_is_registered():
    result = run_js(DELIVERY_SETUP + r"""
    S.delivery = "fast";
    state.hotkey_done = "Ctrl+Alt+Space";
    renderVoice();
    const withKey = hints[0].textContent;
    state.hotkey_done = null;  // taken by another program
    renderVoice();
    const without = hints[0].textContent;
    S.delivery = "balanced";
    state.hotkey_done = "Ctrl+Alt+Space";
    renderVoice();
    return [withKey, without, hints[1].textContent];
    """)
    with_key, without, balanced = result
    assert with_key.startswith("Английский звучит сразу по кускам фразы")
    assert with_key.endswith("Жмите Ctrl+Alt+Space в конце фразы — английский сразу.")
    assert "Жмите" not in without and "Жмите" not in balanced


def test_the_openai_engine_has_no_delivery_and_its_clone_gets_the_delay_switch():
    result = run_js(r"""
    const shown = () => ["#deliveryField", "#deliverySection", "#delayField"].map((id) => !$(id).hidden);
    renderVoice();
    const soniox = shown();
    S.engine = "openai";
    renderVoice();
    const openai = shown();
    S.voice = "clone";
    renderVoice();
    return [soniox, openai, shown()];
    """)
    assert result == [[True, True, False], [False, False, False], [False, False, True]]


def test_pace_matching_is_a_lever_that_is_on_until_switched_off():
    result = run_js(r"""
    applyView();
    const fresh = $("#matchRate").checked;
    $("#matchRate").checked = false;
    await $("#matchRate").onchange({ target: $("#matchRate") });
    applyView();
    return [fresh, saved, $("#matchRate").checked];
    """)
    assert result == [True, [{"match_rate": False}], False]


REGION_SETUP = r"""
const seg = ["", "eu"].map((region) => { const b = el("button"); b.dataset.region = region; return b; });
all["#regionSeg [data-region]"] = seg;
bindUi();
const marked = () => seg.map((b) => b.classList.contains("active"));
"""


def test_the_soniox_region_is_saved_and_asks_for_the_key_of_that_region():
    result = run_js(REGION_SETUP + r"""
    S.soniox_region = "";
    renderRegion();
    const before = marked();
    await seg[0].onclick();  // already there: nothing to save
    const same = saved.length;
    await seg[1].onclick();
    return [before, same, saved, S.soniox_region, marked(), toasts()];
    """)
    before, same, saved, region, marked, (text, bad) = result
    assert before == [True, False] and same == 0 and saved == [{"soniox_region": "eu"}] and region == "eu"
    assert marked == [False, True] and "ключ Soniox из проекта в регионе EU" in text and not bad


def test_the_soniox_region_is_not_switched_mid_call():
    result = run_js(REGION_SETUP + r"""
    running = true;
    await seg[1].onclick();
    return [saved.length, S.soniox_region, ...toasts()];
    """)
    assert result[:2] == [0, None] and "Во время перевода регион не переключить" in result[2] and result[3] is True


def test_every_element_the_script_looks_up_by_id_is_on_the_page():
    """A renamed or forgotten id would stop bindUi() half way: nothing after it would react to a click."""
    page = (APP_JS.parent / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([\w-]+)"', page))
    wanted = set(re.findall(r'\$\("#([\w-]+)"\)', APP_JS.read_text(encoding="utf-8")))
    assert sorted(wanted - ids) == []


# --- audit: the voice picker of a provider without a chosen stock voice, a failing voice list, a refused key ------

def test_a_cartesia_row_without_an_id_or_name_is_never_the_selected_voice():
    """cartesia_builtin_id is null until a voice is picked: the key of the current voice was "builtin:undefined"."""
    result = run_js(r"""
    Object.assign(S, { voice_provider: "cartesia", cartesia_builtin_id: null });
    api.list_voices = async () => ({ ok: true, provider: "cartesia", voices: [{ id: "c-1", name: "Blake" }, {}] });
    await loadVoices();
    const marked = () => els["#voiceList"].children.map((li) => li.classList.contains("sel"));
    const unset = marked();
    S.cartesia_builtin_id = "c-1";
    renderVoiceList();
    return [unset, marked()];
    """)
    assert result == [[False, False, False], [False, True, False]]


def test_a_failed_voice_list_is_shown_instead_of_swallowed():
    result = run_js(r"""
    Object.assign(S, { voice_provider: "cartesia" });
    const note = () => [$("#voiceNote").hidden, $("#voiceNote").textContent];
    api.list_voices = async () => ({ ok: false, error: "Cartesia отклонила ключ (HTTP 401)" });
    await loadVoices();
    const refused = note();
    api.list_voices = async () => { throw new Error("no bridge"); };
    await loadVoices();
    const thrown = note();
    api.list_voices = async () => ({ ok: true, provider: "cartesia", voices: [{ id: "c-1", name: "Blake" }] });
    await loadVoices();
    return [refused, thrown, note(), els["#voiceList"].children.length];
    """)
    refused, thrown, healed, rows = result
    assert refused[0] is False and "Cartesia" in refused[1] and "Cartesia отклонила ключ (HTTP 401)" in refused[1]
    assert thrown[0] is False and "Cartesia" in thrown[1] and "no bridge" not in thrown[1]
    assert healed[0] is True and rows == 2  # the list came, the note went


def test_the_voice_note_is_not_shown_for_a_provider_without_a_key_or_another_engine():
    result = run_js(r"""
    const calls = [];
    api.list_voices = async () => { calls.push(1); return { ok: false, error: "x" }; };
    $("#voiceNote").hidden = false;
    state.keys.soniox = false;
    await loadVoices();
    const noKey = $("#voiceNote").hidden;
    state.keys.soniox = true;
    S.engine = "openai";
    $("#voiceNote").hidden = false;
    await loadVoices();
    return [noKey, $("#voiceNote").hidden, calls.length];
    """)
    assert result == [True, True, 0]


def test_a_voice_list_that_answers_for_a_provider_already_left_is_ignored():
    result = run_js(r"""
    let release;
    api.list_voices = () => new Promise((resolve) => { release = resolve; });
    const pending = loadVoices();  // Soniox
    S.voice_provider = "cartesia";
    release({ ok: false, error: "late" });
    await pending;
    return [$("#voiceNote").hidden, $("#voiceNote").textContent];
    """)
    assert result == [False, ""]  # untouched: the note belongs to the provider on screen


def test_a_key_the_app_refuses_to_store_is_reported_and_stays_in_the_field():
    result = run_js(r"""
    api.set_key = async () => { throw new Error("ValueError"); };
    $('[data-key-input="inworld"]').value = "pasted-with-a-break";
    await saveKey("inworld");
    return [toasts(), state.keys.inworld, $('[data-key-input="inworld"]').value];
    """)
    (text, bad), stored, field = result
    assert bad is True and text.startswith("Ключ не сохранён") and "ValueError" not in text
    assert stored is False and field == "pasted-with-a-break"
