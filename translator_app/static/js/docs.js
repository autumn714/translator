import { api, redirectToLogin } from "./api.js";
import {
  $,
  h,
  store,
  uid,
  fmtNum,
  fmtBytes,
  fmtEta,
  fmtWhen,
  fmtRemaining,
  toast,
  copyText,
  downloadUrl,
  showModal,
  closeModal,
  confirmAction,
  skeletonLines,
} from "./dom.js";
import { icon } from "./icons.js";
import { langLabel, fillLangSelect, fillFormality } from "./langs.js";
import { glossaryState, onGlossaryChange, renderGlossaryOptions } from "./glossary.js";
import { getStatus, onStatus } from "./status.js";

const KEY_JOBS = "translator.jobs";
const KEY_OPTS = "translator.docOptions";
const KEY_FORMALITY = "translator.docFormality";
const POLL_MS = 1500;
const MAX_STORED_JOBS = 50;

const ACTIVE = new Set(["queued", "extracting", "translating", "writing"]);
const STATUS = {
  uploading: ["업로드 중", "accent"],
  queued: ["대기", "neutral"],
  extracting: ["분석 중", "accent"],
  translating: ["번역 중", "accent"],
  writing: ["저장 중", "accent"],
  done: ["완료", "success"],
  error: ["오류", "danger"],
  canceled: ["취소됨", "neutral"],
};
const BILINGUAL_FALLBACK = new Set([".docx", ".hwpx", ".txt", ".md"]);
const LEGACY = { ".doc": ".docx", ".ppt": ".pptx", ".xls": ".xlsx" };
const KIND = {
  ".docx": "word",
  ".pptx": "ppt",
  ".xlsx": "excel",
  ".hwpx": "hwp",
  ".hwp": "hwp",
  ".pdf": "pdf",
  ".txt": "text",
  ".md": "text",
  ".srt": "text",
  ".vtt": "text",
  ".html": "html",
  ".htm": "html",
  ".png": "image",
  ".jpg": "image",
  ".jpeg": "image",
  ".webp": "image",
};
const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".webp"]);

const el = {};
const D = {
  jobs: new Map(),
  order: [],
  cards: new Map(),
  staged: [],
  reports: new Map(),
  output: "translated",
  pdfMode: "layout",
  glossaryId: "",
  timer: 0,
  setOutput: null,
  setPdf: null,
};

const extOf = (name) => {
  const index = String(name || "").lastIndexOf(".");
  return index >= 0 ? String(name).slice(index).toLowerCase() : "";
};
const normalizeExt = (value) => {
  const ext = String(value || "").toLowerCase();
  return ext.startsWith(".") ? ext : ext ? `.${ext}` : "";
};
const maxMb = () => Number(getStatus().limits?.doc_max_mb) || 50;
const retentionHours = () => Number(getStatus().limits?.doc_retention_hours) || 24;
const supportedExts = () => new Set(getStatus().document_formats.map((format) => normalizeExt(format.ext)));
// Formats that accept output "bilingual" (server flag `bilingual`; older servers: fixed list).
function bilingualExts() {
  const formats = getStatus().document_formats;
  if (!formats.some((format) => typeof format.bilingual === "boolean")) return BILINGUAL_FALLBACK;
  return new Set(formats.filter((format) => format.bilingual).map((format) => normalizeExt(format.ext)));
}

function badge(ext) {
  const label = (ext || "").replace(".", "").toUpperCase().slice(0, 4) || "FILE";
  return h("span", { class: `fbadge fb-${KIND[ext] || "other"}`, "aria-hidden": "true" }, label);
}

function langPair(source, target) {
  return `${source && source !== "auto" ? langLabel(source) : "자동 감지"} → ${langLabel(target)}`;
}

// ---------- options ----------

function saveOptions() {
  store.set(KEY_OPTS, {
    source_lang: el.src.value,
    target_lang: el.tgt.value,
    glossary_id: D.glossaryId,
    output: D.output,
    pdf_mode: D.pdfMode,
  });
}

function refreshFormality() {
  const map = store.get(KEY_FORMALITY, {}) || {};
  fillFormality(el.formality, el.tgt.value, map[el.tgt.value] ?? "auto");
}

function renderGlossarySelect() {
  renderGlossaryOptions(el.gloss, { value: D.glossaryId, noneLabel: "사용 안 함" });
}

function bindSegmented(group, value, onChange) {
  const buttons = [...group.querySelectorAll('[role="radio"]')];
  const set = (next) => {
    for (const button of buttons) {
      const on = button.dataset.value === next;
      button.setAttribute("aria-checked", String(on));
      button.tabIndex = on ? 0 : -1;
    }
  };
  set(value);
  for (const button of buttons) {
    button.addEventListener("click", () => {
      if (button.disabled) return;
      set(button.dataset.value);
      onChange(button.dataset.value);
    });
  }
  group.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const enabled = buttons.filter((button) => !button.disabled);
    const index = enabled.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    const next = enabled[(index + (event.key === "ArrowRight" ? 1 : enabled.length - 1)) % enabled.length];
    next.focus();
    next.click();
  });
  return set;
}

function renderFormatChips() {
  const labels = [];
  const seen = new Set();
  for (const format of getStatus().document_formats) {
    const ext = normalizeExt(format.ext);
    let label = ext.replace(".", "").toUpperCase();
    if (IMAGE_EXTS.has(ext)) label = "이미지";
    if (ext === ".htm") label = "HTML";
    if (!label || seen.has(label)) continue;
    seen.add(label);
    labels.push(label);
  }
  el.chips.replaceChildren(...labels.map((label) => h("span", { class: "fmt-chip", text: label })));
  const bilingualLabels = [...new Set([...bilingualExts()].map((ext) => ext.replace(".", "").toUpperCase()))];
  el.outputField.dataset.tip = bilingualLabels.length ? `원문+번역: ${bilingualLabels.join(" · ")}` : "";
  el.dzMeta.textContent = `최대 ${fmtNum(maxMb())}MB · ${fmtNum(retentionHours())}시간 후 자동 삭제`;
  el.fileInput.accept = [...supportedExts()].join(",");
}

// ---------- staging ----------

export function addFiles(files) {
  const supported = supportedExts();
  const limit = maxMb() * 1024 * 1024;
  for (const file of files) {
    const ext = extOf(file.name);
    let error = null;
    if (LEGACY[ext]) error = `${LEGACY[ext].slice(1).toUpperCase()} 형식으로 저장한 뒤 올리세요`;
    else if (!supported.has(ext)) error = "지원하지 않는 형식";
    else if (file.size > limit) error = `${fmtNum(maxMb())}MB 초과`;
    else if (file.size === 0) error = "빈 파일";
    D.staged.push({ id: uid(), file, ext, error });
  }
  renderStaging();
  requestAnimationFrame(() => {
    el.staging.scrollIntoView({ block: "nearest", behavior: "smooth" });
    if (!el.start.disabled) el.start.focus({ preventScroll: true });
  });
}

// Full drop zone only while the page is empty; a slimmer one once files are staged or jobs exist.
function updateDropzone() {
  const staged = D.staged.length > 0;
  el.dropzone.classList.toggle("is-compact", staged);
  el.dropzone.classList.toggle("is-short", !staged && D.order.length > 0);
}

function renderStaging() {
  const staged = D.staged;
  el.staging.hidden = !staged.length;
  updateDropzone();
  el.stagedList.replaceChildren(
    ...staged.map((item) =>
      h(
        "li",
        { class: `staged${item.error ? " is-invalid" : ""}` },
        badge(item.ext),
        h(
          "div",
          { class: "staged-main" },
          h("span", { class: "staged-name", text: item.file.name, title: item.file.name }),
          h("span", { class: "staged-meta" }, item.error ? [icon("alert", { size: 14 }), item.error] : fmtBytes(item.file.size)),
        ),
        h(
          "button",
          {
            type: "button",
            class: "icon-btn icon-btn-sm",
            "aria-label": `${item.file.name} 제거`,
            onclick: () => {
              D.staged = D.staged.filter((other) => other !== item);
              renderStaging();
            },
          },
          icon("x", { size: 16 }),
        ),
      ),
    ),
  );
  const valid = staged.filter((item) => !item.error);
  const bilingualSet = bilingualExts();
  const bilingual = valid.some((item) => bilingualSet.has(item.ext));
  for (const button of el.outputSeg.querySelectorAll("button")) button.disabled = !bilingual && button.dataset.value === "bilingual";
  el.outputField.classList.toggle("is-disabled", !bilingual);
  D.setOutput?.(bilingual ? D.output : "translated");
  el.pdfField.hidden = !valid.some((item) => item.ext === ".pdf");
  el.start.disabled = !valid.length;
  el.startLabel.textContent = valid.length > 1 ? `번역 시작 · ${valid.length}개` : "번역 시작";
}

function startStaged() {
  const valid = D.staged.filter((item) => !item.error);
  if (!valid.length) return;
  D.staged = [];
  renderStaging();
  const base = {
    source_lang: el.src.value,
    target_lang: el.tgt.value,
    formality: el.formality.value || "auto",
    context: el.ctxText.value.trim().slice(0, 4000),
    use_glossary: Boolean(el.gloss.value),
    glossary_id: el.gloss.value || null,
    pdf_mode: D.pdfMode,
  };
  const bilingualSet = bilingualExts();
  const uploads = valid.map((item) => createUpload(item.file, item.ext, { ...base, output: bilingualSet.has(item.ext) ? D.output : "translated" }));
  renderJobs();
  el.section.scrollIntoView({ block: "nearest", behavior: "smooth" });
  (async () => {
    for (const startUpload of uploads) await startUpload();
  })();
}

// ---------- upload ----------

function uploadError(status, detail) {
  if (detail) return detail;
  if (status === 413) return `파일이 너무 큽니다 (최대 ${fmtNum(maxMb())}MB)`;
  if (status === 415 || status === 400) return "지원하지 않는 형식입니다";
  if (status === 429) return "요청이 많습니다. 잠시 후 다시 시도하세요";
  if (status >= 500) return "서버 오류가 발생했습니다";
  return "업로드하지 못했습니다";
}

function createUpload(file, ext, options) {
  const tempId = `upload-${uid()}`;
  const job = {
    id: tempId,
    temp: true,
    filename: file.name,
    size: file.size,
    format: ext,
    source_lang: options.source_lang,
    target_lang: options.target_lang,
    status: "uploading",
    progress: { percent: 0 },
    warnings: [],
    error: null,
  };
  D.jobs.set(tempId, job);
  D.order.unshift(tempId);
  return () =>
    new Promise((resolve) => {
      if (!D.jobs.has(tempId)) {
        resolve();
        return;
      }
      const xhr = new XMLHttpRequest();
      job.xhr = xhr;
      xhr.open("POST", "/api/documents");
      xhr.setRequestHeader("Accept", "application/json");
      xhr.upload.addEventListener("progress", (event) => {
        if (!event.lengthComputable) return;
        job.progress = { percent: (event.loaded / event.total) * 100 };
        renderJobs();
      });
      xhr.addEventListener("load", () => {
        job.xhr = null;
        if (xhr.status === 401) {
          redirectToLogin();
          resolve();
          return;
        }
        let data = null;
        try {
          data = JSON.parse(xhr.responseText);
        } catch {
          data = null;
        }
        if (xhr.status >= 200 && xhr.status < 300 && data?.id) replaceTemp(tempId, data);
        else failTemp(tempId, uploadError(xhr.status, typeof data?.detail === "string" ? data.detail : ""));
        resolve();
      });
      xhr.addEventListener("error", () => {
        job.xhr = null;
        failTemp(tempId, "서버에 연결할 수 없습니다");
        resolve();
      });
      xhr.addEventListener("abort", () => {
        removeJob(tempId);
        resolve();
      });
      const form = new FormData();
      form.append("file", file, file.name);
      form.append("options", JSON.stringify(options));
      xhr.send(form);
    });
}

function replaceTemp(tempId, job) {
  const index = D.order.indexOf(tempId);
  D.jobs.delete(tempId);
  D.cards.get(tempId)?.li.remove();
  D.cards.delete(tempId);
  D.jobs.set(job.id, job);
  if (index >= 0) D.order[index] = job.id;
  else D.order.unshift(job.id);
  persist();
  renderJobs();
  if (job.status === "done") loadReport(job.id);
  schedulePoll();
}

function failTemp(tempId, message) {
  const job = D.jobs.get(tempId);
  if (!job) return;
  job.status = "error";
  job.error = message;
  renderJobs();
}

// ---------- jobs ----------

function persist() {
  const ids = D.order.filter((id) => !D.jobs.get(id)?.temp).slice(0, MAX_STORED_JOBS);
  store.set(KEY_JOBS, ids);
}

function removeJob(id) {
  D.jobs.delete(id);
  D.order = D.order.filter((other) => other !== id);
  D.reports.delete(id);
  persist();
  renderJobs();
}

function activeIds() {
  return D.order.filter((id) => {
    const job = D.jobs.get(id);
    return job && !job.temp && ACTIVE.has(job.status);
  });
}

function schedulePoll(delay = POLL_MS) {
  clearTimeout(D.timer);
  if (!activeIds().length) return;
  D.timer = setTimeout(poll, delay);
}

async function poll() {
  const ids = activeIds();
  if (!ids.length) return;
  try {
    const data = await api(`/api/documents?ids=${ids.map(encodeURIComponent).join(",")}`);
    const seen = new Set();
    for (const job of Array.isArray(data?.jobs) ? data.jobs : []) {
      if (!D.jobs.has(job.id)) continue;
      seen.add(job.id);
      const previous = D.jobs.get(job.id);
      D.jobs.set(job.id, job);
      if (previous && previous.status !== "done" && job.status === "done") onJobDone(job);
      if (previous && previous.status !== "error" && job.status === "error" && $("#panel-docs").hidden) {
        toast(`번역 오류: ${job.filename}`, { type: "error", timeout: 4000 });
      }
    }
    for (const id of ids) {
      if (!seen.has(id)) {
        D.jobs.delete(id);
        D.order = D.order.filter((other) => other !== id);
      }
    }
    persist();
    renderJobs();
  } catch {
    /* transient failure: keep polling */
  }
  schedulePoll();
}

function onJobDone(job) {
  loadReport(job.id);
  if ($("#panel-docs").hidden) toast(`번역 완료: ${job.filename}`, { timeout: 4000 });
}

async function loadReport(id) {
  const job = D.jobs.get(id);
  if (job && job.report_count === 0) {
    D.reports.set(id, []);
    return;
  }
  try {
    const data = await api(`/api/documents/${encodeURIComponent(id)}/report`);
    const items = Array.isArray(data?.items) ? data.items : [];
    D.reports.set(id, items);
  } catch {
    D.reports.set(id, []);
  }
  renderJobs();
}

async function restoreJobs() {
  const stored = store.get(KEY_JOBS, []);
  const ids = (Array.isArray(stored) ? stored : []).filter((id) => typeof id === "string" && id).slice(0, MAX_STORED_JOBS);
  if (!ids.length) return;
  let data;
  try {
    data = await api(`/api/documents?ids=${ids.map(encodeURIComponent).join(",")}`);
  } catch {
    return;
  }
  const jobs = (Array.isArray(data?.jobs) ? data.jobs : []).sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  for (const job of jobs) {
    if (D.jobs.has(job.id)) continue;
    D.jobs.set(job.id, job);
    D.order.push(job.id);
    if (job.status === "done") loadReport(job.id);
  }
  persist();
  renderJobs();
  schedulePoll(0);
}

async function cancelJob(id) {
  const job = D.jobs.get(id);
  if (!job) return;
  if (job.temp) {
    job.xhr?.abort();
    if (!job.xhr) removeJob(id);
    return;
  }
  try {
    const data = await api(`/api/documents/${encodeURIComponent(id)}/cancel`, { method: "POST" });
    if (data && typeof data === "object" && data.id === id) D.jobs.set(id, data);
    else job.status = "canceled";
  } catch (error) {
    if (error.status === 404) {
      removeJob(id);
      return;
    }
    toast(error.message, { type: "error" });
  }
  renderJobs();
  schedulePoll();
}

async function deleteJob(id) {
  const job = D.jobs.get(id);
  if (!job) return;
  if (!job.temp) {
    if (job.status === "done") {
      const ok = await confirmAction({
        title: "작업 삭제",
        message: `${job.output_filename || job.filename} 파일이 서버에서 삭제됩니다.`,
        confirmLabel: "삭제",
        danger: true,
      });
      if (!ok) return;
    }
    try {
      await api(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
    } catch (error) {
      if (error.status !== 404) {
        toast(error.message, { type: "error" });
        return;
      }
    }
  }
  removeJob(id);
}

function downloadJob(job) {
  downloadUrl(`/api/documents/${encodeURIComponent(job.id)}/download`);
}

// ---------- job cards ----------

function actionButton({ label, iconName, onClick, variant = "", compact = false }) {
  return h(
    "button",
    { type: "button", class: `btn btn-sm ${variant}`.trim(), onclick: onClick, "aria-label": compact ? label : null },
    icon(iconName, { size: 16 }),
    h("span", { class: compact ? "btn-label-wide" : "btn-label", text: label }),
  );
}

function buildActions(job) {
  const actions = [];
  if (job.status === "uploading" || ACTIVE.has(job.status)) {
    actions.push(actionButton({ label: "취소", iconName: "ban", onClick: () => cancelJob(job.id), variant: "btn-ghost" }));
  }
  if (job.status === "done") {
    actions.push(actionButton({ label: "미리보기", iconName: "eye", onClick: () => openPreview(job.id), compact: true }));
    const report = D.reports.get(job.id);
    if (report?.length) {
      actions.push(actionButton({ label: `검수 ${fmtNum(report.length)}`, iconName: "clipboardCheck", onClick: () => openReport(job.id), compact: true }));
    }
    actions.push(actionButton({ label: "내려받기", iconName: "download", onClick: () => downloadJob(D.jobs.get(job.id) || job), variant: "btn-primary" }));
  }
  if (job.status !== "uploading" && !ACTIVE.has(job.status)) {
    actions.push(
      h(
        "button",
        { type: "button", class: "icon-btn icon-btn-sm tip tip-end", "aria-label": "삭제", "data-tip": "삭제", onclick: () => deleteJob(job.id) },
        icon("trash", { size: 16 }),
      ),
    );
  }
  return actions;
}

function createCard() {
  const refs = {
    badgeWrap: h("div", { class: "job-icon" }),
    name: h("span", { class: "job-name" }),
    output: h("span", { class: "job-output" }),
    pill: h("span", { class: "pill" }),
    meta: h("span", { class: "job-meta-text" }),
    bar: h("div", { class: "bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100" }, h("span", { class: "bar-fill" })),
    error: h("p", { class: "job-error" }),
    warnSummary: h("span"),
    warnList: h("ul"),
    actions: h("div", { class: "job-actions" }),
  };
  refs.warn = h("details", { class: "job-warn" }, h("summary", {}, icon("warning", { size: 14 }), refs.warnSummary), refs.warnList);
  const li = h(
    "li",
    { class: "job" },
    refs.badgeWrap,
    h(
      "div",
      { class: "job-main" },
      h("div", { class: "job-title" }, refs.name, refs.output),
      h("div", { class: "job-meta" }, refs.pill, refs.meta),
      refs.bar,
      refs.error,
      refs.warn,
    ),
    refs.actions,
  );
  return { li, refs, actionsKey: "", badgeKey: "", warnKey: "" };
}

function percentOf(job) {
  const progress = job.progress || {};
  if (Number.isFinite(Number(progress.percent))) return Math.max(0, Math.min(100, Number(progress.percent)));
  if (progress.total) return Math.max(0, Math.min(100, (Number(progress.done) / Number(progress.total)) * 100));
  return 0;
}

function updateCard(card, job) {
  const { li, refs } = card;
  const ext = normalizeExt(job.format) || extOf(job.filename);
  li.dataset.status = job.status;
  if (card.badgeKey !== ext) {
    refs.badgeWrap.replaceChildren(badge(ext));
    card.badgeKey = ext;
  }
  refs.name.textContent = job.filename;
  refs.name.title = job.filename;
  const showOutput = job.status === "done" && job.output_filename && job.output_filename !== job.filename;
  refs.output.hidden = !showOutput;
  refs.output.textContent = showOutput ? job.output_filename : "";
  refs.output.title = showOutput ? job.output_filename : "";

  const [label, tone] = STATUS[job.status] || [job.status, "neutral"];
  const percent = percentOf(job);
  const running = job.status === "uploading" || ACTIVE.has(job.status);
  refs.pill.className = `pill pill-${tone}`;
  refs.pill.textContent = running && percent > 0 && job.status !== "queued" ? `${label} ${Math.floor(percent)}%` : label;

  const parts = [fmtBytes(job.size), langPair(job.source_lang, job.target_lang)];
  if (job.status === "translating" && job.eta_seconds != null) parts.push(fmtEta(job.eta_seconds));
  if (job.status === "done") {
    if (job.chars) parts.push(`${fmtNum(job.chars)}자`);
    if (job.finished_at) parts.push(fmtWhen(job.finished_at));
    if (job.expires_at) parts.push(fmtRemaining(job.expires_at));
  }
  refs.meta.textContent = parts.filter(Boolean).join(" · ");

  refs.bar.hidden = !running;
  const indeterminate = job.status === "queued" || job.status === "extracting" || (job.status === "uploading" && percent === 0);
  refs.bar.classList.toggle("is-indeterminate", indeterminate);
  refs.bar.firstChild.style.width = indeterminate ? "" : `${percent}%`;
  if (indeterminate) refs.bar.removeAttribute("aria-valuenow");
  else refs.bar.setAttribute("aria-valuenow", String(Math.round(percent)));
  refs.bar.setAttribute("aria-label", `${job.filename} ${label}`);

  refs.error.hidden = job.status !== "error" || !job.error;
  refs.error.textContent = job.status === "error" ? job.error || "" : "";

  const warnings = Array.isArray(job.warnings) ? job.warnings.filter(Boolean) : [];
  refs.warn.hidden = !warnings.length;
  const warnKey = warnings.join("\u0000");
  if (card.warnKey !== warnKey) {
    refs.warnSummary.textContent = `경고 ${fmtNum(warnings.length)}건`;
    refs.warnList.replaceChildren(...warnings.map((warning) => h("li", { text: warning })));
    card.warnKey = warnKey;
  }

  const actionsKey = `${job.status}|${D.reports.get(job.id)?.length || 0}`;
  if (card.actionsKey !== actionsKey) {
    const hadFocus = refs.actions.contains(document.activeElement);
    refs.actions.replaceChildren(...buildActions(job));
    card.actionsKey = actionsKey;
    if (hadFocus) refs.actions.querySelector("button")?.focus({ preventScroll: true });
  }
}

function renderJobs() {
  el.section.hidden = D.order.length === 0;
  updateDropzone();
  D.order.forEach((id, index) => {
    const job = D.jobs.get(id);
    if (!job) return;
    let card = D.cards.get(id);
    if (!card) {
      card = createCard();
      D.cards.set(id, card);
    }
    updateCard(card, job);
    const current = el.list.children[index];
    if (current !== card.li) el.list.insertBefore(card.li, current || null);
  });
  for (const [id, card] of D.cards) {
    if (!D.jobs.has(id)) {
      card.li.remove();
      D.cards.delete(id);
    }
  }
  el.meta.textContent = D.order.length ? `${fmtNum(D.order.length)}개` : "";
  const running = D.order.filter((id) => {
    const job = D.jobs.get(id);
    return job && (job.status === "uploading" || ACTIVE.has(job.status));
  }).length;
  el.tabBadge.hidden = !running;
  el.tabBadge.textContent = running ? String(running) : "";
}

// ---------- viewers ----------

function viewer({ title, subtitle }) {
  const body = h("div", { class: "modal-body viewer-body" }, skeletonLines(8, { lastWidth: 45 }));
  const foot = h("div", { class: "modal-foot" });
  const closeX = h("button", { type: "button", class: "icon-btn", "aria-label": "닫기" }, icon("x"));
  const dialog = h(
    "dialog",
    { class: "modal modal-lg viewer", "aria-label": title },
    h(
      "div",
      { class: "modal-head" },
      h("div", { class: "modal-titles" }, h("h2", { class: "modal-title", text: title }), subtitle ? h("p", { class: "modal-sub", text: subtitle }) : null),
      closeX,
    ),
    body,
    foot,
  );
  closeX.addEventListener("click", () => closeModal(dialog));
  dialog.addEventListener("close", () => dialog.remove());
  showModal(dialog);
  return { dialog, body, foot };
}

async function openPreview(id) {
  const job = D.jobs.get(id);
  if (!job) return;
  const { body, foot } = viewer({ title: "미리보기", subtitle: job.output_filename || job.filename });
  let text = "";
  const copyButton = h(
    "button",
    {
      type: "button",
      class: "btn",
      disabled: true,
      onclick: async () => {
        if (await copyText(text)) toast("복사했습니다");
      },
    },
    icon("copy", { size: 16 }),
    "복사",
  );
  foot.append(
    h("span", { class: "spacer" }),
    copyButton,
    h("button", { type: "button", class: "btn btn-primary", onclick: () => downloadJob(job) }, icon("download", { size: 16 }), "내려받기"),
  );
  try {
    const data = await api(`/api/documents/${encodeURIComponent(id)}/preview`);
    text = typeof data?.text === "string" ? data.text : "";
    body.replaceChildren(text.trim() ? h("pre", { class: "preview-text", text }) : h("p", { class: "viewer-empty", text: "미리볼 내용이 없습니다" }));
    copyButton.disabled = !text.trim();
  } catch (error) {
    body.replaceChildren(h("p", { class: "viewer-empty is-error", text: error.message }));
  }
}

function openReport(id) {
  const job = D.jobs.get(id);
  const items = D.reports.get(id) || [];
  if (!job) return;
  const { dialog, body, foot } = viewer({ title: "검수 결과", subtitle: `${job.filename} · ${fmtNum(items.length)}건` });
  body.replaceChildren(
    h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        { class: "report-table" },
        h("thead", {}, h("tr", {}, h("th", { text: "원문" }), h("th", { text: "번역" }), h("th", { text: "확인 사항" }))),
        h(
          "tbody",
          {},
          items.map((item) =>
            h("tr", {}, h("td", { text: item.source || "" }), h("td", { text: item.target || "" }), h("td", { class: "report-issue", text: item.issue || "" })),
          ),
        ),
      ),
    ),
  );
  foot.append(h("span", { class: "spacer" }), h("button", { type: "button", class: "btn", onclick: () => closeModal(dialog) }, "닫기"));
}

// ---------- init ----------

export function initDocs() {
  Object.assign(el, {
    panel: $("#panel-docs"),
    src: $("#docSrc"),
    tgt: $("#docTgt"),
    formality: $("#docFormality"),
    gloss: $("#docGloss"),
    ctxToggle: $("#docCtxToggle"),
    ctxDot: $("#docCtxToggle .opt-dot"),
    ctx: $("#docCtx"),
    ctxText: $("#docCtxText"),
    dropzone: $("#dropzone"),
    pick: $("#pickBtn"),
    fileInput: $("#fileInput"),
    chips: $("#fmtChips"),
    dzMeta: $("#dzMeta"),
    staging: $("#staging"),
    stagedList: $("#stagedList"),
    outputField: $("#outputField"),
    outputSeg: $("#outputSeg"),
    pdfField: $("#pdfField"),
    pdfSeg: $("#pdfSeg"),
    clearStaged: $("#stageClear"),
    start: $("#startBtn"),
    startLabel: $("#startBtn .btn-text"),
    section: $("#jobsSection"),
    list: $("#jobList"),
    meta: $("#jobsMeta"),
    tabBadge: $("#docsBadge"),
  });

  const saved = store.get(KEY_OPTS, {}) || {};
  fillLangSelect(el.src, { auto: true, autoLabel: "자동 감지", value: saved.source_lang || "auto" });
  fillLangSelect(el.tgt, { value: saved.target_lang || store.get("translator.targetLang", "ko") });
  refreshFormality();
  D.glossaryId = typeof saved.glossary_id === "string" ? saved.glossary_id : glossaryState.enabled ? glossaryState.activeId || "" : "";
  renderGlossarySelect();
  D.output = saved.output === "bilingual" ? "bilingual" : "translated";
  D.pdfMode = saved.pdf_mode === "docx" ? "docx" : "layout";
  D.setOutput = bindSegmented(el.outputSeg, D.output, (value) => {
    D.output = value;
    saveOptions();
  });
  D.setPdf = bindSegmented(el.pdfSeg, D.pdfMode, (value) => {
    D.pdfMode = value;
    saveOptions();
  });

  el.src.addEventListener("change", saveOptions);
  el.tgt.addEventListener("change", () => {
    refreshFormality();
    saveOptions();
  });
  el.formality.addEventListener("change", () => {
    const map = store.get(KEY_FORMALITY, {}) || {};
    map[el.tgt.value] = el.formality.value;
    store.set(KEY_FORMALITY, map);
  });
  el.gloss.addEventListener("change", () => {
    D.glossaryId = el.gloss.value;
    saveOptions();
  });
  onGlossaryChange(() => renderGlossarySelect());
  el.ctxToggle.addEventListener("click", () => {
    const open = el.ctx.hidden;
    el.ctx.hidden = !open;
    el.ctxToggle.setAttribute("aria-expanded", String(open));
    if (open) el.ctxText.focus();
  });
  el.ctxText.addEventListener("input", () => {
    el.ctxDot.hidden = !el.ctxText.value.trim();
  });

  el.pick.addEventListener("click", (event) => {
    event.stopPropagation();
    el.fileInput.click();
  });
  el.dropzone.addEventListener("click", (event) => {
    if (event.target.closest("button")) return;
    el.fileInput.click();
  });
  el.fileInput.addEventListener("change", () => {
    const files = [...(el.fileInput.files || [])];
    el.fileInput.value = "";
    if (files.length) addFiles(files);
  });
  let depth = 0;
  const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
  el.dropzone.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth += 1;
    el.dropzone.classList.add("is-over");
  });
  el.dropzone.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  el.dropzone.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) el.dropzone.classList.remove("is-over");
  });
  el.dropzone.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    depth = 0;
    el.dropzone.classList.remove("is-over");
    const files = [...(event.dataTransfer.files || [])];
    if (files.length) addFiles(files);
  });
  el.clearStaged.addEventListener("click", () => {
    D.staged = [];
    renderStaging();
  });
  el.start.addEventListener("click", startStaged);

  onStatus(() => {
    renderFormatChips();
    if (D.staged.length) {
      for (const item of D.staged) {
        if (item.error === "지원하지 않는 형식" && supportedExts().has(item.ext)) item.error = null;
      }
      renderStaging();
    }
  });
  renderFormatChips();
  renderStaging();
  renderJobs();
  restoreJobs();
  setInterval(() => {
    if (D.order.some((id) => D.jobs.get(id)?.status === "done")) renderJobs();
  }, 60000);
}
