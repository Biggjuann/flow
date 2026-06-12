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
