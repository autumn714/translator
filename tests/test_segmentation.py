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


def test_split_units_are_paragraphs_and_exact_substrings() -> None:
    from translator_app.services.segmentation import join_units, split_units

    text = "Title\r\n\r\n\r\nLine one\nline two\n \nLast."
    units = split_units(text, max_chars=1200)
    assert [u.text for u in units] == ["Title", "Line one\nline two", "Last."]
    normalized = normalize_line_breaks(text)
    assert all(u.text in normalized for u in units)
    assert join_units(units, [u.text for u in units]) == "Title\n\nLine one\nline two\n\nLast."


def test_split_units_splits_long_paragraphs_by_sentence() -> None:
    from translator_app.services.segmentation import join_units, split_units

    paragraph = "First sentence is here. Second one follows! Third?\nFourth on a new line."
    units = split_units(paragraph, max_chars=30)
    assert [u.text for u in units] == ["First sentence is here.", "Second one follows! Third?",
                                       "Fourth on a new line."]
    assert {u.paragraph for u in units} == {0}
    assert [u.newline_before for u in units] == [False, False, True]
    assert join_units(units, ["A.", "B.", "C."]) == "A. B.\nC."
    assert join_units(units, ["가.", "나.", "다."], joiner="") == "가.나.\n다."
    assert all(len(u.text) <= 30 for u in split_units("word " * 50, max_chars=30))
