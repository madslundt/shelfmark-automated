from unittest.mock import MagicMock, patch

import requests

from src.cwa import _build_auth_header, _strip_series_suffix, _titles_match, is_book_in_library
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

_OPDS_FEED_WITH_AUTHOR = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Dark Matter</title>
    <author><name>Blake Crouch</name></author>
  </entry>
</feed>"""

_OPDS_FEED_SAME_TITLE_DIFF_AUTHOR = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Silence</title>
    <author><name>Erling Kagge</name></author>
  </entry>
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


def test_titles_match_reverse_long_subtitle_matches():
    # CWA: "the innovators dilemma" (3 words) ⊆ our long title → True
    assert _titles_match(
        "the innovators dilemma the revolutionary book that will change the way you do business",
        "the innovators dilemma",
    ) is True


def test_titles_match_reverse_short_result_no_match():
    # 2-word result "the one" should NOT match longer title to prevent false positives.
    assert _titles_match("the one dark future 1", "the one") is False


def test_strip_series_suffix_comma_hash():
    assert _strip_series_suffix("Haunting Adeline (Cat and Mouse, #1)") == "Haunting Adeline"


def test_strip_series_suffix_no_comma():
    assert _strip_series_suffix("The One (Dark Future #1)") == "The One"


def test_strip_series_suffix_none_present():
    # Parenthetical without #N should NOT be stripped.
    assert _strip_series_suffix("The Design of Curiosity (Springer Praxis Books)") == \
        "The Design of Curiosity (Springer Praxis Books)"


def test_strip_series_suffix_no_parens():
    assert _strip_series_suffix("Dark Matter") == "Dark Matter"


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


def test_is_book_in_library_correct_author_matches():
    # Feed includes <author> — title + author both match → found
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.text = _OPDS_FEED_WITH_AUTHOR

    book = Book("Dark Matter", "Blake Crouch")
    with patch("requests.get", return_value=mock_resp):
        assert is_book_in_library(book, "http://cwa:8083", None, None) is True


def test_is_book_in_library_author_mismatch_not_found():
    # Title matches but author is clearly different → not counted as owned
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.text = _OPDS_FEED_SAME_TITLE_DIFF_AUTHOR

    book = Book("Silence", "Shusaku Endo")
    with patch("requests.get", return_value=mock_resp):
        assert is_book_in_library(book, "http://cwa:8083", None, None) is False


def test_is_book_in_library_no_author_in_feed_still_matches():
    # Feed without <author> elements — backward compatible, title match alone suffices
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.text = _OPDS_FEED_WITH_DARK_MATTER  # no <author> tags

    book = Book("Dark Matter", "Blake Crouch")
    with patch("requests.get", return_value=mock_resp):
        assert is_book_in_library(book, "http://cwa:8083", None, None) is True
