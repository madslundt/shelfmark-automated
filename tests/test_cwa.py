from unittest.mock import MagicMock, patch

import requests

from src.cwa import _build_auth_header, _titles_match, is_book_in_library
from src.models import Book

_OPDS_FEED_WITH_DARK_MATTER = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Dark Matter</title></entry>
</feed>"""

_OPDS_FEED_EMPTY = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
</feed>"""

_OPDS_COLLECTION = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Surrounded by Idiots &amp; Surrounded by Psychopaths Collection</title></entry>
</feed>"""


def test_titles_match_exact():
    assert _titles_match("dark matter", "dark matter") is True


def test_titles_match_forward_substring():
    assert _titles_match(
        "surrounded by idiots",
        "surrounded by idiots  surrounded by psychopaths collection",
    ) is True


def test_titles_match_no_false_positive():
    # Searching for "The Martian Chronicles" must NOT match a library entry "The Martian".
    # (We should not say we own "The Martian Chronicles" just because "The Martian" is in library.)
    assert _titles_match("the martian chronicles", "the martian") is False


def test_titles_match_empty():
    assert _titles_match("", "") is True


def test_build_auth_header_with_creds():
    headers = _build_auth_header("user", "pass")
    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Basic ")


def test_build_auth_header_none():
    assert _build_auth_header(None, None) == {}


def test_build_auth_header_empty_strings():
    assert _build_auth_header("", "") == {}


def test_is_book_in_library_found():
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.text = _OPDS_FEED_WITH_DARK_MATTER

    book = Book("Dark Matter", "Blake Crouch")
    with patch("requests.get", return_value=mock_resp):
        assert is_book_in_library(book, "http://cwa:8083", None, None) is True


def test_is_book_in_library_not_found():
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.text = _OPDS_FEED_EMPTY

    book = Book("Nonexistent Book", "Unknown Author")
    with patch("requests.get", return_value=mock_resp):
        assert is_book_in_library(book, "http://cwa:8083", None, None) is False


def test_is_book_in_library_collection_match():
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.text = _OPDS_COLLECTION

    book = Book("Surrounded by Idiots", "Thomas Erikson")
    with patch("requests.get", return_value=mock_resp):
        assert is_book_in_library(book, "http://cwa:8083", None, None) is True


def test_is_book_in_library_network_error_returns_false():
    book = Book("Dark Matter", "Blake Crouch")
    with patch("requests.get", side_effect=requests.ConnectionError("no connection")):
        assert is_book_in_library(book, "http://cwa:8083", None, None) is False


def test_is_book_in_library_auth_error_returns_false():
    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 401
    mock_resp.text = ""

    book = Book("Dark Matter", "Blake Crouch")
    with patch("requests.get", return_value=mock_resp):
        assert is_book_in_library(book, "http://cwa:8083", "user", "wrong") is False


def test_is_book_in_library_empty_title_returns_false():
    book = Book("", "Blake Crouch")
    with patch("requests.get") as mock_get:
        result = is_book_in_library(book, "http://cwa:8083", None, None)
    assert result is False
    mock_get.assert_not_called()
