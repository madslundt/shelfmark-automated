from unittest.mock import MagicMock, patch

import pytest
import requests

from src.goodreads import (
    _ensure_to_read_shelf,
    _force_shelf,
    fetch_currently_reading,
    fetch_read,
    fetch_want_to_read,
)


def test_ensure_to_read_shelf_appends():
    url = "https://www.goodreads.com/review/list_rss/123456"
    result = _ensure_to_read_shelf(url)
    assert "shelf=to-read" in result


def test_ensure_to_read_shelf_already_present():
    url = "https://www.goodreads.com/review/list_rss/123456?shelf=to-read"
    result = _ensure_to_read_shelf(url)
    assert result == url


def test_ensure_to_read_shelf_preserves_other_params():
    url = "https://www.goodreads.com/review/list_rss/123456?sort=date"
    result = _ensure_to_read_shelf(url)
    assert "shelf=to-read" in result
    assert "sort=date" in result


def _make_feed(entries):
    mock_feed = MagicMock()
    mock_feed.bozo = False
    mock_feed.entries = entries
    return mock_feed


def _make_entry(title, author_name, isbn="", book_id=""):
    entry = MagicMock()
    entry.title = title
    entry.author_name = author_name
    entry.author = ""
    entry.isbn = isbn
    entry.book_id = book_id
    return entry


def test_fetch_want_to_read_parses_entries():
    mock_resp = MagicMock()
    mock_resp.text = "<rss/>"
    mock_resp.raise_for_status = MagicMock()

    entry = _make_entry("Dark Matter", "Blake Crouch", isbn="0553418815", book_id="23395563")
    mock_feed = _make_feed([entry])

    with patch("requests.get", return_value=mock_resp), \
         patch("src.goodreads.feedparser.parse", return_value=mock_feed):
        books = fetch_want_to_read("https://www.goodreads.com/review/list_rss/123456")

    assert len(books) == 1
    assert books[0].title == "Dark Matter"
    assert books[0].author == "Blake Crouch"
    assert books[0].isbn_10 == "0553418815"
    assert books[0].isbn_13 is None
    assert books[0].source == "goodreads"
    assert books[0].source_id == "23395563"


def test_fetch_want_to_read_empty_isbn():
    mock_resp = MagicMock()
    mock_resp.text = "<rss/>"
    mock_resp.raise_for_status = MagicMock()

    entry = _make_entry("Some Book", "Some Author", isbn="", book_id="99999")
    mock_feed = _make_feed([entry])

    with patch("requests.get", return_value=mock_resp), \
         patch("src.goodreads.feedparser.parse", return_value=mock_feed):
        books = fetch_want_to_read("https://www.goodreads.com/review/list_rss/123456")

    assert len(books) == 1
    assert books[0].isbn_10 is None


def test_fetch_want_to_read_skips_title_less_entries():
    mock_resp = MagicMock()
    mock_resp.text = "<rss/>"
    mock_resp.raise_for_status = MagicMock()

    entry = _make_entry("", "Some Author")
    mock_feed = _make_feed([entry])

    with patch("requests.get", return_value=mock_resp), \
         patch("src.goodreads.feedparser.parse", return_value=mock_feed):
        books = fetch_want_to_read("https://www.goodreads.com/review/list_rss/123456")

    assert books == []


def test_fetch_want_to_read_parse_error_returns_empty():
    mock_resp = MagicMock()
    mock_resp.text = "<invalid>"
    mock_resp.raise_for_status = MagicMock()

    mock_feed = MagicMock()
    mock_feed.bozo = True
    mock_feed.entries = []
    mock_feed.get = MagicMock(return_value="parse error")

    with patch("requests.get", return_value=mock_resp), \
         patch("src.goodreads.feedparser.parse", return_value=mock_feed):
        books = fetch_want_to_read("https://www.goodreads.com/review/list_rss/123456")

    assert books == []


def test_fetch_want_to_read_network_error():
    with patch("requests.get", side_effect=requests.ConnectionError("no connection")):
        with pytest.raises(requests.ConnectionError):
            fetch_want_to_read("https://www.goodreads.com/review/list_rss/123456")


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


# ---------------------------------------------------------------------------
# Description from RSS summary
# ---------------------------------------------------------------------------

def _make_entry_with_summary(title, author_name, summary="", isbn="", book_id=""):
    entry = MagicMock()
    entry.title = title
    entry.author_name = author_name
    entry.author = ""
    entry.isbn = isbn
    entry.book_id = book_id
    entry.summary = summary
    return entry


def test_fetch_want_to_read_parses_description_from_summary():
    mock_resp = MagicMock()
    mock_resp.text = "<rss/>"
    mock_resp.raise_for_status = MagicMock()

    entry = _make_entry_with_summary(
        "Dark Matter", "Blake Crouch",
        summary="A mind-bending thriller about parallel universes.",
    )
    mock_feed = _make_feed([entry])

    with patch("requests.get", return_value=mock_resp), \
         patch("src.goodreads.feedparser.parse", return_value=mock_feed):
        books = fetch_want_to_read("https://www.goodreads.com/review/list_rss/123456")

    assert books[0].description == "A mind-bending thriller about parallel universes."


def test_fetch_want_to_read_description_none_when_no_summary():
    mock_resp = MagicMock()
    mock_resp.text = "<rss/>"
    mock_resp.raise_for_status = MagicMock()

    entry = _make_entry("Dark Matter", "Blake Crouch")
    # _make_entry does not set summary attr → getattr returns ""
    mock_feed = _make_feed([entry])

    with patch("requests.get", return_value=mock_resp), \
         patch("src.goodreads.feedparser.parse", return_value=mock_feed):
        books = fetch_want_to_read("https://www.goodreads.com/review/list_rss/123456")

    assert books[0].description is None


def test_fetch_currently_reading_uses_shelf_currently_reading():
    """fetch_currently_reading() must request shelf=currently-reading."""
    captured = {}

    def _capture_get(url, timeout=None, headers=None):
        captured["url"] = url
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = """<?xml version="1.0"?>
<rss version="2.0"><channel></channel></rss>"""
        return mock_resp

    with patch("src.goodreads.requests.get", side_effect=_capture_get):
        fetch_currently_reading("https://www.goodreads.com/review/list_rss/12345")

    assert "shelf=currently-reading" in captured["url"]
    assert "to-read" not in captured["url"]
    assert captured["url"].count("shelf=") == 1


def test_fetch_currently_reading_returns_books():
    mock_resp = MagicMock()
    mock_resp.text = "<rss/>"
    mock_resp.raise_for_status = MagicMock()

    entry = _make_entry_with_summary("Dune", "Frank Herbert", isbn="0441013597", book_id="44767458")
    mock_feed = _make_feed([entry])

    with patch("src.goodreads.requests.get", return_value=mock_resp), \
         patch("src.goodreads.feedparser.parse", return_value=mock_feed):
        books = fetch_currently_reading("https://www.goodreads.com/review/list_rss/12345")

    assert len(books) == 1
    assert books[0].title == "Dune"
    assert books[0].source == "goodreads"


def test_fetch_read_parses_description_from_summary():
    mock_resp = MagicMock()
    mock_resp.text = "<rss/>"
    mock_resp.raise_for_status = MagicMock()

    entry = _make_entry_with_summary(
        "The Martian", "Andy Weir",
        summary="An astronaut is stranded on Mars.",
    )
    mock_feed = _make_feed([entry])

    with patch("src.goodreads.requests.get", return_value=mock_resp), \
         patch("src.goodreads.feedparser.parse", return_value=mock_feed):
        books = fetch_read("https://www.goodreads.com/review/list_rss/12345")

    assert books[0].description == "An astronaut is stranded on Mars."
