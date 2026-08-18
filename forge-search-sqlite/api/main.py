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


# Second-layer SafeSearch backstop. SearXNG's own safesearch=2 is the
# primary filter (applied at the source, before results even reach us) —
# this is a deliberately blunt keyword check on top of that, so a result
# that slips past the upstream filter still gets flagged. It flags on
# title/URL text only (fast, no extra requests) — false positives are
# an acceptable trade-off for a safety feature; false negatives are not.
_NSFW_KEYWORDS = frozenset([
    "porn", "pornhub", "xvideos", "xnxx", "xhamster", "hentai", "nsfw",
    "onlyfans", "redtube", "youporn", "sex video", "nude", "naked",
    "explicit", "erotic", "escort", "camgirl", "webcam sex",
])


def is_flagged_nsfw(title: str, url: str) -> bool:
    text = f"{title or ''} {url or ''}".lower()
    return any(term in text for term in _NSFW_KEYWORDS)


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


def searxng_fallback(q: str, limit: int = 5, page: int = 1, safe: bool = True) -> list[dict]:
    """
    Real web search results via SearXNG. Tries each instance in
    SEARXNG_INSTANCES in order; moves to the next if one fails, is down,
    or has the JSON format disabled (common on public instances).

    safe=True passes SearXNG's own safesearch=2 (strict) — this filters
    results at the SOURCE (SearXNG/upstream engines), before anything ever
    reaches Forge-Search's frontend. The frontend also does its own black
    "safety wall" overlay as a second layer — see below — since no upstream
    filter is perfect on its own.
    """
    for base_url in SEARXNG_INSTANCES:
        try:
            resp = requests.get(
                f"{base_url}/search",
                params={
                    "q": q,
                    "format": "json",
                    "pageno": page,
                    "safesearch": 2 if safe else 0,
                },
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


def searxng_category(q: str, category: str, limit: int = 20, page: int = 1, safe: bool = True) -> list[dict]:
    """
    Query SearXNG for a specific category (images, news, videos, shopping)
    instead of the default web results, with pagination support. Only
    works against a configured SEARXNG_BASE_URL (your own instance) — the
    public fallback list doesn't reliably support every category, so this
    is skipped if unset.

    safe=True passes safesearch=2 (strict) to SearXNG — this is the FIRST
    line of defense for the Images tab especially, filtering explicit
    content at the source before it's ever returned to the frontend.
    """
    if not os.environ.get("SEARXNG_BASE_URL"):
        return []
    base_url = os.environ["SEARXNG_BASE_URL"]
    try:
        resp = requests.get(
            f"{base_url}/search",
            params={
                "q": q,
                "format": "json",
                "categories": category,
                "pageno": page,
                "safesearch": 2 if safe else 0,
            },
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
        elif category == "shopping":
            # The eBay engine (see searxng/settings.yml) is a general HTML
            # scraper, not a structured product API — it doesn't give us a
            # guaranteed separate price field like a real shopping API would.
            # We do a best-effort price extraction from the title/snippet
            # text (eBay listings usually include "$X.XX" somewhere in
            # there). If no price pattern is found, price is just omitted
            # rather than showing a wrong or fabricated number.
            raw_text = f"{hit.get('title', '')} {hit.get('content', '')}"
            price_match = re.search(r"\$[\d,]+\.?\d{0,2}", raw_text)
            out.append({
                "title": hit.get("title", ""),
                "url": hit.get("url", ""),
                "price": price_match.group(0) if price_match else None,
                "snippet": hit.get("content", ""),
                "source": "ebay",
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


GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"  # llama-3.3-70b-versatile was deprecated by Groq (announced June 2026)
GROQ_TIMEOUT = 20


def groq_chat(messages: list[dict], max_tokens: int = 512) -> str | None:
    """
    Thin wrapper around Groq's OpenAI-compatible chat completions endpoint.
    Returns the assistant's reply text, or None if GROQ_API_KEY isn't set
    or the request fails for any reason (network, auth, rate limit).
    Never raises — callers always get either a string or None.
    """
    if not GROQ_API_KEY:
        return None
    try:
        resp = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.6,
            },
            timeout=GROQ_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[jarvis] Groq request failed: {type(e).__name__}: {e}")
        return None


def people_also_ask(q: str) -> list[dict]:
    """
    Generates a small set of related follow-up questions + short answers,
    similar to Google's "People also ask" accordion. Uses Groq to generate
    plausible related questions AND answer them in one structured call, so
    it's a single request rather than N+1. Returns [] if Groq isn't
    configured or the call fails — this is a nice-to-have, never blocks
    the main search results.
    """
    reply = groq_chat(
        [
            {
                "role": "system",
                "content": (
                    "You generate a 'People also ask' box for a search engine. "
                    "Given a search query, output exactly 4 related questions "
                    "a curious searcher might ask next, each with a concise "
                    "2-3 sentence factual answer. Format STRICTLY as:\n"
                    "Q: <question>\nA: <answer>\n\n(repeat 4 times, blank line between)"
                    "\nNo preamble, no numbering, no markdown."
                ),
            },
            {"role": "user", "content": q},
        ],
        max_tokens=500,
    )
    if not reply:
        return []

    pairs = []
    blocks = [b.strip() for b in reply.split("\n\n") if b.strip()]
    for block in blocks:
        q_match = re.search(r"Q:\s*(.+)", block)
        a_match = re.search(r"A:\s*(.+)", block, re.DOTALL)
        if q_match and a_match:
            pairs.append({
                "question": q_match.group(1).strip(),
                "answer": a_match.group(1).strip(),
            })
    return pairs[:4]


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
def search(q: str = Query(..., min_length=1), limit: int = 10, page: int = 1, safe: bool = True):
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
                "nsfw": is_flagged_nsfw(r["title"], r["url"]) if safe else False,
            })

        # Always try SearXNG too (when configured) — our own crawled index
        # is small, so real web results are shown alongside it rather than
        # only as a last resort. Each result is tagged with where it came
        # from so the frontend can label sections separately.
        web_results = []
        for r in searxng_fallback(q, limit=limit, page=page, safe=safe):
            r["source"] = "web"
            r["nsfw"] = is_flagged_nsfw(r.get("title"), r.get("url")) if safe else False
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
        paa = people_also_ask(q) if page == 1 else []

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
        "people_also_ask": paa,
    }


@app.post("/jarvis")
def jarvis(payload: dict):
    """
    Jarvis — a conversational AI assistant (Gemini-style "keep chatting"
    box). Expects JSON body: {"query": "...", "history": [{"role": "user"|
    "assistant", "content": "..."}]}. History lets the frontend keep the
    thread going across turns, same as Gemini's follow-up chat.
    Returns {"reply": "..."} or an error message if GROQ_API_KEY isn't set.
    """
    query = (payload or {}).get("query", "").strip()
    history = (payload or {}).get("history", [])
    if not query:
        return {"reply": None, "error": "empty query"}

    if not GROQ_API_KEY:
        return {
            "reply": None,
            "error": "Jarvis isn't configured yet — set GROQ_API_KEY on the API service.",
        }

    messages = [
        {
            "role": "system",
            "content": (
                "You are Jarvis, a helpful, concise AI assistant embedded in "
                "Forge-Search, a self-hosted search engine. Answer clearly and "
                "directly. Keep responses focused — a few sentences to a short "
                "paragraph unless the user asks for more detail."
            ),
        }
    ]
    # Only pass through well-formed history entries — never trust client
    # input blindly, even though this is a low-stakes personal project.
    for turn in history[-10:]:  # cap context to the last 10 turns
        role = turn.get("role")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": query})

    reply = groq_chat(messages, max_tokens=800)
    if reply is None:
        return {"reply": None, "error": "Jarvis request failed — check the API logs."}
    return {"reply": reply}


@app.get("/search/images")
def search_images(q: str = Query(..., min_length=1), limit: int = 20, page: int = 1, safe: bool = True):
    results = searxng_category(q, "images", limit, page, safe=safe)
    for r in results:
        r["nsfw"] = is_flagged_nsfw(r.get("title"), r.get("url")) if safe else False
    return {"query": q, "page": page, "has_more": len(results) >= limit, "results": results}


@app.get("/search/news")
def search_news(q: str = Query(..., min_length=1), limit: int = 20, page: int = 1, safe: bool = True):
    results = searxng_category(q, "news", limit, page, safe=safe)
    for r in results:
        r["nsfw"] = is_flagged_nsfw(r.get("title"), r.get("url")) if safe else False
    return {"query": q, "page": page, "has_more": len(results) >= limit, "results": results}


@app.get("/search/videos")
def search_videos(q: str = Query(..., min_length=1), limit: int = 20, page: int = 1, safe: bool = True):
    results = searxng_category(q, "videos", limit, page, safe=safe)
    for r in results:
        r["nsfw"] = is_flagged_nsfw(r.get("title"), r.get("url")) if safe else False
    return {"query": q, "page": page, "has_more": len(results) >= limit, "results": results}


@app.get("/search/shopping")
def search_shopping(q: str = Query(..., min_length=1), limit: int = 20, page: int = 1, safe: bool = True):
    """
    Forge Shopping — product listings from eBay (the only real, working
    product-search engine wired into your SearXNG instance; see the note
    in searxng/settings.yml for why not Amazon/others). Results link out
    to the real eBay listing to complete any purchase — this does not
    process payments or checkouts itself.
    """
    results = searxng_category(q, "shopping", limit, page, safe=safe)
    for r in results:
        r["nsfw"] = is_flagged_nsfw(r.get("title"), r.get("url")) if safe else False
    return {"query": q, "page": page, "has_more": len(results) >= limit, "results": results}


@app.get("/knowledge")
def knowledge(q: str = Query(..., min_length=1)):
    return {"query": q, "panel": knowledge_panel(q)}


@app.post("/fetch")
def fetch_url(url: str):
    """Manually trigger the on-demand crawler for a single URL."""
    return fetch_and_index(url)
