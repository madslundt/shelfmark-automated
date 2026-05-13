"""Tests for src/openlibrary.py"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.openlibrary import _parse_ol_response, fetch_by_isbn


# ---------------------------------------------------------------------------
# _parse_ol_response — unit tests (no HTTP)
# ---------------------------------------------------------------------------

def _make_ol_data(
    isbn,
    title="Dark Matter",
    authors=None,
    publishers=None,
    publish_date="July 26, 2016",
):
    entry: dict = {"title": title, "publish_date": publish_date}
    if authors is not None:
        entry["authors"] = authors
    if publishers is not None:
        entry["publishers"] = publishers
    return {f"ISBN:{isbn}": entry}


_ISBN = "9780553418811"


def test_parse_ol_response_full_entry():
    data = _make_ol_data(
        _ISBN,
        title="Dark Matter",
        authors=[{"name": "Blake Crouch", "url": "..."}],
        publishers=[{"name": "Crown Publishers"}],
        publish_date="July 26, 2016",
    )
    result = _parse_ol_response(data, _ISBN)
    assert result is not None
    assert result["title"] == "Dark Matter"
    assert result["author"] == "Blake Crouch"
    assert result["publisher"] == "Crown Publishers"
    assert result["pubdate"] == "July 26, 2016"


def test_parse_ol_response_missing_isbn_key():
    result = _parse_ol_response({}, _ISBN)
    assert result is None


def test_parse_ol_response_no_authors():
    data = _make_ol_data(_ISBN, authors=[])
    result = _parse_ol_response(data, _ISBN)
    assert result is not None
    assert result["author"] is None


def test_parse_ol_response_no_publishers():
    data = _make_ol_data(_ISBN, authors=[{"name": "A"}], publishers=[])
    result = _parse_ol_response(data, _ISBN)
    assert result["publisher"] is None


def test_parse_ol_response_empty_strings_become_none():
    data = {f"ISBN:{_ISBN}": {"title": "  ", "authors": [], "publishers": [], "publish_date": ""}}
    result = _parse_ol_response(data, _ISBN)
    assert result["title"] is None
    assert result["pubdate"] is None


def test_parse_ol_response_uses_first_author_only():
    data = _make_ol_data(_ISBN, authors=[{"name": "First Author"}, {"name": "Second Author"}])
    result = _parse_ol_response(data, _ISBN)
    assert result["author"] == "First Author"


def test_parse_ol_response_uses_first_publisher_only():
    data = _make_ol_data(_ISBN, publishers=[{"name": "Publisher A"}, {"name": "Publisher B"}])
    result = _parse_ol_response(data, _ISBN)
    assert result["publisher"] == "Publisher A"


def test_parse_ol_response_none_fields_handled():
    data = {f"ISBN:{_ISBN}": {"title": None, "authors": [{"name": None}], "publishers": [{"name": None}], "publish_date": None}}
    result = _parse_ol_response(data, _ISBN)
    assert result["title"] is None
    assert result["author"] is None
    assert result["publisher"] is None
    assert result["pubdate"] is None


# ---------------------------------------------------------------------------
# fetch_by_isbn — tests with mocked HTTP
# ---------------------------------------------------------------------------

def _mock_response(json_data, status_code=200):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.ok = status_code < 400
    mock_resp.json.return_value = json_data
    return mock_resp


def test_fetch_by_isbn_success():
    data = _make_ol_data(
        _ISBN,
        title="Dark Matter",
        authors=[{"name": "Blake Crouch"}],
        publishers=[{"name": "Crown"}],
        publish_date="2016",
    )
    with patch("src.openlibrary._session") as mock_session:
        mock_session.get.return_value = _mock_response(data)
        result = fetch_by_isbn(_ISBN)

    assert result is not None
    assert result["title"] == "Dark Matter"
    assert result["author"] == "Blake Crouch"


def test_fetch_by_isbn_no_record_returns_none():
    with patch("src.openlibrary._session") as mock_session:
        mock_session.get.return_value = _mock_response({})
        result = fetch_by_isbn("9780000000000")
    assert result is None


def test_fetch_by_isbn_http_error_returns_none():
    with patch("src.openlibrary._session") as mock_session:
        mock_session.get.return_value = _mock_response({}, status_code=503)
        result = fetch_by_isbn(_ISBN)
    assert result is None


def test_fetch_by_isbn_connection_error_returns_none():
    with patch("src.openlibrary._session") as mock_session:
        mock_session.get.side_effect = requests.ConnectionError("unreachable")
        result = fetch_by_isbn(_ISBN)
    assert result is None


def test_fetch_by_isbn_timeout_returns_none():
    with patch("src.openlibrary._session") as mock_session:
        mock_session.get.side_effect = requests.Timeout("timed out")
        result = fetch_by_isbn(_ISBN)
    assert result is None


def test_fetch_by_isbn_uses_correct_params():
    with patch("src.openlibrary._session") as mock_session:
        mock_session.get.return_value = _mock_response({})
        fetch_by_isbn(_ISBN)

    call_kwargs = mock_session.get.call_args
    params = call_kwargs.kwargs.get("params", {})
    assert params.get("bibkeys") == f"ISBN:{_ISBN}"
    assert params.get("format") == "json"
    assert params.get("jscmd") == "data"


def test_fetch_by_isbn_uses_injected_session():
    custom_session = MagicMock()
    custom_session.get.return_value = _mock_response({})

    with patch("src.openlibrary._session") as mock_default:
        fetch_by_isbn(_ISBN, session=custom_session)

    custom_session.get.assert_called_once()
    mock_default.get.assert_not_called()
