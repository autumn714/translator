from translator_app.services.segmentation import normalize_line_breaks, split_text, split_translation_units


def test_normalize_line_breaks_caps_empty_lines_at_one_blank_line() -> None:
    assert normalize_line_breaks("a\r\n\r\n\r\nb") == "a\n\nb"


def test_split_translation_units_groups_paragraphs_with_line_breaks() -> None:
    text = "Line 1\nLine 2\n\nParagraph 2\n\n\nParagraph 3"

    units = split_translation_units(text, max_chars=100)

    assert units == ["Line 1\nLine 2\n\nParagraph 2\n\nParagraph 3"]


def test_split_text_keeps_sentence_level_view() -> None:
    parts = split_text("First sentence. Second sentence!\nThird sentence?")

    assert parts == ["First sentence.", "Second sentence!", "Third sentence?"]
