"""SQLite persistence for survey batches, households, and path answers.

A batch is *published* only when every household and every cleaned answer has
been written.  Rows for a batch that is still importing carry ``status =
'importing'``; the list, the batch page, the edit page and the CSV download
all ignore such batches, so a failed or interrupted import never exposes a
half-written survey.  Publishing is a single transaction that flips the
status, after which the batch becomes visible everywhere at once.

Chunked writes use their own short transactions.  They never hold a write
lock across the whole import, so edits to existing batches can be saved
while a large import is running.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    definition TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS households (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    household_no TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    UNIQUE (batch_id, household_no)
);

CREATE TABLE IF NOT EXISTS answers (
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    prefix TEXT NOT NULL,
    qid TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (household_id, prefix, qid)
);

CREATE INDEX IF NOT EXISTS answers_household ON answers (household_id);
"""

IMPORTING = "importing"
READY = "ready"

# Paths whose schema has been checked by this process.  Re-running the DDL on
# every request would take a write lock each time and contend with a running
# import, so do it once per process (and once more for a brand-new file).
_INITIALISED = set()
_INITIALISED_LOCK = threading.Lock()


def connect(path):
    # Autocommit mode: transactions are opened explicitly with BEGIN, which
    # keeps write locks as short as possible around the chunked import.
    connection = sqlite3.connect(path, timeout=30, isolation_level=None,
                                 check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    # WAL lets readers (pages, downloads) and the import's short write
    # transactions proceed at the same time.
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError:
        pass  # e.g. database on a filesystem without WAL support
    key = os.path.abspath(path)
    if key not in _INITIALISED:
        with _INITIALISED_LOCK:
            if key not in _INITIALISED:
                _ensure_schema(connection)
                _INITIALISED.add(key)
    return connection


def _ensure_schema(connection):
    connection.executescript(SCHEMA)
    # Add the status column to databases created by older versions.  A racing
    # process may add it between the check and the ALTER; that is harmless.
    columns = [row["name"] for row in
               connection.execute("PRAGMA table_info(batches)")]
    if "status" not in columns:
        try:
            connection.execute(
                "ALTER TABLE batches ADD COLUMN status TEXT NOT NULL "
                "DEFAULT 'ready'")
        except sqlite3.OperationalError:
            columns = [row["name"] for row in
                       connection.execute("PRAGMA table_info(batches)")]
            if "status" not in columns:
                raise


@contextmanager
def transaction(connection):
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        connection.execute("BEGIN")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def _prefix_key(prefix):
    return json.dumps(list(prefix), ensure_ascii=False, separators=(",", ":"))


def _prefix_tuple(key):
    try:
        value = json.loads(key)
    except (TypeError, ValueError):
        return ()
    return tuple(str(item) for item in value)


def create_batch(connection, name, definition, households, answers_by_household):
    """Insert a small, ready batch in one transaction (tests/legacy callers)."""
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO batches (name, definition, status) "
            "VALUES (?, ?, ?)",
            (name, json.dumps(definition, ensure_ascii=False), READY))
        batch_id = cursor.lastrowid
        for household_no, row_number in households:
            insert_household(connection, batch_id, household_no, row_number,
                             (answers_by_household.get(household_no) or {}))
    return batch_id


def begin_import(connection, name, definition):
    """Create an unpublished batch and return its id."""
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO batches (name, definition, status) "
            "VALUES (?, ?, ?)",
            (name, json.dumps(definition, ensure_ascii=False), IMPORTING))
        return cursor.lastrowid


def insert_household(connection, batch_id, household_no, row_number, answers):
    """Insert one household and its cleaned answers.

    Runs inside the caller's transaction.  Duplicate household numbers are
    ignored so the first row in the file always wins.  ``INSERT OR IGNORE``
    plus ``lastrowid`` keeps this portable to SQLite without ``RETURNING``.
    """
    cursor = connection.execute(
        "INSERT OR IGNORE INTO households "
        "(batch_id, household_no, row_number) VALUES (?, ?, ?)",
        (batch_id, household_no, row_number))
    if cursor.rowcount == 0:  # duplicate: keep the first occurrence
        return None
    household_id = cursor.lastrowid
    rows = [
        (household_id, _prefix_key(prefix), qid, value)
        for (prefix, qid), value in answers.items()
    ]
    if rows:
        connection.executemany(
            "INSERT OR REPLACE INTO answers "
            "(household_id, prefix, qid, value) VALUES (?, ?, ?, ?)", rows)
    return household_id


def write_households(connection, batch_id, households):
    """Write a chunk of ``(household_no, row_number, answers)`` triples.

    Everything in ``households`` is committed together.  A crash between
    chunks leaves an unpublished batch, which is discarded on failure.
    """
    with transaction(connection):
        for household_no, row_number, answers in households:
            insert_household(connection, batch_id, household_no, row_number,
                             answers)


def publish_batch(connection, batch_id):
    """Make a fully written batch visible.  Returns False if it is gone."""
    with transaction(connection):
        cursor = connection.execute(
            "UPDATE batches SET status = ? WHERE id = ? AND status = ?",
            (READY, batch_id, IMPORTING))
        return cursor.rowcount > 0


def discard_batch(connection, batch_id):
    """Remove an unpublished batch and everything written for it."""
    with transaction(connection):
        connection.execute("DELETE FROM batches WHERE id = ?", (batch_id,))


def count_households(connection, batch_id):
    row = connection.execute(
        "SELECT COUNT(*) AS n FROM households WHERE batch_id = ?",
        (batch_id,)).fetchone()
    return row["n"] if row else 0


def discard_stale_imports(connection, older_than_seconds=3600):
    """Drop imports abandoned by a killed process/worker.

    Unpublished batches are never visible, but a hard crash can leave their
    rows behind.  Age is measured by the database's own clock so this works
    regardless of webserver restarts.  The common case (nothing stale) only
    takes a shared read, so browsing the index never blocks saves.
    """
    rows = connection.execute(
        "SELECT id FROM batches WHERE status = ? AND "
        "created_at < datetime('now', 'localtime', ?)",
        (IMPORTING, "-%d seconds" % int(older_than_seconds))).fetchall()
    if not rows:
        return 0
    with transaction(connection):
        for row in rows:
            connection.execute("DELETE FROM batches WHERE id = ?",
                               (row["id"],))
    return len(rows)


def get_batch(connection, batch_id, include_importing=False):
    row = connection.execute(
        "SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        return None
    if not include_importing and row["status"] != READY:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "definition": json.loads(row["definition"]),
        "created_at": row["created_at"],
        "status": row["status"],
    }


def list_batches(connection):
    rows = connection.execute(
        "SELECT b.id, b.name, b.created_at, COUNT(h.id) AS households "
        "FROM batches b LEFT JOIN households h ON h.batch_id = b.id "
        "WHERE b.status = ? "
        "GROUP BY b.id ORDER BY b.id DESC", (READY,)).fetchall()
    return [dict(row) for row in rows]


def list_households(connection, batch_id):
    rows = connection.execute(
        "SELECT id, household_no, row_number FROM households "
        "WHERE batch_id = ? ORDER BY row_number, id", (batch_id,)).fetchall()
    return [dict(row) for row in rows]


def iter_households(connection, batch_id, chunk_size=500):
    """Yield households in small chunks so a listing stays streamed."""
    rows = connection.execute(
        "SELECT id, household_no, row_number FROM households "
        "WHERE batch_id = ? ORDER BY row_number, id", (batch_id,))
    while True:
        chunk = rows.fetchmany(chunk_size)
        if not chunk:
            return
        yield from chunk


def iter_households_with_answers(connection, batch_id, chunk_size=200):
    """Yield ``(household_row, answers)`` in bounded chunks.

    Used by the export to avoid one query per household while still keeping
    memory flat on large batches.
    """
    households = connection.execute(
        "SELECT id, household_no, row_number FROM households "
        "WHERE batch_id = ? ORDER BY row_number, id", (batch_id,))
    while True:
        chunk = households.fetchmany(chunk_size)
        if not chunk:
            return
        ids = [row["id"] for row in chunk]
        placeholders = ",".join("?" * len(ids))
        answers = {}
        for row in connection.execute(
                "SELECT household_id, prefix, qid, value FROM answers "
                "WHERE household_id IN (%s)" % placeholders, ids):
            answers.setdefault(row["household_id"], {})[
                (_prefix_tuple(row["prefix"]), row["qid"])] = row["value"]
        for household in chunk:
            yield household, answers.get(household["id"], {})


def get_household(connection, batch_id, household_id):
    row = connection.execute(
        "SELECT id, household_no, row_number FROM households "
        "WHERE batch_id = ? AND id = ?",
        (batch_id, household_id)).fetchone()
    return dict(row) if row else None


def load_answers(connection, household_id):
    rows = connection.execute(
        "SELECT prefix, qid, value FROM answers WHERE household_id = ?",
        (household_id,)).fetchall()
    return {(_prefix_tuple(row["prefix"]), row["qid"]): row["value"]
            for row in rows}


def load_answers_for_batch(connection, batch_id):
    """Return ``{household_id: {(prefix, qid): value}}`` in one query.

    Only for small batches; the views stream with
    :func:`iter_households_with_answers` instead.
    """
    grouped = {}
    for household, answers in iter_households_with_answers(
            connection, batch_id):
        grouped[household["id"]] = answers
    return grouped


def save_answers(connection, household_id, changes):
    """Persist ``{key: value_or_None}`` for one household.

    ``key`` is ``(prefix_tuple, qid)``; ``None`` deletes.  All changes are
    written in a single transaction, so a failure leaves the previous answers
    fully intact.
    """
    with transaction(connection):
        for (prefix, qid), value in changes.items():
            key = _prefix_key(prefix)
            if value is None:
                connection.execute(
                    "DELETE FROM answers WHERE household_id = ? "
                    "AND prefix = ? AND qid = ?",
                    (household_id, key, qid))
            else:
                connection.execute(
                    "INSERT INTO answers (household_id, prefix, qid, value) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (household_id, prefix, qid) "
                    "DO UPDATE SET value = excluded.value",
                    (household_id, key, qid, value))


def db_path_from_env():
    return os.environ.get("SURVEY_DB", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "survey.sqlite3"))
