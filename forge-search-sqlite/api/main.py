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

# SearXNG — free, open-source metasearch engine (proxies Google/Bing/
# DuckDuckGo/Brave/etc.). Verified against the live instance list at
# https://searx.space/data/instances.json (checked Aug 2026) for high
# uptime and a working /search endpoint. NOT every public instance has
# the JSON API format enabled (many disable it), so we try a short list
# of known-good instances in order and move on if one doesn't cooperate.
# Set SEARXNG_BASE_URL to override with your own instance.
SEARXNG_INSTANCES = (
    [os.environ.get("SEARXNG_BASE_URL")] if os.environ.get("SEARXNG_BASE_URL")
    else ["https://baresearch.org", "https://etsi.me", "https://priv.au"]
)
SEARXNG_TIMEOUT = 8


def searxng_fallback(q: str, limit: int = 5, page: int = 1) -> list[dict]:
    """
    Real web search results via SearXNG. Tries each instance in
    SEARXNG_INSTANCES in order; moves to the next if one fails, is down,
    or has the JSON format disabled (common on public instances).
    """
    for base_url in SEARXNG_INSTANCES:
        try:
            resp = requests.get(
                f"{base_url}/search",
                params={"q": q, "format": "json", "pageno": page},
                timeout=SEARXNG_TIMEOUT,
                headers={"User-Agent": "ForgeSearchBot/0.1 (fallback helper)"},
            )
            if resp.status_code == 403:
                # JSON format disabled on this instance — try the next one
                print(f"[fallback] SearXNG {base_url} has JSON format disabled (403)")
                continue
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("results", [])
        except Exception as e:
            print(f"[fallback] SearXNG {base_url} failed: {type(e).__name__}: {e}")
            continue

        if not hits:
            continue

        results = []
        for hit in hits[:limit]:
            results.append({
                "url": hit.get("url", ""),
                "title": hit.get("title", ""),
                "description": None,
                "snippet": hit.get("content", ""),
                "inbound_links": 0,
                "score": 0,
            })
        return results

    return []


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


def knowledge_panel(q: str) -> dict | None:
    """
    Wikipedia summary for a query — powers the right-side "knowledge panel"
    (title, extract, infobox-style key facts, thumbnail) similar to what
    Google/SearXNG show for companies, people, and well-known topics.
    Returns None if there's no clean Wikipedia match (most queries won't
    have one, which is normal — the panel is optional, not required).
    """
    try:
        resp = requests.get(
            "https://en.wikipedia.org/api/rest_v1/page/summary/" + requests.utils.quote(q),
            timeout=WIKIPEDIA_TIMEOUT,
            headers={"User-Agent": "ForgeSearchBot/0.1 (knowledge panel)"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("type") == "disambiguation":
            return None
        return {
            "title": data.get("title"),
            "extract": data.get("extract"),
            "thumbnail": (data.get("thumbnail") or {}).get("source"),
            "url": (data.get("content_urls", {}).get("desktop") or {}).get("page"),
        }
    except Exception as e:
        print(f"[knowledge] lookup failed: {type(e).__name__}: {e}")
        return None


def searxng_category(q: str, category: str, limit: int = 20, page: int = 1) -> list[dict]:
    """
    Query SearXNG for a specific category (images, news, videos) instead of
    the default web results, with pagination support. Only works against a
    configured SEARXNG_BASE_URL (your own instance) — the public fallback
    list doesn't reliably support every category, so this is skipped if unset.
    """
    if not os.environ.get("SEARXNG_BASE_URL"):
        return []
    base_url = os.environ["SEARXNG_BASE_URL"]
    try:
        resp = requests.get(
            f"{base_url}/search",
            params={"q": q, "format": "json", "categories": category, "pageno": page},
            timeout=SEARXNG_TIMEOUT,
            headers={"User-Agent": "ForgeSearchBot/0.1 (category search)"},
        )
        resp.raise_for_status()
        hits = resp.json().get("results", [])
    except Exception as e:
        print(f"[{category}] SearXNG request failed: {type(e).__name__}: {e}")
        return []

    out = []
    for hit in hits[:limit]:
        if category == "images":
            out.append({
                "title": hit.get("title", ""),
                "url": hit.get("url", ""),
                "image_url": hit.get("img_src", ""),
                "thumbnail_url": hit.get("thumbnail_src") or hit.get("img_src", ""),
                "source": hit.get("source", ""),
            })
        elif category == "videos":
            out.append({
                "title": hit.get("title", ""),
                "url": hit.get("url", ""),
                "thumbnail_url": hit.get("thumbnail", ""),
                "source": hit.get("source", ""),
                "published": hit.get("publishedDate", ""),
                "length": hit.get("length", ""),
            })
        else:  # news
            out.append({
                "title": hit.get("title", ""),
                "url": hit.get("url", ""),
                "snippet": hit.get("content", ""),
                "source": hit.get("source", ""),
                "published": hit.get("publishedDate", ""),
            })
    return out


def run_search(conn, fts_query: str, limit: int, offset: int = 0):
    return conn.execute(
        """
        SELECT p.url, p.title, p.description, p.body_text, p.inbound_links,
               bm25(pages_fts) AS bm25_score
        FROM pages_fts
        JOIN pages p ON p.id = pages_fts.rowid
        WHERE pages_fts MATCH ?
        ORDER BY (bm25(pages_fts) * -1) + (MIN(p.inbound_links, 20) * 0.05) DESC
        LIMIT ? OFFSET ?
        """,
        (fts_query, limit, offset),
    ).fetchall()


@app.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = 10, page: int = 1):
    offset = (page - 1) * limit
    conn = get_connection()
    try:
        # First try an exact match (all terms required) — most precise.
        exact_query = build_fts_query(q, mode="exact")
        rows = run_search(conn, exact_query, limit, offset)

        # Nothing found? Fall back to a looser match: any term, as a prefix.
        # (Only on page 1 — fuzzy fallback on later pages of an empty exact
        # match would show confusing, unrelated "page 3" results.)
        if not rows and page == 1:
            fuzzy_query = build_fts_query(q, mode="fuzzy")
            rows = run_search(conn, fuzzy_query, limit, offset)

        index_results = []
        for r in rows:
            index_results.append({
                "url": r["url"],
                "title": r["title"],
                "description": r["description"],
                "snippet": make_snippet(r["body_text"], q),
                "inbound_links": r["inbound_links"],
                "score": round(-r["bm25_score"], 4),
                "source": "index",
            })

        # Always try SearXNG too (when configured) — our own crawled index
        # is small, so real web results are shown alongside it rather than
        # only as a last resort. Each result is tagged with where it came
        # from so the frontend can label sections separately.
        web_results = []
        for r in searxng_fallback(q, limit=limit, page=page):
            r["source"] = "web"
            web_results.append(r)

        wiki_results = []
        fallback_link = []
        if not index_results and not web_results and page == 1:
            wiki_hits = wikipedia_fallback(q, limit=limit)
            for r in wiki_hits:
                r["source"] = "wiki"
                wiki_results.append(r)
            if not wiki_hits:
                for r in guaranteed_fallback(q):
                    r["source"] = "link"
                    fallback_link.append(r)

        panel = knowledge_panel(q) if page == 1 else None

        total = len(index_results) + len(web_results) + len(wiki_results) + len(fallback_link)

        conn.execute(
            "INSERT INTO search_log (query, result_count) VALUES (?, ?)",
            (q, total),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "query": q,
        "page": page,
        "count": total,
        # has_more is a best-effort signal: if either source returned a full
        # page, there's probably more — used by the frontend to show/hide
        # the "Next page" button rather than promising an exact total count
        # (search engines never really know the true total cheaply).
        "has_more": len(index_results) >= limit or len(web_results) >= limit,
        "index_results": index_results,
        "web_results": web_results,
        "wiki_results": wiki_results,
        "fallback_link": fallback_link,
        "knowledge_panel": panel,
    }


@app.get("/search/images")
def search_images(q: str = Query(..., min_length=1), limit: int = 20, page: int = 1):
    results = searxng_category(q, "images", limit, page)
    return {"query": q, "page": page, "has_more": len(results) >= limit, "results": results}


@app.get("/search/news")
def search_news(q: str = Query(..., min_length=1), limit: int = 20, page: int = 1):
    results = searxng_category(q, "news", limit, page)
    return {"query": q, "page": page, "has_more": len(results) >= limit, "results": results}


@app.get("/search/videos")
def search_videos(q: str = Query(..., min_length=1), limit: int = 20, page: int = 1):
    results = searxng_category(q, "videos", limit, page)
    return {"query": q, "page": page, "has_more": len(results) >= limit, "results": results}


@app.get("/knowledge")
def knowledge(q: str = Query(..., min_length=1)):
    return {"query": q, "panel": knowledge_panel(q)}


@app.post("/fetch")
def fetch_url(url: str):
    """Manually trigger the on-demand crawler for a single URL."""
    return fetch_and_index(url)
