import { icon } from "./icons.js";

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

// Minimal element builder. Strings are always inserted as text, never as HTML.
export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "value" && "value" in node) node.value = value;
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

export const store = {
  get(key, fallback) {
    try {
      const raw = window.localStorage.getItem(key);
      return raw == null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
      return true;
    } catch {
      return false;
    }
  },
  remove(key) {
    try {
      window.localStorage.removeItem(key);
    } catch {
      /* ignore */
    }
  },
};

export const fmtNum = (value) => Number(value || 0).toLocaleString("ko-KR");

export function fmtBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(value < 10 * 1024 ? 1 : 0)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function fmtEta(seconds) {
  if (seconds == null || !Number.isFinite(Number(seconds))) return "";
  const sec = Number(seconds);
  if (sec < 40) return "곧 완료";
  if (sec < 90) return "약 1분 남음";
  if (sec < 3600) return `약 ${Math.round(sec / 60)}분 남음`;
  const hours = Math.floor(sec / 3600);
  const minutes = Math.round((sec % 3600) / 60);
  return minutes ? `약 ${hours}시간 ${minutes}분 남음` : `약 ${hours}시간 남음`;
}

export function fmtClock(date) {
  return new Date(date).toLocaleTimeString("ko-KR", { hour: "numeric", minute: "2-digit" });
}

export function fmtWhen(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  if (sameDay) return fmtClock(date);
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return `어제 ${fmtClock(date)}`;
  if (date.getFullYear() === now.getFullYear()) return `${date.getMonth() + 1}월 ${date.getDate()}일`;
  return `${date.getFullYear()}.${date.getMonth() + 1}.${date.getDate()}`;
}

export function fmtRemaining(isoDate) {
  const ms = new Date(isoDate).getTime() - Date.now();
  if (!Number.isFinite(ms)) return "";
  if (ms <= 0) return "곧 삭제";
  const hours = Math.floor(ms / 3600000);
  if (hours >= 1) return `${hours}시간 후 삭제`;
  return `${Math.max(1, Math.round(ms / 60000))}분 후 삭제`;
}

export function stamp(date = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}`;
}

export const uid = () => `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;

export function topDialog() {
  const open = $$("dialog[open]");
  return open.length ? open[open.length - 1] : null;
}

// Clipboard API is unavailable over plain http on a LAN address; fall back to execCommand.
export async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* fall through */
    }
  }
  const host = topDialog() || document.body;
  const previous = document.activeElement;
  const area = h("textarea", {
    readonly: true,
    "aria-hidden": "true",
    style: { position: "fixed", top: "0", left: "0", width: "1px", height: "1px", opacity: "0" },
  });
  area.value = text;
  host.append(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  if (previous && typeof previous.focus === "function") previous.focus({ preventScroll: true });
  return ok;
}

export function downloadBlob(filename, blob) {
  const url = URL.createObjectURL(blob);
  const link = h("a", { href: url, download: filename, style: { display: "none" } });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

export function downloadUrl(url) {
  const link = h("a", { href: url, download: "", style: { display: "none" } });
  document.body.append(link);
  link.click();
  link.remove();
}

function toastRegion() {
  const host = topDialog() || document.body;
  let region = host.querySelector(":scope > .toast-region");
  if (!region) {
    region = h("div", { class: "toast-region", "aria-live": "polite" });
    host.append(region);
  }
  return region;
}

export function toast(message, { type = "ok", timeout = 2400 } = {}) {
  const node = h(
    "div",
    { class: `toast toast-${type}`, role: type === "error" ? "alert" : "status" },
    icon(type === "error" ? "alert" : type === "info" ? "info" : "check", { size: 16 }),
    h("span", { text: message }),
  );
  toastRegion().append(node);
  setTimeout(() => {
    node.classList.add("is-leaving");
    setTimeout(() => node.remove(), 220);
  }, timeout);
}

export function showModal(dialog) {
  if (!dialog.isConnected) document.body.append(dialog);
  if (dialog.open) return;
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

export function closeModal(dialog) {
  if (!dialog.open) return;
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

// Small modal with custom buttons. Resolves with the chosen button id or null.
export function choose({ title, message = "", buttons }) {
  return new Promise((resolve) => {
    let result = null;
    const dialog = h("dialog", { class: "modal modal-sm", "aria-label": title });
    const actions = buttons.map((button) =>
      h(
        "button",
        {
          type: "button",
          class: `btn ${button.variant ? `btn-${button.variant}` : ""}`.trim(),
          onclick: () => {
            result = button.id;
            closeModal(dialog);
          },
        },
        button.label,
      ),
    );
    dialog.append(
      h(
        "div",
        { class: "modal-body confirm-body" },
        h("h2", { class: "confirm-title", text: title }),
        message ? h("p", { class: "confirm-text", text: message }) : null,
      ),
      h("div", { class: "modal-foot" }, h("span", { class: "spacer" }), actions),
    );
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(result);
    });
    showModal(dialog);
    const primary = actions[actions.length - 1];
    primary?.focus();
  });
}

export async function confirmAction({ title, message = "", confirmLabel = "확인", danger = false }) {
  const choice = await choose({
    title,
    message,
    buttons: [
      { id: "cancel", label: "취소" },
      { id: "ok", label: confirmLabel, variant: danger ? "danger" : "primary" },
    ],
  });
  return choice === "ok";
}

export function debounce(fn, wait) {
  let timer = 0;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

export function skeletonLines(count, { lastWidth = 60 } = {}) {
  const wrap = h("span", { class: "sk-wrap", "aria-hidden": "true" });
  for (let index = 0; index < count; index += 1) {
    wrap.append(h("span", { class: "sk", style: { width: index === count - 1 ? `${lastWidth}%` : "100%" } }));
  }
  return wrap;
}

export function isTypingTarget(target) {
  if (!target || !(target instanceof Element)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") {
    const type = (target.getAttribute("type") || "text").toLowerCase();
    return !["checkbox", "radio", "button", "submit", "reset", "file", "range", "color"].includes(type);
  }
  return false;
}
