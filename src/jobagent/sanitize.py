"""Sanitize job description HTML from ATS APIs.

Greenhouse/Ashby return HTML; Lever returns plain text (sometimes with
entities). We keep only a small allowlist of block/inline tags and strip
everything else, so descriptions are safe to render with |safe in templates.
"""

from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup

_ALLOWED_TAGS = {
    "p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u",
    "h1", "h2", "h3", "h4", "h5", "h6", "a", "code", "pre", "blockquote",
}


def sanitize_description(raw: str | None) -> str | None:
    """Clean an ATS job description. Returns None for empty input."""
    if not raw:
        return None

    # Lever sends plain text with escaped entities.
    if "<" not in raw and "&" in raw:
        raw = html.unescape(raw)

    soup = BeautifulSoup(raw, "lxml")

    # Drop scripts/styles entirely.
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()

    # Strip disallowed tags but keep their text content.
    for tag in soup.find_all(True):
        if tag.name not in _ALLOWED_TAGS:
            tag.unwrap()

    # Remove event-handler attributes and non-https hrefs on remaining tags.
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on") or attr not in {"href"}:
                del tag.attrs[attr]
        if tag.name == "a":
            href = tag.attrs.get("href", "")
            if not isinstance(href, str) or not href.lower().startswith(("http://", "https://")):
                tag.attrs.pop("href", None)
                tag.attrs["rel"] = "noopener"

    out = str(soup)
    # Collapse runs of 3+ newlines left behind by unwrapped divs.
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = out.strip()
    return out or None
