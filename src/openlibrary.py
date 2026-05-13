"""Open Library Books API client — ISBN-based metadata enrichment."""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

OPENLIBRARY_BOOKS_URL = "https://openlibrary.org/api/books"
_DEFAULT_TIMEOUT = 15

_session = requests.Session()
_session.headers.update({"User-Agent": "shelfmark-automated/1.0"})


def fetch_by_isbn(isbn: str, session: requests.Session | None = None) -> dict[str, Any] | None:
    """Fetch {title, author, publisher, pubdate} for ISBN from Open Library.

    Returns None when OL has no record for the ISBN, or on any network/parse error.
    The caller falls back to Hardcover/Goodreads data when None is returned.
    """
    _s = session or _session
    bibkey = f"ISBN:{isbn}"
    try:
        resp = _s.get(
            OPENLIBRARY_BOOKS_URL,
            params={"bibkeys": bibkey, "format": "json", "jscmd": "data"},
            timeout=_DEFAULT_TIMEOUT,
        )
        if not resp.ok:
            log.debug("Open Library: HTTP %d for ISBN %s", resp.status_code, isbn)
            return None
        return _parse_ol_response(resp.json(), isbn)
    except (requests.ConnectionError, requests.Timeout) as exc:
        log.warning("Open Library: network error for ISBN %s: %s", isbn, exc)
        return None
    except (ValueError, KeyError) as exc:
        log.warning("Open Library: parse error for ISBN %s: %s", isbn, exc)
        return None


def _parse_ol_response(data: dict[str, Any], isbn: str) -> dict[str, Any] | None:
    """Extract normalised fields from a raw OL Books API response dict."""
    entry = data.get(f"ISBN:{isbn}")
    if not entry:
        log.debug("Open Library: no record for ISBN %s", isbn)
        return None

    title: str | None = (entry.get("title") or "").strip() or None

    authors = entry.get("authors") or []
    author: str | None = ((authors[0].get("name") or "").strip() or None) if authors else None

    publishers = entry.get("publishers") or []
    publisher: str | None = (
        ((publishers[0].get("name") or "").strip() or None) if publishers else None
    )

    pubdate: str | None = (entry.get("publish_date") or "").strip() or None

    return {"title": title, "author": author, "publisher": publisher, "pubdate": pubdate}
