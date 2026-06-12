"""Fetch and clean site content, multi-page.

Follows up to 5 internal pages: home plus pricing / product / about / contact
style pages, picked by a keyword heuristic over the home page's links.
"""
from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

USER_AGENT = (
    "Mozilla/5.0 (compatible; AdEngineBot/0.1; +https://github.com/biggjuann/flow)"
)

# Ordered by priority: pricing first (most ad-relevant), then product, about, contact.
PAGE_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("pricing", ("pricing", "plans", "price", "packages")),
    ("product", ("product", "services", "features", "solutions", "shop", "menu", "store")),
    ("about", ("about", "company", "team", "story", "who-we-are")),
    ("contact", ("contact", "book", "demo", "quote", "get-started")),
]

_STRIP_TAGS = ("script", "style", "noscript", "svg", "iframe", "template", "form")


class Page(BaseModel):
    url: str
    title: str = ""
    text: str = ""


class ScrapedSite(BaseModel):
    url: str
    domain: str
    pages: list[Page] = Field(default_factory=list)


class ScrapeError(RuntimeError):
    pass


def _clean_text(soup: BeautifulSoup) -> str:
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    # Collapse runs of blank lines / intra-line whitespace.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _parse_page(url: str, html: str) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    return Page(url=url, title=title, text=_clean_text(soup))


def _internal_links(base_url: str, html: str) -> list[tuple[str, str]]:
    """Return (absolute_url, anchor_text) for same-domain links, deduped, in order."""
    soup = BeautifulSoup(html, "html.parser")
    base_netloc = urlparse(base_url).netloc.removeprefix("www.")
    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = urldefrag(urljoin(base_url, a["href"]))[0]
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.removeprefix("www.") != base_netloc:
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append((href, a.get_text(" ", strip=True).lower()))
    return links


def _pick_pages(home_url: str, links: list[tuple[str, str]], max_extra: int) -> list[str]:
    """Heuristic: one URL per hint category, in priority order."""
    home = urldefrag(home_url)[0].rstrip("/")
    picked: list[str] = []
    for _category, keywords in PAGE_HINTS:
        for href, anchor in links:
            if href.rstrip("/") == home or href in picked:
                continue
            haystack = f"{urlparse(href).path.lower()} {anchor}"
            if any(kw in haystack for kw in keywords):
                picked.append(href)
                break
        if len(picked) >= max_extra:
            break
    return picked[:max_extra]


def scrape_site(
    url: str,
    max_pages: int = 5,
    client: httpx.Client | None = None,
) -> ScrapedSite:
    """Fetch the home page plus up to ``max_pages - 1`` heuristic-picked internal pages."""
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    own_client = client is None
    client = client or httpx.Client(
        follow_redirects=True,
        timeout=15.0,
        headers={"User-Agent": USER_AGENT},
    )
    try:
        try:
            response = client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ScrapeError(f"Failed to fetch {url}: {exc}") from exc

        home_url = str(response.url)
        home_html = response.text
        domain = urlparse(home_url).netloc.removeprefix("www.")
        pages = [_parse_page(home_url, home_html)]

        links = _internal_links(home_url, home_html)
        for extra_url in _pick_pages(home_url, links, max_pages - 1):
            try:
                extra = client.get(extra_url)
                extra.raise_for_status()
            except httpx.HTTPError:
                continue  # secondary pages are best-effort
            pages.append(_parse_page(str(extra.url), extra.text))

        return ScrapedSite(url=home_url, domain=domain, pages=pages)
    finally:
        if own_client:
            client.close()


def build_corpus(site: ScrapedSite, max_chars: int = 120_000) -> str:
    """Concatenate page text for the extractor, prioritizing home + pricing.

    ~120k chars ≈ 30k tokens, the extraction input cap from the spec.
    """
    def priority(index_page: tuple[int, Page]) -> tuple[int, int]:
        i, page = index_page
        if i == 0:
            return (0, i)  # home first
        path = urlparse(page.url).path.lower()
        if any(kw in path for kw in PAGE_HINTS[0][1]):
            return (1, i)  # pricing second
        return (2, i)

    ordered = [p for _, p in sorted(enumerate(site.pages), key=priority)]
    sections = []
    for page in ordered:
        header = f"## PAGE: {page.url}" + (f" — {page.title}" if page.title else "")
        sections.append(f"{header}\n{page.text}")
    corpus = "\n\n".join(sections)
    return corpus[:max_chars]
