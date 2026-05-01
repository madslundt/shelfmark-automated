"""shelfmark-automated — book wishlist sync bridge.

Fetches 'Want to Read' books from Hardcover (GraphQL) and Goodreads (RSS),
checks which are already in the CWA library via OPDS, and submits the rest
to Shelfmark for download. Runs in a configurable sleep loop.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass

from src import cwa, goodreads, hardcover
from src.models import Book
from src.shelfmark import ShelfmarkClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    hardcover_api_key: str | None
    goodreads_rss_url: str | None
    cwa_url: str | None
    cwa_username: str | None
    cwa_password: str | None
    shelfmark_url: str | None
    shelfmark_username: str | None
    shelfmark_password: str | None
    sync_interval_seconds: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        """Read configuration from environment variables. All fields are optional."""
        def optional(name: str) -> str | None:
            value = os.environ.get(name, "").strip()
            return value or None

        try:
            interval = int(os.environ.get("SYNC_INTERVAL_SECONDS", "3600"))
        except ValueError:
            interval = 3600

        return cls(
            hardcover_api_key=optional("HARDCOVER_API_KEY"),
            goodreads_rss_url=optional("GOODREADS_RSS_URL"),
            cwa_url=optional("CWA_URL"),
            cwa_username=optional("CWA_USERNAME"),
            cwa_password=optional("CWA_PASSWORD"),
            shelfmark_url=optional("SHELFMARK_URL"),
            shelfmark_username=optional("SHELFMARK_USERNAME"),
            shelfmark_password=optional("SHELFMARK_PASSWORD"),
            sync_interval_seconds=interval,
            log_level=optional("LOG_LEVEL") or "INFO",
        )


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(books: list[Book]) -> list[Book]:
    """Remove duplicate books across sources.

    ISBN-based matching takes priority: two books with the same ISBN are
    considered identical regardless of title/author variations. For books
    without an ISBN, the normalized 'title author' key is used.
    """
    seen_isbns: set[str] = set()
    seen_keys: set[str] = set()
    result: list[Book] = []

    for book in books:
        isbn = book.best_isbn()
        key = book.normalized_key()

        if isbn and isbn in seen_isbns:
            log.debug("Dedup (ISBN): skipping duplicate %r (isbn=%s)", book.title, isbn)
            continue
        if key in seen_keys:
            log.debug("Dedup (key): skipping duplicate %r", book.title)
            continue

        if isbn:
            seen_isbns.add(isbn)
        seen_keys.add(key)
        result.append(book)

    return result


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def sync_once(config: Config, shelfmark_client: ShelfmarkClient | None) -> None:
    """Run one full sync pass: fetch → deduplicate → library check → request."""
    log.info("Starting sync pass")

    # --- Fetch from sources (independently; one failure doesn't abort the other) ---
    books_hardcover: list[Book] = []
    books_goodreads: list[Book] = []

    if config.hardcover_api_key:
        try:
            books_hardcover = hardcover.fetch_want_to_read(config.hardcover_api_key)
        except Exception as exc:  # noqa: BLE001
            log.error("Hardcover fetch failed — skipping source: %s", exc)
    else:
        log.debug("Hardcover: HARDCOVER_API_KEY not set — skipping")

    if config.goodreads_rss_url:
        try:
            books_goodreads = goodreads.fetch_want_to_read(config.goodreads_rss_url)
        except Exception as exc:  # noqa: BLE001
            log.error("Goodreads fetch failed — skipping source: %s", exc)
    else:
        log.debug("Goodreads: GOODREADS_RSS_URL not set — skipping")

    all_books = deduplicate(books_hardcover + books_goodreads)
    log.info(
        "Total unique books: %d  (Hardcover: %d, Goodreads: %d)",
        len(all_books),
        len(books_hardcover),
        len(books_goodreads),
    )

    if not all_books:
        log.info("No books to process — sync pass complete")
        return

    # --- Check library ---
    if config.cwa_url:
        missing: list[Book] = []
        for book in all_books:
            in_library = cwa.is_book_in_library(
                book,
                config.cwa_url,
                config.cwa_username,
                config.cwa_password,
            )
            if in_library:
                log.info("SKIP (already in library): %r", book)
            else:
                log.info("QUEUE (not in library):    %r", book)
                missing.append(book)
        log.info(
            "Library check: %d already owned, %d to request",
            len(all_books) - len(missing),
            len(missing),
        )
    else:
        log.info("CWA: not configured — queuing all %d books", len(all_books))
        missing = list(all_books)

    # --- Submit to Shelfmark ---
    if not missing:
        log.info("Nothing to request — sync pass complete")
        return

    if not shelfmark_client:
        log.info("Shelfmark: SHELFMARK_URL not set — skipping submission (%d books)", len(missing))
        return

    results = shelfmark_client.request_books_batch(missing)
    ok_count = sum(1 for v in results.values() if v)
    fail_count = sum(1 for v in results.values() if not v)
    skip_count = len(missing) - len(results)

    if fail_count:
        failed_books = [b for b in missing if results.get(b.normalized_key()) is False]
        for book in failed_books:
            log.error("FAILED to request: %r", book)

    log.info(
        "Shelfmark: %d submitted, %d failed, %d skipped (no metadata found)",
        ok_count,
        fail_count,
        skip_count,
    )
    if ok_count == 0 and fail_count > 0:
        log.error(
            "Shelfmark: all requests failed — check connectivity and credentials "
            "(run with LOG_LEVEL=DEBUG for details)"
        )
    if ok_count == 0 and skip_count > 0 and fail_count == 0:
        log.warning(
            "Shelfmark: no metadata found for any book — check Shelfmark's metadata provider configuration"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


def main() -> None:
    config = Config.from_env()
    setup_logging(config.log_level)

    log.info(
        "shelfmark-automated starting up  "
        "(interval=%ds, cwa=%s, shelfmark=%s)",
        config.sync_interval_seconds,
        config.cwa_url or "not configured",
        config.shelfmark_url or "not configured",
    )

    if not config.hardcover_api_key and not config.goodreads_rss_url:
        log.warning(
            "No book sources configured — set HARDCOVER_API_KEY and/or GOODREADS_RSS_URL"
        )

    shelfmark_client = (
        ShelfmarkClient(config.shelfmark_url, config.shelfmark_username, config.shelfmark_password)
        if config.shelfmark_url
        else None
    )

    while True:
        try:
            sync_once(config, shelfmark_client)
        except Exception as exc:  # noqa: BLE001
            log.error("Sync pass failed with unexpected error: %s", exc, exc_info=True)

        if config.sync_interval_seconds <= 0:
            log.info("SYNC_INTERVAL_SECONDS=0 — exiting after one pass")
            break

        log.info("Sleeping %ds until next sync", config.sync_interval_seconds)
        time.sleep(config.sync_interval_seconds)


if __name__ == "__main__":
    main()
