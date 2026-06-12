# AdEngine

Autonomous Meta campaign builder — Phase 1 (core engine). Give it a business
website URL; it produces a scored, launch-ready Meta ad campaign package:

```
URL → [1. Brand DNA Extractor] → brand_dna.json
    → [2. Ad Generator]        → ads.json (12–20 concepts across 9 hook frameworks)
    → [3. Pre-Launch Scorer]   → scored_package.json + report.md
```

The scorer is an adversarial, *blind* reviewer (it never sees the generator's
framework labels). Each ad is scored 1–10 on hook strength, message clarity,
audience fit, offer match, creative quality, and Meta policy risk; the launch
gate is **composite ≥ 7.0 AND policy_risk ≥ 8**.

## Local usage (CLI)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv sync

uv run adengine run https://example.com          # full pipeline
uv run adengine extract https://example.com      # step 1 only
uv run adengine generate runs/<run-dir>          # step 2 from brand_dna.json
uv run adengine score runs/<run-dir>             # step 3 + report
```

Every run gets its own `runs/<timestamp>-<domain>/` folder containing
`brand_dna.json`, `ads.json`, `scored_package.json`, `report.md`, and
`llm_log.jsonl` (every LLM call: model, tokens, latency). Runs are resumable —
re-running against the same dir (`--run-dir`) skips steps whose artifacts
already exist, unless `--force`.

## Web API (what runs on Railway)

```bash
uv run uvicorn adengine.api:app --reload
```

| Method | Path | Purpose |
|---|---|---|
| POST | `/runs` `{"url": "...", "force": false}` | start a run (async), returns `run_id` |
| GET | `/runs` | list runs |
| GET | `/runs/{run_id}` | status, current step, artifact list |
| GET | `/runs/{run_id}/artifacts/{name}` | `brand_dna` \| `ads` \| `scored_package` |
| GET | `/runs/{run_id}/report` | `report.md` as markdown |
| GET | `/healthz` | liveness + API-key check |

## Deploying to Railway

1. Create a Railway project → **Deploy from GitHub repo** → pick this repo.
   `railway.json` selects the Dockerfile build and the `/healthz` healthcheck.
2. Add a service variable: `ANTHROPIC_API_KEY=sk-ant-...`
   (optional: `ADENGINE_MODEL` to override the default `claude-sonnet-4-5`).
3. Railway injects `PORT` automatically; the container binds to it.
4. **Persistence (recommended):** the container filesystem is ephemeral, so
   attach a Railway **Volume** mounted at `/app/runs` to keep run artifacts
   across deploys/restarts. Without a volume, runs survive only until the next
   deploy.

Then:

```bash
curl -X POST https://<your-app>.up.railway.app/runs \
  -H 'content-type: application/json' -d '{"url": "https://example.com"}'
curl https://<your-app>.up.railway.app/runs/<run_id>/report
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — (required) | Anthropic API key; the engine fails fast if missing |
| `ADENGINE_MODEL` | `claude-sonnet-4-5` | model for extraction/generation/scoring |
| `ADENGINE_RUNS_DIR` | `runs` | where the API stores run folders |
| `ADENGINE_WORKERS` | `2` | concurrent pipeline runs in the API |
| `ADENGINE_PROMPTS_DIR` | `prompts/` | prompt template location |

## Development

```bash
uv sync
uv run pytest          # no network needed — LLM + HTTP fully mocked
```

All prompts live in `prompts/*.md` and are loaded at runtime. Every LLM call
uses structured output (forced tool use) validated against the Pydantic models
in `adengine/schemas.py` — freeform text is never parsed.

## Phase 2/3 (stubs only)

- `adengine/launcher.py` — `LaunchPlan` (campaign → adset → ads mapped to Meta
  Marketing API objects) and a `MetaLauncher` interface.
- `adengine/optimizer.py` — `PerformanceSnapshot` and `OptimizationAction`
  (kill_ad, shift_budget, refresh_creative, expand_audience, alert_human).
