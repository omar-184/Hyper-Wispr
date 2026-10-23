"use strict";

/* Hyper-Wispr settings page. Talks to Python through window.pywebview.api
   (see settings_ui/__init__.py). Every change is saved right away; the
   running app notices settings.json changed and applies it. */

const S = {
  settings: null,
  defaults: null,
  info: null,
  page: "general",
  gpu: null,
  mics: null,
  ollama: null,
  moreLanguages: false,
};
const api = () => window.pywebview.api;

/* ---- tiny DOM helpers ---------------------------------------------------- */
function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "html") node.innerHTML = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const ICONS = {
  general: '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  speech: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>',
  ai: '<path d="M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8z"/><path d="M18.5 15.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
  vocab: '<path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v16H5.5A1.5 1.5 0 0 1 4 18.5zM20 5.5A1.5 1.5 0 0 0 18.5 4H13v16h5.5a1.5 1.5 0 0 0 1.5-1.5z"/>',
  history: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  about: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.6v.1"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5.5A1.5 1.5 0 0 0 14.5 4h-9A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  trash: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 12.5h9l1-12.5"/>',
  x: '<path d="M7 7l10 10M17 7L7 17"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
};
function icon(name) {
  const span = document.createElement("span");
  span.style.display = "contents";
  span.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
  return span.firstChild;
}

/* ---- settings access ----------------------------------------------------- */
function get(path) {
  return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), S.settings);
}
function has(path) {
  return get(path) !== undefined;
}
function set(path, value) {
  const keys = path.split(".");
  let o = S.settings;
  for (const k of keys.slice(0, -1)) {
    if (typeof o[k] !== "object" || o[k] === null) o[k] = {};
    o = o[k];
  }
  o[keys.at(-1)] = value;
  scheduleSave();
}

let saveTimer = null;
let savePending = false;
function scheduleSave() {
  savePending = true;
  setStatus("saving");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveNow, 350);
}
async function saveNow() {
  clearTimeout(saveTimer);
  if (!savePending) return;
  savePending = false;
  const res = await api().save_settings(S.settings);
  setStatus(res && res.ok ? "saved" : "error", res && res.error);
}
// Text fields save as you type; make sure the last keystrokes land before the window goes away.
window.addEventListener("beforeunload", () => { if (savePending) saveNow(); });

function setStatus(kind, error) {
  const el = document.getElementById("status");
  el.className = "status " + kind;
  const text = { saving: "Saving…", saved: "All changes saved", error: "Couldn't save: " + (error || "unknown error") }[kind] || "";
  el.replaceChildren(h("span", { class: "dot", "aria-hidden": "true" }), h("span", {}, text));
  el.title = text;
}

/* ---- building blocks ----------------------------------------------------- */
let uid = 0;
const nextId = () => "f" + ++uid;

function section(title, rows, note) {
  return h("section", { class: "section" },
    title && h("h2", { class: "section-title" }, title),
    h("div", { class: "card" }, rows),
    note && h("p", { class: "section-note" }, note));
}

/* A row: label + description on the left, control on the right. `control` may be a
   function receiving the id to put on the input, so the label is properly linked. */
function row(label, desc, control, opts = {}) {
  const id = nextId();
  const ctl = typeof control === "function" ? control(id) : control;
  return h("div", { class: "row" + (opts.stack ? " stack" : "") + (opts.disabled ? " disabled" : "") },
    h("div", { class: "row-text" },
      h("label", { class: "row-label", for: id, id: id + "-l" }, label),
      desc && h("div", { class: "row-desc", id: id + "-d" }, desc)),
    ctl && h("div", { class: "row-control" }, ctl));
}

function toggle(path, onChange) {
  return (id) => {
    const btn = h("button", {
      class: "switch", role: "switch", id, type: "button",
      "aria-checked": String(!!get(path)), "aria-describedby": id + "-d",
      onclick: () => {
        const v = !get(path);
        set(path, v);
        btn.setAttribute("aria-checked", String(v));
        if (onChange) onChange(v);
      },
    });
    return btn;
  };
}

function select(path, options, { onChange, toValue = (v) => v } = {}) {
  return (id) => {
    const current = get(path);
    const sel = h("select", {
      id, "aria-describedby": id + "-d",
      onchange: () => {
        const opt = options[sel.selectedIndex];
        set(path, toValue(opt.value));
        if (onChange) onChange(opt.value);
      },
    }, options.map((o) => h("option", { value: String(o.value) }, o.label)));
    const idx = options.findIndex((o) => o.value === current || String(o.value) === String(current ?? ""));
    sel.selectedIndex = idx >= 0 ? idx : 0;
    return h("div", { class: "select-wrap" }, sel);
  };
}

function segmented(path, options, onChange) {
  return (id) => {
    const group = h("div", { class: "segmented", role: "radiogroup", id, "aria-labelledby": id + "-l" });
    const paint = () => {
      for (const [i, b] of [...group.children].entries()) {
        const on = options[i].value === get(path);
        b.setAttribute("aria-checked", String(on));
        b.tabIndex = on ? 0 : -1;
      }
    };
    options.forEach((o, i) => group.append(h("button", {
      type: "button", role: "radio",
      onclick: () => { set(path, o.value); paint(); if (onChange) onChange(o.value); },
      onkeydown: (e) => {
        const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
        if (!step) return;
        e.preventDefault();
        const j = (i + step + options.length) % options.length;
        group.children[j].click();
        group.children[j].focus();
      },
    }, o.label)));
    paint();
    return group;
  };
}

function textField(path, { placeholder = "", type = "text", grow = false, onChange } = {}) {
  return (id) => h("input", {
    id, type, placeholder, class: grow ? "grow" : null, value: get(path) ?? "", spellcheck: "false",
    oninput: (e) => { set(path, e.target.value); if (onChange) onChange(e.target.value); },
  });
}

function numberField(path, { min, max, step = 1 }) {
  return (id) => h("input", {
    id, type: "number", min, max, step, value: get(path),
    onchange: (e) => {
      let v = Number(e.target.value);
      if (!Number.isFinite(v)) v = get(path);
      v = Math.min(max, Math.max(min, v));
      e.target.value = v;
      set(path, v);
    },
  });
}

/* A list of words shown as removable chips, with an "add" field. */
function chipList(path, placeholder) {
  const wrap = h("div", { class: "chips" });
  const input = h("input", { type: "text", class: "grow", placeholder, spellcheck: "false", "aria-label": placeholder });
  const paint = () => {
    const words = get(path) || [];
    wrap.replaceChildren(...(words.length ? words.map((w, i) => h("span", { class: "chip" }, w,
      h("button", { type: "button", "aria-label": `Remove ${w}`, title: "Remove",
        onclick: () => { const next = [...get(path)]; next.splice(i, 1); set(path, next); paint(); } }, icon("x"))))
      : [h("span", { class: "empty" }, "Nothing added yet.")]));
  };
  const add = () => {
    const existing = get(path) || [];
    const fresh = input.value.split(",").map((s) => s.trim()).filter((s) => s && !existing.some((x) => x.toLowerCase() === s.toLowerCase()));
    if (fresh.length) { set(path, [...existing, ...fresh]); paint(); }
    input.value = "";
    input.focus();
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
  paint();
  return h("div", { class: "row stack" }, wrap,
    h("div", { class: "chip-add" }, input, h("button", { class: "btn", type: "button", onclick: add }, "Add")));
}

/* Editor for a {"from": "to"} object, e.g. replacements and snippets.
   Rows are kept in a local list so half-typed entries (empty "from") don't vanish. */
function pairList(path, leftLabel, rightLabel, { multiline = false, leftPlaceholder = "", rightPlaceholder = "" } = {}) {
  let items = Object.entries(get(path) || {}).map(([k, v]) => ({ k, v }));
  const list = h("div", { class: "pairs" });
  const commit = () => {
    const out = {};
    for (const { k, v } of items) if (k.trim()) out[k.trim()] = v;
    set(path, out);
  };
  const paint = () => {
    list.replaceChildren(
      items.length ? h("div", { class: "pair-head", "aria-hidden": "true" }, h("span", {}, leftLabel), h("span"), h("span", {}, rightLabel), h("span")) : h("span", { class: "empty" }, "Nothing added yet."),
      ...items.map((it, i) => {
        const right = multiline
          ? h("textarea", { rows: 1, placeholder: rightPlaceholder, "aria-label": rightLabel, oninput: (e) => { it.v = e.target.value; autosize(e.target); commit(); } }, it.v)
          : h("input", { type: "text", value: it.v, placeholder: rightPlaceholder, "aria-label": rightLabel, spellcheck: "false", oninput: (e) => { it.v = e.target.value; commit(); } });
        return h("div", { class: "pair" },
          h("input", { type: "text", value: it.k, placeholder: leftPlaceholder, "aria-label": leftLabel, spellcheck: "false", oninput: (e) => { it.k = e.target.value; commit(); } }),
          h("span", { class: "arrow", "aria-hidden": "true" }, "→"),
          right,
          h("button", { class: "icon-btn", type: "button", title: "Remove", "aria-label": "Remove", onclick: () => { items.splice(i, 1); commit(); paint(); } }, icon("trash")));
      }));
    list.querySelectorAll("textarea").forEach(autosize);
  };
  paint();
  return h("div", { class: "row stack" }, list,
    h("div", {}, h("button", { class: "btn", type: "button", onclick: () => {
      items.push({ k: "", v: "" });
      paint();
      list.lastChild.querySelector("input").focus();
    } }, "Add")));
}

function autosize(t) {
  requestAnimationFrame(() => { t.style.height = "auto"; t.style.height = Math.max(32, t.scrollHeight + 2) + "px"; });
}

/* Inline "are you sure" instead of a blocking dialog. */
function confirmButton(label, question, confirmLabel, action) {
  const box = h("div", { class: "confirm" });
  const idle = () => box.replaceChildren(h("button", { class: "btn danger", type: "button", onclick: ask }, label));
  const ask = () => {
    box.replaceChildren(h("span", {}, question),
      h("button", { class: "btn danger solid", type: "button", onclick: async () => { await action(); idle(); } }, confirmLabel),
      h("button", { class: "btn", type: "button", onclick: idle }, "Cancel"));
    box.querySelector(".solid").focus();
  };
  idle();
  return box;
}

/* ---- keys ---------------------------------------------------------------- */
// Browser key codes -> the key names the app's keyboard hook (pynput) uses.
const CODE_TO_KEY = {
  ControlLeft: "ctrl", ControlRight: "ctrl", MetaLeft: "cmd", MetaRight: "cmd", OSLeft: "cmd", OSRight: "cmd",
  AltLeft: "alt", AltRight: "alt", ShiftLeft: "shift", ShiftRight: "shift", Space: "space",
  CapsLock: "caps_lock", Insert: "insert", Pause: "pause", ScrollLock: "scroll_lock", ContextMenu: "menu",
  Home: "home", End: "end", PageUp: "page_up", PageDown: "page_down", Backquote: "`",
};
const MODIFIERS = ["ctrl", "cmd", "alt", "shift"];
function keyFromEvent(e) {
  if (CODE_TO_KEY[e.code]) return CODE_TO_KEY[e.code];
  let m;
  if ((m = /^F(\d{1,2})$/.exec(e.code))) return "f" + m[1];
  if ((m = /^Key([A-Z])$/.exec(e.code))) return m[1].toLowerCase();
  if ((m = /^Digit(\d)$/.exec(e.code))) return m[1];
  return null;
}
function keyLabel(k) {
  const labels = {
    ctrl: "Ctrl", ctrl_l: "Ctrl", ctrl_r: "Right Ctrl", cmd: "Win", cmd_l: "Win", cmd_r: "Right Win",
    alt: "Alt", alt_l: "Alt", alt_r: "Right Alt", alt_gr: "AltGr", shift: "Shift", shift_l: "Shift", shift_r: "Right Shift",
    space: "Space", caps_lock: "Caps Lock", scroll_lock: "Scroll Lock", page_up: "Page Up", page_down: "Page Down", menu: "Menu",
  };
  return labels[k] || (k.length <= 3 ? k.toUpperCase() : k[0].toUpperCase() + k.slice(1));
}
function sortKeys(keys) {
  const rank = (k) => { const i = MODIFIERS.indexOf(k.split("_")[0]); return i < 0 ? 9 : i; };
  return [...keys].sort((a, b) => rank(a) - rank(b));
}
function keysView(keys) {
  const out = h("span", { class: "keys", "aria-label": keys.map(keyLabel).join(" plus ") });
  keys.forEach((k, i) => { if (i) out.append("+"); out.append(h("kbd", {}, keyLabel(k))); });
  return out;
}

function shortcutRow() {
  const wrap = h("div", { class: "row stack" });
  let listening = false;
  const show = (error) => {
    listening = false;
    const keys = get("push_to_talk_keys") || [];
    const isDefault = JSON.stringify(keys) === JSON.stringify(S.defaults.push_to_talk_keys);
    wrap.replaceChildren(...[
      h("div", { class: "meter-row" },
        h("div", { class: "row-text" },
          h("div", { class: "row-label" }, "Push-to-talk shortcut"),
          h("div", { class: "row-desc" }, handsFreeText())),
        keysView(keys),
        h("button", { class: "btn", type: "button", onclick: record }, "Change")),
      error && h("div", { class: "hint-error", role: "alert" }, error),
      !isDefault && h("div", {}, h("button", { class: "link", type: "button", onclick: () => { set("push_to_talk_keys", [...S.defaults.push_to_talk_keys]); show(); } }, `Reset to ${S.defaults.push_to_talk_keys.map(keyLabel).join(" + ")}`)),
    ].filter(Boolean));
  };
  const record = () => {
    listening = true;
    const held = new Set();
    let best = [];
    const box = h("div", { class: "recorder", tabindex: "-1" },
      h("span", { class: "pulse", "aria-hidden": "true" }),
      h("span", { class: "rec-text" }, "Press and hold the keys you want, then let go. Esc cancels."));
    wrap.replaceChildren(h("div", { class: "row-label" }, "Push-to-talk shortcut"), box);
    box.focus();
    const done = (error) => {
      window.removeEventListener("keydown", down, true);
      window.removeEventListener("keyup", up, true);
      window.removeEventListener("blur", cancel);
      show(error);
    };
    const cancel = () => done();
    const down = (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "Escape") return done();
      const k = keyFromEvent(e);
      if (!k) return;
      held.add(k);
      if (held.size >= best.length) best = sortKeys(held);
      box.querySelector(".rec-text").replaceChildren(keysView(best));
    };
    const up = (e) => {
      e.preventDefault();
      e.stopPropagation();
      held.delete(keyFromEvent(e));
      if (held.size || !best.length) return;
      const hasModifierOrF = best.some((k) => MODIFIERS.includes(k) || /^f\d+$/.test(k) || ["caps_lock", "scroll_lock", "pause", "insert", "menu"].includes(k));
      if (!hasModifierOrF) return done("Pick a combination with Ctrl, Win, Alt or Shift (or an F-key), so normal typing doesn't start dictation.");
      if (best.length === 1 && MODIFIERS.includes(best[0])) return done(`${keyLabel(best[0])} on its own is used by too many other shortcuts. Add a second key.`);
      set("push_to_talk_keys", best);
      done();
    };
    window.addEventListener("keydown", down, true);
    window.addEventListener("keyup", up, true);
    window.addEventListener("blur", cancel);
  };
  show();
  return wrap;
}

function handsFreeText() {
  if (has("hands_free_extra_key")) {
    const k = get("hands_free_extra_key");
    return k ? `Hold to talk and let go to paste. For long dictation, also tap ${keyLabel(k)} while holding, then press the shortcut again to finish. Esc cancels.`
      : "Hold to talk and let go to paste. Esc cancels.";
  }
  return "Hold to talk and let go to paste. Tap it quickly instead to dictate hands-free, and tap again to finish. Esc cancels.";
}

/* ---- microphone ---------------------------------------------------------- */
let micTest = null;
function stopMicTest() {
  if (!micTest) return;
  micTest.stop();
  micTest = null;
}

function micRows() {
  const rows = [];
  const mics = S.mics && S.mics.microphones ? S.mics.microphones : [];
  const current = get("audio.input_device");
  const options = [{ value: "", label: "Windows default" }].concat(mics.map((m) => ({ value: m.id, label: m.name + (m.is_default ? " (default)" : "") })));
  if (current && !mics.some((m) => m.id === current)) options.push({ value: current, label: String(current).split(",")[0] + " (not connected)" });
  rows.push(row("Microphone", S.mics && !S.mics.ok ? "Couldn't list microphones: " + S.mics.error : "Which microphone to listen to.",
    select("audio.input_device", options, { toValue: (v) => v || null, onChange: () => { if (micTest) startMicTest(); } })));

  const hasGate = has("audio.silence_threshold");
  const fill = h("div", { class: "meter-fill quiet" });
  const mark = hasGate ? h("div", { class: "meter-mark", title: "Silence cutoff" }) : null;
  const meter = h("div", { class: "meter", role: "meter", "aria-label": "Microphone level", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": "0" }, fill, mark);
  const msg = h("div", { class: "meter-msg", "aria-live": "polite" }, "Press Test and say a few words.");
  const btn = h("button", { class: "btn", type: "button" }, "Test");

  // The bar uses decibels so quiet and loud voices both move it visibly: -60 dB (left) to 0 dB (right).
  const toPct = (rms) => Math.max(0, Math.min(100, ((20 * Math.log10(Math.max(rms, 1e-6)) + 60) / 60) * 100));
  const fromPct = (p) => Math.pow(10, ((p / 100) * 60 - 60) / 20);
  const placeMark = () => { if (mark) mark.style.left = toPct(get("audio.silence_threshold")) + "%"; };
  placeMark();

  async function startMicTest() {
    stopMicTest();
    const res = await api().start_mic_test(get("audio.input_device"));
    if (!res.ok) { msg.textContent = "Couldn't open the microphone: " + res.error; return; }
    btn.textContent = "Stop";
    let running = true, peak = 0, last = 0;
    const started = performance.now();
    const tick = async (now) => {
      if (!running) return;
      if (now - last > 45) {
        last = now;
        const level = await api().mic_level();
        if (!running) return;
        peak = Math.max(peak, level);
        const pct = toPct(level);
        fill.style.width = pct + "%";
        meter.setAttribute("aria-valuenow", String(Math.round(pct)));
        const gate = hasGate ? get("audio.silence_threshold") : 0.003;
        fill.classList.toggle("quiet", level < gate);
        msg.textContent = peak < gate ? "Listening… say something." : "Hyper-Wispr can hear you.";
      }
      if (now - started > 20000) { stopMicTest(); return; } // don't hold the mic forever
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    micTest = {
      stop: () => {
        running = false;
        api().stop_mic_test();
        btn.textContent = "Test";
        fill.style.width = "0";
        if (peak === 0) msg.textContent = "Press Test and say a few words.";
        else if (peak < (hasGate ? get("audio.silence_threshold") : 0.003)) msg.textContent = "That was very quiet. Check the microphone is unmuted, or pick another one above.";
      },
    };
  }
  btn.addEventListener("click", () => (micTest ? stopMicTest() : startMicTest()));
  rows.push(h("div", { class: "row stack" },
    h("div", { class: "meter-row" }, h("div", { class: "row-label" }, "Level"), meter, btn), msg));

  if (hasGate) {
    const slider = h("input", {
      type: "range", min: "5", max: "60", step: "1", "aria-label": "Silence cutoff",
      value: String(toPct(get("audio.silence_threshold"))),
      oninput: (e) => { set("audio.silence_threshold", Number(fromPct(Number(e.target.value)).toPrecision(2))); placeMark(); },
    });
    rows.push(h("div", { class: "row stack" },
      h("div", { class: "row-text" }, h("div", { class: "row-label" }, "Silence cutoff"),
        h("div", { class: "row-desc" }, "Recordings that never get louder than the line on the meter are ignored, so background noise isn't typed. Move it left if a quiet voice gets missed.")),
      h("div", { class: "meter-row" }, h("span", { class: "row-desc" }, "More sensitive"), slider, h("span", { class: "row-desc" }, "Less sensitive"))));
  }
  return rows;
}

/* ---- pages --------------------------------------------------------------- */
const PAGES = [
  { id: "general", label: "General", icon: "general" },
  { id: "speech", label: "Speech", icon: "speech" },
  { id: "ai", label: "AI cleanup", icon: "ai" },
  { id: "vocab", label: "Vocabulary", icon: "vocab" },
  { id: "history", label: "History", icon: "history" },
  { id: "about", label: "About", icon: "about" },
];

function pageHeader(title, lede) {
  return [h("h1", {}, title), lede && h("p", { class: "lede" }, lede)];
}

function generalPage() {
  const startRow = S.info.can_start_at_login ? row("Open at login", "Start Hyper-Wispr in the tray when you sign in to Windows.", (id) => {
    const btn = h("button", { class: "switch", role: "switch", id, type: "button", "aria-checked": "false", "aria-describedby": id + "-d" });
    api().get_start_at_login().then((on) => btn.setAttribute("aria-checked", String(!!on)));
    btn.addEventListener("click", async () => {
      const want = btn.getAttribute("aria-checked") !== "true";
      btn.setAttribute("aria-checked", String(want));
      const ok = await api().set_start_at_login(want);
      if (!ok) btn.setAttribute("aria-checked", String(!want));
    });
    return btn;
  }) : null;

  return [
    ...pageHeader("General", "How you start dictating, which microphone is used, and how text is put into your apps."),
    S.info.load_error && h("div", { class: "banner", role: "alert" }, S.info.load_error),
    section("Shortcut", [shortcutRow()]),
    section("Microphone", micRows()),
    section("Typing", [
      row("Sounds", "A short tone when recording starts and stops.", toggle("audio.sounds")),
      row("Insert text by", "Pasting is instant and works almost everywhere. Typing is slower but works in apps that block pasting.",
        segmented("paste.method", [{ value: "clipboard", label: "Pasting" }, { value: "type", label: "Typing" }], () => render())),
      get("paste.method") === "clipboard" && row("Keep my clipboard", "Put back whatever you had copied after pasting the dictation.", toggle("paste.restore_clipboard")),
      row("Add a space after", "So the next sentence doesn't run into the last one.", toggle("paste.add_trailing_space")),
    ]),
    startRow && section("System", [startRow]),
  ];
}

const MODELS = [
  { value: "auto", label: "Automatic (recommended)" },
  { value: "large-v3-turbo", label: "Large v3 Turbo: best balance (1.6 GB)" },
  { value: "large-v3", label: "Large v3: most accurate, slowest (3 GB)" },
  { value: "distil-large-v3", label: "Distil Large v3: fast, English only (1.5 GB)" },
  { value: "medium", label: "Medium (1.5 GB)" },
  { value: "small", label: "Small: good on CPU (0.5 GB)" },
  { value: "base", label: "Base: fast, less accurate (150 MB)" },
  { value: "tiny", label: "Tiny: fastest, least accurate (75 MB)" },
  { value: "small.en", label: "Small, English only (0.5 GB)" },
  { value: "medium.en", label: "Medium, English only (1.5 GB)" },
];
const LANGUAGES = [
  ["en", "English"], ["ar", "Arabic"], ["fr", "French"], ["es", "Spanish"], ["de", "German"], ["it", "Italian"],
  ["pt", "Portuguese"], ["nl", "Dutch"], ["tr", "Turkish"], ["ru", "Russian"], ["uk", "Ukrainian"], ["pl", "Polish"],
  ["fa", "Persian"], ["ur", "Urdu"], ["hi", "Hindi"], ["bn", "Bengali"], ["he", "Hebrew"], ["el", "Greek"],
  ["zh", "Chinese"], ["ja", "Japanese"], ["ko", "Korean"], ["id", "Indonesian"], ["ms", "Malay"], ["sv", "Swedish"],
];

function languageToggle(code, name) {
  // A switch that adds or removes one language from the auto-detect list.
  return (id) => {
    const btn = h("button", {
      class: "switch", role: "switch", id, type: "button", "aria-describedby": id + "-d",
      "aria-checked": String((get("speech.languages") || []).includes(code)),
      onclick: () => {
        const cur = get("speech.languages") || [];
        const next = cur.includes(code) ? cur.filter((c) => c !== code) : [...cur, code];
        set("speech.languages", next);
        render();
      },
    });
    return btn;
  };
}

function languagesPicker() {
  // Auto-detect only chooses between these languages, which stops Whisper guessing an unrelated one.
  const wrap = h("div", { class: "chips", role: "group", "aria-label": "Languages you speak" });
  const paint = () => {
    const chosen = get("speech.languages") || [];
    wrap.replaceChildren(...LANGUAGES.map(([code, name]) => {
      const on = chosen.includes(code);
      return h("button", {
        type: "button", class: "chip toggle" + (on ? " on" : ""), "aria-pressed": String(on),
        onclick: () => {
          const cur = get("speech.languages") || [];
          set("speech.languages", cur.includes(code) ? cur.filter((c) => c !== code) : [...cur, code]);
          render(); // keeps the English/Arabic switches above in step
        },
      }, name);
    }));
  };
  paint();
  return h("div", { class: "row stack" },
    h("div", { class: "row-text" }, h("div", { class: "row-label" }, "Languages you speak"),
      h("div", { class: "row-desc" }, "Each dictation is recognised as one of these. With none selected, any language can be detected, which is less reliable.")),
    wrap);
}

function languageSummary() {
  const chosen = get("speech.languages") || [];
  if (!chosen.length) return "Any language is detected automatically.";
  const names = chosen.map((c) => (LANGUAGES.find(([code]) => code === c) || [c, c])[1]);
  return names.length === 1 ? `Always ${names[0]}.` : `Detected each time from: ${names.join(", ")}.`;
}

function speechPage() {
  const auto = S.gpu === null ? "Checking your graphics card…"
    : S.gpu ? "Your PC has an NVIDIA graphics card, so Automatic uses Large v3 Turbo on it."
      : "No NVIDIA graphics card found, so Automatic uses Small on the processor.";
  return [
    ...pageHeader("Speech", "The speech-recognition model runs entirely on this computer. Nothing you say is sent anywhere."),
    section("Recognition", [
      row("Model", auto, select("speech.model", MODELS)),
      ...languageRows(),
      row("Run on", "Automatic uses an NVIDIA graphics card when there is one.",
        segmented("speech.device", [{ value: "auto", label: "Automatic" }, { value: "cuda", label: "NVIDIA GPU" }, { value: "cpu", label: "Processor" }])),
      row("Skip silence", "Ignores pauses so the model doesn't invent words while you think.", toggle("speech.vad_filter")),
    ], "Changing the model downloads it the first time (it can take a few minutes). Dictation keeps working with the old model until the new one is ready."),
    advanced([
      row("Accuracy search (beam size)", "Higher can be slightly more accurate but slower. 5 is a good default.", numberField("speech.beam_size", { min: 1, max: 10 })),
      row("Number precision", "Leave on Automatic unless the model fails to load on your graphics card.",
        select("speech.compute_type", [{ value: "auto", label: "Automatic" }, { value: "float16", label: "float16 (GPU)" }, { value: "int8_float16", label: "int8_float16 (GPU, less memory)" }, { value: "int8", label: "int8 (CPU)" }])),
    ]),
  ];
}

function languageRows() {
  if (!has("speech.languages")) {
    return [row("Language", "Pick one if you always dictate in the same language; it's faster and more accurate.",
      select("speech.language", [{ value: "", label: "Detect automatically" }, ...LANGUAGES.map(([v, l]) => ({ value: v, label: l }))]))];
  }
  if (get("speech.language")) {
    // A single forced language was set (e.g. by hand in the file): offer the way back.
    return [row("Language", "Only this language is recognised.",
      select("speech.language", LANGUAGES.map(([v, l]) => ({ value: v, label: l })).concat([{ value: "", label: "Use my languages list instead" }]), { onChange: () => render() }))];
  }
  const english = (get("speech.languages") || []).includes("en");
  return [
    row("English", english ? languageSummary() : "Turn on if you dictate in English.", languageToggle("en", "English")),
    row("Also Arabic", "Turn on if you sometimes dictate in Arabic. Each dictation is then recognised as English or Arabic.", languageToggle("ar", "Arabic")),
    h("details", { class: "more", open: S.moreLanguages || (get("speech.languages") || []).some((c) => c !== "en" && c !== "ar") ? true : null,
      ontoggle: (e) => { S.moreLanguages = e.target.open; } },
      h("summary", {}, "Other languages"), languagesPicker()),
  ];
}

function advanced(rows) {
  return h("section", { class: "section" }, h("div", { class: "card" },
    h("details", { class: "more" }, h("summary", {}, "Advanced"), ...rows)));
}

function aiPage() {
  const enabled = !!get("ai_cleanup.enabled");
  const st = S.ollama;
  const pill = st === null ? h("span", { class: "pill" }, h("span", { class: "dot" }), "Checking Ollama…")
    : st.running ? h("span", { class: "pill ok" }, h("span", { class: "dot" }), `Ollama is running · ${st.models.length} model${st.models.length === 1 ? "" : "s"}`)
      : h("span", { class: "pill bad" }, h("span", { class: "dot" }), "Ollama isn't running");
  const current = get("ai_cleanup.model");
  const models = st && st.running ? st.models : [];
  const modelOptions = models.map((m) => ({ value: m, label: m }));
  if (current && !models.includes(current) && !models.includes(current + ":latest")) {
    modelOptions.unshift({ value: current, label: `${current}${st && st.running ? " (downloads on next start)" : ""}` });
  }
  const off = !enabled;
  return [
    ...pageHeader("AI cleanup", "After transcription, a small AI model on your computer fixes punctuation, removes \"um\"s, handles \"no wait, I mean…\" corrections and formats lists."),
    section(null, [
      h("div", { class: "row" },
        h("div", { class: "row-text" }, h("div", { class: "row-label" }, "Status"),
          h("div", { class: "row-desc" }, st && !st.running
            ? ["AI cleanup needs the free Ollama app. Without it, you still get the basic cleanup. ", h("button", { class: "link", type: "button", onclick: () => api().open_url("https://ollama.com/download") }, "Get Ollama")]
            : "Uses Ollama, which runs AI models locally.")),
        h("div", { class: "row-control" }, pill, h("button", { class: "btn small", type: "button", onclick: () => { S.ollama = null; render(); refreshOllama(); } }, "Check again"))),
      row("Use AI cleanup", null, toggle("ai_cleanup.enabled", () => render())),
    ]),
    section("Writing", [
      row("Medical mode", "Keeps drug names, doses, units and clinical abbreviations exact (\"metoprolol 25 mg PO BID\").", toggle("ai_cleanup.medical_mode"), { disabled: off }),
      row("Model", "Larger models write better but take longer. qwen2.5:3b is a good start.",
        modelOptions.length ? select("ai_cleanup.model", modelOptions) : textField("ai_cleanup.model", { placeholder: "qwen2.5:3b" }), { disabled: off }),
      h("div", { class: "row stack" + (off ? " disabled" : "") },
        h("div", { class: "row-text" }, h("label", { class: "row-label", for: "extra" }, "Extra instructions"),
          h("div", { class: "row-desc" }, "Anything else the AI should always do, in plain English. For example: \"Use British spelling.\"")),
        h("textarea", { id: "extra", rows: 3, oninput: (e) => set("ai_cleanup.extra_instructions", e.target.value) }, get("ai_cleanup.extra_instructions") || "")),
    ]),
    advanced([
      row("Skip AI for short phrases", "Dictations with fewer words than this are pasted without the AI step, so they appear instantly.", numberField("ai_cleanup.min_words", { min: 0, max: 50 })),
      row("Time limit (seconds)", "If the AI takes longer, the basic cleanup is pasted instead.", numberField("ai_cleanup.timeout_seconds", { min: 1, max: 60 })),
      row("Ollama address", null, textField("ai_cleanup.ollama_url", { placeholder: "http://127.0.0.1:11434" })),
    ]),
  ];
}

function vocabPage() {
  return [
    ...pageHeader("Vocabulary", "Teach Hyper-Wispr the words you use and the shortcuts you want."),
    section("Dictionary", [chipList("dictionary", "Add a word, e.g. a colleague's name or a drug")],
      "Spelled exactly like this. Separate several with commas."),
    section("Replacements", [pairList("replacements", "When you say", "Write", { leftPlaceholder: "hyper wisper", rightPlaceholder: "Hyper-Wispr" })],
      "Always swaps the left side for the right side, ignoring capitals."),
    section("Snippets", [pairList("snippets", "Say this on its own", "To insert", { multiline: true, leftPlaceholder: "my signature", rightPlaceholder: "Dr. …\nClinic …" })],
      "Saying the phrase by itself pastes the full text instead."),
    section("Basic cleanup", [
      row("Remove filler words", "Removes the words below when they're said on their own.", toggle("cleanup.remove_fillers", () => render())),
      get("cleanup.remove_fillers") && chipList("cleanup.filler_words", "Add a filler word"),
      row("Spoken punctuation", "Say \"new line\", \"new paragraph\", \"period\" or \"comma\" to insert them.", toggle("cleanup.spoken_punctuation")),
    ]),
    section("Undo phrases", [chipList("cleanup.backtrack_phrases", "Add a phrase")],
      "Saying one of these deletes the sentence you just said."),
  ];
}

function historyPage() {
  const list = h("div", { class: "card" }, h("div", { class: "row" }, h("span", { class: "empty" }, "Loading…")));
  const load = async () => {
    const items = await api().get_history(200);
    if (!items.length) {
      list.replaceChildren(h("div", { class: "row" }, h("span", { class: "empty" }, "No dictations yet. Hold your shortcut and speak to make one.")));
      return;
    }
    list.replaceChildren(...items.map((it) => {
      const when = new Date(it.created * 1000);
      const copyBtn = h("button", { class: "icon-btn", type: "button", title: "Copy", "aria-label": "Copy text" }, icon("copy"));
      copyBtn.addEventListener("click", async () => {
        if (await api().copy_text(it.text)) {
          copyBtn.replaceChildren(icon("check"));
          copyBtn.classList.add("copied");
          setTimeout(() => { copyBtn.replaceChildren(icon("copy")); copyBtn.classList.remove("copied"); }, 1400);
        }
      });
      return h("div", { class: "history-item" },
        h("div", { class: "row-text" },
          h("div", { class: "history-meta" }, [when.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }), it.app && it.app !== "unknown" ? it.app.replace(/\.exe$/i, "") : null, it.seconds ? `${it.seconds.toFixed(1)} s` : null].filter(Boolean).join(" · ")),
          h("div", { class: "history-text" }, it.text || h("span", { class: "empty" }, "(nothing was pasted)"))),
        h("div", { class: "history-actions" }, copyBtn,
          h("button", { class: "icon-btn", type: "button", title: "Delete", "aria-label": "Delete this dictation", onclick: async () => { await api().delete_history(it.id); load(); } }, icon("trash"))));
    }));
  };
  load();
  return [
    ...pageHeader("History", "Your recent dictations, stored only on this computer."),
    section(null, [
      row("Keep history", "Turn off to stop saving new dictations.", toggle("history.enabled")),
      h("div", { class: "row" }, h("div", { class: "row-text" }, h("div", { class: "row-label" }, "Delete all history")),
        h("div", { class: "row-control" }, confirmButton("Delete all…", "Delete every saved dictation?", "Delete all", async () => { await api().delete_history(null); load(); }))),
    ]),
    h("section", { class: "section" }, h("h2", { class: "section-title" }, "Recent"), list),
  ];
}

function aboutPage() {
  return [
    h("div", { class: "card section" }, h("div", { class: "about-hero" },
      h("div", { class: "brand-mark", "aria-hidden": "true", html: '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><rect x="9" y="3" width="6" height="11" rx="3" fill="currentColor"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/></svg>' }),
      h("div", {}, h("h1", {}, "Hyper-Wispr"), h("div", { class: "row-desc" }, `Version ${S.info.version} · Private, offline dictation. Speech recognition and AI cleanup both run on this computer.`)))),
    section("Files", [
      h("div", { class: "row" }, h("div", { class: "row-text" }, h("div", { class: "row-label" }, "Data folder"),
        h("div", { class: "mono" }, S.info.data_dir), h("div", { class: "row-desc" }, "Settings, history and the log file (useful when reporting a problem).")),
        h("div", { class: "row-control" }, h("button", { class: "btn", type: "button", onclick: () => api().open_data_folder() }, "Open folder"))),
      h("div", { class: "row" }, h("div", { class: "row-text" }, h("div", { class: "row-label" }, "Settings file"),
        h("div", { class: "row-desc" }, "For advanced users. Changes made there are picked up automatically.")),
        h("div", { class: "row-control" }, h("button", { class: "btn", type: "button", onclick: () => api().open_settings_file() }, "Open file"))),
    ]),
    section("Reset", [
      h("div", { class: "row" }, h("div", { class: "row-text" }, h("div", { class: "row-label" }, "Reset all settings"),
        h("div", { class: "row-desc" }, "Puts every setting back to how it was after installing. Your history is kept.")),
        h("div", { class: "row-control" }, confirmButton("Reset…", "Reset everything?", "Reset", async () => {
          clearTimeout(saveTimer);
          savePending = false;
          const st = await api().reset_settings();
          S.settings = st.settings;
          setStatus("saved");
        }))),
    ]),
    h("p", { class: "section-note" }, h("button", { class: "link", type: "button", onclick: () => api().open_url("https://github.com/omar-184/Hyper-Wispr") }, "Hyper-Wispr on GitHub")),
  ];
}

const RENDER = { general: generalPage, speech: speechPage, ai: aiPage, vocab: vocabPage, history: historyPage, about: aboutPage };

/* ---- navigation ---------------------------------------------------------- */
function renderNav() {
  const nav = document.getElementById("nav");
  nav.replaceChildren(...PAGES.map((p) => h("button", {
    type: "button", "aria-current": p.id === S.page ? "page" : null, title: p.label,
    onclick: () => go(p.id),
  }, icon(p.icon), h("span", {}, p.label))));
}

function go(page) {
  if (page === S.page) return;
  stopMicTest();
  S.page = page;
  try { localStorage.setItem("hw-page", page); } catch (e) { /* storage may be unavailable */ }
  renderNav();
  render(true);
  document.getElementById("main").scrollTop = 0;
}

/* Re-render the current page. Keeps scroll position and focus when it's the same page. */
function render(animate = false) {
  const main = document.getElementById("main");
  const scroll = main.scrollTop;
  const focusedId = document.activeElement && document.activeElement.id;
  stopMicTest();
  const page = h("div", { class: "page" + (animate ? " entering" : "") }, RENDER[S.page]());
  main.replaceChildren(page);
  if (!animate) {
    main.scrollTop = scroll;
    if (focusedId && document.getElementById(focusedId)) document.getElementById(focusedId).focus();
  }
}

async function refreshOllama() {
  S.ollama = await api().ollama_status(get("ai_cleanup.ollama_url") || "http://127.0.0.1:11434");
  if (S.page === "ai") render();
}

async function init() {
  const st = await api().get_state();
  S.settings = st.settings;
  S.defaults = st.defaults;
  S.info = st;
  try { const p = localStorage.getItem("hw-page"); if (RENDER[p]) S.page = p; } catch (e) { /* ignore */ }
  setStatus("saved");
  renderNav();
  render(true);
  window.__hwReady = true;
  api().window_ready();

  // Slower lookups fill in when ready.
  api().list_microphones().then((m) => { S.mics = m; if (S.page === "general") render(); });
  api().gpu_available().then((g) => { S.gpu = !!g; if (S.page === "speech") render(); }).catch(() => { S.gpu = false; });
  refreshOllama();
}

document.addEventListener("keydown", (e) => {
  // Ctrl+1..6 jumps between sections.
  if (e.ctrlKey && !e.altKey && /^[1-6]$/.test(e.key)) { e.preventDefault(); go(PAGES[Number(e.key) - 1].id); }
});

if (window.pywebview && window.pywebview.api && window.pywebview.api.get_state) init();
else window.addEventListener("pywebviewready", init, { once: true });
