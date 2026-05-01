from unittest.mock import MagicMock, patch

import pytest
import requests

from src.goodreads import _ensure_to_read_shelf, fetch_want_to_read


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
