# Forge-Search

A from-scratch search engine MVP — no Google APIs, no third-party search
indices, no external database server. Just Python + SQLite. Scoped to a
**tech blogs / dev docs** niche as seed data (easy to change — see below).

## Architecture

```
crawler/
  common.py        shared fetch/parse/robots.txt logic
  primary.py        1. Primary crawler — broad BFS link discovery
  specialized.py     2. Specialized crawler — niche metadata enrichment
  ondemand.py         3. On-demand fetcher — single-URL fresh fetch, callable from the API
db/
  schema.sql        SQLite schema (FTS5 full-text search built in)
  connection.py     connection helper — creates the .db file automatically
api/
  main.py           FastAPI /search endpoint with bm25 ranking
frontend/
  index.html        minimal search UI, no build step needed
```

No database server to install. The whole index is a single file:
`forge_search.db`, created automatically the first time you run anything.

## Local setup

1. **Python deps**:
   ```bash
   python -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Run the primary crawler** (populates the index from seed URLs in
   `crawler/primary.py`, creates `forge_search.db` on first run):
   ```bash
   python crawler/primary.py
   ```
   Run it a few times — each run crawls up to `MAX_PAGES_PER_RUN` (50) pages
   and queues newly discovered links for the next run.

3. **(Optional) Run the specialized crawler** to enrich already-indexed pages:
   ```bash
   python crawler/specialized.py
   ```

4. **Start the API**:
   ```bash
   uvicorn api.main:app --reload --port 8000
   ```

5. **Open the frontend**: just open `frontend/index.html` in a browser
   (it talks to `http://localhost:8000` by default).

That's it — no createdb, no connection strings, no server to run alongside it.

## Deploying

- **Render**: `render.yaml` runs the API as a plain Python web service.
  Important: Render's free-tier filesystem is **ephemeral** — `forge_search.db`
  gets wiped on every deploy/restart. Fine for demos; if you want the index
  to persist, add a paid Render Disk and point `DB_PATH` at it (commented
  example in `render.yaml`).
- **GitHub Actions**: `.github/workflows/crawl.yml` runs the primary +
  specialized crawlers every 6 hours and commits the updated `forge_search.db`
  back to the repo — so your index is versioned in git itself. No secrets
  or external DB needed.

## Changing the niche

Edit `SEED_URLS` in `crawler/primary.py`. The specialized crawler's
extraction logic (`crawler/specialized.py`) is written for tech-blog
signals (author, publish date, code blocks) — swap it out for whatever
structured fields matter in your new niche.

## How ranking works

SQLite's FTS5 extension does full-text indexing and scores relevance with
`bm25()` (a well-established ranking algorithm, the same family used in
Elasticsearch/Lucene). We add a small boost from `inbound_links` — a simple
authority signal built from the link graph the primary crawler records in
the `links` table. More real inbound links from other crawled pages = ranks
higher. This is a genuine, if simplified, version of text relevance +
link-graph authority — the same idea PageRank started from.

## Not included (intentionally, for MVP scope)

- **Ads system**: auction/targeting/billing is its own large project.
- **Distributed crawling**: single-process crawlers are fine up to tens of
  thousands of pages.
- **Concurrent write safety**: SQLite handles one writer at a time well
  enough for an MVP; if you outgrow that, Postgres is the natural upgrade
  and the schema/queries translate closely.
- **Spam/quality filtering**: the MVP indexes whatever it's told to crawl.
