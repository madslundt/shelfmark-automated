"""Book domain model shared across all integration modules."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


def _normalize(text: str) -> str:
    """Lowercase, remove accents, strip punctuation, collapse whitespace."""
    text = text.lower()
    # Decompose accented characters (é → e + combining accent)
    text = unicodedata.normalize("NFKD", text)
    # Drop combining characters (the accent marks)
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Strip punctuation and non-word characters (keep spaces)
    text = re.sub(r"[^\w\s]", "", text)
    # Collapse multiple whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


@dataclass
class Book:
    title: str
    author: str
    isbn_10: str | None = None
    isbn_13: str | None = None
    source: str = ""            # "hardcover" | "goodreads"
    source_id: str = ""         # Hardcover book ID or Goodreads book_id
    description: str | None = None
    series: str | None = None
    series_index: str | None = None
    publisher: str | None = None
    pubdate: str | None = None
    tags: list[str] = field(default_factory=list)
    community_rating: float | None = None
    ratings_count: int | None = None

    def best_isbn(self) -> str | None:
        """Return isbn_13 if available, then isbn_10, then None."""
        return self.isbn_13 or self.isbn_10 or None

    def normalized_key(self) -> str:
        """Lowercase, accent-stripped, punctuation-free 'title author' string for deduplication."""
        return _normalize(f"{self.title} {self.author}")

    def __repr__(self) -> str:
        isbn = self.best_isbn()
        isbn_part = f" isbn={isbn}" if isbn else ""
        return f"Book({self.title!r} by {self.author!r}{isbn_part} [{self.source}])"
