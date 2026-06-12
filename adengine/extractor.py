"""Step 1 of the pipeline: scraped site -> BrandDNA."""
from __future__ import annotations

from adengine.llm import LLMClient
from adengine.prompts import load_prompt
from adengine.scraper import ScrapedSite, build_corpus

MAX_CORPUS_CHARS = 120_000  # ~30k tokens


def extract_brand_dna(site: ScrapedSite, llm: LLMClient) -> "BrandDNA":
    from adengine.schemas import BrandDNA

    system = load_prompt("extract_brand_dna")
    corpus = build_corpus(site, max_chars=MAX_CORPUS_CHARS)
    user = (
        f"Website: {site.url}\n\n"
        "<site_content>\n"
        f"{corpus}\n"
        "</site_content>\n\n"
        "Extract the Brand DNA from the site content above."
    )
    dna = llm.structured(
        BrandDNA,
        system=system,
        user=user,
        name="extract_brand_dna",
        max_tokens=4096,
    )
    dna.source_url = site.url
    return dna
