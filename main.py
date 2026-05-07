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
from src.cwa import CWAAuthError, CWAClient
from src.models import Book
from src.shelfmark import ShelfmarkClient
from src.state import REASON_IMPORTED, REASON_SUBMITTED, StateManager

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
    read_status_sync_interval_seconds: int
    fix_metadata: bool
    sync_on_start: bool
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

        try:
            read_status_sync_interval = int(
                os.environ.get("READ_STATUS_SYNC_INTERVAL_SECONDS", "86400") or "86400"
            )
        except ValueError:
            read_status_sync_interval = 86400

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
            read_status_sync_interval_seconds=read_status_sync_interval,
            fix_metadata=os.environ.get("FIX_METADATA", "true").strip().lower() not in {
                "false", "0", "no"
            },
            sync_on_start=os.environ.get("SYNC_ON_START", "false").strip().lower() not in {
                "false", "0", "no", ""
            },
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


def _is_read_status_sync_due(last: datetime | None, interval_seconds: int) -> bool:
    """Return True if the read status sync should run now.

    Args:
        last: Timestamp of the last read status sync run, or None if never run.
        interval_seconds: How often to run (seconds). 0 means disabled.
    """
    if interval_seconds <= 0:
        return False
    if last is None:
        return True  # Never run before — run immediately
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
            log.debug("Incremental: skipping %d already-handled book(s)", skipped)
        all_books = new_books
    elif force_full and state is not None:
        new_books = [b for b in all_books if not state.is_imported(b)]
        skipped = len(all_books) - len(new_books)
        if skipped:
            log.debug(
                "Full verification sync: skipping %d imported book(s), checking %d",
                skipped, len(new_books),
            )
        else:
            log.debug("Full verification sync: checking all %d books", len(all_books))
        all_books = new_books

    if not all_books:
        log.debug("No books to process — sync pass complete")
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
            if in_library is True:
                log.debug("SKIP (already imported):   %r", book)
                if state is not None:
                    state.mark_handled(book, REASON_IMPORTED)
            elif in_library is None:
                log.info("SKIP (CWA unreachable):    %r — will retry next cycle", book)
            else:
                log.info("QUEUE (not in library):    %r", book)
                missing.append(book)
        log.debug(
            "Library check: %d already owned, %d to request",
            len(all_books) - len(missing),
            len(missing),
        )
    else:
        log.debug("CWA: not configured — queuing all %d books", len(all_books))
        missing = list(all_books)

    # --- Submit to Shelfmark ---
    if not missing:
        log.debug("Nothing to request — sync pass complete")
        if state is not None:
            state.save()
        return

    if not shelfmark_client:
        log.debug("Shelfmark: SHELFMARK_URL not set — skipping submission (%d books)", len(missing))
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
            "with the specific error, or verify SHELFMARK_URL is reachable and "
            "credentials are correct"
        )
    if ok_count == 0 and skip_count > 0 and fail_count == 0:
        log.debug(
            "Shelfmark: no metadata found for any book — "
            "check Shelfmark's metadata provider configuration"
        )

    if state is not None:
        state.save()


def sync_read_status_once(
    config: Config,
    cwa_client: "CWAClient | None",
    state: StateManager | None = None,
) -> None:
    """Fetch fully-read books and mark them as read in CWA.

    Only processes books with a confirmed completion status:
    Hardcover status_id=3 (Read) and Goodreads shelf=read.
    Currently-reading / in-progress entries are never touched.
    """
    if not config.cwa_url or not cwa_client:
        log.debug("Read status sync: CWA not configured — skipping")
        return

    books_hardcover: list[Book] = []
    books_goodreads: list[Book] = []

    if config.hardcover_api_key:
        try:
            books_hardcover = hardcover.fetch_read(config.hardcover_api_key)
        except Exception as exc:  # noqa: BLE001
            log.error("Hardcover read fetch failed — skipping source: %s", exc)
    else:
        log.debug("Read status sync: Hardcover not configured — skipping")

    if config.goodreads_rss_url:
        try:
            books_goodreads = goodreads.fetch_read(config.goodreads_rss_url)
        except Exception as exc:  # noqa: BLE001
            log.error("Goodreads read fetch failed — skipping source: %s", exc)
    else:
        log.debug("Read status sync: Goodreads not configured — skipping")

    all_books = deduplicate(books_hardcover + books_goodreads)
    log.debug(
        "Read status sync: %d unique read books (Hardcover: %d, Goodreads: %d)",
        len(all_books), len(books_hardcover), len(books_goodreads),
    )

    if state is not None:
        all_books = [b for b in all_books if not state.is_read_status_set(b)]
        log.debug("Read status sync: %d books not yet synced", len(all_books))

    if not all_books:
        log.debug("Read status sync: nothing to process")
        return

    ok_count = 0
    skip_count = 0
    for book in all_books:
        book_id = cwa.find_book_in_library(
            book, config.cwa_url, config.cwa_username, config.cwa_password
        )
        if book_id is None:
            log.debug("Read status sync: %r not found in CWA — skipping", book.title)
            skip_count += 1
            continue
        try:
            ok = cwa_client.mark_as_read(book_id)
        except CWAAuthError as exc:
            log.error("CWA auth failed during read status sync: %s", exc)
            break
        if ok:
            ok_count += 1
            if state is not None:
                state.mark_read_status_set(book)
        else:
            log.warning("Read status sync: failed to mark %r (book_id=%d)", book.title, book_id)

    log.info(
        "Read status sync: %d marked as read, %d not found in library",
        ok_count, skip_count,
    )

    if state is not None:
        state.save()


def sync_metadata_once(
    config: Config,
    cwa_client: "CWAClient | None",
) -> None:
    """Fetch all books from Hardcover/Goodreads and fix wrong author metadata in CWA.

    Checks both read and want-to-read shelves. Skips silently when disabled
    (FIX_METADATA=false) or when CWA is not configured.
    """
    if not config.fix_metadata:
        log.debug("Metadata fix: disabled via FIX_METADATA=false — skipping")
        return

    if not config.cwa_url or not cwa_client:
        log.debug("Metadata fix: CWA not configured — skipping")
        return

    all_books: list[Book] = []

    if config.hardcover_api_key:
        try:
            all_books += hardcover.fetch_want_to_read(config.hardcover_api_key)
            all_books += hardcover.fetch_read(config.hardcover_api_key)
        except Exception as exc:  # noqa: BLE001
            log.error("Metadata fix: Hardcover fetch failed — %s", exc)
    else:
        log.debug("Metadata fix: Hardcover not configured — skipping")

    if config.goodreads_rss_url:
        try:
            all_books += goodreads.fetch_want_to_read(config.goodreads_rss_url)
            all_books += goodreads.fetch_read(config.goodreads_rss_url)
        except Exception as exc:  # noqa: BLE001
            log.error("Metadata fix: Goodreads fetch failed — %s", exc)
    else:
        log.debug("Metadata fix: Goodreads not configured — skipping")

    all_books = deduplicate(all_books)
    log.debug("Metadata fix: checking %d unique books", len(all_books))

    # Build set of known correct authors from our book lists.
    # If the "wrong" author in CWA is actually a correct author for a DIFFERENT book
    # in our list, it likely means CWA has the book stored under a wrong title (bad
    # ebook file metadata). Updating the author in that case makes things worse.
    known_correct_authors = {b.author.lower() for b in all_books}

    ok_count = 0
    retry_queue: list[tuple[Book, int, str]] = []
    for book in all_books:
        result = cwa.find_mismatched_author(
            book, config.cwa_url, config.cwa_username, config.cwa_password
        )
        if result is None:
            continue
        book_id, wrong_author = result
        if wrong_author in known_correct_authors:
            log.debug(
                "Metadata fix: skipping book %d %r — CWA author %r is a known correct "
                "author (likely a CWA title mismatch, not a wrong author)",
                book_id, book.title, wrong_author,
            )
            continue
        try:
            ok = cwa_client.update_book_author(book_id, book.author)
        except CWAAuthError as exc:
            log.error("Metadata fix: CWA auth failed — stopping: %s", exc)
            break
        if ok:
            ok_count += 1
            log.info(
                "Metadata fix: book %d %r — author corrected from %r to %r",
                book_id, book.title, wrong_author, book.author,
            )
        else:
            log.warning("Metadata fix: failed book %d %r — will retry", book_id, book.title)
            retry_queue.append((book, book_id, wrong_author))

    for attempt in range(1, 4):
        if not retry_queue:
            break
        log.info(
            "Metadata fix: retrying %d failed book(s) (attempt %d/3)",
            len(retry_queue), attempt,
        )
        time.sleep(5)
        still_failing: list[tuple[Book, int, str]] = []
        for book, book_id, wrong_author in retry_queue:
            try:
                ok = cwa_client.update_book_author(book_id, book.author)
            except CWAAuthError as exc:
                log.error("Metadata fix: CWA auth failed during retry — stopping: %s", exc)
                retry_queue = still_failing
                break
            if ok:
                ok_count += 1
                log.info(
                    "Metadata fix: book %d %r — author corrected on retry %d from %r to %r",
                    book_id, book.title, attempt, wrong_author, book.author,
                )
            else:
                still_failing.append((book, book_id, wrong_author))
        else:
            retry_queue = still_failing

    log.info("Metadata fix: %d corrected, %d failed after retries", ok_count, len(retry_queue))


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
        "(interval=%d-%ds, full_sync=%ds, read_status_sync=%s, fix_metadata=%s, "
        "sync_on_start=%s, cwa=%s, shelfmark=%s, state=%s)",
        config.sync_interval_min_seconds,
        config.sync_interval_max_seconds,
        config.full_sync_interval_seconds,
        f"{config.read_status_sync_interval_seconds}s"
        if config.read_status_sync_interval_seconds > 0
        else "disabled",
        "enabled" if config.fix_metadata else "disabled",
        "yes" if config.sync_on_start else "no",
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

    cwa_read_client: CWAClient | None = None
    if config.cwa_url and config.cwa_username and config.cwa_password:
        cwa_read_client = CWAClient(config.cwa_url, config.cwa_username, config.cwa_password)

    state: StateManager | None = None
    if config.state_file is not None:
        state = StateManager(config.state_file)

    if config.sync_on_start:
        log.info("SYNC_ON_START: running read status sync and metadata fix before main loop")
        try:
            sync_read_status_once(config, cwa_read_client, state)
            if state is not None:
                state.set_last_read_status_sync()
        except Exception as exc:  # noqa: BLE001
            log.error("Read status sync on start failed: %s", exc, exc_info=True)
        try:
            sync_metadata_once(config, cwa_read_client)
        except Exception as exc:  # noqa: BLE001
            log.error("Metadata fix on start failed: %s", exc, exc_info=True)

    try:
        while True:
            force_full = _is_full_sync_due(state, config.full_sync_interval_seconds)
            try:
                sync_once(config, shelfmark_client, state, force_full=force_full)
                if force_full and state is not None:
                    state.set_last_full_sync()
            except Exception as exc:  # noqa: BLE001
                log.error("Sync pass failed with unexpected error: %s", exc, exc_info=True)

            rs_last: datetime | None = state.get_last_read_status_sync() if state else None
            if _is_read_status_sync_due(rs_last, config.read_status_sync_interval_seconds):
                try:
                    sync_read_status_once(config, cwa_read_client, state)
                    if state is not None:
                        state.set_last_read_status_sync()
                    else:
                        rs_last = datetime.now()
                except Exception as exc:  # noqa: BLE001
                    log.error("Read status sync pass failed: %s", exc, exc_info=True)

                try:
                    sync_metadata_once(config, cwa_read_client)
                except Exception as exc:  # noqa: BLE001
                    log.error("Metadata fix pass failed: %s", exc, exc_info=True)
            else:
                log.debug("Read status sync: not due yet — skipping this cycle")

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
