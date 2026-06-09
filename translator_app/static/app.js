const LANGUAGES = [
  { code: "ko", label: "한국어", favorite: true },
  { code: "en", label: "영어", favorite: true },
  { code: "ja", label: "일본어", favorite: true },
  { code: "zh-Hans", label: "중국어(간체)", favorite: true },
  { code: "zh-Hant", label: "중국어(번체)", favorite: true },
  { code: "ar", label: "아랍어", favorite: false },
  { code: "bg", label: "불가리아어", favorite: false },
  { code: "bn", label: "벵골어", favorite: false },
  { code: "cs", label: "체코어", favorite: false },
  { code: "da", label: "덴마크어", favorite: false },
  { code: "de", label: "독일어", favorite: false },
  { code: "el", label: "그리스어", favorite: false },
  { code: "es", label: "스페인어", favorite: false },
  { code: "et", label: "에스토니아어", favorite: false },
  { code: "fa", label: "페르시아어", favorite: false },
  { code: "fi", label: "핀란드어", favorite: false },
  { code: "fil", label: "필리핀어", favorite: false },
  { code: "fr", label: "프랑스어", favorite: false },
  { code: "he", label: "히브리어", favorite: false },
  { code: "hi", label: "힌디어", favorite: false },
  { code: "hr", label: "크로아티아어", favorite: false },
  { code: "hu", label: "헝가리어", favorite: false },
  { code: "id", label: "인도네시아어", favorite: false },
  { code: "it", label: "이탈리아어", favorite: false },
  { code: "lt", label: "리투아니아어", favorite: false },
  { code: "lv", label: "라트비아어", favorite: false },
  { code: "ms", label: "말레이어", favorite: false },
  { code: "nl", label: "네덜란드어", favorite: false },
  { code: "no", label: "노르웨이어", favorite: false },
  { code: "pl", label: "폴란드어", favorite: false },
  { code: "pt", label: "포르투갈어", favorite: false },
  { code: "ro", label: "루마니아어", favorite: false },
  { code: "ru", label: "러시아어", favorite: false },
  { code: "sk", label: "슬로바키아어", favorite: false },
  { code: "sl", label: "슬로베니아어", favorite: false },
  { code: "sr", label: "세르비아어", favorite: false },
  { code: "sv", label: "스웨덴어", favorite: false },
  { code: "sw", label: "스와힐리어", favorite: false },
  { code: "th", label: "태국어", favorite: false },
  { code: "tr", label: "튀르키예어", favorite: false },
  { code: "uk", label: "우크라이나어", favorite: false },
  { code: "vi", label: "베트남어", favorite: false },
];

const SERVER_LANGUAGES = Array.isArray(window.TRANSLATOR_LANGUAGES) ? window.TRANSLATOR_LANGUAGES : null;
if (SERVER_LANGUAGES?.length) {
  LANGUAGES.splice(0, LANGUAGES.length, ...SERVER_LANGUAGES);
}

const FAVORITE_LANGUAGE_CODES = LANGUAGES.filter((item) => item.favorite).map((item) => item.code);
const DEFAULT_TARGET_LANGUAGE = "ko";
const SWAP_AUTO_SHARE_THRESHOLD = 70;
const RETRANSLATE_LANGUAGE_PREFIX = "__retranslate__:";

const SOURCE_MIN_HEIGHT = 320;
const UI_PHASES = {
  IDLE: "idle",
  WAITING: "waiting",
  TRANSLATING: "translating",
  READY: "ready",
  ERROR: "error",
};

const sourcePanel = document.querySelector("#sourcePanel");
const targetPanel = document.querySelector("#targetPanel");
const sourceText = document.querySelector("#sourceText");
const translationText = document.querySelector("#translationText");
const segmentList = document.querySelector("#segmentList");
const segmentsBody = document.querySelector("#segmentsBody");
const toggleSegmentsButton = document.querySelector("#toggleSegments");
const statusBadge = document.querySelector("#statusBadge");
const engineBadge = document.querySelector("#engineBadge");
const latencyText = document.querySelector("#latencyText");
const sourceCount = document.querySelector("#sourceCount");
const targetPanelTitle = document.querySelector("#targetPanelTitle");
const translationActivity = document.querySelector("#translationActivity");
const translationActivityText = document.querySelector("#translationActivityText");
const targetLanguage = document.querySelector("#targetLanguage");
const swapButton = document.querySelector("#swapButton");
const titleResetButton = document.querySelector("#titleResetButton");
const useGlossary = document.querySelector("#useGlossary");
const glossarySelect = document.querySelector("#glossarySelect");
const glossaryTableBody = document.querySelector("#glossaryTableBody");
const addGlossaryRowButton = document.querySelector("#addGlossaryRow");
const saveGlossaryButton = document.querySelector("#saveGlossary");
const deleteGlossaryButton = document.querySelector("#deleteGlossaryButton");
const newGlossaryButton = document.querySelector("#newGlossaryButton");
const glossaryMeta = document.querySelector("#glossaryMeta");
const glossaryNotice = document.querySelector("#glossaryNotice");
const glossaryHits = document.querySelector("#glossaryHits");

const glossaryModal = document.querySelector("#glossaryModal");
const glossaryModalForm = document.querySelector("#glossaryModalForm");
const closeGlossaryModalButton = document.querySelector("#closeGlossaryModal");
const newGlossaryName = document.querySelector("#newGlossaryName");
const newGlossaryTableBody = document.querySelector("#newGlossaryTableBody");
const addNewGlossaryRowButton = document.querySelector("#addNewGlossaryRow");
const glossaryModalNotice = document.querySelector("#glossaryModalNotice");

const swapModal = document.querySelector("#swapModal");
const closeSwapModalButton = document.querySelector("#closeSwapModal");
const swapModalCopy = document.querySelector("#swapModalCopy");
const swapDetectedLanguages = document.querySelector("#swapDetectedLanguages");
const swapModalNotice = document.querySelector("#swapModalNotice");

const state = {
  debounceHandle: null,
  activeController: null,
  requestCounter: 0,
  manualSourceHeight: null,
  pointerDownHeight: SOURCE_MIN_HEIGHT,
  uiPhase: UI_PHASES.IDLE,
  lastTranslation: null,
  swapContext: null,
  glossaries: [],
  activeGlossaryId: null,
  activeGlossary: null,
};

function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function languageLabel(code) {
  return LANGUAGES.find((item) => item.code === code)?.label ?? code;
}

function pairLabel(sourceLang, targetLang) {
  return `${languageLabel(sourceLang)} → ${languageLabel(targetLang)}`;
}

function languageOptions(selectedCode, { excludeCode = null, grouped = false, retranslateCode = null } = {}) {
  const available = LANGUAGES.filter((language) => language.code !== excludeCode);
  const retranslateOption = retranslateCode
    ? `<option value="${RETRANSLATE_LANGUAGE_PREFIX}${retranslateCode}">${languageLabel(retranslateCode)} (현재 언어로 다시 번역)</option>`
    : "";
  if (!grouped) {
    return (
      retranslateOption +
      available
        .map(
          (language) =>
            `<option value="${language.code}" ${language.code === selectedCode ? "selected" : ""}>${language.label}</option>`,
        )
        .join("")
    );
  }

  const favorites = available.filter((language) => FAVORITE_LANGUAGE_CODES.includes(language.code));
  const others = available.filter((language) => !FAVORITE_LANGUAGE_CODES.includes(language.code));

  const renderGroup = (label, items) => {
    if (!items.length) {
      return "";
    }
    const options = items
      .map(
        (language) =>
          `<option value="${language.code}" ${language.code === selectedCode ? "selected" : ""}>${language.label}</option>`,
      )
      .join("");
    return `<optgroup label="${label}">${options}</optgroup>`;
  };

  return `${retranslateOption}${renderGroup("자주 사용하는 언어", favorites)}${renderGroup("전체 지원 언어", others)}`;
}

function setStatus(text) {
  statusBadge.textContent = text;
}

function setUiPhase(phase) {
  state.uiPhase = phase;
  sourcePanel.classList.remove("is-waiting", "is-translating");
  targetPanel.classList.remove("is-waiting", "is-translating");
  translationActivity.hidden = true;
  translationActivity.setAttribute("aria-hidden", "true");
  translationActivityText.textContent = "";

  if (phase === UI_PHASES.WAITING) {
    sourcePanel.classList.add("is-waiting");
    targetPanel.classList.add("is-waiting");
    translationActivity.hidden = false;
    translationActivity.setAttribute("aria-hidden", "false");
    translationActivityText.textContent = "입력 반영 대기";
  } else if (phase === UI_PHASES.TRANSLATING) {
    sourcePanel.classList.add("is-translating");
    targetPanel.classList.add("is-translating");
    translationActivity.hidden = false;
    translationActivity.setAttribute("aria-hidden", "false");
    translationActivityText.textContent = "번역 중";
  }

  updateSwapAvailability();
}

function setSegmentsOpen(open) {
  segmentsBody.hidden = !open;
  toggleSegmentsButton.textContent = open ? "닫기" : "열기";
  toggleSegmentsButton.setAttribute("aria-expanded", String(open));
}

function clearSegments(message = "구간별 결과가 여기에 표시됩니다.") {
  segmentList.innerHTML = `<div class="placeholder">${escapeHtml(message)}</div>`;
}

function clearGlossaryHits(message = "현재 입력에 적용된 용어집 항목이 없습니다.") {
  glossaryHits.innerHTML = `<div class="placeholder">${escapeHtml(message)}</div>`;
}

function clearLastTranslation() {
  state.lastTranslation = null;
  updateSwapAvailability();
}

function clearSwapContext() {
  state.swapContext = null;
}

function cloneDetectedLanguages(languages = []) {
  return languages.map((item) => ({
    code: item.code,
    char_count: item.char_count,
    share: item.share,
  }));
}

function normalizeLineBreaks(text) {
  return String(text ?? "").replace(/\r\n?/g, "\n");
}

function splitParagraphs(text) {
  const normalized = normalizeLineBreaks(text).trim();
  if (!normalized) {
    return [];
  }

  return normalized
    .split(/\n{2,}/)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean);
}

function buildParagraphList(text) {
  const paragraphs = splitParagraphs(text);
  if (paragraphs.length) {
    return paragraphs;
  }

  const normalized = normalizeLineBreaks(text).trim();
  return normalized ? [normalized] : [];
}

function joinParagraphs(paragraphs) {
  return paragraphs.filter((paragraph) => paragraph.trim()).join("\n\n");
}

function sameParagraphs(left, right) {
  if (left.length !== right.length) {
    return false;
  }

  return left.every((paragraph, index) => paragraph === right[index]);
}

function createSyntheticSegments(sourceParagraphs, targetParagraphs) {
  const length = Math.max(sourceParagraphs.length, targetParagraphs.length);
  return Array.from({ length }, (_, index) => ({
    source: sourceParagraphs[index] ?? "",
    target: targetParagraphs[index] ?? "",
  }));
}

function buildDetectedLanguageSnapshot(code, charCount) {
  return [
    {
      code,
      char_count: charCount,
      share: 1,
    },
  ];
}

function buildSyntheticResult({
  translation,
  segments,
  sourceLanguageMode,
  primarySourceLang,
  detectedSourceLanguages,
  engine,
}) {
  return {
    translation,
    segments,
    glossary_hits: [],
    glossary_applied: false,
    glossary_name: null,
    detected_source_languages: cloneDetectedLanguages(detectedSourceLanguages),
    primary_source_lang: primarySourceLang,
    source_language_mode: sourceLanguageMode,
    engine,
    latency_ms: 0,
  };
}

function renderIdle() {
  clearSwapContext();
  syncTargetLanguageOptions(targetLanguage.value || DEFAULT_TARGET_LANGUAGE);
  clearLastTranslation();
  translationText.textContent = "번역 결과가 여기에 표시됩니다.";
  translationText.classList.add("empty");
  latencyText.textContent = "-";
  clearSegments();
  clearGlossaryHits();
  setUiPhase(UI_PHASES.IDLE);
}

function renderTranslation(result, requestText) {
  translationText.textContent = result.translation || "번역 결과가 없습니다.";
  translationText.classList.toggle("empty", !result.translation);
  engineBadge.textContent = `engine: ${result.engine}`;
  latencyText.textContent = `${result.latency_ms} ms`;

  state.lastTranslation = {
    sourceText: requestText,
    translation: result.translation,
    targetLang: targetLanguage.value,
    sourceLanguageMode: result.source_language_mode,
    primarySourceLang: result.primary_source_lang,
    detectedSourceLanguages: result.detected_source_languages ?? [],
    segments: result.segments ?? [],
    engine: result.engine,
  };
  updateSwapAvailability();

  if (!result.segments.length) {
    clearSegments();
  } else {
    segmentList.innerHTML = result.segments
      .map(
        (segment) => `
          <article class="segment-card">
            <div class="segment-box source">${escapeHtml(segment.source)}</div>
            <div class="segment-box target">${escapeHtml(segment.target)}</div>
          </article>
        `,
      )
      .join("");
  }

  if (!useGlossary.checked) {
    clearGlossaryHits("용어집 사용이 꺼져 있습니다.");
    return;
  }

  if (!state.activeGlossary) {
    clearGlossaryHits("선택된 용어집이 없습니다.");
    return;
  }

  if (!result.glossary_hits.length) {
    clearGlossaryHits("현재 번역 언어에 맞는 용어집 항목이 적용되지 않았습니다.");
    return;
  }

  glossaryHits.innerHTML = result.glossary_hits
    .map(
      (entry) =>
        `<span class="glossary-chip">${pairLabel(entry.source_lang, entry.target_lang)} | ${escapeHtml(entry.source)} → ${escapeHtml(entry.target)}</span>`,
    )
    .join("");
}

function renderSwapWorkspace(context) {
  const sourceValue = joinParagraphs(context.sourceParagraphs);
  const outputValue = joinParagraphs(context.outputParagraphs);

  renderTranslation(
    buildSyntheticResult({
      translation: outputValue,
      segments: createSyntheticSegments(context.sourceParagraphs, context.outputParagraphs),
      sourceLanguageMode: "single",
      primarySourceLang: context.sourceLang,
      detectedSourceLanguages: buildDetectedLanguageSnapshot(context.sourceLang, sourceValue.length),
      engine: context.engine,
    }),
    sourceValue,
  );
}

function updateSourceCount() {
  sourceCount.textContent = `${sourceText.value.length.toLocaleString("ko-KR")}자`;
}

function adjustSourceHeight() {
  sourceText.style.height = "auto";
  const contentHeight = Math.max(SOURCE_MIN_HEIGHT, sourceText.scrollHeight + 2);
  const nextHeight = state.manualSourceHeight
    ? Math.max(contentHeight, state.manualSourceHeight)
    : contentHeight;
  sourceText.style.height = `${nextHeight}px`;
}

function captureManualSourceHeight() {
  const currentHeight = sourceText.offsetHeight;
  if (Math.abs(currentHeight - state.pointerDownHeight) > 6) {
    state.manualSourceHeight = Math.max(SOURCE_MIN_HEIGHT, currentHeight);
  }
}

function updateTargetUI() {
  const targetCode = targetLanguage.value;
  targetPanelTitle.textContent = `${languageLabel(targetCode)} 번역`;
  sourceText.placeholder = `텍스트를 입력하면 ${languageLabel(targetCode)}로 자동 번역됩니다.`;
  glossaryMetaText();
  updateSwapAvailability();
}

function syncTargetLanguageOptions(selectedCode = DEFAULT_TARGET_LANGUAGE) {
  targetLanguage.innerHTML = languageOptions(selectedCode, {
    grouped: true,
    retranslateCode: state.swapContext ? selectedCode : null,
  });
  targetLanguage.value = selectedCode;
}

function setInitialTargetLanguage() {
  syncTargetLanguageOptions(DEFAULT_TARGET_LANGUAGE);
  updateTargetUI();
}

function persistTargetLanguage() {
  return;
}

function resetWorkspace() {
  if (state.debounceHandle) {
    clearTimeout(state.debounceHandle);
    state.debounceHandle = null;
  }
  if (state.activeController) {
    state.activeController.abort();
    state.activeController = null;
  }
  clearSwapContext();
  window.localStorage.removeItem("translator.targetLang");
  window.location.assign(window.location.pathname);
}

function tryReuseTranslationForTarget(targetCode) {
  const translation = state.lastTranslation;
  const currentSource = sourceText.value.trim();
  if (!translation || !currentSource) {
    return false;
  }
  if (translation.sourceText.trim() !== currentSource) {
    return false;
  }

  if (translation.targetLang === targetCode) {
    renderTranslation(
      buildSyntheticResult({
        translation: translation.translation,
        segments: translation.segments,
        sourceLanguageMode: translation.sourceLanguageMode,
        primarySourceLang: translation.primarySourceLang,
        detectedSourceLanguages: translation.detectedSourceLanguages,
        engine: translation.engine,
      }),
      currentSource,
    );
    setStatus("준비 완료");
    setUiPhase(UI_PHASES.READY);
    return true;
  }

  if (translation.sourceLanguageMode !== "single" || translation.primarySourceLang !== targetCode) {
    return false;
  }

  const identitySegments = (translation.segments ?? []).length
    ? translation.segments.map((segment) => ({
        source: segment.source,
        target: segment.source,
      }))
    : [{ source: currentSource, target: currentSource }];

  renderTranslation(
    buildSyntheticResult({
      translation: currentSource,
      segments: identitySegments,
      sourceLanguageMode: "single",
      primarySourceLang: targetCode,
      detectedSourceLanguages: [
        {
          code: targetCode,
          char_count: currentSource.length,
          share: 1,
        },
      ],
      engine: translation.engine,
    }),
    currentSource,
  );
  setStatus("준비 완료");
  setUiPhase(UI_PHASES.READY);
  return true;
}

function createRowMarkup(entry = {}) {
  const sourceLang = entry.source_lang ?? "en";
  const targetLang = entry.target_lang ?? targetLanguage.value;
  const source = entry.source ?? "";
  const target = entry.target ?? "";
  const note = entry.note ?? "";
  const enabled = entry.enabled ?? true;

  return `
    <tr>
      <td><input data-field="enabled" type="checkbox" ${enabled ? "checked" : ""} /></td>
      <td><select data-field="source_lang" class="table-select">${languageOptions(sourceLang)}</select></td>
      <td><select data-field="target_lang" class="table-select">${languageOptions(targetLang)}</select></td>
      <td><input data-field="source" type="text" value="${escapeHtml(source)}" /></td>
      <td><input data-field="target" type="text" value="${escapeHtml(target)}" /></td>
      <td><input data-field="note" type="text" value="${escapeHtml(note)}" /></td>
      <td><button class="icon-button" data-action="remove" type="button">삭제</button></td>
    </tr>
  `;
}

function renderGlossaryRows(targetBody, entries) {
  if (!entries.length) {
    targetBody.innerHTML = createRowMarkup();
    return;
  }
  targetBody.innerHTML = entries.map((entry) => createRowMarkup(entry)).join("");
}

function addGlossaryRow(targetBody) {
  targetBody.insertAdjacentHTML("beforeend", createRowMarkup());
}

function readGlossaryRows(targetBody) {
  return [...targetBody.querySelectorAll("tr")]
    .map((row) => ({
      enabled: row.querySelector('[data-field="enabled"]').checked,
      source_lang: row.querySelector('[data-field="source_lang"]').value,
      target_lang: row.querySelector('[data-field="target_lang"]').value,
      source: row.querySelector('[data-field="source"]').value.trim(),
      target: row.querySelector('[data-field="target"]').value.trim(),
      note: row.querySelector('[data-field="note"]').value.trim(),
    }))
    .filter((entry) => entry.source && entry.target);
}

function getGlossaryEntriesForText(text, targetCode = targetLanguage.value) {
  if (!useGlossary.checked || !state.activeGlossaryId) {
    return null;
  }

  const loweredSourceText = text.toLowerCase();
  return readGlossaryRows(glossaryTableBody).filter(
    (entry) =>
      entry.enabled &&
      entry.target_lang === targetCode &&
      loweredSourceText.includes(entry.source.toLowerCase()),
  );
}

function getActiveGlossaryEntriesForTranslation() {
  return getGlossaryEntriesForText(sourceText.value);
}

function scheduleGlossaryRefresh({ force = false } = {}) {
  glossaryMetaText();
  if (sourceText.value.trim() && (force || useGlossary.checked)) {
    scheduleTranslation();
  } else if (!useGlossary.checked) {
    clearGlossaryHits("용어집 사용이 꺼져 있습니다.");
  } else {
    clearGlossaryHits();
  }
}

function syncGlossaryActionState() {
  const disabled = !state.activeGlossary;
  saveGlossaryButton.disabled = disabled;
  deleteGlossaryButton.disabled = disabled;
}

function renderGlossarySelect() {
  if (!state.glossaries.length) {
    glossarySelect.innerHTML = `<option value="">용어집 없음</option>`;
    glossarySelect.disabled = true;
    syncGlossaryActionState();
    return;
  }

  glossarySelect.disabled = false;
  glossarySelect.innerHTML = state.glossaries
    .map(
      (glossary) => `
        <option value="${glossary.id}" ${glossary.id === state.activeGlossaryId ? "selected" : ""}>
          ${escapeHtml(glossary.name)} (${glossary.entry_count}개)
        </option>
      `,
    )
    .join("");
  syncGlossaryActionState();
}

function glossaryMetaText() {
  if (!state.activeGlossary) {
    glossaryMeta.textContent = "선택된 용어집이 없습니다.";
    return;
  }

  const matchingCount = state.activeGlossary.entries.filter(
    (entry) => entry.target_lang === targetLanguage.value,
  ).length;
  glossaryMeta.textContent = `${state.activeGlossary.name} | 전체 ${state.activeGlossary.entries.length}개 | 현재 번역 언어(${languageLabel(targetLanguage.value)}) 적용 가능 ${matchingCount}개`;
}

function resolveActiveGlossaryId(defaultGlossaryId) {
  const savedId = window.localStorage.getItem("translator.activeGlossaryId");
  const existingIds = new Set(state.glossaries.map((item) => item.id));

  if (state.activeGlossaryId && existingIds.has(state.activeGlossaryId)) {
    return state.activeGlossaryId;
  }
  if (savedId && existingIds.has(savedId)) {
    return savedId;
  }
  if (defaultGlossaryId && existingIds.has(defaultGlossaryId)) {
    return defaultGlossaryId;
  }
  return state.glossaries[0]?.id ?? null;
}

async function loadGlossarySummaries() {
  glossaryNotice.textContent = "용어집 목록을 불러오는 중입니다.";
  const response = await fetch("/api/glossaries");
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  const data = await response.json();
  state.glossaries = data.glossaries;
  state.activeGlossaryId = resolveActiveGlossaryId(data.default_glossary_id);
  renderGlossarySelect();
}

async function loadActiveGlossary() {
  if (!state.activeGlossaryId) {
    state.activeGlossary = null;
    renderGlossaryRows(glossaryTableBody, []);
    glossaryMetaText();
    glossaryNotice.textContent = "선택된 용어집이 없습니다.";
    syncGlossaryActionState();
    return;
  }

  glossaryNotice.textContent = "용어집을 불러오는 중입니다.";
  const response = await fetch(`/api/glossaries/${encodeURIComponent(state.activeGlossaryId)}`);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  const data = await response.json();
  state.activeGlossary = data.glossary;
  window.localStorage.setItem("translator.activeGlossaryId", state.activeGlossary.id);
  renderGlossaryRows(glossaryTableBody, state.activeGlossary.entries);
  glossaryMetaText();
  glossaryNotice.textContent = `용어집을 불러왔습니다: ${state.activeGlossary.name}`;
  syncGlossaryActionState();
}

async function refreshGlossaries() {
  try {
    await loadGlossarySummaries();
    await loadActiveGlossary();
    if (sourceText.value.trim()) {
      scheduleTranslation();
    }
  } catch (error) {
    state.activeGlossary = null;
    renderGlossaryRows(glossaryTableBody, []);
    glossaryMeta.textContent = "용어집 정보를 불러오지 못했습니다.";
    glossaryNotice.textContent = `용어집 로드 실패: ${error.message}`;
    syncGlossaryActionState();
  }
}

async function saveCurrentGlossary() {
  if (!state.activeGlossary) {
    glossaryNotice.textContent = "저장할 용어집이 없습니다.";
    return;
  }

  const response = await fetch(`/api/glossaries/${encodeURIComponent(state.activeGlossary.id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: state.activeGlossary.name,
      entries: readGlossaryRows(glossaryTableBody),
    }),
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  const data = await response.json();
  state.activeGlossary = data.glossary;
  renderGlossaryRows(glossaryTableBody, state.activeGlossary.entries);
  glossaryMetaText();
  glossaryNotice.textContent = `저장을 완료했습니다: ${state.activeGlossary.entries.length}개 항목`;
}

async function deleteCurrentGlossary() {
  if (!state.activeGlossary) {
    glossaryNotice.textContent = "삭제할 용어집이 없습니다.";
    return;
  }

  const glossaryName = state.activeGlossary.name;
  const confirmed = window.confirm(`"${glossaryName}" 용어집을 삭제하시겠습니까?`);
  if (!confirmed) {
    return;
  }

  const response = await fetch(`/api/glossaries/${encodeURIComponent(state.activeGlossary.id)}`, {
    method: "DELETE",
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  window.localStorage.removeItem("translator.activeGlossaryId");
  state.activeGlossaryId = null;
  state.activeGlossary = null;
  await refreshGlossaries();
  glossaryNotice.textContent = `용어집을 삭제했습니다: ${glossaryName}`;
}

function openGlossaryModal() {
  glossaryModalNotice.textContent = "";
  newGlossaryName.value = "";
  renderGlossaryRows(newGlossaryTableBody, []);

  if (typeof glossaryModal.showModal === "function") {
    glossaryModal.showModal();
  } else {
    glossaryModal.setAttribute("open", "");
  }
}

function closeGlossaryModal() {
  if (typeof glossaryModal.close === "function") {
    glossaryModal.close();
  } else {
    glossaryModal.removeAttribute("open");
  }
}

async function createGlossaryFromModal() {
  const name = newGlossaryName.value.trim();
  const entries = readGlossaryRows(newGlossaryTableBody);

  if (!name) {
    glossaryModalNotice.textContent = "용어집 이름을 입력하세요.";
    return;
  }

  const response = await fetch("/api/glossaries", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, entries }),
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  const data = await response.json();
  state.activeGlossaryId = data.glossary.id;
  closeGlossaryModal();
  await refreshGlossaries();
  glossaryNotice.textContent = `새 용어집을 만들었습니다: ${data.glossary.name}`;
}

function getVisibleDetectedShare(item) {
  return Math.round((item.share ?? 0) * 1000) / 10;
}

function formatDetectedShare(item) {
  const value = getVisibleDetectedShare(item);
  return Number.isInteger(value) ? `${value.toFixed(0)}%` : `${value.toFixed(1)}%`;
}

function getSwapLanguageOptions(lastTranslation) {
  const currentTarget = lastTranslation.targetLang;
  return (lastTranslation.detectedSourceLanguages ?? []).filter(
    (item, index, items) =>
      item.code !== currentTarget &&
      getVisibleDetectedShare(item) > 0 &&
      items.findIndex((candidate) => candidate.code === item.code) === index &&
      LANGUAGES.some((language) => language.code === item.code),
  );
}

function resolvePreferredSwapLanguage(lastTranslation) {
  const primary = getSwapLanguageOptions(lastTranslation).find(
    (item) => getVisibleDetectedShare(item) > SWAP_AUTO_SHARE_THRESHOLD,
  );
  return primary?.code ?? null;
}

function updateSwapAvailability() {
  const translation = state.lastTranslation;
  const busy = state.uiPhase === UI_PHASES.WAITING || state.uiPhase === UI_PHASES.TRANSLATING;
  const interactive = !busy && !!translation && !!translation.translation?.trim();

  swapButton.classList.toggle("is-inactive", !interactive);
  swapButton.setAttribute("aria-disabled", String(!interactive));

  if (busy) {
    swapButton.title = "번역이 진행 중일 때는 전환할 수 없습니다.";
    return;
  }

  if (!translation) {
    swapButton.title = "번역 결과가 있어야 전환할 수 있습니다.";
    return;
  }

  const preferredTarget = resolvePreferredSwapLanguage(translation);
  if (preferredTarget) {
    swapButton.title = `새 목표 언어: ${languageLabel(preferredTarget)}`;
    return;
  }

  if (getSwapLanguageOptions(translation).length) {
    swapButton.title = "감지된 원문 언어 중에서 새 목표 언어를 선택합니다.";
    return;
  }

  swapButton.title = "전환 가능한 원문 언어를 찾지 못했습니다.";
}

function renderSwapDetectedLanguages(lastTranslation) {
  const detected = getSwapLanguageOptions(lastTranslation);
  if (!detected.length) {
    swapDetectedLanguages.innerHTML = `<span class="placeholder">전환 가능한 원문 언어가 없습니다.</span>`;
    return 0;
  }

  swapDetectedLanguages.innerHTML = detected
    .map(
      (item) =>
        `<button class="swap-language-option" type="button" data-swap-lang="${item.code}">
          <span>${languageLabel(item.code)}</span>
          <span class="swap-language-share">${formatDetectedShare(item)}</span>
        </button>`,
    )
    .join("");
  return detected.length;
}

function openSwapModal() {
  const translation = state.lastTranslation;
  if (!translation) {
    return;
  }

  const optionCount = renderSwapDetectedLanguages(translation);
  swapModalNotice.textContent = optionCount ? "" : "전환할 원문 언어를 찾지 못했습니다.";
  swapModalCopy.textContent =
    "자동 전환 기준을 충족하지 않아 감지된 원문 언어만 표시합니다. 전환할 언어를 바로 클릭하세요.";

  if (typeof swapModal.showModal === "function") {
    swapModal.showModal();
  } else {
    swapModal.setAttribute("open", "");
  }
}

function closeSwapModal() {
  if (typeof swapModal.close === "function") {
    swapModal.close();
  } else {
    swapModal.removeAttribute("open");
  }
}

function preserveViewportPosition(callback) {
  const scrollX = window.scrollX;
  const scrollY = window.scrollY;

  callback();

  window.requestAnimationFrame(() => {
    window.scrollTo(scrollX, scrollY);
    window.requestAnimationFrame(() => {
      window.scrollTo(scrollX, scrollY);
    });
  });
}

function buildSwapContext(translation, nextTargetLang) {
  const nextSourceText = normalizeLineBreaks(translation.translation ?? "").trim();
  const nextOutputText = normalizeLineBreaks(translation.sourceText ?? nextSourceText).trim();

  let sourceParagraphs = buildParagraphList(nextSourceText);
  let outputParagraphs = buildParagraphList(nextOutputText);

  if (sourceParagraphs.length !== outputParagraphs.length) {
    sourceParagraphs = nextSourceText ? [nextSourceText] : [];
    outputParagraphs = nextOutputText ? [nextOutputText] : [];
  }

  return {
    sourceLang: translation.targetLang,
    targetLang: nextTargetLang,
    sourceParagraphs,
    outputParagraphs,
    engine: translation.engine,
  };
}

function performSwap(nextTargetLang) {
  const translation = state.lastTranslation;
  if (!translation || !translation.translation?.trim()) {
    return;
  }
  if (!LANGUAGES.some((language) => language.code === nextTargetLang)) {
    swapModalNotice.textContent = "지원되지 않는 목표 언어입니다.";
    return;
  }

  const nextContext = buildSwapContext(translation, nextTargetLang);
  const nextSourceText = joinParagraphs(nextContext.sourceParagraphs);

  preserveViewportPosition(() => {
    targetLanguage.value = nextTargetLang;
    state.swapContext = nextContext;
    syncTargetLanguageOptions(nextTargetLang);
    persistTargetLanguage();
    updateTargetUI();

    sourceText.value = nextSourceText;
    state.manualSourceHeight = null;
    updateSourceCount();
    adjustSourceHeight();

    renderSwapWorkspace(nextContext);
    setStatus("준비 완료");
    setUiPhase(UI_PHASES.READY);
    closeSwapModal();
  });
}

function handleSwap() {
  const translation = state.lastTranslation;
  const busy = state.uiPhase === UI_PHASES.WAITING || state.uiPhase === UI_PHASES.TRANSLATING;
  if (busy) {
    setStatus("번역 중");
    return;
  }

  if (!translation) {
    setStatus("최근 번역 결과 없음");
    return;
  }

  const preferredTarget = resolvePreferredSwapLanguage(translation);
  if (preferredTarget) {
    performSwap(preferredTarget);
    return;
  }

  openSwapModal();
}

async function requestTranslation(
  text,
  { signal, sourceLang = "auto", targetLangOverride = targetLanguage.value } = {},
) {
  const response = await fetch("/api/translate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text,
      source_lang: sourceLang,
      target_lang: targetLangOverride,
      use_glossary: useGlossary.checked,
      glossary_id: state.activeGlossaryId,
      glossary_entries: getGlossaryEntriesForText(text, targetLangOverride),
    }),
    signal,
  });

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const payload = await response.json();
      if (payload?.detail) {
        detail = payload.detail;
      }
    } catch {
      // Ignore JSON parsing failure.
    }
    throw new Error(detail);
  }

  return response.json();
}

function getParagraphDiff(previousParagraphs, nextParagraphs) {
  if (sameParagraphs(previousParagraphs, nextParagraphs)) {
    return {
      changedIndices: [],
      start: 0,
      previousEnd: 0,
      nextEnd: 0,
    };
  }

  if (previousParagraphs.length === nextParagraphs.length) {
    return {
      changedIndices: nextParagraphs
        .map((paragraph, index) => (paragraph === previousParagraphs[index] ? null : index))
        .filter((index) => index !== null),
      start: 0,
      previousEnd: previousParagraphs.length,
      nextEnd: nextParagraphs.length,
    };
  }

  let start = 0;
  while (
    start < previousParagraphs.length &&
    start < nextParagraphs.length &&
    previousParagraphs[start] === nextParagraphs[start]
  ) {
    start += 1;
  }

  let previousEnd = previousParagraphs.length;
  let nextEnd = nextParagraphs.length;
  while (
    previousEnd > start &&
    nextEnd > start &&
    previousParagraphs[previousEnd - 1] === nextParagraphs[nextEnd - 1]
  ) {
    previousEnd -= 1;
    nextEnd -= 1;
  }

  return {
    changedIndices: null,
    start,
    previousEnd,
    nextEnd,
  };
}

async function runSwapTranslation(text, controller, currentRequest) {
  const swapContext = state.swapContext;
  if (!swapContext) {
    return false;
  }

  const nextSourceParagraphs = buildParagraphList(text);
  const diff = getParagraphDiff(swapContext.sourceParagraphs, nextSourceParagraphs);

  if (Array.isArray(diff.changedIndices) && diff.changedIndices.length === 0) {
    state.swapContext = {
      ...swapContext,
      targetLang: targetLanguage.value,
    };
    renderSwapWorkspace(state.swapContext);
    return true;
  }

  const nextOutputParagraphs = [...swapContext.outputParagraphs];

  if (Array.isArray(diff.changedIndices)) {
    const results = await Promise.all(
      diff.changedIndices.map((index) =>
        requestTranslation(nextSourceParagraphs[index], {
          signal: controller.signal,
          sourceLang: swapContext.sourceLang,
          targetLangOverride: targetLanguage.value,
        }),
      ),
    );

    if (currentRequest !== state.requestCounter) {
      return false;
    }

    diff.changedIndices.forEach((index, resultIndex) => {
      nextOutputParagraphs[index] = results[resultIndex].translation;
    });
  } else {
    const replacementParagraphs = nextSourceParagraphs.slice(diff.start, diff.nextEnd);
    const results = await Promise.all(
      replacementParagraphs.map((paragraph) =>
        requestTranslation(paragraph, {
          signal: controller.signal,
          sourceLang: swapContext.sourceLang,
          targetLangOverride: targetLanguage.value,
        }),
      ),
    );

    if (currentRequest !== state.requestCounter) {
      return false;
    }

    nextOutputParagraphs.splice(
      diff.start,
      diff.previousEnd - diff.start,
      ...results.map((result) => result.translation),
    );
  }

  state.swapContext = {
    ...swapContext,
    targetLang: targetLanguage.value,
    sourceParagraphs: nextSourceParagraphs,
    outputParagraphs: nextOutputParagraphs,
  };
  renderSwapWorkspace(state.swapContext);
  return true;
}

async function runTranslation(text) {
  const trimmed = text.trim();
  if (!trimmed) {
    if (state.activeController) {
      state.activeController.abort();
      state.activeController = null;
    }
    clearSwapContext();
    setStatus("대기 중");
    renderIdle();
    return;
  }

  if (state.activeController) {
    state.activeController.abort();
  }

  const controller = new AbortController();
  state.activeController = controller;
  const currentRequest = ++state.requestCounter;
  setStatus("번역 중");
  setUiPhase(UI_PHASES.TRANSLATING);

  try {
    if (state.swapContext) {
      const applied = await runSwapTranslation(trimmed, controller, currentRequest);
      if (!applied) {
        return;
      }
    } else {
      const result = await requestTranslation(trimmed, {
        signal: controller.signal,
      });
      if (currentRequest !== state.requestCounter) {
        return;
      }

      clearSwapContext();
      syncTargetLanguageOptions(targetLanguage.value);
      renderTranslation(result, trimmed);
    }

    setStatus("준비 완료");
    setUiPhase(UI_PHASES.READY);
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
    clearLastTranslation();
    setStatus("오류");
    setUiPhase(UI_PHASES.ERROR);
    translationText.textContent = `번역 요청 실패: ${error.message}`;
    translationText.classList.remove("empty");
    clearSegments("구간별 결과를 표시할 수 없습니다.");
    clearGlossaryHits("용어집 적용 상태를 표시할 수 없습니다.");
  } finally {
    if (state.activeController === controller) {
      state.activeController = null;
    }
  }
}

function scheduleTranslation() {
  const text = sourceText.value;
  const endsWithBoundary = /[.!?\n]\s*$/.test(text);
  const delay = endsWithBoundary ? 180 : 420;

  if (state.debounceHandle) {
    clearTimeout(state.debounceHandle);
  }

  setStatus("입력 대기");
  setUiPhase(UI_PHASES.WAITING);
  state.debounceHandle = window.setTimeout(() => {
    state.debounceHandle = null;
    runTranslation(text);
  }, delay);
}

function wireGlossaryRowRemoval(targetBody) {
  targetBody.addEventListener("click", (event) => {
    const button = event.target.closest('[data-action="remove"]');
    if (!button) {
      return;
    }
    button.closest("tr")?.remove();
    if (!targetBody.querySelector("tr")) {
      targetBody.innerHTML = createRowMarkup();
    }
    if (targetBody === glossaryTableBody) {
      scheduleGlossaryRefresh();
    }
  });
}

function initializeUI() {
  setSegmentsOpen(false);
  setInitialTargetLanguage();
  updateSourceCount();
  adjustSourceHeight();
  captureManualSourceHeight();
  renderIdle();
  wireGlossaryRowRemoval(glossaryTableBody);
  wireGlossaryRowRemoval(newGlossaryTableBody);
  syncGlossaryActionState();
  updateSwapAvailability();
}

sourceText.addEventListener("input", () => {
  updateSourceCount();
  adjustSourceHeight();
  scheduleTranslation();
});

sourceText.addEventListener("mouseup", captureManualSourceHeight);
sourceText.addEventListener("pointerup", captureManualSourceHeight);
sourceText.addEventListener("pointerdown", () => {
  state.pointerDownHeight = sourceText.offsetHeight;
});

targetLanguage.addEventListener("change", () => {
  if (targetLanguage.value.startsWith(RETRANSLATE_LANGUAGE_PREFIX)) {
    const rerunTarget = targetLanguage.value.slice(RETRANSLATE_LANGUAGE_PREFIX.length);
    clearSwapContext();
    syncTargetLanguageOptions(rerunTarget);
    persistTargetLanguage();
    updateTargetUI();
    if (sourceText.value.trim()) {
      scheduleTranslation();
    } else {
      clearGlossaryHits();
    }
    return;
  }

  const wasSwapped = Boolean(state.swapContext);
  if (wasSwapped) {
    clearSwapContext();
    syncTargetLanguageOptions(targetLanguage.value);
  }

  persistTargetLanguage();
  updateTargetUI();
  if (sourceText.value.trim()) {
    if (!wasSwapped && tryReuseTranslationForTarget(targetLanguage.value)) {
      return;
    }
    scheduleTranslation();
  } else {
    clearGlossaryHits();
  }
});

swapButton.addEventListener("click", handleSwap);
titleResetButton.addEventListener("click", resetWorkspace);

swapDetectedLanguages.addEventListener("click", (event) => {
  const button = event.target.closest("[data-swap-lang]");
  if (!button) {
    return;
  }

  performSwap(button.dataset.swapLang);
});

closeSwapModalButton.addEventListener("click", closeSwapModal);

toggleSegmentsButton.addEventListener("click", () => {
  setSegmentsOpen(segmentsBody.hidden);
});

glossarySelect.addEventListener("change", async () => {
  state.activeGlossaryId = glossarySelect.value || null;
  await refreshGlossaries();
});

addGlossaryRowButton.addEventListener("click", () => {
  addGlossaryRow(glossaryTableBody);
  glossaryNotice.textContent = "새 행을 추가했습니다.";
});

saveGlossaryButton.addEventListener("click", async () => {
  try {
    await saveCurrentGlossary();
    if (sourceText.value.trim()) {
      scheduleTranslation();
    }
  } catch (error) {
    glossaryNotice.textContent = `용어집 저장 실패: ${error.message}`;
  }
});

deleteGlossaryButton.addEventListener("click", async () => {
  try {
    await deleteCurrentGlossary();
    if (sourceText.value.trim()) {
      scheduleTranslation();
    }
  } catch (error) {
    glossaryNotice.textContent = `용어집 삭제 실패: ${error.message}`;
  }
});

useGlossary.addEventListener("change", () => {
  scheduleGlossaryRefresh({ force: true });
});

glossaryTableBody.addEventListener("input", () => {
  scheduleGlossaryRefresh();
});

glossaryTableBody.addEventListener("change", () => {
  scheduleGlossaryRefresh();
});

newGlossaryButton.addEventListener("click", openGlossaryModal);
closeGlossaryModalButton.addEventListener("click", closeGlossaryModal);

addNewGlossaryRowButton.addEventListener("click", () => {
  addGlossaryRow(newGlossaryTableBody);
  glossaryModalNotice.textContent = "새 행을 추가했습니다.";
});

glossaryModalForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await createGlossaryFromModal();
    if (sourceText.value.trim()) {
      scheduleTranslation();
    }
  } catch (error) {
    glossaryModalNotice.textContent = `용어집 생성 실패: ${error.message}`;
  }
});

initializeUI();
refreshGlossaries();

fetch("/health")
  .then((response) => response.json())
  .then((data) => {
    engineBadge.textContent = `engine: ${data.engine}`;
  })
  .catch(() => {
    engineBadge.textContent = "engine: unavailable";
  });
