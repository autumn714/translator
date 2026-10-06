import { api, MODEL_DOWN_MESSAGE } from "./api.js";
import { $, h, store, fmtNum, copyText, toast, skeletonLines } from "./dom.js";
import { icon, setIcon } from "./icons.js";
import { AUTO_LABEL, fillLangSelect, isLanguage, langLabel, setAutoOptionLabel } from "./langs.js";
import { diffWords, countChanges } from "./diff.js";

const KEY = "translator.write";
const MAX_CHARS = 10000;
// Style changes re-run a shown result only after the user settles on a style (arrow keys/chip clicks in a row).
const STYLE_DEBOUNCE_MS = 600;
const STYLES = [
  ["polish", "다듬기"],
  ["formal", "격식"],
  ["concise", "간결"],
  ["plain", "쉽게"],
  ["friendly", "친근"],
  ["gaejoshik", "개조식"],
  ["academic", "학술"],
  ["business", "비즈니스"],
];

const el = {};
const W = {
  style: "polish",
  showDiff: true,
  controller: null,
  styleTimer: 0,
  busy: false,
  before: "",
  after: "",
  hasResult: false,
};

function saveSettings() {
  store.set(KEY, { lang: el.lang.value, style: W.style, showDiff: W.showDiff });
}

function updateCount() {
  const length = el.input.value.length;
  el.count.textContent = `${fmtNum(length)} / ${fmtNum(MAX_CHARS)}`;
  el.count.classList.toggle("is-over", length > MAX_CHARS);
  el.clear.hidden = !el.input.value;
  el.run.disabled = !el.input.value.trim() || length > MAX_CHARS;
}

function autoGrow() {
  el.input.style.height = "auto";
  el.input.style.height = `${el.input.scrollHeight}px`;
}

function setBusy(busy) {
  W.busy = busy;
  el.busy.hidden = !busy;
  el.run.classList.toggle("is-loading", busy);
  el.out.setAttribute("aria-busy", String(busy));
}

function renderStyles() {
  el.styles.replaceChildren(
    ...STYLES.map(([value, label]) =>
      h(
        "button",
        {
          type: "button",
          class: "chip",
          role: "radio",
          "aria-checked": String(value === W.style),
          tabindex: value === W.style ? "0" : "-1",
          "data-value": value,
        },
        label,
      ),
    ),
  );
}

function renderResult() {
  const after = W.after;
  if (!W.hasResult) {
    el.out.replaceChildren();
    el.placeholder.hidden = false;
    el.foot.hidden = true;
    return;
  }
  el.placeholder.hidden = Boolean(after);
  el.foot.hidden = false;
  const parts = diffWords(W.before, after);
  const changes = countChanges(parts);
  el.changes.textContent = changes ? `${fmtNum(changes)}곳 변경` : "변경 없음";
  if (!W.showDiff) {
    el.out.replaceChildren(document.createTextNode(after));
    return;
  }
  const nodes = [];
  for (const part of parts) {
    if (part.type === "same") {
      nodes.push(document.createTextNode(part.text));
      continue;
    }
    const removed = part.removed;
    const added = part.added;
    if (removed.trim()) nodes.push(h("del", { class: "d-del" }, removed));
    if (added.trim()) nodes.push(h("ins", { class: "d-ins" }, added));
    else if (added) nodes.push(document.createTextNode(added));
  }
  el.out.replaceChildren(...nodes);
}

function showError(message) {
  el.placeholder.hidden = true;
  el.foot.hidden = true;
  el.out.replaceChildren(
    h(
      "div",
      { class: "out-error", role: "alert" },
      h("div", { class: "out-error-msg" }, icon("alert", { size: 18 }), h("span", { text: message })),
      h("button", { type: "button", class: "btn btn-sm", onclick: () => runRewrite() }, icon("refresh", { size: 16 }), "다시 시도"),
    ),
  );
}

// Aborting the fetch also cancels the request on the server (it stops when the client disconnects).
function cancelRewrite() {
  clearTimeout(W.styleTimer);
  W.styleTimer = 0;
  W.controller?.abort();
  W.controller = null;
  setBusy(false);
}

function scheduleStyleRewrite() {
  clearTimeout(W.styleTimer);
  W.styleTimer = 0;
  const inFlight = Boolean(W.controller);
  if (!(W.hasResult || inFlight) || !el.input.value.trim()) return;
  if (inFlight) {
    W.controller.abort();
    W.controller = null;
  }
  if (W.hasResult) el.out.classList.add("is-stale");
  W.styleTimer = setTimeout(() => {
    W.styleTimer = 0;
    runRewrite();
  }, STYLE_DEBOUNCE_MS);
}

export async function runRewrite() {
  clearTimeout(W.styleTimer);
  W.styleTimer = 0;
  const text = el.input.value;
  if (!text.trim()) return;
  if (text.length > MAX_CHARS) {
    showError(`최대 ${fmtNum(MAX_CHARS)}자까지 다듬을 수 있습니다.`);
    return;
  }
  W.controller?.abort();
  const controller = new AbortController();
  W.controller = controller;
  setBusy(true);
  el.out.classList.remove("is-stale");
  el.placeholder.hidden = true;
  if (!W.hasResult) el.out.replaceChildren(skeletonLines(Math.max(2, Math.min(8, Math.ceil(text.length / 80))), { lastWidth: 55 }));
  else el.out.classList.add("is-stale");
  try {
    const data = await api("/api/rewrite", {
      method: "POST",
      json: { text, lang: el.lang.value, style: W.style, context: "" },
      signal: controller.signal,
    });
    if (W.controller !== controller) return;
    W.before = text;
    W.after = typeof data?.text === "string" ? data.text : "";
    W.hasResult = true;
    if (el.lang.value === "auto" && isLanguage(data?.detected_lang)) setAutoOptionLabel(el.lang, `${langLabel(data.detected_lang)} (감지됨)`);
    el.out.classList.remove("is-stale");
    renderResult();
  } catch (error) {
    if (error?.name === "AbortError" || W.controller !== controller) return;
    W.hasResult = false;
    el.out.classList.remove("is-stale");
    showError(error.modelDown ? MODEL_DOWN_MESSAGE : error.message || "다듬지 못했습니다.");
  } finally {
    if (W.controller === controller) {
      W.controller = null;
      setBusy(false);
    }
  }
}

async function copyOutput() {
  if (!W.after) return;
  if (await copyText(W.after)) {
    toast("복사했습니다");
    setIcon(el.copy, "check");
    setTimeout(() => setIcon(el.copy, "copy"), 1400);
  } else {
    toast("복사하지 못했습니다", { type: "error" });
  }
}

function applyToInput() {
  if (!W.hasResult) return;
  el.input.value = W.after;
  W.before = W.after;
  updateCount();
  autoGrow();
  renderResult();
  el.out.classList.remove("is-stale");
  toast("입력에 적용했습니다");
}

export function initWrite() {
  Object.assign(el, {
    lang: $("#wLang"),
    styles: $("#wStyles"),
    input: $("#wInput"),
    clear: $("#wClear"),
    count: $("#wCount"),
    run: $("#wRun"),
    out: $("#wOut"),
    placeholder: $("#wPlaceholder"),
    busy: $("#wBusy"),
    foot: $("#wFoot"),
    showDiff: $("#wShowDiff"),
    changes: $("#wChanges"),
    copy: $("#wCopy"),
    apply: $("#wApply"),
  });
  const saved = store.get(KEY, {}) || {};
  fillLangSelect(el.lang, { auto: true, value: saved.lang || "auto" });
  W.style = STYLES.some(([value]) => value === saved.style) ? saved.style : "polish";
  W.showDiff = saved.showDiff !== false;
  el.showDiff.checked = W.showDiff;
  renderStyles();

  el.styles.addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) return;
    if (chip.dataset.value === W.style) return;
    W.style = chip.dataset.value;
    renderStyles();
    el.styles.querySelector(`[data-value="${W.style}"]`)?.focus();
    saveSettings();
    scheduleStyleRewrite();
  });
  el.styles.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const index = STYLES.findIndex(([value]) => value === W.style);
    const next = STYLES[(index + (event.key === "ArrowRight" ? 1 : STYLES.length - 1)) % STYLES.length][0];
    el.styles.querySelector(`[data-value="${next}"]`)?.click();
  });
  el.lang.addEventListener("change", () => {
    setAutoOptionLabel(el.lang, AUTO_LABEL);
    saveSettings();
  });
  el.input.addEventListener("input", () => {
    updateCount();
    autoGrow();
    if (W.hasResult) el.out.classList.toggle("is-stale", el.input.value !== W.before);
    if (!el.input.value.trim()) {
      cancelRewrite();
      W.hasResult = false;
      W.before = W.after = "";
      setAutoOptionLabel(el.lang, AUTO_LABEL);
      el.out.classList.remove("is-stale");
      renderResult();
    }
  });
  el.clear.addEventListener("click", () => {
    el.input.value = "";
    el.input.dispatchEvent(new Event("input"));
    el.input.focus();
  });
  el.run.addEventListener("click", runRewrite);
  el.showDiff.addEventListener("change", () => {
    W.showDiff = el.showDiff.checked;
    saveSettings();
    renderResult();
  });
  el.copy.addEventListener("click", copyOutput);
  el.apply.addEventListener("click", applyToInput);
  window.addEventListener("resize", autoGrow);
  updateCount();
  renderResult();
}
