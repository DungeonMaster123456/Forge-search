"""
Shared utilities used by all three crawler types:
  1. Primary crawler   (crawler/primary.py)
  2. Specialized crawler (crawler/specialized.py)
  3. On-demand fetcher (crawler/ondemand.py)

Handles: robots.txt compliance, polite fetching, HTML parsing, content hashing.
"""
import hashlib
import time
import urllib.robotparser as robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = "ForgeSearchBot/0.1 (+https://forge-search.example.com/bot)"
REQUEST_TIMEOUT = 10
DEFAULT_CRAWL_DELAY = 1.0  # seconds, polite default if robots.txt doesn't specify

_robots_cache = {}  # domain -> RobotFileParser


def get_robot_parser(url: str):
    """Fetch and cache robots.txt for the given URL's domain."""
    parsed = urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    if domain in _robots_cache:
        return _robots_cache[domain]

    rp = robotparser.RobotFileParser()
    rp.set_url(urljoin(domain, "/robots.txt"))
    try:
        rp.read()
    except Exception:
        # If robots.txt is unreachable, be conservative but don't hard-fail —
        # treat as "allow" per common convention, but log it.
        pass
    _robots_cache[domain] = rp
    return rp


def is_allowed(url: str) -> bool:
    rp = get_robot_parser(url)
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def get_crawl_delay(url: str) -> float:
    rp = get_robot_parser(url)
    try:
        delay = rp.crawl_delay(USER_AGENT)
        if delay:
            return float(delay)
    except Exception:
        pass
    return DEFAULT_CRAWL_DELAY


def fetch(url: str):
    """
    Fetch a URL politely, respecting robots.txt.
    Returns (status_code, html_text) or (None, None) if disallowed/failed.
    """
    if not is_allowed(url):
        print(f"[skip] disallowed by robots.txt: {url}")
        return None, None

    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        return resp.status_code, resp.text
    except requests.RequestException as e:
        print(f"[error] fetch failed for {url}: {e}")
        return None, None
    finally:
        time.sleep(get_crawl_delay(url))


def parse_page(url: str, html: str):
    """
    Extract structured fields from raw HTML.
    Returns a dict: title, description, body_text, canonical_url, links[]
    """
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None

    desc_tag = soup.find("meta", attrs={"name": "description"})
    description = desc_tag["content"].strip() if desc_tag and desc_tag.get("content") else None

    canonical_tag = soup.find("link", attrs={"rel": "canonical"})
    canonical_url = canonical_tag["href"] if canonical_tag and canonical_tag.get("href") else url

    # Strip script/style/nav/footer before extracting body text
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    body_text = " ".join(soup.get_text(separator=" ").split())
    # Cap stored body text length to keep the DB sane for an MVP
    body_text = body_text[:20000]

    links = []
    for a in soup.find_all("a", href=True):
        absolute = urljoin(url, a["href"])
        parsed = urlparse(absolute)
        if parsed.scheme in ("http", "https"):
            # Drop fragments/query junk for cleaner dedup
            clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            links.append(clean)

    return {
        "title": title,
        "description": description,
        "canonical_url": canonical_url,
        "body_text": body_text,
        "links": list(set(links)),
    }


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def get_domain(url: str) -> str:
    return urlparse(url).netloc
