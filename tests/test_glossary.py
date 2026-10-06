from translator_app.schemas import GlossaryEntry
from translator_app.services.glossary import GlossaryStore, match_glossary_entries


def test_match_glossary_entries_returns_enabled_matches_sorted_by_length() -> None:
    entries = [
        GlossaryEntry(source_lang="en", target_lang="ko", source="GPU", target="GPU", enabled=True),
        GlossaryEntry(
            source_lang="en",
            target_lang="ko",
            source="GPU server",
            target="gpu-server-ko",
            enabled=True,
        ),
        GlossaryEntry(
            source_lang="en",
            target_lang="ja",
            source="GPU server",
            target="gpu-server-ja",
            enabled=True,
        ),
        GlossaryEntry(
            source_lang="en",
            target_lang="ko",
            source="disabled",
            target="disabled-ko",
            enabled=False,
        ),
    ]

    matched = match_glossary_entries(
        "The GPU server is online.",
        entries,
        target_lang="ko",
        source_lang="auto",
    )

    assert [entry.source for entry in matched] == ["GPU server", "GPU"]


def test_delete_glossary_recreates_default_when_last_file_removed(tmp_path) -> None:
    store = GlossaryStore(tmp_path / "glossary" / "default.json")

    assert store.delete_glossary("default") is True

    summaries = store.list_glossaries()
    assert len(summaries) == 1
    assert summaries[0].id == "default"
    assert summaries[0].entry_count == 0


def test_list_glossaries_quarantines_invalid_documents(tmp_path) -> None:
    root = tmp_path / "glossary"
    store = GlossaryStore(root / "default.json")
    (root / "broken.json").write_text("{not-json", encoding="utf-8")

    summaries = store.list_glossaries()

    assert [summary.id for summary in summaries] == ["default"]
    assert not (root / "broken.json").exists()
    assert len(list(root.glob("broken.json.invalid-*.bak"))) == 1


def test_invalid_default_is_recreated_during_startup(tmp_path) -> None:
    root = tmp_path / "glossary"
    root.mkdir(parents=True)
    (root / "default.json").write_text("{not-json", encoding="utf-8")

    store = GlossaryStore(root / "default.json")
    default_glossary = store.load_glossary("default")

    assert default_glossary is not None
    assert default_glossary.id == "default"
    assert default_glossary.entries == []
    assert len(list(root.glob("default.json.invalid-*.bak"))) == 1


def test_valid_existing_default_file_is_never_rewritten(tmp_path) -> None:
    root = tmp_path / "glossary"
    root.mkdir(parents=True)
    seed = root / "default.json"
    raw = rb'{"id": "default", "name": "seed", "entries": [{"source": "H2", "target": "H\u2082"}]}'
    seed.write_bytes(raw)

    store = GlossaryStore(seed)
    store.list_glossaries()
    assert seed.read_bytes() == raw
    assert store.load_glossary("default").entries[0].target == "H\u2082"


def test_saved_files_end_with_a_newline_and_use_lf(tmp_path) -> None:
    store = GlossaryStore(tmp_path / "glossary" / "default.json")
    created = store.create_glossary(name="x", entries=[GlossaryEntry(source="a", target="b")])
    data = (tmp_path / "glossary" / f"{created.id}.json").read_bytes()
    assert data.endswith(b"}\n") and b"\r\n" not in data
    assert not list((tmp_path / "glossary").glob("*.tmp"))


def test_parse_import_variants() -> None:
    from translator_app.services.glossary import GlossaryImportError, merge_entries, parse_import

    with_header = parse_import("source,target,note\nfuel cell,연료전지,\"a, b\"\n".encode(), "x.csv")
    assert [(e.source, e.target, e.note) for e in with_header] == [("fuel cell", "연료전지", "a, b")]

    korean_header = parse_import("원문\t번역\t사용\n수소\thydrogen\t미사용\n".encode("cp949"), "x.tsv",
                                 default_source_lang="ko", default_target_lang="en")
    assert korean_header[0].source == "수소" and korean_header[0].enabled is False
    assert (korean_header[0].source_lang, korean_header[0].target_lang) == ("ko", "en")

    langs_first = parse_import(b"en\tja\tplant\t\xe3\x83\x97\xe3\x83\xa9\xe3\x83\xb3\xe3\x83\x88\t\ttrue\n", "x.txt")
    assert (langs_first[0].source_lang, langs_first[0].target_lang, langs_first[0].source) == ("en", "ja", "plant")

    for bad in (b"", b"only-one-column\n", b"source,target\n,missing\n"):
        try:
            parse_import(bad, "x.csv")
        except GlossaryImportError:
            pass
        else:  # pragma: no cover
            raise AssertionError(bad)

    existing = [GlossaryEntry(source="Grid", target="old"), GlossaryEntry(source="keep", target="k")]
    merged = merge_entries(existing, [GlossaryEntry(source="grid", target="new"), GlossaryEntry(source="n", target="m")])
    assert [(e.source, e.target) for e in merged] == [("grid", "new"), ("keep", "k"), ("n", "m")]


def test_latin_terms_are_bounded_by_other_scripts() -> None:
    from translator_app.services.glossary import term_occurs

    for term, text in (("ESS", "ESS를 설치한다."), ("PEMFC", "수소 PEMFC의 효율"), ("hydrogen", "hydrogen과"),
                       ("LNG", "LNG船"), ("AI", "生成AIの"), ("café", "le café, s'il vous plaît"),
                       ("fuel cell", "fuel\n\n cell")):
        assert term_occurs(term, text), (term, text)
    for term, text in (("IT", "it"), ("cat", "concatenate"), ("AI", "maintain"), ("caf", "café"),
                       ("ESS", "ESSENTIAL"), ("LNG", "LNG2")):
        assert not term_occurs(term, text), (term, text)


def test_export_neutralises_formulas_and_import_restores_them() -> None:
    from translator_app.services.glossary import export_entries, parse_import

    values = ["=SUM(A1:A2)", "+82 10", "-minus", "@user", "'=already quoted", "''+two quotes", "\tTabbed",
              "\rReturn", "'plain quote", "plain", "a=b"]
    entries = [GlossaryEntry(source=value, target=f"t{index}", note=value) for index, value in enumerate(values)]
    for fmt, ext in (("csv", ".csv"), ("tsv", ".tsv")):
        data = export_entries(entries, fmt)
        text = data.decode("utf-8-sig")
        assert "'=SUM(A1:A2)" in text and "'+82 10" in text and "'@user" in text and "''=already quoted" in text
        assert "\n=" not in text and ",=" not in text and "\t=" not in text
        restored = parse_import(data, f"glossary{ext}")
        assert [(e.source, e.target, e.note) for e in restored] == [(e.source, e.target, e.note) for e in entries]
    # a hand-written file keeps its values: only a "'" right before a formula character is removed
    plain = parse_import("source,target\n'=x,=y\n'abc,'@d\n".encode(), "x.csv")
    assert [(e.source, e.target) for e in plain] == [("=x", "=y"), ("'abc", "@d")]


def test_xlsx_import_refuses_huge_parts() -> None:
    import io
    import zipfile

    import pytest

    from translator_app.services.glossary import _XLSX_MAX_XML, GlossaryImportError, parse_import

    assert _XLSX_MAX_XML == 10 * 1024 * 1024
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             + " " * (_XLSX_MAX_XML + 1) + "</sheetData></worksheet>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    assert len(buffer.getvalue()) < 1024 * 1024
    with pytest.raises(GlossaryImportError, match="너무 큽니다"):
        parse_import(buffer.getvalue(), "big.xlsx")
