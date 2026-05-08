from unittest.mock import MagicMock, patch

import pytest
import requests

from src.hardcover import fetch_read, fetch_want_to_read


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


# ---------------------------------------------------------------------------
# New metadata fields: description, series, publisher, pubdate
# ---------------------------------------------------------------------------

def _full_book_payload(*, title, author, description=None, release_date=None,
                        isbn_10=None, isbn_13=None, publisher_name=None,
                        series_name=None, series_position=None, book_id=1,
                        tags=None, community_rating=None, ratings_count=None):
    """Build a Hardcover GraphQL response for a single book with all new fields."""
    edition = {"isbn_10": isbn_10, "isbn_13": isbn_13}
    if publisher_name is not None:
        edition["publisher"] = {"name": publisher_name}
    else:
        edition["publisher"] = None

    book_series = []
    if series_name is not None:
        book_series = [{"series": {"name": series_name}, "position": series_position}]

    taggings = [{"tag": {"tag": t}} for t in (tags or [])]

    return {"data": {"me": [{"user_books": [{
        "book": {
            "id": book_id,
            "title": title,
            "description": description,
            "release_date": release_date,
            "contributions": [{"author": {"name": author}}],
            "default_physical_edition": edition,
            "book_series": book_series,
            "taggings": taggings,
            "community_rating": community_rating,
            "ratings_count": ratings_count,
        }
    }]}]}}


def test_fetch_want_to_read_populates_description():
    data = _full_book_payload(
        title="Dark Matter", author="Blake Crouch",
        description="A physicist is kidnapped into a parallel universe.",
    )
    mock_resp = _mock_session_post(data)
    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("key")
    assert books[0].description == "A physicist is kidnapped into a parallel universe."


def test_fetch_want_to_read_populates_pubdate():
    data = _full_book_payload(
        title="Dark Matter", author="Blake Crouch", release_date="2016-07-26",
    )
    mock_resp = _mock_session_post(data)
    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("key")
    assert books[0].pubdate == "2016-07-26"


def test_fetch_want_to_read_populates_series():
    data = _full_book_payload(
        title="The Name of the Wind", author="Patrick Rothfuss",
        series_name="The Kingkiller Chronicle", series_position=1,
    )
    mock_resp = _mock_session_post(data)
    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("key")
    assert books[0].series == "The Kingkiller Chronicle"
    assert books[0].series_index == "1"


def test_fetch_want_to_read_populates_publisher():
    data = _full_book_payload(
        title="Project Hail Mary", author="Andy Weir", publisher_name="Ballantine Books",
    )
    mock_resp = _mock_session_post(data)
    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("key")
    assert books[0].publisher == "Ballantine Books"


def test_fetch_want_to_read_null_description_and_series_give_none():
    data = _full_book_payload(title="Some Book", author="Some Author")
    mock_resp = _mock_session_post(data)
    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("key")
    assert books[0].description is None
    assert books[0].series is None
    assert books[0].series_index is None
    assert books[0].publisher is None
    assert books[0].pubdate is None


def test_fetch_read_populates_description():
    data = _full_book_payload(
        title="The Martian", author="Andy Weir",
        description="An astronaut is stranded on Mars.",
    )
    mock_resp = _mock_session_post(data)
    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_read("key")
    assert books[0].description == "An astronaut is stranded on Mars."


# ---------------------------------------------------------------------------
# New metadata fields: tags, community_rating, ratings_count
# ---------------------------------------------------------------------------

def test_fetch_want_to_read_populates_tags():
    data = _full_book_payload(
        title="Dark Matter", author="Blake Crouch",
        tags=["Fiction", "Science Fiction", "Thriller"],
    )
    mock_resp = _mock_session_post(data)
    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("key")
    assert books[0].tags == ["Fiction", "Science Fiction", "Thriller"]


def test_fetch_want_to_read_no_tags_gives_empty_list():
    data = _full_book_payload(title="Dark Matter", author="Blake Crouch")
    mock_resp = _mock_session_post(data)
    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("key")
    assert books[0].tags == []


def test_fetch_want_to_read_populates_community_rating():
    data = _full_book_payload(
        title="Dark Matter", author="Blake Crouch",
        community_rating=4.2, ratings_count=15000,
    )
    mock_resp = _mock_session_post(data)
    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("key")
    assert books[0].community_rating == 4.2
    assert books[0].ratings_count == 15000


def test_fetch_want_to_read_null_rating_gives_none():
    data = _full_book_payload(title="Dark Matter", author="Blake Crouch")
    mock_resp = _mock_session_post(data)
    with patch("src.hardcover.requests.Session") as mock_session_cls:
        mock_session_cls.return_value.post.return_value = mock_resp
        mock_session_cls.return_value.headers = MagicMock()
        books = fetch_want_to_read("key")
    assert books[0].community_rating is None
    assert books[0].ratings_count is None
