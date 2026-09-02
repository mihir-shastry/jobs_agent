"""Classify job titles into experience levels and categories.

Keyword matching against lowercased titles, longest/most-specific match wins
by check order: levels are checked from most senior-specific markers first
when ambiguous (e.g. "Senior Intern Coordinator" is nonsense; "intern" beats
"senior" only when it appears as its own token set — see tests).
"""

from __future__ import annotations

import re

from .models import CATEGORIES, EXPERIENCE_LEVELS

# ---------------------------------------------------------------------------
# Experience level
# ---------------------------------------------------------------------------

_LEVEL_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    # Internship markers are unambiguous.
    (
        "internship",
        ("intern", "co-op", "co op", "coop", "placement year", "summer analyst"),
    ),
    (
        "new_grad",
        ("new grad", "new grad", "recent grad", "university grad", "graduate engineer",
         "campus hire", "early career"),
    ),
    (
        "senior",
        ("senior", "sr.", "sr ", "staff", "principal", "distinguished", "fellow",
         "lead engineer", "tech lead", "head of", "director",
         "engineering manager", "eng manager", "engineering leader",
         "manager ii", "manager iii", "architect"),
    ),
    (
        "mid",
        ("mid-level", "mid level", "intermediate", "ii", "iii", "2-5 years", "3+ years"),
    ),
    (
        "entry",
        ("junior", "jr.", "jr ", "entry level", "entry-level", "associate",
         "assistant", "graduate", "trainee", "i"),
    ),
]


def classify_level(title: str) -> str:
    """Classify experience level from a job title. Defaults to 'entry'."""
    t = f" {title.lower()} "
    t = re.sub(r"[-_/]", " ", t)

    # Internship wins outright — "Software Engineer Intern, Senior Year" etc.
    if any(_word_boundary_hit(t, kw) for kw in _LEVEL_PATTERNS[0][1]):
        return "internship"

    # "new grad" beats senior markers ("Senior" appears in "Senior New Grad"? rare).
    if any(_word_boundary_hit(t, kw) for kw in _LEVEL_PATTERNS[1][1]):
        return "new_grad"

    # Senior: explicit senior/staff/principal markers.
    if any(_word_boundary_hit(t, kw) for kw in _LEVEL_PATTERNS[2][1]):
        return "senior"

    # Mid: roman numerals or explicit mid markers, but "II"/"III" only count
    # as standalone tokens (avoid matching "II" inside "SwiftUI"? already spaced).
    if any(_word_boundary_hit(t, kw) for kw in _LEVEL_PATTERNS[3][1]):
        return "mid"

    # Entry: junior/associate/roman numeral I.
    if any(_word_boundary_hit(t, kw) for kw in _LEVEL_PATTERNS[4][1]):
        return "entry"

    return "entry"


def _word_boundary_hit(text: str, keyword: str) -> bool:
    """True when keyword appears as whole word(s) in text."""
    pattern = r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------

_CATEGORY_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    (
        "quant",
        ("quant", "quantitative", "quantitative analyst", "quantitative researcher",
         "quantitative developer", "algo", "algorithmic trading"),
    ),
    (
        "data_science",
        ("data scientist", "data science", "data analyst", "data engineer",
         "machine learning", "ml engineer", "ml", "ai engineer", "ai ",
         "research scientist", "nlp", "computer vision", "applied scientist"),
    ),
    (
        "design",
        ("designer", "design", "ux", "ui ", "ui engineer", "product design",
         "graphic", "illustrator", "brand"),
    ),
    (
        "pm",
        ("product manager", "product management", "program manager",
         "technical program manager", "tpm", "product owner"),
    ),
    (
        "swe",
        ("software engineer", "software engineering", "software developer",
         "swe", "sde", "backend", "back end", "frontend", "front end",
         "full stack", "fullstack", "developer", "engineer", "engineering",
         "devops", "sre", "site reliability", "platform engineer",
         "infrastructure", "mobile engineer", "ios engineer",
         "android engineer", "qa engineer", "test engineer",
         "security engineer", "systems engineer", "embedded"),
    ),
]


def classify_category(title: str) -> str:
    """Classify job category from a title. Defaults to 'other'."""
    t = f" {title.lower()} "
    t = re.sub(r"[-_/]", " ", t)

    for category, keywords in _CATEGORY_PATTERNS:
        if any(_word_boundary_hit(t, kw) for kw in keywords):
            return category
    return "other"


def classify(title: str) -> tuple[str, str]:
    """Convenience: returns (experience_level, category)."""
    return classify_level(title), classify_category(title)


__all__ = ["classify", "classify_level", "classify_category", "EXPERIENCE_LEVELS", "CATEGORIES"]
