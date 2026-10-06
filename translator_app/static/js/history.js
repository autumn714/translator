import { h, store, uid, fmtWhen, showModal, closeModal, confirmAction, toast } from "./dom.js";
import { icon } from "./icons.js";
import { langLabel } from "./langs.js";

const KEY_ITEMS = "translator.history";
const KEY_ENABLED = "translator.historyEnabled";
const MAX_ITEMS = 50;
const MERGE_WINDOW_MS = 5 * 60 * 1000;

let items = Array.isArray(store.get(KEY_ITEMS, [])) ? store.get(KEY_ITEMS, []) : [];
let enabled = store.get(KEY_ENABLED, false) === true;
let restoreHandler = () => {};
let drawer = null;
const els = {};

export const setRestoreHandler = (fn) => {
  restoreHandler = fn;
};

function save() {
  while (items.length && !store.set(KEY_ITEMS, items)) items.pop();
  if (!items.length) store.remove(KEY_ITEMS);
}

// The newest record is replaced only by an edit of the same draft (typing on, deleting at the end, or a
// change in the middle that keeps the shared start and end at 80 % or more of the shorter text),
// not by a different text that merely starts the same way.
const MERGE_SHARED = 0.8;

function sharedEnds(a, b) {
  const limit = Math.min(a.length, b.length);
  let prefix = 0;
  while (prefix < limit && a[prefix] === b[prefix]) prefix += 1;
  let suffix = 0;
  while (suffix < limit - prefix && a[a.length - 1 - suffix] === b[b.length - 1 - suffix]) suffix += 1;
  return prefix + suffix;
}

export function sameDraft(previous, next, now = Date.now()) {
  if (!previous || previous.target_lang !== next.target_lang) return false;
  if (now - previous.ts > MERGE_WINDOW_MS) return false;
  const a = previous.source;
  const b = next.source;
  if (b.startsWith(a) || a.startsWith(b)) return true;
  return sharedEnds(a, b) >= MERGE_SHARED * Math.min(a.length, b.length);
}

export function recordTranslation(entry) {
  if (!enabled) return;
  const source = (entry.source || "").trim();
  const translation = (entry.translation || "").trim();
  if (!source || !translation) return;
  const next = { ...entry, source, translation };
  if (sameDraft(items[0], next)) items.shift();
  items = items.filter((item) => !(item.source === source && item.target_lang === next.target_lang));
  items.unshift({ id: uid(), ts: Date.now(), ...next });
  items = items.slice(0, MAX_ITEMS);
  save();
  if (drawer?.open) renderList();
}

function renderList() {
  const list = els.list;
  els.clear.disabled = !items.length;
  if (!items.length) {
    list.replaceChildren(
      h(
        "li",
        { class: "hist-empty" },
        icon("history", { size: 26 }),
        h("span", { text: enabled ? "기록 없음" : "기록 저장 꺼짐" }),
      ),
    );
    return;
  }
  list.replaceChildren(
    ...items.map((item) =>
      h(
        "li",
        { class: "hist-row" },
        h(
          "button",
          {
            type: "button",
            class: "hist-item",
            onclick: () => {
              closeModal(drawer);
              restoreHandler(item);
            },
          },
          h(
            "span",
            { class: "hist-top" },
            h("span", { class: "hist-langs", text: `${langLabel(item.source_lang || "auto")} → ${langLabel(item.target_lang)}` }),
            h("span", { class: "hist-time", text: fmtWhen(item.ts) }),
          ),
          h("span", { class: "hist-src", text: item.source }),
          h("span", { class: "hist-tgt", text: item.translation }),
        ),
        h(
          "button",
          {
            type: "button",
            class: "icon-btn icon-btn-sm hist-del",
            "aria-label": "이 기록 삭제",
            onclick: () => {
              items = items.filter((other) => other.id !== item.id);
              save();
              renderList();
            },
          },
          icon("x", { size: 16 }),
        ),
      ),
    ),
  );
}

function build() {
  els.toggle = h("input", { type: "checkbox", role: "switch", id: "histToggle" });
  els.toggle.checked = enabled;
  els.list = h("ul", { class: "hist-list" });
  els.clear = h("button", { type: "button", class: "btn btn-ghost btn-sm icon-danger" }, icon("trash", { size: 16 }), "모두 지우기");
  const closeButton = h("button", { type: "button", class: "icon-btn", "aria-label": "닫기" }, icon("x"));
  drawer = h(
    "dialog",
    { class: "drawer", "aria-labelledby": "histTitle" },
    h("div", { class: "modal-head" }, h("h2", { class: "modal-title", id: "histTitle", text: "번역 기록" }), closeButton),
    h(
      "div",
      { class: "hist-bar" },
      h(
        "label",
        { class: "switch", title: "이 브라우저에만 저장됩니다" },
        els.toggle,
        h("span", { class: "switch-ui", "aria-hidden": "true" }),
        h("span", { class: "switch-text", text: "기록 저장" }),
      ),
      els.clear,
    ),
    h("div", { class: "drawer-body" }, els.list),
  );
  closeButton.addEventListener("click", () => closeModal(drawer));
  drawer.addEventListener("click", (event) => {
    if (event.target === drawer) closeModal(drawer);
  });
  els.toggle.addEventListener("change", () => {
    enabled = els.toggle.checked;
    store.set(KEY_ENABLED, enabled);
    renderList();
  });
  els.clear.addEventListener("click", async () => {
    const ok = await confirmAction({ title: "기록 모두 지우기", message: `${items.length}개 기록이 삭제됩니다.`, confirmLabel: "지우기", danger: true });
    if (!ok) return;
    items = [];
    save();
    renderList();
    toast("기록을 지웠습니다");
  });
  document.body.append(drawer);
}

export function openHistory() {
  if (!drawer) build();
  els.toggle.checked = enabled;
  renderList();
  showModal(drawer);
}
