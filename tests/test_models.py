from src.models import Book, _normalize


def test_normalize_basic():
    assert _normalize("Hello World") == "hello world"


def test_normalize_accents():
    assert _normalize("café") == "cafe"


def test_normalize_punctuation():
    assert _normalize("Hello, World!") == "hello world"


def test_normalize_whitespace():
    assert _normalize("  Hello   World  ") == "hello world"


def test_normalize_empty():
    assert _normalize("") == ""


def test_book_best_isbn_prefers_isbn13():
    book = Book("Title", "Author", isbn_10="1234567890", isbn_13="9781234567897")
    assert book.best_isbn() == "9781234567897"


def test_book_best_isbn_falls_back_to_isbn10():
    book = Book("Title", "Author", isbn_10="1234567890")
    assert book.best_isbn() == "1234567890"


def test_book_best_isbn_none():
    book = Book("Title", "Author")
    assert book.best_isbn() is None


def test_book_normalized_key():
    book = Book("The Martian", "Andy Weir")
    assert book.normalized_key() == "the martian andy weir"


def test_book_normalized_key_strips_accents():
    book = Book("Café au Lait", "André Rieu")
    assert book.normalized_key() == "cafe au lait andre rieu"


def test_book_normalized_key_strips_punctuation():
    book = Book("It's: A Book", "Author")
    assert book.normalized_key() == "its a book author"


def test_book_description_defaults_none():
    book = Book("Title", "Author")
    assert book.description is None


def test_book_description_can_be_set():
    book = Book("Title", "Author", description="A great book about things.")
    assert book.description == "A great book about things."


def test_book_series_fields_default_none():
    book = Book("Title", "Author")
    assert book.series is None
    assert book.series_index is None


def test_book_series_can_be_set():
    book = Book("Title", "Author", series="My Series", series_index="2")
    assert book.series == "My Series"
    assert book.series_index == "2"


def test_book_publisher_pubdate_default_none():
    book = Book("Title", "Author")
    assert book.publisher is None
    assert book.pubdate is None


def test_book_publisher_pubdate_can_be_set():
    book = Book("Title", "Author", publisher="Penguin", pubdate="2023-06-01")
    assert book.publisher == "Penguin"
    assert book.pubdate == "2023-06-01"
