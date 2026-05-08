from unittest.mock import MagicMock, patch

import pytest
import requests

from src.cwa import (
    CWAAuthError,
    CWAClient,
    _build_auth_header,
    _strip_series_suffix,
    _titles_match,
    find_book_in_library,
    find_mismatched_author,
    is_book_in_library,
)
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


def test_is_book_in_library_network_error_returns_none():
    # Connection error → all queries fail → return None (skip, retry next cycle)
    book = Book("Dark Matter", "Blake Crouch")
    with patch("requests.get", side_effect=requests.ConnectionError("no connection")):
        assert is_book_in_library(book, "http://cwa:8083", None, None) is None


def test_is_book_in_library_timeout_returns_none():
    # Timeout → all queries fail → return None (skip, retry next cycle)
    book = Book("Dark Matter", "Blake Crouch")
    with patch("requests.get", side_effect=requests.Timeout("timed out")):
        assert is_book_in_library(book, "http://cwa:8083", None, None) is None


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
    # Short title matches but author is clearly different → not counted as owned.
    # "Silence" is only 1 significant word — too common to override author check.
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.text = _OPDS_FEED_SAME_TITLE_DIFF_AUTHOR

    book = Book("Silence", "Shusaku Endo")
    with patch("requests.get", return_value=mock_resp):
        assert is_book_in_library(book, "http://cwa:8083", None, None) is False


def test_is_book_in_library_exact_long_title_author_mismatch_found():
    # Exact match on a specific multi-word title overrides author mismatch.
    # Real-world case: Calibre stores a different edition/editor than Goodreads reports.
    # Title has ≥5 significant words so the author check is bypassed.
    feed = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Nikola Tesla: Imagination and the Man That Invented the 20th Century</title>
    <author><name>Mohammad Belal Ansari</name></author>
  </entry>
</feed>"""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.text = feed

    book = Book(
        "Nikola Tesla: Imagination and the Man That Invented the 20th Century",
        "Sean Patrick",
    )
    with patch("requests.get", return_value=mock_resp):
        assert is_book_in_library(book, "http://cwa:8083", None, None) is True


def test_is_book_in_library_no_author_in_feed_still_matches():
    # Feed without <author> elements — backward compatible, title match alone suffices
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.text = _OPDS_FEED_WITH_DARK_MATTER  # no <author> tags

    book = Book("Dark Matter", "Blake Crouch")
    with patch("requests.get", return_value=mock_resp):
        assert is_book_in_library(book, "http://cwa:8083", None, None) is True


_OPDS_FEED_WITH_ID = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Dark Matter</title>
    <author><name>Blake Crouch</name></author>
    <link href="/opds/download/42/epub/"
          type="application/epub+zip"
          rel="http://opds-spec.org/acquisition"/>
  </entry>
</feed>"""

_OPDS_FEED_TITLE_MATCH_WRONG_AUTHOR = """\
<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>All the Lies We Told</title>
    <author><name>Jennifer Harvey</name></author>
    <link rel="http://opds-spec.org/acquisition"
          href="/opds/download/42/epub/"
          type="application/epub+zip"/>
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
    mock_resp.text = _OPDS_FEED_EMPTY  # already defined in the test file

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


# ---------------------------------------------------------------------------
# find_mismatched_author
# ---------------------------------------------------------------------------

def test_find_mismatched_author_returns_id_and_wrong_author():
    """Title substring match but author differs → returns (book_id, wrong_author)."""
    book = Book("All The Lies", "Nicola Sanders")
    mock_resp = MagicMock(ok=True, text=_OPDS_FEED_TITLE_MATCH_WRONG_AUTHOR)
    with patch("requests.get", return_value=mock_resp):
        result = find_mismatched_author(book, "http://cwa", None, None)
    assert result == (42, "jennifer harvey")


def test_find_mismatched_author_returns_none_when_author_correct():
    """Title and author both match → no mismatch, return None."""
    book = Book("All the Lies We Told", "Jennifer Harvey")
    mock_resp = MagicMock(ok=True, text=_OPDS_FEED_TITLE_MATCH_WRONG_AUTHOR)
    with patch("requests.get", return_value=mock_resp):
        result = find_mismatched_author(book, "http://cwa", None, None)
    assert result is None


def test_find_mismatched_author_returns_none_when_not_in_library():
    """No title match → book not in library, return None."""
    book = Book("Some Completely Different Book", "Some Author")
    mock_resp = MagicMock(ok=True, text=_OPDS_FEED_EMPTY)
    with patch("requests.get", return_value=mock_resp):
        result = find_mismatched_author(book, "http://cwa", None, None)
    assert result is None


def test_find_mismatched_author_returns_none_on_network_error():
    """Network error → return None (don't crash)."""
    book = Book("All The Lies", "Nicola Sanders")
    with patch("requests.get", side_effect=requests.ConnectionError("timeout")):
        result = find_mismatched_author(book, "http://cwa", None, None)
    assert result is None


def test_find_mismatched_author_returns_none_when_id_missing():
    """Title matches, author mismatches, but no acquisition link → None."""
    feed_no_link = """\
<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>All the Lies We Told</title>
    <author><name>Jennifer Harvey</name></author>
  </entry>
</feed>"""
    book = Book("All The Lies", "Nicola Sanders")
    mock_resp = MagicMock(ok=True, text=feed_no_link)
    with patch("requests.get", return_value=mock_resp):
        result = find_mismatched_author(book, "http://cwa", None, None)
    assert result is None


# ---------------------------------------------------------------------------
# CWAClient.update_book_author
# ---------------------------------------------------------------------------

_EDIT_PAGE_HTML = """\
<input type="hidden" name="csrf_token" value="test-csrf-token">
<input type="hidden" name="book_id" value="42">
<input type="text" class="form-control" name="title" id="title" value="All the Lies We Told">
<input type="text" class="form-control" name="authors" id="authors" value="Jennifer Harvey">
<input type="text" class="form-control" name="tags" id="tags" value="Fiction, Thriller">
<input type="text" class="form-control" name="series" id="series" value="">
<input type="number" class="form-control" name="series_index" id="series_index" value="1">
<input type="text" class="form-control" name="pubdate" id="pubdate" value="2023-03-15">
<input type="text" class="form-control" name="publisher" id="publisher" value="Bookouture">
<input type="text" class="form-control" name="languages" id="languages" value="English">
<input type="number" name="rating" id="rating" value="4"/>
<textarea name="comments" rows="7">&lt;p&gt;A gripping thriller.&lt;/p&gt;</textarea>
<input type="text" name="cover_url" id="cover_url" value="">
"""


def test_update_book_author_success():
    """Successful update returns True and POSTs corrected author from admin page."""
    client = CWAClient("http://cwa:8083", "user", "pass")
    client._authenticated = True
    client._csrf_token = "test-csrf-token"

    mock_edit_get = MagicMock(ok=True, text=_EDIT_PAGE_HTML)
    mock_edit_post = MagicMock(ok=True, status_code=200)

    with patch.object(client._session, "get", return_value=mock_edit_get), \
         patch.object(client._session, "post", return_value=mock_edit_post) as mock_post:
        result = client.update_book_author(42, "Nicola Sanders")

    assert result is True
    call_data = mock_post.call_args
    assert call_data[1]["data"]["authors"] == "Nicola Sanders"
    assert call_data[1]["data"]["title"] == "All the Lies We Told"


def test_update_book_author_uses_correct_title_when_provided():
    """When correct_title is passed it overrides the value scraped from the admin page."""
    client = CWAClient("http://cwa:8083", "user", "pass")
    client._authenticated = True
    client._csrf_token = "test-csrf-token"

    mock_edit_get = MagicMock(ok=True, text=_EDIT_PAGE_HTML)
    mock_edit_post = MagicMock(ok=True, status_code=200)

    with patch.object(client._session, "get", return_value=mock_edit_get), \
         patch.object(client._session, "post", return_value=mock_edit_post) as mock_post:
        result = client.update_book_author(42, "Nicola Sanders", correct_title="All the Lies")

    assert result is True
    assert mock_post.call_args[1]["data"]["title"] == "All the Lies"


def test_update_book_author_returns_false_when_admin_page_fails():
    """If /admin/book/<id> returns non-2xx, returns False."""
    client = CWAClient("http://cwa:8083", "user", "pass")
    client._authenticated = True
    client._csrf_token = "tok"

    mock_admin = MagicMock(ok=False, status_code=404)
    with patch.object(client._session, "get", return_value=mock_admin):
        result = client.update_book_author(42, "Nicola Sanders")

    assert result is False


def test_update_book_author_returns_false_when_edit_post_fails():
    """If POST /admin/book/<id> returns non-2xx, returns False."""
    client = CWAClient("http://cwa:8083", "user", "pass")
    client._authenticated = True
    client._csrf_token = "tok"

    mock_edit_get = MagicMock(ok=True, text=_EDIT_PAGE_HTML)
    mock_edit_post = MagicMock(ok=False, status_code=403)

    with patch.object(client._session, "get", return_value=mock_edit_get), \
         patch.object(client._session, "post", return_value=mock_edit_post):
        result = client.update_book_author(42, "Nicola Sanders")

    assert result is False


def test_update_book_author_raises_without_credentials():
    """Raises CWAAuthError if no credentials configured."""
    client = CWAClient("http://cwa:8083", None, None)
    with pytest.raises(CWAAuthError):
        client.update_book_author(42, "Nicola Sanders")


# ---------------------------------------------------------------------------
# CWAClient.get_book_metadata
# ---------------------------------------------------------------------------

def test_get_book_metadata_returns_current_fields():
    """get_book_metadata parses the admin edit page and returns all relevant fields."""
    client = CWAClient("http://cwa:8083", "user", "pass")
    client._authenticated = True

    mock_get = MagicMock(ok=True, text=_EDIT_PAGE_HTML)
    with patch.object(client._session, "get", return_value=mock_get):
        meta = client.get_book_metadata(42)

    assert meta is not None
    assert meta["authors"] == "Jennifer Harvey"
    assert meta["title"] == "All the Lies We Told"
    assert meta["series"] == ""
    assert meta["publisher"] == "Bookouture"
    assert meta["pubdate"] == "2023-03-15"
    assert "gripping thriller" in meta["comments"]


def test_get_book_metadata_returns_none_when_admin_page_fails():
    """If the admin page returns non-2xx, get_book_metadata returns None."""
    client = CWAClient("http://cwa:8083", "user", "pass")
    client._authenticated = True

    mock_get = MagicMock(ok=False, status_code=403)
    with patch.object(client._session, "get", return_value=mock_get):
        meta = client.get_book_metadata(42)

    assert meta is None


def test_get_book_metadata_raises_without_credentials():
    client = CWAClient("http://cwa:8083", None, None)
    with pytest.raises(CWAAuthError):
        client.get_book_metadata(42)


# ---------------------------------------------------------------------------
# CWAClient.update_book_metadata
# ---------------------------------------------------------------------------

def test_update_book_metadata_applies_specified_fields():
    """update_book_metadata merges updates into current page values and POSTs."""
    client = CWAClient("http://cwa:8083", "user", "pass")
    client._authenticated = True
    client._csrf_token = "test-csrf-token"

    mock_get = MagicMock(ok=True, text=_EDIT_PAGE_HTML)
    mock_post = MagicMock(ok=True, status_code=200)

    with patch.object(client._session, "get", return_value=mock_get), \
         patch.object(client._session, "post", return_value=mock_post) as mock_post_call:
        result = client.update_book_metadata(42, {
            "authors": "Nicola Sanders", "series": "Liar Series",
        })

    assert result is True
    posted = mock_post_call.call_args[1]["data"]
    assert posted["authors"] == "Nicola Sanders"
    assert posted["series"] == "Liar Series"
    assert posted["title"] == "All the Lies We Told"   # unchanged from page


def test_update_book_metadata_returns_false_when_admin_page_fails():
    client = CWAClient("http://cwa:8083", "user", "pass")
    client._authenticated = True

    mock_get = MagicMock(ok=False, status_code=404)
    with patch.object(client._session, "get", return_value=mock_get):
        result = client.update_book_metadata(42, {"authors": "Someone"})

    assert result is False


def test_update_book_metadata_raises_without_credentials():
    client = CWAClient("http://cwa:8083", None, None)
    with pytest.raises(CWAAuthError):
        client.update_book_metadata(42, {"authors": "Someone"})


# ---------------------------------------------------------------------------
# Identifier support: get_book_metadata returns identifier_types,
# update_book_metadata accepts new_identifiers
# ---------------------------------------------------------------------------

_EDIT_PAGE_HTML_WITH_IDS = """\
<input type="hidden" name="csrf_token" value="test-csrf-token">
<input type="hidden" name="book_id" value="42">
<input type="text" name="title" value="Dark Matter">
<input type="text" name="authors" value="Blake Crouch">
<input type="text" name="tags" value="">
<input type="text" name="series" value="">
<input type="number" name="series_index" value="1">
<input type="text" name="pubdate" value="">
<input type="text" name="publisher" value="">
<input type="text" name="languages" value="">
<input type="number" name="rating" value="0"/>
<textarea name="comments" rows="7"></textarea>
<input type="text" name="cover_url" value="">
<input type="text" name="identifier-type-0" value="isbn">
<input type="text" name="identifier-val-0" value="9780553418811">
<input type="text" name="identifier-type-1" value="goodreads">
<input type="text" name="identifier-val-1" value="22055262">
"""


def test_get_book_metadata_returns_identifier_types():
    """get_book_metadata returns 'identifier_types' with comma-separated existing types."""
    client = CWAClient("http://cwa:8083", "user", "pass")
    client._authenticated = True

    mock_get = MagicMock(ok=True, text=_EDIT_PAGE_HTML_WITH_IDS)
    with patch.object(client._session, "get", return_value=mock_get):
        meta = client.get_book_metadata(42)

    assert meta is not None
    types = {t.strip() for t in meta["identifier_types"].split(",") if t.strip()}
    assert "isbn" in types
    assert "goodreads" in types


def test_update_book_metadata_adds_new_identifiers():
    """new_identifiers are appended as additional identifier-type/val form fields."""
    client = CWAClient("http://cwa:8083", "user", "pass")
    client._authenticated = True
    client._csrf_token = "test-csrf-token"

    mock_get = MagicMock(ok=True, text=_EDIT_PAGE_HTML_WITH_IDS)
    mock_post_resp = MagicMock(ok=True, status_code=200)

    with patch.object(client._session, "get", return_value=mock_get), \
         patch.object(client._session, "post", return_value=mock_post_resp) as mock_post_call:
        result = client.update_book_metadata(42, {}, new_identifiers={"hardcover": "hc-99"})

    assert result is True
    posted = mock_post_call.call_args[1]["data"]
    assert posted["identifier-type-0"] == "isbn"
    assert posted["identifier-val-0"] == "9780553418811"
    id_values = [posted.get(f"identifier-val-{i}") for i in range(5)]
    assert "hc-99" in id_values
