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
            all_ids = [aid for adset in plan.adsets for aid in adset.ad_ids]
            return LaunchResult(
                campaign_id="cmp_1",
                objective=plan.campaign.objective.value,
                adset_ids=[f"as_{i}" for i in range(len(plan.adsets))],
                ad_ids={aid: f"meta_{aid}" for aid in all_ids},
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
    plan = captured["plan"]
    # default A/B split: one adset per ready ad, budget split across them
    assert len(plan.adsets) == 3
    assert sum(a.daily_budget_cents for a in plan.adsets) == 5000
    assert [aid for a in plan.adsets for aid in a.ad_ids] == ["ad_01", "ad_02", "ad_03"]

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


# ------------------------------------------------------------------ optimize

def test_optimize_endpoint(client, monkeypatch):
    import json as _json

    from adengine import api as api_module

    run_id = client.post("/runs", json={"url": "acmecoffee.example"}).json()["run_id"]
    run_dir = api_module.RUNS_BASE / run_id

    # not launched yet -> 409
    assert client.post(f"/runs/{run_id}/optimize", json={}).status_code == 409

    (run_dir / api_module.LAUNCH_FILE).write_text(_json.dumps({"campaign_id": "cmp_1"}))

    class FakeLauncher:
        paused = []

        def get_campaign_insights(self, campaign_id, date_preset="last_7d"):
            assert campaign_id == "cmp_1"
            return [
                {
                    "ad_id": "120211",
                    "ad_name": "AdEngine ad_01",
                    "impressions": "3000",
                    "clicks": "5",
                    "spend": "95.00",
                    "date_start": "2026-06-06",
                    "date_stop": "2026-06-12",
                    "actions": [],
                }
            ]

        def pause_ad(self, meta_ad_id):
            self.paused.append(meta_ad_id)

    fake = FakeLauncher()
    monkeypatch.setattr(
        api_module.meta_launcher.MetaLauncher, "from_env", classmethod(lambda cls: fake)
    )

    # check only: decisions recommended, nothing applied
    body = client.post(
        f"/runs/{run_id}/optimize", json={"target_cpa_usd": 30}
    ).json()
    assert body["campaign_id"] == "cmp_1"
    assert body["snapshots"][0]["ad_id"] == "ad_01"
    assert body["decisions"][0]["action"] == "kill_ad"  # $95 spent, 0 conv, $30 target
    assert body["applied"] == []
    assert fake.paused == []

    # apply: the kill executes
    body = client.post(
        f"/runs/{run_id}/optimize", json={"target_cpa_usd": 30, "apply": True}
    ).json()
    applied = [a for a in body["applied"] if a["action"] == "kill_ad"]
    assert applied and applied[0]["ok"]
    assert fake.paused == ["120211"]

    # history accumulates and is exposed as an artifact
    log = client.get(f"/runs/{run_id}/artifacts/optimization_log").json()
    assert len(log) == 2
    assert "optimization_log" in client.get(f"/runs/{run_id}").json()["artifacts"]


# ------------------------------------------------------ orphan / restart recovery

def test_reap_orphans_fails_stuck_runs(client, monkeypatch, tmp_path):
    from adengine import api as api_module

    # a run left "running" by a dead process
    stuck = api_module.RUNS_BASE / "20260613-011609-arete.shop"
    stuck.mkdir()
    pipeline.write_status(stuck, state="running", step="score", url="https://arete.shop/")
    # a completed run that must be left alone
    done = api_module.RUNS_BASE / "20260613-010000-done.example"
    done.mkdir()
    pipeline.write_status(done, state="completed", step="done")

    reaped = api_module.reap_orphans()
    assert reaped == 1
    assert pipeline.read_status(stuck)["state"] == "failed"
    assert "restarted" in pipeline.read_status(stuck)["error"]
    assert pipeline.read_status(done)["state"] == "completed"


def test_reap_skips_inflight_runs(client):
    from adengine import api as api_module

    live = api_module.RUNS_BASE / "20260613-020000-live.example"
    live.mkdir()
    pipeline.write_status(live, state="running", step="generate")
    api_module._inflight.add(live.name)
    try:
        assert api_module.reap_orphans() == 0
        assert pipeline.read_status(live)["state"] == "running"
    finally:
        api_module._inflight.discard(live.name)


def test_list_runs_ignores_non_run_dirs(client):
    from adengine import api as api_module

    # volume cruft like lost+found has no status.json and must not appear
    (api_module.RUNS_BASE / "lost+found").mkdir()
    run_id = client.post("/runs", json={"url": "acmecoffee.example"}).json()["run_id"]

    runs = client.get("/runs").json()
    assert [r["run_id"] for r in runs] == [run_id]
    assert all(r["state"] != "unknown" for r in runs)


# ------------------------------------------------------------ image previews

def test_images_endpoint_generates_and_serves(client, monkeypatch, brand_dna, ads):
    from adengine import api as api_module

    run_id = client.post("/runs", json={"url": "acmecoffee.example"}).json()["run_id"]
    run_dir = api_module.RUNS_BASE / run_id
    (run_dir / pipeline.SCORED_FILE).write_text(
        make_scored_package(brand_dna, ads).model_dump_json()
    )

    png = b"\x89PNG\r\n\x1a\n-fake"

    class FakeProvider:
        model = "gpt-image-1"
        last_error = None

        def __call__(self, prompt):
            return png

    monkeypatch.setattr(api_module.image_mod, "provider_from_env", lambda *a, **k: FakeProvider())

    manifest = client.post(f"/runs/{run_id}/images", json={}).json()
    assert manifest["generated"] == 3  # only launch-ready ads
    assert manifest["model"] == "gpt-image-1"

    # manifest exposed as artifact + image served as PNG
    assert "images_manifest" in client.get(f"/runs/{run_id}").json()["artifacts"]
    img = client.get(f"/runs/{run_id}/images/ad_01")
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/png"
    assert img.content == png

    # a blocked / non-generated ad has no image
    assert client.get(f"/runs/{run_id}/images/ad_99").status_code == 404
    assert client.get(f"/runs/{run_id}/images/..%2f..%2fetc").status_code in (400, 404)


def test_images_endpoint_unconfigured_400(client, monkeypatch, brand_dna, ads):
    from adengine import api as api_module

    run_id = client.post("/runs", json={"url": "x.example"}).json()["run_id"]
    run_dir = api_module.RUNS_BASE / run_id
    (run_dir / pipeline.SCORED_FILE).write_text(
        make_scored_package(brand_dna, ads).model_dump_json()
    )
    monkeypatch.setattr(api_module.image_mod, "provider_from_env", lambda *a, **k: None)
    response = client.post(f"/runs/{run_id}/images", json={})
    assert response.status_code == 400
    assert "ADENGINE_IMAGE_PROVIDER" in response.json()["detail"]


def test_launch_reuses_reviewed_images(client, monkeypatch, brand_dna, ads):
    from adengine import api as api_module
    from adengine.launcher import LaunchResult

    run_id = client.post("/runs", json={"url": "acmecoffee.example"}).json()["run_id"]
    run_dir = api_module.RUNS_BASE / run_id
    (run_dir / pipeline.SCORED_FILE).write_text(
        make_scored_package(brand_dna, ads).model_dump_json()
    )

    seen = {}

    class FakeLauncher:
        ad_account_id = "act_1"
        pixel_id = None
        app_id = None
        image_dir = None

        def launch(self, plan):
            seen["image_dir"] = self.image_dir
            return LaunchResult(campaign_id="cmp", objective=plan.campaign.objective.value)

    monkeypatch.setattr(
        api_module.meta_launcher.MetaLauncher, "from_env", classmethod(lambda cls: FakeLauncher())
    )
    client.post(f"/runs/{run_id}/launch", json={})
    assert seen["image_dir"] == run_dir / api_module.IMAGES_DIR


# ------------------------------------------------------------ image uploads

def _scored(client, brand_dna, ads):
    from adengine import api as api_module
    run_id = client.post("/runs", json={"url": "acmecoffee.example"}).json()["run_id"]
    run_dir = api_module.RUNS_BASE / run_id
    (run_dir / pipeline.SCORED_FILE).write_text(
        make_scored_package(brand_dna, ads).model_dump_json()
    )
    return run_id, run_dir


def test_upload_then_serve_and_lock(client, monkeypatch, brand_dna, ads):
    from adengine import api as api_module

    run_id, run_dir = _scored(client, brand_dna, ads)
    png = b"\x89PNG\r\n\x1a\nMYLOGO"

    # upload a user image for ad_01
    res = client.post(
        f"/runs/{run_id}/images/ad_01/upload",
        files={"file": ("logo.png", png, "image/png")},
    )
    assert res.status_code == 200, res.text
    assert res.json()["source"] == "upload"

    # served back with correct content type
    got = client.get(f"/runs/{run_id}/images/ad_01")
    assert got.status_code == 200
    assert got.content == png
    assert got.headers["content-type"] == "image/png"

    # manifest marks it as an upload + exposed as artifact
    manifest = client.get(f"/runs/{run_id}/artifacts/images_manifest").json()
    assert manifest["ads"]["ad_01"]["source"] == "upload"

    # regeneration must NOT overwrite the upload
    class FakeProvider:
        model = "gpt-image-1"
        last_error = None
        def __call__(self, prompt):
            return b"\x89PNG\r\n\x1a\nAIGEN"

    monkeypatch.setattr(api_module.image_mod, "provider_from_env", lambda *a, **k: FakeProvider())
    client.post(f"/runs/{run_id}/images", json={"force": True})
    assert client.get(f"/runs/{run_id}/images/ad_01").content == png  # still the upload


def test_upload_rejects_non_image(client, brand_dna, ads):
    run_id, _ = _scored(client, brand_dna, ads)
    res = client.post(
        f"/runs/{run_id}/images/ad_01/upload",
        files={"file": ("x.txt", b"hello not an image", "text/plain")},
    )
    assert res.status_code == 400
    assert "PNG" in res.json()["detail"]


def test_upload_works_without_ai_provider(client, monkeypatch, brand_dna, ads):
    # uploading your own creative must not require ADENGINE_IMAGE_PROVIDER
    from adengine import api as api_module
    monkeypatch.setattr(api_module.image_mod, "provider_from_env", lambda *a, **k: None)
    run_id, _ = _scored(client, brand_dna, ads)
    res = client.post(
        f"/runs/{run_id}/images/ad_02/upload",
        files={"file": ("a.jpg", b"\xff\xd8\xff\xe0JFIF-bytes", "image/jpeg")},
    )
    assert res.status_code == 200
    assert client.get(f"/runs/{run_id}/images/ad_02").headers["content-type"] == "image/jpeg"


def test_delete_image_reverts(client, brand_dna, ads):
    run_id, _ = _scored(client, brand_dna, ads)
    client.post(
        f"/runs/{run_id}/images/ad_01/upload",
        files={"file": ("l.png", b"\x89PNG\r\n\x1a\nX", "image/png")},
    )
    assert client.get(f"/runs/{run_id}/images/ad_01").status_code == 200
    client.delete(f"/runs/{run_id}/images/ad_01")
    assert client.get(f"/runs/{run_id}/images/ad_01").status_code == 404
    manifest = client.get(f"/runs/{run_id}/artifacts/images_manifest").json()
    assert "ad_01" not in (manifest.get("ads") or {})
