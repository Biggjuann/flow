import json

import pytest
from fastapi.testclient import TestClient

from adengine import api, pipeline


@pytest.fixture
def client(tmp_path, monkeypatch, brand_dna):
    monkeypatch.setattr(api, "RUNS_BASE", tmp_path)

    def instant_pipeline(url, run_dir, llm=None, force=False, on_step=None):
        (run_dir / pipeline.BRAND_DNA_FILE).write_text(brand_dna.model_dump_json())
        (run_dir / pipeline.REPORT_FILE).write_text("# fake report\n")
        pipeline.write_status(
            run_dir, state="completed", step="done", launch_ready=3, blocked=1
        )
        return run_dir

    monkeypatch.setattr(pipeline, "run_pipeline", instant_pipeline)

    class ImmediateExecutor:
        def submit(self, fn, *args):
            fn(*args)

    monkeypatch.setattr(api, "_executor", ImmediateExecutor())
    return TestClient(api.app)


def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert "api_key_configured" in body


def test_run_lifecycle(client):
    created = client.post("/runs", json={"url": "acmecoffee.example"})
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    assert run_id.endswith("-acmecoffee.example")

    status = client.get(f"/runs/{run_id}").json()
    assert status["state"] == "completed"
    assert status["launch_ready"] == 3
    assert "brand_dna" in status["artifacts"]
    assert "report" in status["artifacts"]

    dna = client.get(f"/runs/{run_id}/artifacts/brand_dna")
    assert dna.status_code == 200
    assert dna.json()["business_name"] == "Acme Coffee Club"

    report = client.get(f"/runs/{run_id}/report")
    assert report.status_code == 200
    assert report.text.startswith("# fake report")

    runs = client.get("/runs").json()
    assert [r["run_id"] for r in runs] == [run_id]


def test_missing_artifacts_404(client):
    run_id = client.post("/runs", json={"url": "https://x.example"}).json()["run_id"]
    assert client.get(f"/runs/{run_id}/artifacts/ads").status_code == 404
    assert client.get(f"/runs/{run_id}/artifacts/nope").status_code == 404


def test_run_id_path_traversal_rejected(client):
    assert client.get("/runs/..%2F..%2Fetc").status_code in (400, 404)
    assert client.get("/runs/.hidden").status_code == 400
    assert client.get("/runs/does-not-exist").status_code == 404


def test_index_serves_web_ui(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "AdEngine" in response.text
    assert "Build campaign" in response.text


# ----------------------------------------------------------------- Meta launch

def make_scored_package(brand_dna, ads):
    from adengine.schemas import ScoredAd, ScoredPackage
    from adengine.scorer import composite_score, gate
    from tests.test_scorer import make_review

    scored = []
    for i, ad in enumerate(ads[:4]):
        review = make_review(ad.id, score=9 if i < 3 else 4, policy=9 if i < 3 else 6)
        scored.append(
            ScoredAd(ad=ad, review=review, composite=composite_score(review), verdict=gate(review))
        )
    return ScoredPackage(
        source_url=brand_dna.source_url,
        business_name=brand_dna.business_name,
        model="test",
        total_ads=4,
        launch_ready_count=3,
        blocked_count=1,
        scored_ads=scored,
    )


def test_launch_endpoint(client, monkeypatch, brand_dna, ads):
    from adengine import api as api_module
    from adengine.launcher import LaunchResult

    run_id = client.post("/runs", json={"url": "acmecoffee.example"}).json()["run_id"]
    run_dir = api_module.RUNS_BASE / run_id
    (run_dir / pipeline.SCORED_FILE).write_text(
        make_scored_package(brand_dna, ads).model_dump_json()
    )

    captured = {}

    class FakeLauncher:
        ad_account_id = "act_999"
        pixel_id = None
        app_id = None

        def launch(self, plan):
            captured["plan"] = plan
            return LaunchResult(
                campaign_id="cmp_1",
                objective=plan.campaign.objective.value,
                adset_ids=["as_1"],
                ad_ids={aid: f"meta_{aid}" for aid in plan.adsets[0].ad_ids},
            )

    monkeypatch.setattr(
        api_module.meta_launcher.MetaLauncher, "from_env", classmethod(lambda cls: FakeLauncher())
    )

    response = client.post(f"/runs/{run_id}/launch", json={"daily_budget_usd": 50})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["campaign_id"] == "cmp_1"
    assert body["status"] == "PAUSED"
    assert body["objective"] == "OUTCOME_TRAFFIC"  # purchase path, no pixel -> traffic
    assert "act=999" in body["ads_manager_url"]
    assert captured["plan"].adsets[0].daily_budget_cents == 5000
    assert captured["plan"].adsets[0].ad_ids == ["ad_01", "ad_02", "ad_03"]

    # result persisted + exposed as an artifact
    status = client.get(f"/runs/{run_id}").json()
    assert "launch_result" in status["artifacts"]
    saved = client.get(f"/runs/{run_id}/artifacts/launch_result").json()
    assert saved["campaign_id"] == "cmp_1"


def test_launch_requires_scored_package(client):
    run_id = client.post("/runs", json={"url": "x.example"}).json()["run_id"]
    # instant_pipeline writes no scored package
    response = client.post(f"/runs/{run_id}/launch", json={})
    assert response.status_code == 409


def test_launch_unconfigured_returns_400(client, monkeypatch, brand_dna, ads):
    from adengine import api as api_module

    for var in ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_PAGE_ID"):
        monkeypatch.delenv(var, raising=False)
    run_id = client.post("/runs", json={"url": "acmecoffee.example"}).json()["run_id"]
    run_dir = api_module.RUNS_BASE / run_id
    (run_dir / pipeline.SCORED_FILE).write_text(
        make_scored_package(brand_dna, ads).model_dump_json()
    )
    response = client.post(f"/runs/{run_id}/launch", json={})
    assert response.status_code == 400
    assert "META_ACCESS_TOKEN" in response.json()["detail"]
