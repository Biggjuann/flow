"""Typer CLI entrypoint.

    adengine run <url>            # full pipeline -> runs/<timestamp>-<domain>/
    adengine run <url> --run-dir runs/...   # resume an existing run
    adengine extract <url>        # step 1 only
    adengine generate <run_dir>   # step 2 from an existing brand_dna.json
    adengine score <run_dir>      # step 3 + report from existing artifacts
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from adengine import pipeline
from adengine.llm import LLMClient, LLMError
from adengine.schemas import Verdict

app = typer.Typer(no_args_is_help=True, add_completion=False, help="AdEngine — URL to scored Meta ad package")
console = Console()

RUNS_BASE = Path("runs")


def _llm_for(run_dir: Path) -> LLMClient:
    try:
        return LLMClient(log_path=run_dir / pipeline.LLM_LOG_FILE)
    except LLMError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1)


def _resolve_run_dir(url: str, run_dir: Optional[Path]) -> Path:
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir
    return pipeline.new_run_dir(RUNS_BASE, url)


def _print_summary(run_dir: Path) -> None:
    package = pipeline.load_scored_package(run_dir)
    table = Table(title=f"{package.business_name} — {package.total_ads} ads scored")
    table.add_column("id")
    table.add_column("framework")
    table.add_column("composite", justify="right")
    table.add_column("policy", justify="right")
    table.add_column("verdict")
    for scored in sorted(package.scored_ads, key=lambda s: s.composite, reverse=True):
        verdict = (
            "[green]launch_ready[/green]"
            if scored.verdict is Verdict.launch_ready
            else "[red]blocked[/red]"
        )
        table.add_row(
            scored.ad.id or "?",
            scored.ad.framework.value,
            f"{scored.composite:.2f}",
            str(scored.review.policy_risk),
            verdict,
        )
    console.print(table)
    console.print(f"Report: [bold]{run_dir / pipeline.REPORT_FILE}[/bold]")


@app.command()
def run(
    url: str = typer.Argument(..., help="Business website URL"),
    run_dir: Optional[Path] = typer.Option(None, "--run-dir", help="Resume an existing run directory"),
    force: bool = typer.Option(False, "--force", help="Re-run steps even if artifacts exist"),
) -> None:
    """Full pipeline: scrape -> Brand DNA -> ads -> scores -> report."""
    target = _resolve_run_dir(url, run_dir)
    console.print(f"Run dir: [bold]{target}[/bold]")
    try:
        with console.status("starting...") as status:
            pipeline.run_pipeline(
                url, target, force=force, on_step=lambda s: status.update(f"step: {s}")
            )
    except LLMError as exc:
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1)
    _print_summary(target)


@app.command()
def extract(
    url: str = typer.Argument(..., help="Business website URL"),
    run_dir: Optional[Path] = typer.Option(None, "--run-dir"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Step 1 only: scrape the site and extract brand_dna.json."""
    from adengine.extractor import extract_brand_dna
    from adengine.scraper import scrape_site

    target = _resolve_run_dir(url, run_dir)
    dna_path = target / pipeline.BRAND_DNA_FILE
    if dna_path.exists() and not force:
        console.print(f"[yellow]{dna_path} exists — use --force to re-extract[/yellow]")
        raise typer.Exit(code=0)

    with console.status("scraping..."):
        site = scrape_site(url)
    console.print(f"Scraped {len(site.pages)} page(s) from {site.domain}")
    with console.status("extracting Brand DNA..."):
        dna = extract_brand_dna(site, _llm_for(target))
    dna_path.write_text(dna.model_dump_json(indent=2))
    console.print(f"Wrote [bold]{dna_path}[/bold]")
    if dna.gaps:
        console.print("[yellow]Site readiness gaps:[/yellow]")
        for gap in dna.gaps:
            console.print(f"  - {gap}")


@app.command()
def generate(
    run_dir: Path = typer.Argument(..., help="Run directory containing brand_dna.json"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Step 2: generate ads.json from an existing brand_dna.json."""
    from adengine.generator import generate_ads

    ads_path = run_dir / pipeline.ADS_FILE
    if ads_path.exists() and not force:
        console.print(f"[yellow]{ads_path} exists — use --force to regenerate[/yellow]")
        raise typer.Exit(code=0)
    dna = pipeline.load_brand_dna(run_dir)
    with console.status("generating ad concepts..."):
        ads = generate_ads(dna, _llm_for(run_dir))
    ads_path.write_text(pipeline._ADS_ADAPTER.dump_json(ads, indent=2).decode())
    console.print(f"Wrote [bold]{ads_path}[/bold] ({len(ads)} concepts)")


@app.command()
def score(
    run_dir: Path = typer.Argument(..., help="Run directory containing brand_dna.json + ads.json"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Step 3: score ads, apply the launch gate, and render report.md."""
    from adengine.report import render_report
    from adengine.scorer import score_ads

    scored_path = run_dir / pipeline.SCORED_FILE
    dna = pipeline.load_brand_dna(run_dir)
    if scored_path.exists() and not force:
        package = pipeline.load_scored_package(run_dir)
        console.print(f"[yellow]{scored_path} exists — re-rendering report only[/yellow]")
    else:
        ads = pipeline.load_ads(run_dir)
        with console.status("scoring ads..."):
            package = score_ads(dna, ads, _llm_for(run_dir))
        scored_path.write_text(package.model_dump_json(indent=2))
    (run_dir / pipeline.REPORT_FILE).write_text(render_report(package, dna))
    _print_summary(run_dir)


if __name__ == "__main__":
    app()
