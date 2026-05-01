"""CWA (Calibre-Web Automated) OPDS library checker.

Uses the OPDS catalog search endpoint to determine whether a book is already
present in the library, avoiding redundant download requests.

Key findings from live testing:
- OPDS search works by title only — ISBN-based search returns empty results.
- Title+author combined queries also return 0 results.
- Search by title alone is fast and reliable.
- A book in a collection (e.g. "Surrounded by Idiots" found inside
  "Surrounded by Idiots & Surrounded by Psychopaths Collection") counts as
  owned, so we use a substring match rather than exact equality.
"""

from __future__ import annotations

import base64
import logging
import urllib.parse
import xml.etree.ElementTree as ET

import requests

from .models import Book, _normalize

log = logging.getLogger(__name__)

# Atom namespace used by OPDS / Calibre-Web
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# Words ignored when comparing author names (too common to be distinctive)
_AUTHOR_STOPWORDS: frozenset[str] = frozenset({"the", "a", "an", "of", "and", "&"})


def _build_auth_header(username: str | None, password: str | None) -> dict[str, str]:
    """Return an Authorization header dict for basic auth, or empty dict if no creds."""
    if username and password:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {}


def _get_opds_entries(xml_text: str) -> list[tuple[str, str]]:
    """Return (normalized_title, normalized_author) pairs for all <entry> elements.

    Author is an empty string when the entry has no <author><name> element.
    """
    try:
        root = ET.fromstring(xml_text)
        entries = []
        for entry in root.findall("atom:entry", _ATOM_NS):
            raw_title = entry.findtext("atom:title", namespaces=_ATOM_NS) or ""
            if not raw_title.strip():
                continue
            author_el = entry.find("atom:author/atom:name", _ATOM_NS)
            raw_author = author_el.text if author_el is not None and author_el.text else ""
            entries.append((_normalize(raw_title), _normalize(raw_author)))
        return entries
    except ET.ParseError as exc:
        log.debug("OPDS XML parse error: %s", exc)
        return []


def _titles_match(book_title_norm: str, result_title_norm: str) -> bool:
    """Return True if the book title appears within the result title.

    Forward-only check: the book's normalised title must be a substring of
    the result title. This handles exact matches and collections (e.g.
    "Surrounded by Idiots" found inside a multi-title collection entry).

    Deliberately NOT bidirectional: "The Martian" must not match a library
    entry called "The Martian Chronicles" just because the shorter title is
    a prefix of the longer one.
    """
    return book_title_norm in result_title_norm


def _authors_compatible(book_author_norm: str, entry_author_norm: str) -> bool:
    """Return True if the two author strings share at least one significant word.

    Significant means not in _AUTHOR_STOPWORDS.  If either side is an empty
    string (author unknown in feed or in book), returns True to avoid
    incorrectly rejecting a valid title match.
    """
    if not book_author_norm or not entry_author_norm:
        return True
    book_words = {w for w in book_author_norm.split() if w not in _AUTHOR_STOPWORDS}
    entry_words = {w for w in entry_author_norm.split() if w not in _AUTHOR_STOPWORDS}
    if not book_words or not entry_words:
        return True  # all stopwords — degenerate input, don't reject
    return bool(book_words & entry_words)


def _search_opds(
    base_url: str, query: str, headers: dict[str, str]
) -> list[tuple[str, str]]:
    """Search the OPDS catalog by query, returning (normalised_title, normalised_author) pairs.

    Returns an empty list on any error (network, auth, parse).
    """
    encoded = urllib.parse.quote(query, safe="")
    url = f"{base_url.rstrip('/')}/opds/search/{encoded}"
    try:
        resp = requests.get(
            url,
            headers={**headers, "Accept": "application/atom+xml,application/xml,*/*"},
            timeout=15,
        )
        if resp.status_code == 401:
            log.error(
                "CWA: authentication required but credentials are missing or wrong "
                "(HTTP 401). Set CWA_USERNAME and CWA_PASSWORD."
            )
            return []
        if not resp.ok:
            log.warning("CWA OPDS search returned HTTP %d for query %r", resp.status_code, query)
            return []
        entries = _get_opds_entries(resp.text)
        log.debug("CWA OPDS search %r → %d entries: %s", query, len(entries), entries[:3])
        return entries
    except (requests.ConnectionError, requests.Timeout) as exc:
        log.warning("CWA OPDS unreachable during search for %r: %s", query, exc)
        return []


def is_book_in_library(
    book: Book,
    base_url: str,
    username: str | None,
    password: str | None,
) -> bool:
    """Check whether the book already exists in the CWA library via OPDS search.

    Strategy:
      Search by title only (CWA OPDS does not index by ISBN, and combined
      title+author queries consistently return empty results).
      A match requires both a title substring match and author compatibility.
      Author compatibility: at least one significant word in common, or either
      side has no author info (backward compatible with feeds that omit author).

    Returns:
        True  — at least one matching entry found (book is in library).
        False — no match, OR CWA was unreachable. Conservative: safer to
                re-request a duplicate than to silently skip a missing book.
    """
    headers = _build_auth_header(username, password)

    book_title_norm = _normalize(book.title)
    if not book_title_norm:
        return False

    book_author_norm = _normalize(book.author)
    result_entries = _search_opds(base_url, book.title, headers)

    for result_title, result_author in result_entries:
        if not _titles_match(book_title_norm, result_title):
            continue
        if not _authors_compatible(book_author_norm, result_author):
            log.debug(
                "CWA: title match for %r rejected — author mismatch (wanted %r, got %r)",
                book.title, book_author_norm, result_author,
            )
            continue
        log.debug(
            "CWA: found %r (matched title=%r author=%r)",
            book.title, result_title, result_author,
        )
        return True

    return False
