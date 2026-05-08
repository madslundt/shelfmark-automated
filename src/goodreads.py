"""Goodreads RSS feed parser — fetches 'Want to Read' shelf books."""

from __future__ import annotations

import logging
import time
import urllib.parse

import feedparser
import requests

from .models import Book

log = logging.getLogger(__name__)


def _with_retry(fn, max_attempts: int = 3, base_delay: float = 1.0):
    """Retry fn() with exponential backoff on network errors and transient HTTP errors.

    Retries on: ConnectionError, Timeout, HTTP 429, HTTP 5xx.
    Does NOT retry on other HTTP 4xx.
    """
    for attempt in range(max_attempts):
        try:
            return fn()
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            log.warning("Network error (attempt %d/%d), retrying in %.0fs: %s",
                        attempt + 1, max_attempts, delay, exc)
            time.sleep(delay)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in (429,) or status >= 500:
                if attempt == max_attempts - 1:
                    raise
                delay = base_delay * (2 ** attempt)
                log.warning("HTTP %d (attempt %d/%d), retrying in %.0fs",
                            status, attempt + 1, max_attempts, delay)
                time.sleep(delay)
            else:
                raise


def _ensure_to_read_shelf(url: str) -> str:
    """Append ?shelf=to-read to the URL if not already present."""
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if "shelf" not in params:
        new_query = urllib.parse.urlencode({**params, "shelf": "to-read"}, doseq=True)
        return parsed._replace(query=new_query).geturl()
    return url


def _force_shelf(url: str, shelf: str) -> str:
    """Set ?shelf=<shelf> in the URL, replacing any existing shelf value."""
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    params["shelf"] = [shelf]
    new_query = urllib.parse.urlencode(params, doseq=True)
    return parsed._replace(query=new_query).geturl()


def fetch_want_to_read(rss_url: str) -> list[Book]:
    """Fetch books from the Goodreads 'to-read' RSS shelf.

    Args:
        rss_url: Goodreads RSS URL (e.g. https://www.goodreads.com/review/list_rss/12345).
                 The ?shelf=to-read parameter is appended automatically if absent.

    Returns:
        List of Book objects. isbn_10 may be None for roughly half of entries.

    Raises:
        requests.ConnectionError / requests.Timeout: After all retries exhausted.
    """
    url = _ensure_to_read_shelf(rss_url)
    log.debug("Goodreads: fetching RSS from %s", url)

    def _do_fetch():
        resp = requests.get(url, timeout=30, headers={"User-Agent": "shelfmark-automated/1.0"})
        resp.raise_for_status()
        return resp.text

    raw_xml = _with_retry(_do_fetch)

    feed = feedparser.parse(raw_xml)

    if feed.bozo and not feed.entries:
        log.warning("Goodreads: RSS parse error — %s", feed.get("bozo_exception", "unknown"))
        return []

    books: list[Book] = []
    for entry in feed.entries:
        try:
            title = (getattr(entry, "title", "") or "").strip()
            if not title:
                continue

            # feedparser exposes the Goodreads custom <author_name> tag
            author = (getattr(entry, "author_name", "") or "").strip()
            if not author:
                # Fallback: feedparser also sometimes puts it in .author
                author = (getattr(entry, "author", "") or "").strip()

            # Goodreads RSS exposes 10-digit ISBNs in <isbn>
            raw_isbn = (getattr(entry, "isbn", "") or "").strip()
            isbn_10 = raw_isbn if raw_isbn else None

            source_id = str(getattr(entry, "book_id", "") or "").strip()

            raw_summary = getattr(entry, "summary", "")
            description = raw_summary.strip() or None if isinstance(raw_summary, str) else None

            books.append(Book(
                title=title,
                author=author,
                isbn_10=isbn_10,
                isbn_13=None,  # Goodreads RSS does not provide ISBN-13
                source="goodreads",
                source_id=source_id,
                description=description,
            ))
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed Goodreads entry: %s", exc)
            continue

    log.info("Goodreads: fetched %d 'to-read' books", len(books))
    return books


def fetch_currently_reading(rss_url: str) -> list[Book]:
    """Fetch currently-reading books from the Goodreads 'currently-reading' shelf RSS feed.

    Args:
        rss_url: Goodreads RSS URL. The shelf parameter is forced to 'currently-reading'.

    Returns:
        List of Book objects with source="goodreads".

    Raises:
        requests.ConnectionError / requests.Timeout: After all retries exhausted.
    """
    url = _force_shelf(rss_url, "currently-reading")
    log.debug("Goodreads: fetching currently-reading shelf RSS from %s", url)

    def _do_fetch():
        resp = requests.get(url, timeout=30, headers={"User-Agent": "shelfmark-automated/1.0"})
        resp.raise_for_status()
        return resp.text

    raw_xml = _with_retry(_do_fetch)
    feed = feedparser.parse(raw_xml)

    if feed.bozo and not feed.entries:
        log.warning("Goodreads: RSS parse error — %s", feed.get("bozo_exception", "unknown"))
        return []

    books: list[Book] = []
    for entry in feed.entries:
        try:
            title = (getattr(entry, "title", "") or "").strip()
            if not title:
                continue

            author = (getattr(entry, "author_name", "") or "").strip()
            if not author:
                author = (getattr(entry, "author", "") or "").strip()

            raw_isbn = (getattr(entry, "isbn", "") or "").strip()
            isbn_10 = raw_isbn if raw_isbn else None
            source_id = str(getattr(entry, "book_id", "") or "").strip()

            raw_summary = getattr(entry, "summary", "")
            description = raw_summary.strip() or None if isinstance(raw_summary, str) else None

            books.append(Book(
                title=title,
                author=author,
                isbn_10=isbn_10,
                isbn_13=None,
                source="goodreads",
                source_id=source_id,
                description=description,
            ))
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed Goodreads entry: %s", exc)
            continue

    log.info("Goodreads: fetched %d currently-reading books", len(books))
    return books


def fetch_read(rss_url: str) -> list[Book]:
    """Fetch fully-read books from the Goodreads 'read' shelf RSS feed.

    Only fully-completed reads are returned. Currently-reading / in-progress
    entries are not fetched (uses shelf=read, not shelf=currently-reading).

    Args:
        rss_url: Goodreads RSS URL. The shelf parameter is forced to 'read'.

    Returns:
        List of Book objects with source="goodreads".

    Raises:
        requests.ConnectionError / requests.Timeout: After all retries exhausted.
    """
    url = _force_shelf(rss_url, "read")
    log.debug("Goodreads: fetching read shelf RSS from %s", url)

    def _do_fetch():
        resp = requests.get(url, timeout=30, headers={"User-Agent": "shelfmark-automated/1.0"})
        resp.raise_for_status()
        return resp.text

    raw_xml = _with_retry(_do_fetch)
    feed = feedparser.parse(raw_xml)

    if feed.bozo and not feed.entries:
        log.warning("Goodreads: RSS parse error — %s", feed.get("bozo_exception", "unknown"))
        return []

    books: list[Book] = []
    for entry in feed.entries:
        try:
            title = (getattr(entry, "title", "") or "").strip()
            if not title:
                continue

            author = (getattr(entry, "author_name", "") or "").strip()
            if not author:
                author = (getattr(entry, "author", "") or "").strip()

            raw_isbn = (getattr(entry, "isbn", "") or "").strip()
            isbn_10 = raw_isbn if raw_isbn else None
            source_id = str(getattr(entry, "book_id", "") or "").strip()

            raw_summary = getattr(entry, "summary", "")
            description = raw_summary.strip() or None if isinstance(raw_summary, str) else None

            books.append(Book(
                title=title,
                author=author,
                isbn_10=isbn_10,
                isbn_13=None,
                source="goodreads",
                source_id=source_id,
                description=description,
            ))
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed Goodreads entry: %s", exc)
            continue

    log.info("Goodreads: fetched %d read books", len(books))
    return books
