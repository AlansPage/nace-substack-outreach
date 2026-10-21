"""Storage connections: a local SQLite file (default) or hosted Postgres.

Local mode is the standard library only. Hosted mode (a postgres:// URL, or env:NAME pointing at one)
imports pg8000 lazily and adapts the small SQLite dialect the rest of the code uses, so the queries in
store/pipeline/sources stay unchanged.
"""
from __future__ import annotations

import os
import re
import sqlite3
import ssl
from pathlib import Path

TABLES = """
CREATE TABLE IF NOT EXISTS publications (
  id {id}, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Imported',
  score INTEGER NOT NULL DEFAULT 0, shortlisted INTEGER NOT NULL DEFAULT 0,
  pilot INTEGER NOT NULL DEFAULT 0, author_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aliases (key TEXT PRIMARY KEY, publication_id {ref} NOT NULL REFERENCES publications(id));
CREATE TABLE IF NOT EXISTS imports (fingerprint TEXT PRIMARY KEY, path TEXT, headers TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS source_rows (fingerprint TEXT, row_number INTEGER, publication_id {ref},
  data TEXT NOT NULL, PRIMARY KEY(fingerprint,row_number));
CREATE TABLE IF NOT EXISTS articles (id {id}, publication_id {ref} NOT NULL REFERENCES publications(id),
  url TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(publication_id,url));
CREATE TABLE IF NOT EXISTS contacts (id {id}, author_key TEXT NOT NULL,
  contact_key TEXT, channel TEXT, event TEXT, at TEXT, source TEXT, note TEXT, dedup TEXT UNIQUE, actor TEXT);
CREATE TABLE IF NOT EXISTS suppressions (key TEXT PRIMARY KEY, reason TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS identities (alias TEXT PRIMARY KEY, author_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drafts (id {id}, publication_id {ref} NOT NULL REFERENCES publications(id),
  version INTEGER NOT NULL, data TEXT NOT NULL, hash TEXT NOT NULL, approved_hash TEXT, approved_by TEXT,
  approved_at TEXT, created_at TEXT, UNIQUE(publication_id,version));
CREATE TABLE IF NOT EXISTS registry_history (hash TEXT PRIMARY KEY, version INTEGER, data TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS facts_registry (hash TEXT PRIMARY KEY, version INTEGER, data TEXT NOT NULL,
  pushed_at TEXT, pushed_by TEXT);
CREATE TABLE IF NOT EXISTS runs (id {id}, kind TEXT, started_at TEXT, finished_at TEXT,
  requests INTEGER DEFAULT 0, reserved_usd {real} DEFAULT 0, usage TEXT DEFAULT '[]', error TEXT);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, data TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id {id}, publication_id {ref}, kind TEXT, detail TEXT, at TEXT, actor TEXT);
CREATE INDEX IF NOT EXISTS pub_queue ON publications(pilot,score);
CREATE INDEX IF NOT EXISTS pub_author ON publications(author_key)
"""
# Columns added after the first release, for databases created before them.
ADDED_COLUMNS = (("events", "actor"), ("contacts", "actor"))
SQLITE_SCHEMA = TABLES.format(id="INTEGER PRIMARY KEY", ref="INTEGER", real="REAL")
POSTGRES_SCHEMA = TABLES.format(id="BIGSERIAL PRIMARY KEY", ref="BIGINT", real="DOUBLE PRECISION")

# INSERT OR REPLACE needs the conflict key and the full column list in Postgres.
REPLACE_TABLES = {"cache": ("key", ("key", "data", "at")), "suppressions": ("key", ("key", "reason", "at"))}
ID_TABLES = {"publications", "articles", "contacts", "drafts", "runs", "events"}


def resolve(url):
    url = str(url or "")
    if url.startswith("env:"):
        value = os.environ.get(url[4:], "")
        if not value:
            raise ValueError(f"Environment variable {url[4:]} is not set")
        return value
    return url


def is_postgres(url):
    return resolve(url).startswith(("postgres://", "postgresql://"))


def connect(url):
    target = resolve(url)
    if is_postgres(target):
        db = PgConnection(target)
        # DDL waits for locks held by running writes (CREATE INDEX, ALTER TABLE), so a request that ran it
        # on every connection stalled behind any upload. Read the catalog first; change the schema only when needed.
        if not pg_schema_ready(db):
            db.executescript(POSTGRES_SCHEMA)
            for table, column in ADDED_COLUMNS:
                db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} TEXT")
        db.commit()
        return db
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(target, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SQLITE_SCHEMA + ";")
    # Older databases gain the newer columns; existing rows keep NULL.
    for table, column in ADDED_COLUMNS:
        if column not in [r["name"] for r in db.execute(f"PRAGMA table_info({table})")]:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    db.commit()
    return db


SCHEMA_TABLES = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", TABLES))
SCHEMA_INDEXES = set(re.findall(r"CREATE INDEX IF NOT EXISTS (\w+)", TABLES))


def pg_schema_ready(db):
    """True when every table, index and later-added column exists; catalog reads take no table locks."""
    tables = {r[0] for r in db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
    indexes = {r[0] for r in db.execute("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")}
    columns = {(r[0], r[1]) for r in db.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema()")}
    return SCHEMA_TABLES <= tables and SCHEMA_INDEXES <= indexes and set(ADDED_COLUMNS) <= columns


def translate(sql):
    """SQLite statement -> (Postgres statement or None to skip, whether it returns the new id)."""
    text = sql.strip()
    if text.upper().startswith("PRAGMA"):
        return None, False
    text = re.sub(r"json_extract\((\w+),\s*'\$\.(\w+)'\)", r"(\1::jsonb->>'\2')", text)
    text = re.sub(r"\bdata LIKE \?", "data ILIKE ?", text)
    m = re.match(r"INSERT OR REPLACE INTO (\w+) VALUES\s*\(", text, re.I)
    if m:
        key, cols = REPLACE_TABLES[m.group(1)]
        updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != key)
        text = (f"INSERT INTO {m.group(1)}({','.join(cols)}) VALUES(" + text[m.end():]
                + f" ON CONFLICT ({key}) DO UPDATE SET {updates}")
    elif re.match(r"INSERT OR IGNORE INTO", text, re.I):
        text = "INSERT INTO" + text[len("INSERT OR IGNORE INTO"):] + " ON CONFLICT DO NOTHING"
    m = re.match(r"INSERT INTO (\w+)", text, re.I)
    returns_id = bool(m and m.group(1) in ID_TABLES and "RETURNING" not in text.upper())
    if returns_id:
        text += " RETURNING id"
    return text, returns_id


class Row(tuple):
    """Postgres row that behaves like sqlite3.Row: by index, by column name, and dict(row)."""

    def __new__(cls, values, names):
        row = super().__new__(cls, values)
        row._names = names
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            return tuple.__getitem__(self, self._names.index(key))
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)


class Cursor:
    def __init__(self, rows, lastrowid=None):
        self.rows, self.lastrowid = rows, lastrowid

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __iter__(self):
        return iter(list(self.rows))


class PgConnection:
    def __init__(self, url):
        try:
            import pg8000.dbapi
        except ImportError:
            raise ValueError("Hosted mode needs the pg8000 package (pip install -r requirements.txt)") from None
        from urllib.parse import unquote, urlsplit
        pg8000.dbapi.paramstyle = "qmark"
        u = urlsplit(url)
        query = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
        secure = query.get("sslmode", "require") != "disable"
        self.conn = pg8000.dbapi.connect(user=unquote(u.username or ""), password=unquote(u.password or ""),
                                         host=u.hostname, port=u.port or 5432, database=(u.path or "/").lstrip("/") or None,
                                         ssl_context=ssl.create_default_context() if secure else None, timeout=30)

    def execute(self, sql, params=()):
        text, returns_id = translate(sql)
        if text is None:
            return Cursor([])
        cur = self.conn.cursor()
        try:
            cur.execute(text, tuple(params))
            rows = []
            if cur.description:
                names = [d[0] for d in cur.description]
                rows = [Row(r, names) for r in cur.fetchall()]
        finally:
            cur.close()
        return Cursor(rows, rows[0][0] if returns_id and rows else None)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        if kind is None:
            self.commit()
        else:
            self.rollback()
        return False
