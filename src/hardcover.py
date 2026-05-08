"""Hardcover GraphQL API client — fetches 'Want to Read' books."""

from __future__ import annotations

import logging
import time

import requests

from .models import Book

log = logging.getLogger(__name__)

HARDCOVER_GRAPHQL_URL = "https://api.hardcover.app/v1/graphql"

_WANT_TO_READ_QUERY = """
{
  me {
    user_books(where: {status_id: {_eq: 1}}) {
      book {
        id
        title
        description
        release_date
        contributions {
          author {
            name
          }
        }
        default_physical_edition {
          isbn_10
          isbn_13
          publisher {
            name
          }
        }
        book_series {
          series {
            name
          }
          position
        }
      }
    }
  }
}
"""


_READ_QUERY = """
{
  me {
    user_books(where: {status_id: {_eq: 3}}) {
      book {
        id
        title
        description
        release_date
        contributions {
          author {
            name
          }
        }
        default_physical_edition {
          isbn_10
          isbn_13
          publisher {
            name
          }
        }
        book_series {
          series {
            name
          }
          position
        }
      }
    }
  }
}
"""


def _with_retry(fn, max_attempts: int = 3, base_delay: float = 1.0):
    """Call fn() up to max_attempts times with exponential backoff.

    Retries on: ConnectionError, Timeout, HTTP 429, HTTP 5xx.
    Does NOT retry on other HTTP 4xx (including 401).
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


def fetch_want_to_read(api_key: str) -> list[Book]:
    """Fetch books with status 'Want to Read' (status_id=1) from Hardcover.

    Args:
        api_key: Hardcover Bearer token.

    Returns:
        List of Book objects. May be partial if some entries have incomplete data.

    Raises:
        requests.HTTPError: On HTTP 401 (invalid API key) or unrecoverable errors.
        requests.ConnectionError / requests.Timeout: After all retries exhausted.
    """
    session = requests.Session()
    session.headers.update({
        "Authorization": api_key if api_key.startswith("Bearer ") else f"Bearer {api_key}",
        "Content-Type": "application/json",
    })

    def _do_request():
        resp = session.post(
            HARDCOVER_GRAPHQL_URL,
            json={"query": _WANT_TO_READ_QUERY},
            timeout=30,
        )
        if resp.status_code == 401:
            raise requests.HTTPError(
                "Hardcover API key is invalid or expired (HTTP 401)",
                response=resp,
            )
        resp.raise_for_status()
        return resp

    resp = _with_retry(_do_request)
    data = resp.json()

    if "errors" in data:
        for err in data["errors"]:
            log.warning("Hardcover GraphQL error: %s", err.get("message", err))

    books: list[Book] = []
    try:
        me_list = data["data"]["me"]
        if not me_list:
            log.warning("Hardcover: 'me' returned empty list — check API key / user")
            return []
        user_books = me_list[0]["user_books"]
    except (KeyError, TypeError, IndexError) as exc:
        log.error("Unexpected Hardcover response structure: %s", exc)
        return []

    for ub in user_books:
        try:
            book_data = ub["book"]
            title = (book_data.get("title") or "").strip()
            if not title:
                continue

            # First contribution author, fall back to empty string
            contributions = book_data.get("contributions") or []
            author = ""
            if contributions:
                author = (contributions[0].get("author") or {}).get("name", "") or ""
            author = author.strip()

            edition = book_data.get("default_physical_edition") or {}
            isbn_10 = (edition.get("isbn_10") or "").strip() or None
            isbn_13 = (edition.get("isbn_13") or "").strip() or None
            publisher_obj = edition.get("publisher") or {}
            publisher = (publisher_obj.get("name") or "").strip() or None

            raw_desc = book_data.get("description") or ""
            description = raw_desc.strip() or None

            pubdate = (book_data.get("release_date") or "").strip() or None

            series: str | None = None
            series_index: str | None = None
            book_series_list = book_data.get("book_series") or []
            if book_series_list:
                first = book_series_list[0]
                series_name = ((first.get("series") or {}).get("name") or "").strip()
                series = series_name or None
                pos = first.get("position")
                if pos is not None:
                    try:
                        float_pos = float(pos)
                        series_index = (
                            str(int(float_pos)) if float_pos == int(float_pos) else str(float_pos)
                        )
                    except (ValueError, TypeError):
                        series_index = str(pos)

            books.append(Book(
                title=title,
                author=author,
                isbn_10=isbn_10,
                isbn_13=isbn_13,
                source="hardcover",
                source_id=str(book_data.get("id", "")),
                description=description,
                series=series,
                series_index=series_index,
                publisher=publisher,
                pubdate=pubdate,
            ))
        except (KeyError, TypeError) as exc:
            log.warning("Skipping malformed Hardcover entry: %s", exc)
            continue

    log.info("Hardcover: fetched %d 'Want to Read' books", len(books))
    return books


def fetch_read(api_key: str) -> list[Book]:
    """Fetch fully-read books (status_id=3) from Hardcover.

    Only fetches books with completed read status. Currently-reading (status_id=2)
    and want-to-read (status_id=1) books are not included.

    Args:
        api_key: Hardcover Bearer token.

    Returns:
        List of Book objects with source="hardcover".

    Raises:
        requests.HTTPError: On HTTP 401 (invalid API key) or unrecoverable errors.
        requests.ConnectionError / requests.Timeout: After all retries exhausted.
    """
    session = requests.Session()
    session.headers.update({
        "Authorization": api_key if api_key.startswith("Bearer ") else f"Bearer {api_key}",
        "Content-Type": "application/json",
    })

    def _do_request():
        resp = session.post(
            HARDCOVER_GRAPHQL_URL,
            json={"query": _READ_QUERY},
            timeout=30,
        )
        if resp.status_code == 401:
            raise requests.HTTPError(
                "Hardcover API key is invalid or expired (HTTP 401)",
                response=resp,
            )
        resp.raise_for_status()
        return resp

    resp = _with_retry(_do_request)
    data = resp.json()

    if "errors" in data:
        for err in data["errors"]:
            log.warning("Hardcover GraphQL error: %s", err.get("message", err))

    books: list[Book] = []
    try:
        me_list = data["data"]["me"]
        if not me_list:
            log.warning("Hardcover: 'me' returned empty list — check API key / user")
            return []
        user_books = me_list[0]["user_books"]
    except (KeyError, TypeError, IndexError) as exc:
        log.error("Unexpected Hardcover response structure: %s", exc)
        return []

    for ub in user_books:
        try:
            book_data = ub["book"]
            title = (book_data.get("title") or "").strip()
            if not title:
                continue

            contributions = book_data.get("contributions") or []
            author = ""
            if contributions:
                author = (contributions[0].get("author") or {}).get("name", "") or ""
            author = author.strip()

            edition = book_data.get("default_physical_edition") or {}
            isbn_10 = (edition.get("isbn_10") or "").strip() or None
            isbn_13 = (edition.get("isbn_13") or "").strip() or None
            publisher_obj = edition.get("publisher") or {}
            publisher = (publisher_obj.get("name") or "").strip() or None

            raw_desc = book_data.get("description") or ""
            description = raw_desc.strip() or None

            pubdate = (book_data.get("release_date") or "").strip() or None

            series: str | None = None
            series_index: str | None = None
            book_series_list = book_data.get("book_series") or []
            if book_series_list:
                first = book_series_list[0]
                series_name = ((first.get("series") or {}).get("name") or "").strip()
                series = series_name or None
                pos = first.get("position")
                if pos is not None:
                    try:
                        float_pos = float(pos)
                        series_index = (
                            str(int(float_pos)) if float_pos == int(float_pos) else str(float_pos)
                        )
                    except (ValueError, TypeError):
                        series_index = str(pos)

            books.append(Book(
                title=title,
                author=author,
                isbn_10=isbn_10,
                isbn_13=isbn_13,
                source="hardcover",
                source_id=str(book_data.get("id", "")),
                description=description,
                series=series,
                series_index=series_index,
                publisher=publisher,
                pubdate=pubdate,
            ))
        except (KeyError, TypeError) as exc:
            log.warning("Skipping malformed Hardcover entry: %s", exc)
            continue

    log.info("Hardcover: fetched %d read books", len(books))
    return books
