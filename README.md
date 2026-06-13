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

## Web app (what runs on Railway)

```bash
uv run uvicorn adengine.api:app --reload
```

Open `http://localhost:8000/` for the **web UI**: paste a URL, watch the run
progress live, browse scored ad cards / the rendered report / the Brand DNA,
and push the launch-ready ads to Meta with one click.

The underlying API:

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | web UI |
| POST | `/runs` `{"url": "...", "force": false}` | start a run (async), returns `run_id` |
| GET | `/runs` | list runs |
| GET | `/runs/{run_id}` | status, current step, artifact list |
| GET | `/runs/{run_id}/artifacts/{name}` | `brand_dna` \| `ads` \| `scored_package` \| `launch_result` |
| GET | `/runs/{run_id}/report` | `report.md` as markdown |
| POST | `/runs/{run_id}/launch` `{"daily_budget_usd": 20, "country": "US"}` | push launch-ready ads to Meta (**created PAUSED**) |
| GET | `/healthz` | liveness + config check |

## Pushing ads to Meta

`POST /runs/{id}/launch` (or the **Push to Meta** button in the UI) creates a
campaign → adset → ads on your Meta ad account via the Marketing API.

**Safety:** every object is created with `status=PAUSED`. Nothing spends money
until you review and activate the campaign in Ads Manager.

Required environment variables:

| Env var | What it is |
|---|---|
| `META_ACCESS_TOKEN` | a (system) user access token with `ads_management` permission |
| `META_AD_ACCOUNT_ID` | your ad account id (`act_...` or just the number) |
| `META_PAGE_ID` | the Facebook Page the ads are published from |

Getting the token: create an app at developers.facebook.com → add the
Marketing API product → generate a token with `ads_management` +
`pages_read_engagement` (a System User token from Business Settings is best
for servers — it doesn't expire).

v1 scope and known limits:
- Campaign objective is **Traffic** (LINK_CLICKS). Sales/Leads objectives need
  a pixel / lead form — later phase.
- Creatives are **link ads**; Meta pulls the preview image from your site's
  `og:image`. Swap in dedicated creative images in Ads Manager (each ad's
  `image_prompt` in `ads.json` tells a designer/image model what to make).
- Interest targeting is resolved best-effort by name; unmatched interests are
  skipped.

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
| `META_ACCESS_TOKEN` | — | required for `/launch`; token with `ads_management` |
| `META_AD_ACCOUNT_ID` | — | required for `/launch`; `act_...` or bare number |
| `META_PAGE_ID` | — | required for `/launch`; publishing Page id |
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
