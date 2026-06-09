from translator_app.services.language_detection import detect_source_languages, resolve_source_language


def test_detect_source_languages_marks_single_language_text() -> None:
    result = detect_source_languages("This report summarizes energy demand in 2026.")

    assert result.mode == "single"
    assert result.primary_language == "en"
    assert result.languages[0].code == "en"


def test_detect_source_languages_marks_mixed_language_text() -> None:
    result = detect_source_languages("The project targets \uC218\uC18C supply chain optimization.")

    assert result.mode == "mixed"
    assert result.primary_language is None
    assert {item.code for item in result.languages} >= {"en", "ko"}


def test_detect_source_languages_uses_visible_script_share_for_mixed_text() -> None:
    result = detect_source_languages("The project targets \uC218\uC18C supply chain optimization.")

    shares = {item.code: item.share for item in result.languages}

    assert shares["en"] > shares["ko"]
    assert abs(sum(shares.values()) - 1.0) < 0.01


def test_detect_source_languages_uses_kana_to_resolve_japanese() -> None:
    result = detect_source_languages(
        "\u30A8\u30CD\u30EB\u30AE\u30FC\u653F\u7B56\u306E\u898B\u76F4\u3057\u3092\u884C\u3044\u307E\u3059\u3002"
    )

    assert result.mode == "single"
    assert result.primary_language == "ja"


def test_detect_source_languages_handles_french_text() -> None:
    result = detect_source_languages("Le projet vise a accelerer la transition energetique europeenne.")

    assert result.mode == "single"
    assert result.primary_language == "fr"


def test_resolve_source_language_uses_detected_single_language() -> None:
    detection = detect_source_languages("This report summarizes energy demand in 2026.")

    assert resolve_source_language("auto", detection) == "en"


def test_resolve_source_language_preserves_auto_for_mixed_text() -> None:
    detection = detect_source_languages("The project targets \uC218\uC18C supply chain optimization.")

    assert resolve_source_language("auto", detection) == "auto"
