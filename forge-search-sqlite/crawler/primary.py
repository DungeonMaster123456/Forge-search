"""
1. PRIMARY CRAWLER
Broad discovery bot. Starts from seed URLs, follows outbound links (BFS),
stores every page it can fetch, and records the link graph.

Run:
    python crawler/primary.py

This is the workhorse — run it repeatedly (manually, a cron job, or a
scheduled task) to keep growing/refreshing the index.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.connection import get_connection
from crawler.common import fetch, parse_page, content_hash, get_domain

# Seed URLs for the "tech blogs / dev docs" niche.
# Add more here as you grow the index.
SEED_URLS = [
    "https://docs.python.org/3/",
    "https://martinfowler.com/",
    "https://overreacted.io/",
    "https://joel.is/",
    "https://www.joelonsoftware.com/",
]

MAX_PAGES_PER_RUN = 50  # keep an MVP run bounded and fast


def upsert_page(conn, url, status, parsed, crawler_type="primary"):
    chash = content_hash(parsed["body_text"])
    existing = conn.execute("SELECT content_hash FROM pages WHERE url = ?", (url,)).fetchone()

    if existing and existing["content_hash"] == chash:
        return  # unchanged since last crawl, skip rewrite

    conn.execute(
        """
        INSERT INTO pages (url, domain, title, description, body_text,
                            canonical_url, crawler_type, http_status, content_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            body_text = excluded.body_text,
            canonical_url = excluded.canonical_url,
            http_status = excluded.http_status,
            content_hash = excluded.content_hash,
            fetched_at = datetime('now')
        """,
        (url, get_domain(url), parsed["title"], parsed["description"],
         parsed["body_text"], parsed["canonical_url"], crawler_type,
         status, chash),
    )
    conn.commit()


def record_links(conn, from_url, links):
    for to_url in links:
        conn.execute(
            "INSERT OR IGNORE INTO links (from_url, to_url) VALUES (?, ?)",
            (from_url, to_url),
        )
    for to_url in links:
        conn.execute(
            """
            UPDATE pages SET inbound_links = (
                SELECT count(*) FROM links WHERE links.to_url = pages.url
            ) WHERE url = ?
            """,
            (to_url,),
        )
    conn.commit()


def enqueue(conn, urls, crawler_type="primary", priority=0):
    for url in urls:
        conn.execute(
            "INSERT OR IGNORE INTO crawl_queue (url, crawler_type, priority) VALUES (?, ?, ?)",
            (url, crawler_type, priority),
        )
    conn.commit()


def run():
    conn = get_connection()
    enqueue(conn, SEED_URLS, priority=10)

    batch = conn.execute(
        """
        SELECT id, url FROM crawl_queue
        WHERE status = 'pending' AND crawler_type = 'primary'
        ORDER BY priority DESC, added_at ASC
        LIMIT ?
        """,
        (MAX_PAGES_PER_RUN,),
    ).fetchall()

    pages_done = 0
    for row in batch:
        url = row["url"]
        print(f"[crawl] {url}")

        conn.execute(
            "UPDATE crawl_queue SET status = 'in_progress', attempts = attempts + 1 WHERE id = ?",
            (row["id"],),
        )
        conn.commit()

        status, html = fetch(url)
        if not html:
            conn.execute("UPDATE crawl_queue SET status = 'failed' WHERE id = ?", (row["id"],))
            conn.commit()
            continue

        parsed = parse_page(url, html)
        upsert_page(conn, url, status, parsed)
        record_links(conn, url, parsed["links"])

        enqueue(conn, parsed["links"][:20], priority=0)  # cap to avoid queue explosion

        conn.execute("UPDATE crawl_queue SET status = 'done' WHERE id = ?", (row["id"],))
        conn.commit()

        pages_done += 1

    conn.close()
    print(f"[done] crawled {pages_done} pages this run")


if __name__ == "__main__":
    run()
