"""
2. SPECIALIZED CRAWLER
Deeper, niche-focused extraction. For "tech blogs / dev docs" this means
pulling structured signals generic crawling misses: author, publish date,
tags, code-block languages, OpenGraph/schema.org metadata.

This crawler re-visits pages already in the index and enriches them,
rather than doing broad discovery.

Run:
    python crawler/specialized.py
"""
import sys
import os
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup
from db.connection import get_connection
from crawler.common import fetch

MAX_PAGES_PER_RUN = 30


def extract_structured(html: str):
    soup = BeautifulSoup(html, "html.parser")

    author = None
    author_tag = soup.find("meta", attrs={"name": "author"}) or soup.find(
        "meta", attrs={"property": "article:author"}
    )
    if author_tag and author_tag.get("content"):
        author = author_tag["content"].strip()

    published = None
    date_tag = soup.find("meta", attrs={"property": "article:published_time"}) or soup.find(
        "time"
    )
    if date_tag:
        published = date_tag.get("datetime") or date_tag.get("content") or date_tag.get_text(strip=True)

    code_block_count = len(soup.find_all(["pre", "code"]))

    langs = set()
    for tag in soup.find_all(class_=re.compile(r"language-(\w+)")):
        classes = " ".join(tag.get("class", []))
        m = re.search(r"language-(\w+)", classes)
        if m:
            langs.add(m.group(1))

    return {
        "author": author,
        "published": published,
        "code_block_count": code_block_count,
        "languages": sorted(langs),
    }


def run():
    conn = get_connection()

    rows = conn.execute(
        "SELECT id, url FROM pages ORDER BY fetched_at ASC LIMIT ?",
        (MAX_PAGES_PER_RUN,),
    ).fetchall()

    enriched = 0
    for row in rows:
        url = row["url"]
        print(f"[specialized] {url}")
        _, html = fetch(url)
        if not html:
            continue

        meta = extract_structured(html)

        summary = (
            f"By {meta['author'] or 'unknown'}"
            + (f", published {meta['published']}" if meta["published"] else "")
            + (f" — {meta['code_block_count']} code blocks" if meta["code_block_count"] else "")
        )

        conn.execute(
            """
            UPDATE pages SET description = COALESCE(NULLIF(description, ''), ?)
            WHERE id = ?
            """,
            (summary, row["id"]),
        )
        conn.commit()
        enriched += 1

    conn.close()
    print(f"[done] enriched {enriched} pages this run")


if __name__ == "__main__":
    run()
