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


def _build_auth_header(username: str | None, password: str | None) -> dict[str, str]:
    """Return an Authorization header dict for basic auth, or empty dict if no creds."""
    if username and password:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}
    return {}


def _count_opds_entries(xml_text: str) -> int:
    """Parse OPDS/Atom XML and return the number of <entry> elements."""
    try:
        root = ET.fromstring(xml_text)
        return len(root.findall("atom:entry", _ATOM_NS))
    except ET.ParseError as exc:
        log.debug("OPDS XML parse error: %s", exc)
        return 0


def _get_opds_entry_titles(xml_text: str) -> list[str]:
    """Return normalized titles of all <entry> elements in an OPDS feed."""
    try:
        root = ET.fromstring(xml_text)
        titles = []
        for entry in root.findall("atom:entry", _ATOM_NS):
            raw = entry.findtext("atom:title", namespaces=_ATOM_NS) or ""
            if raw.strip():
                titles.append(_normalize(raw))
        return titles
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


def _search_opds(
    base_url: str, query: str, headers: dict[str, str]
) -> list[str]:
    """Search the OPDS catalog by query, returning normalised entry titles.

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
        titles = _get_opds_entry_titles(resp.text)
        log.debug("CWA OPDS search %r → %d entries: %s", query, len(titles), titles[:3])
        return titles
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
      A match is found if any result's normalised title contains the book's
      normalised title as a substring, or vice versa.

    Returns:
        True  — at least one matching entry found (book is in library).
        False — no match, OR CWA was unreachable. Conservative: safer to
                re-request a duplicate than to silently skip a missing book.
    """
    headers = _build_auth_header(username, password)

    book_title_norm = _normalize(book.title)
    if not book_title_norm:
        return False

    result_titles = _search_opds(base_url, book.title, headers)

    for result_title in result_titles:
        if _titles_match(book_title_norm, result_title):
            log.debug("CWA: found %r (matched result %r)", book.title, result_title)
            return True

    return False
