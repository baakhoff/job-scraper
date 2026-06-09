"""Tests for the heuristic language detector + locale header helper."""

from __future__ import annotations

from src.language import accept_language_header, detect_language


def test_detect_language_english() -> None:
    text = "We are looking for a backend engineer to join our team"
    assert detect_language(text) == "en"


def test_detect_language_german() -> None:
    text = "Wir suchen einen Entwickler für unser Projekt und die Zukunft bei uns"
    assert detect_language(text) == "de"


def test_detect_language_none_on_thin_or_empty_text() -> None:
    # Too few stop-word hits → unknown rather than a confident wrong guess.
    assert detect_language("Senior Engineer") is None
    assert detect_language("") is None
    assert detect_language(None) is None


def test_detect_language_combines_multiple_texts() -> None:
    # Signal can come from any of the provided fields (title, snippet, body).
    assert detect_language("Engineer", None, "and you will join our team for the work") == "en"


def test_accept_language_header() -> None:
    assert accept_language_header("de") == "de,en;q=0.8"
    assert accept_language_header("en") == "en-US,en;q=0.9"
    # Unknown / empty codes yield no header (scraper falls back to its default).
    assert accept_language_header(None) is None
    assert accept_language_header("") is None
    assert accept_language_header("xx") is None
