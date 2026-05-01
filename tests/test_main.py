import os
from unittest.mock import MagicMock, patch

from main import Config, deduplicate, sync_once
from src.models import Book


def test_config_from_env_fully_set():
    env = {
        "HARDCOVER_API_KEY": "test-key",
        "GOODREADS_RSS_URL": "https://www.goodreads.com/review/list_rss/123",
        "CWA_URL": "http://cwa:8083",
        "SHELFMARK_URL": "http://shelfmark:8084",
    }
    with patch.dict(os.environ, env, clear=True):
        config = Config.from_env()

    assert config.hardcover_api_key == "test-key"
    assert config.goodreads_rss_url == "https://www.goodreads.com/review/list_rss/123"
    assert config.cwa_url == "http://cwa:8083"
    assert config.shelfmark_url == "http://shelfmark:8084"
    assert config.sync_interval_seconds == 3600
    assert config.log_level == "INFO"


def test_config_from_env_empty_returns_none_fields():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()

    assert config.hardcover_api_key is None
    assert config.goodreads_rss_url is None
    assert config.cwa_url is None
    assert config.shelfmark_url is None


def test_config_from_env_custom_values():
    env = {
        "HARDCOVER_API_KEY": "key",
        "GOODREADS_RSS_URL": "https://goodreads.com/rss",
        "CWA_URL": "http://192.168.1.10:8083",
        "CWA_USERNAME": "admin",
        "CWA_PASSWORD": "secret",
        "SHELFMARK_URL": "http://192.168.1.10:8084",
        "SYNC_INTERVAL_SECONDS": "1800",
        "LOG_LEVEL": "DEBUG",
    }
    with patch.dict(os.environ, env, clear=True):
        config = Config.from_env()

    assert config.cwa_url == "http://192.168.1.10:8083"
    assert config.cwa_username == "admin"
    assert config.sync_interval_seconds == 1800
    assert config.log_level == "DEBUG"


def test_deduplicate_by_isbn():
    books = [
        Book("Dark Matter", "Blake Crouch", isbn_13="9780553418811", source="hardcover"),
        Book("Dark Matter", "B. Crouch", isbn_13="9780553418811", source="goodreads"),
    ]
    result = deduplicate(books)
    assert len(result) == 1
    assert result[0].source == "hardcover"


def test_deduplicate_by_normalized_key():
    books = [
        Book("Project Hail Mary", "Andy Weir", source="hardcover"),
        Book("Project Hail Mary", "Andy Weir", source="goodreads"),
    ]
    result = deduplicate(books)
    assert len(result) == 1


def test_deduplicate_different_books():
    books = [
        Book("Dark Matter", "Blake Crouch"),
        Book("Project Hail Mary", "Andy Weir"),
    ]
    result = deduplicate(books)
    assert len(result) == 2


def test_deduplicate_empty():
    assert deduplicate([]) == []


def _make_config(**overrides):
    defaults = dict(
        hardcover_api_key="key",
        goodreads_rss_url="https://goodreads.com/rss",
        cwa_url="http://cwa:8083",
        cwa_username=None,
        cwa_password=None,
        shelfmark_url="http://shelfmark:8084",
        shelfmark_username=None,
        shelfmark_password=None,
        sync_interval_seconds=0,
        log_level="INFO",
    )
    return Config(**{**defaults, **overrides})


def test_sync_once_skips_owned_books():
    config = _make_config()
    book = Book("Dark Matter", "Blake Crouch")
    mock_client = MagicMock()

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.goodreads.fetch_want_to_read", return_value=[]), \
         patch("main.cwa.is_book_in_library", return_value=True):
        sync_once(config, mock_client)

    mock_client.request_books_batch.assert_not_called()


def test_sync_once_requests_missing_books():
    config = _make_config()
    book = Book("Dark Matter", "Blake Crouch")
    mock_client = MagicMock()
    mock_client.request_books_batch.return_value = {book.normalized_key(): True}

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.goodreads.fetch_want_to_read", return_value=[]), \
         patch("main.cwa.is_book_in_library", return_value=False):
        sync_once(config, mock_client)

    mock_client.request_books_batch.assert_called_once_with([book])


def test_sync_once_source_failure_continues():
    config = _make_config()
    book = Book("Project Hail Mary", "Andy Weir")
    mock_client = MagicMock()
    mock_client.request_books_batch.return_value = {book.normalized_key(): True}

    with patch("main.hardcover.fetch_want_to_read", side_effect=Exception("API down")), \
         patch("main.goodreads.fetch_want_to_read", return_value=[book]), \
         patch("main.cwa.is_book_in_library", return_value=False):
        sync_once(config, mock_client)

    # Goodreads book still processed despite Hardcover failure
    mock_client.request_books_batch.assert_called_once_with([book])


def test_sync_once_no_books():
    config = _make_config()
    mock_client = MagicMock()

    with patch("main.hardcover.fetch_want_to_read", return_value=[]), \
         patch("main.goodreads.fetch_want_to_read", return_value=[]):
        sync_once(config, mock_client)

    mock_client.request_books_batch.assert_not_called()


def test_sync_once_skips_hardcover_when_not_configured():
    config = _make_config(hardcover_api_key=None)
    book = Book("Project Hail Mary", "Andy Weir")
    mock_client = MagicMock()
    mock_client.request_books_batch.return_value = {book.normalized_key(): True}

    with patch("main.hardcover.fetch_want_to_read") as mock_hc, \
         patch("main.goodreads.fetch_want_to_read", return_value=[book]), \
         patch("main.cwa.is_book_in_library", return_value=False):
        sync_once(config, mock_client)

    mock_hc.assert_not_called()
    mock_client.request_books_batch.assert_called_once()


def test_sync_once_skips_goodreads_when_not_configured():
    config = _make_config(goodreads_rss_url=None)
    book = Book("Dark Matter", "Blake Crouch")
    mock_client = MagicMock()
    mock_client.request_books_batch.return_value = {book.normalized_key(): True}

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.goodreads.fetch_want_to_read") as mock_gr, \
         patch("main.cwa.is_book_in_library", return_value=False):
        sync_once(config, mock_client)

    mock_gr.assert_not_called()
    mock_client.request_books_batch.assert_called_once()


def test_sync_once_skips_cwa_check_when_not_configured():
    config = _make_config(cwa_url=None)
    book = Book("Dark Matter", "Blake Crouch")
    mock_client = MagicMock()
    mock_client.request_books_batch.return_value = {book.normalized_key(): True}

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.goodreads.fetch_want_to_read", return_value=[]), \
         patch("main.cwa.is_book_in_library") as mock_cwa:
        sync_once(config, mock_client)

    mock_cwa.assert_not_called()
    mock_client.request_books_batch.assert_called_once_with([book])


def test_sync_once_skips_shelfmark_when_not_configured():
    config = _make_config()
    book = Book("Dark Matter", "Blake Crouch")

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.goodreads.fetch_want_to_read", return_value=[]), \
         patch("main.cwa.is_book_in_library", return_value=False):
        sync_once(config, None)  # no shelfmark client
