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


def test_request_books_batch_success():
    client = _make_client()
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


def test_request_books_batch_empty():
    client = _make_client()
    assert client.request_books_batch([]) == {}
