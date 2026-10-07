"""Pure helpers: inline tag engine, tag checks, report rules, names, format registry."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from translator_app.documents import get_handler, output_ext, supported_formats, unsupported_message
from translator_app.documents.base import Collector, DocumentError, Lookup, needs_translation, strip_tags, tags_ok
from translator_app.documents.inline_tags import Piece, decode, encode
from translator_app.documents.jobs import output_name, sanitize_filename
from translator_app.documents.report import build_report, looks_like, missing_numbers


# ------------------------------------------------------------------ inline tags
def _pieces():
    return [Piece("text", key="plain", text="The "), Piece("text", key="bold", text="valve"),
            Piece("atom"), Piece("text", key="plain", text=" is closed & <locked>.")]


def test_encode_decode_roundtrip():
    enc = encode(_pieces())
    assert enc.tagged and enc.src == "The <g1>valve</g1><x1/> is closed &amp; &lt;locked&gt;."
    ops = decode(enc, "<g1>밸브</g1><x1/>가 닫혀 &amp; 잠김.")
    assert [(o[0], o[2] if o[0] == "text" else None) for o in ops] == [
        ("text", "밸브"), ("atom", None), ("text", "가 닫혀 & 잠김.")]
    assert decode(enc, "<g1>밸브<x1/></g1>") is not None          # atom inside a group is fine
    assert decode(enc, "<g1>밸브</g1> 닫힘") is None               # atom dropped
    assert decode(enc, "<g1>밸브</g1><x1/><x1/>") is None          # atom duplicated
    assert decode(enc, "<g2>밸브</g2><x1/>") is None               # unknown id
    assert decode(enc, "<g1>밸브<x1/>") is None                    # unclosed


def test_encode_skips_text_without_letters():
    assert encode([Piece("text", key=1, text=" 12,345 ")]) is None
    enc = encode([Piece("text", key=1, text="Hello")])
    assert enc and not enc.tagged and enc.src == "Hello"


def test_tags_ok_and_needs_translation():
    assert tags_ok("a <g1>b</g1> <x1/>", "<x1/> 가 <g1>나</g1>")              # groups may move
    assert tags_ok("a <g1>b</g1> <x1/> c <x2/>", "<g1>나</g1> 가 <x1/> 다 <x2/>")
    assert not tags_ok("a <x1/> b <x2/>", "가 <x2/> 나 <x1/>")
    assert not tags_ok("a <g1>b</g1>", "가 나")
    assert tags_ok("plain", "anything")
    assert strip_tags("<g1>a &amp; b</g1><x2/>") == "a & b"
    for s in ("", "  ", "12,345.6", "--", "https://example.com/a?b=1", "info@keei.re.kr", "<x1/> 2024"):
        assert not needs_translation(s), s
    for s in ("Hello", "수소", "<g1>Valve</g1> 3", "see https://example.com"):
        assert needs_translation(s), s


def test_collector_and_lookup():
    c = Collector()
    assert c(["a", "b", "a"]) == ["a", "b", "a"]
    assert c(["c", "b"]) == ["c", "b"]
    assert c.items == ["a", "b", "c"]
    assert Lookup({"a": "가"})(["a", "z"]) == ["가", "z"]


# ------------------------------------------------------------------ report
def test_missing_numbers():
    assert missing_numbers("Produced 1,250.5 t in 2024 (50%).", "2024년에 1250.5톤 생산(50%).") == []
    assert missing_numbers("Revenue 3,000,000 USD", "매출 300만 달러") == []
    assert missing_numbers("3 million users", "300만 명") == []
    assert missing_numbers("Article 2 applies", "제2조가 적용된다") == []
    assert missing_numbers("Pressure 30 bar, 12.50 MPa", "압력 바, 12.5 MPa") == ["30"]
    assert missing_numbers("Druck 12,5 bar", "pressure 12.5 bar") == []
    assert missing_numbers("the 1st item", "첫 번째 항목") == []


def test_build_report_rules():
    gl = [SimpleNamespace(source="electrolyzer", target="수전해 장치", enabled=True),
          SimpleNamespace(source="tank", target="탱크", enabled=False)]
    pairs = [
        ("The <g1>electrolyzer</g1> runs at 30 bar.", "<g1>전해조</g1>는 30 bar로 운전한다."),
        ("Keep the valve closed at all times.", "Keep the valve closed at all times."),
        ("Pressure 700 bar", "압력 바"),
        ("Store the tank <g1>outside</g1>.", "탱크를 밖에 보관한다."),
        ("Microsoft Excel", "Microsoft Excel"),
        ("KEEI", "KEEI"),
    ]
    items = build_report(pairs, source_lang="en", target_lang="ko", glossary=gl)
    issues = [(i["source"], i["issue"]) for i in items]
    assert ("The electrolyzer runs at 30 bar.", "용어집 미적용: electrolyzer → 수전해 장치") in issues
    assert ("Keep the valve closed at all times.", "번역되지 않음") in issues
    assert ("Pressure 700 bar", "숫자 누락: 700") in issues
    assert any(s.startswith("Store the tank") and i.startswith("서식 태그") for s, i in issues)
    assert not any(s in ("Microsoft Excel", "KEEI") for s, _ in issues)
    assert not any("tank" in i for _, i in issues)                       # disabled entry
    assert len(build_report(pairs * 300, source_lang="en", target_lang="ko", limit=5)) <= 5


def test_report_auto_source_language():
    pairs = [("수소 저장 탱크를 점검한다.", "수소 저장 탱크를 점검한다."), ("Check the gauge daily.", "Check the gauge daily.")]
    items = build_report(pairs, source_lang="auto", target_lang="ko")
    assert [i["source"] for i in items] == ["Check the gauge daily."]
    assert build_report(pairs, source_lang="ko", target_lang="ko") == []
    assert looks_like("수소 탱크", "ko") and not looks_like("hydrogen", "ko") and looks_like("ガス", "ja")


# ------------------------------------------------------------------ names / registry
def test_filenames():
    assert sanitize_filename("C:\\Users\\me\\보고서.docx") == "보고서.docx"
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("a\x00b\u202e.txt") == "ab.txt"
    assert sanitize_filename('x<>:"|?*.md') == "x_______.md"
    assert sanitize_filename("") == "document"
    assert sanitize_filename("...") == "document"
    long = sanitize_filename("가" * 300 + ".docx")
    assert len(long) <= 180 and long.endswith(".docx")
    assert output_name("보고서.docx", ".docx", "en", "ko", False) == "보고서_ko.docx"
    assert output_name("보고서.hwp", ".docx", "ko", "en", True) == "보고서_ko-en.docx"


def test_registry():
    exts = [f["ext"] for f in supported_formats()]
    assert exts == [".docx", ".pptx", ".xlsx", ".hwpx", ".hwp", ".pdf", ".txt", ".md", ".html", ".htm",
                    ".srt", ".vtt", ".png", ".jpg", ".jpeg", ".webp"]
    f = {x["ext"]: x for x in supported_formats()}
    assert f[".hwp"]["output"] == ".docx" and f[".hwp"]["layout"] is False
    assert f[".docx"]["bilingual"] and not f[".pptx"]["bilingual"]
    assert ".png" not in [x["ext"] for x in supported_formats(vision=False)]
    assert output_ext(".pdf", pdf_mode="docx") == ".docx" and output_ext(".pdf") == ".pdf"
    assert ".xlsx" in unsupported_message(".xls") and ".hwpx" in unsupported_message(".hwt")
    assert "지원 형식" in unsupported_message(".zip")
    with pytest.raises(DocumentError):
        get_handler(".exe")
    assert get_handler(".docx") is not get_handler(".docx")              # one instance per job
