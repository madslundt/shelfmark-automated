"""Shelfmark API client with transparent session-based authentication.

Supports three auth modes detected at runtime:
  1. No-auth mode  — AUTH_METHOD=none on the Shelfmark side; all endpoints public.
  2. Cookie session — username/password login; session valid for ~7 days.
  3. Missing creds  — raises ShelfmarkAuthError immediately with a clear message.

Request flow (discovered from live testing):
  POST /api/requests requires book_data.provider + book_data.provider_id.
  These are obtained by first calling GET /api/metadata/search?query=<title author>.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import requests

from .models import Book, _normalize

log = logging.getLogger(__name__)

# Re-authenticate 12 hours before the 7-day session expires
_SESSION_TTL = timedelta(days=6, hours=12)

# Words ignored when comparing author/title fields during metadata scoring
_METADATA_STOPWORDS: frozenset[str] = frozenset({"the", "a", "an", "of", "and", "&"})
# Minimum Jaccard-based score for a metadata result to be accepted
_MIN_METADATA_SCORE: float = 0.3


class ShelfmarkAuthError(Exception):
    """Raised when Shelfmark authentication cannot be established."""


def _score_metadata_result(
    result: dict,
    book_title_words: set[str],
    book_author_words: set[str],
) -> float:
    """Compute a relevance score [0.0–1.0] for a metadata result against a Book.

    Uses Jaccard similarity of word sets: 0.6 * title_score + 0.4 * author_score.
    Returns 0.5 (neutral) when the result has neither title nor author fields.

    book_title_words and book_author_words are precomputed by the caller to avoid
    redundant normalization when scoring multiple results for the same book.
    """
    result_title = result.get("title", "") or ""
    result_author = result.get("author", "") or ""

    if not result_title and not result_author:
        return 0.5

    def _jaccard(a: set[str], b: set[str]) -> float:
        union = a | b
        return len(a & b) / len(union) if union else 1.0

    title_score = _jaccard(
        book_title_words,
        set(_normalize(result_title).split()),
    )
    res_aw = {w for w in _normalize(result_author).split() if w not in _METADATA_STOPWORDS}
    author_score = _jaccard(book_author_words, res_aw) if (book_author_words and res_aw) else 0.5

    return 0.6 * title_score + 0.4 * author_score


class ShelfmarkClient:
    """Stateful Shelfmark API client.

    Authentication is handled lazily: the first actual book request triggers
    auth detection and, if needed, login. The session cookie is then reused
    for the lifetime of this object (re-authenticated before expiry).
    """

    def __init__(
        self,
        base_url: str,
        username: str | None,
        password: str | None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._session = requests.Session()
        self._authenticated = False
        self._no_auth_mode = False
        self._auth_time: datetime | None = None
        self._submission_mode: str | None = None  # "request" or "download"; fetched lazily

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request_book(self, book: Book) -> bool:
        """Submit a single book download request to Shelfmark.

        Checks the request policy first to decide between POST /api/requests
        (request workflow enabled) and POST /api/releases/download (download mode).

        Returns True on success (2xx), False on any failure.
        Logs errors and returns False rather than raising, so callers can
        process remaining books uninterrupted.
        """
        try:
            self._ensure_ready()
        except ShelfmarkAuthError as exc:
            log.critical("Shelfmark auth failed: %s", exc)
            return False

        if self._submission_mode is None:
            self._submission_mode = self._fetch_submission_mode()

        metadata = self._search_metadata(book.title, book.author, isbn=book.best_isbn())
        if not metadata:
            log.warning(
                "Shelfmark: no metadata result for %r — skipping request", book.title
            )
            return False

        if self._submission_mode == "download":
            payload = self._build_download_payload(book, metadata)
            submit_fn = self._post_download
        else:
            payload = self._build_request_payload(book, metadata)
            submit_fn = self._post_request

        try:
            return submit_fn(payload, book.title)
        except ShelfmarkAuthError:
            log.info("Shelfmark: session expired mid-request, re-authenticating")
            self._authenticated = False
            try:
                self._login()
                return submit_fn(payload, book.title)
            except (ShelfmarkAuthError, requests.RequestException) as exc:
                log.error("Shelfmark: retry after re-auth failed for %r: %s", book.title, exc)
                return False

    def request_books_batch(self, books: list[Book]) -> dict[str, bool]:
        """Submit multiple book requests.

        Fetches metadata for each book first (required for provider/provider_id),
        then attempts a batch POST; falls back to individual requests if the
        batch endpoint is unavailable.

        Returns:
            Mapping of book.normalized_key() → success bool.
        """
        if not books:
            return {}

        try:
            self._ensure_ready()
        except ShelfmarkAuthError as exc:
            log.critical("Shelfmark auth failed — skipping all %d requests: %s", len(books), exc)
            return {b.normalized_key(): False for b in books}

        if self._submission_mode is None:
            self._submission_mode = self._fetch_submission_mode()

        build_payload = (
            self._build_download_payload
            if self._submission_mode == "download"
            else self._build_request_payload
        )
        submit_fn = (
            self._post_download
            if self._submission_mode == "download"
            else self._post_request
        )

        # Fetch metadata for all books first
        payloads: list[tuple[Book, dict]] = []
        results: dict[str, bool] = {}
        for book in books:
            metadata = self._search_metadata(book.title, book.author, isbn=book.best_isbn())
            if metadata:
                payloads.append((book, build_payload(book, metadata)))
            else:
                log.warning("Shelfmark: no metadata for %r — skipping", book.title)

        if not payloads:
            return results

        # Try batch endpoint (request mode only — download mode has no batch endpoint)
        if self._submission_mode != "download":
            batch_results = self._try_batch(payloads)
            if batch_results is not None:
                results.update(batch_results)
                return results
            log.debug(
                "Shelfmark: falling back to individual requests for %d books", len(payloads)
            )
        else:
            log.debug(
                "Shelfmark: download mode — submitting %d books individually", len(payloads)
            )

        for book, payload in payloads:
            try:
                ok = submit_fn(payload, book.title)
            except ShelfmarkAuthError:
                self._authenticated = False
                try:
                    self._login()
                    ok = submit_fn(payload, book.title)
                except Exception as exc:
                    log.error(
                        "Shelfmark: request failed for %r after re-auth attempt: %s",
                        book.title,
                        exc,
                    )
                    ok = False
            results[book.normalized_key()] = ok

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_ready(self) -> None:
        """Ensure the client is ready to make requests (auth established)."""
        if self._no_auth_mode:
            return

        if self._authenticated and self._auth_time:
            if datetime.now() - self._auth_time < _SESSION_TTL:
                return  # Session is still fresh

        # Not yet authenticated or session is stale
        if self._username and self._password:
            self._login()
        else:
            # No credentials configured — probe the auth/check endpoint
            self._detect_no_auth_mode()

    def _detect_no_auth_mode(self) -> None:
        """Check whether Shelfmark is running with AUTH_METHOD=none.

        /api/auth/check always returns HTTP 200 — use the 'auth_required'
        field in the JSON body to determine if authentication is needed.
        """
        try:
            resp = self._session.get(
                f"{self._base_url}/api/auth/check",
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
            if body.get("auth_required", True):
                raise ShelfmarkAuthError(
                    "Shelfmark requires authentication but SHELFMARK_USERNAME / "
                    "SHELFMARK_PASSWORD are not configured."
                )
            log.info("Shelfmark: no-auth mode detected — proceeding without credentials")
            self._no_auth_mode = True
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise ShelfmarkAuthError(
                f"Cannot reach Shelfmark at {self._base_url}: {exc}"
            ) from exc
        except (ValueError, KeyError):
            # JSON parse failure — assume no-auth for safety
            log.warning("Shelfmark: could not parse /api/auth/check response, assuming no-auth")
            self._no_auth_mode = True

    def _login(self) -> None:
        """POST credentials to /api/auth/login and store the session cookie."""
        try:
            resp = self._session.post(
                f"{self._base_url}/api/auth/login",
                json={"username": self._username, "password": self._password},
                timeout=15,
            )
            if resp.status_code in (401, 403):
                raise ShelfmarkAuthError(
                    f"Shelfmark login failed (HTTP {resp.status_code}) — "
                    "check SHELFMARK_USERNAME and SHELFMARK_PASSWORD."
                )
            resp.raise_for_status()
            self._authenticated = True
            self._auth_time = datetime.now()
            log.info("Shelfmark: logged in successfully as %r", self._username)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise ShelfmarkAuthError(
                f"Cannot reach Shelfmark at {self._base_url}: {exc}"
            ) from exc

    def _search_metadata(self, title: str, author: str, isbn: str | None = None) -> dict | None:
        """Search Shelfmark's metadata provider and return the best-matching result.

        If an ISBN is provided it is tried first, since ISBN queries return a
        precise match without needing title/author scoring. Falls back to a
        title + author query when the ISBN search yields no results.

        Returns None if no confident match is found (logs a warning).
        """
        book_obj = Book(title=title, author=author or "")

        if isbn:
            result = self._run_metadata_query(isbn, book_obj)
            if result:
                log.debug("Shelfmark: metadata found via ISBN %s for %r", isbn, title)
                return result

        result = self._run_metadata_query(f"{title} {author}".strip(), book_obj)
        if result is None:
            log.warning("Shelfmark: no confident metadata match for %r — skipping request", title)
        return result

    def _run_metadata_query(self, query: str, book: Book) -> dict | None:
        """Execute one /api/metadata/search query and return the best-scored result.

        Returns None (without warning) if the query produces no results or if
        the best match scores below _MIN_METADATA_SCORE. Raises ShelfmarkAuthError
        on HTTP 401 so the caller can re-authenticate.
        """
        try:
            resp = self._session.get(
                f"{self._base_url}/api/metadata/search",
                params={"query": query},
                timeout=30,
            )
            if resp.status_code == 401:
                self._authenticated = False
                raise ShelfmarkAuthError("Session expired during metadata search (HTTP 401)")
            if not resp.ok:
                log.debug(
                    "Shelfmark metadata search returned HTTP %d for query %r",
                    resp.status_code, query,
                )
                return None
            data = resp.json()
            books = data.get("books", []) if isinstance(data, dict) else data
            if not books:
                return None

            book_title_words = set(_normalize(book.title).split())
            book_author_words = {
                w for w in _normalize(book.author).split() if w not in _METADATA_STOPWORDS
            }
            scored = [
                (b, _score_metadata_result(b, book_title_words, book_author_words))
                for b in books
            ]
            best_result, best_score = max(scored, key=lambda x: x[1])

            if best_score < _MIN_METADATA_SCORE:
                log.debug(
                    "Shelfmark: query %r low confidence (score=%.2f, matched title=%r)",
                    query, best_score, best_result.get("title", "?"),
                )
                return None

            log.debug(
                "Shelfmark: query %r → result title=%r author=%r score=%.2f",
                query, best_result.get("title", "?"), best_result.get("author", "?"), best_score,
            )
            return best_result
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.error("Shelfmark: metadata search failed for query %r: %s", query, exc)
            return None

    def _fetch_submission_mode(self) -> str:
        """Return 'download' when the Shelfmark request workflow is disabled, else 'request'.

        Calls GET /api/request-policy and checks the requests_enabled field.
        Defaults to 'request' on any error so existing behaviour is preserved.
        """
        try:
            resp = self._session.get(f"{self._base_url}/api/request-policy", timeout=10)
            if resp.ok:
                policy = resp.json()
                if not policy.get("requests_enabled", True):
                    log.info("Shelfmark: request workflow disabled — using download mode")
                    return "download"
                log.debug("Shelfmark: request workflow enabled — using request mode")
                return "request"
        except (requests.ConnectionError, requests.Timeout, ValueError) as exc:
            log.warning(
                "Shelfmark: could not fetch request-policy (%s) — defaulting to request mode",
                exc,
            )
        return "request"

    def _build_request_payload(self, book: Book, metadata: dict) -> dict:
        """Build the POST /api/requests payload for a book using metadata result."""
        book_data: dict = {
            "title": book.title,
            "author": book.author,
            "provider": metadata.get("provider", ""),
            "provider_id": str(metadata.get("provider_id", "")),
            "content_type": "ebook",
        }
        isbn = book.best_isbn()
        if isbn:
            book_data["isbn"] = isbn
        return {"book_data": book_data}

    def _build_download_payload(self, book: Book, metadata: dict) -> dict:
        """Build the POST /api/releases/download payload from a metadata result.

        Maps source/source_id (or falls back to provider/provider_id) and passes
        through optional release fields (year, format, size, preview, search_mode).
        """
        payload: dict = {
            "source": metadata.get("source") or metadata.get("provider", ""),
            "source_id": str(metadata.get("source_id") or metadata.get("provider_id", "")),
            "title": metadata.get("title") or book.title,
            "author": metadata.get("author") or book.author,
            "content_type": "ebook",
        }
        for field in ("year", "format", "size", "preview", "search_mode"):
            if field in metadata:
                payload[field] = metadata[field]
        return payload

    def _post_request(self, payload: dict, title: str = "") -> bool:
        """POST to /api/requests. Returns True on 2xx, raises ShelfmarkAuthError on 401."""
        try:
            resp = self._session.post(
                f"{self._base_url}/api/requests",
                json=payload,
                timeout=30,
            )
            if resp.status_code == 401:
                self._authenticated = False
                raise ShelfmarkAuthError("Session expired (HTTP 401)")
            if resp.ok:
                return True
            if resp.status_code == 409:
                # Shelfmark's pending-request queue is full — not a bug, retry next cycle
                log.warning(
                    "Shelfmark: queue limit reached for %r (will retry next sync)", title
                )
                return False
            log.error(
                "Shelfmark: POST /api/requests returned HTTP %d for %r: %s",
                resp.status_code,
                title,
                resp.text[:300],
            )
            return False
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.error("Shelfmark: network error posting request for %r: %s", title, exc)
            return False

    def _post_download(self, payload: dict, title: str = "") -> bool:
        """POST to /api/releases/download. Returns True on 2xx, raises ShelfmarkAuthError on 401."""
        try:
            resp = self._session.post(
                f"{self._base_url}/api/releases/download",
                json=payload,
                timeout=30,
            )
            if resp.status_code == 401:
                self._authenticated = False
                raise ShelfmarkAuthError("Session expired (HTTP 401)")
            if resp.ok:
                return True
            log.error(
                "Shelfmark: POST /api/releases/download returned HTTP %d for %r: %s",
                resp.status_code,
                title,
                resp.text[:300],
            )
            return False
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.error("Shelfmark: network error downloading %r: %s", title, exc)
            return False

    def _try_batch(
        self, payloads: list[tuple[Book, dict]]
    ) -> dict[str, bool] | None:
        """Attempt POST /api/requests/batch. Returns None if the endpoint is unavailable."""
        requests_list = [payload for _, payload in payloads]
        try:
            resp = self._session.post(
                f"{self._base_url}/api/requests/batch",
                json={"requests": requests_list},
                timeout=60,
            )
            if resp.status_code == 404:
                log.debug(
                    "Shelfmark: batch endpoint not found (HTTP 404), using individual requests"
                )
                return None
            if resp.status_code == 401:
                self._authenticated = False
                raise ShelfmarkAuthError("Session expired during batch (HTTP 401)")
            if resp.status_code == 409:
                # Queue limit hit on the batch — fall through to individual requests
                # so partial submissions can still succeed
                log.warning("Shelfmark: queue limit reached on batch, retrying individually")
                return None
            if resp.ok:
                log.info("Shelfmark: batch submitted %d books", len(payloads))
                return {b.normalized_key(): True for b, _ in payloads}
            log.warning(
                "Shelfmark: batch endpoint returned HTTP %d — falling back to individual requests",
                resp.status_code,
            )
            return None
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.warning("Shelfmark: batch request failed (%s), falling back to individual", exc)
            return None
