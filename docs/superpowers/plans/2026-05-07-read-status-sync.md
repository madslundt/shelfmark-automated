# Read Status Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a sync pass that fetches fully-read books from Hardcover (status_id=3) and Goodreads (shelf=read) and marks them as read in CWA via its web session API.

**Architecture:** Two new fetch functions mirror the existing `fetch_want_to_read` pattern. A new `find_book_in_library()` in `cwa.py` reuses the existing OPDS title/author matching but also extracts the Calibre numeric book ID from entry link hrefs. A `CWAClient` class handles web-session login and `POST /ajax/book/{id}/readstatus`. State tracking uses a separate `read_status_books` SQLite table so the download sync is not affected. A new `sync_read_status_once()` in `main.py` ties it together and is gated by a configurable interval (`READ_STATUS_SYNC_INTERVAL_SECONDS`, default 86400 = daily, 0 = disabled). Timing is persisted in the `meta` table when a state file is configured; otherwise tracked in memory.

**Scope constraint:** Only fully-completed reads are processed. Currently-reading / in-progress books (Hardcover status_id=2, Goodreads shelf=currently-reading) are never touched.

**Tech Stack:** Python 3.11+, requests, feedparser, sqlite3, unittest.mock (tests)

---

## File Map

| File | Change |
|------|--------|
| `src/hardcover.py` | Add `fetch_read()` (status_id=3) |
| `src/goodreads.py` | Add `_force_shelf()` helper + `fetch_read()` (shelf=read) |
| `src/cwa.py` | Add `_BOOK_ID_RE`, `_extract_book_id()`, `_get_opds_entries_with_id()`, `find_book_in_library()`, `CWAAuthError`, `CWAClient` |
| `src/state.py` | Add `read_status_books` table, `is_read_status_set()`, `mark_read_status_set()`, `get_last_read_status_sync()`, `set_last_read_status_sync()` |
| `main.py` | Add `read_status_sync_interval_seconds` to `Config`, `_is_read_status_sync_due()`, `sync_read_status_once()`, init `CWAClient` in `main()`, call in loop |
| `tests/test_hardcover.py` | Add `test_fetch_read_*` tests |
| `tests/test_goodreads.py` | Add `test_fetch_read_*` tests |
| `tests/test_cwa.py` | Add tests for `find_book_in_library()` and `CWAClient` |
| `tests/test_main.py` | Add `test_sync_read_status_once_*` and `test_read_status_scheduling_*` tests |

---

## Task 1: Add `fetch_read()` to `src/hardcover.py`

**Files:**
- Modify: `src/hardcover.py`
- Test: `tests/test_hardcover.py`

The new function is identical to `fetch_want_to_read` except `status_id: {_eq: 3}` (Read/Finished) instead of `{_eq: 1}`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_hardcover.py`:

```python
from src.hardcover import fetch_read  # add to existing import line

def test_fetch_read_success():
    data = _want_to_read_payload({
        "id": 10,
        "title": "The Martian",
        "author": "Andy Weir",
        "edition": {"isbn_10": "0553418025", "isbn_13": "9780553418026"},
    })
    mock_resp = _mock_session_post(data)

    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_read("test-api-key")

    assert len(books) == 1
    assert books[0].title == "The Martian"
    assert books[0].source == "hardcover"


def test_fetch_read_uses_status_id_3():
    """Verify the GraphQL query targets status_id=3 (Read), not 1 (Want to Read)."""
    captured = {}

    def _capture_post(url, json=None, timeout=None):
        captured["query"] = json.get("query", "")
        mock_resp = _mock_session_post({"data": {"me": [{"user_books": []}]}})
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.side_effect = _capture_post
        mock_session_cls.return_value.headers = MagicMock()
        fetch_read("key")

    assert "_eq: 3" in captured["query"]
    assert "_eq: 1" not in captured["query"]


def test_fetch_read_empty_returns_empty_list():
    data = {"data": {"me": [{"user_books": []}]}}
    mock_resp = _mock_session_post(data)

    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_read("key")

    assert books == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/madslundt/Documents/shelfmark-automated
uv run pytest tests/test_hardcover.py::test_fetch_read_success tests/test_hardcover.py::test_fetch_read_uses_status_id_3 tests/test_hardcover.py::test_fetch_read_empty_returns_empty_list -v
```

Expected: FAIL with `ImportError: cannot import name 'fetch_read'`

- [ ] **Step 3: Add `_READ_QUERY` constant and `fetch_read()` to `src/hardcover.py`**

After the existing `_WANT_TO_READ_QUERY` constant, add:

```python
_READ_QUERY = """
{
  me {
    user_books(where: {status_id: {_eq: 3}}) {
      book {
        id
        title
        contributions {
          author {
            name
          }
        }
        default_physical_edition {
          isbn_10
          isbn_13
        }
      }
    }
  }
}
"""
```

After the existing `fetch_want_to_read()` function, add:

```python
def fetch_read(api_key: str) -> list[Book]:
    """Fetch fully-read books (status_id=3) from Hardcover.

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

            books.append(Book(
                title=title,
                author=author,
                isbn_10=isbn_10,
                isbn_13=isbn_13,
                source="hardcover",
                source_id=str(book_data.get("id", "")),
            ))
        except (KeyError, TypeError) as exc:
            log.warning("Skipping malformed Hardcover entry: %s", exc)
            continue

    log.info("Hardcover: fetched %d read books", len(books))
    return books
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_hardcover.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hardcover.py tests/test_hardcover.py
git commit -m "feat: add fetch_read() to hardcover module (status_id=3)"
```

---

## Task 2: Add `fetch_read()` to `src/goodreads.py`

**Files:**
- Modify: `src/goodreads.py`
- Test: `tests/test_goodreads.py`

Add a `_force_shelf()` helper that replaces the shelf param (unlike `_ensure_to_read_shelf` which only adds if missing). `fetch_read()` uses `_force_shelf(url, "read")`.

- [ ] **Step 1: Write the failing tests**

Read `tests/test_goodreads.py` first to understand the existing fixture structure, then add:

```python
from src.goodreads import fetch_read, _force_shelf  # add to existing import

def test_force_shelf_adds_when_missing():
    url = "https://www.goodreads.com/review/list_rss/12345"
    result = _force_shelf(url, "read")
    assert "shelf=read" in result


def test_force_shelf_replaces_existing():
    url = "https://www.goodreads.com/review/list_rss/12345?shelf=to-read"
    result = _force_shelf(url, "read")
    assert "shelf=read" in result
    assert "to-read" not in result


def test_fetch_read_uses_shelf_read():
    """fetch_read() must request shelf=read, not shelf=to-read."""
    captured = {}

    def _capture_get(url, timeout=None, headers=None):
        captured["url"] = url
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = """<?xml version="1.0"?>
<rss version="2.0"><channel></channel></rss>"""
        return mock_resp

    with patch("src.goodreads.requests.get", side_effect=_capture_get):
        fetch_read("https://www.goodreads.com/review/list_rss/12345")

    assert "shelf=read" in captured["url"]
    assert "to-read" not in captured["url"]


def test_fetch_read_returns_books():
    rss = """<?xml version="1.0"?>
<rss version="2.0"
     xmlns:gr="http://www.goodreads.com/gr/item/">
  <channel>
    <item>
      <title>The Martian</title>
      <gr:author_name>Andy Weir</gr:author_name>
      <gr:isbn>0553418025</gr:isbn>
      <gr:book_id>18007564</gr:book_id>
    </item>
  </channel>
</rss>"""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.text = rss

    with patch("src.goodreads.requests.get", return_value=mock_resp):
        books = fetch_read("https://www.goodreads.com/review/list_rss/12345")

    assert len(books) == 1
    assert books[0].title == "The Martian"
    assert books[0].source == "goodreads"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_goodreads.py::test_force_shelf_adds_when_missing tests/test_goodreads.py::test_force_shelf_replaces_existing tests/test_goodreads.py::test_fetch_read_uses_shelf_read tests/test_goodreads.py::test_fetch_read_returns_books -v
```

Expected: FAIL with `ImportError: cannot import name 'fetch_read'`

- [ ] **Step 3: Add `_force_shelf()` and `fetch_read()` to `src/goodreads.py`**

After `_ensure_to_read_shelf()`, add:

```python
def _force_shelf(url: str, shelf: str) -> str:
    """Set ?shelf=<shelf> in the URL, replacing any existing shelf value."""
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    params["shelf"] = [shelf]
    new_query = urllib.parse.urlencode(params, doseq=True)
    return parsed._replace(query=new_query).geturl()
```

After `fetch_want_to_read()`, add:

```python
def fetch_read(rss_url: str) -> list[Book]:
    """Fetch fully-read books from the Goodreads 'read' shelf RSS feed.

    Only fully-completed reads are returned. Currently-reading / in-progress
    entries are not fetched (use shelf=read, not shelf=currently-reading).

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

            books.append(Book(
                title=title,
                author=author,
                isbn_10=isbn_10,
                isbn_13=None,
                source="goodreads",
                source_id=source_id,
            ))
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed Goodreads entry: %s", exc)
            continue

    log.info("Goodreads: fetched %d read books", len(books))
    return books
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_goodreads.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/goodreads.py tests/test_goodreads.py
git commit -m "feat: add fetch_read() and _force_shelf() to goodreads module"
```

---

## Task 3: Add `find_book_in_library()` and `CWAClient` to `src/cwa.py`

**Files:**
- Modify: `src/cwa.py`
- Test: `tests/test_cwa.py`

`find_book_in_library()` reuses the same OPDS title/author matching as `is_book_in_library()` but also extracts the Calibre numeric book ID from entry link hrefs (pattern `/opds/book/(\d+)/`). `CWAClient` handles web session login (with CSRF token extraction) and `POST /ajax/book/{id}/readstatus`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cwa.py`:

```python
import re
from src.cwa import find_book_in_library, CWAClient, CWAAuthError  # add to import

_OPDS_FEED_WITH_ID = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Dark Matter</title>
    <author><name>Blake Crouch</name></author>
    <link href="/opds/book/42/epub/Dark Matter - Blake Crouch.epub"
          type="application/epub+zip"
          rel="http://opds-spec.org/acquisition"/>
  </entry>
</feed>"""

_OPDS_FEED_NO_LINK = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Dark Matter</title>
    <author><name>Blake Crouch</name></author>
  </entry>
</feed>"""


def test_find_book_in_library_returns_id():
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.text = _OPDS_FEED_WITH_ID

    book = Book("Dark Matter", "Blake Crouch")
    with patch("requests.get", return_value=mock_resp):
        result = find_book_in_library(book, "http://cwa:8083", None, None)

    assert result == 42


def test_find_book_in_library_not_found_returns_none():
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.text = _OPDS_FEED_EMPTY

    book = Book("Nonexistent Book", "Unknown Author")
    with patch("requests.get", return_value=mock_resp):
        result = find_book_in_library(book, "http://cwa:8083", None, None)

    assert result is None


def test_find_book_in_library_match_no_link_returns_none():
    # Title matches but no acquisition link → cannot extract ID
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.text = _OPDS_FEED_NO_LINK

    book = Book("Dark Matter", "Blake Crouch")
    with patch("requests.get", return_value=mock_resp):
        result = find_book_in_library(book, "http://cwa:8083", None, None)

    assert result is None


def test_find_book_in_library_network_error_returns_none():
    book = Book("Dark Matter", "Blake Crouch")
    with patch("requests.get", side_effect=requests.ConnectionError("no connection")):
        result = find_book_in_library(book, "http://cwa:8083", None, None)

    assert result is None


def test_cwa_client_mark_as_read_success():
    mock_login_page = MagicMock()
    mock_login_page.status_code = 200
    mock_login_page.text = '<input name="csrf_token" type="hidden" value="test-csrf-123">'
    mock_login_page.url = "http://cwa:8083/login"

    mock_login_post = MagicMock()
    mock_login_post.status_code = 200
    mock_login_post.url = "http://cwa:8083/"

    mock_mark = MagicMock()
    mock_mark.ok = True
    mock_mark.status_code = 200

    client = CWAClient("http://cwa:8083", "admin", "secret")
    with patch.object(client._session, "get", return_value=mock_login_page), \
         patch.object(client._session, "post", side_effect=[mock_login_post, mock_mark]):
        result = client.mark_as_read(42)

    assert result is True


def test_cwa_client_mark_as_read_http_error_returns_false():
    mock_login_page = MagicMock()
    mock_login_page.status_code = 200
    mock_login_page.text = '<input name="csrf_token" type="hidden" value="tok">'
    mock_login_page.url = "http://cwa:8083/login"

    mock_login_post = MagicMock()
    mock_login_post.status_code = 200
    mock_login_post.url = "http://cwa:8083/"

    mock_mark = MagicMock()
    mock_mark.ok = False
    mock_mark.status_code = 500

    client = CWAClient("http://cwa:8083", "admin", "secret")
    with patch.object(client._session, "get", return_value=mock_login_page), \
         patch.object(client._session, "post", side_effect=[mock_login_post, mock_mark]):
        result = client.mark_as_read(42)

    assert result is False


def test_cwa_client_raises_on_missing_credentials():
    client = CWAClient("http://cwa:8083", None, None)
    with pytest.raises(CWAAuthError):
        client.mark_as_read(42)
```

Also add `import pytest` to the test file if not already present.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_cwa.py::test_find_book_in_library_returns_id tests/test_cwa.py::test_cwa_client_mark_as_read_success tests/test_cwa.py::test_cwa_client_raises_on_missing_credentials -v
```

Expected: FAIL with `ImportError`

- [ ] **Step 3: Add `find_book_in_library()`, `CWAAuthError`, and `CWAClient` to `src/cwa.py`**

Add after the existing imports and constants (after `_SERIES_SUFFIX_RE`):

```python
# Extracts the numeric Calibre book ID from an OPDS acquisition link href.
# Calibre-Web serves these as /opds/book/<id>/epub/... or /opds/book/<id>/pdf/...
_BOOK_ID_RE = re.compile(r"/opds/book/(\d+)/")


class CWAAuthError(Exception):
    """Raised when CWA web session authentication cannot be established."""
```

Add a new private helper after `_search_opds()`:

```python
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
```

Add after `is_book_in_library()`:

```python
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

    def _login(self) -> None:
        """Login to CWA via the web form, storing the session cookie."""
        try:
            resp = self._session.get(f"{self._base_url}/login", timeout=10)
            resp.raise_for_status()
            # Extract CSRF token from the hidden form field
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
                f"{self._base_url}/ajax/book/{book_id}/readstatus",
                data={"is_read": 1},
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_cwa.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/cwa.py tests/test_cwa.py
git commit -m "feat: add find_book_in_library() and CWAClient to cwa module"
```

---

## Task 4: Add read status table to `src/state.py`

**Files:**
- Modify: `src/state.py`
- Test: `tests/test_state.py`

A separate `read_status_books` table prevents read-status entries from interfering with the download sync's `is_handled()` check.

- [ ] **Step 1: Write the failing tests**

Read `tests/test_state.py` first to see the fixture pattern, then add:

```python
from src.state import REASON_READ_STATUS_SET  # add to existing import

def test_is_read_status_set_false_initially(tmp_path):
    state = StateManager(str(tmp_path / "state.db"))
    book = Book("The Martian", "Andy Weir")
    assert state.is_read_status_set(book) is False
    state.close()


def test_mark_read_status_set_and_retrieve(tmp_path):
    state = StateManager(str(tmp_path / "state.db"))
    book = Book("The Martian", "Andy Weir")
    state.mark_read_status_set(book)
    state.save()
    assert state.is_read_status_set(book) is True
    state.close()


def test_read_status_does_not_affect_is_handled(tmp_path):
    # Marking read status must NOT cause is_handled() to return True
    # (prevents read-status sync from blocking download sync)
    state = StateManager(str(tmp_path / "state.db"))
    book = Book("The Martian", "Andy Weir")
    state.mark_read_status_set(book)
    state.save()
    assert state.is_handled(book) is False
    state.close()


def test_read_status_constant():
    assert REASON_READ_STATUS_SET == "read_status_set"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_state.py::test_is_read_status_set_false_initially tests/test_state.py::test_mark_read_status_set_and_retrieve tests/test_state.py::test_read_status_does_not_affect_is_handled tests/test_state.py::test_read_status_constant -v
```

Expected: FAIL

- [ ] **Step 3: Update `src/state.py`**

Add the constant after `REASON_SUBMITTED`:

```python
REASON_READ_STATUS_SET = "read_status_set"
```

In `_setup_schema()`, extend the `executescript` to add the new table:

```python
def _setup_schema(self) -> None:
    self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS handled_books (
            key        TEXT PRIMARY KEY,
            reason     TEXT NOT NULL,
            handled_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS read_status_books (
            key        TEXT PRIMARY KEY,
            handled_at TEXT NOT NULL
        );
    """)
    self._conn.commit()
    # Migrate legacy "in_library" reason to "imported"
    self._conn.execute(
        "UPDATE handled_books SET reason = ? WHERE reason = ?",
        (REASON_IMPORTED, REASON_IN_LIBRARY),
    )
    self._conn.commit()
```

Add two new public methods after `mark_handled()`:

```python
def is_read_status_set(self, book: Book) -> bool:
    """Return True if this book's read status was already synced to CWA."""
    row = self._conn.execute(
        "SELECT 1 FROM read_status_books WHERE key = ?",
        (book.normalized_key(),),
    ).fetchone()
    return row is not None

def mark_read_status_set(self, book: Book) -> None:
    """Record that this book's read status was synced. Deferred — call save() to commit."""
    self._conn.execute(
        "INSERT OR REPLACE INTO read_status_books (key, handled_at) VALUES (?, ?)",
        (book.normalized_key(), datetime.now().isoformat(timespec="seconds")),
    )
    self._dirty = True
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_state.py -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/state.py tests/test_state.py
git commit -m "feat: add read_status_books table and tracking methods to StateManager"
```

---

## Task 5: Add `sync_read_status_once()` to `main.py`

**Files:**
- Modify: `main.py`
- Test: `tests/test_main.py`

`sync_read_status_once()` fetches read books from Hardcover/Goodreads, skips already-processed ones via state, finds each book in CWA via OPDS, and marks it as read via `CWAClient`. The function is called once per main-loop iteration, after the existing download sync.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_main.py`:

```python
from main import sync_read_status_once  # add to existing import
from src.cwa import CWAClient, CWAAuthError  # add to existing import

def test_sync_read_status_marks_found_book(tmp_path):
    config = Config(
        hardcover_api_key="key",
        goodreads_rss_url=None,
        cwa_url="http://cwa:8083",
        cwa_username="admin",
        cwa_password="secret",
        shelfmark_url=None,
        shelfmark_username=None,
        shelfmark_password=None,
        sync_interval_min_seconds=120,
        sync_interval_max_seconds=900,
        state_file=None,
        full_sync_interval_seconds=86400,
        log_level="INFO",
    )
    book = Book("The Martian", "Andy Weir", source="hardcover")

    mock_cwa = MagicMock(spec=CWAClient)
    mock_cwa.mark_as_read.return_value = True

    with patch("main.hardcover.fetch_read", return_value=[book]), \
         patch("main.cwa.find_book_in_library", return_value=42):
        sync_read_status_once(config, mock_cwa, state=None)

    mock_cwa.mark_as_read.assert_called_once_with(42)


def test_sync_read_status_skips_book_not_in_library(tmp_path):
    config = Config(
        hardcover_api_key="key",
        goodreads_rss_url=None,
        cwa_url="http://cwa:8083",
        cwa_username="admin",
        cwa_password="secret",
        shelfmark_url=None,
        shelfmark_username=None,
        shelfmark_password=None,
        sync_interval_min_seconds=120,
        sync_interval_max_seconds=900,
        state_file=None,
        full_sync_interval_seconds=86400,
        log_level="INFO",
    )
    book = Book("Unknown Book", "Nobody", source="hardcover")

    mock_cwa = MagicMock(spec=CWAClient)

    with patch("main.hardcover.fetch_read", return_value=[book]), \
         patch("main.cwa.find_book_in_library", return_value=None):
        sync_read_status_once(config, mock_cwa, state=None)

    mock_cwa.mark_as_read.assert_not_called()


def test_sync_read_status_skips_already_processed(tmp_path):
    from src.state import StateManager
    state = StateManager(str(tmp_path / "state.db"))
    config = Config(
        hardcover_api_key="key",
        goodreads_rss_url=None,
        cwa_url="http://cwa:8083",
        cwa_username="admin",
        cwa_password="secret",
        shelfmark_url=None,
        shelfmark_username=None,
        shelfmark_password=None,
        sync_interval_min_seconds=120,
        sync_interval_max_seconds=900,
        state_file=None,
        full_sync_interval_seconds=86400,
        log_level="INFO",
    )
    book = Book("The Martian", "Andy Weir", source="hardcover")
    state.mark_read_status_set(book)
    state.save()

    mock_cwa = MagicMock(spec=CWAClient)

    with patch("main.hardcover.fetch_read", return_value=[book]):
        sync_read_status_once(config, mock_cwa, state=state)

    mock_cwa.mark_as_read.assert_not_called()
    state.close()


def test_sync_read_status_no_cwa_client_exits_early():
    config = Config(
        hardcover_api_key="key",
        goodreads_rss_url=None,
        cwa_url=None,
        cwa_username=None,
        cwa_password=None,
        shelfmark_url=None,
        shelfmark_username=None,
        shelfmark_password=None,
        sync_interval_min_seconds=120,
        sync_interval_max_seconds=900,
        state_file=None,
        full_sync_interval_seconds=86400,
        log_level="INFO",
    )

    with patch("main.hardcover.fetch_read", return_value=[]) as mock_fetch:
        sync_read_status_once(config, cwa_client=None, state=None)

    # Should return early without calling fetch when no CWA URL configured
    mock_fetch.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_main.py::test_sync_read_status_marks_found_book tests/test_main.py::test_sync_read_status_skips_book_not_in_library tests/test_main.py::test_sync_read_status_skips_already_processed tests/test_main.py::test_sync_read_status_no_cwa_client_exits_early -v
```

Expected: FAIL with `ImportError: cannot import name 'sync_read_status_once'`

- [ ] **Step 3: Add imports and `sync_read_status_once()` to `main.py`**

Add to the existing imports at the top of `main.py`:

```python
from src.cwa import CWAAuthError, CWAClient
```

Add `sync_read_status_once()` after `sync_once()`:

```python
def sync_read_status_once(
    config: Config,
    cwa_client: CWAClient | None,
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
```

- [ ] **Step 4: Update `main()` to init `CWAClient` and call `sync_read_status_once()`**

In `main()`, after the `shelfmark_client` initialization, add:

```python
cwa_read_client: CWAClient | None = None
if config.cwa_url and config.cwa_username and config.cwa_password:
    cwa_read_client = CWAClient(config.cwa_url, config.cwa_username, config.cwa_password)
```

Inside the main `while True` loop, after `sync_once(...)` call, add:

```python
try:
    sync_read_status_once(config, cwa_read_client, state)
except Exception as exc:  # noqa: BLE001
    log.error("Read status sync pass failed: %s", exc, exc_info=True)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_main.py -v
```

Expected: all tests PASS

- [ ] **Step 6: Run full test suite**

```bash
uv run pytest -v
```

Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: add sync_read_status_once() — marks completed books as read in CWA"
```

---

## Task 6: Configurable schedule for read status sync

**Files:**
- Modify: `src/state.py`
- Modify: `main.py`
- Test: `tests/test_state.py`
- Test: `tests/test_main.py`

Add `READ_STATUS_SYNC_INTERVAL_SECONDS` env var (default 86400 = daily, 0 = disabled). Timing is persisted in the existing `meta` table using a new key `last_read_status_sync_at`; when state is None, the last-run time is tracked in an in-memory variable inside `main()`.

- [ ] **Step 1: Write failing tests for state timing methods**

Add to `tests/test_state.py`:

```python
def test_get_last_read_status_sync_returns_none_initially(tmp_path):
    state = StateManager(str(tmp_path / "state.db"))
    assert state.get_last_read_status_sync() is None
    state.close()


def test_set_and_get_last_read_status_sync(tmp_path):
    from datetime import datetime, timedelta
    state = StateManager(str(tmp_path / "state.db"))
    state.set_last_read_status_sync()
    result = state.get_last_read_status_sync()
    assert result is not None
    assert abs((result - datetime.now()).total_seconds()) < 5
    state.close()
```

Add to `tests/test_main.py`:

```python
from main import _is_read_status_sync_due  # add to existing import

def test_config_read_status_sync_interval_default():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()
    assert config.read_status_sync_interval_seconds == 86400


def test_config_read_status_sync_interval_zero_disables():
    with patch.dict(os.environ, {"READ_STATUS_SYNC_INTERVAL_SECONDS": "0"}, clear=True):
        config = Config.from_env()
    assert config.read_status_sync_interval_seconds == 0


def test_config_read_status_sync_interval_custom():
    with patch.dict(os.environ, {"READ_STATUS_SYNC_INTERVAL_SECONDS": "3600"}, clear=True):
        config = Config.from_env()
    assert config.read_status_sync_interval_seconds == 3600


def test_is_read_status_sync_due_disabled():
    # interval=0 means disabled → never due
    assert _is_read_status_sync_due(last=None, interval_seconds=0) is False


def test_is_read_status_sync_due_no_prior_run():
    # No prior run → due immediately (first ever run)
    assert _is_read_status_sync_due(last=None, interval_seconds=86400) is True


def test_is_read_status_sync_due_recent_run():
    from datetime import datetime, timedelta
    recent = datetime.now() - timedelta(hours=1)
    assert _is_read_status_sync_due(last=recent, interval_seconds=86400) is False


def test_is_read_status_sync_due_stale_run():
    from datetime import datetime, timedelta
    old = datetime.now() - timedelta(days=2)
    assert _is_read_status_sync_due(last=old, interval_seconds=86400) is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_state.py::test_get_last_read_status_sync_returns_none_initially tests/test_state.py::test_set_and_get_last_read_status_sync tests/test_main.py::test_config_read_status_sync_interval_default tests/test_main.py::test_is_read_status_sync_due_disabled tests/test_main.py::test_is_read_status_sync_due_no_prior_run -v
```

Expected: FAIL

- [ ] **Step 3: Add timing methods to `src/state.py`**

Add after `set_last_full_sync()`:

```python
def get_last_read_status_sync(self) -> datetime | None:
    """Return the timestamp of the last read status sync, or None."""
    row = self._conn.execute(
        "SELECT value FROM meta WHERE key = 'last_read_status_sync_at'",
    ).fetchone()
    if row is None:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None

def set_last_read_status_sync(self) -> None:
    """Record the current time as the last read status sync timestamp."""
    try:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_read_status_sync_at', ?)",
            (datetime.now().isoformat(timespec="seconds"),),
        )
        self._conn.commit()
    except sqlite3.Error as exc:
        log.error("Failed to record read status sync timestamp: %s", exc)
```

- [ ] **Step 4: Add `read_status_sync_interval_seconds` to `Config` in `main.py`**

Add field to the `Config` dataclass after `full_sync_interval_seconds`:

```python
read_status_sync_interval_seconds: int
```

Add parsing in `Config.from_env()` after the `full_sync_interval` block:

```python
try:
    read_status_sync_interval = int(
        os.environ.get("READ_STATUS_SYNC_INTERVAL_SECONDS", "86400") or "86400"
    )
except ValueError:
    read_status_sync_interval = 86400
```

Add to the `return cls(...)` call:

```python
read_status_sync_interval_seconds=read_status_sync_interval,
```

- [ ] **Step 5: Add `_is_read_status_sync_due()` to `main.py`**

Add after `_is_full_sync_due()`:

```python
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
```

- [ ] **Step 6: Update `main()` to gate `sync_read_status_once()` on schedule**

Replace the `sync_read_status_once` call added in Task 5 with:

```python
# Read status sync — runs on its own configurable interval (default: daily)
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
else:
    log.debug("Read status sync: not due yet — skipping this cycle")
```

Also update the startup log message in `main()` to include the read status interval:

```python
log.info(
    "shelfmark-automated starting up  "
    "(interval=%d-%ds, full_sync=%ds, read_status_sync=%s, cwa=%s, shelfmark=%s, state=%s)",
    config.sync_interval_min_seconds,
    config.sync_interval_max_seconds,
    config.full_sync_interval_seconds,
    f"{config.read_status_sync_interval_seconds}s" if config.read_status_sync_interval_seconds > 0 else "disabled",
    config.cwa_url or "not configured",
    config.shelfmark_url or "not configured",
    config.state_file or "disabled",
)
```

- [ ] **Step 7: Run the full test suite**

```bash
uv run pytest -v
```

Expected: all tests PASS

- [ ] **Step 8: Commit**

```bash
git add src/state.py main.py tests/test_state.py tests/test_main.py
git commit -m "feat: add configurable schedule for read status sync (READ_STATUS_SYNC_INTERVAL_SECONDS)"
```

---

## Self-Review

**Spec coverage check:**

| Requirement | Task covering it |
|-------------|-----------------|
| Fetch read books from Hardcover API | Task 1 — `fetch_read()` with status_id=3 |
| Fetch read books from Goodreads RSS | Task 2 — `fetch_read()` with shelf=read |
| Match books to CWA using same strategy | Task 3 — `find_book_in_library()` reuses `_titles_match` + `_authors_compatible` |
| Mark matched books as read in CWA | Task 3 — `CWAClient.mark_as_read()` |
| Only fully-read books (no in-progress) | Task 1 (status_id=3 only), Task 2 (shelf=read only, not currently-reading) |
| State tracking to avoid re-processing | Task 4 — `read_status_books` table |
| Integrate into sync loop | Task 5 — `sync_read_status_once()` in main loop |
| Configurable schedule (default daily) | Task 6 — `READ_STATUS_SYNC_INTERVAL_SECONDS`, `_is_read_status_sync_due()` |
| Disable read status sync entirely | Task 6 — `READ_STATUS_SYNC_INTERVAL_SECONDS=0` |

**Placeholder scan:** None found — all steps contain actual code.

**Type consistency:** `find_book_in_library` returns `int | None` throughout Tasks 3 and 5. `CWAClient` is referenced consistently. `StateManager.mark_read_status_set` / `is_read_status_set` match across Tasks 4 and 5.

**Edge cases covered:**
- Book in Hardcover/Goodreads but not in CWA library → skipped (book_id=None)
- CWA credentials missing → `CWAAuthError` raised, loop breaks for that pass
- Book already processed in previous run → state check skips it
- `CWA_URL` not configured → exits early without fetching
- Network errors in OPDS lookup → returns None (same as `is_book_in_library`)
