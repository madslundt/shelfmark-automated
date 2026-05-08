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
import html as _html
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET

import requests

from .models import Book, _normalize

log = logging.getLogger(__name__)

# Atom namespace used by OPDS / Calibre-Web
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# Words ignored when comparing author names (too common to be distinctive)
_AUTHOR_STOPWORDS: frozenset[str] = frozenset({"the", "a", "an", "of", "and", "&"})

# Matches Goodreads-style series suffixes: "(Series Name, #3)" or "(Series #3)"
_SERIES_SUFFIX_RE = re.compile(r"\s*\([^)]*#\s*\d+\)\s*$")

# Extracts the numeric Calibre book ID from an OPDS link href.
# Calibre-Web serves these as /opds/download/<id>/epub/ or /opds/cover/<id>
_BOOK_ID_RE = re.compile(r"/opds/(?:download|cover)/(\d+)")


class CWAAuthError(Exception):
    """Raised when CWA web session authentication cannot be established."""


def _strip_series_suffix(title: str) -> str:
    """Remove a trailing Goodreads series annotation such as '(Dark Future, #1)'."""
    return _SERIES_SUFFIX_RE.sub("", title).strip()


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
    """Return True if the two normalised titles refer to the same book.

    Forward check: book title is a substring of the result title — handles
    exact matches and collections ("Surrounded by Idiots" inside a multi-title
    collection entry).

    Reverse check: result title is a substring of the book title — handles
    books where CWA stores only the base title but Goodreads appends a long
    subtitle (e.g. CWA has "The Innovator's Dilemma", Goodreads has
    "The Innovator's Dilemma: The Revolutionary Book..."). Guarded by a
    3-word minimum on the result title so that short 1-2 word result titles
    cannot spuriously match any book whose title begins with those words.
    """
    if book_title_norm in result_title_norm:
        return True
    if len(result_title_norm.split()) >= 3 and result_title_norm in book_title_norm:
        return True
    return False


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
) -> list[tuple[str, str]] | None:
    """Search the OPDS catalog by query, returning (normalised_title, normalised_author) pairs.

    Returns:
        list  — successful response (may be empty if no results).
        None  — network/timeout error; caller should treat as "unknown" not "not found".
    """
    encoded = urllib.parse.quote(query, safe="")
    url = f"{base_url.rstrip('/')}/opds/search/{encoded}"
    try:
        resp = requests.get(
            url,
            headers={**headers, "Accept": "application/atom+xml,application/xml,*/*"},
            timeout=30,
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
        return None


def _extract_book_id(entry_el) -> int | None:
    """Extract the Calibre numeric book ID from an OPDS entry element's acquisition links."""
    for link in entry_el.findall("atom:link", _ATOM_NS):
        href = link.get("href", "")
        m = _BOOK_ID_RE.search(href)
        if m:
            return int(m.group(1))
    return None


def _get_opds_entries_with_id(xml_text: str) -> list[tuple[str, str, int | None]]:
    """Return (normalised_title, normalised_author, calibre_book_id) for all entries."""
    try:
        root = ET.fromstring(xml_text)
        entries = []
        for entry in root.findall("atom:entry", _ATOM_NS):
            raw_title = entry.findtext("atom:title", namespaces=_ATOM_NS) or ""
            if not raw_title.strip():
                continue
            author_el = entry.find("atom:author/atom:name", _ATOM_NS)
            raw_author = author_el.text if author_el is not None and author_el.text else ""
            book_id = _extract_book_id(entry)
            entries.append((_normalize(raw_title), _normalize(raw_author), book_id))
        return entries
    except ET.ParseError as exc:
        log.debug("OPDS XML parse error: %s", exc)
        return []


def is_book_in_library(
    book: Book,
    base_url: str,
    username: str | None,
    password: str | None,
) -> bool | None:
    """Check whether the book already exists in the CWA library via OPDS search.

    Strategy:
      Search by title only (CWA OPDS does not index by ISBN, and combined
      title+author queries consistently return empty results).
      A match requires both a title substring match and author compatibility.
      Author compatibility: at least one significant word in common, or either
      side has no author info (backward compatible with feeds that omit author).

    Calibre stores a "sort title" that strips leading articles ("The One" →
    "One, The"). Some OPDS implementations index by sort title, so searching
    for "The One" returns nothing while searching for "One" succeeds. If the
    full-title search yields no match and the title starts with a common
    article (The/A/An), a second search without the article is attempted.

    Returns:
        True  — at least one matching entry found (book is in library).
        False — queries succeeded but no match found (book is not in library).
        None  — all queries timed out or failed; status unknown. Caller should
                skip this book for now and retry on the next sync cycle.
    """
    headers = _build_auth_header(username, password)

    # Strip Goodreads series suffix "(Series, #N)" before searching and comparing.
    # CWA stores "The One", not "The One (Dark Future #1)".
    clean_title = _strip_series_suffix(book.title)
    book_title_norm = _normalize(clean_title)
    if not book_title_norm:
        return False

    book_author_norm = _normalize(book.author)

    # Build the list of queries to try. The fallback strips a leading article so
    # that "The One" also tries "One" (handles Calibre's sort-title indexing).
    queries = [clean_title]
    for article in ("The ", "A ", "An "):
        if clean_title.startswith(article):
            queries.append(clean_title[len(article):])
            break

    any_succeeded = False
    for query in queries:
        entries = _search_opds(base_url, query, headers)
        if entries is None:
            continue  # timeout/network error — try next query
        any_succeeded = True
        for result_title, result_author in entries:
            if not _titles_match(book_title_norm, result_title):
                continue
            if _authors_compatible(book_author_norm, result_author):
                log.debug(
                    "CWA: found %r via query %r (matched title=%r author=%r)",
                    book.title, query, result_title, result_author,
                )
                return True
            # Author mismatch — but an exact title match on a specific (≥5 significant
            # words) title is trusted anyway: Calibre edition metadata often differs
            # from what Goodreads/Hardcover reports (e.g. different editor attribution).
            sig_words = sum(1 for w in result_title.split() if w not in _AUTHOR_STOPWORDS)
            if book_title_norm == result_title and sig_words >= 5:
                log.debug(
                    "CWA: found %r via exact title match — author mismatch ignored "
                    "(wanted %r, got %r)",
                    book.title, book_author_norm, result_author,
                )
                return True
            log.debug(
                "CWA: title match for %r rejected — author mismatch (wanted %r, got %r)",
                book.title, book_author_norm, result_author,
            )

    if not any_succeeded:
        log.warning(
            "CWA: all OPDS queries timed out for %r — skipping this cycle", book.title
        )
        return None

    return False


def find_book_in_library(
    book: Book,
    base_url: str,
    username: str | None,
    password: str | None,
) -> int | None:
    """Return the Calibre book ID if the book is in the CWA library, else None.

    Uses the same OPDS title/author matching strategy as is_book_in_library().
    Returns None on network error or when no match is found.
    """
    headers = _build_auth_header(username, password)

    clean_title = _strip_series_suffix(book.title)
    book_title_norm = _normalize(clean_title)
    if not book_title_norm:
        return None

    book_author_norm = _normalize(book.author)

    queries = [clean_title]
    for article in ("The ", "A ", "An "):
        if clean_title.startswith(article):
            queries.append(clean_title[len(article):])
            break

    for query in queries:
        encoded = urllib.parse.quote(query, safe="")
        url = f"{base_url.rstrip('/')}/opds/search/{encoded}"
        try:
            resp = requests.get(
                url,
                headers={**headers, "Accept": "application/atom+xml,application/xml,*/*"},
                timeout=30,
            )
            if not resp.ok:
                continue
            entries = _get_opds_entries_with_id(resp.text)
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.warning("CWA OPDS unreachable during find_book_in_library for %r: %s", query, exc)
            return None

        for result_title, result_author, book_id in entries:
            if not _titles_match(book_title_norm, result_title):
                continue
            if _authors_compatible(book_author_norm, result_author):
                log.debug(
                    "CWA: found %r (id=%s) via query %r",
                    book.title, book_id, query,
                )
                return book_id
            sig_words = sum(1 for w in result_title.split() if w not in _AUTHOR_STOPWORDS)
            if book_title_norm == result_title and sig_words >= 5:
                return book_id

    return None


def find_mismatched_author(
    book: Book,
    base_url: str,
    username: str | None,
    password: str | None,
) -> tuple[int, str] | None:
    """Return (calibre_book_id, wrong_author) if the book is in CWA with a mismatched author.

    Uses the same OPDS title search + matching as find_book_in_library.
    Returns None when:
      - Book is not in the library.
      - Book is in the library with a compatible author (no fix needed).
      - Network error or book_id cannot be extracted from OPDS links.
    """
    headers = _build_auth_header(username, password)

    clean_title = _strip_series_suffix(book.title)
    book_title_norm = _normalize(clean_title)
    if not book_title_norm:
        return None

    book_author_norm = _normalize(book.author)

    queries = [clean_title]
    for article in ("The ", "A ", "An "):
        if clean_title.startswith(article):
            queries.append(clean_title[len(article):])
            break

    for query in queries:
        encoded = urllib.parse.quote(query, safe="")
        url = f"{base_url.rstrip('/')}/opds/search/{encoded}"
        try:
            resp = requests.get(
                url,
                headers={**headers, "Accept": "application/atom+xml,application/xml,*/*"},
                timeout=30,
            )
            if not resp.ok:
                continue
            entries = _get_opds_entries_with_id(resp.text)
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.warning("CWA OPDS unreachable during find_mismatched_author for %r: %s", query, exc)
            return None

        for result_title, result_author, book_id in entries:
            if not _titles_match(book_title_norm, result_title):
                continue
            if _authors_compatible(book_author_norm, result_author):
                return None  # author is already correct
            if book_id is not None:
                log.debug(
                    "CWA: author mismatch for %r — CWA has %r, correct is %r (id=%d)",
                    book.title, result_author, book.author, book_id,
                )
                return (book_id, result_author)

    return None


class CWAClient:
    """Stateful CWA web session client for marking books as read.

    Uses form-based login (with CSRF token extraction) to establish a session,
    then POST /ajax/book/{id}/readstatus to mark books as read.

    Requires username and password — CWA read status is per-user and cannot
    be set without an authenticated session.
    """

    def __init__(self, base_url: str, username: str | None, password: str | None) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._session = requests.Session()
        self._authenticated = False
        self._csrf_token: str = ""

    def mark_as_read(self, book_id: int) -> bool:
        """Mark a book as read in CWA.

        Returns True on success (2xx), False on any failure.
        Raises CWAAuthError immediately if credentials are not configured.
        """
        if not self._username or not self._password:
            raise CWAAuthError(
                "CWA_USERNAME and CWA_PASSWORD are required to mark books as read"
            )

        if not self._authenticated:
            self._login()

        return self._post_read_status(book_id)

    def get_book_metadata(self, book_id: int) -> dict[str, str] | None:
        """Return current metadata for a book as scraped from the CWA admin page.

        Returns None if the admin page is unreachable or returns a non-2xx status.
        Raises CWAAuthError if credentials are not configured.
        """
        if not self._username or not self._password:
            raise CWAAuthError(
                "CWA_USERNAME and CWA_PASSWORD are required to read book metadata"
            )
        if not self._authenticated:
            self._login()

        resp = self._session.get(f"{self._base_url}/admin/book/{book_id}")
        if not resp.ok:
            log.debug("CWA: admin page for book %d returned HTTP %d", book_id, resp.status_code)
            return None

        _, fields = self._parse_admin_page(resp.text)
        return fields

    def update_book_metadata(self, book_id: int, updates: dict[str, str]) -> bool:
        """Apply field updates to a book in CWA via the admin edit form.

        Fetches the current admin page for existing values and CSRF token, merges
        *updates* into them, then POSTs the full form.  Only keys present in
        *updates* are changed; all other fields are preserved as-is.

        Requires the CWA user to have 'Edit books' permission (Admin → Edit User).
        Returns True on success, False on any failure.
        Raises CWAAuthError if credentials are not configured.
        """
        if not self._username or not self._password:
            raise CWAAuthError(
                "CWA_USERNAME and CWA_PASSWORD are required to edit book metadata"
            )
        if not self._authenticated:
            self._login()

        edit_url = f"{self._base_url}/admin/book/{book_id}"
        edit_get = self._session.get(edit_url)
        if not edit_get.ok:
            log.warning(
                "CWA: admin edit page for book %d returned HTTP %d — "
                "ensure 'Edit books' permission is enabled for your CWA account",
                book_id, edit_get.status_code,
            )
            return False

        page_html = edit_get.text
        csrf, fields = self._parse_admin_page(page_html)

        form: dict[str, str] = {
            "book_id": str(book_id),
            "csrf_token": csrf,
            **fields,
            **updates,
        }

        # Preserve all book identifiers (amazon, goodreads, isbn, hardcover-id, etc.)
        for im in re.finditer(
            r'name="(identifier-(?:type|val)-\d+)"[^>]*value="([^"]*)"', page_html
        ):
            form[im.group(1)] = _html.unescape(im.group(2))

        post_resp = self._session.post(
            edit_url,
            data=form,
            headers={"X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest"},
        )
        if post_resp.ok:
            log.debug("CWA: book %d metadata updated: %s", book_id, list(updates.keys()))
            return True
        log.warning(
            "CWA: failed to update metadata for book %d: HTTP %d",
            book_id, post_resp.status_code,
        )
        return False

    def update_book_author(
        self,
        book_id: int,
        correct_author: str,
        correct_title: str | None = None,
    ) -> bool:
        """Update the author (and optionally title) of a book. Delegates to update_book_metadata."""
        updates: dict[str, str] = {"authors": correct_author}
        if correct_title is not None:
            updates["title"] = correct_title
        return self.update_book_metadata(book_id, updates)

    def _parse_admin_page(self, page_html: str) -> tuple[str, dict[str, str]]:
        """Parse a CWA admin book-edit page, returning (csrf_token, {field: value})."""
        m = re.search(r'name="csrf_token"[^>]+value="([^"]+)"', page_html)
        if not m:
            m = re.search(r'csrf_token.*?value="([^"]+)"', page_html, re.DOTALL)
        csrf = m.group(1) if m else self._csrf_token

        def _field(name: str) -> str:
            fm = re.search(rf'name="{re.escape(name)}"[^>]*value="([^"]*)"', page_html)
            if fm:
                return _html.unescape(fm.group(1))
            fm = re.search(rf'value="([^"]*)"[^>]*name="{re.escape(name)}"', page_html)
            return _html.unescape(fm.group(1)) if fm else ""

        def _textarea(name: str) -> str:
            fm = re.search(
                rf'<textarea[^>]*name="{re.escape(name)}"[^>]*>(.*?)</textarea>',
                page_html, re.DOTALL,
            )
            return _html.unescape(fm.group(1)) if fm else ""

        fields = {
            "title": _field("title"),
            "authors": _field("authors"),
            "tags": _field("tags"),
            "series": _field("series"),
            "series_index": _field("series_index"),
            "pubdate": _field("pubdate"),
            "publisher": _field("publisher"),
            "languages": _field("languages"),
            "rating": _field("rating"),
            "comments": _textarea("comments"),
            "cover_url": _field("cover_url"),
        }
        return csrf, fields

    def _login(self) -> None:
        """Login to CWA via the web form, storing the session cookie."""
        try:
            resp = self._session.get(f"{self._base_url}/login", timeout=10)
            resp.raise_for_status()
            m = re.search(
                r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']',
                resp.text,
            )
            if not m:
                m = re.search(
                    r'value=["\']([^"\']+)["\'][^>]*name=["\']csrf_token["\']',
                    resp.text,
                )
            self._csrf_token = m.group(1) if m else ""

            post_resp = self._session.post(
                f"{self._base_url}/login",
                data={
                    "username": self._username,
                    "password": self._password,
                    "csrf_token": self._csrf_token,
                    "remember_me": "on",
                    "next": "/",
                },
                timeout=15,
                allow_redirects=True,
            )
            if "/login" in post_resp.url:
                raise CWAAuthError(
                    "CWA login failed — check CWA_USERNAME and CWA_PASSWORD"
                )
            self._authenticated = True
            log.info("CWA: logged in as %r", self._username)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise CWAAuthError(f"Cannot reach CWA at {self._base_url}: {exc}") from exc

    def _post_read_status(self, book_id: int) -> bool:
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if self._csrf_token:
            headers["X-CSRFToken"] = self._csrf_token
        try:
            resp = self._session.post(
                f"{self._base_url}/ajax/toggleread/{book_id}",
                headers=headers,
                timeout=10,
            )
            if resp.ok:
                log.debug("CWA: book %d marked as read", book_id)
                return True
            log.warning(
                "CWA: mark-as-read returned HTTP %d for book_id=%d", resp.status_code, book_id
            )
            return False
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.error("CWA: network error marking book %d as read: %s", book_id, exc)
            return False
