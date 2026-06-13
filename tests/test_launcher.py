import json

import httpx
import pytest

from adengine.launcher import (
    MetaAPIError,
    MetaConfigError,
    MetaLauncher,
    build_plan,
    _parse_age_range,
)
from adengine.schemas import ScoredAd, ScoredPackage, Verdict
from adengine.scorer import composite_score, gate
from tests.test_scorer import make_review


def make_package(brand_dna, ads, ready_count=3):
    scored = []
    for i, ad in enumerate(ads):
        review = (
            make_review(ad.id, score=9, policy=9)
            if i < ready_count
            else make_review(ad.id, score=4, policy=6)
        )
        scored.append(
            ScoredAd(ad=ad, review=review, composite=composite_score(review), verdict=gate(review))
        )
    return ScoredPackage(
        source_url=brand_dna.source_url,
        business_name=brand_dna.business_name,
        model="test",
        total_ads=len(scored),
        launch_ready_count=ready_count,
        blocked_count=len(scored) - ready_count,
        scored_ads=scored,
    )


# ------------------------------------------------------------------ build_plan

def test_build_plan_only_launch_ready(brand_dna, ads):
    package = make_package(brand_dna, ads, ready_count=3)
    plan = build_plan(package, daily_budget_cents=2000, destination_url="https://acme.example")

    assert plan.campaign.name == f"AdEngine — {brand_dna.business_name}"
    assert len(plan.adsets) == 1
    adset = plan.adsets[0]
    assert adset.daily_budget_cents == 2000
    assert adset.ad_ids == ["ad_01", "ad_02", "ad_03"]  # only the ready ones
    assert len(plan.creatives) == 3
    assert all(c.destination_url == "https://acme.example" for c in plan.creatives)
    # hook is prepended when primary_text doesn't already start with it
    assert plan.creatives[0].message.startswith(ads[0].hook)


def test_build_plan_no_ready_ads_raises(brand_dna, ads):
    package = make_package(brand_dna, ads, ready_count=0)
    with pytest.raises(MetaConfigError, match="No launch-ready ads"):
        build_plan(package, 2000, "https://acme.example")


def test_parse_age_range():
    assert _parse_age_range("25-44") == (25, 44)
    assert _parse_age_range("garbage") == (18, 65)
    assert _parse_age_range("13-90") == (18, 65)  # clamped


# ---------------------------------------------------------------- MetaLauncher

class GraphFake:
    """Mock Graph API: records POSTs, hands out sequential ids."""

    def __init__(self):
        self.posts = []
        self.counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/search"):
            q = request.url.params.get("q", "")
            if q == "Specialty coffee":
                return httpx.Response(200, json={"data": [{"id": "601", "name": q}]})
            return httpx.Response(200, json={"data": []})
        body = dict(httpx.QueryParams(request.content.decode()))
        self.posts.append((path, body))
        if "fail" in body.get("name", ""):
            return httpx.Response(
                400, json={"error": {"message": "Invalid parameter"}}
            )
        self.counter += 1
        return httpx.Response(200, json={"id": f"meta_{self.counter}"})


@pytest.fixture
def graph():
    return GraphFake()


@pytest.fixture
def launcher(graph):
    client = httpx.Client(
        transport=httpx.MockTransport(graph.handler), base_url="https://graph.test/v21.0"
    )
    return MetaLauncher("token", "1234567890", "page_1", client=client)


def test_account_id_normalized(launcher):
    assert launcher.ad_account_id == "act_1234567890"


def test_launch_creates_everything_paused(brand_dna, ads, launcher, graph):
    package = make_package(brand_dna, ads, ready_count=3)
    plan = build_plan(package, 2000, "https://acme.example")
    result = launcher.launch(plan)

    assert result.campaign_id == "meta_1"
    assert len(result.adset_ids) == 1
    assert set(result.ad_ids) == {"ad_01", "ad_02", "ad_03"}
    assert result.status == "PAUSED"

    paths = [p for p, _ in graph.posts]
    assert paths.count("/v21.0/act_1234567890/campaigns") == 1
    assert paths.count("/v21.0/act_1234567890/adsets") == 1
    assert paths.count("/v21.0/act_1234567890/adcreatives") == 3
    assert paths.count("/v21.0/act_1234567890/ads") == 3

    # every created object is PAUSED; nothing can spend
    for path, body in graph.posts:
        if "adcreatives" not in path:
            assert body["status"] == "PAUSED", path

    # adset carries budget + targeting with resolved interest + parsed ages
    adset_body = next(b for p, b in graph.posts if p.endswith("/adsets"))
    assert adset_body["daily_budget"] == "2000"
    targeting = json.loads(adset_body["targeting"])
    assert targeting["geo_locations"] == {"countries": ["US"]}
    assert targeting["age_min"] == 25 and targeting["age_max"] == 44
    assert targeting["flexible_spec"][0]["interests"][0]["id"] == "601"

    # creative carries the page + full copy
    creative_body = next(b for p, b in graph.posts if p.endswith("/adcreatives"))
    spec = json.loads(creative_body["object_story_spec"])
    assert spec["page_id"] == "page_1"
    assert spec["link_data"]["link"] == "https://acme.example"
    assert spec["link_data"]["call_to_action"]["type"] == "SHOP_NOW"


def test_graph_error_surfaces(launcher):
    from adengine.launcher import CampaignObjective, CampaignSpec

    with pytest.raises(MetaAPIError, match="Invalid parameter"):
        launcher.create_campaign(
            CampaignSpec(name="fail me", objective=CampaignObjective.OUTCOME_TRAFFIC)
        )


def test_from_env_requires_config(monkeypatch):
    for var in ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_PAGE_ID"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(MetaConfigError, match="META_ACCESS_TOKEN"):
        MetaLauncher.from_env()

    monkeypatch.setenv("META_ACCESS_TOKEN", "t")
    monkeypatch.setenv("META_AD_ACCOUNT_ID", "123")
    monkeypatch.setenv("META_PAGE_ID", "p")
    assert MetaLauncher.from_env().ad_account_id == "act_123"
