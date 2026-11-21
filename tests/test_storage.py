import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

import storage  # noqa: E402

DEFN = [{"id": "Q1", "title": "来源", "type": "single",
         "options": ["自购", "租赁"], "jumps": {}}]


def open_db(tmp_path):
    return storage.connect(str(tmp_path / "survey.sqlite3"))


def test_importing_batch_is_invisible_until_published(tmp_path):
    connection = open_db(tmp_path)
    batch_id = storage.begin_import(connection, "批次", DEFN)
    storage.write_households(connection, batch_id, [
        ("H1", 1, {((), "Q1"): "自购"}),
        ("H2", 2, {((), "Q1"): "租赁"}),
    ])

    # Written, but not visible anywhere.
    assert storage.list_batches(connection) == []
    assert storage.get_batch(connection, batch_id) is None
    assert storage.get_batch(connection, batch_id,
                             include_importing=True)["status"] == "importing"

    assert storage.publish_batch(connection, batch_id) is True
    assert [b["id"] for b in storage.list_batches(connection)] == [batch_id]
    assert storage.get_batch(connection, batch_id)["status"] == "ready"
    assert storage.count_households(connection, batch_id) == 2
    connection.close()


def test_discard_removes_everything_written(tmp_path):
    connection = open_db(tmp_path)
    batch_id = storage.begin_import(connection, "批次", DEFN)
    storage.write_households(connection, batch_id, [
        ("H1", 1, {((), "Q1"): "自购"}),
    ])
    storage.discard_batch(connection, batch_id)

    assert storage.count_households(connection, batch_id) == 0
    assert storage.list_batches(connection) == []
    households = connection.execute(
        "SELECT COUNT(*) FROM households").fetchone()[0]
    answers = connection.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    assert (households, answers) == (0, 0)
    connection.close()


def test_duplicate_household_in_chunks_keeps_first(tmp_path):
    """A duplicate spanning two chunks must still keep the first row."""
    connection = open_db(tmp_path)
    batch_id = storage.begin_import(connection, "批次", DEFN)
    storage.write_households(connection, batch_id, [
        ("H1", 1, {((), "Q1"): "自购"}),
    ])
    storage.write_households(connection, batch_id, [
        ("H1", 2, {((), "Q1"): "租赁"}),
        ("H2", 3, {((), "Q1"): "自购"}),
    ])
    storage.publish_batch(connection, batch_id)

    households = storage.list_households(connection, batch_id)
    assert [h["household_no"] for h in households] == ["H1", "H2"]
    assert storage.load_answers(connection, households[0]["id"]) == {
        ((), "Q1"): "自购"}
    connection.close()


def test_existing_batch_editable_between_import_chunks(tmp_path):
    """An import holds no long write lock, so edits can be saved meanwhile."""
    path = str(tmp_path / "survey.sqlite3")
    importer_connection = storage.connect(path)
    editor_connection = storage.connect(path)

    ready = storage.begin_import(importer_connection, "旧批次", DEFN)
    storage.write_households(importer_connection, ready, [
        ("H1", 1, {((), "Q1"): "自购"})])
    storage.publish_batch(importer_connection, ready)
    household_id = storage.list_households(importer_connection, ready)[0]["id"]

    pending = storage.begin_import(importer_connection, "新批次", DEFN)
    storage.write_households(importer_connection, pending, [
        ("N1", 1, {((), "Q1"): "租赁"})])

    # While the new batch is mid-import, an existing batch saves fine.
    storage.save_answers(editor_connection, household_id,
                         {((), "Q1"): "租赁"})
    assert storage.load_answers(editor_connection, household_id) == {
        ((), "Q1"): "租赁"}

    storage.write_households(importer_connection, pending, [
        ("N2", 2, {((), "Q1"): "自购"})])
    storage.publish_batch(importer_connection, pending)
    assert {b["name"] for b in storage.list_batches(editor_connection)} == {
        "旧批次", "新批次"}
    importer_connection.close()
    editor_connection.close()


def test_schema_upgrade_adds_status_to_old_database(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            definition TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')));
        INSERT INTO batches (name, definition) VALUES ('旧', '[]');
    """)
    old.commit()
    old.close()

    connection = storage.connect(path)
    assert storage.get_batch(connection, 1)["status"] == "ready"
    assert [b["id"] for b in storage.list_batches(connection)] == [1]
    connection.close()


def test_stale_import_is_swept_but_live_import_kept(tmp_path):
    connection = open_db(tmp_path)
    live = storage.begin_import(connection, "进行中", DEFN)
    stale = storage.begin_import(connection, "被放弃", DEFN)
    # Age the abandoned one past the threshold (database clock).
    connection.execute(
        "UPDATE batches SET created_at = datetime('now', 'localtime', "
        "'-2 hours') WHERE id = ?", (stale,))
    storage.write_households(connection, stale, [
        ("H1", 1, {((), "Q1"): "自购"})])

    removed = storage.discard_stale_imports(connection, older_than_seconds=3600)
    assert removed == 1
    assert storage.get_batch(connection, live, include_importing=True)
    assert storage.get_batch(connection, stale, include_importing=True) is None
    assert storage.count_households(connection, stale) == 0
    connection.close()


def test_publish_only_flips_importing_batch(tmp_path):
    connection = open_db(tmp_path)
    ready = storage.begin_import(connection, "已完成", DEFN)
    assert storage.publish_batch(connection, ready) is True
    # Publishing twice is a no-op, not a success.
    assert storage.publish_batch(connection, ready) is False
    assert storage.publish_batch(connection, 999) is False
    connection.close()
