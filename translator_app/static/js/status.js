import { api } from "./api.js";
import { $, fmtNum, toast } from "./dom.js";

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
let checking = false;
const listeners = new Set();

export const getStatus = () => current || DEFAULT_STATUS;

export function onStatus(listener) {
  listeners.add(listener);
  if (current) listener(current);
}

// An explicit empty document_formats means the document feature is unavailable; only a missing field falls back.
export function mergeStatus(data) {
  return {
    ...DEFAULT_STATUS,
    ...data,
    model: { ...DEFAULT_STATUS.model, ...(data?.model || {}) },
    auth: { ...DEFAULT_STATUS.auth, ...(data?.auth || {}) },
    limits: { ...DEFAULT_STATUS.limits, ...(data?.limits || {}) },
    document_formats: Array.isArray(data?.document_formats) ? data.document_formats : DEFAULT_FORMATS,
  };
}

export async function refreshStatus() {
  try {
    current = mergeStatus(await api("/api/status"));
    current.unreachable = false;
  } catch (error) {
    if (error?.status === 401) return;
    const previous = current || DEFAULT_STATUS;
    current = mergeStatus({
      ...previous,
      model: { ...previous.model, connected: false, error: error?.message || "서버에 연결할 수 없습니다." },
    });
    current.unreachable = true;
  }
  loaded = true;
  render();
  listeners.forEach((listener) => listener(current));
}

// Only the connection state is shown (no model name, context length or version).
function chipState(status) {
  if (!loaded || checking) return { state: "loading", label: "확인 중" };
  if (status.unreachable || !status.model.connected) return { state: "down", label: "연결 안 됨" };
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
  chip.setAttribute("aria-label", `서버 상태: ${label} · 다시 확인`);
  chip.dataset.tip = "다시 확인";

  const logout = $("#logoutBtn");
  if (logout) {
    logout.hidden = !status.auth.enabled;
    const user = status.auth.user ? `로그아웃 (${status.auth.user})` : "로그아웃";
    logout.dataset.tip = user;
    logout.setAttribute("aria-label", user);
  }
}

async function recheck() {
  if (checking) return;
  checking = true;
  render();
  try {
    await refreshStatus();
  } finally {
    checking = false;
    render();
  }
  const { state, label } = chipState(getStatus());
  toast(label, { type: state === "down" ? "error" : "info", timeout: 1600 });
}

export function initStatus() {
  $("#statusChip")?.addEventListener("click", recheck);
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
