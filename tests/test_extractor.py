from adengine.extractor import extract_brand_dna
from adengine.scraper import Page, ScrapedSite


class FakeLLM:
    """Stands in for LLMClient: records calls, returns canned objects."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.model = "fake-model"

    def structured(self, schema, **kwargs):
        self.calls.append({"schema": schema, **kwargs})
        result = self.results.pop(0)
        assert isinstance(result, schema)
        return result


def test_extract_brand_dna(brand_dna):
    site = ScrapedSite(
        url="https://acmecoffee.example/",
        domain="acmecoffee.example",
        pages=[
            Page(url="https://acmecoffee.example/", title="Home", text="Fresh beans"),
            Page(url="https://acmecoffee.example/pricing", title="Pricing", text="$24/mo"),
        ],
    )
    llm = FakeLLM([brand_dna.model_copy(update={"source_url": None})])
    dna = extract_brand_dna(site, llm)

    assert dna.source_url == "https://acmecoffee.example/"  # set by pipeline
    call = llm.calls[0]
    assert call["name"] == "extract_brand_dna"
    assert "Brand DNA Extractor" in call["system"]
    assert "<site_content>" in call["user"]
    assert "$24/mo" in call["user"]
