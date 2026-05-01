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

from .models import Book

log = logging.getLogger(__name__)

# Re-authenticate 12 hours before the 7-day session expires
_SESSION_TTL = timedelta(days=6, hours=12)


class ShelfmarkAuthError(Exception):
    """Raised when Shelfmark authentication cannot be established."""


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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def request_book(self, book: Book) -> bool:
        """Submit a single book download request to Shelfmark.

        Searches Shelfmark's metadata provider first to obtain the
        provider + provider_id required by POST /api/requests.

        Returns True on success (2xx), False on any failure.
        Logs errors and returns False rather than raising, so callers can
        process remaining books uninterrupted.
        """
        try:
            self._ensure_ready()
        except ShelfmarkAuthError as exc:
            log.critical("Shelfmark auth failed: %s", exc)
            return False

        metadata = self._search_metadata(book.title, book.author)
        if not metadata:
            log.warning(
                "Shelfmark: no metadata result for %r — skipping request", book.title
            )
            return False

        payload = self._build_payload(book, metadata)
        try:
            return self._post_request(payload, book.title)
        except ShelfmarkAuthError:
            # Session expired mid-batch — re-authenticate once and retry
            log.info("Shelfmark: session expired mid-request, re-authenticating")
            self._authenticated = False
            try:
                self._login()
                return self._post_request(payload, book.title)
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

        # Fetch metadata for all books first
        payloads: list[tuple[Book, dict]] = []
        results: dict[str, bool] = {}
        for book in books:
            metadata = self._search_metadata(book.title, book.author)
            if metadata:
                payloads.append((book, self._build_payload(book, metadata)))
            else:
                log.warning("Shelfmark: no metadata for %r — skipping", book.title)
                results[book.normalized_key()] = False

        if not payloads:
            return results

        # Try batch endpoint
        batch_results = self._try_batch(payloads)
        if batch_results is not None:
            results.update(batch_results)
            return results

        # Fallback: individual requests
        log.debug("Shelfmark: falling back to individual requests for %d books", len(payloads))
        for book, payload in payloads:
            try:
                ok = self._post_request(payload, book.title)
            except ShelfmarkAuthError:
                self._authenticated = False
                try:
                    self._login()
                    ok = self._post_request(payload, book.title)
                except Exception:
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

    def _search_metadata(self, title: str, author: str) -> dict | None:
        """Search Shelfmark's configured metadata provider for a book.

        Returns the first result dict (containing 'provider' and 'provider_id')
        or None if no results or on error.
        """
        query = f"{title} {author}".strip()
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
                log.warning(
                    "Shelfmark metadata search returned HTTP %d for %r",
                    resp.status_code, title,
                )
                return None
            data = resp.json()
            books = data.get("books", []) if isinstance(data, dict) else data
            if not books:
                log.debug("Shelfmark: metadata search returned no results for %r", title)
                return None
            return books[0]
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.error("Shelfmark: metadata search failed for %r: %s", title, exc)
            return None

    def _build_payload(self, book: Book, metadata: dict) -> dict:
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
