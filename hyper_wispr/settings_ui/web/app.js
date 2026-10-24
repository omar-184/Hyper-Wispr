"use strict";

/* The Hyper-Wispr window: dashboard, history and settings. Talks to Python through
   window.pywebview.api (see settings_ui/__init__.py). Every change is saved right
   away; the running app notices settings.json changed and applies it. */

const S = {
  settings: null,
  // settings.json as this window last loaded or saved it. Saves send only what changed since,
  // so a change made meanwhile from the tray menu (AI cleanup on/off) isn't undone.
  saved: null,
  defaults: null,
  info: null,
  page: "home",
  status: null, // what the tray app is doing (status.json)
  stats: null,
  update: null, // result of the last update check
  updateInfo: null,
  download: null, // installer download progress
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
  home: '<rect x="4" y="4" width="7" height="8" rx="1.5"/><rect x="13" y="4" width="7" height="5" rx="1.5"/><rect x="13" y="11" width="7" height="9" rx="1.5"/><rect x="4" y="14" width="7" height="6" rx="1.5"/>',
  arrow: '<path d="M9 6l6 6-6 6"/>',
  download: '<path d="M12 4v11M7.5 10.5L12 15l4.5-4.5M5 19.5h14"/>',
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
  notes: '<path d="M6.5 3.5h8l4 4v12a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1z"/><path d="M14 3.5V8h4.5M8.5 12.5h7M8.5 16h5"/>',
  redo: '<path d="M19 8v4.5h-4.5"/><path d="M18.6 12.4A7 7 0 1 1 17 7.4"/>',
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
  const sent = structuredClone(S.settings);
  const res = await api().save_settings(sent, S.saved);
  if (res && res.ok) S.saved = sent;
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
    // While recording, the tray app ignores the current shortcut instead of starting to dictate.
    api().begin_shortcut_capture();
    const done = (error) => {
      window.removeEventListener("keydown", down, true);
      window.removeEventListener("keyup", up, true);
      window.removeEventListener("blur", cancel);
      api().end_shortcut_capture();
      show(error);
    };
    const cancel = () => done();
    const down = (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "Escape") return done();
      const k = keyFromEvent(e);
      if (!k) return;
      // Letting go of Win (or Alt) on its own would open the Start menu and take the focus away.
      if ((k === "cmd" || k === "alt") && !held.has(k)) api().mask_menu_key();
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
  { id: "home", label: "Dashboard", icon: "home" },
  { id: "history", label: "History", icon: "history" },
  { id: "notes", label: "Notes", icon: "notes" },
  { id: "general", label: "General", icon: "general", group: "Settings" },
  { id: "speech", label: "Speech", icon: "speech" },
  { id: "ai", label: "AI cleanup", icon: "ai" },
  { id: "vocab", label: "Vocabulary", icon: "vocab" },
  { id: "about", label: "About & updates", icon: "about" },
];

function pageHeader(title, lede) {
  return [h("h1", {}, title), lede && h("p", { class: "lede" }, lede)];
}

/* Two columns side by side when the window is wide enough, one under the other when it isn't. */
function cols(left, right) {
  return h("div", { class: "cols" }, h("div", { class: "col" }, left), h("div", { class: "col" }, right));
}

/* ---- dashboard ----------------------------------------------------------- */
function greeting() {
  const hr = new Date().getHours();
  return hr < 5 ? "Good evening" : hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening";
}

function statusPill() {
  const st = S.status;
  if (!st) return h("span", { class: "pill" }, h("span", { class: "dot" }), "Checking…");
  const kind = { idle: "ok", recording: "live", working: "busy", loading: "busy", error: "bad", stopped: "bad" }[st.status] || "";
  return h("span", { class: "pill " + kind, title: st.model ? "Speech model: " + st.model : null },
    h("span", { class: "dot" }), st.status === "idle" ? "Ready to dictate" : st.label);
}

function formatNumber(n) {
  return Number(n || 0).toLocaleString();
}
function formatMinutes(m) {
  if (m < 60) return { value: String(m), unit: m === 1 ? "minute" : "minutes" };
  const hrs = m / 60;
  return { value: hrs < 10 ? hrs.toFixed(1) : String(Math.round(hrs)), unit: "hours" };
}

function statTile(label, value, unit, hint) {
  return h("div", { class: "tile" },
    h("div", { class: "tile-label" }, label),
    h("div", { class: "tile-value" }, value, unit && h("span", { class: "tile-unit" }, unit)),
    hint && h("div", { class: "tile-hint" }, hint));
}

function activityChart(daily) {
  const max = Math.max(1, ...daily.map((d) => d.words));
  const total = daily.reduce((a, d) => a + d.words, 0);
  const bars = h("div", { class: "bars", role: "img", "aria-label": `Words dictated per day over the last ${daily.length} days, ${formatNumber(total)} in total.` });
  const today = daily.length - 1;
  daily.forEach((d, i) => {
    const date = new Date(d.day + "T12:00:00");
    const label = i === today ? "Today" : date.toLocaleDateString(undefined, { weekday: "short" });
    const full = date.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
    bars.append(h("div", { class: "bar-col" + (i === today ? " today" : ""), title: `${full}: ${formatNumber(d.words)} words` },
      h("div", { class: "bar-track" }, h("div", { class: "bar" + (d.words ? "" : " zero"), style: `height:${d.words ? Math.max(4, (d.words / max) * 100) : 0}%` })),
      h("div", { class: "bar-label" }, i % 2 === today % 2 ? label : "")));
  });
  return h("div", { class: "chart" },
    h("div", { class: "chart-scale" }, h("span", {}, formatNumber(max)), h("span", {}, "0")),
    bars);
}

function dashCard(title, action, ...body) {
  return h("section", { class: "card dash-card" },
    h("div", { class: "dash-card-head" }, h("h2", {}, title), action),
    ...body);
}

function homePage() {
  const st = S.stats;
  const keys = get("push_to_talk_keys") || [];
  const saved = formatMinutes(st ? st.minutes_saved : 0);

  const recent = h("div", { class: "dash-list" });
  const paintRecent = (items) => {
    recent.replaceChildren(...(items.length ? items.map((it) => historyItem(it, () => { loadRecent(); refreshStats(); }))
      : [h("div", { class: "dash-empty" },
        h("div", { class: "row-label" }, "No dictations yet"),
        h("div", { class: "row-desc" }, ["Click into any text box, hold ", keysView(keys), " and speak. Let go and the text appears."]))]));
  };
  // Painted from the last load straight away, so a re-render doesn't flash "Loading…".
  const loadRecent = async () => { S.recent = await api().get_history(6); paintRecent(S.recent); };
  if (S.recent) paintRecent(S.recent);
  else recent.append(h("div", { class: "row" }, h("span", { class: "empty" }, "Loading…")));
  loadRecent();

  const stopped = S.status && !S.status.running;
  const dl = S.status && S.status.status === "loading" && S.status.download;
  const firstRun = dl ? h("div", { class: "banner info first-run" },
    h("div", { class: "row-text" },
      h("div", { class: "row-label" }, "Getting Hyper-Wispr ready"),
      h("div", { class: "row-desc" }, dl.what === "gpu"
        ? "Your NVIDIA graphics card can run speech recognition much faster, so its speed-up files are downloading. This happens once."
        : "The speech model is downloading. This happens once; after that everything works offline."),
      h("div", { class: "meter progress", role: "progressbar", "aria-label": "Download progress", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(dl.total ? Math.round((dl.done / dl.total) * 100) : 0) },
        h("div", { class: "meter-fill", style: `width:${dl.total ? Math.min(100, (dl.done / dl.total) * 100) : 4}%` })),
      h("div", { class: "row-desc" }, dl.total ? `${formatSize(dl.done)} of ${formatSize(dl.total)}` : `${formatSize(dl.done)} so far`))) : null;
  const statusLine = S.status && S.status.status === "error" ? S.status.error
    : S.status && S.status.status === "loading" && S.status.download ? ""
    : S.status && S.status.status === "loading" ? "The speech model is loading. The first start downloads it, which can take a few minutes."
      : S.status && S.status.model ? `Speech recognition: ${S.status.model}` : "";

  const upd = S.update && S.update.available ? h("div", { class: "banner info" },
    h("div", { class: "row-text" }, h("div", { class: "row-label" }, `Hyper-Wispr ${S.update.latest} is available`),
      h("div", { class: "row-desc" }, `You have ${S.info.version}. Updating keeps your settings and history.`)),
    h("button", { class: "btn primary", type: "button", onclick: () => go("about") }, "See update")) : null;

  return [
    h("header", { class: "dash-head" },
      h("div", {}, h("h1", {}, greeting()), h("p", { class: "lede" }, ["Hold ", keysView(keys), " anywhere and speak. Tap it once for hands-free."])),
      h("div", { class: "dash-status" }, statusPill(), statusLine && h("div", { class: "row-desc" }, statusLine))),
    stopped && h("div", { class: "banner", role: "alert" },
      h("div", { class: "row-text" }, h("div", { class: "row-label" }, "Hyper-Wispr isn't running"),
        h("div", { class: "row-desc" }, "Dictation is off until it's started again.")),
      h("button", { class: "btn primary", type: "button", onclick: async () => { await api().start_app(); S.status = null; render(); setTimeout(refreshStatus, 1500); } }, "Start Hyper-Wispr")),
    firstRun,
    upd,
    h("div", { class: "tiles" },
      statTile("Today", formatNumber(st && st.today_words), "words"),
      statTile("This week", formatNumber(st && st.week_words), "words"),
      statTile("Time saved", saved.value, saved.unit, `vs. typing ${formatNumber(st && st.words)} words`),
      statTile("Speaking speed", st && st.wpm ? formatNumber(st.wpm) : "–", "wpm", "Typing is about 40"),
      statTile("Streak", formatNumber(st && st.streak), st && st.streak === 1 ? "day" : "days", `${formatNumber(st && st.dictations)} dictations in all`)),
    h("div", { class: "dash-grid" },
      h("div", { class: "dash-main" },
        dashCard("Last 14 days", null, st ? activityChart(st.daily) : h("div", { class: "chart" })),
        dashCard("Recent dictations", h("button", { class: "link with-icon", type: "button", onclick: () => go("history") }, "See all", icon("arrow")), recent)),
      h("div", { class: "dash-side" },
        dashCard("Quick settings", null, h("div", { class: "dash-rows" },
          row("AI cleanup", "Punctuation, \"um\"s and corrections fixed by a local AI.", toggle("ai_cleanup.enabled")),
          row("Medical mode", "Drug names, doses and abbreviations kept exact.", toggle("ai_cleanup.medical_mode")),
          row("Sounds", "A soft tone when listening starts and stops.", toggle("audio.sounds")),
          row("On-screen indicator", "The \"Listening\" bubble while you dictate.", toggle("overlay.enabled")))),
        dashCard("Your setup", h("button", { class: "link with-icon", type: "button", onclick: () => go("general") }, "Settings", icon("arrow")), h("div", { class: "dash-rows" },
          setupRow("Microphone", micName(), "general"),
          setupRow("Languages", languageSummary().replace(/\.$/, ""), "speech"),
          setupRow("AI model", get("ai_cleanup.enabled") ? get("ai_cleanup.model") : "Off", "ai"),
          setupRow("Words in your dictionary", formatNumber((get("dictionary") || []).length), "vocab"))))),
  ];
}

function setupRow(label, value, page) {
  return h("button", { class: "setup-row", type: "button", onclick: () => go(page) },
    h("span", { class: "row-desc" }, label), h("span", { class: "setup-value" }, value), icon("arrow"));
}

function micName() {
  const id = get("audio.input_device");
  if (!id) return "Windows default";
  return String(id).split(",")[0];
}

async function refreshStats() {
  S.stats = await api().get_stats(14);
  if (S.page === "home") render();
}
async function refreshStatus() {
  const before = S.status;
  S.status = await api().get_status();
  if (JSON.stringify(S.status) === JSON.stringify(before)) return;
  // A dictation just finished: the numbers and the recent list have changed.
  if (before && before.status !== "idle" && S.status.status === "idle") return refreshStats();
  if (S.page === "home") render();
}

function commandRows() {
  const keys = get("push_to_talk_keys") || [];
  const on = get("command_mode.enabled") !== false;
  return [
    row("Command mode", ["Select some text, then hold ", keysView([...keys, get("command_mode.key") || "alt_l"]), " and say what to do, like \"make this shorter\" or \"turn this into a referral letter\". Needs AI cleanup."],
      toggle("command_mode.enabled", () => render())),
    on && row("Extra key for commands", null, segmented("command_mode.key", [{ value: "alt_l", label: "Alt" }, { value: "shift", label: "Shift" }], () => render())),
    row("Paste last dictation", ["Press ", keysView(get("paste_last_keys") || []), " to paste your last dictation again."], null),
  ];
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
    cols([
      section("Shortcut", [shortcutRow(), ...commandRows()]),
      section("Microphone", [
        ...micRows(),
        row("Whisper mode", "For speaking quietly, e.g. next to a patient. Picks up much softer speech (and a little more background noise). Also in the tray menu.", toggle("audio.whisper_mode")),
        row("Keep the microphone ready", "The first word is never cut off. Windows then shows the microphone as in use whenever Hyper-Wispr runs.", toggle("audio.keep_mic_open")),
        row("Mute music while dictating", "Mutes music and videos while you speak, then brings the sound back. Calls (Teams, Zoom…) are never muted.", toggle("audio.mute_media")),
      ]),
    ], [
      section("Typing", [
        row("Sounds", "A soft tone when recording starts and stops.", toggle("audio.sounds")),
        row("Insert text by", "Pasting is instant and works almost everywhere. Typing is slower but works in apps that block pasting.",
          segmented("paste.method", [{ value: "clipboard", label: "Pasting" }, { value: "type", label: "Typing" }], () => render())),
        get("paste.method") === "clipboard" && row("Keep my clipboard", "Put back whatever you had copied after pasting the dictation (except in Remote Desktop or Citrix, where that could paste the old contents).", toggle("paste.restore_clipboard")),
        row("Add a space after", "So the next sentence doesn't run into the last one.", toggle("paste.add_trailing_space")),
        row("Fit into the text around the cursor", "Reads the text just before the cursor so a dictation continues your sentence (no capital mid-sentence, the right spacing) and names on screen are spelled the same. Read on this PC only, never saved.", toggle("context_awareness")),
        row("Learn from my corrections", "When you fix a word right after dictating, Hyper-Wispr offers it for your dictionary (Vocabulary page).", toggle("learn_corrections")),
      ]),
      section("On-screen indicator", [
        row("Show while dictating", "A small \"Listening\" bubble with voice bars appears while recording, then \"Transcribing\" until the text is typed.", toggle("overlay.enabled", () => render())),
        get("overlay.enabled") && row("Position", null, segmented("overlay.position", [{ value: "bottom", label: "Bottom" }, { value: "top", label: "Top" }])),
      ]),
      startRow && section("System", [startRow]),
    ]),
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
    : S.gpu ? "Your PC has an NVIDIA graphics card, so Automatic uses Large v3 Turbo on it (the first start downloads its speed-up files once)."
      : "This PC has no NVIDIA graphics card that can run the speech model, so Automatic uses Small on the processor.";
  return [
    ...pageHeader("Speech", "The speech-recognition model runs entirely on this computer. Nothing you say is sent anywhere."),
    cols([
      section("Recognition", [
        row("Model", auto, select("speech.model", MODELS)),
        ...languageRows(),
      ], "Changing the model downloads it the first time (it can take a few minutes). Dictation keeps working with the old model until the new one is ready."),
    ], [
      section("Performance", [
        row("Run on", "Automatic uses an NVIDIA graphics card when it can run the model (GTX 10-series, MX150 or newer).",
          segmented("speech.device", [{ value: "auto", label: "Automatic" }, { value: "cuda", label: "NVIDIA GPU" }, { value: "cpu", label: "Processor" }])),
        row("Skip silence", "Ignores pauses so the model doesn't invent words while you think.", toggle("speech.vad_filter")),
      ]),
      advanced([
        row("Accuracy search (beam size)", "Higher can be slightly more accurate but slower. 5 is a good default.", numberField("speech.beam_size", { min: 1, max: 10 })),
        row("Number precision", "Leave on Automatic unless the model fails to load on your graphics card.",
          select("speech.compute_type", [{ value: "auto", label: "Automatic" }, { value: "float16", label: "float16 (GPU)" }, { value: "int8_float16", label: "int8_float16 (GPU, less memory)" }, { value: "int8", label: "int8 (CPU)" }])),
      ]),
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
  const langs = get("speech.languages") || [];
  const others = langs.filter((c) => c !== "en" && c !== "ar");
  const choice = others.length ? null : langs.includes("en") && langs.includes("ar") ? "en+ar" : langs.includes("ar") ? "ar" : langs.includes("en") ? "en" : null;
  const pick = (v) => {
    set("speech.languages", { en: ["en"], ar: ["ar"], "en+ar": ["en", "ar"] }[v]);
    render();
  };
  return [
    row("I dictate in", choice ? { en: "Always English.", ar: "Always Arabic.", "en+ar": "Each dictation is recognised as English or Arabic." }[choice] : languageSummary(), (id) => {
      const group = h("div", { class: "segmented", role: "radiogroup", id, "aria-labelledby": id + "-l" });
      [["en", "English"], ["en+ar", "English + Arabic"], ["ar", "Arabic"]].forEach(([v, label]) => group.append(h("button", {
        type: "button", role: "radio", "aria-checked": String(choice === v), onclick: () => pick(v),
      }, label)));
      return group;
    }),
    h("details", { class: "more", open: S.moreLanguages || (get("speech.languages") || []).some((c) => c !== "en" && c !== "ar") ? true : null,
      ontoggle: (e) => { S.moreLanguages = e.target.open; } },
      h("summary", {}, "Other languages"), languagesPicker()),
  ];
}

function advanced(rows) {
  return h("section", { class: "section" }, h("div", { class: "card" },
    h("details", { class: "more" }, h("summary", {}, "Advanced"), ...rows)));
}

const STYLE_APPS = [["email", "Email"], ["message", "Chat and messages"], ["document", "Documents"], ["ai_chat", "AI assistants"], ["code", "Code editors"], ["other", "Everything else"]];
const TONE_OPTIONS = [{ value: "formal", label: "Formal" }, { value: "casual", label: "Casual" }, { value: "very_casual", label: "Very casual" }];

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
    cols([
      section(null, [
        h("div", { class: "row" },
          h("div", { class: "row-text" }, h("div", { class: "row-label" }, "Status"),
            h("div", { class: "row-desc" }, st && !st.running
              ? ["AI cleanup needs the free Ollama app. Without it, you still get the basic cleanup. ", h("button", { class: "link", type: "button", onclick: () => api().open_url("https://ollama.com/download") }, "Get Ollama")]
              : "Uses Ollama, which runs AI models locally.")),
          h("div", { class: "row-control" }, pill, h("button", { class: "btn small", type: "button", onclick: () => { S.ollama = null; render(); refreshOllama(); } }, "Check again"))),
        row("Use AI cleanup", null, toggle("ai_cleanup.enabled", () => render())),
      ]),
      section("Writing style", STYLE_APPS.map(([app, label]) => row(label, null, segmented(`ai_cleanup.styles.${app}`, TONE_OPTIONS))),
        "How your text should sound in each kind of app. Very casual writes like a quick text: no capital at the start, no final full stop. Without AI cleanup only that part (and dropping the full stop of a one-line chat message) applies."),
      advanced([
        row("Skip AI for short phrases", "Dictations with fewer words than this are pasted without the AI step, so they appear instantly.", numberField("ai_cleanup.min_words", { min: 0, max: 50 })),
        row("Time limit (seconds)", "If the AI takes longer, the basic cleanup is pasted instead.", numberField("ai_cleanup.timeout_seconds", { min: 1, max: 60 })),
        row("Ollama address", null, textField("ai_cleanup.ollama_url", { placeholder: "http://127.0.0.1:11434" })),
      ]),
    ], [
      section("Writing", [
        row("Medical mode", "Keeps drug names, doses, units and clinical abbreviations exact (\"metoprolol 25 mg PO BID\"). If you correct a number out loud, both numbers are kept; say \"scratch that\" and repeat the sentence instead.", toggle("ai_cleanup.medical_mode"), { disabled: off }),
        row("Model", "Larger models write better but take longer. qwen2.5:3b is a good start.",
          modelOptions.length ? select("ai_cleanup.model", modelOptions) : textField("ai_cleanup.model", { placeholder: "qwen2.5:3b" }), { disabled: off }),
        h("div", { class: "row stack" + (off ? " disabled" : "") },
          h("div", { class: "row-text" }, h("label", { class: "row-label", for: "extra" }, "Extra instructions"),
            h("div", { class: "row-desc" }, "Anything else the AI should always do, in plain English. For example: \"Use British spelling.\"")),
          h("textarea", { id: "extra", rows: 3, oninput: (e) => set("ai_cleanup.extra_instructions", e.target.value) }, get("ai_cleanup.extra_instructions") || "")),
      ]),
    ]),
  ];
}

function suggestionsSection() {
  const card = h("div", { class: "card" });
  const wrap = h("section", { class: "section", hidden: true }, h("h2", { class: "section-title" }, "Words you corrected"), card,
    h("p", { class: "section-note" }, "You fixed these right after a dictation. Add them so they're spelled your way next time."));
  const load = async () => {
    const items = await api().get_suggestions();
    wrap.hidden = !items.length;
    card.replaceChildren(...items.map((it) => h("div", { class: "row" },
      h("div", { class: "row-text" }, h("div", { class: "row-label" }, it.word),
        h("div", { class: "row-desc" }, `Heard as "${it.heard}"` + (it.count > 1 ? `, corrected ${it.count} times` : ""))),
      h("div", { class: "row-control" },
        h("button", { class: "btn small", type: "button", onclick: async () => { await api().answer_suggestion(it.word, false); load(); } }, "Not now"),
        h("button", { class: "btn small primary", type: "button", onclick: async () => {
          await api().answer_suggestion(it.word, true);
          // Saved by Python already; keep this window's copy in step so it isn't saved back without it.
          const words = get("dictionary") || [];
          if (!words.some((w) => w.toLowerCase() === it.word.toLowerCase())) {
            S.settings.dictionary = [...words, it.word];
            S.saved.dictionary = [...S.settings.dictionary];
          }
          render();
        } }, "Add to dictionary")))));
  };
  load();
  return wrap;
}

function vocabPage() {
  return [
    ...pageHeader("Vocabulary", "Teach Hyper-Wispr the words you use and the shortcuts you want."),
    suggestionsSection(),
    cols([
      section("Dictionary", [chipList("dictionary", "Add a word, e.g. a colleague's name or a drug")],
        "Spelled exactly like this. Separate several with commas."),
      section("Starred words", [chipList("dictionary_starred", "Add a word that matters most")],
        "The speech model only takes a limited number of words as hints; these always go in. Use it for the names and drugs you say most."),
      section("Replacements", [pairList("replacements", "When you say", "Write", { leftPlaceholder: "hyper wisper", rightPlaceholder: "Hyper-Wispr" })],
        "Always swaps the left side for the right side, ignoring capitals."),
      section("Snippets", [pairList("snippets", "Say this on its own", "To insert", { multiline: true, leftPlaceholder: "my signature", rightPlaceholder: "Dr. …\nClinic …" })],
        "Saying the phrase by itself pastes the full text instead."),
    ], [
      section("Basic cleanup", [
        row("Remove filler words", "Removes the words below when they're said on their own.", toggle("cleanup.remove_fillers", () => render())),
        get("cleanup.remove_fillers") && chipList("cleanup.filler_words", "Add a filler word"),
        row("Spoken punctuation", "Say \"new line\", \"new paragraph\", \"period\" or \"comma\" to insert them.", toggle("cleanup.spoken_punctuation")),
      ]),
      section("Undo phrases", [chipList("cleanup.backtrack_phrases", "Add a phrase")],
        "Saying one of these deletes the sentence you just said."),
    ]),
  ];
}

function historyItem(it, onDelete, onRedo) {
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
      it.can_redo && onRedo && h("button", { class: "icon-btn", type: "button", title: "Redo with a bigger speech model", "aria-label": "Redo this dictation with a bigger speech model",
        onclick: async (e) => {
          const btn = e.currentTarget;
          btn.disabled = true;
          btn.classList.add("busy");
          historyNote("Redoing the dictation… The first time loads the bigger speech model, which can take a minute.");
          const res = await api().redo_dictation(it.id);
          btn.disabled = false;
          btn.classList.remove("busy");
          historyNote(res.ok ? "Redone. Use Copy to paste the new version." : "Couldn't redo it: " + res.error);
          if (res.ok) onRedo();
        } }, icon("redo")),
      h("button", { class: "icon-btn", type: "button", title: "Delete", "aria-label": "Delete this dictation", onclick: async () => { await api().delete_history(it.id); onDelete(); } }, icon("trash"))));
}

function historyNote(text) {
  const el = document.getElementById("history-note");
  if (el) el.textContent = text;
}

function historyPage() {
  const list = h("div", { class: "card" }, h("div", { class: "row" }, h("span", { class: "empty" }, "Loading…")));
  const load = async () => {
    const q = S.historySearch || "";
    const items = await api().get_history(200, q);
    if (!items.length) {
      list.replaceChildren(h("div", { class: "row" }, h("span", { class: "empty" }, q ? "No dictations contain those words." : "No dictations yet. Hold your shortcut and speak to make one.")));
      return;
    }
    list.replaceChildren(...items.map((it) => historyItem(it, load, load)));
  };
  load();
  let searchTimer = null;
  const search = h("input", { type: "search", class: "grow", placeholder: "Search your dictations", "aria-label": "Search your dictations", value: S.historySearch || "",
    oninput: (e) => { S.historySearch = e.target.value; clearTimeout(searchTimer); searchTimer = setTimeout(load, 250); } });
  const days = get("history.keep_days") || 0;
  return [
    ...pageHeader("History", "Your recent dictations, stored only on this computer and encrypted so only your Windows account can read them."),
    section(null, [
      row("Keep history", "Turn off to stop saving new dictations.", toggle("history.enabled")),
      row("Delete dictations after", days ? "Older dictations are deleted automatically. Your usage totals on the dashboard are kept." : "Dictations are kept until you delete them.",
        select("history.keep_days", [{ value: 7, label: "1 week" }, { value: 30, label: "30 days" }, { value: 90, label: "90 days" }, { value: 365, label: "1 year" }, { value: 0, label: "Never" }], { toValue: Number, onChange: () => render() })),
      row("Keep recordings for Redo", "The last few recordings are kept (encrypted) so a dictation can be redone with a bigger, more accurate speech model.",
        select("history.keep_recordings", [{ value: 20, label: "Last 20" }, { value: 5, label: "Last 5" }, { value: 0, label: "None" }], { toValue: Number })),
      h("div", { class: "row" }, h("div", { class: "row-text" }, h("div", { class: "row-label" }, "Delete all history")),
        h("div", { class: "row-control" }, confirmButton("Delete all…", "Delete every saved dictation?", "Delete all", async () => { await api().delete_history(null); load(); }))),
    ]),
    h("section", { class: "section" }, h("h2", { class: "section-title" }, "Recent"),
      h("div", { class: "search-bar" }, search), h("p", { class: "section-note", id: "history-note", "aria-live": "polite" }), list),
  ];
}

/* ---- notes --------------------------------------------------------------- */
function notesPage() {
  const listEl = h("div", { class: "card notes-list" });
  const editor = h("textarea", { class: "note-editor", "aria-label": "Note", placeholder: "Click here and dictate, or type. Notes save as you go." });
  const meta = h("div", { class: "row-desc", "aria-live": "polite" });
  let saveTimer = null;
  const current = () => (S.notes || []).find((n) => n.id === S.noteId);
  const paintList = () => {
    const notes = S.notes || [];
    listEl.replaceChildren(...(notes.length ? notes.map((n) => h("button", {
      type: "button", class: "note-item", "aria-current": n.id === S.noteId ? "true" : null, onclick: () => open(n.id),
    }, h("span", { class: "note-title" }, (n.body.trim().split("\n")[0] || "Empty note").slice(0, 60)),
      h("span", { class: "row-desc" }, new Date(n.updated * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }))))
      : [h("div", { class: "row" }, h("span", { class: "empty" }, "No notes yet."))]));
  };
  const open = (id) => {
    clearTimeout(saveTimer);
    S.noteId = id;
    const n = current();
    editor.value = n ? n.body : "";
    meta.textContent = n ? "Saved" : "";
    paintList();
  };
  const save = async () => {
    const res = await api().save_note(editor.value, S.noteId ?? null);
    if (!res.ok) { meta.textContent = "Couldn't save: " + res.error; return; }
    S.noteId = res.id;
    S.notes = await api().get_notes();
    meta.textContent = "Saved";
    paintList();
  };
  editor.addEventListener("input", () => { meta.textContent = "Saving…"; clearTimeout(saveTimer); saveTimer = setTimeout(save, 600); });
  const load = async () => {
    S.notes = await api().get_notes();
    if (S.noteId == null && S.notes.length) S.noteId = S.notes[0].id;
    open(S.noteId);
  };
  load();
  return [
    ...pageHeader("Notes", "A notepad for things that don't belong in another app yet: ward lists, reminders, drafts. Click into a note and dictate. Stored only on this computer, encrypted."),
    h("div", { class: "notes" },
      h("div", { class: "notes-side" },
        h("button", { class: "btn primary with-icon", type: "button", onclick: () => { open(null); editor.focus(); } }, icon("plus"), "New note"),
        listEl),
      h("div", { class: "card notes-main" },
        editor,
        h("div", { class: "notes-foot" }, meta,
          h("div", { class: "row-control" },
            h("button", { class: "btn", type: "button", onclick: () => api().copy_text(editor.value) }, "Copy all"),
            confirmButton("Delete…", "Delete this note?", "Delete", async () => {
              if (S.noteId != null) await api().delete_note(S.noteId);
              S.noteId = null;
              load();
            }))))),
  ];
}

/* ---- updates ------------------------------------------------------------- */
async function checkForUpdate(manual) {
  S.checking = true;
  if (S.page === "about") render();
  const res = await api().check_update();
  S.checking = false;
  // A failed automatic check stays quiet; a failed manual one says why.
  if (res.ok || manual) S.update = res;
  renderNav();
  if (S.page === "about" || S.page === "home") render();
}

function formatSize(bytes) {
  return bytes >= 1e9 ? (bytes / 1e9).toFixed(1) + " GB" : Math.max(1, Math.round(bytes / 1e6)) + " MB";
}

let downloadTimer = null;
function watchDownload() {
  clearInterval(downloadTimer);
  downloadTimer = setInterval(async () => {
    S.download = await api().update_progress();
    if (S.download.state !== "downloading") clearInterval(downloadTimer);
    const bar = document.getElementById("dl-fill");
    if (bar && S.download.state === "downloading") {
      const pct = S.download.total ? (S.download.done / S.download.total) * 100 : 0;
      bar.style.width = pct + "%";
      document.getElementById("dl-text").textContent = `Downloading… ${formatSize(S.download.done)} of ${formatSize(S.download.total)}`;
    } else if (S.page === "about") render();
  }, 300);
}

function updateRows() {
  const u = S.update;
  const d = S.download || { state: "idle" };
  const canInstall = S.updateInfo && S.updateInfo.can_install;
  const checkBtn = h("button", { class: "btn", type: "button", disabled: S.checking || d.state === "downloading", onclick: () => checkForUpdate(true) }, S.checking ? "Checking…" : "Check now");
  let status;
  if (S.checking && !u) status = "Checking GitHub for a newer version…";
  else if (!u) status = "Not checked yet.";
  else if (!u.ok) status = h("span", { class: "hint-error" }, u.error);
  else if (!u.available) status = `You're up to date. ${u.latest && u.latest !== S.info.version ? `The latest release is ${u.latest}.` : ""}`;
  else status = h("span", { class: "update-new" }, `Version ${u.latest} is available.`);
  const rows = [
    h("div", { class: "row" },
      h("div", { class: "row-text" }, h("div", { class: "row-label" }, `Version ${S.info.version}`), h("div", { class: "row-desc", "aria-live": "polite" }, status)),
      h("div", { class: "row-control" }, checkBtn)),
  ];
  if (u && u.ok && u.available) {
    let action;
    if (!canInstall) {
      action = h("button", { class: "btn primary", type: "button", onclick: () => api().open_url(u.page) }, "Open download page");
    } else if (d.state === "downloading") {
      const pct = d.total ? (d.done / d.total) * 100 : 0;
      action = h("div", { class: "download" },
        h("div", { class: "meter" }, h("div", { class: "meter-fill", id: "dl-fill", style: `width:${pct}%` })),
        h("span", { class: "row-desc", id: "dl-text" }, "Downloading…"),
        h("button", { class: "btn small", type: "button", onclick: () => api().cancel_update() }, "Cancel"));
    } else if (d.state === "ready") {
      action = h("button", { class: "btn primary", type: "button", onclick: async () => {
        const res = await api().install_update();
        if (!res.ok) { S.download = { state: "error", error: res.error }; render(); }
        else { S.download = { state: "installing" }; render(); }
      } }, "Install and restart");
    } else if (d.state === "installing") {
      action = h("span", { class: "row-desc" }, "Installing… Hyper-Wispr closes and opens again by itself.");
    } else {
      action = h("button", { class: "btn primary with-icon", type: "button", onclick: async () => {
        const res = await api().download_update(u.asset);
        if (!res.ok) { S.download = { state: "error", error: res.error }; render(); return; }
        S.download = { state: "downloading", done: 0, total: u.asset.size };
        render();
        watchDownload();
      } }, icon("download"), `Download ${formatSize(u.asset.size)}`);
    }
    rows.push(h("div", { class: "row stack" },
      h("div", { class: "meter-row" },
        h("div", { class: "row-text" }, h("div", { class: "row-label" }, `What's new in ${u.latest}`),
          h("div", { class: "row-desc" }, d.state === "ready" ? "Downloaded. Installing takes about a minute; your settings, history and speech model are kept." : "Your settings, history and speech model are kept.")),
        action),
      d.state === "error" && h("div", { class: "hint-error", role: "alert" }, d.error),
      u.notes && h("details", { class: "notes" }, h("summary", {}, "Release notes"), h("div", { class: "notes-body" }, u.notes))));
  }
  rows.push(row("Check automatically", "Looks for a new version once a day when this window opens. Only the version number is fetched from GitHub; nothing about you or your dictations is sent.", toggle("updates.check_automatically")));
  return rows;
}

function aboutPage() {
  return [
    h("div", { class: "card section" }, h("div", { class: "about-hero" },
      h("div", { class: "brand-mark", "aria-hidden": "true", html: '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><rect x="9" y="3" width="6" height="11" rx="3" fill="currentColor"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/></svg>' }),
      h("div", {}, h("h1", {}, "Hyper-Wispr"), h("div", { class: "row-desc" }, `Version ${S.info.version} · Private, offline dictation. Speech recognition and AI cleanup both run on this computer.`)))),
    cols([
      section("Updates", updateRows()),
    ], [
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
            S.saved = structuredClone(st.settings);
            S.info = st; // clears a "settings file could not be read" warning
            setStatus("saved");
            render();
          }))),
      ]),
    ]),
    h("p", { class: "section-note" }, h("button", { class: "link", type: "button", onclick: () => api().open_url("https://github.com/omar-184/Hyper-Wispr") }, "Hyper-Wispr on GitHub")),
  ];
}

const RENDER = { home: homePage, general: generalPage, speech: speechPage, ai: aiPage, vocab: vocabPage, history: historyPage, notes: notesPage, about: aboutPage };

/* ---- navigation ---------------------------------------------------------- */
function renderNav() {
  const nav = document.getElementById("nav");
  nav.replaceChildren(...PAGES.flatMap((p) => [
    p.group && h("div", { class: "nav-group" }, h("span", {}, p.group)),
    h("button", {
      type: "button", "aria-current": p.id === S.page ? "page" : null, title: p.label,
      onclick: () => go(p.id),
    }, icon(p.icon), h("span", {}, p.label),
    p.id === "about" && S.update && S.update.available && h("span", { class: "badge", title: "Update available", "aria-label": "Update available" })),
  ].filter(Boolean)));
}

function go(page) {
  if (page === S.page) return;
  stopMicTest();
  S.page = page;
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
  S.saved = structuredClone(st.settings);
  S.defaults = st.defaults;
  S.info = st;
  document.getElementById("brand-sub").textContent = "Version " + st.version;
  setStatus("saved");
  renderNav();
  render(true);
  window.__hwReady = true;
  api().window_ready();

  // Slower lookups fill in when ready.
  refreshStatus();
  refreshStats();
  setInterval(refreshStatus, 1500);
  setInterval(() => { if (S.page === "home") refreshStats(); }, 30000); // e.g. after midnight
  api().update_info().then((info) => {
    S.updateInfo = info;
    const last = info.last;
    // Shows what the last check found straight away, then checks again if that was over a day ago.
    if (last && last.ok && last.current === st.version) S.update = last;
    renderNav();
    if (get("updates.check_automatically") && (!last || Date.now() / 1000 - last.checked > 20 * 3600 || last.current !== st.version)) checkForUpdate(false);
    else if (S.page === "home" || S.page === "about") render();
  });
  api().list_microphones().then((m) => { S.mics = m; if (S.page === "general") render(); });
  api().gpu_available().then((g) => { S.gpu = !!g; if (S.page === "speech") render(); }).catch(() => { S.gpu = false; });
  refreshOllama();
}

document.addEventListener("keydown", (e) => {
  // Ctrl+1..8 jumps between sections.
  if (e.ctrlKey && !e.altKey && /^[1-8]$/.test(e.key)) { e.preventDefault(); go(PAGES[Number(e.key) - 1].id); }
});

if (window.pywebview && window.pywebview.api && window.pywebview.api.get_state) init();
else window.addEventListener("pywebviewready", init, { once: true });
