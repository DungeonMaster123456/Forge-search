"""
Forge-Search API
Run locally:
    uvicorn api.main:app --reload --port 8000

Endpoints:
    GET  /search?q=your+query   -> ranked results
    POST /fetch?url=...         -> manually trigger on-demand crawl of one URL
    GET  /health                -> liveness check
"""
import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from db.connection import get_connection
from crawler.ondemand import fetch_and_index

app = FastAPI(title="Forge-Search API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this before real deployment
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


def make_snippet(text: str, query: str, max_words: int = 30) -> str:
    """Cheap keyword-centered snippet — good enough for MVP, no FTS5 dependency."""
    if not text:
        return ""
    words = text.split()
    lower_terms = [t.lower() for t in query.split()]
    for i, w in enumerate(words):
        if w.lower().strip(".,!?") in lower_terms:
            start = max(0, i - 8)
            return " ".join(words[start:start + max_words])
    return " ".join(words[:max_words])


def build_fts_query(q: str, mode: str = "exact") -> str:
    """
    Turn a raw user query into valid FTS5 MATCH syntax.

    mode="exact"  -> "word1 word2"      (implicit AND, must contain all terms)
    mode="fuzzy"  -> "word1* OR word2*"  (prefix match, any term, much looser)

    Also strips characters that are special to FTS5 syntax (", *, etc.) out
    of the raw terms so user input can't break the query.
    """
    terms = re.findall(r"\w+", q)
    terms = [t for t in terms if t]  # drop empties
    if not terms:
        return '""'  # matches nothing, safely

    if mode == "fuzzy":
        return " OR ".join(f"{t}*" for t in terms)
    return " ".join(terms)


import requests

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_TIMEOUT = 5

# 4get — free, open-source metasearch engine (proxies Google/Bing/DuckDuckGo/
# Brave/etc. and returns combined JSON). The public 4get.ca instance now
# requires a "pass" token for API access (as of Aug 2026), so we don't hit
# it by default anymore — it just 401s. If you get your own pass token, or
# stand up your own instance (https://git.lolcat.ca/lolcat/4get), set both
# FOURGET_BASE_URL and FOURGET_PASS as environment variables and this tier
# will activate automatically. Until then it's skipped, and Wikipedia is
# the real fallback tier.
FOURGET_BASE_URL = os.environ.get("FOURGET_BASE_URL")  # unset by default
FOURGET_PASS = os.environ.get("FOURGET_PASS")
FOURGET_TIMEOUT = 8


def fourget_fallback(q: str, limit: int = 5) -> list[dict]:
    """
    Real web search results via 4get. Only runs if FOURGET_BASE_URL is
    explicitly set (see note above) — otherwise skipped entirely so we
    don't waste a request hitting a known-401 endpoint on every search.
    """
    if not FOURGET_BASE_URL:
        return []

    try:
        cookies = {"pass": FOURGET_PASS} if FOURGET_PASS else {}
        resp = requests.get(
            f"{FOURGET_BASE_URL}/api/v1/web",
            params={"s": q, "scraper": "ddg"},
            cookies=cookies,
            timeout=FOURGET_TIMEOUT,
            headers={"User-Agent": "ForgeSearchBot/0.1 (fallback helper)"},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            print(f"[fallback] 4get returned status={data.get('status')}")
            return []
        hits = data.get("web", [])
    except Exception as e:
        print(f"[fallback] 4get request failed: {type(e).__name__}: {e}")
        return []

    results = []
    for hit in hits[:limit]:
        results.append({
            "url": hit.get("url", ""),
            "title": hit.get("title", ""),
            "description": None,
            "snippet": hit.get("description", ""),
            "inbound_links": 0,
            "score": 0,
        })
    return results


def wikipedia_fallback(q: str, limit: int = 5) -> list[dict]:
    """
    Called ONLY when our own index has nothing for this query, even with
    fuzzy matching. Hits Wikipedia's public search API directly (not
    scraping — this is their documented, key-free API) and returns
    results clearly separate from our own index results.
    """
    try:
        resp = requests.get(
            WIKIPEDIA_API,
            params={
                "action": "query",
                "list": "search",
                "srsearch": q,
                "srlimit": limit,
                "format": "json",
            },
            timeout=WIKIPEDIA_TIMEOUT,
            headers={"User-Agent": "ForgeSearchBot/0.1 (fallback helper)"},
        )
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("query", {}).get("search", [])
    except Exception as e:
        # Print the REAL reason instead of silently returning empty —
        # so failures are visible in your terminal instead of looking
        # like "Wikipedia had nothing" when it's actually a network/API issue.
        print(f"[fallback] Wikipedia request failed: {type(e).__name__}: {e}")
        return []

    results = []
    for hit in hits:
        title = hit.get("title", "")
        # Wikipedia returns snippet with <span class="searchmatch"> HTML — strip it
        snippet = re.sub(r"<[^>]+>", "", hit.get("snippet", ""))
        page_url = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
        results.append({
            "url": page_url,
            "title": title,
            "description": None,
            "snippet": snippet,
            "inbound_links": 0,
            "score": 0,
        })
    return results


def guaranteed_fallback(q: str) -> list[dict]:
    """
    Last-resort tier: fires only if BOTH our index AND Wikipedia came back
    empty (Wikipedia down, no network, or a genuinely obscure query).
    Always returns something — a direct link out, never a dead end.
    """
    return [{
        "url": f"https://duckduckgo.com/?q={requests.utils.quote(q)}",
        "title": f'Search the web for "{q}"',
        "description": None,
        "snippet": "Our index and Wikipedia didn't have a match — click through to search the open web directly.",
        "inbound_links": 0,
        "score": 0,
    }]


def run_search(conn, fts_query: str, limit: int):
    return conn.execute(
        """
        SELECT p.url, p.title, p.description, p.body_text, p.inbound_links,
               bm25(pages_fts) AS bm25_score
        FROM pages_fts
        JOIN pages p ON p.id = pages_fts.rowid
        WHERE pages_fts MATCH ?
        ORDER BY (bm25(pages_fts) * -1) + (MIN(p.inbound_links, 20) * 0.05) DESC
        LIMIT ?
        """,
        (fts_query, limit),
    ).fetchall()


@app.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = 10):
    conn = get_connection()
    try:
        # First try an exact match (all terms required) — most precise.
        exact_query = build_fts_query(q, mode="exact")
        rows = run_search(conn, exact_query, limit)
        fuzzy_used = False

        # Nothing found? Fall back to a looser match: any term, as a prefix.
        # This is what makes "pytho" or a query with one typo/extra word
        # still return something instead of a hard zero.
        if not rows:
            fuzzy_query = build_fts_query(q, mode="fuzzy")
            rows = run_search(conn, fuzzy_query, limit)
            fuzzy_used = True

        results = []
        for r in rows:
            results.append({
                "url": r["url"],
                "title": r["title"],
                "description": r["description"],
                "snippet": make_snippet(r["body_text"], q),
                "inbound_links": r["inbound_links"],
                "score": round(-r["bm25_score"], 4),
            })

        source = "index"

        # Our own index still has nothing at all (even fuzzy): try real web
        # search (Serper) first, then Wikipedia, then a guaranteed link-out.
        # Every tier is labeled clearly on the frontend — none of it is
        # presented as if it came from our own crawled index.
        if not results:
            fallback_results = fourget_fallback(q)
            if fallback_results:
                results = fallback_results
                source = "fallback-web"
            else:
                fallback_results = wikipedia_fallback(q)
                if fallback_results:
                    results = fallback_results
                    source = "fallback-wiki"
                else:
                    results = guaranteed_fallback(q)
                    source = "fallback-link"

        conn.execute(
            "INSERT INTO search_log (query, result_count) VALUES (?, ?)",
            (q, len(results)),
        )
        conn.commit()
    finally:
        conn.close()

    return {"query": q, "count": len(results), "source": source, "results": results}


@app.post("/fetch")
def fetch_url(url: str):
    """Manually trigger the on-demand crawler for a single URL."""
    return fetch_and_index(url)
