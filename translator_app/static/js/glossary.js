import { api } from "./api.js";
import { $, h, store, fmtNum, toast, showModal, closeModal, choose, confirmAction, downloadUrl } from "./dom.js";
import { icon } from "./icons.js";
import { LANGUAGES, langLabel, fillLangSelect } from "./langs.js";
import { openPopover, closePopover, isPopoverFor, enableListNavigation } from "./popover.js";

const KEY_ID = "translator.glossaryId";
const KEY_USE = "translator.useGlossary";

const bus = new EventTarget();
const state = {
  list: [],
  defaultId: null,
  activeId: store.get(KEY_ID, null),
  enabled: store.get(KEY_USE, true) !== false,
  docs: new Map(),
  loaded: false,
};
let directionProvider = () => ({ source: "en", target: "ko" });

export const glossaryState = state;
export const setDirectionProvider = (fn) => {
  directionProvider = fn;
};

export function onGlossaryChange(listener) {
  bus.addEventListener("change", (event) => listener(event.detail));
}

function emit(reason, extra = {}) {
  bus.dispatchEvent(new CustomEvent("change", { detail: { reason, ...extra } }));
}

function persist() {
  store.set(KEY_ID, state.activeId);
  store.set(KEY_USE, state.enabled);
}

export function activeSummary() {
  return state.list.find((item) => item.id === state.activeId) || null;
}

let loading = null;
let retryTimer = 0;
let retryDelay = 2000;

// A failed load keeps the saved choice (nothing is persisted) and is retried with backoff.
export function loadGlossaries() {
  if (loading) return loading;
  clearTimeout(retryTimer);
  loading = (async () => {
    let data;
    try {
      data = await api("/api/glossaries");
    } catch {
      retryTimer = setTimeout(loadGlossaries, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 60000);
      return;
    } finally {
      loading = null;
    }
    retryDelay = 2000;
    state.list = Array.isArray(data?.glossaries) ? data.glossaries : [];
    state.defaultId = data?.default_glossary_id || null;
    const ids = new Set(state.list.map((item) => item.id));
    if (!ids.has(state.activeId)) {
      state.activeId = ids.has(state.defaultId) ? state.defaultId : state.list[0]?.id ?? null;
    }
    state.loaded = true;
    persist();
    emit("load");
  })();
  return loading;
}

export function setActiveGlossary(id) {
  if (state.activeId === id) return;
  state.activeId = id || null;
  persist();
  emit("active");
}

export function setGlossaryEnabled(enabled) {
  state.enabled = Boolean(enabled);
  persist();
  emit("enabled");
}

export async function fetchGlossary(id, { fresh = false } = {}) {
  if (!fresh && state.docs.has(id)) return state.docs.get(id);
  const data = await api(`/api/glossaries/${encodeURIComponent(id)}`);
  state.docs.set(id, data.glossary);
  return data.glossary;
}

function rememberDoc(doc) {
  state.docs.set(doc.id, doc);
  const summary = state.list.find((item) => item.id === doc.id);
  if (summary) {
    summary.name = doc.name;
    summary.entry_count = doc.entries.length;
  } else {
    state.list.push({ id: doc.id, name: doc.name, entry_count: doc.entries.length });
  }
}

export async function addGlossaryEntry(entry) {
  if (!state.activeId) {
    const created = await api("/api/glossaries", { method: "POST", json: { name: "기본 용어집", entries: [entry] } });
    rememberDoc(created.glossary);
    state.activeId = created.glossary.id;
    state.enabled = true;
    persist();
    emit("entries", { entry });
    return created.glossary;
  }
  const doc = await fetchGlossary(state.activeId, { fresh: true });
  const saved = await api(`/api/glossaries/${encodeURIComponent(doc.id)}`, {
    method: "PUT",
    json: { name: doc.name, entries: [...doc.entries, entry] },
  });
  rememberDoc(saved.glossary);
  if (manager.editingId === doc.id && !manager.dirty) renderRows(saved.glossary.entries);
  emit("entries", { entry });
  return saved.glossary;
}

// Unsaved edits of the active glossary (sent with translation requests while the editor is dirty).
// With a text, only entries that can match it are sent (a superset of the server's rule: case-insensitive
// substring with whitespace runs collapsed on both sides).
const squash = (value) => value.replace(/\s+/g, " ").toLowerCase();

export function draftEntries(text) {
  if (!manager.dirty || manager.editingId !== state.activeId) return null;
  const entries = readRows();
  if (typeof text !== "string") return entries;
  const haystack = squash(text);
  return entries.filter((entry) => entry.enabled && haystack.includes(squash(entry.source)));
}

// ---------- Bindings for selects in the text/document tabs ----------

export function renderGlossaryOptions(select, { value, noneLabel = null } = {}) {
  const options = [];
  if (noneLabel) options.push(h("option", { value: "" }, noneLabel));
  for (const item of state.list) {
    options.push(h("option", { value: item.id }, `${item.name} (${fmtNum(item.entry_count)})`));
  }
  if (!options.length) options.push(h("option", { value: "" }, "용어집 없음"));
  select.replaceChildren(...options);
  const ids = state.list.map((item) => item.id);
  select.value = ids.includes(value) ? value : noneLabel ? "" : ids[0] ?? "";
  select.disabled = !state.list.length && !noneLabel;
}

// ---------- Manager dialog ----------

// The editor keeps entries in `rows` (the source of truth) and renders only a page of the filtered view,
// so glossaries with thousands of entries open instantly.
const PAGE_ROWS = 100;
const manager = {
  dialog: null,
  editingId: null,
  doc: null,
  dirty: false,
  langTemplate: null,
  rows: [],
  view: [],
  limit: PAGE_ROWS,
  invalid: new Set(),
  els: {},
};

const knownLang = (code) => (LANGUAGES.some((language) => language.code === code) ? code : LANGUAGES[0].code);

function toRecord(entry = {}) {
  const direction = directionProvider();
  return {
    enabled: entry.enabled ?? true,
    source_lang: knownLang(entry.source_lang ?? direction.source),
    target_lang: knownLang(entry.target_lang ?? direction.target),
    source: entry.source ?? "",
    target: entry.target ?? "",
    note: entry.note ?? "",
  };
}

const isPartial = (record) => Boolean(record.source.trim()) !== Boolean(record.target.trim());

function langSelect(value, label) {
  if (!manager.langTemplate) {
    manager.langTemplate = document.createElement("select");
    fillLangSelect(manager.langTemplate, {});
    manager.langTemplate.className = "select select-cell";
  }
  const select = manager.langTemplate.cloneNode(true);
  select.setAttribute("aria-label", label);
  select.value = LANGUAGES.some((language) => language.code === value) ? value : LANGUAGES[0].code;
  return select;
}

function makeRow(record) {
  const enabled = h("input", { type: "checkbox", "aria-label": "사용" });
  enabled.checked = record.enabled;
  const source = h("input", { type: "text", class: "cell-input", maxlength: "200", "aria-label": "원문 용어", placeholder: "원문 용어", "data-field": "source" });
  source.value = record.source;
  const target = h("input", { type: "text", class: "cell-input", maxlength: "200", "aria-label": "번역 용어", placeholder: "번역 용어", "data-field": "target" });
  target.value = record.target;
  const note = h("input", { type: "text", class: "cell-input", maxlength: "300", "aria-label": "메모", placeholder: "메모", "data-field": "note" });
  note.value = record.note;
  const remove = h(
    "button",
    { type: "button", class: "icon-btn icon-btn-sm row-del", "aria-label": "행 삭제", "data-action": "remove" },
    icon("trash", { size: 16 }),
  );
  const row = h(
    "tr",
    {},
    h("td", { class: "c-use" }, enabled),
    h("td", { class: "c-lang" }, langSelect(record.source_lang, "원문 언어")),
    h("td", { class: "c-lang" }, langSelect(record.target_lang, "번역 언어")),
    h("td", {}, source),
    h("td", {}, target),
    h("td", {}, note),
    h("td", { class: "c-del" }, remove),
  );
  row._fields = { enabled, source, target, note, sourceLang: row.children[1].firstChild, targetLang: row.children[2].firstChild };
  row._record = record;
  markRow(row);
  return row;
}

function markRow(row) {
  const record = row._record;
  const invalid = manager.invalid.has(record);
  row.classList.toggle("is-invalid", invalid);
  row._fields.source.toggleAttribute("aria-invalid", invalid && !record.source.trim());
  row._fields.target.toggleAttribute("aria-invalid", invalid && !record.target.trim());
}

// Copies a row's inputs into its record.
function syncRecord(row) {
  const { enabled, source, target, note, sourceLang, targetLang } = row._fields;
  Object.assign(row._record, {
    enabled: enabled.checked,
    source_lang: sourceLang.value,
    target_lang: targetLang.value,
    source: source.value,
    target: target.value,
    note: note.value,
  });
}

// Complete entries only; rows with both terms empty are ignored (half-filled rows are caught by validateRows).
function readRows() {
  return manager.rows
    .map((record) => ({
      enabled: record.enabled,
      source_lang: record.source_lang,
      target_lang: record.target_lang,
      source: record.source.trim(),
      target: record.target.trim(),
      note: record.note.trim(),
    }))
    .filter((entry) => entry.source && entry.target);
}

function renderRows(entries) {
  manager.rows = entries.map(toRecord);
  manager.invalid = new Set();
  manager.limit = PAGE_ROWS;
  renderView();
  updateMeta();
}

function filteredRows() {
  const query = manager.els.search.value.trim().toLowerCase();
  if (!query) return manager.rows;
  return manager.rows.filter((record) => `${record.source} ${record.target} ${record.note}`.toLowerCase().includes(query));
}

// Renders the first `limit` rows of the current view; with `append`, adds only the rows not yet shown.
function renderView({ append = false } = {}) {
  const { body, noMatch, search } = manager.els;
  if (!append) manager.view = filteredRows();
  const view = manager.view;
  const shown = append ? body.rows.length : 0;
  const fragment = document.createDocumentFragment();
  for (const record of view.slice(shown, manager.limit)) fragment.append(makeRow(record));
  if (append) body.append(fragment);
  else body.replaceChildren(fragment);
  updateMore();
  noMatch.hidden = !search.value.trim() || view.length > 0;
}

function updateMore() {
  const rest = manager.view.length - manager.els.body.rows.length;
  manager.els.more.hidden = rest <= 0;
  manager.els.more.textContent = rest > 0 ? `더 보기 (${fmtNum(rest)})` : "";
}

// Refuses to save half-filled rows: flags them, shows the first one and focuses its empty field.
function validateRows() {
  const partial = manager.rows.filter(isPartial);
  manager.invalid = new Set(partial);
  if (!partial.length) {
    for (const row of manager.els.body.rows) if (row._record) markRow(row);
    return true;
  }
  const first = partial[0];
  if (!manager.view.includes(first)) {
    manager.els.search.value = "";
    manager.view = manager.rows;
  }
  const index = manager.view.indexOf(first);
  if (index >= manager.limit) manager.limit = Math.ceil((index + 1) / PAGE_ROWS) * PAGE_ROWS;
  renderView();
  const row = [...manager.els.body.rows].find((item) => item._record === first);
  if (row) {
    row.scrollIntoView({ block: "nearest" });
    (first.source.trim() ? row._fields.target : row._fields.source).focus();
  }
  toast(`원문 용어와 번역 용어를 모두 입력하세요 (${fmtNum(partial.length)}행)`, { type: "error", timeout: 4000 });
  return false;
}

function setDirty(dirty) {
  manager.dirty = dirty;
  manager.els.dirty.hidden = !dirty;
  manager.els.save.disabled = !dirty || !manager.editingId;
  emit("draft");
}

function updateMeta() {
  const total = manager.rows.length;
  manager.els.count.textContent = `${fmtNum(total)}개 항목`;
  manager.els.emptyRows.hidden = total > 0 || !manager.editingId;
}

function applyFilter() {
  manager.limit = PAGE_ROWS;
  renderView();
}

function renderPicker() {
  const { picker } = manager.els;
  picker.replaceChildren(
    ...state.list.map((item) => h("option", { value: item.id }, `${item.name} (${fmtNum(item.entry_count)})`)),
  );
  picker.value = manager.editingId || "";
  const has = Boolean(manager.editingId);
  picker.disabled = !state.list.length;
  manager.els.rename.disabled = !has;
  manager.els.remove.disabled = !has;
  manager.els.importBtn.disabled = !has;
  manager.els.exportBtn.disabled = !has;
  manager.els.addRow.disabled = !has;
  manager.els.search.disabled = !has;
  manager.els.tableWrap.hidden = !has;
  manager.els.empty.hidden = has;
}

async function openEditing(id) {
  manager.editingId = id || null;
  manager.doc = null;
  manager.rows = [];
  manager.view = [];
  manager.invalid = new Set();
  manager.els.more.hidden = true;
  renderPicker();
  setDirty(false);
  manager.els.search.value = "";
  if (!id) {
    manager.els.body.replaceChildren();
    updateMeta();
    return;
  }
  manager.els.body.replaceChildren(
    h("tr", { class: "row-loading" }, h("td", { colspan: "7" }, h("span", { class: "sk", style: { width: "40%" } }))),
  );
  try {
    const doc = await fetchGlossary(id, { fresh: true });
    if (manager.editingId !== id) return;
    manager.doc = doc;
    rememberDoc(doc);
    renderPicker();
    renderRows(doc.entries);
  } catch (error) {
    manager.els.body.replaceChildren(h("tr", {}, h("td", { colspan: "7", class: "cell-error", text: error.message })));
  }
}

async function resolveUnsaved() {
  if (!manager.dirty) return true;
  const choice = await choose({
    title: "저장하지 않은 변경 사항",
    message: "변경 사항을 저장할까요?",
    buttons: [
      { id: "cancel", label: "취소" },
      { id: "discard", label: "저장 안 함" },
      { id: "save", label: "저장", variant: "primary" },
    ],
  });
  if (choice === "save") return save();
  if (choice === "discard") {
    setDirty(false);
    return true;
  }
  return false;
}

async function save() {
  if (!manager.editingId || !manager.doc) return false;
  if (!validateRows()) return false;
  const button = manager.els.save;
  button.disabled = true;
  try {
    const data = await api(`/api/glossaries/${encodeURIComponent(manager.editingId)}`, {
      method: "PUT",
      json: { name: manager.doc.name, entries: readRows() },
    });
    manager.doc = data.glossary;
    rememberDoc(data.glossary);
    renderRows(data.glossary.entries);
    setDirty(false);
    renderPicker();
    toast("저장했습니다");
    emit("saved");
    return true;
  } catch (error) {
    toast(error.message, { type: "error", timeout: 4000 });
    button.disabled = false;
    return false;
  }
}

function nameForm({ title, value = "", submitLabel, onSubmit }) {
  const input = h("input", {
    type: "text",
    class: "input",
    maxlength: "120",
    required: true,
    "aria-label": "용어집 이름",
    placeholder: "용어집 이름",
    autofocus: true,
  });
  input.value = value;
  const error = h("p", { class: "pop-error", hidden: true });
  const submit = h("button", { type: "submit", class: "btn btn-primary btn-sm" }, submitLabel);
  const form = h(
    "form",
    { class: "pop-form" },
    h("div", { class: "pop-head" }, h("span", { class: "pop-title", text: title })),
    input,
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
    const name = input.value.trim();
    if (!name) {
      input.focus();
      return;
    }
    submit.disabled = true;
    try {
      await onSubmit(name);
      closePopover();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      submit.disabled = false;
    }
  });
  setTimeout(() => input.select(), 0);
  return form;
}

async function createGlossary(anchor) {
  if (isPopoverFor(anchor)) return closePopover();
  if (!(await resolveUnsaved())) return;
  openPopover({
    anchor,
    align: "start",
    focus: true,
    label: "새 용어집",
    content: nameForm({
      title: "새 용어집",
      submitLabel: "만들기",
      onSubmit: async (name) => {
        const data = await api("/api/glossaries", { method: "POST", json: { name, entries: [] } });
        rememberDoc(data.glossary);
        state.activeId = data.glossary.id;
        persist();
        emit("active");
        await openEditing(data.glossary.id);
        toast("용어집을 만들었습니다");
      },
    }),
  });
}

function renameGlossary(anchor) {
  if (!manager.doc) return;
  if (isPopoverFor(anchor)) return closePopover();
  openPopover({
    anchor,
    align: "start",
    focus: true,
    label: "이름 변경",
    content: nameForm({
      title: "이름 변경",
      value: manager.doc.name,
      submitLabel: "변경",
      onSubmit: async (name) => {
        if (manager.dirty && !validateRows()) return;
        const entries = manager.dirty ? readRows() : manager.doc.entries;
        const data = await api(`/api/glossaries/${encodeURIComponent(manager.doc.id)}`, {
          method: "PUT",
          json: { name, entries },
        });
        manager.doc = data.glossary;
        rememberDoc(data.glossary);
        if (manager.dirty) renderRows(data.glossary.entries);
        setDirty(false);
        renderPicker();
        emit("saved");
      },
    }),
  });
}

async function deleteGlossary() {
  if (!manager.doc) return;
  const ok = await confirmAction({
    title: `'${manager.doc.name}' 삭제`,
    message: `${fmtNum(manager.doc.entries.length)}개 항목이 함께 삭제됩니다.`,
    confirmLabel: "삭제",
    danger: true,
  });
  if (!ok) return;
  try {
    await api(`/api/glossaries/${encodeURIComponent(manager.doc.id)}`, { method: "DELETE" });
  } catch (error) {
    if (error.status !== 404) {
      toast(error.message, { type: "error" });
      return;
    }
  }
  const deletedId = manager.doc.id;
  state.docs.delete(deletedId);
  state.list = state.list.filter((item) => item.id !== deletedId);
  if (state.activeId === deletedId) {
    state.activeId = state.list[0]?.id ?? null;
    persist();
    emit("active");
  }
  setDirty(false);
  await loadGlossaries();
  await openEditing(state.activeId);
  toast("삭제했습니다");
}

function importPopover(anchor) {
  if (!manager.doc) return;
  if (isPopoverFor(anchor)) return closePopover();
  const direction = directionProvider();
  const fileInput = h("input", {
    type: "file",
    accept: ".csv,.tsv,.txt,.xlsx,text/csv,text/tab-separated-values,text/plain,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    hidden: true,
  });
  const fileName = h("span", { class: "file-name muted", text: "선택된 파일 없음" });
  const pick = h("button", { type: "button", class: "btn btn-sm", onclick: () => fileInput.click() }, icon("file", { size: 16 }), "파일 선택");
  let mode = "append";
  const modeButtons = [
    ["append", "추가"],
    ["replace", "교체"],
  ].map(([value, label]) =>
    h(
      "button",
      {
        type: "button",
        role: "radio",
        "aria-checked": String(value === mode),
        onclick: (event) => {
          mode = value;
          for (const button of modeButtons) button.setAttribute("aria-checked", String(button === event.currentTarget));
        },
      },
      label,
    ),
  );
  const sourceLang = langSelect(direction.source, "기본 원문 언어");
  const targetLang = langSelect(direction.target, "기본 번역 언어");
  sourceLang.className = targetLang.className = "select select-sm";
  const error = h("p", { class: "pop-error", hidden: true });
  const submit = h("button", { type: "button", class: "btn btn-primary btn-sm", disabled: true }, "가져오기");
  fileInput.addEventListener("change", () => {
    const file = fileInput.files?.[0];
    fileName.textContent = file ? file.name : "선택된 파일 없음";
    submit.disabled = !file;
  });
  submit.addEventListener("click", async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    if (manager.dirty) {
      const ok = await confirmAction({ title: "저장하지 않은 변경 사항", message: "가져오면 저장하지 않은 변경 사항이 사라집니다.", confirmLabel: "계속" });
      if (!ok) return;
    }
    submit.disabled = true;
    error.hidden = true;
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("mode", mode);
    form.append("source_lang", sourceLang.value);
    form.append("target_lang", targetLang.value);
    try {
      const before = manager.doc.entries.length;
      const data = await api(`/api/glossaries/${encodeURIComponent(manager.doc.id)}/import`, { method: "POST", body: form });
      manager.doc = data.glossary;
      rememberDoc(data.glossary);
      renderRows(data.glossary.entries);
      setDirty(false);
      renderPicker();
      closePopover();
      const total = data.glossary.entries.length;
      toast(mode === "replace" ? `${fmtNum(total)}개 항목으로 교체했습니다` : `${fmtNum(Math.max(0, total - before))}개 항목을 추가했습니다`);
      emit("saved");
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      submit.disabled = false;
    }
  });
  const content = h(
    "div",
    { class: "pop-form import-form" },
    h("div", { class: "pop-head" }, h("span", { class: "pop-title", text: "가져오기" }), h("span", { class: "pop-sub", text: "CSV · TSV · XLSX" })),
    h("div", { class: "form-row" }, h("span", { class: "form-label", text: "파일" }), h("div", { class: "form-control file-pick" }, pick, fileName, fileInput)),
    h("div", { class: "form-row" }, h("span", { class: "form-label", text: "방식" }), h("div", { class: "seg seg-sm", role: "radiogroup", "aria-label": "가져오기 방식" }, modeButtons)),
    h(
      "div",
      { class: "form-row" },
      h("span", { class: "form-label", text: "2열 파일" }),
      h("div", { class: "form-control lang-pair" }, sourceLang, icon("arrowRight", { size: 16, cls: "muted-icon" }), targetLang),
    ),
    error,
    h("div", { class: "pop-actions" }, h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => closePopover() }, "취소"), submit),
  );
  openPopover({ anchor, content, align: "end", label: "용어집 가져오기", className: "pop-wide" });
}

function exportPopover(anchor) {
  if (!manager.doc) return;
  if (isPopoverFor(anchor)) return closePopover();
  const go = async (format) => {
    closePopover();
    if (manager.dirty && !(await save())) return;
    downloadUrl(`/api/glossaries/${encodeURIComponent(manager.doc.id)}/export?format=${format}`);
  };
  const menu = h(
    "div",
    { class: "menu", role: "menu" },
    h("button", { type: "button", class: "menu-item", role: "menuitem", onclick: () => go("csv") }, h("span", { text: "CSV" }), h("span", { class: "menu-hint", text: "Excel" })),
    h("button", { type: "button", class: "menu-item", role: "menuitem", onclick: () => go("tsv") }, h("span", { text: "TSV" }), h("span", { class: "menu-hint", text: "탭 구분" })),
  );
  enableListNavigation(menu, ".menu-item");
  openPopover({ anchor, content: menu, align: "end", label: "내보내기", focus: true, role: "presentation" });
}

function buildManager() {
  const els = manager.els;
  els.picker = h("select", { class: "select gl-picker", "aria-label": "편집할 용어집" });
  els.create = h("button", { type: "button", class: "btn btn-sm" }, icon("plus", { size: 16 }), "새 용어집");
  els.rename = h("button", { type: "button", class: "icon-btn tip", "aria-label": "이름 변경", "data-tip": "이름 변경" }, icon("edit"));
  els.remove = h("button", { type: "button", class: "icon-btn tip icon-danger", "aria-label": "용어집 삭제", "data-tip": "용어집 삭제" }, icon("trash"));
  els.importBtn = h("button", { type: "button", class: "btn btn-sm" }, icon("importFile", { size: 16 }), "가져오기");
  els.exportBtn = h("button", { type: "button", class: "btn btn-sm" }, icon("exportFile", { size: 16 }), "내보내기");
  els.search = h("input", { type: "search", class: "input input-search", placeholder: "검색", "aria-label": "용어 검색" });
  els.count = h("span", { class: "muted small" });
  els.dirty = h("span", { class: "badge badge-warn", hidden: true }, "저장 안 됨");
  els.body = h("tbody");
  els.noMatch = h("div", { class: "table-empty", hidden: true, text: "검색 결과 없음" });
  els.emptyRows = h("div", { class: "table-empty", hidden: true, text: "항목 없음" });
  els.more = h("button", { type: "button", class: "btn btn-ghost btn-sm gl-more", hidden: true });
  els.tableWrap = h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      { class: "gl-table" },
      h(
        "thead",
        {},
        h(
          "tr",
          {},
          h("th", { class: "c-use", text: "사용" }),
          h("th", { class: "c-lang", text: "원문 언어" }),
          h("th", { class: "c-lang", text: "번역 언어" }),
          h("th", { text: "원문 용어" }),
          h("th", { text: "번역 용어" }),
          h("th", { text: "메모" }),
          h("th", { class: "c-del" }, h("span", { class: "sr-only", text: "삭제" })),
        ),
      ),
      els.body,
    ),
    els.more,
    els.noMatch,
    els.emptyRows,
  );
  const emptyCreate = h("button", { type: "button", class: "btn btn-primary" }, icon("plus", { size: 16 }), "새 용어집");
  els.empty = h("div", { class: "gl-empty", hidden: true }, icon("book", { size: 28 }), h("p", { text: "용어집이 없습니다" }), emptyCreate);
  els.addRow = h("button", { type: "button", class: "btn btn-sm" }, icon("plus", { size: 16 }), "행 추가");
  els.save = h("button", { type: "button", class: "btn btn-primary", disabled: true }, "저장");
  const close = h("button", { type: "button", class: "btn" }, "닫기");
  const closeX = h("button", { type: "button", class: "icon-btn", "aria-label": "닫기" }, icon("x"));

  const dialog = h(
    "dialog",
    { class: "modal modal-lg gl-dialog", "aria-labelledby": "glTitle" },
    h("div", { class: "modal-head" }, h("h2", { class: "modal-title", id: "glTitle", text: "용어집" }), closeX),
    h(
      "div",
      { class: "gl-toolbar" },
      h("div", { class: "gl-pick" }, els.picker, els.rename, els.remove, els.create),
      h("div", { class: "gl-io" }, els.importBtn, els.exportBtn),
    ),
    h(
      "div",
      { class: "gl-subbar" },
      h("label", { class: "search-field" }, icon("search", { size: 16 }), els.search),
      h("div", { class: "gl-meta" }, els.dirty, els.count),
    ),
    h("div", { class: "modal-body gl-body" }, els.tableWrap, els.empty),
    h("div", { class: "modal-foot" }, els.addRow, h("span", { class: "spacer" }), close, els.save),
  );
  manager.dialog = dialog;

  const requestClose = async () => {
    if (await resolveUnsaved()) closeModal(dialog);
  };
  dialog.addEventListener("cancel", (event) => {
    if (manager.dirty) {
      event.preventDefault();
      requestClose();
    }
  });
  dialog.addEventListener("close", () => closePopover());
  close.addEventListener("click", requestClose);
  closeX.addEventListener("click", requestClose);
  els.picker.addEventListener("change", async () => {
    const next = els.picker.value;
    if (!(await resolveUnsaved())) {
      els.picker.value = manager.editingId;
      return;
    }
    await openEditing(next);
  });
  els.create.addEventListener("click", () => createGlossary(els.create));
  emptyCreate.addEventListener("click", () => createGlossary(emptyCreate));
  els.rename.addEventListener("click", () => renameGlossary(els.rename));
  els.remove.addEventListener("click", deleteGlossary);
  els.importBtn.addEventListener("click", () => importPopover(els.importBtn));
  els.exportBtn.addEventListener("click", () => exportPopover(els.exportBtn));
  els.save.addEventListener("click", save);
  els.search.addEventListener("input", applyFilter);
  els.more.addEventListener("click", () => {
    manager.limit = manager.els.body.rows.length + PAGE_ROWS * 2;
    renderView({ append: true });
  });
  // New rows go on top so they are always on the first page.
  els.addRow.addEventListener("click", () => {
    manager.rows.unshift(toRecord());
    els.search.value = "";
    applyFilter();
    updateMeta();
    const row = els.body.rows[0];
    row?._fields.source.focus();
    row?.scrollIntoView({ block: "nearest" });
  });
  const onCellEdit = (event) => {
    const row = event.target.closest("tr");
    if (!row?._record) return;
    syncRecord(row);
    if (manager.invalid.has(row._record) && !isPartial(row._record)) manager.invalid.delete(row._record);
    markRow(row);
    setDirty(true);
  };
  els.body.addEventListener("input", onCellEdit);
  els.body.addEventListener("change", onCellEdit);
  els.body.addEventListener("click", (event) => {
    const button = event.target.closest('[data-action="remove"]');
    if (!button) return;
    const row = button.closest("tr");
    const record = row._record;
    const index = [...els.body.rows].indexOf(row);
    manager.rows = manager.rows.filter((item) => item !== record);
    manager.view = manager.view.filter((item) => item !== record);
    manager.invalid.delete(record);
    row.remove();
    // Keep the page full: show the row of the view that was just below the page.
    if (els.body.rows.length < Math.min(manager.limit, manager.view.length)) renderView({ append: true });
    else updateMore();
    setDirty(true);
    updateMeta();
    const next = els.body.rows[Math.min(index, els.body.rows.length - 1)];
    next?.querySelector(".row-del")?.focus();
  });
  dialog.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (manager.dirty) save();
    }
  });
  document.body.append(dialog);
}

export async function openGlossaryManager() {
  if (!manager.dialog) buildManager();
  showModal(manager.dialog);
  if (!state.loaded) await loadGlossaries();
  const target = manager.editingId && state.list.some((item) => item.id === manager.editingId) ? manager.editingId : state.activeId;
  if (!manager.dirty || target !== manager.editingId) await openEditing(target);
  else renderPicker();
}

export function directionLabel(entry) {
  return `${langLabel(entry.source_lang)} → ${langLabel(entry.target_lang)}`;
}
