#!/usr/bin/env python3
"""Fix metadata for specific books in CWA by looking them up in Open Library.

Usage:
    python scripts/fix_book_metadata.py --book-ids 702 701 700 699 698
    python scripts/fix_book_metadata.py --book-ids 702 701 700 699 698 --dry-run

For each book ID, fetches current metadata from the CWA admin page (including
the ISBN stored in Calibre identifiers), looks up the canonical metadata from
Open Library, shows a diff of what would change, and applies the updates.

Requires CWA_URL, CWA_USERNAME, CWA_PASSWORD environment variables.
"""

from __future__ import annotations

import argparse
import html as _html
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src import openlibrary
from src.cwa import CWAAuthError, CWAClient
from src.models import _normalize


def _extract_isbn_from_html(page_html: str) -> str | None:
    """Extract the ISBN identifier value from CWA admin page HTML."""
    pairs: dict[str, str] = {}
    for m in re.finditer(
        r'name="(identifier-(?:type|val)-(\d+))"[^>]*value="([^"]*)"', page_html
    ):
        pairs[m.group(1)] = _html.unescape(m.group(3))

    for i in range(20):
        id_type = pairs.get(f"identifier-type-{i}", "").lower().strip()
        id_val = pairs.get(f"identifier-val-{i}", "").strip()
        if id_type == "isbn" and id_val:
            return id_val
    return None


def fix_book_metadata(
    book_ids: list[int],
    cwa_client: CWAClient,
    *,
    dry_run: bool = False,
) -> int:
    """Fix metadata for the given CWA book IDs. Returns count of books updated."""
    import requests as _requests

    ol_session = _requests.Session()
    changed = 0

    for i, book_id in enumerate(book_ids):
        print(f"\n[{book_id}] Fetching CWA metadata...")

        page_html = cwa_client.get_book_admin_html(book_id)
        if page_html is None:
            print(f"  ERROR: could not fetch admin page — skipping")
            continue

        _, cwa_meta = cwa_client._parse_admin_page(page_html)
        isbn = _extract_isbn_from_html(page_html)

        print(f"  Title:  {cwa_meta.get('title', '')!r}")
        print(f"  Author: {cwa_meta.get('authors', '')!r}")
        print(f"  ISBN:   {isbn or '(none)'}")

        if not isbn:
            print("  SKIP: no ISBN in CWA identifiers — cannot look up Open Library")
            continue

        if i > 0:
            time.sleep(1.0)

        print(f"  Looking up ISBN {isbn} in Open Library...")
        ol_data = openlibrary.fetch_by_isbn(isbn, session=ol_session)

        if ol_data is None:
            print("  SKIP: Open Library has no record for this ISBN")
            continue

        updates: dict[str, str] = {}

        ol_title = ol_data.get("title")
        cwa_title = cwa_meta.get("title", "")
        if ol_title and _normalize(ol_title) != _normalize(cwa_title):
            updates["title"] = ol_title
            print(f"  title:  {cwa_title!r}  →  {ol_title!r}")

        ol_author = ol_data.get("author")
        cwa_author = cwa_meta.get("authors", "")
        if ol_author and _normalize(ol_author) != _normalize(cwa_author):
            updates["authors"] = ol_author
            print(f"  author: {cwa_author!r}  →  {ol_author!r}")

        ol_publisher = ol_data.get("publisher")
        cwa_publisher = cwa_meta.get("publisher", "").strip()
        if ol_publisher and not cwa_publisher:
            updates["publisher"] = ol_publisher
            print(f"  publisher: (empty)  →  {ol_publisher!r}")

        ol_pubdate = ol_data.get("pubdate")
        cwa_pubdate = cwa_meta.get("pubdate", "").strip()
        if ol_pubdate and not cwa_pubdate:
            updates["pubdate"] = ol_pubdate
            print(f"  pubdate: (empty)  →  {ol_pubdate!r}")

        if not updates:
            print("  OK: metadata already matches Open Library — nothing to change")
            continue

        if dry_run:
            print(f"  [dry-run] would update: {list(updates.keys())}")
            changed += 1
            continue

        ok = cwa_client.update_book_metadata(book_id, updates)
        if ok:
            print(f"  UPDATED: {list(updates.keys())}")
            changed += 1
        else:
            print("  ERROR: update failed")

    return changed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix metadata for specific CWA books via Open Library ISBN lookup.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--book-ids",
        nargs="+",
        type=int,
        required=True,
        metavar="ID",
        help="Calibre book IDs to fix (e.g. --book-ids 702 701 700)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing anything to CWA",
    )
    args = parser.parse_args()

    cwa_url = os.environ.get("CWA_URL", "").strip()
    cwa_username = os.environ.get("CWA_USERNAME", "").strip()
    cwa_password = os.environ.get("CWA_PASSWORD", "").strip()

    if not cwa_url or not cwa_username or not cwa_password:
        print(
            "ERROR: CWA_URL, CWA_USERNAME, and CWA_PASSWORD must be set",
            file=sys.stderr,
        )
        sys.exit(1)

    cwa_client = CWAClient(cwa_url, cwa_username, cwa_password)
    try:
        cwa_client._login()
    except CWAAuthError as exc:
        print(f"ERROR: CWA login failed — {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print("[dry-run] No changes will be written to CWA\n")

    changed = fix_book_metadata(args.book_ids, cwa_client, dry_run=args.dry_run)
    noun = "book" if changed == 1 else "books"
    suffix = " would be" if args.dry_run else ""
    print(f"\nDone: {changed} {noun}{suffix} updated")


if __name__ == "__main__":
    main()
