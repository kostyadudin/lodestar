"""SQLite storage for Lodestar indexes."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    language TEXT NOT NULL,
    role TEXT NOT NULL,
    summary TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    symbol_id TEXT,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    text TEXT NOT NULL,
    summary TEXT NOT NULL,
    token_estimate INTEGER NOT NULL,
    hash TEXT NOT NULL,
    chunk_type TEXT NOT NULL DEFAULT 'window',
    FOREIGN KEY(path) REFERENCES files(path) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);

CREATE TABLE IF NOT EXISTS symbols (
    symbol_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    signature TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    summary TEXT NOT NULL,
    hash TEXT NOT NULL,
    token_estimate INTEGER NOT NULL,
    FOREIGN KEY(path) REFERENCES files(path) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);

CREATE TABLE IF NOT EXISTS relations (
    relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ref TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0
);

CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_ref);
CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_ref);

CREATE TABLE IF NOT EXISTS subsystems (
    name TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    representative_paths TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    symbol_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    memory_id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    evidence_refs TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_validated_at TEXT
);

CREATE TABLE IF NOT EXISTS query_cache (
    query_key TEXT PRIMARY KEY,
    query_text TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings (
    ref TEXT PRIMARY KEY,
    vector BLOB NOT NULL,
    model TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_files USING fts5(
    path UNINDEXED,
    summary,
    role,
    language,
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_symbols USING fts5(
    path UNINDEXED,
    symbol_id UNINDEXED,
    name,
    kind,
    summary,
    tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
    path UNINDEXED,
    chunk_id UNINDEXED,
    text,
    tokenize='porter unicode61'
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "chunks", "symbol_id", "TEXT")
    _ensure_column(conn, "chunks", "chunk_type", "TEXT NOT NULL DEFAULT 'window'")
    _ensure_column(conn, "memories", "evidence_hash", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "memories", "last_validated_at", "TEXT")


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Repopulate all FTS5 virtual tables from the main tables."""
    conn.execute("DELETE FROM fts_files")
    conn.execute("DELETE FROM fts_symbols")
    conn.execute("DELETE FROM fts_chunks")
    conn.execute("INSERT INTO fts_files(path, summary, role, language) SELECT path, summary, role, language FROM files")
    conn.execute(
        "INSERT INTO fts_symbols(path, symbol_id, name, kind, summary) "
        "SELECT path, symbol_id, name, kind, summary FROM symbols"
    )
    conn.execute("INSERT INTO fts_chunks(path, chunk_id, text) SELECT path, chunk_id, text FROM chunks")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
