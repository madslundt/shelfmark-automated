import os
from unittest.mock import MagicMock, patch

from main import (
    Config,
    _build_metadata_updates,
    _build_new_identifiers,
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
        sync_on_start=False,
        log_level="INFO",
        dry_run=False,
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
        sync_on_start=False,
        log_level="INFO",
        dry_run=False,
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
        sync_on_start=False,
        log_level="INFO",
        dry_run=False,
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
        sync_on_start=False,
        log_level="INFO",
        dry_run=False,
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
        sync_on_start=False,
        log_level="INFO",
        dry_run=False,
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

_EMPTY_CWA_META = {
    "title": "A Book", "authors": "blake crouch", "series": "", "series_index": "1",
    "comments": "", "publisher": "", "pubdate": "", "tags": "", "languages": "", "rating": "0",
    "identifier_types": "",
}


def test_sync_metadata_once_skips_when_cwa_not_configured():
    config = _make_config(cwa_url=None, fix_metadata=True)
    sync_metadata_once(config, None)  # must not raise


def test_sync_metadata_once_skips_when_disabled():
    config = _make_config(fix_metadata=False)
    client = MagicMock()
    with patch("main.cwa.find_book_in_library") as mock_find:
        sync_metadata_once(config, client)
    mock_find.assert_not_called()


def test_sync_metadata_once_calls_update_for_author_mismatch():
    config = _make_config(fix_metadata=True, hardcover_api_key="key", goodreads_rss_url=None)
    client = MagicMock()
    book = Book("All The Lies", "Nicola Sanders")

    cwa_meta = {**_EMPTY_CWA_META, "authors": "jennifer harvey", "title": "All The Lies"}
    client.get_book_metadata.return_value = cwa_meta
    client.update_book_metadata.return_value = True

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.hardcover.fetch_read", return_value=[]), \
         patch("main.cwa.find_book_in_library", return_value=42):
        sync_metadata_once(config, client)

    client.update_book_metadata.assert_called_once_with(42, {"authors": "Nicola Sanders"})


def test_sync_metadata_once_skips_when_wrong_author_is_known_correct():
    """If CWA's stored author is a known correct author for another book, skip the update.

    This guards against CWA books with wrong title metadata: the sync finds a title
    match to a different book from our list, but the stored author is actually correct
    for the real book — updating it would make metadata worse.
    """
    config = _make_config(fix_metadata=True, hardcover_api_key="key", goodreads_rss_url=None)
    client = MagicMock()
    book_housemaid = Book("The Housemaid", "Freida McFadden")
    book_winning = Book("Winning Without Losing", "Martin Bjergegaard")

    def fake_find_lib(book, *args, **kwargs):
        if book.title == "Winning Without Losing":
            return 99
        return None

    meta = {**_EMPTY_CWA_META, "authors": "freida mcfadden", "title": "Winning Without Losing"}
    client.get_book_metadata.return_value = meta

    with patch("main.hardcover.fetch_want_to_read", return_value=[book_housemaid, book_winning]), \
         patch("main.hardcover.fetch_read", return_value=[]), \
         patch("main.cwa.find_book_in_library", side_effect=fake_find_lib):
        sync_metadata_once(config, client)

    # Author update skipped (freida mcfadden is a known correct author), no other fields differ
    client.update_book_metadata.assert_not_called()


def test_sync_metadata_once_skips_when_book_not_in_library():
    config = _make_config(fix_metadata=True, hardcover_api_key="key", goodreads_rss_url=None)
    client = MagicMock()
    book = Book("Dark Matter", "Blake Crouch")

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.hardcover.fetch_read", return_value=[]), \
         patch("main.cwa.find_book_in_library", return_value=None):
        sync_metadata_once(config, client)

    client.update_book_metadata.assert_not_called()


def test_sync_metadata_once_fills_empty_description():
    config = _make_config(fix_metadata=True, hardcover_api_key="key", goodreads_rss_url=None)
    client = MagicMock()
    book = Book("Dark Matter", "Blake Crouch", description="A mind-bending thriller.")

    cwa_meta = {**_EMPTY_CWA_META, "title": "Dark Matter"}
    client.get_book_metadata.return_value = cwa_meta
    client.update_book_metadata.return_value = True

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.hardcover.fetch_read", return_value=[]), \
         patch("main.cwa.find_book_in_library", return_value=7):
        sync_metadata_once(config, client)

    client.update_book_metadata.assert_called_once_with(7, {"comments": "A mind-bending thriller."})


def test_sync_metadata_once_retries_failed_updates():
    """Failed updates are retried up to 3 times."""
    config = _make_config(fix_metadata=True, hardcover_api_key="key", goodreads_rss_url=None)
    client = MagicMock()
    book = Book("All The Lies", "Nicola Sanders")

    cwa_meta = {**_EMPTY_CWA_META, "authors": "jennifer harvey", "title": "All The Lies"}
    client.get_book_metadata.return_value = cwa_meta
    client.update_book_metadata.side_effect = [False, False, True]

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.hardcover.fetch_read", return_value=[]), \
         patch("main.cwa.find_book_in_library", return_value=42), \
         patch("main.time.sleep"):
        sync_metadata_once(config, client)

    assert client.update_book_metadata.call_count == 3


def test_sync_metadata_once_gives_up_after_three_retries():
    """After 3 retries the book is logged as permanently failed."""
    config = _make_config(fix_metadata=True, hardcover_api_key="key", goodreads_rss_url=None)
    client = MagicMock()
    book = Book("All The Lies", "Nicola Sanders")

    cwa_meta = {**_EMPTY_CWA_META, "authors": "jennifer harvey", "title": "All The Lies"}
    client.get_book_metadata.return_value = cwa_meta
    client.update_book_metadata.return_value = False

    with patch("main.hardcover.fetch_want_to_read", return_value=[book]), \
         patch("main.hardcover.fetch_read", return_value=[]), \
         patch("main.cwa.find_book_in_library", return_value=42), \
         patch("main.time.sleep"):
        sync_metadata_once(config, client)

    # 1 initial attempt + 3 retries = 4 total calls
    assert client.update_book_metadata.call_count == 4


# ---------------------------------------------------------------------------
# _build_metadata_updates
# ---------------------------------------------------------------------------

def test_build_metadata_updates_empty_when_nothing_to_change():
    book = Book("A Book", "Blake Crouch")
    updates = _build_metadata_updates(book, _EMPTY_CWA_META)
    assert updates == {}


def test_build_metadata_updates_author_when_different():
    meta = {**_EMPTY_CWA_META, "authors": "wrong author"}
    book = Book("A Book", "Right Author")
    updates = _build_metadata_updates(book, meta)
    assert updates.get("authors") == "Right Author"


def test_build_metadata_updates_fills_empty_description():
    book = Book("A Book", "Blake Crouch", description="Great description.")
    updates = _build_metadata_updates(book, _EMPTY_CWA_META)
    assert updates.get("comments") == "Great description."


def test_build_metadata_updates_skips_description_if_cwa_has_one():
    meta = {**_EMPTY_CWA_META, "comments": "Existing CWA description."}
    book = Book("A Book", "Blake Crouch", description="Different description.")
    updates = _build_metadata_updates(book, meta)
    assert "comments" not in updates


def test_build_metadata_updates_fills_empty_series():
    book = Book("A Book", "Blake Crouch", series="My Series", series_index="2")
    updates = _build_metadata_updates(book, _EMPTY_CWA_META)
    assert updates.get("series") == "My Series"
    assert updates.get("series_index") == "2"


def test_build_metadata_updates_skips_series_if_cwa_has_one():
    meta = {**_EMPTY_CWA_META, "series": "Existing Series"}
    book = Book("A Book", "Blake Crouch", series="New Series", series_index="1")
    updates = _build_metadata_updates(book, meta)
    assert "series" not in updates


def test_build_metadata_updates_fills_empty_publisher():
    book = Book("A Book", "Blake Crouch", publisher="Penguin")
    updates = _build_metadata_updates(book, _EMPTY_CWA_META)
    assert updates.get("publisher") == "Penguin"


def test_build_metadata_updates_skips_publisher_if_cwa_has_one():
    meta = {**_EMPTY_CWA_META, "publisher": "Existing Publisher"}
    book = Book("A Book", "Blake Crouch", publisher="New Publisher")
    updates = _build_metadata_updates(book, meta)
    assert "publisher" not in updates


def test_build_metadata_updates_fills_empty_pubdate():
    book = Book("A Book", "Blake Crouch", pubdate="2023-01-01")
    updates = _build_metadata_updates(book, _EMPTY_CWA_META)
    assert updates.get("pubdate") == "2023-01-01"


def test_build_metadata_updates_skips_pubdate_if_cwa_has_one():
    meta = {**_EMPTY_CWA_META, "pubdate": "2020-05-01"}
    book = Book("A Book", "Blake Crouch", pubdate="2023-01-01")
    updates = _build_metadata_updates(book, meta)
    assert "pubdate" not in updates


def test_build_metadata_updates_fills_tags_when_empty():
    book = Book("A Book", "Blake Crouch", tags=["Fiction", "Thriller"])
    updates = _build_metadata_updates(book, _EMPTY_CWA_META)
    assert updates.get("tags") == "Fiction, Thriller"


def test_build_metadata_updates_skips_tags_if_cwa_has_some():
    meta = {**_EMPTY_CWA_META, "tags": "Existing Tag"}
    book = Book("A Book", "Blake Crouch", tags=["Fiction"])
    updates = _build_metadata_updates(book, meta)
    assert "tags" not in updates


def test_build_metadata_updates_fills_rating_with_enough_reviews():
    book = Book("A Book", "Blake Crouch", community_rating=4.0, ratings_count=100)
    updates = _build_metadata_updates(book, _EMPTY_CWA_META)
    assert updates.get("rating") == "8"  # 4.0 * 2 = 8, Calibre scale 0-10


def test_build_metadata_updates_skips_rating_when_too_few_reviews():
    book = Book("A Book", "Blake Crouch", community_rating=4.0, ratings_count=5)
    updates = _build_metadata_updates(book, _EMPTY_CWA_META)
    assert "rating" not in updates


def test_build_metadata_updates_skips_rating_if_cwa_has_one():
    meta = {**_EMPTY_CWA_META, "rating": "6"}
    book = Book("A Book", "Blake Crouch", community_rating=4.0, ratings_count=100)
    updates = _build_metadata_updates(book, meta)
    assert "rating" not in updates


# ---------------------------------------------------------------------------
# _build_new_identifiers
# ---------------------------------------------------------------------------

def test_build_new_identifiers_adds_isbn13_when_missing():
    book = Book("A Book", "Author", isbn_13="9781234567890")
    result = _build_new_identifiers(book, _EMPTY_CWA_META)
    assert result.get("isbn") == "9781234567890"


def test_build_new_identifiers_falls_back_to_isbn10():
    book = Book("A Book", "Author", isbn_10="1234567890")
    result = _build_new_identifiers(book, _EMPTY_CWA_META)
    assert result.get("isbn") == "1234567890"


def test_build_new_identifiers_skips_isbn_if_already_in_cwa():
    book = Book("A Book", "Author", isbn_13="9781234567890")
    meta = {**_EMPTY_CWA_META, "identifier_types": "isbn,goodreads"}
    result = _build_new_identifiers(book, meta)
    assert "isbn" not in result


def test_build_new_identifiers_adds_goodreads_source_id():
    book = Book("A Book", "Author", source="goodreads", source_id="12345")
    result = _build_new_identifiers(book, _EMPTY_CWA_META)
    assert result.get("goodreads") == "12345"


def test_build_new_identifiers_adds_hardcover_source_id():
    book = Book("A Book", "Author", source="hardcover", source_id="hc-99")
    result = _build_new_identifiers(book, _EMPTY_CWA_META)
    assert result.get("hardcover") == "hc-99"


def test_build_new_identifiers_skips_source_id_if_already_in_cwa():
    book = Book("A Book", "Author", source="goodreads", source_id="12345")
    meta = {**_EMPTY_CWA_META, "identifier_types": "goodreads"}
    result = _build_new_identifiers(book, meta)
    assert "goodreads" not in result


def test_build_new_identifiers_empty_when_no_identifiers():
    book = Book("A Book", "Author")
    result = _build_new_identifiers(book, _EMPTY_CWA_META)
    assert result == {}


# ---------------------------------------------------------------------------
# Config: sync_on_start
# ---------------------------------------------------------------------------

def test_config_sync_on_start_defaults_false():
    with patch.dict(os.environ, {}, clear=True):
        c = Config.from_env()
    assert c.sync_on_start is False


def test_config_sync_on_start_enabled_by_env():
    with patch.dict(os.environ, {"SYNC_ON_START": "true"}):
        c = Config.from_env()
    assert c.sync_on_start is True


def test_config_sync_on_start_disabled_explicitly():
    with patch.dict(os.environ, {"SYNC_ON_START": "false"}):
        c = Config.from_env()
    assert c.sync_on_start is False


def test_sync_on_start_runs_read_status_and_metadata(tmp_path):
    """SYNC_ON_START=true triggers both syncs before the main loop."""
    from main import main
    config_env = {
        "SYNC_ON_START": "true",
        "SYNC_INTERVAL_SECONDS": "0",
        "READ_STATUS_SYNC_INTERVAL_SECONDS": "0",  # disabled in loop
        "CWA_URL": "http://cwa:8083",
        "CWA_USERNAME": "admin",
        "CWA_PASSWORD": "secret",
        "HARDCOVER_API_KEY": "key",
    }
    with patch.dict(os.environ, config_env, clear=True), \
         patch("main.sync_read_status_once") as mock_rs, \
         patch("main.sync_metadata_once") as mock_meta, \
         patch("main.sync_once"), \
         patch("main.CWAClient"):
        main()

    mock_rs.assert_called_once()
    mock_meta.assert_called_once()


def test_sync_on_start_false_does_not_run_early(tmp_path):
    """SYNC_ON_START not set → no early sync run."""
    from main import main
    config_env = {
        "SYNC_INTERVAL_SECONDS": "0",
        "READ_STATUS_SYNC_INTERVAL_SECONDS": "0",
        "CWA_URL": "http://cwa:8083",
        "CWA_USERNAME": "admin",
        "CWA_PASSWORD": "secret",
        "HARDCOVER_API_KEY": "key",
    }
    with patch.dict(os.environ, config_env, clear=True), \
         patch("main.sync_read_status_once") as mock_rs, \
         patch("main.sync_metadata_once") as mock_meta, \
         patch("main.sync_once"), \
         patch("main.CWAClient"):
        main()

    mock_rs.assert_not_called()
    mock_meta.assert_not_called()
