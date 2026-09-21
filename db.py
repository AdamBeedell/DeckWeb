"""SQLite schema and small query helpers. One file, no ORM."""
import os
import sqlite3

from flask import g

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "app.db")

MAX_LISTS_PER_USER = 10
MAX_ITEMS_PER_LIST = 250
MAX_TAGS_PER_LIST = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    pw_hash     TEXT NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    key_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS lists (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    mode        TEXT NOT NULL DEFAULT 'mtg',      -- 'mtg' or 'plain'
    source_text TEXT,
    created     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS items (
    id        INTEGER PRIMARY KEY,
    list_id   INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    oracle_id TEXT,                                -- NULL for plain lists
    print_id  TEXT,
    position  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tags (
    id      INTEGER PRIMARY KEY,
    list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    name    TEXT NOT NULL COLLATE NOCASE,          -- 'Ramp' and 'ramp' are one tag
    source  TEXT NOT NULL DEFAULT 'manual',        -- manual / import / query
    UNIQUE (list_id, name)
);
CREATE TABLE IF NOT EXISTS item_tags (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (item_id, tag_id)
);
-- Card database (from Scryfall Oracle Cards bulk data)
CREATE TABLE IF NOT EXISTS cards (
    oracle_id      TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    supertypes     TEXT, types TEXT, subtypes TEXT, type_line TEXT,
    mana_cost      TEXT, cmc REAL, color_identity TEXT,
    keywords       TEXT, oracle_text TEXT,
    default_print  TEXT
);
CREATE TABLE IF NOT EXISTS card_names (           -- full name + each face name
    name_norm TEXT PRIMARY KEY,
    oracle_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prints (
    print_id  TEXT PRIMARY KEY,
    oracle_id TEXT,
    set_code  TEXT, collector_number TEXT, set_name TEXT,
    image_url TEXT
);
CREATE INDEX IF NOT EXISTS prints_setnum ON prints(set_code, collector_number);
CREATE TABLE IF NOT EXISTS query_cache (
    query      TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    names_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def get_db():
    """Per-request connection."""
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def init_db():
    con = connect()
    con.executescript(SCHEMA)
    try:  # full-text search over card text, for future rules search
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS cards_fts USING fts5("
                    "name, type_line, oracle_text, content='cards', content_rowid='rowid')")
    except sqlite3.OperationalError:
        pass
    con.commit()
    con.close()


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))
