import json

import httpx
import pytest

from adengine.launcher import (
    CampaignObjective,
    CampaignSpec,
    MetaAPIError,
    MetaConfigError,
    MetaLauncher,
    OptimizationGoal,
    build_plan,
    tag_destination,
    _parse_age_range,
)
from adengine.schemas import ConversionPath, ScoredAd, ScoredPackage, Verdict
from adengine.scorer import composite_score, gate
from tests.test_scorer import make_review


def make_package(brand_dna, ads, ready_count=3, url="https://acme.example"):
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
        source_url=url,
        business_name=brand_dna.business_name,
        model="test",
        total_ads=len(scored),
        launch_ready_count=ready_count,
        blocked_count=len(scored) - ready_count,
        scored_ads=scored,
    )


# ------------------------------------------------------------------ build_plan

def test_build_plan_traffic_without_pixel(brand_dna, ads):
    package = make_package(brand_dna, ads, ready_count=3)
    plan = build_plan(package, ConversionPath.purchase, 2000, "https://acme.example")
    assert plan.campaign.objective == CampaignObjective.OUTCOME_TRAFFIC
    assert plan.adsets[0].optimization_goal == OptimizationGoal.LINK_CLICKS
    assert plan.adsets[0].promoted_object is None
    assert plan.adsets[0].ad_ids == ["ad_01", "ad_02", "ad_03"]
    assert any("META_PIXEL_ID" in n for n in plan.notes)


def test_build_plan_conversion_optimized_with_pixel(brand_dna, ads):
    package = make_package(brand_dna, ads, ready_count=2)
    plan = build_plan(package, ConversionPath.purchase, 5000, "https://acme.example", pixel_id="px_1")
    assert plan.campaign.objective == CampaignObjective.OUTCOME_SALES
    assert plan.adsets[0].optimization_goal == OptimizationGoal.OFFSITE_CONVERSIONS
    assert plan.adsets[0].promoted_object == {"pixel_id": "px_1", "custom_event_type": "PURCHASE"}


def test_build_plan_lead_maps_to_lead_event(brand_dna, ads):
    package = make_package(brand_dna, ads, ready_count=2)
    plan = build_plan(package, ConversionPath.lead_form, 3000, "https://acme.example", pixel_id="px")
    assert plan.campaign.objective == CampaignObjective.OUTCOME_LEADS
    assert plan.adsets[0].promoted_object["custom_event_type"] == "LEAD"


def test_build_plan_app_install_with_app_id(brand_dna, ads):
    store = "https://apps.apple.com/us/app/acme/id123"
    package = make_package(brand_dna, ads, ready_count=2, url=store)
    plan = build_plan(package, ConversionPath.app_install, 4000, store, app_id="fb_app_9")
    assert plan.campaign.objective == CampaignObjective.OUTCOME_APP_PROMOTION
    assert plan.adsets[0].optimization_goal == OptimizationGoal.APP_INSTALLS
    assert plan.adsets[0].promoted_object == {"application_id": "fb_app_9", "object_store_url": store}
    assert all(c.cta_type == "INSTALL_MOBILE_APP" for c in plan.creatives)


def test_build_plan_app_detected_from_store_url(brand_dna, ads):
    store = "https://apps.apple.com/us/app/acme/id123"
    package = make_package(brand_dna, ads, ready_count=2, url=store)
    # even with a web conversion_path, an App Store destination is treated as an app
    plan = build_plan(package, ConversionPath.purchase, 4000, store)
    assert plan.campaign.objective == CampaignObjective.OUTCOME_TRAFFIC
    assert any("META_APP_ID" in n for n in plan.notes)


def test_build_plan_no_ready_ads_raises(brand_dna, ads):
    package = make_package(brand_dna, ads, ready_count=0)
    with pytest.raises(MetaConfigError, match="No launch-ready ads"):
        build_plan(package, ConversionPath.purchase, 2000, "https://acme.example")


def test_parse_age_range():
    assert _parse_age_range("25-44") == (25, 44)
    assert _parse_age_range("garbage") == (18, 65)
    assert _parse_age_range("13-90") == (18, 65)


def test_tag_destination_web_vs_appstore():
    web = tag_destination("https://acme.example/buy", "AdEngine — Acme", "ad_01")
    assert "utm_source=facebook" in web and "utm_content=ad_01" in web
    app = tag_destination("https://apps.apple.com/app/id1", "AdEngine — Acme", "ad_01")
    assert "ct=" in app and "utm_source" not in app


# ---------------------------------------------------------------- MetaLauncher

class GraphFake:
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
        if path.endswith("/adimages"):
            self.posts.append((path, {}))
            return httpx.Response(200, json={"images": {"f.png": {"hash": "IMGHASH"}}})
        body = dict(httpx.QueryParams(request.content.decode()))
        self.posts.append((path, body))
        if "fail" in body.get("name", ""):
            return httpx.Response(400, json={"error": {"message": "Invalid parameter"}})
        self.counter += 1
        return httpx.Response(200, json={"id": f"meta_{self.counter}"})


@pytest.fixture
def graph():
    return GraphFake()


def make_launcher(graph, **kw):
    client = httpx.Client(
        transport=httpx.MockTransport(graph.handler), base_url="https://graph.test/v21.0"
    )
    return MetaLauncher("token", "1234567890", "page_1", client=client, **kw)


def test_account_id_normalized(graph):
    assert make_launcher(graph).ad_account_id == "act_1234567890"


def test_launch_creates_everything_paused(brand_dna, ads, graph):
    package = make_package(brand_dna, ads, ready_count=3)
    plan = build_plan(package, ConversionPath.purchase, 2000, "https://acme.example", pixel_id="px_1")
    result = make_launcher(graph).launch(plan)

    assert result.campaign_id == "meta_1"
    assert result.objective == "OUTCOME_SALES"
    assert set(result.ad_ids) == {"ad_01", "ad_02", "ad_03"}
    assert result.status == "PAUSED"
    assert result.images_generated == 0  # no provider

    paths = [p for p, _ in graph.posts]
    assert paths.count("/v21.0/act_1234567890/campaigns") == 1
    assert paths.count("/v21.0/act_1234567890/adsets") == 1
    assert paths.count("/v21.0/act_1234567890/adcreatives") == 3
    assert paths.count("/v21.0/act_1234567890/ads") == 3

    for path, body in graph.posts:
        if path.endswith(("/campaigns", "/adsets", "/ads")):
            assert body["status"] == "PAUSED", path

    adset_body = next(b for p, b in graph.posts if p.endswith("/adsets"))
    assert adset_body["daily_budget"] == "2000"
    assert json.loads(adset_body["promoted_object"]) == {"pixel_id": "px_1", "custom_event_type": "PURCHASE"}
    targeting = json.loads(adset_body["targeting"])
    assert targeting["flexible_spec"][0]["interests"][0]["id"] == "601"

    creative_body = next(b for p, b in graph.posts if p.endswith("/adcreatives"))
    spec = json.loads(creative_body["object_story_spec"])
    assert spec["page_id"] == "page_1"
    assert "utm_content=ad_01" in spec["link_data"]["link"]
    assert "image_hash" not in spec["link_data"]


def test_launch_generates_and_uploads_images(brand_dna, ads, graph):
    package = make_package(brand_dna, ads, ready_count=2)
    plan = build_plan(package, ConversionPath.purchase, 2000, "https://acme.example")
    launcher = make_launcher(graph, image_provider=lambda prompt: b"PNGBYTES")
    result = launcher.launch(plan)

    assert result.images_generated == 2
    assert any(p.endswith("/adimages") for p, _ in graph.posts)
    creative_body = next(b for p, b in graph.posts if p.endswith("/adcreatives"))
    spec = json.loads(creative_body["object_story_spec"])
    assert spec["link_data"]["image_hash"] == "IMGHASH"


def test_image_provider_failure_falls_back(brand_dna, ads, graph):
    package = make_package(brand_dna, ads, ready_count=1)
    plan = build_plan(package, ConversionPath.purchase, 2000, "https://acme.example")

    def boom(prompt):
        raise RuntimeError("provider down")

    result = make_launcher(graph, image_provider=boom).launch(plan)
    assert result.images_generated == 0
    assert len(result.ad_ids) == 1  # still launched


def test_graph_error_surfaces(graph):
    with pytest.raises(MetaAPIError, match="Invalid parameter"):
        make_launcher(graph).create_campaign(
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
    monkeypatch.setenv("META_PIXEL_ID", "px_9")
    launcher = MetaLauncher.from_env()
    assert launcher.ad_account_id == "act_123"
    assert launcher.pixel_id == "px_9"
