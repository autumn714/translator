"""translator_app.selftest: the PDF check accepts the fake model and the real model, rejects untranslated output."""
from __future__ import annotations

import pymupdf

from translator_app import selftest as st


def _pdf(lines: list[str]) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_htmlbox(pymupdf.Rect(72, 80 + 28 * i, 520, 104 + 28 * i), line)
    data = doc.tobytes()
    doc.close()
    return data


def test_check_pdf_accepts_fake_and_real_translations():
    assert st.check_pdf(_pdf([st.FAKE_KO + line for line in st.DOC_LINES])) is None
    assert st.check_pdf(_pdf(["수소 안전 수칙", "수소 용기는 환기가 잘 되는 곳에 보관한다.", "사용 전마다 압력계를 확인한다."])) is None


def test_check_pdf_rejects_untranslated_output():
    assert st.check_pdf(st.make_pdf()) is not None
    assert st.check_pdf(_pdf([st.FAKE_KO + st.DOC_LINES[0], st.DOC_LINES[1], st.FAKE_KO + st.DOC_LINES[2]])) is not None
    assert st.check_pdf(_pdf(["수소 안전 수칙", st.DOC_LINES[1], "사용 전마다 압력계를 확인한다."])) is not None
    assert st.check_pdf(_pdf(["Wasserstoff", "Flaschen gut belüftet lagern.", "Druck prüfen."])) is not None
    assert st.check_pdf(b"not a pdf") is not None
