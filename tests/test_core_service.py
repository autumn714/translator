"""TranslatorService orchestration: streaming, cache, dedupe, batches, tags, cancellation."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from conftest import SSEStream, batch_answer, batch_items, completion, sse_chunks, user_text
from translator_app.engines.factory import build_engine
from translator_app.llm.client import LLMClient, TranslationCancelled, VisionUnavailable
from translator_app.schemas import AlternativesRequest, GlossaryEntry, LookupRequest, RewriteRequest, TranslateOptions
from translator_app.services.glossary import GlossaryStore
from translator_app.services.translator import TranslatorService, needs_translation

pytestmark = pytest.mark.anyio


def build_service(settings, llm: LLMClient | None = None) -> TranslatorService:
    store = GlossaryStore(settings.glossary_file)
    return TranslatorService(settings, llm, build_engine(settings, llm), store)


def fake_translate(text: str) -> str:
    return f"[KO] {text}"


def default_chat(body: dict) -> object:
    """Pseudo-translation: plain → '[KO] text', stream → SSE pieces, batch → JSON."""
    if "response_format" in body:
        return batch_answer([fake_translate(t) for t in batch_items(body)])
    text = fake_translate(user_text(body))
    if body.get("stream"):
        return sse_chunks([text[: len(text) // 2], text[len(text) // 2:]])
    return completion(text)


def chat_bodies(fake) -> list[dict]:
    return fake.bodies


async def collect(service: TranslatorService, text: str, opts: TranslateOptions) -> list[dict]:
    return [event async for event in service.stream_text(text, opts)]


# ---------------------------------------------------------------- streaming
async def test_stream_events_in_order_then_cache_hits(fake_llm_factory) -> None:
    llm, fake = fake_llm_factory(default_chat)
    service = build_service(llm.settings, llm)
    opts = TranslateOptions(source_lang="en", target_lang="ko")
    text = "First paragraph here.\n\nSecond paragraph here."
    try:
        events = await collect(service, text, opts)
        assert events[0]["type"] == "start" and events[0]["units"] == 2
        assert events[0]["segments"] == ["First paragraph here.", "Second paragraph here."]
        assert events[-1]["type"] == "done"
        assert events[-1]["translation"] == "[KO] First paragraph here.\n\n[KO] Second paragraph here."
        assert events[-1]["segments"][1] == {"source": "Second paragraph here.",
                                             "target": "[KO] Second paragraph here."}
        kinds = [e["type"] for e in events[1:-1]]
        assert set(kinds) == {"delta", "unit"} and kinds.count("unit") == 2
        for index in (0, 1):
            unit_events = [e for e in events if e.get("index") == index]
            assert unit_events[-1]["type"] == "unit"
            assert "".join(e["text"] for e in unit_events[:-1]) == unit_events[-1]["text"]
        streamed = len(fake.bodies)
        assert streamed == 2 and all(b["stream"] for b in fake.bodies)

        again = await collect(service, text, opts)
        assert len(fake.bodies) == streamed  # served from the segment cache
        assert [e["type"] for e in again] == ["start", "delta", "unit", "delta", "unit", "done"]
        assert again[-1]["translation"] == events[-1]["translation"]
    finally:
        await llm.aclose()


async def test_identity_shortcut_sends_nothing(fake_llm_factory) -> None:
    llm, fake = fake_llm_factory(default_chat)
    service = build_service(llm.settings, llm)
    opts = TranslateOptions(source_lang="ko", target_lang="ko")
    try:
        result = await service.translate_text("안녕하세요.\r\n\r\n반갑습니다.", opts)
        events = await collect(service, "안녕하세요.", opts)
    finally:
        await llm.aclose()
    assert result.translation == "안녕하세요.\n\n반갑습니다."
    assert [s.target for s in result.segments] == ["안녕하세요.", "반갑습니다."]
    assert [e["type"] for e in events] == ["start", "delta", "unit", "done"]
    assert fake.bodies == []


async def test_translate_text_dedupes_identical_units(fake_llm_factory) -> None:
    async def slow(body: dict) -> dict:
        await asyncio.sleep(0.02)
        return completion("translated")

    llm, fake = fake_llm_factory(slow)
    service = build_service(llm.settings, llm)
    try:
        result = await service.translate_text("same\n\nsame", TranslateOptions(source_lang="en", target_lang="ko"))
    finally:
        await llm.aclose()
    assert result.translation == "translated\n\ntranslated"
    assert len(fake.bodies) == 1


async def test_stream_close_cancels_upstream_requests(fake_llm_factory) -> None:
    streams: list[SSEStream] = []

    def chat(body: dict) -> SSEStream:
        stream = SSEStream(sse_chunks([f"w{i} " for i in range(200)]), delay=0.01)
        streams.append(stream)
        return stream

    llm, fake = fake_llm_factory(chat)
    service = build_service(llm.settings, llm)
    text = "\n\n".join(f"Paragraph number {i}." for i in range(5))
    try:
        agen = service.stream_text(text, TranslateOptions(source_lang="en", target_lang="ko"))
        assert (await agen.__anext__())["type"] == "start"
        assert (await agen.__anext__())["type"] == "delta"
        await agen.aclose()
        await asyncio.sleep(0.05)
        assert streams and all(stream.closed for stream in streams)
        assert all(stream.sent < 200 for stream in streams)
        assert len(streams) <= 3  # LLM_MAX_PARALLEL
        assert llm.limiter.active == 0 and llm.limiter.waiting() == 0
    finally:
        await llm.aclose()


async def test_stream_repetition_is_replaced_by_a_non_stream_retry(fake_llm_factory) -> None:
    def chat(body: dict) -> object:
        if body.get("stream"):
            return sse_chunks(["반복 반복 반복"], finish_reason="repetition")
        return completion("정상 번역")

    llm, fake = fake_llm_factory(chat)
    service = build_service(llm.settings, llm)
    try:
        events = await collect(service, "Hello there.", TranslateOptions(source_lang="en", target_lang="ko"))
    finally:
        await llm.aclose()
    unit = next(e for e in events if e["type"] == "unit")
    assert unit["text"] == "정상 번역"
    assert events[-1]["translation"] == "정상 번역"
    assert fake.bodies[-1].get("stream") is None and fake.bodies[-1]["presence_penalty"] == 1.0


async def test_stream_reports_llm_errors_as_an_error_event(fake_llm_factory) -> None:
    llm, _ = fake_llm_factory(lambda body: httpx.Response(500, json={"message": "boom"}))
    service = build_service(llm.settings, llm)
    try:
        events = await collect(service, "Hello.", TranslateOptions(source_lang="en", target_lang="ko"))
    finally:
        await llm.aclose()
    assert events[0]["type"] == "start" and events[-1]["type"] == "error"
    assert "모델 서버" in events[-1]["detail"]


# ---------------------------------------------------------------- prompts
async def test_prompt_contains_rules_register_glossary_and_context(fake_llm_factory) -> None:
    llm, fake = fake_llm_factory(default_chat)
    service = build_service(llm.settings, llm)
    entries = [
        GlossaryEntry(source_lang="en", target_lang="ko", source="electrolyzer", target="수전해 설비"),
        GlossaryEntry(source_lang="en", target_lang="ko", source="fuel cell", target="연료전지"),
    ]
    opts = TranslateOptions(source_lang="en", target_lang="ko", formality="gaejoshik", context="Hydrogen plant manual",
                            instructions="숫자는 아라비아 숫자로", glossary_entries=entries)
    try:
        result = await service.translate_text("The electrolyzer runs daily.", opts)
    finally:
        await llm.aclose()
    system = fake.bodies[0]["messages"][0]["content"]
    assert fake.bodies[0]["messages"][1]["content"] == "The electrolyzer runs daily."
    assert "Target language: Korean (ko)" in system and "Source language: English (en)" in system
    assert "개조식" in system
    assert "| electrolyzer | 수전해 설비 |" in system and "fuel cell" not in system
    assert "Context (do not translate" in system and "Hydrogen plant manual" in system
    assert "- 숫자는 아라비아 숫자로" in system
    assert system.index("Rules:") < system.index("Glossary") < system.index("Context")
    assert [hit.source for hit in result.glossary_hits] == ["electrolyzer"]


# ---------------------------------------------------------------- batches
async def test_batch_splits_in_halves_on_count_mismatch(fake_llm_factory) -> None:
    def chat(body: dict) -> object:
        if "response_format" in body:
            items = batch_items(body)
            outputs = [fake_translate(t) for t in items]
            if len(items) > 2:
                outputs = outputs[:-1]  # model dropped an item
            return batch_answer(outputs)
        return completion(fake_translate(user_text(body)))

    llm, fake = fake_llm_factory(chat)
    service = build_service(llm.settings, llm)
    texts = [f"Sentence number {n}." for n in range(5)]
    try:
        result = await service.translate_batch(texts, TranslateOptions(source_lang="en", target_lang="ko"))
    finally:
        await llm.aclose()
    assert result == [fake_translate(t) for t in texts]
    sizes = [len(batch_items(b)) if "response_format" in b else 1 for b in fake.bodies]
    assert sizes[0] == 5 and sizes[1] == 2  # 5 → (2, 3) → (2, (1, 2))


async def test_batch_skips_items_without_words_and_uses_exact_glossary_terms(fake_llm_factory) -> None:
    llm, fake = fake_llm_factory(default_chat)
    service = build_service(llm.settings, llm)
    texts = ["2026", "  ", "https://example.com/a", "kim@example.com", "PFD-001", "12.5 %", " H2 ", "Hello world"]
    opts = TranslateOptions(source_lang="en", target_lang="ko", glossary_entries=[
        GlossaryEntry(source_lang="en", target_lang="ko", source="H2", target="H₂")])
    try:
        result = await service.translate_batch(texts, opts)
    finally:
        await llm.aclose()
    assert result[:6] == texts[:6]
    assert result[6] == " H₂ "
    assert result[7] == "[KO] Hello world"
    assert len(fake.bodies) == 1 and fake.bodies[0]["messages"][1]["content"] == "Hello world"


async def test_batch_request_body_and_preceding_context(fake_llm_factory) -> None:
    llm, fake = fake_llm_factory(default_chat)
    service = build_service(llm.settings, llm)
    try:
        result = await service.translate_batch(
            ["  First <g1>bold</g1> item ", "Second item<x1/>"],
            TranslateOptions(source_lang="en", target_lang="ko"),
            tags=True,
            preceding=[("Earlier sentence.", "앞 문장.")],
        )
    finally:
        await llm.aclose()
    assert result == ["  [KO]   First <g1>bold</g1> item ", "[KO] Second item<x1/>"]  # outer whitespace kept
    body = fake.bodies[0]
    assert body["response_format"]["type"] == "json_schema"
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["translations"]["minItems"] == 2 == schema["properties"]["translations"]["maxItems"]
    assert json.loads(user_text(body)) == {"items": [{"id": 0, "text": "  First <g1>bold</g1> item "},
                                                     {"id": 1, "text": "Second item<x1/>"}]}
    system = body["messages"][0]["content"]
    assert "<g1>" in system and "Earlier sentence." in system and "앞 문장." in system
    assert body["temperature"] == 0.0 and body["top_k"] == -1


async def test_broken_tags_are_retried_once_strictly(fake_llm_factory) -> None:
    def chat(body: dict) -> object:
        if "response_format" in body:
            return batch_answer(["[KO] Hello world", "[KO] <g2>two</g2> plain"])  # first lost its tags
        assert "IMPORTANT" in body["messages"][0]["content"]
        return completion("[KO] <g1>Hello</g1> world<x1/>")

    llm, fake = fake_llm_factory(chat)
    service = build_service(llm.settings, llm)
    try:
        result = await service.translate_batch(["<g1>Hello</g1> world<x1/>", "<g2>two</g2> plain"],
                                               TranslateOptions(source_lang="en", target_lang="ko"), tags=True)
    finally:
        await llm.aclose()
    assert result == ["[KO] <g1>Hello</g1> world<x1/>", "[KO] <g2>two</g2> plain"]
    retry = fake.bodies[-1]
    assert retry["temperature"] == 0.0 and "<g1>" in retry["messages"][0]["content"]
    assert len(fake.bodies) == 2


async def test_still_broken_tags_keep_the_best_output(fake_llm_factory) -> None:
    def chat(body: dict) -> object:
        if "response_format" in body:
            return batch_answer(["[KO] <g1>Hello world", "[KO] ok"])
        return completion("[KO] Hello world")  # retry is worse (no tags at all)

    llm, _ = fake_llm_factory(chat)
    service = build_service(llm.settings, llm)
    try:
        result = await service.translate_batch(["<g1>Hello</g1> world", "ok then"],
                                               TranslateOptions(source_lang="en", target_lang="ko"), tags=True)
    finally:
        await llm.aclose()
    assert result[0] == "[KO] <g1>Hello world"


async def test_batch_cancel_aborts_the_running_request(fake_llm_factory) -> None:
    started = asyncio.Event()

    async def slow(body: dict) -> dict:
        started.set()
        await asyncio.sleep(10)
        return completion("late")

    llm, _ = fake_llm_factory(slow)
    service = build_service(llm.settings, llm)
    cancel = asyncio.Event()
    try:
        task = asyncio.create_task(service.translate_batch(["Hello one.", "Hello two."],
                                                           TranslateOptions(source_lang="en", target_lang="ko"),
                                                           cancel=cancel))
        await asyncio.wait_for(started.wait(), 2)
        cancel.set()
        with pytest.raises(TranslationCancelled):
            await asyncio.wait_for(task, 2)
        assert llm.limiter.active == 0
        with pytest.raises(TranslationCancelled):
            await service.translate_batch(["x y"], TranslateOptions(), cancel=cancel)
    finally:
        await llm.aclose()


async def test_batch_with_same_languages_is_returned_unchanged(settings_factory) -> None:
    service = build_service(settings_factory())
    texts = ["안녕", "세계"]
    assert await service.translate_batch(texts, TranslateOptions(source_lang="ko", target_lang="ko")) == texts


# ---------------------------------------------------------------- mock engine (ENGINE_TYPE=mock)
async def test_mock_service_is_deterministic_and_keeps_tags(settings_factory) -> None:
    service = build_service(settings_factory())
    opts = TranslateOptions(source_lang="en", target_lang="ja")
    assert await service.translate_batch(["<g1>Bold</g1> text<x1/>", "123"], opts, tags=True) == [
        "[ja] <g1>Bold</g1> text<x1/>", "123"]
    response = await service.translate_text("One.\n\nTwo.", opts)
    assert response.translation == "[ja] One.\n\n[ja] Two." and response.engine == "mock"
    events = [e async for e in service.stream_text("One.", opts)]
    assert events[-1]["translation"] == "[ja] One."
    assert await service.describe_image(b"img", "image/png", opts) == "[ja] 이미지 텍스트"
    alternatives = await service.alternatives(AlternativesRequest(source="One.", translation="[ja] One.",
                                                                  span="One", target_lang="ja"))
    assert alternatives == ["One (1)", "One (2)", "One (3)"]
    rewritten = await service.rewrite(RewriteRequest(text="This are wrong.", lang="en", style="polish"))
    assert rewritten.text == "[polish] This are wrong." and rewritten.detected_lang == "en"
    looked = await service.lookup(LookupRequest(term="hydrogen", target_lang="ko"))
    assert looked.term == "hydrogen" and looked.entries[0].translation == "[ko] hydrogen"


# ---------------------------------------------------------------- other tasks through the real client
async def test_alternatives_rewrite_lookup_bodies(fake_llm_factory) -> None:
    def chat(body: dict) -> object:
        name = body.get("response_format", {}).get("json_schema", {}).get("name")
        if name == "alternatives":
            return completion(json.dumps({"alternatives": ["대안 하나", "현재 표현", "대안 하나 "]}, ensure_ascii=False))
        if name == "dictionary":
            return completion(json.dumps({"entries": [{"translation": "수소", "pos": "명사", "note": ""}],
                                          "examples": [{"source": "Hydrogen is light.", "target": "수소는 가볍다."}]},
                                         ensure_ascii=False))
        return completion("다듬은 문장입니다.")

    llm, fake = fake_llm_factory(chat)
    service = build_service(llm.settings, llm)
    try:
        alternatives = await service.alternatives(AlternativesRequest(
            source="The plant is big.", translation="그 공장은 현재 표현.", span="현재 표현",
            source_lang="en", target_lang="ko"))
        rewritten = await service.rewrite(RewriteRequest(text="다듬을 문장 입니다", lang="auto", style="formal"))
        looked = await service.lookup(LookupRequest(term="hydrogen", context="Hydrogen is light.",
                                                    source_lang="en", target_lang="ko"))
    finally:
        await llm.aclose()
    assert alternatives == ["대안 하나"]
    alt_body, rewrite_body, lookup_body = fake.bodies
    assert alt_body["temperature"] == 0.7 and alt_body["top_p"] == 0.8 and alt_body["top_k"] == 20
    assert alt_body["response_format"]["json_schema"]["schema"]["properties"]["alternatives"]["minItems"] == 3
    assert "n" not in alt_body
    assert rewrite_body["temperature"] == 0.3 and "never translate" in rewrite_body["messages"][0]["content"].lower()
    assert rewrite_body["messages"][1]["content"] == "다듬을 문장 입니다"
    assert rewritten.text == "다듬은 문장입니다." and rewritten.detected_lang == "ko"
    assert lookup_body["temperature"] == 0.0
    assert looked.entries[0].translation == "수소" and looked.examples[0].target == "수소는 가볍다."


async def test_describe_image_sends_a_data_uri_and_honours_vision_off(fake_llm_factory) -> None:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (64, 32), (255, 255, 255)).save(buffer, format="PNG")
    llm, fake = fake_llm_factory(lambda body: completion("# 제목\n\n본문"))
    service = build_service(llm.settings, llm)
    try:
        text = await service.describe_image(buffer.getvalue(), "image/png",
                                            TranslateOptions(target_lang="ko"))
    finally:
        await llm.aclose()
    assert text == "# 제목\n\n본문"
    body = fake.bodies[0]
    parts = body["messages"][-1]["content"]
    assert parts[1]["type"] == "image_url" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert isinstance(body["messages"][0]["content"], str)  # no image in the system message
    assert body["temperature"] == 0.1 and body["max_completion_tokens"] <= 6144

    off, _ = fake_llm_factory(settings={"llm_vision": "off"})
    service = build_service(off.settings, off)
    try:
        with pytest.raises(VisionUnavailable):
            await service.describe_image(b"x", "image/png", TranslateOptions())
    finally:
        await off.aclose()


def test_needs_translation_rules() -> None:
    assert not needs_translation("")
    assert not needs_translation("<g1>2026</g1>")
    assert not needs_translation("www.keei.re.kr")
    assert not needs_translation("A-12/3")
    assert needs_translation("Table 3")
    assert needs_translation("<g1>수소</g1>")
    assert needs_translation("&amp; hydrogen")
