"""shelfmark-automated — book wishlist sync bridge.

Fetches 'Want to Read' books from Hardcover (GraphQL) and Goodreads (RSS),
checks which are already in the CWA library via OPDS, and submits the rest
to Shelfmark for download. Runs in a loop with a randomized sleep interval
to reduce API ban risk.
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime

from src import cwa, goodreads, hardcover
from src.models import Book
from src.shelfmark import ShelfmarkClient
from src.state import REASON_IN_LIBRARY, REASON_SUBMITTED, StateManager

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
    sync_interval_min_seconds: int
    sync_interval_max_seconds: int
    state_file: str | None
    full_sync_interval_seconds: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        """Read configuration from environment variables. All fields are optional."""
        def optional(name: str) -> str | None:
            value = os.environ.get(name, "").strip()
            return value or None

        # Sync interval: random between min and max to reduce API ban risk.
        # SYNC_INTERVAL_SECONDS=0  → run-once mode (testing/CI), preserved for backward compat.
        # SYNC_INTERVAL_SECONDS=N  → legacy fixed interval, sets min=max=N.
        # SYNC_INTERVAL_MIN/MAX    → new explicit range (default 2–15 minutes).
        legacy = os.environ.get("SYNC_INTERVAL_SECONDS", "").strip()
        if legacy == "0":
            min_seconds = 0
            max_seconds = 0
        else:
            try:
                min_seconds = int(os.environ.get("SYNC_INTERVAL_MIN_SECONDS", "120") or "120")
            except ValueError:
                min_seconds = 120
            try:
                max_seconds = int(os.environ.get("SYNC_INTERVAL_MAX_SECONDS", "900") or "900")
            except ValueError:
                max_seconds = 900
            if legacy:
                try:
                    fixed = int(legacy)
                    if fixed > 0:
                        min_seconds = max_seconds = fixed
                except ValueError:
                    pass

        try:
            full_sync_interval = int(
                os.environ.get("FULL_SYNC_INTERVAL_SECONDS", "86400") or "86400"
            )
        except ValueError:
            full_sync_interval = 86400

        return cls(
            hardcover_api_key=optional("HARDCOVER_API_KEY"),
            goodreads_rss_url=optional("GOODREADS_RSS_URL"),
            cwa_url=optional("CWA_URL"),
            cwa_username=optional("CWA_USERNAME"),
            cwa_password=optional("CWA_PASSWORD"),
            shelfmark_url=optional("SHELFMARK_URL"),
            shelfmark_username=optional("SHELFMARK_USERNAME"),
            shelfmark_password=optional("SHELFMARK_PASSWORD"),
            sync_interval_min_seconds=min_seconds,
            sync_interval_max_seconds=max_seconds,
            state_file=optional("STATE_FILE"),
            full_sync_interval_seconds=full_sync_interval,
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
# Full-sync scheduling
# ---------------------------------------------------------------------------

def _is_full_sync_due(state: StateManager | None, interval_seconds: int) -> bool:
    """Return True if a full verification sync should run this pass."""
    if state is None or interval_seconds <= 0:
        return False
    last = state.get_last_full_sync()
    if last is None:
        return False  # first ever run naturally processes all books (state is empty)
    return (datetime.now() - last).total_seconds() >= interval_seconds


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def sync_once(
    config: Config,
    shelfmark_client: ShelfmarkClient | None,
    state: StateManager | None = None,
    force_full: bool = False,
) -> None:
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

    # --- Incremental filtering ---
    if state is not None and not force_full:
        new_books = [b for b in all_books if not state.is_handled(b)]
        skipped = len(all_books) - len(new_books)
        if skipped:
            log.info("Incremental: skipping %d already-handled book(s)", skipped)
        all_books = new_books
    elif force_full and state is not None:
        log.info("Full verification sync: checking all %d books", len(all_books))

    if not all_books:
        log.info("No books to process — sync pass complete")
        if state is not None:
            state.save()
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
                if state is not None:
                    state.mark_handled(book, REASON_IN_LIBRARY)
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
        if state is not None:
            state.save()
        return

    if not shelfmark_client:
        log.info("Shelfmark: SHELFMARK_URL not set — skipping submission (%d books)", len(missing))
        if state is not None:
            state.save()
        return

    results = shelfmark_client.request_books_batch(missing)
    ok_count = sum(1 for v in results.values() if v)
    fail_count = sum(1 for v in results.values() if not v)
    skip_count = len(missing) - len(results)

    if fail_count:
        failed_books = [b for b in missing if results.get(b.normalized_key()) is False]
        for book in failed_books:
            log.error("FAILED to request: %r", book)

    if state is not None:
        for book in missing:
            if results.get(book.normalized_key()) is True:
                state.mark_handled(book, REASON_SUBMITTED)

    log.info(
        "Shelfmark: %d submitted, %d failed, %d skipped (no metadata found)",
        ok_count,
        fail_count,
        skip_count,
    )
    if ok_count == 0 and fail_count > 0:
        log.error(
            "Shelfmark: all requests failed — check the log above for a CRITICAL message "
            "with the specific error, or verify SHELFMARK_URL is reachable and credentials are correct"
        )
    if ok_count == 0 and skip_count > 0 and fail_count == 0:
        log.warning(
            "Shelfmark: no metadata found for any book — "
            "check Shelfmark's metadata provider configuration"
        )

    if state is not None:
        state.save()


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
        "(interval=%d-%ds, full_sync=%ds, cwa=%s, shelfmark=%s, state=%s)",
        config.sync_interval_min_seconds,
        config.sync_interval_max_seconds,
        config.full_sync_interval_seconds,
        config.cwa_url or "not configured",
        config.shelfmark_url or "not configured",
        config.state_file or "disabled",
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

    state: StateManager | None = None
    if config.state_file is not None:
        state = StateManager(config.state_file)

    try:
        while True:
            force_full = _is_full_sync_due(state, config.full_sync_interval_seconds)
            try:
                sync_once(config, shelfmark_client, state, force_full=force_full)
                if force_full and state is not None:
                    state.set_last_full_sync()
            except Exception as exc:  # noqa: BLE001
                log.error("Sync pass failed with unexpected error: %s", exc, exc_info=True)

            if config.sync_interval_max_seconds <= 0:
                log.info("SYNC_INTERVAL=0 — exiting after one pass")
                break

            sleep_seconds = random.randint(
                config.sync_interval_min_seconds,
                config.sync_interval_max_seconds,
            )
            log.info("Sleeping %ds until next sync", sleep_seconds)
            time.sleep(sleep_seconds)
    finally:
        if state is not None:
            state.close()


if __name__ == "__main__":
    main()
