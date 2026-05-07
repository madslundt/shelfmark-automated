import os
from unittest.mock import MagicMock, patch

from main import (
    Config,
    _is_read_status_sync_due,
    deduplicate,
    sync_metadata_once,
    sync_once,
    sync_read_status_once,
)
from src.cwa import CWAClient
from src.models import Book
from src.state import REASON_IMPORTED, REASON_SUBMITTED, StateManager


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
    assert config.sync_interval_min_seconds == 120
    assert config.sync_interval_max_seconds == 900
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
    assert config.sync_interval_min_seconds == 1800
    assert config.sync_interval_max_seconds == 1800
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
        sync_interval_min_seconds=0,
        sync_interval_max_seconds=0,
        state_file=None,
        full_sync_interval_seconds=0,
        read_status_sync_interval_seconds=86400,
        fix_metadata=True,
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


# ---------------------------------------------------------------------------
# force_full: imported books skipped, submitted books rechecked
# ---------------------------------------------------------------------------

def test_sync_once_force_full_skips_imported_books(tmp_path):
    """Imported books are excluded even from force_full (daily) runs."""
    config = _make_config(state_file=str(tmp_path / "state.db"))
    book = Book("Dark Matter", "Blake Crouch")

    state = StateManager(str(tmp_path / "state.db"))
    state.mark_handled(book, REASON_IMPORTED)
    state.save()

    mock_client = MagicMock()

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.goodreads.fetch_want_to_read", return_value=[]), \
         patch("main.cwa.is_book_in_library") as mock_cwa:
        sync_once(config, mock_client, state=state, force_full=True)

    mock_cwa.assert_not_called()
    mock_client.request_books_batch.assert_not_called()
    state.close()


def test_sync_once_force_full_rechecks_submitted_books(tmp_path):
    """Submitted (but not yet imported) books are re-checked in CWA during force_full."""
    config = _make_config(state_file=str(tmp_path / "state.db"))
    book = Book("Project Hail Mary", "Andy Weir")

    state = StateManager(str(tmp_path / "state.db"))
    state.mark_handled(book, REASON_SUBMITTED)
    state.save()

    mock_client = MagicMock()
    mock_client.request_books_batch.return_value = {}

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.goodreads.fetch_want_to_read", return_value=[]), \
         patch("main.cwa.is_book_in_library", return_value=False) as mock_cwa:
        sync_once(config, mock_client, state=state, force_full=True)

    mock_cwa.assert_called_once()
    state.close()


# ---------------------------------------------------------------------------
# sync_read_status_once tests
# ---------------------------------------------------------------------------

def test_sync_read_status_marks_found_book():
    config = Config(
        hardcover_api_key="key",
        goodreads_rss_url=None,
        cwa_url="http://cwa:8083",
        cwa_username="admin",
        cwa_password="secret",
        shelfmark_url=None,
        shelfmark_username=None,
        shelfmark_password=None,
        sync_interval_min_seconds=120,
        sync_interval_max_seconds=900,
        state_file=None,
        full_sync_interval_seconds=86400,
        read_status_sync_interval_seconds=86400,
        fix_metadata=True,
        log_level="INFO",
    )
    book = Book("The Martian", "Andy Weir", source="hardcover")

    mock_cwa = MagicMock(spec=CWAClient)
    mock_cwa.mark_as_read.return_value = True

    with patch("main.hardcover.fetch_read", return_value=[book]), \
         patch("main.cwa.find_book_in_library", return_value=42):
        sync_read_status_once(config, mock_cwa, state=None)

    mock_cwa.mark_as_read.assert_called_once_with(42)


def test_sync_read_status_skips_book_not_in_library():
    config = Config(
        hardcover_api_key="key",
        goodreads_rss_url=None,
        cwa_url="http://cwa:8083",
        cwa_username="admin",
        cwa_password="secret",
        shelfmark_url=None,
        shelfmark_username=None,
        shelfmark_password=None,
        sync_interval_min_seconds=120,
        sync_interval_max_seconds=900,
        state_file=None,
        full_sync_interval_seconds=86400,
        read_status_sync_interval_seconds=86400,
        fix_metadata=True,
        log_level="INFO",
    )
    book = Book("Unknown Book", "Nobody", source="hardcover")

    mock_cwa = MagicMock(spec=CWAClient)

    with patch("main.hardcover.fetch_read", return_value=[book]), \
         patch("main.cwa.find_book_in_library", return_value=None):
        sync_read_status_once(config, mock_cwa, state=None)

    mock_cwa.mark_as_read.assert_not_called()


def test_sync_read_status_skips_already_processed(tmp_path):
    from src.state import StateManager
    state = StateManager(str(tmp_path / "state.db"))
    config = Config(
        hardcover_api_key="key",
        goodreads_rss_url=None,
        cwa_url="http://cwa:8083",
        cwa_username="admin",
        cwa_password="secret",
        shelfmark_url=None,
        shelfmark_username=None,
        shelfmark_password=None,
        sync_interval_min_seconds=120,
        sync_interval_max_seconds=900,
        state_file=None,
        full_sync_interval_seconds=86400,
        read_status_sync_interval_seconds=86400,
        fix_metadata=True,
        log_level="INFO",
    )
    book = Book("The Martian", "Andy Weir", source="hardcover")
    state.mark_read_status_set(book)
    state.save()

    mock_cwa = MagicMock(spec=CWAClient)

    with patch("main.hardcover.fetch_read", return_value=[book]):
        sync_read_status_once(config, mock_cwa, state=state)

    mock_cwa.mark_as_read.assert_not_called()
    state.close()


def test_sync_read_status_no_cwa_client_exits_early():
    config = Config(
        hardcover_api_key="key",
        goodreads_rss_url=None,
        cwa_url=None,
        cwa_username=None,
        cwa_password=None,
        shelfmark_url=None,
        shelfmark_username=None,
        shelfmark_password=None,
        sync_interval_min_seconds=120,
        sync_interval_max_seconds=900,
        state_file=None,
        full_sync_interval_seconds=86400,
        read_status_sync_interval_seconds=86400,
        fix_metadata=True,
        log_level="INFO",
    )

    with patch("main.hardcover.fetch_read", return_value=[]) as mock_fetch:
        sync_read_status_once(config, cwa_client=None, state=None)

    mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Config: read_status_sync_interval_seconds
# ---------------------------------------------------------------------------

def test_config_read_status_sync_interval_default():
    with patch.dict(os.environ, {}, clear=True):
        config = Config.from_env()
    assert config.read_status_sync_interval_seconds == 86400


def test_config_read_status_sync_interval_zero_disables():
    with patch.dict(os.environ, {"READ_STATUS_SYNC_INTERVAL_SECONDS": "0"}, clear=True):
        config = Config.from_env()
    assert config.read_status_sync_interval_seconds == 0


def test_config_read_status_sync_interval_custom():
    with patch.dict(os.environ, {"READ_STATUS_SYNC_INTERVAL_SECONDS": "3600"}, clear=True):
        config = Config.from_env()
    assert config.read_status_sync_interval_seconds == 3600


# ---------------------------------------------------------------------------
# _is_read_status_sync_due
# ---------------------------------------------------------------------------

def test_is_read_status_sync_due_disabled():
    assert _is_read_status_sync_due(last=None, interval_seconds=0) is False


def test_is_read_status_sync_due_no_prior_run():
    assert _is_read_status_sync_due(last=None, interval_seconds=86400) is True


def test_is_read_status_sync_due_recent_run():
    from datetime import datetime, timedelta
    recent = datetime.now() - timedelta(hours=1)
    assert _is_read_status_sync_due(last=recent, interval_seconds=86400) is False


def test_is_read_status_sync_due_stale_run():
    from datetime import datetime, timedelta
    old = datetime.now() - timedelta(days=2)
    assert _is_read_status_sync_due(last=old, interval_seconds=86400) is True


# ---------------------------------------------------------------------------
# Config: fix_metadata
# ---------------------------------------------------------------------------

def test_config_fix_metadata_defaults_true():
    with patch.dict(os.environ, {}, clear=True):
        c = Config.from_env()
    assert c.fix_metadata is True


def test_config_fix_metadata_disabled_by_env():
    with patch.dict(os.environ, {"FIX_METADATA": "false"}):
        c = Config.from_env()
    assert c.fix_metadata is False


def test_config_fix_metadata_disabled_by_zero():
    with patch.dict(os.environ, {"FIX_METADATA": "0"}):
        c = Config.from_env()
    assert c.fix_metadata is False


# ---------------------------------------------------------------------------
# sync_metadata_once
# ---------------------------------------------------------------------------

def test_sync_metadata_once_skips_when_cwa_not_configured():
    config = _make_config(cwa_url=None, fix_metadata=True)
    sync_metadata_once(config, None)  # must not raise


def test_sync_metadata_once_skips_when_disabled():
    config = _make_config(fix_metadata=False)
    client = MagicMock()
    with patch("main.cwa.find_mismatched_author") as mock_find:
        sync_metadata_once(config, client)
    mock_find.assert_not_called()


def test_sync_metadata_once_calls_update_for_mismatch():
    config = _make_config(fix_metadata=True, hardcover_api_key="key", goodreads_rss_url=None)
    client = MagicMock()
    book = Book("All The Lies", "Nicola Sanders")

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.hardcover.fetch_read", return_value=[]), \
         patch("main.cwa.find_mismatched_author", return_value=(42, "jennifer harvey")):
        sync_metadata_once(config, client)

    client.update_book_author.assert_called_once_with(42, "Nicola Sanders")


def test_sync_metadata_once_skips_no_mismatch():
    config = _make_config(fix_metadata=True, hardcover_api_key="key", goodreads_rss_url=None)
    client = MagicMock()
    book = Book("Dark Matter", "Blake Crouch")

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.hardcover.fetch_read", return_value=[]), \
         patch("main.cwa.find_mismatched_author", return_value=None):
        sync_metadata_once(config, client)

    client.update_book_author.assert_not_called()
