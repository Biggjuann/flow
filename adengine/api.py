"""FastAPI service wrapping the pipeline — the Railway deployment surface.

    GET  /                           web UI (paste a URL, watch the run, browse results)
    POST /runs {"url": "..."}        start a run (async), returns run_id
    GET  /runs                       list runs
    GET  /runs/{run_id}              status + available artifacts
    GET  /runs/{run_id}/artifacts/{name}   brand_dna | ads | scored_package (JSON)
    GET  /runs/{run_id}/report       report.md (markdown)
    POST /runs/{run_id}/launch       push launch-ready ads to Meta (created PAUSED)
    GET  /healthz                    liveness + config check
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from adengine import images as image_mod
from adengine import launcher as meta_launcher
from adengine import optimizer as optimizer_mod
from adengine import pipeline
from adengine.schemas import ConversionPath

RUNS_BASE = Path(os.environ.get("ADENGINE_RUNS_DIR", "runs"))
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

LAUNCH_FILE = "launch_result.json"
OPTIMIZE_LOG_FILE = "optimization_log.json"
IMAGES_DIR = "images"
IMAGES_MANIFEST = "images_manifest.json"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
_AD_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

ARTIFACTS = {
    "brand_dna": pipeline.BRAND_DNA_FILE,
    "ads": pipeline.ADS_FILE,
    "scored_package": pipeline.SCORED_FILE,
    "launch_result": LAUNCH_FILE,
    "optimization_log": OPTIMIZE_LOG_FILE,
    "images_manifest": IMAGES_MANIFEST,
}

app = FastAPI(title="AdEngine", version="0.1.0")
_executor = ThreadPoolExecutor(max_workers=int(os.environ.get("ADENGINE_WORKERS", "2")))

# run_ids this process is actively executing — used to tell a live run apart
# from one orphaned by a restart.
_inflight: set[str] = set()
_LIVE_STATES = {"queued", "running"}


def _is_run_dir(path: Path) -> bool:
    """A real run directory has a status.json (written at creation)."""
    return path.is_dir() and (path / pipeline.STATUS_FILE).exists()


def reap_orphans() -> int:
    """Mark runs left 'running'/'queued' by a dead process as failed.

    The in-process executor loses all work when the container restarts, but the
    run's status.json on the (persistent) volume still says running — so the UI
    would spin forever. At startup nothing is in flight, so any live-state run
    on disk is an orphan.
    """
    if not RUNS_BASE.is_dir():
        return 0
    reaped = 0
    for path in RUNS_BASE.iterdir():
        if not _is_run_dir(path) or path.name in _inflight:
            continue
        status = pipeline.read_status(path)
        if status.get("state") in _LIVE_STATES:
            pipeline.write_status(
                path,
                state="failed",
                error="Interrupted — the server restarted while this run was in progress. "
                "Re-run the URL (existing steps are cached and will be skipped).",
            )
            reaped += 1
    return reaped


@app.on_event("startup")
def _on_startup() -> None:
    reap_orphans()


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
    _inflight.add(run_dir.name)
    try:
        pipeline.run_pipeline(url, run_dir, force=force)
    except Exception:
        # run_pipeline already recorded the failure in status.json
        pass
    finally:
        _inflight.discard(run_dir.name)


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


STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
def healthz() -> dict:
    return {
        "ok": True,
        "api_key_configured": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "meta_configured": all(
            os.environ.get(var)
            for var in ("META_ACCESS_TOKEN", "META_AD_ACCOUNT_ID", "META_PAGE_ID")
        ),
        "pixel_configured": bool(os.environ.get("META_PIXEL_ID")),
        "app_id_configured": bool(os.environ.get("META_APP_ID")),
        "images_configured": bool(
            (os.environ.get("ADENGINE_IMAGE_PROVIDER") or "").lower() == "openai"
            and (os.environ.get("OPENAI_API_KEY") or os.environ.get("ADENGINE_IMAGE_API_KEY"))
        ),
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
    run_dirs = sorted((d for d in RUNS_BASE.iterdir() if _is_run_dir(d)), reverse=True)
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


class ImagesRequest(BaseModel):
    force: bool = Field(default=False, description="Regenerate even if a preview exists")
    launch_ready_only: bool = Field(
        default=True, description="Only generate for ads that passed the launch gate"
    )


def _read_manifest(run_dir: Path) -> dict:
    path = run_dir / IMAGES_MANIFEST
    return json.loads(path.read_text()) if path.exists() else {}


def _uploaded_ad_ids(run_dir: Path) -> set[str]:
    manifest = _read_manifest(run_dir)
    return {
        ad_id
        for ad_id, entry in (manifest.get("ads") or {}).items()
        if entry.get("source") == "upload"
    }


@app.post("/runs/{run_id}/images")
def generate_images(run_id: str, request: ImagesRequest) -> dict:
    """Generate preview creative images per ad so they can be reviewed in the UI."""
    run_dir = _run_dir_for(run_id)
    if not (run_dir / pipeline.SCORED_FILE).exists():
        raise HTTPException(status_code=409, detail="run has no scored package yet")

    provider = image_mod.provider_from_env()
    if provider is None:
        raise HTTPException(
            status_code=400,
            detail="Image generation is not configured — set ADENGINE_IMAGE_PROVIDER=openai "
            "and OPENAI_API_KEY in the server environment.",
        )

    package = pipeline.load_scored_package(run_dir)
    ads = [
        s.ad
        for s in package.scored_ads
        if not request.launch_ready_only or s.verdict.value == "launch_ready"
    ]
    if not ads:
        raise HTTPException(status_code=409, detail="no launch-ready ads to generate images for")

    # User uploads are never overwritten by (re)generation.
    manifest = image_mod.generate_previews(
        run_dir / IMAGES_DIR,
        ads,
        provider,
        force=request.force,
        locked=_uploaded_ad_ids(run_dir),
    )
    (run_dir / IMAGES_MANIFEST).write_text(json.dumps(manifest, indent=2))
    return manifest


@app.post("/runs/{run_id}/images/{ad_id}/upload")
async def upload_image(run_id: str, ad_id: str, file: UploadFile = File(...)) -> dict:
    """Upload your own creative image for one ad (overrides AI/preview at launch)."""
    run_dir = _run_dir_for(run_id)
    if not _AD_ID_RE.match(ad_id):
        raise HTTPException(status_code=400, detail="invalid ad id")

    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="image too large (max 8 MB)")
    try:
        image_mod.save_upload(run_dir / IMAGES_DIR, ad_id, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Record it in the manifest so the UI and (re)generation know it's user-owned.
    manifest = _read_manifest(run_dir)
    manifest.setdefault("ads", {})[ad_id] = {"ok": True, "source": "upload", "error": None}
    manifest["generated"] = sum(1 for e in manifest["ads"].values() if e.get("ok"))
    (run_dir / IMAGES_MANIFEST).write_text(json.dumps(manifest, indent=2))
    return {"ad_id": ad_id, "source": "upload", "ok": True}


@app.delete("/runs/{run_id}/images/{ad_id}")
def delete_image(run_id: str, ad_id: str) -> dict:
    """Remove an ad's image (reverts to the link-preview image at launch)."""
    run_dir = _run_dir_for(run_id)
    if not _AD_ID_RE.match(ad_id):
        raise HTTPException(status_code=400, detail="invalid ad id")
    path = run_dir / IMAGES_DIR / f"{ad_id}.png"
    if path.exists():
        path.unlink()
    manifest = _read_manifest(run_dir)
    if (manifest.get("ads") or {}).pop(ad_id, None) is not None:
        manifest["generated"] = sum(1 for e in manifest["ads"].values() if e.get("ok"))
        (run_dir / IMAGES_MANIFEST).write_text(json.dumps(manifest, indent=2))
    return {"ad_id": ad_id, "removed": True}


@app.get("/runs/{run_id}/images/{ad_id}")
def get_image(run_id: str, ad_id: str):
    run_dir = _run_dir_for(run_id)
    if not _AD_ID_RE.match(ad_id):
        raise HTTPException(status_code=400, detail="invalid ad id")
    path = run_dir / IMAGES_DIR / f"{ad_id}.png"
    if not path.exists():
        raise HTTPException(status_code=404, detail="no image for this ad")
    media_type = image_mod.sniff_image_type(path.read_bytes()[:8]) or "image/png"
    return FileResponse(path, media_type=media_type)


class LaunchRequest(BaseModel):
    daily_budget_usd: float = Field(default=20.0, ge=1, le=10_000)
    country: str = Field(default="US", min_length=2, max_length=2)
    split: bool = Field(
        default=True,
        description="A/B split: one adset per top ad so Meta's learning is isolated",
    )


@app.post("/runs/{run_id}/launch")
def launch_run(run_id: str, request: LaunchRequest) -> dict:
    """Push the run's launch-ready ads to Meta. Everything is created PAUSED."""
    run_dir = _run_dir_for(run_id)
    if not (run_dir / pipeline.SCORED_FILE).exists():
        raise HTTPException(status_code=409, detail="run has no scored package yet")

    package = pipeline.load_scored_package(run_dir)
    destination = package.source_url or pipeline.read_status(run_dir).get("url")
    if not destination:
        raise HTTPException(status_code=409, detail="run has no destination URL")

    # Conversion path drives the campaign objective (sales / leads / installs).
    conversion_path = ConversionPath.purchase
    if (run_dir / pipeline.BRAND_DNA_FILE).exists():
        conversion_path = pipeline.load_brand_dna(run_dir).offer.conversion_path

    try:
        launcher = meta_launcher.MetaLauncher.from_env()
        # Reuse the previews the user reviewed in the UI; generate any missing on the fly.
        launcher.image_dir = run_dir / IMAGES_DIR
        plan = meta_launcher.build_plan(
            package,
            conversion_path,
            daily_budget_cents=int(round(request.daily_budget_usd * 100)),
            destination_url=destination,
            country=request.country.upper(),
            pixel_id=launcher.pixel_id,
            app_id=launcher.app_id,
            split=request.split,
        )
        result = launcher.launch(plan)
    except meta_launcher.MetaConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except meta_launcher.MetaAPIError as exc:
        raise HTTPException(status_code=502, detail=f"Meta API rejected the launch: {exc}")

    body = result.model_dump(mode="json")
    body["ads_manager_url"] = (
        "https://adsmanager.facebook.com/adsmanager/manage/campaigns"
        f"?act={launcher.ad_account_id.removeprefix('act_')}"
    )
    (run_dir / LAUNCH_FILE).write_text(json.dumps(body, indent=2))
    return body


class OptimizeRequest(BaseModel):
    apply: bool = Field(
        default=False,
        description="Execute the automatic actions (pause losers, bounded budget shifts)",
    )
    target_cpa_usd: float | None = Field(default=None, ge=0.5, le=100_000)
    date_preset: str = Field(default="last_7d", pattern=r"^[a-z0-9_]+$")


@app.post("/runs/{run_id}/optimize")
def optimize_run(run_id: str, request: OptimizeRequest) -> dict:
    """Read live campaign performance, recommend actions, optionally apply them."""
    run_dir = _run_dir_for(run_id)
    launch_path = run_dir / LAUNCH_FILE
    if not launch_path.exists():
        raise HTTPException(status_code=409, detail="run has not been launched to Meta yet")
    campaign_id = json.loads(launch_path.read_text()).get("campaign_id")
    if not campaign_id:
        raise HTTPException(status_code=409, detail="launch result has no campaign id")

    try:
        launcher = meta_launcher.MetaLauncher.from_env()
        optimizer = optimizer_mod.Optimizer(
            launcher,
            target_cpa_cents=(
                int(round(request.target_cpa_usd * 100)) if request.target_cpa_usd else None
            ),
        )
        snapshots = optimizer.fetch_snapshots(campaign_id, date_preset=request.date_preset)
        decisions = optimizer.decide(snapshots)
        applied = optimizer.apply(decisions) if request.apply else []
    except meta_launcher.MetaConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except meta_launcher.MetaAPIError as exc:
        raise HTTPException(status_code=502, detail=f"Meta API error: {exc}")

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "campaign_id": campaign_id,
        "date_preset": request.date_preset,
        "applied_mode": request.apply,
        "snapshots": [s.model_dump(mode="json") for s in snapshots],
        "decisions": [d.model_dump(mode="json") for d in decisions],
        "applied": [a.model_dump(mode="json") for a in applied],
    }
    log_path = run_dir / OPTIMIZE_LOG_FILE
    history = json.loads(log_path.read_text()) if log_path.exists() else []
    history.append(record)
    log_path.write_text(json.dumps(history, indent=2))
    return record


@app.get("/runs/{run_id}/report")
def get_report(run_id: str) -> PlainTextResponse:
    run_dir = _run_dir_for(run_id)
    path = run_dir / pipeline.REPORT_FILE
    if not path.exists():
        raise HTTPException(status_code=404, detail="report not generated yet")
    return PlainTextResponse(path.read_text(), media_type="text/markdown")
