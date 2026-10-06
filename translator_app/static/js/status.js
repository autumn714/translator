import { api } from "./api.js";
import { $, h, fmtNum, toast } from "./dom.js";
import { openPopover, closePopover, isPopoverFor } from "./popover.js";

const POLL_MS = 15000;

export const DEFAULT_FORMATS = [
  { ext: ".docx", label: "Word", output: ".docx", layout: true, bilingual: true },
  { ext: ".pptx", label: "PowerPoint", output: ".pptx", layout: true, bilingual: false },
  { ext: ".xlsx", label: "Excel", output: ".xlsx", layout: true, bilingual: false },
  { ext: ".hwpx", label: "한글", output: ".hwpx", layout: true, bilingual: true },
  { ext: ".hwp", label: "한글(구버전)", output: ".docx", layout: false, bilingual: false },
  { ext: ".pdf", label: "PDF", output: ".pdf", layout: true, bilingual: false },
  { ext: ".txt", label: "텍스트", output: ".txt", layout: true, bilingual: true },
  { ext: ".md", label: "Markdown", output: ".md", layout: true, bilingual: true },
  { ext: ".html", label: "HTML", output: ".html", layout: true, bilingual: false },
  { ext: ".htm", label: "HTML", output: ".htm", layout: true, bilingual: false },
  { ext: ".srt", label: "자막", output: ".srt", layout: true, bilingual: false },
  { ext: ".vtt", label: "자막", output: ".vtt", layout: true, bilingual: false },
  { ext: ".png", label: "이미지", output: ".docx", layout: false, bilingual: false },
  { ext: ".jpg", label: "이미지", output: ".docx", layout: false, bilingual: false },
  { ext: ".jpeg", label: "이미지", output: ".docx", layout: false, bilingual: false },
  { ext: ".webp", label: "이미지", output: ".docx", layout: false, bilingual: false },
];

const DEFAULT_STATUS = {
  app_version: "",
  engine: "",
  model: { connected: false, name: null, max_model_len: null, vision: false, running: null, waiting: null, error: null },
  auth: { enabled: false, user: null },
  limits: { text_max_chars: 30000, doc_max_mb: 50, doc_retention_hours: 24 },
  document_formats: DEFAULT_FORMATS,
};

let current = null;
let loaded = false;
const listeners = new Set();

export const getStatus = () => current || DEFAULT_STATUS;

export function onStatus(listener) {
  listeners.add(listener);
  if (current) listener(current);
}

function merge(data) {
  return {
    ...DEFAULT_STATUS,
    ...data,
    model: { ...DEFAULT_STATUS.model, ...(data?.model || {}) },
    auth: { ...DEFAULT_STATUS.auth, ...(data?.auth || {}) },
    limits: { ...DEFAULT_STATUS.limits, ...(data?.limits || {}) },
    document_formats: Array.isArray(data?.document_formats) && data.document_formats.length ? data.document_formats : DEFAULT_FORMATS,
  };
}

export async function refreshStatus() {
  try {
    current = merge(await api("/api/status"));
    current.unreachable = false;
  } catch (error) {
    if (error?.status === 401) return;
    const previous = current || DEFAULT_STATUS;
    current = merge({
      ...previous,
      model: { ...previous.model, connected: false, error: error?.message || "서버에 연결할 수 없습니다." },
    });
    current.unreachable = true;
  }
  loaded = true;
  render();
  listeners.forEach((listener) => listener(current));
}

function chipState(status) {
  if (!loaded) return { state: "loading", label: "확인 중" };
  if (status.unreachable) return { state: "down", label: "서버 연결 안 됨" };
  if (!status.model.connected) return { state: "down", label: "모델 연결 안 됨" };
  if (Number(status.model.waiting) > 0) return { state: "busy", label: `대기 ${fmtNum(status.model.waiting)}건` };
  return { state: "ok", label: "연결됨" };
}

function render() {
  const chip = $("#statusChip");
  if (!chip) return;
  const status = getStatus();
  const { state, label } = chipState(status);
  chip.dataset.state = state;
  chip.querySelector(".status-label").textContent = label;
  const tooltip = status.model.connected ? status.model.name || "모델" : status.model.error || label;
  chip.setAttribute("aria-label", `모델 상태: ${label}`);
  chip.dataset.tip = tooltip;

  const logout = $("#logoutBtn");
  if (logout) {
    logout.hidden = !status.auth.enabled;
    const user = status.auth.user ? `로그아웃 (${status.auth.user})` : "로그아웃";
    logout.dataset.tip = user;
    logout.setAttribute("aria-label", user);
  }
  if (isPopoverFor(chip)) openDetails(chip);
}

function row(label, value, cls = "") {
  return h("div", { class: `kv-row ${cls}`.trim() }, h("dt", { text: label }), h("dd", { text: value }));
}

function openDetails(chip) {
  const status = getStatus();
  const model = status.model;
  const { label } = chipState(status);
  const dash = "–";
  const list = h(
    "dl",
    { class: "kv" },
    row("상태", label, `kv-state kv-${chip.dataset.state}`),
    row("모델", model.name || dash),
    row("처리 중", model.running == null ? dash : `${fmtNum(model.running)}건`),
    row("대기", model.waiting == null ? dash : `${fmtNum(model.waiting)}건`),
    row("최대 길이", model.max_model_len ? `${fmtNum(model.max_model_len)} 토큰` : dash),
    row("이미지 인식", model.vision ? "지원" : "미지원"),
    status.app_version ? row("버전", status.app_version) : null,
  );
  const content = h(
    "div",
    { class: "status-pop" },
    h("div", { class: "pop-head" }, h("span", { class: "pop-title", text: "모델 서버" })),
    list,
    !model.connected && model.error ? h("p", { class: "pop-error", text: model.error }) : null,
    h(
      "div",
      { class: "pop-actions" },
      h(
        "button",
        {
          type: "button",
          class: "btn btn-sm",
          onclick: async () => {
            await refreshStatus();
            toast("상태를 갱신했습니다", { type: "info", timeout: 1400 });
          },
        },
        "다시 확인",
      ),
    ),
  );
  openPopover({ anchor: chip, content, className: "pop-status", label: "모델 상태", align: "end" });
}

export function initStatus() {
  const chip = $("#statusChip");
  chip?.addEventListener("click", () => {
    if (isPopoverFor(chip)) closePopover();
    else openDetails(chip);
  });
  $("#logoutBtn")?.addEventListener("click", async () => {
    try {
      await api("/api/logout", { method: "POST" });
    } catch {
      /* ignore and continue to login page */
    }
    window.location.assign("/login");
  });
  refreshStatus();
  setInterval(() => {
    if (document.visibilityState === "visible") refreshStatus();
  }, POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshStatus();
  });
}
