import { $, h, store, fmtNum, copyText, downloadBlob, toast, stamp, skeletonLines } from "./dom.js";
import { icon, setIcon } from "./icons.js";
import { api, streamNdjson, ApiError, MODEL_DOWN_MESSAGE } from "./api.js";
import {
  AUTO_LABEL,
  isLanguage,
  langLabel,
  fillLangSelect,
  setSelectValue,
  setAutoOptionLabel,
  fillFormality,
  speechLang,
  unitJoiner,
  toLabel,
} from "./langs.js";
import { openPopover, closePopover, currentPopover, isPopoverFor, enableListNavigation } from "./popover.js";
import {
  glossaryState,
  onGlossaryChange,
  renderGlossaryOptions,
  setActiveGlossary,
  setGlossaryEnabled,
  draftEntries,
  addGlossaryEntry,
  activeSummary,
  setDirectionProvider,
} from "./glossary.js";
import { getStatus, onStatus } from "./status.js";
import { recordTranslation } from "./history.js";

const KEYS = {
  src: "translator.sourceLang",
  tgt: "translator.targetLang",
  formality: "translator.formality",
  rules: "translator.instructions",
  lastSource: "translator.lastDetectedSource",
};
const CONTEXT_MAX = 4000;
const RULES_MAX = 2000;
const PARA_SPLIT = /\n[ \t]*\n/;
// Alternatives open only after the pointer settles; long paragraphs are cut to a window around the span
// (the API caps source/translation at 20,000 chars).
const ALT_DELAY_MS = 300;
const ALT_CONTEXT = 1500;
const ALT_TRANSLATION_MAX = 4000;
const ALT_SOURCE_MAX = 6000;
const ALT_CACHE_MAX = 30;

const el = {};
const S = {
  timer: 0,
  controller: null,
  reqId: 0,
  phase: "idle",
  current: null,
  units: [],
  paras: [],
  finals: [],
  segments: [],
  detected: null,
  hits: [],
  glossaryName: null,
  result: null,
  prev: { key: "", map: new Map() },
  pinned: null,
  altController: null,
  altTimer: 0,
  editing: false,
  lookupController: null,
  speaking: false,
};
const paraSource = new WeakMap();
const altCache = new Map();
const speechSupported = "speechSynthesis" in window && typeof window.SpeechSynthesisUtterance === "function";
const highlightSupported = typeof CSS !== "undefined" && CSS.highlights && typeof window.Highlight === "function";
let onFiles = () => {};

// ---------- helpers ----------

const normalizeText = (text) => text.replace(/\r\n?/g, "\n").replace(/\n{3,}/g, "\n\n");
const splitParagraphs = (text) =>
  normalizeText(text)
    .trim()
    .split(PARA_SPLIT)
    .map((part) => part.trim())
    .filter(Boolean);
const textLimit = () => Number(getStatus().limits?.text_max_chars) || 30000;
const unitText = (unit) => unit.buf || unit.prefill || "";

function resolvedSource() {
  if (el.srcLang.value !== "auto") return el.srcLang.value;
  return S.detected?.primary && isLanguage(S.detected.primary) ? S.detected.primary : "auto";
}

function requestOptions(text) {
  const glossaryId = glossaryState.activeId;
  const useGlossary = Boolean(glossaryState.enabled && glossaryId);
  return {
    source_lang: el.srcLang.value,
    target_lang: el.tgtLang.value,
    formality: el.formality.value || "auto",
    context: el.ctx.value.trim().slice(0, CONTEXT_MAX),
    instructions: el.rules.value.trim().slice(0, RULES_MAX),
    use_glossary: useGlossary,
    glossary_id: useGlossary ? glossaryId : null,
    glossary_entries: useGlossary ? draftEntries(text) : null,
  };
}

const optionsKey = (opts) =>
  JSON.stringify([opts.source_lang, opts.target_lang, opts.formality, opts.context, opts.instructions, opts.use_glossary, opts.glossary_id]);

function setHighlight(name, range) {
  if (!highlightSupported) return;
  if (range) CSS.highlights.set(name, new window.Highlight(range));
  else CSS.highlights.delete(name);
}

// Screen-reader status for the output (role=status, aria-live=polite).
function announce(message) {
  if (!el.live) return;
  el.live.textContent = "";
  if (message) setTimeout(() => (el.live.textContent = message), 60);
}

function flashIcon(button, name) {
  setIcon(button, "check");
  button.classList.add("is-done");
  clearTimeout(button._flash);
  button._flash = setTimeout(() => {
    setIcon(button, name);
    button.classList.remove("is-done");
  }, 1400);
}

// ---------- language / option controls ----------

function refreshFormality(value) {
  const map = store.get(KEYS.formality, {}) || {};
  fillFormality(el.formality, el.tgtLang.value, value ?? map[el.tgtLang.value] ?? "auto");
}

function saveFormality() {
  const map = store.get(KEYS.formality, {}) || {};
  map[el.tgtLang.value] = el.formality.value;
  store.set(KEYS.formality, map);
}

function renderGlossaryControls() {
  renderGlossaryOptions(el.glossSel, { value: glossaryState.activeId });
  const has = glossaryState.list.length > 0;
  el.glossToggle.checked = glossaryState.enabled && has;
  el.glossToggle.disabled = !has;
  el.glossSel.disabled = !has;
  el.glossWrap.classList.toggle("is-off", !el.glossToggle.checked);
}

function updateCtxIndicators() {
  el.ctxCount.textContent = `${fmtNum(el.ctx.value.length)} / ${fmtNum(CONTEXT_MAX)}`;
  el.rulesCount.textContent = `${fmtNum(el.rules.value.length)} / ${fmtNum(RULES_MAX)}`;
  el.ctxDot.hidden = !(el.ctx.value.trim() || el.rules.value.trim());
}

function toggleCtxPanel(force) {
  const open = typeof force === "boolean" ? force : el.ctxPanel.hidden;
  el.ctxPanel.hidden = !open;
  el.ctxToggle.setAttribute("aria-expanded", String(open));
  if (open) el.ctx.focus();
}

function updateDetectedLabel() {
  const detected = S.detected;
  if (el.srcLang.value !== "auto" || !detected) {
    setAutoOptionLabel(el.srcLang, AUTO_LABEL);
    return;
  }
  let label = AUTO_LABEL;
  if (detected.mode === "mixed") {
    const names = (detected.languages || [])
      .filter((item) => isLanguage(item.code))
      .slice(0, 2)
      .map((item) => langLabel(item.code));
    if (names.length) label = `${names.join("·")} (감지됨)`;
  } else if (detected.primary && isLanguage(detected.primary)) {
    label = `${langLabel(detected.primary)} (감지됨)`;
  }
  setAutoOptionLabel(el.srcLang, label);
}

// ---------- source input ----------

function updateCount() {
  const length = el.src.value.length;
  const limit = textLimit();
  el.count.textContent = `${fmtNum(length)} / ${fmtNum(limit)}`;
  el.count.classList.toggle("is-over", length > limit);
}

function autoGrow() {
  const area = el.src;
  area.style.height = "auto";
  area.style.height = `${area.scrollHeight}px`;
}

function abortStream() {
  if (S.controller) {
    S.controller.abort();
    S.controller = null;
  }
  S.reqId += 1;
}

function schedule(delay) {
  clearTimeout(S.timer);
  setPhase("waiting");
  S.timer = setTimeout(run, delay);
}

function scheduleIfText(delay = 0) {
  if (!el.src.value.trim()) return;
  abortStream();
  schedule(delay);
}

function onSourceInput() {
  updateCount();
  autoGrow();
  el.clear.hidden = !el.src.value;
  abortStream();
  if (!el.src.value.trim()) {
    resetOutput();
    return;
  }
  schedule(/[.!?。！？…\n]\s*$/.test(el.src.value) ? 180 : 400);
}

export function translateNow() {
  if (!el.src.value.trim()) return;
  abortStream();
  run();
}

// ---------- streaming translation ----------

function setPhase(phase) {
  S.phase = phase;
  el.busy.hidden = phase !== "streaming";
  el.out.classList.toggle("is-stale", phase === "waiting");
  el.out.setAttribute("aria-busy", String(phase === "streaming" || phase === "waiting"));
}

function enableEditing(on) {
  if (on) {
    el.out.contentEditable = "plaintext-only";
    if (el.out.contentEditable !== "plaintext-only") el.out.contentEditable = "true";
    el.out.setAttribute("aria-readonly", "false");
  } else {
    el.out.contentEditable = "false";
    el.out.setAttribute("aria-readonly", "true");
  }
}

async function run() {
  clearTimeout(S.timer);
  const text = el.src.value;
  if (!text.trim()) {
    resetOutput();
    return;
  }
  const limit = textLimit();
  if (text.length > limit) {
    abortStream();
    showError(`최대 ${fmtNum(limit)}자까지 번역할 수 있습니다.`, { retry: false });
    return;
  }
  const opts = requestOptions(text);
  const key = optionsKey(opts);
  if (S.pinned && S.pinned.key !== key) S.pinned = null;
  if (S.pinned && renderPinnedOnly(text)) return;

  // After a swap, paragraphs that still match the swapped output keep their text and are not re-sent.
  let layout = null;
  let sendText = text;
  if (S.pinned) {
    const paragraphs = splitParagraphs(text);
    if (paragraphs.some((part) => S.pinned.map.has(part))) {
      layout = paragraphs.map((part) => ({ source: part, pinned: S.pinned.map.get(part) }));
      sendText = layout
        .filter((slot) => slot.pinned == null)
        .map((slot) => slot.source)
        .join("\n\n");
    }
  }

  abortStream();
  const controller = new AbortController();
  S.controller = controller;
  const id = S.reqId;
  S.current = { text: sendText, fullText: text, layout, opts, key, started: false };
  announce("");
  setPhase("streaming");
  updateFoot();
  try {
    await streamNdjson(
      "/api/translate/stream",
      { ...opts, text: sendText },
      {
        signal: controller.signal,
        onEvent: (event) => {
          if (id === S.reqId) handleEvent(event);
        },
      },
    );
    if (id === S.reqId && S.phase === "streaming") {
      if (S.current.started && S.units.every((unit) => unit.done || unitText(unit))) finalize(null);
      else failStream("번역이 중단되었습니다.");
    }
  } catch (error) {
    if (error?.name === "AbortError" || id !== S.reqId) return;
    const down = error instanceof ApiError && error.modelDown;
    failStream(down ? MODEL_DOWN_MESSAGE : error?.message || "번역하지 못했습니다.");
  } finally {
    if (S.controller === controller) S.controller = null;
  }
}

function handleEvent(event) {
  switch (event?.type) {
    case "start":
      onStart(event);
      break;
    case "delta": {
      const unit = S.units[event.index];
      if (!unit || unit.locked) return;
      unit.buf += event.text || "";
      paintUnit(unit);
      break;
    }
    case "unit": {
      const unit = S.units[event.index];
      if (!unit || unit.locked) return;
      if (typeof event.text === "string") unit.buf = event.text;
      unit.done = true;
      paintUnit(unit);
      break;
    }
    case "done":
      finalize(event);
      break;
    case "error":
      failStream(event.detail || "번역하지 못했습니다.");
      break;
    default:
      break;
  }
}

function groupUnits(text, segments, target) {
  const norm = normalizeText(text);
  const joiner = unitJoiner(target);
  const paras = [];
  let cursor = 0;
  let prevEnd = -1;
  let aligned = true;
  segments.forEach((segment, index) => {
    let start = -1;
    let length = segment.length;
    if (aligned) {
      start = norm.indexOf(segment, cursor);
      if (start < 0) {
        const trimmed = segment.trim();
        if (trimmed) {
          start = norm.indexOf(trimmed, cursor);
          length = trimmed.length;
        }
      }
      if (start < 0) aligned = false;
    }
    if (start < 0) {
      paras.push({ units: [index], joins: [""], source: segment });
      prevEnd = -1;
      return;
    }
    const last = paras[paras.length - 1];
    const gap = prevEnd >= 0 ? norm.slice(prevEnd, start) : null;
    if (last && last.start != null && gap !== null && !PARA_SPLIT.test(gap)) {
      last.units.push(index);
      last.joins.push(gap.includes("\n") ? "\n" : joiner);
      last.end = start + length;
    } else {
      paras.push({ units: [index], joins: [""], start, end: start + length });
    }
    cursor = start + length;
    prevEnd = cursor;
  });
  for (const para of paras) {
    if (para.source == null) para.source = norm.slice(para.start, para.end);
  }
  return paras;
}

function pinnedFor(source) {
  const map = S.pinned?.map;
  if (!map) return null;
  const key = source.trim();
  if (map.has(key)) return map.get(key);
  const parts = key.split(PARA_SPLIT).map((part) => part.trim());
  if (parts.length > 1 && parts.every((part) => map.has(part))) return parts.map((part) => map.get(part)).join("\n\n");
  return null;
}

// DeepL 방식: 자동 감지한 원문 언어가 번역 언어와 같으면 번역 언어를 바꿔 다시 번역한다.
// 바꿀 언어는 마지막으로 감지된 다른 원문 언어, 없으면 한국어 ↔ 영어.
function autoSwitchTarget(event) {
  const primary = event.primary_source_lang;
  if (el.srcLang.value !== "auto" || event.source_language_mode !== "single" || !isLanguage(primary)) return false;
  if (primary !== el.tgtLang.value) {
    store.set(KEYS.lastSource, primary);
    return false;
  }
  const remembered = store.get(KEYS.lastSource, null);
  const next = isLanguage(remembered) && remembered !== primary ? remembered : primary === "ko" ? "en" : "ko";
  setSelectValue(el.tgtLang, next);
  store.set(KEYS.tgt, next);
  refreshFormality();
  abortStream();
  schedule(0);
  return true;
}

function onStart(event) {
  if (autoSwitchTarget(event)) return;
  const current = S.current;
  current.started = true;
  S.detected = {
    primary: event.primary_source_lang || null,
    mode: event.source_language_mode || "unknown",
    languages: Array.isArray(event.detected_source_languages) ? event.detected_source_languages : [],
  };
  S.hits = Array.isArray(event.glossary_hits) ? event.glossary_hits : [];
  S.glossaryName = event.glossary_name || null;
  updateDetectedLabel();
  const segments = Array.isArray(event.segments) ? event.segments.map((segment) => String(segment ?? "")) : [];
  const previous = S.prev.key === current.key ? S.prev.map : null;
  S.units = segments.map((source) => ({
    source,
    buf: "",
    prefill: previous?.get(source) ?? "",
    done: false,
    locked: false,
    el: null,
  }));
  S.paras = groupUnits(current.text, segments, el.tgtLang.value);
  if (current.layout) {
    const groups = [...S.paras];
    const merged = [];
    for (const slot of current.layout) {
      if (slot.pinned != null) merged.push({ units: [], joins: [], source: slot.source, pinned: slot.pinned });
      else if (groups.length) merged.push(groups.shift());
    }
    merged.push(...groups);
    S.paras = merged;
  } else if (S.pinned) {
    for (const para of S.paras) {
      const pinned = pinnedFor(para.source);
      if (pinned != null) {
        para.pinned = pinned;
        for (const index of para.units) Object.assign(S.units[index], { locked: true, done: true });
        continue;
      }
      for (const index of para.units) {
        const value = pinnedFor(S.units[index].source);
        if (value != null) Object.assign(S.units[index], { locked: true, done: true, buf: value });
      }
    }
  }
  buildStreamingOutput();
  updateFoot();
}

function buildStreamingOutput() {
  enableEditing(false);
  const nodes = S.paras.map((para) => {
    const node = h("div", { class: "para" });
    para.el = node;
    if (para.pinned != null) {
      node.textContent = para.pinned;
      return node;
    }
    para.units.forEach((index, position) => {
      if (position) node.append(document.createTextNode(para.joins[position]));
      const unit = S.units[index];
      unit.el = h("span", { class: "unit" });
      node.append(unit.el);
      paintUnit(unit);
    });
    return node;
  });
  el.out.replaceChildren(...nodes);
  el.out.classList.remove("is-stale", "is-error");
  el.placeholder.hidden = nodes.length > 0;
}

function paintUnit(unit) {
  if (!unit.el) return;
  const text = unit.buf || (unit.done ? "" : unit.prefill);
  if (!text && !unit.done) {
    if (!unit.el.classList.contains("pending")) {
      const length = unit.source.length;
      const lines = Math.max(1, Math.min(6, Math.ceil(length / 70)));
      unit.el.className = "unit pending";
      unit.el.replaceChildren(skeletonLines(lines, { lastWidth: lines === 1 ? Math.min(85, 25 + length) : 35 + ((length * 7) % 50) }));
    }
    return;
  }
  unit.el.className = unit.done || !unit.buf ? "unit" : "unit typing";
  unit.el.textContent = text;
}

function splitPairs(source, target) {
  const sources = source.split(PARA_SPLIT);
  const targets = target.split(PARA_SPLIT);
  if (sources.length > 1 && sources.length === targets.length) {
    return sources.map((part, index) => ({ source: part.trim(), target: targets[index].trim() }));
  }
  return [{ source: source.trim(), target: target.trim() }];
}

function renderFinal(finals) {
  S.finals = finals;
  S.editing = false;
  const nodes = finals.map((pair) => {
    const node = h("div", { class: "para" });
    node.textContent = pair.target;
    paraSource.set(node, pair.source);
    return node;
  });
  el.out.replaceChildren(...nodes);
  el.out.classList.remove("is-stale", "is-error");
  el.placeholder.hidden = finals.some((pair) => pair.target);
  enableEditing(true);
}

function finalize(event) {
  const current = S.current;
  if (!current) return;
  for (const unit of S.units) unit.done = true;
  const finals = [];
  for (const para of S.paras) {
    const text =
      para.pinned != null
        ? para.pinned
        : para.units.map((index, position) => (position ? para.joins[position] : "") + unitText(S.units[index])).join("");
    finals.push(...splitPairs(para.source, text));
  }
  S.segments =
    Array.isArray(event?.segments) && event.segments.length
      ? event.segments
      : S.units.map((unit) => ({ source: unit.source, target: unitText(unit) }));
  if (!S.pinned) S.prev = { key: current.key, map: new Map(S.units.map((unit) => [unit.source, unitText(unit)])) };
  S.result = {
    sourceText: current.fullText,
    sourceLang: resolvedSource(),
    sourceSelect: el.srcLang.value,
    targetLang: el.tgtLang.value,
    formality: el.formality.value,
  };
  renderFinal(finals);
  setPhase("done");
  updateFoot();
  announce("번역 완료");
  if (!el.segPanel.hidden) renderSegments();
  recordTranslation({
    source: current.fullText,
    translation: outputText(),
    source_lang: S.result.sourceLang,
    source_select: S.result.sourceSelect,
    target_lang: S.result.targetLang,
    formality: S.result.formality,
  });
}

function renderPinnedOnly(text) {
  const paragraphs = splitParagraphs(text);
  if (!paragraphs.length || !paragraphs.every((part) => S.pinned.map.has(part))) return false;
  abortStream();
  renderFinal(paragraphs.map((part) => ({ source: part, target: S.pinned.map.get(part) })));
  S.segments = S.finals.map((pair) => ({ ...pair }));
  S.result = { ...(S.result || {}), sourceText: text };
  setPhase("done");
  updateFoot();
  return true;
}

function errorBox(message, retry) {
  return h(
    "div",
    { class: "out-error", role: "alert" },
    h("div", { class: "out-error-msg" }, icon("alert", { size: 18 }), h("span", { text: message })),
    retry
      ? h(
          "button",
          { type: "button", class: "btn btn-sm", onclick: () => translateNow() },
          icon("refresh", { size: 16 }),
          "다시 시도",
        )
      : null,
  );
}

// Error after some paragraphs arrived: keep what was translated, drop the placeholders, show the error below.
function showPartialError(message) {
  closeAltPopover();
  setPhase("error");
  enableEditing(false);
  S.finals = [];
  for (const unit of S.units) {
    if (!unit.el) continue;
    if (unitText(unit)) {
      unit.el.className = "unit";
      unit.el.textContent = unitText(unit);
    } else {
      unit.el.remove();
    }
  }
  for (const para of S.paras) {
    if (para.el && !para.el.textContent.trim()) para.el.remove();
  }
  el.out.append(errorBox(message, true));
  el.out.classList.remove("is-stale");
  el.placeholder.hidden = true;
  updateFoot();
}

function failStream(message) {
  if (S.current?.started && S.units.some((unit) => unitText(unit))) showPartialError(message);
  else showError(message);
}

function showError(message, { retry = true } = {}) {
  closeAltPopover();
  setPhase("error");
  enableEditing(false);
  S.finals = [];
  el.out.replaceChildren(errorBox(message, retry));
  el.out.classList.add("is-error");
  el.out.classList.remove("is-stale");
  el.placeholder.hidden = true;
  updateFoot();
}

function resetOutput() {
  abortStream();
  clearTimeout(S.timer);
  Object.assign(S, { current: null, units: [], paras: [], finals: [], segments: [], hits: [], detected: null, result: null, pinned: null });
  el.out.replaceChildren();
  el.out.classList.remove("is-stale", "is-error");
  el.placeholder.hidden = false;
  enableEditing(false);
  setPhase("idle");
  updateDetectedLabel();
  updateFoot();
  if (!el.segPanel.hidden) renderSegments();
  stopSpeaking();
  cancelPendingAlternatives();
  closeAltPopover();
}

function outputText() {
  const parts = [];
  for (const node of el.out.childNodes) {
    const text = node.nodeType === Node.TEXT_NODE ? node.textContent : node.innerText ?? node.textContent;
    if (text && text.trim()) parts.push(text.replace(/\n+$/, ""));
  }
  return parts.join("\n\n");
}

function identityAlternative() {
  return S.detected?.primary === "ko" ? "en" : "ko";
}

function updateFoot() {
  const hasOutput = S.phase === "done" && S.finals.some((pair) => pair.target);
  el.foot.hidden = !(hasOutput || S.phase === "streaming");
  for (const button of [el.copy, el.save, el.speak, el.segBtn]) button.disabled = !hasOutput;
  el.speak.hidden = !speechSupported;
  const hitCount = S.hits.length;
  el.hits.hidden = !hitCount || !(hasOutput || S.phase === "streaming");
  if (hitCount) el.hits.querySelector(".chip-text").textContent = `용어 ${fmtNum(hitCount)}`;
  const identity =
    hasOutput && el.srcLang.value === "auto" && S.detected?.mode === "single" && S.detected.primary === el.tgtLang.value;
  el.identity.hidden = !identity;
  if (identity) {
    const lang = identityAlternative();
    el.identity.dataset.lang = lang;
    el.identity.querySelector(".chip-text").textContent = `${toLabel(lang)} 번역`;
  }
}

// ---------- swap ----------

function detectedChoices() {
  const target = el.tgtLang.value;
  const seen = new Set();
  return (S.detected?.languages || [])
    .filter((item) => {
      if (!item || !isLanguage(item.code) || item.code === target || !(Number(item.share) > 0) || seen.has(item.code)) return false;
      seen.add(item.code);
      return true;
    })
    .sort((a, b) => Number(b.share) - Number(a.share));
}

export function swapLanguages() {
  if (S.phase === "streaming" || S.phase === "waiting") {
    toast("번역이 끝난 뒤 바꿀 수 있습니다", { type: "info" });
    return;
  }
  const target = el.tgtLang.value;
  let next = null;
  if (el.srcLang.value !== "auto") {
    next = el.srcLang.value;
  } else {
    const choices = detectedChoices();
    const primary = S.detected?.primary;
    if (S.detected?.mode === "single" && primary && primary !== target && isLanguage(primary)) next = primary;
    else if (choices.length === 1 || (choices[0] && Number(choices[0].share) >= 0.7)) next = choices[0].code;
    else if (choices.length > 1) {
      openSwapPicker(choices);
      return;
    }
  }
  if (!next || next === target) {
    toast("원문 언어를 선택하세요", { type: "info" });
    return;
  }
  performSwap(next);
}

function openSwapPicker(choices) {
  if (isPopoverFor(el.swap)) {
    closePopover();
    return;
  }
  const menu = h(
    "div",
    { class: "menu", role: "menu" },
    h("div", { class: "menu-label", text: "새 번역 언어" }),
    choices.map((item) =>
      h(
        "button",
        {
          type: "button",
          class: "menu-item",
          role: "menuitem",
          onclick: () => {
            closePopover();
            performSwap(item.code);
          },
        },
        h("span", { text: langLabel(item.code) }),
        h("span", { class: "menu-hint", text: `${Math.round(Number(item.share) * 100)}%` }),
      ),
    ),
  );
  enableListNavigation(menu, ".menu-item");
  openPopover({ anchor: el.swap, content: menu, label: "새 번역 언어", focus: true, role: "presentation" });
}

function currentPairs() {
  const pairs = [];
  for (const node of el.out.childNodes) {
    const text = (node.nodeType === Node.TEXT_NODE ? node.textContent : node.innerText ?? node.textContent ?? "").trim();
    if (!text) continue;
    pairs.push({ source: text, target: node.nodeType === Node.ELEMENT_NODE ? paraSource.get(node) : undefined });
  }
  if (pairs.length && pairs.every((pair) => pair.target != null)) return pairs;
  return [{ source: pairs.map((pair) => pair.source).join("\n\n"), target: (S.result?.sourceText || "").trim() }];
}

function performSwap(next) {
  closePopover();
  const oldTarget = el.tgtLang.value;
  const hasOutput = S.phase === "done" && S.finals.length && outputText().trim();
  const pairs = hasOutput ? currentPairs() : null;
  setSelectValue(el.srcLang, oldTarget, "auto");
  setSelectValue(el.tgtLang, next);
  store.set(KEYS.src, el.srcLang.value);
  store.set(KEYS.tgt, el.tgtLang.value);
  setAutoOptionLabel(el.srcLang, AUTO_LABEL);
  refreshFormality();
  if (!pairs) {
    S.pinned = null;
    scheduleIfText(0);
    updateFoot();
    return;
  }
  stopSpeaking();
  el.src.value = pairs.map((pair) => pair.source).join("\n\n");
  updateCount();
  autoGrow();
  el.clear.hidden = !el.src.value;
  renderFinal(pairs);
  S.pinned = { key: optionsKey(requestOptions()), map: new Map(pairs.map((pair) => [pair.source.trim(), pair.target])) };
  S.prev = { key: "", map: new Map() };
  S.detected = { primary: oldTarget, mode: "single", languages: [{ code: oldTarget, char_count: el.src.value.length, share: 1 }] };
  S.hits = [];
  S.segments = pairs.map((pair) => ({ ...pair }));
  S.result = { sourceText: el.src.value, sourceLang: oldTarget, sourceSelect: oldTarget, targetLang: next, formality: el.formality.value };
  setPhase("done");
  updateFoot();
  if (!el.segPanel.hidden) renderSegments();
}

// ---------- output tools ----------

export async function copyResult() {
  const text = outputText();
  if (S.phase !== "done" || !text.trim()) return;
  if (await copyText(text)) {
    toast("복사했습니다");
    flashIcon(el.copy, "copy");
  } else {
    toast("복사하지 못했습니다", { type: "error" });
  }
}

function saveTxt() {
  const text = outputText();
  if (!text.trim()) return;
  downloadBlob(`번역_${el.tgtLang.value}_${stamp()}.txt`, new Blob([text.replace(/\n/g, "\r\n")], { type: "text/plain;charset=utf-8" }));
}

function stopSpeaking() {
  if (!speechSupported) return;
  if (S.speaking) window.speechSynthesis.cancel();
  S.speaking = false;
  setIcon(el.speak, "volume");
  el.speak.setAttribute("aria-pressed", "false");
  el.speak.dataset.tip = "듣기";
  el.speak.setAttribute("aria-label", "듣기");
}

function toggleSpeak() {
  if (!speechSupported) return;
  if (S.speaking) {
    stopSpeaking();
    return;
  }
  const text = outputText();
  if (!text.trim()) return;
  const lang = speechLang(el.tgtLang.value);
  const voices = window.speechSynthesis.getVoices();
  const base = lang.split("-")[0].toLowerCase();
  const voice =
    voices.find((item) => item.lang?.toLowerCase() === lang.toLowerCase()) ||
    voices.find((item) => item.lang?.toLowerCase().startsWith(base));
  if (voices.length && !voice) {
    toast(`${langLabel(el.tgtLang.value)} 음성이 없습니다`, { type: "info" });
    return;
  }
  const utterance = new window.SpeechSynthesisUtterance(text.slice(0, 4000));
  utterance.lang = lang;
  if (voice) utterance.voice = voice;
  utterance.onend = utterance.onerror = () => stopSpeaking();
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(utterance);
  S.speaking = true;
  setIcon(el.speak, "stop");
  el.speak.setAttribute("aria-pressed", "true");
  el.speak.dataset.tip = "중지";
  el.speak.setAttribute("aria-label", "중지");
}

function renderSegments() {
  const segments = S.phase === "done" ? S.segments || [] : [];
  el.segCount.textContent = segments.length ? `${fmtNum(segments.length)}개` : "";
  el.segList.replaceChildren(
    ...(segments.length
      ? segments.map((segment, index) =>
          h(
            "li",
            { class: "seg-row" },
            h("span", { class: "seg-no", text: String(index + 1) }),
            h("div", { class: "seg-src", text: segment.source }),
            h("div", { class: "seg-tgt", text: segment.target }),
          ),
        )
      : [h("li", { class: "seg-empty", text: "구간 없음" })]),
  );
}

function toggleSegments(force) {
  const open = typeof force === "boolean" ? force : el.segPanel.hidden;
  el.segPanel.hidden = !open;
  el.segBtn.setAttribute("aria-pressed", String(open));
  if (open) {
    renderSegments();
    el.segPanel.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

function openHits() {
  if (isPopoverFor(el.hits)) {
    closePopover();
    return;
  }
  const list = h(
    "ul",
    { class: "hit-list" },
    S.hits.map((entry) =>
      h(
        "li",
        { class: "hit-row" },
        h("span", { class: "hit-src", text: entry.source }),
        icon("arrowRight", { size: 14, cls: "muted-icon" }),
        h("span", { class: "hit-tgt", text: entry.target }),
        entry.note ? h("span", { class: "hit-note", text: entry.note }) : null,
      ),
    ),
  );
  openPopover({
    anchor: el.hits,
    align: "start",
    placement: "top",
    label: "적용된 용어",
    content: h(
      "div",
      { class: "hits-pop" },
      h(
        "div",
        { class: "pop-head" },
        h("span", { class: "pop-title", text: "적용된 용어" }),
        S.glossaryName ? h("span", { class: "pop-sub", text: S.glossaryName }) : null,
      ),
      list,
    ),
  });
}

// ---------- alternatives ----------

function closeAltPopover() {
  const pop = currentPopover();
  if (pop && pop.el.classList.contains("pop-alt")) closePopover();
}

function closestPara(node) {
  let current = node;
  while (current && current !== el.out) {
    if (current.nodeType === Node.ELEMENT_NODE && current.parentNode === el.out) return current;
    current = current.parentNode;
  }
  return null;
}

function rangeOffsets(root, range) {
  const before = document.createRange();
  before.selectNodeContents(root);
  before.setEnd(range.startContainer, range.startOffset);
  const start = before.toString().length;
  return [start, start + range.toString().length];
}

function makeRange(root, start, end) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let position = 0;
  let started = false;
  let node = walker.nextNode();
  while (node) {
    const length = node.textContent.length;
    if (!started && start <= position + length) {
      range.setStart(node, start - position);
      started = true;
    }
    if (started && end <= position + length) {
      range.setEnd(node, end - position);
      return range;
    }
    position += length;
    node = walker.nextNode();
  }
  return null;
}

const CLOSERS = "\"'”’)]」』";

function sentenceBounds(text, position) {
  let start = Math.min(position, text.length);
  while (start > 0) {
    const char = text[start - 1];
    if (char === "\n" || "。！？".includes(char)) break;
    if (/\s/.test(char)) {
      let k = start - 2;
      while (k >= 0 && CLOSERS.includes(text[k])) k -= 1;
      if (k >= 0 && ".!?…".includes(text[k])) break;
    }
    start -= 1;
  }
  let end = Math.min(position, text.length);
  while (end < text.length) {
    const char = text[end];
    if (char === "\n") break;
    end += 1;
    if ("。！？".includes(char) || (".!?…".includes(char) && (end >= text.length || /[\s"'”’)\]]/.test(text[end])))) {
      while (end < text.length && CLOSERS.includes(text[end])) end += 1;
      if (end >= text.length || /\s/.test(text[end]) || "。！？".includes(char)) break;
    }
  }
  while (start < end && /\s/.test(text[start])) start += 1;
  while (end > start && /\s/.test(text[end - 1])) end -= 1;
  return [start, end];
}

// Alternatives cost an LLM call, so they open only on an explicit action: a primary-button click on a sentence
// (not a caret move while the user is editing the output) or a real selection, once the pointer has settled.
function cancelPendingAlternatives() {
  clearTimeout(S.altTimer);
  S.altTimer = 0;
}

function scheduleAlternatives({ selectionOnly = false } = {}) {
  cancelPendingAlternatives();
  if (S.phase !== "done") return;
  S.altTimer = setTimeout(() => {
    S.altTimer = 0;
    openFromSelection(selectionOnly);
  }, ALT_DELAY_MS);
}

function onOutputMouseUp(event) {
  if (event.button !== 0) return;
  scheduleAlternatives();
}

function openFromSelection(selectionOnly) {
  if (S.phase !== "done") return;
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount) return;
  const range = selection.getRangeAt(0);
  if (!el.out.contains(range.commonAncestorContainer)) return;
  const para = closestPara(range.startContainer);
  if (!para || para !== closestPara(range.endContainer)) return;
  const text = para.textContent;
  if (!text.trim()) return;
  let [start, end] = rangeOffsets(para, range);
  if (start === end) {
    if (selectionOnly || S.editing) return;
    [start, end] = sentenceBounds(text, start);
  } else {
    while (start < end && /\s/.test(text[start])) start += 1;
    while (end > start && /\s/.test(text[end - 1])) end -= 1;
  }
  if (end <= start || end - start > 800) return;
  const span = text.slice(start, end);
  if (!/[\p{L}\p{N}]/u.test(span)) return;
  openAlternatives(para, start, end, span);
}

function snapStart(text, from, limit) {
  if (from <= 0) return 0;
  const line = text.indexOf("\n", from);
  if (line >= 0 && line < limit) return line + 1;
  const stop = Math.min(limit, from + 200);
  for (let index = from; index < stop; index += 1) if (/\s/.test(text[index])) return index + 1;
  return from;
}

function snapEnd(text, to, limit) {
  if (to >= text.length) return text.length;
  const line = text.lastIndexOf("\n", to - 1);
  if (line >= limit) return line;
  const stop = Math.max(limit, to - 200);
  for (let index = to - 1; index >= stop; index -= 1) if (/\s/.test(text[index])) return index;
  return to;
}

// Source/translation sent with an alternatives request: whole paragraphs when short, otherwise a window
// around the span (translation) and around the proportional position (source).
function alternativesContext(source, translation, start, end) {
  let windowSource = source;
  let windowTranslation = translation;
  if (translation.length > ALT_TRANSLATION_MAX) {
    const from = snapStart(translation, Math.max(0, start - ALT_CONTEXT), start);
    const to = snapEnd(translation, Math.min(translation.length, end + ALT_CONTEXT), end);
    windowTranslation = translation.slice(from, to);
  }
  if (source.length > ALT_SOURCE_MAX) {
    const center = Math.round(((start + end) / 2 / Math.max(1, translation.length)) * source.length);
    const to = Math.min(source.length, Math.max(0, center - ALT_SOURCE_MAX / 2) + ALT_SOURCE_MAX);
    const from = Math.max(0, to - ALT_SOURCE_MAX);
    const middle = Math.min(Math.max(center, from), to);
    windowSource = source.slice(snapStart(source, from, middle), snapEnd(source, to, middle));
  }
  return { source: windowSource, translation: windowTranslation };
}

function cachedAlternatives(key) {
  if (!altCache.has(key)) return null;
  const value = altCache.get(key);
  altCache.delete(key);
  altCache.set(key, value);
  return value;
}

function rememberAlternatives(key, value) {
  altCache.set(key, value);
  while (altCache.size > ALT_CACHE_MAX) altCache.delete(altCache.keys().next().value);
}

function applyAlternative(para, start, end, span, replacement) {
  const text = para.textContent;
  let from = start;
  let to = end;
  if (text.slice(from, to) !== span) {
    const index = text.indexOf(span);
    if (index < 0) {
      closePopover();
      return;
    }
    from = index;
    to = index + span.length;
  }
  para.textContent = text.slice(0, from) + replacement + text.slice(to);
  closePopover();
  const range = makeRange(para, from, from + replacement.length);
  if (range) {
    setHighlight("alt-applied", range);
    setTimeout(() => setHighlight("alt-applied", null), 1600);
  }
}

function openGlossaryForm(pop, span) {
  const sourceLang = resolvedSource() === "auto" ? "en" : resolvedSource();
  const targetLang = el.tgtLang.value;
  const source = h("input", { type: "text", class: "input", maxlength: "200", "aria-label": `원문 용어 (${langLabel(sourceLang)})` });
  const target = h("input", { type: "text", class: "input", maxlength: "200", "aria-label": `번역 용어 (${langLabel(targetLang)})` });
  target.value = span.slice(0, 200);
  const error = h("p", { class: "pop-error", hidden: true });
  const submit = h("button", { type: "submit", class: "btn btn-primary btn-sm" }, "추가");
  const form = h(
    "form",
    { class: "pop-form gl-add-form" },
    h(
      "div",
      { class: "pop-head" },
      h("span", { class: "pop-title", text: "용어집에 추가" }),
      h("span", { class: "pop-sub", text: activeSummary()?.name || "새 용어집" }),
    ),
    h("label", { class: "mini-field" }, h("span", { class: "mini-label", text: langLabel(sourceLang) }), source),
    h("label", { class: "mini-field" }, h("span", { class: "mini-label", text: langLabel(targetLang) }), target),
    error,
    h(
      "div",
      { class: "pop-actions" },
      h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => closePopover() }, "취소"),
      submit,
    ),
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const sourceText = source.value.trim();
    const targetText = target.value.trim();
    if (!sourceText) {
      source.focus();
      return;
    }
    if (!targetText) {
      target.focus();
      return;
    }
    submit.disabled = true;
    try {
      await addGlossaryEntry({ source_lang: sourceLang, target_lang: targetLang, source: sourceText, target: targetText, note: "", enabled: true });
      closePopover();
      const off = glossaryState.enabled ? "" : " (적용 꺼짐)";
      toast(`용어집에 추가했습니다${off}: ${sourceText} → ${targetText}`);
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      submit.disabled = false;
    }
  });
  pop.setContent(form);
  source.focus({ preventScroll: true });
}

function openAlternatives(para, start, end, span) {
  const range = makeRange(para, start, end);
  if (!range) return;
  S.altController?.abort();
  const controller = new AbortController();
  S.altController = controller;
  setHighlight("alt-target", range);
  const list = h("div", { class: "alt-list", role: "listbox", "aria-label": "대안" });
  const addButton = h("button", { type: "button", class: "btn btn-ghost btn-sm" }, icon("bookPlus", { size: 16 }), "용어집에 추가");
  const content = h(
    "div",
    { class: "alt-pop" },
    h("div", { class: "pop-head" }, h("span", { class: "pop-title", text: "대안" })),
    list,
    h("div", { class: "pop-foot" }, addButton),
  );
  const pop = openPopover({
    anchor: () => range.getBoundingClientRect(),
    content,
    className: "pop-alt",
    label: "대안 표현",
    owner: el.out,
    onClose: () => {
      controller.abort();
      setHighlight("alt-target", null);
    },
  });
  addButton.addEventListener("click", () => openGlossaryForm(pop, span));
  enableListNavigation(list, ".alt-item");
  const context = alternativesContext(paraSource.get(para) ?? S.result?.sourceText ?? "", para.textContent, start, end);
  const payload = {
    ...requestOptions(context.source),
    source_lang: resolvedSource(),
    source: context.source,
    translation: context.translation,
    span,
  };
  const cacheKey = JSON.stringify(payload);
  const load = async () => {
    list.replaceChildren(skeletonLines(3, { lastWidth: 70 }));
    pop.update();
    try {
      let data = cachedAlternatives(cacheKey);
      if (!data) {
        data = await api("/api/alternatives", { method: "POST", json: payload, signal: controller.signal });
        if (Array.isArray(data?.alternatives) && data.alternatives.length) rememberAlternatives(cacheKey, data);
      }
      if (currentPopover() !== pop) return;
      const seen = new Set([span.trim()]);
      const alternatives = (Array.isArray(data?.alternatives) ? data.alternatives : [])
        .map((item) => String(item ?? "").trim())
        .filter((item) => {
          if (!item || seen.has(item)) return false;
          seen.add(item);
          return true;
        })
        .slice(0, 3);
      list.replaceChildren(
        ...(alternatives.length
          ? alternatives.map((item) =>
              h(
                "button",
                { type: "button", class: "alt-item", role: "option", onclick: () => applyAlternative(para, start, end, span, item) },
                item,
              ),
            )
          : [h("p", { class: "pop-empty", text: "대안 없음" })]),
      );
      pop.update();
    } catch (error) {
      if (error?.name === "AbortError" || currentPopover() !== pop) return;
      list.replaceChildren(
        h(
          "div",
          { class: "pop-error-row" },
          h("span", { class: "pop-error", text: error.modelDown ? MODEL_DOWN_MESSAGE : error.message }),
          h("button", { type: "button", class: "btn btn-sm", onclick: load }, "다시 시도"),
        ),
      );
      pop.update();
    }
  };
  load();
}

// ---------- dictionary ----------

function contextAround(text, start, end) {
  const before = text.lastIndexOf("\n\n", start);
  const after = text.indexOf("\n\n", end);
  let from = before < 0 ? 0 : before + 2;
  let to = after < 0 ? text.length : after;
  if (to - from > 2000) {
    from = Math.max(from, start - 1000);
    to = Math.min(to, from + 2000);
  }
  return text.slice(from, to).trim();
}

function renderLookup(body, data) {
  const entries = Array.isArray(data?.entries) ? data.entries.filter((entry) => entry?.translation) : [];
  const examples = Array.isArray(data?.examples) ? data.examples.filter((example) => example?.source || example?.target) : [];
  if (!entries.length && !examples.length) {
    body.replaceChildren(h("p", { class: "pop-empty", text: "결과 없음" }));
    return;
  }
  body.replaceChildren(
    entries.length
      ? h(
          "ul",
          { class: "dict-entries" },
          entries.slice(0, 6).map((entry) =>
            h(
              "li",
              { class: "dict-entry" },
              h(
                "div",
                { class: "dict-line" },
                h("span", { class: "dict-tr", text: entry.translation }),
                entry.pos ? h("span", { class: "dict-pos", text: entry.pos }) : null,
              ),
              entry.note ? h("p", { class: "dict-note", text: entry.note }) : null,
            ),
          ),
        )
      : null,
    examples.length
      ? h(
          "div",
          { class: "dict-examples" },
          h("div", { class: "dict-label", text: "예문" }),
          examples.slice(0, 3).map((example) =>
            h(
              "div",
              { class: "dict-ex" },
              h("p", { class: "dict-ex-src", text: example.source || "" }),
              h("p", { class: "dict-ex-tgt", text: example.target || "" }),
            ),
          ),
        )
      : null,
  );
}

function onSourceDoubleClick(event) {
  const { selectionStart, selectionEnd, value } = el.src;
  if (selectionEnd <= selectionStart) return;
  const term = value
    .slice(selectionStart, selectionEnd)
    .replace(/^[\s"'“‘(\[{<«「『]+/u, "")
    .replace(/[\s"'”’)\]}>»」』.,;:!?。、，！？]+$/u, "");
  if (!term || term.length > 60 || term.includes("\n") || !/[\p{L}\p{N}]/u.test(term)) return;
  const context = contextAround(value, selectionStart, selectionEnd);
  const rect = { left: event.clientX, right: event.clientX, width: 0, top: event.clientY - 14, bottom: event.clientY + 14, height: 28 };
  S.lookupController?.abort();
  const controller = new AbortController();
  S.lookupController = controller;
  const body = h("div", { class: "dict-body" }, skeletonLines(3, { lastWidth: 55 }));
  const content = h(
    "div",
    { class: "dict-pop" },
    h("div", { class: "pop-head" }, h("span", { class: "dict-term", text: term }), h("span", { class: "ai-badge", text: "AI 사전" })),
    body,
  );
  const pop = openPopover({
    anchor: rect,
    content,
    className: "pop-dict",
    label: `${term} 사전`,
    owner: el.src,
    onClose: () => controller.abort(),
  });
  api("/api/lookup", {
    method: "POST",
    json: { term, context, source_lang: resolvedSource(), target_lang: el.tgtLang.value },
    signal: controller.signal,
  })
    .then((data) => {
      if (currentPopover() !== pop) return;
      renderLookup(body, data);
      pop.update();
    })
    .catch((error) => {
      if (error?.name === "AbortError" || currentPopover() !== pop) return;
      body.replaceChildren(h("p", { class: "pop-error", text: error.modelDown ? MODEL_DOWN_MESSAGE : error.message }));
      pop.update();
    });
}

// ---------- history restore ----------

export function restoreEntry(entry) {
  abortStream();
  clearTimeout(S.timer);
  closePopover();
  stopSpeaking();
  el.src.value = entry.source || "";
  updateCount();
  autoGrow();
  el.clear.hidden = !el.src.value;
  const sourceSelect = entry.source_select && (entry.source_select === "auto" || isLanguage(entry.source_select)) ? entry.source_select : "auto";
  setSelectValue(el.srcLang, sourceSelect, "auto");
  if (isLanguage(entry.target_lang)) setSelectValue(el.tgtLang, entry.target_lang);
  store.set(KEYS.src, el.srcLang.value);
  store.set(KEYS.tgt, el.tgtLang.value);
  refreshFormality(entry.formality);
  const pairs = splitPairs(entry.source || "", entry.translation || "");
  renderFinal(pairs);
  Object.assign(S, {
    segments: pairs.map((pair) => ({ ...pair })),
    hits: [],
    pinned: null,
    prev: { key: "", map: new Map() },
    current: null,
    detected: isLanguage(entry.source_lang) ? { primary: entry.source_lang, mode: "single", languages: [] } : null,
    result: {
      sourceText: entry.source,
      sourceLang: entry.source_lang,
      sourceSelect,
      targetLang: el.tgtLang.value,
      formality: el.formality.value,
    },
  });
  setPhase("done");
  updateDetectedLabel();
  updateFoot();
  if (!el.segPanel.hidden) renderSegments();
  el.src.focus({ preventScroll: true });
}

// ---------- init ----------

export function focusSource() {
  el.src?.focus({ preventScroll: true });
}

export function initText({ onFilesDropped }) {
  onFiles = onFilesDropped;
  Object.assign(el, {
    panel: $("#panel-text"),
    src: $("#srcText"),
    srcLang: $("#srcLang"),
    tgtLang: $("#tgtLang"),
    swap: $("#swapBtn"),
    formality: $("#formality"),
    glossWrap: $("#glossOpt"),
    glossSel: $("#glossSel"),
    glossToggle: $("#glossToggle"),
    ctxToggle: $("#ctxToggle"),
    ctxDot: $("#ctxToggle .opt-dot"),
    ctxPanel: $("#ctxPanel"),
    ctx: $("#ctxText"),
    rules: $("#rulesText"),
    ctxCount: $("#ctxCount"),
    rulesCount: $("#rulesCount"),
    clear: $("#clearBtn"),
    count: $("#srcCount"),
    out: $("#outText"),
    live: $("#outStatus"),
    placeholder: $("#outPlaceholder"),
    busy: $("#busyLine"),
    foot: $("#outFoot"),
    hits: $("#hitsChip"),
    identity: $("#identityChip"),
    segBtn: $("#segBtn"),
    speak: $("#speakBtn"),
    copy: $("#copyBtn"),
    save: $("#saveBtn"),
    segPanel: $("#segPanel"),
    segList: $("#segList"),
    segCount: $("#segCount"),
    segClose: $("#segClose"),
    drop: $("#textDrop"),
  });

  fillLangSelect(el.srcLang, { auto: true, value: store.get(KEYS.src, "auto") });
  fillLangSelect(el.tgtLang, { value: store.get(KEYS.tgt, "ko") });
  refreshFormality();
  el.rules.value = String(store.get(KEYS.rules, "") || "").slice(0, RULES_MAX);
  updateCtxIndicators();
  renderGlossaryControls();
  setDirectionProvider(() => ({ source: resolvedSource() === "auto" ? "en" : resolvedSource(), target: el.tgtLang.value }));

  el.src.addEventListener("input", onSourceInput);
  el.src.addEventListener("dblclick", onSourceDoubleClick);
  el.srcLang.addEventListener("change", () => {
    store.set(KEYS.src, el.srcLang.value);
    S.pinned = null;
    updateDetectedLabel();
    scheduleIfText(0);
  });
  el.tgtLang.addEventListener("change", () => {
    store.set(KEYS.tgt, el.tgtLang.value);
    refreshFormality();
    S.pinned = null;
    stopSpeaking();
    scheduleIfText(0);
  });
  el.formality.addEventListener("change", () => {
    saveFormality();
    scheduleIfText(0);
  });
  el.glossSel.addEventListener("change", () => setActiveGlossary(el.glossSel.value || null));
  el.glossToggle.addEventListener("change", () => setGlossaryEnabled(el.glossToggle.checked));
  onGlossaryChange(({ reason, entry }) => {
    renderGlossaryControls();
    if (reason === "draft" || reason === "load") return;
    // A term added from the output only re-translates when it applies to the current text (keeps manual edits otherwise).
    if (reason === "entries" && entry) {
      const applies =
        glossaryState.enabled &&
        Boolean(glossaryState.activeId) &&
        entry.target_lang === el.tgtLang.value &&
        el.src.value.toLowerCase().includes(String(entry.source || "").toLowerCase());
      if (!applies) return;
    }
    scheduleIfText(0);
  });
  el.ctxToggle.addEventListener("click", () => toggleCtxPanel());
  el.ctx.addEventListener("input", () => {
    updateCtxIndicators();
    scheduleIfText(800);
  });
  el.rules.addEventListener("input", () => {
    updateCtxIndicators();
    store.set(KEYS.rules, el.rules.value);
    scheduleIfText(800);
  });
  el.clear.addEventListener("click", () => {
    el.src.value = "";
    onSourceInput();
    el.src.focus();
  });
  el.swap.addEventListener("click", swapLanguages);
  el.copy.addEventListener("click", copyResult);
  el.save.addEventListener("click", saveTxt);
  el.speak.addEventListener("click", toggleSpeak);
  el.segBtn.addEventListener("click", () => toggleSegments());
  el.segClose.addEventListener("click", () => toggleSegments(false));
  el.hits.addEventListener("click", openHits);
  el.identity.addEventListener("click", () => {
    const lang = el.identity.dataset.lang;
    if (!isLanguage(lang)) return;
    setSelectValue(el.tgtLang, lang);
    store.set(KEYS.tgt, lang);
    refreshFormality();
    scheduleIfText(0);
  });

  el.out.addEventListener("pointerdown", cancelPendingAlternatives);
  el.out.addEventListener("mouseup", onOutputMouseUp);
  el.out.addEventListener("keyup", (event) => {
    if (event.shiftKey && event.key.startsWith("Arrow")) scheduleAlternatives({ selectionOnly: true });
  });
  el.out.addEventListener("keydown", (event) => {
    if (event.altKey && event.key === "ArrowDown") {
      const first = currentPopover()?.el.querySelector(".alt-item");
      if (first) {
        event.preventDefault();
        first.focus();
      }
    }
  });
  el.out.addEventListener("input", () => {
    S.editing = true;
    cancelPendingAlternatives();
    closeAltPopover();
  });
  el.out.addEventListener("focusout", (event) => {
    if (!el.out.contains(event.relatedTarget)) S.editing = false;
  });
  el.out.addEventListener("paste", (event) => {
    if (el.out.contentEditable !== "true") return;
    event.preventDefault();
    document.execCommand("insertText", false, event.clipboardData?.getData("text/plain") || "");
  });

  let dragDepth = 0;
  const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
  el.panel.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    el.drop.hidden = false;
  });
  el.panel.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  el.panel.addEventListener("dragleave", (event) => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) el.drop.hidden = true;
  });
  el.panel.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    el.drop.hidden = true;
    const files = [...(event.dataTransfer.files || [])];
    if (files.length) onFiles(files);
  });

  window.addEventListener("resize", () => autoGrow());
  onStatus(() => updateCount());
  updateCount();
  autoGrow();
  resetOutput();
}
