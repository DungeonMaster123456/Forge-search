-- Forge-Search: SQLite schema (uses FTS5 for full-text search)
-- Applied automatically on first run by db/connection.py — you don't need
-- to run this by hand, but it's here for reference / manual setup.

CREATE TABLE IF NOT EXISTS pages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT UNIQUE NOT NULL,
    domain          TEXT NOT NULL,
    title           TEXT,
    description     TEXT,
    body_text       TEXT,
    canonical_url   TEXT,
    crawler_type    TEXT NOT NULL DEFAULT 'primary',
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now')),
    http_status     INTEGER,
    content_hash    TEXT,
    inbound_links   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pages_domain ON pages (domain);

CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    title, description, body_text,
    content='pages', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS pages_ai AFTER INSERT ON pages BEGIN
    INSERT INTO pages_fts(rowid, title, description, body_text)
    VALUES (new.id, new.title, new.description, new.body_text);
END;

CREATE TRIGGER IF NOT EXISTS pages_ad AFTER DELETE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, description, body_text)
    VALUES ('delete', old.id, old.title, old.description, old.body_text);
END;

CREATE TRIGGER IF NOT EXISTS pages_au AFTER UPDATE ON pages BEGIN
    INSERT INTO pages_fts(pages_fts, rowid, title, description, body_text)
    VALUES ('delete', old.id, old.title, old.description, old.body_text);
    INSERT INTO pages_fts(rowid, title, description, body_text)
    VALUES (new.id, new.title, new.description, new.body_text);
END;

CREATE TABLE IF NOT EXISTS links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_url    TEXT NOT NULL,
    to_url      TEXT NOT NULL,
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (from_url, to_url)
);

CREATE INDEX IF NOT EXISTS idx_links_to_url ON links (to_url);

CREATE TABLE IF NOT EXISTS crawl_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT UNIQUE NOT NULL,
    crawler_type TEXT NOT NULL DEFAULT 'primary',
    priority    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    attempts    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_crawl_queue_status_priority
    ON crawl_queue (status, priority DESC);

CREATE TABLE IF NOT EXISTS search_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    query       TEXT NOT NULL,
    result_count INTEGER NOT NULL,
    searched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
