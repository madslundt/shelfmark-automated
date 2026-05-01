from unittest.mock import MagicMock, patch

import pytest
import requests

from src.hardcover import fetch_want_to_read


def _mock_session_post(json_data, status_code=200):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _want_to_read_payload(*books):
    user_books = []
    for b in books:
        user_books.append({
            "book": {
                "id": b.get("id", 1),
                "title": b["title"],
                "contributions": [{"author": {"name": b.get("author", "")}}],
                "default_physical_edition": b.get("edition"),
            }
        })
    return {"data": {"me": [{"user_books": user_books}]}}


def test_fetch_want_to_read_success():
    data = _want_to_read_payload({
        "id": 1,
        "title": "Dark Matter",
        "author": "Blake Crouch",
        "edition": {"isbn_10": "0553418815", "isbn_13": "9780553418811"},
    })
    mock_resp = _mock_session_post(data)

    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("test-api-key")

    assert len(books) == 1
    assert books[0].title == "Dark Matter"
    assert books[0].author == "Blake Crouch"
    assert books[0].isbn_10 == "0553418815"
    assert books[0].isbn_13 == "9780553418811"
    assert books[0].source == "hardcover"


def test_fetch_want_to_read_no_edition():
    data = _want_to_read_payload({
        "id": 2,
        "title": "Project Hail Mary",
        "author": "Andy Weir",
        "edition": None,
    })
    mock_resp = _mock_session_post(data)

    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("test-api-key")

    assert len(books) == 1
    assert books[0].isbn_10 is None
    assert books[0].isbn_13 is None


def test_fetch_want_to_read_empty_me():
    data = {"data": {"me": []}}
    mock_resp = _mock_session_post(data)

    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("test-api-key")

    assert books == []


def test_fetch_want_to_read_skips_empty_title():
    data = _want_to_read_payload(
        {"id": 1, "title": "Real Book", "author": "Author A", "edition": None},
        {"id": 2, "title": "", "author": "Author B", "edition": None},
    )
    mock_resp = _mock_session_post(data)

    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("test-api-key")

    assert len(books) == 1
    assert books[0].title == "Real Book"


def test_fetch_want_to_read_raises_on_401():
    mock_resp = _mock_session_post({}, status_code=401)

    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        with pytest.raises(requests.HTTPError):
            fetch_want_to_read("bad-key")


def test_fetch_want_to_read_graphql_errors_logged(caplog):
    data = {
        "data": {"me": [{"user_books": []}]},
        "errors": [{"message": "Some GraphQL warning"}],
    }
    mock_resp = _mock_session_post(data)

    import logging
    with patch("src.hardcover.requests.Session") as mock_session_cls, \
         caplog.at_level(logging.WARNING, logger="src.hardcover"):
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("test-api-key")

    assert books == []
    assert "GraphQL error" in caplog.text
