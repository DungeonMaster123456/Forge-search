"""
SEED DISCOVERY via DuckDuckGo Lite
Not a data source for search results — this only harvests URLs to feed
into YOUR OWN crawler (crawler/primary.py). Forge-Search still crawls,
parses, and indexes every page itself; DDG Lite is just used the way a
human would use it to go find sites worth adding.

DuckDuckGo Lite (html.duckduckgo.com/html/) is a plain HTML results page
with no JS and no API key required, which is why it's used here instead
of the main DDG site or an API.

Run:
    python crawler/discover_seeds.py "python performance tips"
    python crawler/discover_seeds.py "javascript async guide" "react hooks tutorial"

This adds discovered URLs into crawl_queue at a lower priority than your
hand-picked SEED_URLS, so your curated seeds still get crawled first.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs, unquote

from db.connection import get_connection
from crawler.common import USER_AGENT, REQUEST_TIMEOUT, is_allowed

DDG_LITE_URL = "https://html.duckduckgo.com/html/"
RESULTS_PER_QUERY = 15


def search_ddg_lite(query: str) -> list[str]:
    """Returns a list of result URLs for a query, using DDG Lite's HTML page."""
    if not is_allowed(DDG_LITE_URL):
        print(f"[skip] DDG Lite disallows crawling per robots.txt")
        return []

    try:
        resp = requests.post(
            DDG_LITE_URL,
            data={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"[error] DDG Lite request failed: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    urls = []

    for a in soup.find_all("a", class_="result__a", href=True):
        href = a["href"]
        # DDG Lite wraps result links as /l/?uddg=<encoded real url>
        parsed = urlparse(href)
        if parsed.path == "/l/":
            qs = parse_qs(parsed.query)
            real_url = qs.get("uddg", [None])[0]
            if real_url:
                urls.append(unquote(real_url))
        elif href.startswith("http"):
            urls.append(href)

    return urls[:RESULTS_PER_QUERY]


def enqueue(conn, urls, priority=5):
    added = 0
    for url in urls:
        cur = conn.execute(
            "INSERT OR IGNORE INTO crawl_queue (url, crawler_type, priority) VALUES (?, ?, ?)",
            (url, "primary", priority),
        )
        if cur.rowcount:
            added += 1
    conn.commit()
    return added


def run(queries: list[str]):
    conn = get_connection()
    total_added = 0

    for query in queries:
        print(f"[discover] searching DDG Lite for: {query!r}")
        urls = search_ddg_lite(query)
        print(f"[discover] found {len(urls)} result URLs")
        added = enqueue(conn, urls)
        print(f"[discover] queued {added} new URLs (rest were already known)")
        total_added += added

    conn.close()
    print(f"[done] {total_added} new URLs added to crawl_queue — run crawler/primary.py to fetch them")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python crawler/discover_seeds.py "search query" ["another query" ...]')
        sys.exit(1)
    run(sys.argv[1:])
