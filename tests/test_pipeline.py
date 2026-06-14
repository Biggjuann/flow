import json

import pytest

from adengine import pipeline
from adengine.schemas import AdBatch, ReviewBatch
from tests.conftest import make_ad
from tests.test_extractor import FakeLLM
from tests.test_scorer import make_review


@pytest.fixture
def scrape_stub(monkeypatch, brand_dna):
    from adengine.scraper import Page, ScrapedSite

    site = ScrapedSite(
        url="https://acmecoffee.example/",
        domain="acmecoffee.example",
        pages=[Page(url="https://acmecoffee.example/", title="Home", text="beans")],
    )
    calls = []

    def fake_scrape(url, **kwargs):
        calls.append(url)
        return site

    monkeypatch.setattr(pipeline, "scrape_site", fake_scrape)
    return calls


def full_run_llm(brand_dna):
    ads_a = [make_ad(i) for i in range(1, 9)]
    ads_b = [make_ad(i) for i in range(9, 17)]
    reviews = [
        ReviewBatch(reviews=[make_review(f"ad_{i:02d}") for i in range(start, min(start + 5, 17))])
        for start in range(1, 17, 5)
    ]
    return FakeLLM(
        [brand_dna.model_copy(update={"source_url": None}), AdBatch(ads=ads_a), AdBatch(ads=ads_b)]
        + reviews
    )


def test_full_pipeline_writes_all_artifacts(tmp_path, brand_dna, scrape_stub):
    llm = full_run_llm(brand_dna)
    run_dir = pipeline.new_run_dir(tmp_path, "https://acmecoffee.example/")
    assert run_dir.name.endswith("-acmecoffee.example")

    pipeline.run_pipeline("https://acmecoffee.example/", run_dir, llm=llm)

    for artifact in ("brand_dna.json", "ads.json", "scored_package.json", "report.md", "status.json"):
        assert (run_dir / artifact).exists(), artifact

    status = json.loads((run_dir / "status.json").read_text())
    assert status["state"] == "completed"
    assert status["launch_ready"] + status["blocked"] == 16

    package = pipeline.load_scored_package(run_dir)
    assert package.total_ads == 16


def test_pipeline_is_resumable(tmp_path, brand_dna, scrape_stub):
    run_dir = pipeline.new_run_dir(tmp_path, "https://acmecoffee.example/")
    pipeline.run_pipeline("https://acmecoffee.example/", run_dir, llm=full_run_llm(brand_dna))
    assert len(scrape_stub) == 1

    # Second run: all artifacts exist -> no scraping, no LLM calls at all.
    exhausted = FakeLLM([])
    pipeline.run_pipeline("https://acmecoffee.example/", run_dir, llm=exhausted)
    assert len(scrape_stub) == 1  # not re-scraped
    assert exhausted.calls == []

    # force=True re-runs everything.
    pipeline.run_pipeline(
        "https://acmecoffee.example/", run_dir, llm=full_run_llm(brand_dna), force=True
    )
    assert len(scrape_stub) == 2


def test_pipeline_records_failure(tmp_path, brand_dna, scrape_stub):
    class ExplodingLLM:
        model = "boom"

        def structured(self, *args, **kwargs):
            raise RuntimeError("kaboom")

    run_dir = pipeline.new_run_dir(tmp_path, "https://acmecoffee.example/")
    with pytest.raises(RuntimeError, match="kaboom"):
        pipeline.run_pipeline("https://acmecoffee.example/", run_dir, llm=ExplodingLLM())
    status = pipeline.read_status(run_dir)
    assert status["state"] == "failed"
    assert "kaboom" in status["error"]
