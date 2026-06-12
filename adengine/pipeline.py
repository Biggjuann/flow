"""Pipeline orchestration shared by the CLI and the web API.

Every run lives in its own timestamped folder under runs/. Runs are resumable:
if an artifact (brand_dna.json, ads.json, scored_package.json) already exists
in the run dir, that step is skipped unless force=True.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pydantic import TypeAdapter

from adengine.extractor import extract_brand_dna
from adengine.generator import generate_ads
from adengine.llm import LLMClient
from adengine.report import render_report
from adengine.schemas import AdConcept, BrandDNA, ScoredPackage
from adengine.scorer import score_ads
from adengine.scraper import scrape_site

BRAND_DNA_FILE = "brand_dna.json"
ADS_FILE = "ads.json"
SCORED_FILE = "scored_package.json"
REPORT_FILE = "report.md"
STATUS_FILE = "status.json"
LLM_LOG_FILE = "llm_log.jsonl"

_ADS_ADAPTER = TypeAdapter(list[AdConcept])

StatusCallback = Callable[[str], None]


def slugify(domain: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", domain.lower()).strip("-") or "site"


def new_run_dir(base: Path, url: str) -> Path:
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"https://{url}")
    domain = slugify(parsed.netloc.removeprefix("www.") or parsed.path.split("/")[0])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = base / f"{stamp}-{domain}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def write_status(run_dir: Path, **fields) -> dict:
    path = run_dir / STATUS_FILE
    status = json.loads(path.read_text()) if path.exists() else {}
    status.update(fields, updated_at=datetime.now(timezone.utc).isoformat())
    path.write_text(json.dumps(status, indent=2))
    return status


def read_status(run_dir: Path) -> dict:
    path = run_dir / STATUS_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def load_brand_dna(run_dir: Path) -> BrandDNA:
    return BrandDNA.model_validate_json((run_dir / BRAND_DNA_FILE).read_text())


def load_ads(run_dir: Path) -> list[AdConcept]:
    return _ADS_ADAPTER.validate_json((run_dir / ADS_FILE).read_text())


def load_scored_package(run_dir: Path) -> ScoredPackage:
    return ScoredPackage.model_validate_json((run_dir / SCORED_FILE).read_text())


def run_pipeline(
    url: str,
    run_dir: Path,
    llm: LLMClient | None = None,
    force: bool = False,
    on_step: StatusCallback | None = None,
) -> Path:
    """Run extract -> generate -> score -> report, resuming from existing artifacts."""

    def step(name: str) -> None:
        write_status(run_dir, state="running", step=name)
        if on_step:
            on_step(name)

    def get_llm() -> LLMClient:
        nonlocal llm
        if llm is None:
            llm = LLMClient(log_path=run_dir / LLM_LOG_FILE)
        return llm

    write_status(run_dir, state="running", url=url, error=None)
    try:
        # Step 1 — Brand DNA
        dna_path = run_dir / BRAND_DNA_FILE
        if dna_path.exists() and not force:
            step("extract (cached)")
            dna = load_brand_dna(run_dir)
        else:
            step("scrape")
            site = scrape_site(url)
            step("extract")
            dna = extract_brand_dna(site, get_llm())
            dna_path.write_text(dna.model_dump_json(indent=2))

        # Step 2 — Ad concepts
        ads_path = run_dir / ADS_FILE
        if ads_path.exists() and not force:
            step("generate (cached)")
            ads = load_ads(run_dir)
        else:
            step("generate")
            ads = generate_ads(dna, get_llm())
            ads_path.write_text(_ADS_ADAPTER.dump_json(ads, indent=2).decode())

        # Step 3 — Scoring + gate
        scored_path = run_dir / SCORED_FILE
        if scored_path.exists() and not force:
            step("score (cached)")
            package = load_scored_package(run_dir)
        else:
            step("score")
            package = score_ads(dna, ads, get_llm())
            scored_path.write_text(package.model_dump_json(indent=2))

        # Report (cheap — always re-rendered)
        step("report")
        (run_dir / REPORT_FILE).write_text(render_report(package, dna))

        write_status(
            run_dir,
            state="completed",
            step="done",
            launch_ready=package.launch_ready_count,
            blocked=package.blocked_count,
        )
        return run_dir
    except Exception as exc:
        write_status(run_dir, state="failed", error=f"{type(exc).__name__}: {exc}")
        raise
