import { $, $$, h, store, isTypingTarget, topDialog } from "./js/dom.js";
import { hydrateIcons } from "./js/icons.js";
import { openPopover, closePopover, isPopoverFor } from "./js/popover.js";
import { initStatus } from "./js/status.js";
import { loadGlossaries, openGlossaryManager } from "./js/glossary.js";
import { openHistory, setRestoreHandler } from "./js/history.js";
import { initText, translateNow, copyResult, swapLanguages, restoreEntry, focusSource } from "./js/text.js";
import { initDocs, addFiles } from "./js/docs.js";
import { initWrite, runRewrite } from "./js/write.js";

const TABS = ["text", "docs", "write"];
const KEY_TAB = "translator.tab";
let activeTab = "text";

function switchTab(name, { focus = false, updateHash = true } = {}) {
  if (!TABS.includes(name)) name = "text";
  activeTab = name;
  for (const tab of TABS) {
    const button = $(`#tab-${tab}`);
    const panel = $(`#panel-${tab}`);
    const on = tab === name;
    button.setAttribute("aria-selected", String(on));
    button.tabIndex = on ? 0 : -1;
    panel.hidden = !on;
  }
  document.body.dataset.tab = name;
  store.set(KEY_TAB, name);
  if (updateHash && window.location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  closePopover();
  if (focus) $(`#tab-${name}`).focus();
}

function initTabs() {
  const list = $(".tabs");
  for (const tab of TABS) $(`#tab-${tab}`).addEventListener("click", () => switchTab(tab));
  list.addEventListener("keydown", (event) => {
    const index = TABS.indexOf(activeTab);
    let next = null;
    if (event.key === "ArrowRight") next = TABS[(index + 1) % TABS.length];
    else if (event.key === "ArrowLeft") next = TABS[(index - 1 + TABS.length) % TABS.length];
    else if (event.key === "Home") next = TABS[0];
    else if (event.key === "End") next = TABS[TABS.length - 1];
    if (!next) return;
    event.preventDefault();
    switchTab(next, { focus: true });
  });
  window.addEventListener("hashchange", () => {
    const name = window.location.hash.slice(1);
    if (TABS.includes(name) && name !== activeTab) switchTab(name, { updateHash: false });
  });
  const fromHash = window.location.hash.slice(1);
  const initial = TABS.includes(fromHash) ? fromHash : store.get(KEY_TAB, "text");
  switchTab(TABS.includes(initial) ? initial : "text");
}

const SHORTCUTS = [
  [["Ctrl", "Enter"], "바로 번역 · 다듬기"],
  [["Ctrl", "Shift", "C"], "번역 결과 복사"],
  [["Ctrl", "Shift", "S"], "언어 바꾸기"],
  [["더블클릭"], "원문 단어 사전"],
  [["클릭"], "번역문 문장 대안"],
  [["Esc"], "닫기"],
];

function toggleShortcuts() {
  const anchor = $(".topbar-actions");
  if (isPopoverFor(anchor)) {
    closePopover();
    return;
  }
  const rows = SHORTCUTS.map(([keys, label]) =>
    h(
      "div",
      { class: "kbd-row" },
      h("span", { class: "kbd-label", text: label }),
      h("span", { class: "kbd-keys" }, keys.map((key, index) => [index ? h("span", { class: "kbd-plus", text: "+" }) : null, h("kbd", { text: key })])),
    ),
  );
  openPopover({
    anchor,
    align: "end",
    label: "단축키",
    className: "pop-kbd",
    content: h("div", {}, h("div", { class: "pop-head" }, h("span", { class: "pop-title", text: "단축키" })), rows),
  });
}

function initShortcuts() {
  document.addEventListener("keydown", (event) => {
    const mod = event.ctrlKey || event.metaKey;
    const key = event.key.toLowerCase();
    const inDialog = Boolean(topDialog());
    if (mod && event.key === "Enter" && !inDialog) {
      event.preventDefault();
      if (activeTab === "text") translateNow();
      else if (activeTab === "write") runRewrite();
      return;
    }
    if (mod && event.shiftKey && key === "c" && activeTab === "text" && !inDialog) {
      event.preventDefault();
      copyResult();
      return;
    }
    if (mod && event.shiftKey && key === "s" && activeTab === "text" && !inDialog) {
      event.preventDefault();
      swapLanguages();
      return;
    }
    if (event.key === "?" && !mod && !event.altKey && !inDialog && !isTypingTarget(event.target)) {
      event.preventDefault();
      toggleShortcuts();
    }
  });
}

function preventStrayDrops() {
  const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
  window.addEventListener("dragover", (event) => {
    if (hasFiles(event)) event.preventDefault();
  });
  window.addEventListener("drop", (event) => {
    if (hasFiles(event)) event.preventDefault();
  });
}

function init() {
  hydrateIcons();
  initStatus();
  initTabs();
  initText({
    onFilesDropped: (files) => {
      switchTab("docs");
      addFiles(files);
    },
  });
  initDocs();
  initWrite();
  setRestoreHandler((entry) => {
    switchTab("text");
    restoreEntry(entry);
  });
  $("#historyBtn").addEventListener("click", openHistory);
  $("#glossaryBtn").addEventListener("click", openGlossaryManager);
  $$(".brand").forEach((brand) =>
    brand.addEventListener("click", (event) => {
      event.preventDefault();
      switchTab("text");
      focusSource();
    }),
  );
  initShortcuts();
  preventStrayDrops();
  loadGlossaries();
  if (activeTab === "text" && window.matchMedia("(pointer: fine)").matches) focusSource();
  document.documentElement.classList.add("is-ready");
}

init();
