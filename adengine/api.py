"""FastAPI service wrapping the pipeline — the Railway deployment surface.

    POST /runs {"url": "..."}        start a run (async), returns run_id
    GET  /runs                       list runs
    GET  /runs/{run_id}              status + available artifacts
    GET  /runs/{run_id}/artifacts/{name}   brand_dna | ads | scored_package (JSON)
    GET  /runs/{run_id}/report       report.md (markdown)
    GET  /healthz                    liveness + config check
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from adengine import pipeline

RUNS_BASE = Path(os.environ.get("ADENGINE_RUNS_DIR", "runs"))
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

ARTIFACTS = {
    "brand_dna": pipeline.BRAND_DNA_FILE,
    "ads": pipeline.ADS_FILE,
    "scored_package": pipeline.SCORED_FILE,
}

app = FastAPI(title="AdEngine", version="0.1.0")
_executor = ThreadPoolExecutor(max_workers=int(os.environ.get("ADENGINE_WORKERS", "2")))


class RunRequest(BaseModel):
    url: str = Field(min_length=4, description="Business website URL")
    force: bool = False


def _run_dir_for(run_id: str) -> Path:
    if not _RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=400, detail="invalid run id")
    run_dir = (RUNS_BASE / run_id).resolve()
    if RUNS_BASE.resolve() not in run_dir.parents:
        raise HTTPException(status_code=400, detail="invalid run id")
    if not run_dir.is_dir():
        raise HTTPException(status_code=404, detail="run not found")
    return run_dir


def _execute(url: str, run_dir: Path, force: bool) -> None:
    try:
        pipeline.run_pipeline(url, run_dir, force=force)
    except Exception:
        # run_pipeline already recorded the failure in status.json
        pass


def _run_summary(run_dir: Path) -> dict:
    status = pipeline.read_status(run_dir)
    return {
        "run_id": run_dir.name,
        "state": status.get("state", "unknown"),
        "step": status.get("step"),
        "url": status.get("url"),
        "error": status.get("error"),
        "launch_ready": status.get("launch_ready"),
        "blocked": status.get("blocked"),
        "updated_at": status.get("updated_at"),
        "artifacts": [
            name for name, filename in ARTIFACTS.items() if (run_dir / filename).exists()
        ]
        + (["report"] if (run_dir / pipeline.REPORT_FILE).exists() else []),
    }


@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": True,
        "api_key_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }


@app.post("/runs", status_code=202)
def create_run(request: RunRequest) -> dict:
    if not request.url.startswith(("http://", "https://")):
        request.url = f"https://{request.url}"
    RUNS_BASE.mkdir(parents=True, exist_ok=True)
    run_dir = pipeline.new_run_dir(RUNS_BASE, request.url)
    pipeline.write_status(run_dir, state="queued", step="queued", url=request.url, error=None)
    _executor.submit(_execute, request.url, run_dir, request.force)
    return {"run_id": run_dir.name, "state": "queued"}


@app.get("/runs")
def list_runs() -> list[dict]:
    if not RUNS_BASE.is_dir():
        return []
    run_dirs = sorted((d for d in RUNS_BASE.iterdir() if d.is_dir()), reverse=True)
    return [_run_summary(d) for d in run_dirs]


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    return _run_summary(_run_dir_for(run_id))


@app.get("/runs/{run_id}/artifacts/{name}")
def get_artifact(run_id: str, name: str) -> JSONResponse:
    run_dir = _run_dir_for(run_id)
    filename = ARTIFACTS.get(name)
    if filename is None:
        raise HTTPException(
            status_code=404, detail=f"unknown artifact; one of {sorted(ARTIFACTS)}"
        )
    path = run_dir / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{name} not generated yet")
    return JSONResponse(content=json.loads(path.read_text()))


@app.get("/runs/{run_id}/report")
def get_report(run_id: str) -> PlainTextResponse:
    run_dir = _run_dir_for(run_id)
    path = run_dir / pipeline.REPORT_FILE
    if not path.exists():
        raise HTTPException(status_code=404, detail="report not generated yet")
    return PlainTextResponse(path.read_text(), media_type="text/markdown")
