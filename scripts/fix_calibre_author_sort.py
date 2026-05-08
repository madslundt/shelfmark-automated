#!/usr/bin/env python3
"""
Fix Calibre author sort names.

Usage:
    python scripts/fix_calibre_author_sort.py /path/to/calibre/library
    python scripts/fix_calibre_author_sort.py /path/to/calibre/library --dry-run

Fixes two related issues in the Calibre SQLite database:

1. ``authors.sort`` equals ``authors.name`` instead of being in
   "Lastname, Firstname" sort format.
2. ``books.author_sort`` is inconsistent with the linked authors' sort names.

These mismatches cause Calibre-Web to log:
    ERROR Author 'Sivers, Derek' not found to display name in right order

The script works on a local temp copy of the database to avoid SQLite
locking problems with SMB/NFS network shares, then writes the fixed file
back and creates a ``.bak`` backup of the original.

Stop Calibre-Web before running so the database is not locked.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# Suffixes that are not a "last name" (case-insensitive, trailing dots stripped)
_SUFFIXES: frozenset[str] = frozenset({
    "jr", "sr", "ii", "iii", "iv", "v",
    "md", "m.d", "do", "d.o",
    "phd", "ph.d", "dphil", "d.phil",
    "dds", "d.d.s", "dvm", "d.v.m",
    "esq", "ret", "cpa",
})

# Particles that attach to the following last name
_PREFIXES: frozenset[str] = frozenset({
    "von", "van", "de", "der", "del", "della",
    "di", "du", "la", "le", "ten", "ter", "den", "los", "las",
})


def _author_to_sort(name: str) -> str:
    """Convert 'Firstname Lastname' to 'Lastname, Firstname'.

    Mirrors Calibre's author_to_author_sort algorithm:
    - trailing suffixes (MD, PhD, Jr., …) are kept after the comma
    - noble particles (von, van, de, …) are attached to the last name
    - names already containing a comma are returned unchanged
    """
    name = name.strip()
    if not name or "," in name:
        return name
    parts = name.split()
    if len(parts) == 1:
        return name

    # Peel trailing suffixes
    suffixes: list[str] = []
    while len(parts) > 1 and parts[-1].lower().rstrip(".") in _SUFFIXES:
        suffixes.insert(0, parts.pop())

    if len(parts) == 1:
        return name  # whole name was suffixes — leave unchanged

    last = parts[-1]
    rest = parts[:-1]

    # Attach noble particle to last name (e.g. "van Gogh")
    if rest and rest[-1].lower() in _PREFIXES:
        last = rest.pop() + " " + last

    first_part = " ".join(rest)
    if suffixes:
        return f"{last}, {first_part} {' '.join(suffixes)}"
    return f"{last}, {first_part}"


def _title_sort(title: str) -> str:
    """Move leading article to end.

    Registered as a SQLite function to satisfy Calibre's ``books_update_trg``
    trigger, which references ``title_sort()`` even when the title is unchanged.
    """
    if not title:
        return title
    for article in ("the ", "a ", "an "):
        if title.lower().startswith(article):
            return title[len(article):].strip() + ", " + title[: len(article)].strip()
    return title


def fix_author_sort(library_path: Path, *, dry_run: bool = False) -> int:
    """Fix author sort names in the Calibre library at *library_path*.

    Returns the number of changes made (0 when nothing needed fixing).
    Raises ``SystemExit`` on fatal errors.
    """
    db_path = library_path / "metadata.db"
    if not db_path.exists():
        print(f"ERROR: metadata.db not found in {library_path}", file=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "metadata.db"

        # Copy all three SQLite files (WAL-mode databases need all three).
        # Use copy() not copy2() — copy2 tries to preserve macOS file flags
        # which fail on SMB/NFS shares (PermissionError from chflags).
        for suffix in ("", "-shm", "-wal"):
            src = Path(str(db_path) + suffix)
            if src.exists():
                shutil.copy(src, Path(str(tmp_db) + suffix))

        con = sqlite3.connect(str(tmp_db))
        con.create_function("title_sort", 1, _title_sort)
        cur = con.cursor()

        # ── Step 1: compute new sort values for authors where sort == name ──
        cur.execute("SELECT id, name FROM authors WHERE name = sort")
        author_fixes: list[tuple[int, str, str]] = [
            (aid, name, new)
            for aid, name in cur.fetchall()
            if (new := _author_to_sort(name)) != name
        ]

        if not dry_run:
            for aid, _name, new_sort in author_fixes:
                cur.execute("UPDATE authors SET sort = ? WHERE id = ?", (new_sort, aid))

        # ── Step 2: recompute books.author_sort from (updated) authors ──
        cur.execute("SELECT DISTINCT book FROM books_authors_link ORDER BY book")
        book_ids = [row[0] for row in cur.fetchall()]

        book_fixes: list[tuple[int, str, str, str]] = []
        for book_id in book_ids:
            cur.execute(
                """
                SELECT a.sort
                FROM books_authors_link bal
                JOIN authors a ON bal.author = a.id
                WHERE bal.book = ?
                ORDER BY bal.id
                """,
                (book_id,),
            )
            correct = " & ".join(row[0] for row in cur.fetchall())
            if not correct:
                continue
            cur.execute("SELECT author_sort, title FROM books WHERE id = ?", (book_id,))
            row = cur.fetchone()
            if row and row[0] != correct:
                book_fixes.append((book_id, row[1], row[0], correct))
                if not dry_run:
                    cur.execute(
                        "UPDATE books SET author_sort = ? WHERE id = ?",
                        (correct, book_id),
                    )

        total = len(author_fixes) + len(book_fixes)

        if dry_run:
            if not total:
                print("Nothing to fix — all author sort names are already correct.")
                con.close()
                return 0
            print(f"[dry-run] {len(author_fixes)} author sort name(s) to fix:")
            for _, name, new_sort in author_fixes:
                print(f"  {name!r}  →  {new_sort!r}")
            print(f"[dry-run] {len(book_fixes)} book author_sort(s) to fix:")
            for book_id, title, old, new in book_fixes:
                print(f"  [{book_id}] {title!r}")
                print(f"    was: {old!r}")
                print(f"    now: {new!r}")
            con.close()
            return total

        con.commit()
        con.close()

        if not total:
            print("Nothing to fix — all author sort names are already correct.")
            return 0

        # Back up original, then write fixed copy to the library
        backup = Path(str(db_path) + ".bak")
        shutil.copy2(db_path, backup)
        shutil.copy2(tmp_db, db_path)
        print(f"Backup saved to {backup}")

    if author_fixes:
        print(f"Fixed {len(author_fixes)} author sort name(s):")
        for _, name, new_sort in author_fixes:
            print(f"  {name!r}  →  {new_sort!r}")
    if book_fixes:
        print(f"Fixed {len(book_fixes)} book author_sort(s).")

    print("Done.")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fix Calibre author sort names to resolve Calibre-Web "
            "'not found to display name in right order' errors."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "library_path",
        type=Path,
        help="Path to the Calibre library directory (contains metadata.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing anything",
    )
    args = parser.parse_args()

    if not args.library_path.is_dir():
        print(f"ERROR: {args.library_path} is not a directory", file=sys.stderr)
        sys.exit(1)

    fix_author_sort(args.library_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
