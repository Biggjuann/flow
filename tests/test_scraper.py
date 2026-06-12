from pathlib import Path

import httpx
import pytest

from adengine.scraper import ScrapeError, build_corpus, scrape_site

FIXTURES = Path(__file__).parent / "fixtures"


def site_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path in ("", "/"):
        return httpx.Response(200, text=(FIXTURES / "home.html").read_text())
    if path == "/pricing":
        return httpx.Response(200, text=(FIXTURES / "pricing.html").read_text())
    if path == "/shop":
        return httpx.Response(200, text="<html><title>Shop</title><body>All our beans</body></html>")
    if path == "/about-us":
        return httpx.Response(200, text="<html><title>About</title><body>Founded in 2019</body></html>")
    if path == "/contact":
        return httpx.Response(500)  # secondary pages are best-effort
    return httpx.Response(404)


@pytest.fixture
def mock_client():
    return httpx.Client(
        transport=httpx.MockTransport(site_handler), base_url="https://acmecoffee.example"
    )


def test_scrape_picks_pages_by_heuristic(mock_client):
    site = scrape_site("https://acmecoffee.example/", client=mock_client)
    urls = [p.url for p in site.pages]
    assert site.domain == "acmecoffee.example"
    # home first, then pricing > shop > about; contact 500s and is skipped
    assert urls[0] == "https://acmecoffee.example/"
    assert urls[1] == "https://acmecoffee.example/pricing"
    assert "https://acmecoffee.example/shop" in urls
    assert "https://acmecoffee.example/about-us" in urls
    assert all("instagram.com" not in u for u in urls)
    assert all("/blog/" not in u for u in urls)
    assert len(site.pages) <= 5


def test_text_is_cleaned(mock_client):
    site = scrape_site("https://acmecoffee.example/", client=mock_client)
    home = site.pages[0]
    assert home.title == "Acme Coffee Club — Fresh beans monthly"
    assert "Roast-date guaranteed" in home.text
    assert "console.log" not in home.text  # scripts stripped
    assert ".hero" not in home.text  # styles stripped


def test_corpus_prioritizes_home_then_pricing(mock_client):
    site = scrape_site("https://acmecoffee.example/", client=mock_client)
    corpus = build_corpus(site)
    home_pos = corpus.index("## PAGE: https://acmecoffee.example/ —")
    pricing_pos = corpus.index("## PAGE: https://acmecoffee.example/pricing")
    assert home_pos < pricing_pos
    assert "$24/mo" in corpus


def test_corpus_respects_char_cap(mock_client):
    site = scrape_site("https://acmecoffee.example/", client=mock_client)
    assert len(build_corpus(site, max_chars=100)) == 100


def test_scheme_added_and_home_failure_raises():
    def fail(request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https"
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(fail))
    with pytest.raises(ScrapeError, match="Failed to fetch"):
        scrape_site("acmecoffee.example", client=client)
