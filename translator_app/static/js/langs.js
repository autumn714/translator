import { h } from "./dom.js";

const FALLBACK_LANGUAGES = [
  { code: "ko", label: "한국어", favorite: true },
  { code: "en", label: "영어", favorite: true },
  { code: "ja", label: "일본어", favorite: true },
  { code: "zh-Hans", label: "중국어(간체)", favorite: true },
  { code: "zh-Hant", label: "중국어(번체)", favorite: true },
];

const injected = Array.isArray(window.TRANSLATOR_LANGUAGES) ? window.TRANSLATOR_LANGUAGES : [];
export const LANGUAGES = injected.length ? injected : FALLBACK_LANGUAGES;
const LANGUAGE_MAP = new Map(LANGUAGES.map((language) => [language.code, language]));

export const AUTO_LABEL = "언어 감지";

export const isLanguage = (code) => LANGUAGE_MAP.has(code);
export const langLabel = (code) => (code === "auto" ? AUTO_LABEL : LANGUAGE_MAP.get(code)?.label ?? code ?? "");

// Fills a <select> with languages: optional "auto" first, then favorites and the rest.
export function fillLangSelect(select, { auto = false, autoLabel = AUTO_LABEL, value } = {}) {
  const favorites = LANGUAGES.filter((language) => language.favorite);
  const others = LANGUAGES.filter((language) => !language.favorite);
  const option = (language) => h("option", { value: language.code }, language.label);
  const nodes = [];
  if (auto) nodes.push(h("option", { value: "auto", "data-auto": "1" }, autoLabel));
  if (favorites.length && others.length) {
    nodes.push(h("optgroup", { label: "자주 사용" }, favorites.map(option)));
    nodes.push(h("optgroup", { label: "전체 언어" }, others.map(option)));
  } else {
    nodes.push(...LANGUAGES.map(option));
  }
  select.replaceChildren(...nodes);
  setSelectValue(select, value, auto ? "auto" : LANGUAGES[0]?.code);
}

export function setSelectValue(select, value, fallback) {
  const has = [...select.options].some((option) => option.value === value);
  select.value = has ? value : fallback ?? select.options[0]?.value ?? "";
}

export function setAutoOptionLabel(select, label) {
  const option = select.querySelector('option[data-auto="1"]');
  if (option && option.textContent !== label) option.textContent = label;
}

const FORMALITY = {
  ko: [
    ["auto", "자동"],
    ["formal", "합니다체"],
    ["informal", "해요체"],
    ["plain", "한다체"],
    ["gaejoshik", "개조식"],
  ],
  ja: [
    ["auto", "자동"],
    ["formal", "정중체"],
    ["plain", "보통체"],
  ],
  other: [
    ["auto", "자동"],
    ["formal", "격식"],
    ["informal", "비격식"],
  ],
};

const FORMALITY_FALLBACK_LABELS = { auto: "자동", formal: "격식", informal: "비격식", plain: "평어", gaejoshik: "개조식" };

// Labels depend on the target language; the server-injected list (language.formality) decides which codes exist.
export function formalityChoices(target) {
  const base = FORMALITY[target] || FORMALITY.other;
  const allowed = LANGUAGE_MAP.get(target)?.formality;
  if (!Array.isArray(allowed) || !allowed.length) return base;
  const labels = new Map(base);
  const choices = allowed.filter((code) => typeof code === "string").map((code) => [code, labels.get(code) || FORMALITY_FALLBACK_LABELS[code] || code]);
  if (!choices.some(([code]) => code === "auto")) choices.unshift(["auto", "자동"]);
  return choices;
}

export function fillFormality(select, target, value) {
  const choices = formalityChoices(target);
  select.replaceChildren(...choices.map(([code, label]) => h("option", { value: code }, label)));
  setSelectValue(select, value, "auto");
}

const SPEECH = { ko: "ko-KR", en: "en-US", ja: "ja-JP", "zh-Hans": "zh-CN", "zh-Hant": "zh-TW", pt: "pt-BR", no: "nb-NO", fil: "fil-PH" };
export const speechLang = (code) => SPEECH[code] || code;

const NO_SPACE = new Set(["ja", "zh-Hans", "zh-Hant", "th"]);
export const unitJoiner = (target) => (NO_SPACE.has(target) ? "" : " ");

// "로" reads naturally after every label in the list (…어, …체)).
export const toLabel = (code) => `${langLabel(code)}로`;
