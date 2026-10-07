import { h } from "./dom.js";

// One floating popover at a time. Anchors: Element, DOMRect-like object or a function returning a rect.
let active = null;

function rectOf(anchor) {
  if (typeof anchor === "function") return anchor();
  if (anchor instanceof Element) return anchor.getBoundingClientRect();
  return anchor;
}

function place(pop) {
  const rect = rectOf(pop.anchor);
  if (!rect) return;
  const node = pop.el;
  const margin = 8;
  const gap = 8;
  node.style.maxHeight = "";
  node.style.left = "0px";
  node.style.top = "0px";
  const width = node.offsetWidth;
  const height = node.offsetHeight;
  const viewportWidth = document.documentElement.clientWidth;
  const viewportHeight = window.innerHeight;
  let left;
  if (pop.align === "start") left = rect.left;
  else if (pop.align === "end") left = rect.right - width;
  else left = rect.left + rect.width / 2 - width / 2;
  left = Math.max(margin, Math.min(left, viewportWidth - width - margin));
  const below = rect.bottom + gap;
  const above = rect.top - gap - height;
  let top;
  if (pop.placement === "top") top = above >= margin ? above : below;
  else top = below + height <= viewportHeight - margin || above < margin ? below : above;
  if (top + height > viewportHeight - margin) {
    node.style.maxHeight = `${Math.max(140, viewportHeight - margin - top)}px`;
  }
  node.style.left = `${Math.round(left)}px`;
  node.style.top = `${Math.round(Math.max(margin, top))}px`;
}

export function openPopover({
  anchor,
  content,
  className = "",
  label = "",
  placement = "bottom",
  align = "center",
  owner = null,
  onClose = null,
  focus = false,
  role = "dialog",
}) {
  closePopover();
  const ownerElement = owner || (anchor instanceof Element ? anchor : null);
  const host = ownerElement?.closest?.("dialog[open]") || document.body;
  const el = h("div", { class: `popover ${className}`.trim(), role, "aria-label": label || null, tabindex: "-1" });
  if (content) el.append(content);
  host.append(el);
  const pop = {
    el,
    anchor,
    placement,
    align,
    onClose,
    update: () => place(pop),
    close: () => {
      if (active === pop) closePopover();
    },
    setContent(node) {
      el.replaceChildren(node);
      place(pop);
    },
  };
  active = pop;
  place(pop);
  if (focus) {
    const target = el.querySelector("[autofocus]") || el.querySelector("input, textarea, select, button") || el;
    target.focus({ preventScroll: true });
  }
  return pop;
}

export function closePopover({ restoreFocus = false } = {}) {
  if (!active) return;
  const pop = active;
  active = null;
  pop.el.remove();
  pop.onClose?.();
  if (restoreFocus && pop.anchor instanceof Element) pop.anchor.focus({ preventScroll: true });
}

export const currentPopover = () => active;
export const isPopoverFor = (anchor) => Boolean(active && active.anchor === anchor);

document.addEventListener(
  "pointerdown",
  (event) => {
    if (!active) return;
    if (active.el.contains(event.target)) return;
    if (active.anchor instanceof Element && active.anchor.contains(event.target)) return;
    closePopover();
  },
  true,
);

document.addEventListener(
  "keydown",
  (event) => {
    if (event.key !== "Escape" || !active) return;
    event.preventDefault();
    event.stopPropagation();
    closePopover({ restoreFocus: active.el.contains(document.activeElement) });
  },
  true,
);

let frame = 0;
const schedule = () => {
  if (!active || frame) return;
  frame = requestAnimationFrame(() => {
    frame = 0;
    if (active) place(active);
  });
};
window.addEventListener("resize", schedule);
window.addEventListener("scroll", schedule, true);

// Arrow-key navigation inside menu-like popovers.
export function enableListNavigation(container, selector) {
  container.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const items = [...container.querySelectorAll(selector)];
    if (!items.length) return;
    event.preventDefault();
    const index = items.indexOf(document.activeElement);
    const next = event.key === "ArrowDown" ? (index + 1) % items.length : (index - 1 + items.length) % items.length;
    items[index < 0 ? 0 : next].focus();
  });
}
