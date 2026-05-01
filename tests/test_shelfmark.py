from unittest.mock import MagicMock, patch

import requests

from src.models import Book
from src.shelfmark import ShelfmarkClient


def _make_client(username="user", password="pass"):
    return ShelfmarkClient("http://shelfmark:8084", username, password)


def _mock_resp(status_code, json_data=None):
    mock = MagicMock()
    mock.status_code = status_code
    mock.ok = 200 <= status_code < 300
    mock.json.return_value = json_data or {}
    mock.text = ""
    return mock


# ---------------------------------------------------------------------------
# request_book — request mode (default)
# ---------------------------------------------------------------------------

def test_request_book_success():
    client = _make_client()
    book = Book("Dark Matter", "Blake Crouch")

    login_resp = _mock_resp(200)
    metadata_resp = _mock_resp(200, {"books": [{"provider": "hardcover", "provider_id": "123"}]})
    request_resp = _mock_resp(200)

    with patch.object(client._session, "post", side_effect=[login_resp, request_resp]), \
         patch.object(client._session, "get", return_value=metadata_resp):
        result = client.request_book(book)

    assert result is True


def test_request_book_no_metadata_returns_false():
    client = _make_client()
    book = Book("Unknown Book", "Unknown Author")

    login_resp = _mock_resp(200)
    metadata_resp = _mock_resp(200, {"books": []})

    with patch.object(client._session, "post", return_value=login_resp), \
         patch.object(client._session, "get", return_value=metadata_resp):
        result = client.request_book(book)

    assert result is False


def test_request_book_connection_error_returns_false():
    client = _make_client()
    book = Book("Dark Matter", "Blake Crouch")

    with patch.object(client._session, "post", side_effect=requests.ConnectionError("no conn")):
        result = client.request_book(book)

    assert result is False


def test_request_book_queue_full_returns_false():
    client = _make_client()
    book = Book("Dark Matter", "Blake Crouch")

    login_resp = _mock_resp(200)
    metadata_resp = _mock_resp(200, {"books": [{"provider": "hardcover", "provider_id": "123"}]})
    queue_full_resp = _mock_resp(409)

    with patch.object(client._session, "post", side_effect=[login_resp, queue_full_resp]), \
         patch.object(client._session, "get", return_value=metadata_resp):
        result = client.request_book(book)

    assert result is False


def test_no_auth_mode_detected():
    client = _make_client(username=None, password=None)
    client._submission_mode = "request"
    book = Book("Dark Matter", "Blake Crouch")

    auth_check_resp = _mock_resp(200, {"auth_required": False})
    metadata_resp = _mock_resp(200, {"books": [{"provider": "hardcover", "provider_id": "123"}]})
    request_resp = _mock_resp(200)

    with patch.object(client._session, "get", side_effect=[auth_check_resp, metadata_resp]), \
         patch.object(client._session, "post", return_value=request_resp):
        result = client.request_book(book)

    assert result is True


def test_auth_required_raises_when_no_creds():
    client = _make_client(username=None, password=None)

    auth_check_resp = _mock_resp(200, {"auth_required": True})

    with patch.object(client._session, "get", return_value=auth_check_resp):
        result = client.request_book(Book("Title", "Author"))

    assert result is False


# ---------------------------------------------------------------------------
# request_books_batch — request mode
# ---------------------------------------------------------------------------

def test_request_books_batch_success():
    client = _make_client()
    client._submission_mode = "request"
    books = [
        Book("Dark Matter", "Blake Crouch"),
        Book("Project Hail Mary", "Andy Weir"),
    ]

    login_resp = _mock_resp(200)
    metadata_resp1 = _mock_resp(200, {"books": [{"provider": "hardcover", "provider_id": "1"}]})
    metadata_resp2 = _mock_resp(200, {"books": [{"provider": "hardcover", "provider_id": "2"}]})
    batch_resp = _mock_resp(200)

    with patch.object(client._session, "post", side_effect=[login_resp, batch_resp]), \
         patch.object(client._session, "get", side_effect=[metadata_resp1, metadata_resp2]):
        results = client.request_books_batch(books)

    assert all(v for v in results.values())


def test_request_books_batch_falls_back_to_individual():
    client = _make_client()
    client._submission_mode = "request"
    books = [Book("Dark Matter", "Blake Crouch")]

    login_resp = _mock_resp(200)
    metadata_resp = _mock_resp(200, {"books": [{"provider": "hardcover", "provider_id": "1"}]})
    batch_404 = _mock_resp(404)
    individual_resp = _mock_resp(200)

    post_side_effect = [login_resp, batch_404, individual_resp]
    with patch.object(client._session, "post", side_effect=post_side_effect), \
            patch.object(client._session, "get", return_value=metadata_resp):
        results = client.request_books_batch(books)

    assert results[books[0].normalized_key()] is True


def test_request_books_batch_no_metadata_skips_book():
    client = _make_client()
    book = Book("Unknown Book", "Unknown Author")

    login_resp = _mock_resp(200)
    metadata_resp = _mock_resp(200, {"books": []})

    with patch.object(client._session, "post", return_value=login_resp), \
         patch.object(client._session, "get", return_value=metadata_resp):
        results = client.request_books_batch([book])

    # absent from results — not a request failure, just skipped
    assert book.normalized_key() not in results


def test_request_books_batch_empty():
    client = _make_client()
    assert client.request_books_batch([]) == {}


# ---------------------------------------------------------------------------
# Submission mode detection
# ---------------------------------------------------------------------------

def test_fetch_submission_mode_returns_download_when_requests_disabled():
    client = _make_client()
    client._no_auth_mode = True

    policy_resp = _mock_resp(200, {"requests_enabled": False})

    with patch.object(client._session, "get", return_value=policy_resp):
        mode = client._fetch_submission_mode()

    assert mode == "download"


def test_fetch_submission_mode_returns_request_when_requests_enabled():
    client = _make_client()
    client._no_auth_mode = True

    policy_resp = _mock_resp(200, {"requests_enabled": True})

    with patch.object(client._session, "get", return_value=policy_resp):
        mode = client._fetch_submission_mode()

    assert mode == "request"


def test_fetch_submission_mode_defaults_to_request_on_error():
    client = _make_client()
    client._no_auth_mode = True

    with patch.object(
        client._session, "get", side_effect=requests.ConnectionError("unreachable")
    ):
        mode = client._fetch_submission_mode()

    assert mode == "request"


# ---------------------------------------------------------------------------
# Download mode — payload building
# ---------------------------------------------------------------------------

def test_build_download_payload_uses_source_fields():
    client = _make_client()
    book = Book("The Martian", "Andy Weir")
    metadata = {
        "source": "direct_download",
        "source_id": "abc123",
        "title": "The Martian",
        "author": "Andy Weir",
        "year": "2014",
        "format": "epub",
        "size": "1.2MB",
        "preview": "/api/covers/abc123",
        "search_mode": "direct",
    }

    payload = client._build_download_payload(book, metadata)

    assert payload["source"] == "direct_download"
    assert payload["source_id"] == "abc123"
    assert payload["year"] == "2014"
    assert payload["format"] == "epub"
    assert payload["size"] == "1.2MB"
    assert payload["preview"] == "/api/covers/abc123"
    assert payload["search_mode"] == "direct"
    assert payload["content_type"] == "ebook"
    assert "book_data" not in payload


def test_build_download_payload_falls_back_to_provider_fields():
    client = _make_client()
    book = Book("The Martian", "Andy Weir")
    metadata = {"provider": "hardcover", "provider_id": "999"}

    payload = client._build_download_payload(book, metadata)

    assert payload["source"] == "hardcover"
    assert payload["source_id"] == "999"


# ---------------------------------------------------------------------------
# Download mode — end-to-end
# ---------------------------------------------------------------------------

def test_request_book_download_mode_success():
    client = _make_client()
    client._no_auth_mode = True
    client._submission_mode = "download"
    book = Book("The Martian", "Andy Weir")

    metadata_resp = _mock_resp(200, {"books": [
        {"source": "direct_download", "source_id": "abc123",
         "title": "The Martian", "author": "Andy Weir", "format": "epub"},
    ]})
    download_resp = _mock_resp(200, {"status": "queued"})

    with patch.object(client._session, "get", return_value=metadata_resp), \
         patch.object(client._session, "post", return_value=download_resp) as mock_post:
        result = client.request_book(book)

    assert result is True
    posted_url = mock_post.call_args.args[0]
    assert posted_url.endswith("/api/releases/download")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["source"] == "direct_download"
    assert payload["source_id"] == "abc123"
    assert "book_data" not in payload


def test_request_books_batch_download_mode_skips_batch_endpoint():
    client = _make_client()
    client._no_auth_mode = True
    client._submission_mode = "download"
    books = [
        Book("The Martian", "Andy Weir"),
        Book("Dark Matter", "Blake Crouch"),
    ]

    meta1 = _mock_resp(200, {"books": [
        {"source": "direct_download", "source_id": "x1",
         "title": "The Martian", "author": "Andy Weir"},
    ]})
    meta2 = _mock_resp(200, {"books": [
        {"source": "direct_download", "source_id": "x2",
         "title": "Dark Matter", "author": "Blake Crouch"},
    ]})
    download_resp = _mock_resp(200, {"status": "queued"})

    with patch.object(client._session, "get", side_effect=[meta1, meta2]), \
         patch.object(client._session, "post", return_value=download_resp) as mock_post:
        results = client.request_books_batch(books)

    assert all(v for v in results.values())
    posted_urls = [call.args[0] for call in mock_post.call_args_list]
    assert all(u.endswith("/api/releases/download") for u in posted_urls)
    assert not any("/api/requests" in u for u in posted_urls)


# ---------------------------------------------------------------------------
# Metadata scoring
# ---------------------------------------------------------------------------

def test_search_metadata_picks_best_not_first():
    # First result is a poor match; second result is the correct book.
    # The scorer should return the second result's provider_id.
    client = _make_client()
    client._no_auth_mode = True
    client._submission_mode = "request"

    metadata_resp = _mock_resp(200, {"books": [
        {"provider": "hardcover", "provider_id": "999",
         "title": "The Martian Chronicles", "author": "Ray Bradbury"},
        {"provider": "hardcover", "provider_id": "42",
         "title": "The Martian", "author": "Andy Weir"},
    ]})
    request_resp = _mock_resp(200)

    book = Book("The Martian", "Andy Weir")
    with patch.object(client._session, "get", return_value=metadata_resp), \
         patch.object(client._session, "post", return_value=request_resp) as mock_post:
        result = client.request_book(book)

    assert result is True
    post_payload = mock_post.call_args.kwargs["json"]
    assert post_payload["book_data"]["provider_id"] == "42"


def test_search_metadata_uses_isbn_first():
    # When a book has an ISBN, the first GET should query by ISBN.
    # If the ISBN query returns a good result, no title+author query is made.
    client = _make_client()
    client._no_auth_mode = True
    client._submission_mode = "request"

    isbn_metadata_resp = _mock_resp(200, {"books": [
        {"provider": "hardcover", "provider_id": "42",
         "title": "The Martian", "author": "Andy Weir"},
    ]})
    request_resp = _mock_resp(200)

    book = Book("The Martian", "Andy Weir", isbn_10="1250364418")
    get_calls = []

    def fake_get(url, **kwargs):
        get_calls.append(kwargs.get("params", {}).get("query", ""))
        return isbn_metadata_resp

    with patch.object(client._session, "get", side_effect=fake_get), \
         patch.object(client._session, "post", return_value=request_resp):
        result = client.request_book(book)

    assert result is True
    # Only one GET — the ISBN query succeeded, no fallback needed
    assert len(get_calls) == 1
    assert get_calls[0] == "1250364418"


def test_search_metadata_falls_back_to_title_author_when_isbn_misses():
    # ISBN query returns empty; fallback title+author query succeeds.
    client = _make_client()
    client._no_auth_mode = True
    client._submission_mode = "request"

    empty_resp = _mock_resp(200, {"books": []})
    title_author_resp = _mock_resp(200, {"books": [
        {"provider": "hardcover", "provider_id": "42",
         "title": "The Martian", "author": "Andy Weir"},
    ]})
    request_resp = _mock_resp(200)

    book = Book("The Martian", "Andy Weir", isbn_10="1250364418")
    responses = [empty_resp, title_author_resp]

    with patch.object(client._session, "get", side_effect=responses), \
         patch.object(client._session, "post", return_value=request_resp):
        result = client.request_book(book)

    assert result is True


def test_search_metadata_low_score_skips_book():
    # Metadata returns a completely unrelated book — score below threshold → skip.
    client = _make_client()
    client._no_auth_mode = True
    client._submission_mode = "request"

    metadata_resp = _mock_resp(200, {"books": [
        {"provider": "hardcover", "provider_id": "1",
         "title": "Completely Different Title", "author": "Someone Else"},
    ]})

    book = Book("The Martian", "Andy Weir")
    with patch.object(client._session, "get", return_value=metadata_resp), \
         patch.object(client._session, "post") as mock_post:
        result = client.request_book(book)

    assert result is False
    mock_post.assert_not_called()
