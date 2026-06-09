"""Best-effort, dependency-free language detection + locale helpers.

Two jobs, both deliberately lightweight:

* :func:`detect_language` scores text against small stop-word sets for a handful
  of common languages and returns the best match (or ``None``). It is a
  *heuristic* — short job titles carry little signal — so callers treat ``None``
  as "unknown" and never depend on it being correct.
* :func:`accept_language_header` maps a language code to an ``Accept-Language``
  header value, used to hint LinkedIn's guest endpoint toward a locale.

Kept in its own module (single responsibility) so the detector can be swapped
for a real library later without touching the models or storage.
"""

from __future__ import annotations

import re

# Human-readable names for the languages we can tag/filter, keyed by ISO code.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "nl": "Dutch",
    "it": "Italian",
    "pt": "Portuguese",
}

# Distinctive, high-frequency stop words per language. Chosen to minimise
# cross-language collisions; detection is a margin-based vote over these.
_STOPWORDS: dict[str, set[str]] = {
    "en": {"the", "and", "for", "with", "you", "our", "are", "will", "your", "team", "join"},
    "de": {"und", "der", "die", "das", "für", "mit", "sie", "wir", "ist", "ein", "eine", "bei"},
    "fr": {"et", "les", "des", "pour", "avec", "vous", "nous", "une", "dans", "est", "sur", "au"},
    "es": {"los", "las", "para", "con", "una", "nuestro", "experiencia", "trabajo", "como", "más"},
    "nl": {"het", "een", "van", "voor", "met", "wij", "zijn", "onze", "naar", "bij", "jij", "ben"},
    "it": {"il", "gli", "per", "con", "una", "nostro", "esperienza", "lavoro", "della", "che"},
    "pt": {"os", "as", "para", "com", "uma", "nosso", "experiência", "trabalho", "como", "você"},
}

_WORD_RE = re.compile(r"[a-zA-ZÀ-ÿ]+")


def detect_language(*texts: str | None, min_hits: int = 2) -> str | None:
    """Return the best-guess ISO language code for ``texts``, or ``None``.

    Tokenises the combined text and votes by stop-word membership per language.
    Returns ``None`` when the winner scores below ``min_hits`` or ties the
    runner-up (ambiguous) — favouring "unknown" over a confident wrong guess.
    """
    words: list[str] = []
    for text in texts:
        if text:
            words.extend(match.lower() for match in _WORD_RE.findall(text))
    if not words:
        return None
    scores = {lang: sum(w in stop for w in words) for lang, stop in _STOPWORDS.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best_lang, best_score = ranked[0]
    if best_score < min_hits:
        return None
    if len(ranked) > 1 and ranked[1][1] == best_score:  # ambiguous tie
        return None
    return best_lang


def accept_language_header(code: str | None) -> str | None:
    """Build an ``Accept-Language`` value for an ISO code, or ``None``.

    ``"de"`` → ``"de,en;q=0.8"`` so LinkedIn prefers the chosen language but
    still degrades to English; English maps to ``"en-US,en;q=0.9"``. Unknown or
    empty codes return ``None`` (the scraper then uses its default header).
    """
    if not code or code not in LANGUAGE_NAMES:
        return None
    if code == "en":
        return "en-US,en;q=0.9"
    return f"{code},en;q=0.8"
