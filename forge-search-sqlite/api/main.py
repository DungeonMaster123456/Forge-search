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


@app.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = 10):
    conn = get_connection()
    try:
        # FTS5 MATCH + bm25() ranking (lower bm25 score = more relevant, so we
        # negate it for a "higher is better" ordering). We also fold in our
        # inbound_links authority signal, same idea as the Postgres version.
        rows = conn.execute(
            """
            SELECT p.url, p.title, p.description, p.body_text, p.inbound_links,
                   bm25(pages_fts) AS bm25_score
            FROM pages_fts
            JOIN pages p ON p.id = pages_fts.rowid
            WHERE pages_fts MATCH ?
            ORDER BY (bm25(pages_fts) * -1) + (MIN(p.inbound_links, 20) * 0.05) DESC
            LIMIT ?
            """,
            (q, limit),
        ).fetchall()

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

        conn.execute(
            "INSERT INTO search_log (query, result_count) VALUES (?, ?)",
            (q, len(results)),
        )
        conn.commit()
    finally:
        conn.close()

    return {"query": q, "count": len(results), "results": results}


@app.post("/fetch")
def fetch_url(url: str):
    """Manually trigger the on-demand crawler for a single URL."""
    return fetch_and_index(url)
