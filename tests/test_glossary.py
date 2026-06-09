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
