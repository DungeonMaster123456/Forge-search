"""
3. ON-DEMAND / USER-TRIGGERED FETCHER
Freshness layer. Called synchronously (or near-real-time) by the API when:
  - a search returns few/no results, and we have a plausible URL to try, or
  - an existing page hasn't been recrawled recently.

This is intentionally the SIMPLEST crawler — one URL in, one page indexed,
fast. It does not do link discovery (that's the primary crawler's job).

Importable as a function so api/main.py can call it directly.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.connection import get_connection
from crawler.common import fetch, parse_page, content_hash, get_domain


def fetch_and_index(url: str) -> dict:
    """
    Fetch a single URL right now and index it immediately.
    Returns a status dict — safe to call from a web request handler.
    """
    status, html = fetch(url)
    if not html:
        return {"ok": False, "reason": "fetch_failed_or_disallowed", "url": url}

    parsed = parse_page(url, html)
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO pages (url, domain, title, description, body_text,
                                canonical_url, crawler_type, http_status, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, 'ondemand', ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                body_text = excluded.body_text,
                http_status = excluded.http_status,
                content_hash = excluded.content_hash,
                fetched_at = datetime('now')
            """,
            (url, get_domain(url), parsed["title"], parsed["description"],
             parsed["body_text"], parsed["canonical_url"], status,
             content_hash(parsed["body_text"])),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "url": url, "title": parsed["title"]}


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 2:
        print("Usage: python crawler/ondemand.py <url>")
    else:
        print(fetch_and_index(_sys.argv[1]))
