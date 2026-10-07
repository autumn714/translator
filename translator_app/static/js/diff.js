// Word-level diff (LCS over tokens). CJK ideographs/kana are compared per character.
const TOKEN_RE = /\s+|[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]|[\p{L}\p{M}\p{N}_'’-]+|[^\s]/gu;
const MAX_CELLS = 4_000_000;

export function tokenize(text) {
  return text.match(TOKEN_RE) || [];
}

function lcsOps(a, b) {
  const n = a.length;
  const m = b.length;
  const ops = [];
  if (!n || !m || n * m > MAX_CELLS) {
    if (n) ops.push(["-", a.join("")]);
    if (m) ops.push(["+", b.join("")]);
    return ops;
  }
  const width = m + 1;
  const table = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i * width + j] =
        a[i] === b[j] ? table[(i + 1) * width + j + 1] + 1 : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
    }
  }
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      ops.push(["=", a[i]]);
      i += 1;
      j += 1;
    } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
      ops.push(["-", a[i]]);
      i += 1;
    } else {
      ops.push(["+", b[j]]);
      j += 1;
    }
  }
  while (i < n) ops.push(["-", a[i++]]);
  while (j < m) ops.push(["+", b[j++]]);
  return ops;
}

// Returns a list of parts: {type:"same", text} | {type:"change", removed, added}.
export function diffWords(before, after) {
  const a = tokenize(before);
  const b = tokenize(after);
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start += 1;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA -= 1;
    endB -= 1;
  }
  const ops = [
    ...a.slice(0, start).map((token) => ["=", token]),
    ...lcsOps(a.slice(start, endA), b.slice(start, endB)),
    ...a.slice(endA).map((token) => ["=", token]),
  ];

  // Group changes; whitespace-only equal tokens between two changes join the group.
  const parts = [];
  let index = 0;
  while (index < ops.length) {
    const [kind, text] = ops[index];
    if (kind === "=") {
      const last = parts[parts.length - 1];
      if (last?.type === "same") last.text += text;
      else parts.push({ type: "same", text });
      index += 1;
      continue;
    }
    let removed = "";
    let added = "";
    while (index < ops.length) {
      const [k, t] = ops[index];
      if (k === "-") removed += t;
      else if (k === "+") added += t;
      else if (/^\s+$/.test(t) && ops[index + 1] && ops[index + 1][0] !== "=") {
        removed += t;
        added += t;
      } else break;
      index += 1;
    }
    pushChange(parts, removed, added);
  }
  return parts;
}

// Whitespace shared by both sides at the edges of a change is shown as unchanged text.
function pushChange(parts, removed, added) {
  let head = 0;
  while (head < removed.length && head < added.length && removed[head] === added[head] && /\s/.test(removed[head])) head += 1;
  let tail = 0;
  while (
    tail < removed.length - head &&
    tail < added.length - head &&
    removed[removed.length - 1 - tail] === added[added.length - 1 - tail] &&
    /\s/.test(removed[removed.length - 1 - tail])
  ) {
    tail += 1;
  }
  const same = (text) => {
    if (!text) return;
    const last = parts[parts.length - 1];
    if (last?.type === "same") last.text += text;
    else parts.push({ type: "same", text });
  };
  same(removed.slice(0, head));
  parts.push({ type: "change", removed: removed.slice(head, removed.length - tail), added: added.slice(head, added.length - tail) });
  same(removed.slice(removed.length - tail));
}

export function countChanges(parts) {
  return parts.filter((part) => part.type === "change" && (part.removed.trim() || part.added.trim())).length;
}
