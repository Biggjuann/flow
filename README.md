# AdEngine

Autonomous Meta campaign builder. Give it a **website URL or an App Store /
Google Play listing**; it produces a scored, launch-ready Meta ad campaign and
can push it live (conversion-optimized, all PAUSED until you activate):

```
URL / App Store link
    → [1. Brand DNA Extractor] → brand_dna.json
    → [2. Ad Generator]        → ads.json (12–20 concepts across 9 hook frameworks)
    → [3. Pre-Launch Scorer]   → scored_package.json + report.md
    → [4. Meta Launcher]       → live campaign (PAUSED), conversion-optimized
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
| POST | `/runs/{run_id}/launch` `{"daily_budget_usd": 20, "country": "US", "split": true}` | push launch-ready ads to Meta (**created PAUSED**, A/B split adsets) |
| POST | `/runs/{run_id}/optimize` `{"apply": false, "target_cpa_usd": 30}` | read live performance, recommend/apply actions |
| GET | `/healthz` | liveness + config check |

## Pushing ads to Meta

`POST /runs/{id}/launch` (or the **Push to Meta** button in the UI) creates a
campaign → adset → ads on your Meta ad account via the Marketing API.

**Safety:** every object is created with `status=PAUSED`. Nothing spends money
until you review and activate the campaign in Ads Manager.

**Required** (campaign creation):

| Env var | What it is |
|---|---|
| `META_ACCESS_TOKEN` | a (system) user access token with `ads_management` permission |
| `META_AD_ACCOUNT_ID` | your ad account id (`act_...` or just the number) |
| `META_PAGE_ID` | the Facebook Page the ads are published from |

**Optional** (these are what make it *sell*):

| Env var | Effect |
|---|---|
| `META_PIXEL_ID` | Web campaigns optimize for the real money event (Purchase / Lead / Schedule / Contact) instead of clicks — Meta's delivery hunts for buyers. Without it, runs fall back to Traffic. |
| `META_APP_ID` | App Store / Play runs become install-optimized **App Promotion** campaigns. Without it, app runs drive Traffic to the store page. |
| `ADENGINE_IMAGE_PROVIDER=openai` + `OPENAI_API_KEY` | Lets you generate a real creative image per ad (from the ad's `image_prompt`), **review it in the web app**, and ship it to Meta at launch. Without it, ads use the link/store preview image. |

### Reviewing AI creative images before launch

Creative is ~70% of Meta performance, so you review it *before* spending:

1. Set `ADENGINE_IMAGE_PROVIDER=openai` and `OPENAI_API_KEY` (two separate
   server variables — both required). Confirm on `/healthz` → `images_configured: true`.
2. Open a finished run in the web app → the **Ads** tab shows a **Generate
   creative images** button. Click it; ~30–60s later each launch-ready ad card
   shows its real generated image.
3. Don't like one? **Regenerate** re-rolls all of them. The images you see are
   exactly what ships — at launch the reviewed previews are reused (no
   regeneration, no double cost), uploaded to Meta, and attached to each ad.

If generation fails, the card shows **why** (e.g. *"gpt-image-1 requires a
verified OpenAI organization"* or *"no quota — add billing"*) instead of
silently doing nothing. Common fixes:

- **`gpt-image-1` needs org verification.** Either verify at
  platform.openai.com/settings/organization/general, or set
  `ADENGINE_IMAGE_MODEL=dall-e-3` (no verification required).
- **401** → wrong/inactive `OPENAI_API_KEY`. **429 / quota** → add OpenAI
  billing (image generation is billed by OpenAI, separate from Anthropic).

It never blocks a launch: any ad whose image fails just falls back to the
link-preview image.

Getting the token: create an app at developers.facebook.com → add the
Marketing API product → generate a token with `ads_management` +
`pages_read_engagement` (a System User token from Business Settings is best
for servers — it doesn't expire).

### How it's built to drive sales

- **A/B split by default.** Each top ad (max 5, by composite score) launches in
  its **own adset** with its own audience hint and an even slice of the budget,
  so Meta's learning is isolated per ad and the winner emerges fast. Extra
  launch-ready ads are held in reserve for creative refresh. Adsets never split
  below $5/day; uncheck "A/B split" in the UI to group everything in one adset.

- **Objective from intent.** The campaign objective is derived from the brand's
  conversion path: `purchase → Sales/Purchase`, `lead_form → Leads/Lead`,
  `booking → Leads/Schedule`, `call → Leads/Contact`, `app_install → App
  Promotion/Installs`. With a pixel/app id configured, Meta optimizes for that
  exact event.
- **Attribution baked in.** Destination links are tagged automatically — UTM
  params for the web (`utm_source=facebook…&utm_content=<ad_id>`), Apple `ct`
  campaign token for the App Store — so the client can prove which ad drove
  each sale/install in their own analytics.
- **Real creative.** With an image provider set, each ad ships with a generated
  image; otherwise the preview image is used.

### iOS / mobile apps

Paste an **App Store** (`apps.apple.com`) or **Google Play** link as the URL.
AdEngine reads the listing as the product, the extractor sets the conversion
path to `app_install`, ad copy and CTAs (`INSTALL_MOBILE_APP`) are written for
installs, and the launcher runs an install-optimized App Promotion campaign
(set `META_APP_ID` to the Facebook app id linked to your iOS app).

## Optimization loop (Phase 3)

Once a campaign is live, `POST /runs/{id}/optimize` (or **Check performance**
in the UI) reads per-ad Insights (last 7 days) and produces recommendations
from deterministic rules — no LLM in the money loop:

| Signal | Action |
|---|---|
| ≥3× target CPA spent, zero conversions | `kill_ad` (pause) |
| CTR < 0.4% after 2,000+ impressions, no conversions | `kill_ad` (pause) |
| Frequency ≥ 3.5 | `refresh_creative` (swap in a held-back ad) |
| Best CPA in campaign, within target | `shift_budget` +20% |
| CPA ≥ 2× the best converting ad | `shift_budget` −20% |
| Best CPA but frequency ≥ 2.5 | `expand_audience` |
| Meaningful spend, zero conversions campaign-wide | `alert_human` (check pixel wiring) |

- **Check only** (`{"apply": false}`, default) recommends; nothing changes.
- **Apply** (`{"apply": true}`) executes only the safe automatic actions:
  pausing losers and bounded ±20% budget shifts (never below $5/day). Creative
  refresh / audience expansion / anomalies stay human follow-ups.
- Pass `target_cpa_usd` so the kill rule knows what "too expensive" means.
- Every check is appended to `optimization_log.json` (full snapshots,
  decisions, and applied actions — an audit trail).

**Run it on a schedule:** add a Railway cron service (or any scheduler) that
calls the endpoint daily:

```bash
curl -X POST https://<your-app>.up.railway.app/runs/<run_id>/optimize \
  -H 'content-type: application/json' \
  -d '{"apply": true, "target_cpa_usd": 30}'
```

### Known limits

- Interest targeting is resolved best-effort by name; unmatched interests are
  skipped.
- True app-install optimization requires the app registered in your Meta
  Business account and linked to `META_APP_ID`.
- The optimizer acts only on the campaign AdEngine created for that run.

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
| `META_PIXEL_ID` | — | optional; enables conversion-optimized web campaigns |
| `META_APP_ID` | — | optional; enables install-optimized app campaigns |
| `ADENGINE_IMAGE_PROVIDER` | — | `openai` to enable AI creative images |
| `OPENAI_API_KEY` | — | OpenAI key for image generation (or `ADENGINE_IMAGE_API_KEY`) |
| `ADENGINE_IMAGE_MODEL` | `gpt-image-1` | image model; set `dall-e-3` to skip org verification |
| `OPENAI_API_KEY` | — | image generation key (or `ADENGINE_IMAGE_API_KEY`) |
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
