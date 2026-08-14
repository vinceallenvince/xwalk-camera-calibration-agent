# xwalk-camera-calibration-agent

Relocates the [XWALK KEYBOARDS](https://github.com/vinceallenvince/xwalk-keyboards) Realtime crosswalk calibration onto a current camera frame. Deployed to Google Cloud Run and triggered by Cloud Scheduler every 15 minutes.

The camera (511NY View 5056 — West Street at W. 34 St, Manhattan) drifts over time from wind, thermal expansion, and occasional re-aims. This agent detects the current stripe positions and publishes updated calibration data so the web app's keyboard stays aligned with the painted crosswalk.

## How it works

Each calibration run makes two calls in sequence:

```
Cloud Scheduler (every 15 min)
  │
  ▼
Calibration Agent (Cloud Run)
  │
  ├─ 1. Gemini 2.5 Flash  — classify the frame
  │     Is the crosswalk visible? What are the conditions?
  │     Returns: status, conditions, reasoning
  │
  ├─ if crosswalk is visible:
  │
  ├─ 2a. Roboflow Workflow — detect stripe polygons
  │      Returns: 25 instance-segmentation polygons, paint-accurate
  │
  ├─ 2b. Roboflow Workflow — detect crosswalk boundaries
  │      Returns: left/right crosswalk bounding polygons
  │
  ├─ 3. Code: match detected polygons to reference stripe index/note
  │
  ▼
  BigQuery: append one row per run (Looker Studio source)
  GCS: write current calibration JSON (web client reads this)
```

**Gemini** is the triage gate — it decides whether the crosswalk is visible and produces the `reasoning` field operators read. If it returns `no_crosswalk` or `feed_down`, the run stops.

**Roboflow** does the geometry — paint-accurate instance segmentation polygons in ~1s. This is the only approach tested that produced correct geometry on both crosswalks.

**Code** does the bookkeeping — stripe identity (which bar maps to which note) is assigned deterministically by position, never by the model.

## Reference calibration

The agent relocates a known calibration, it does not discover crosswalks from scratch. The hand-authored reference in [`app/reference.py`](app/reference.py) defines:

- **25 crosswalk stripes** — 18 on the left crosswalk (C4–F5), 7 on the right (F#5–B5)
- **Stripe identity** — each stripe has a fixed index, segment (left/right), and MIDI note
- **Cell geometry** — each stripe is a 4-point quadrilateral centred on a painted bar, extending to the mid-gap on each side so there are no dead zones between keys
- **Reference frame** — 352 × 240 native HLS frame of View 5056

Kept in sync with `src/lib/realtime-calibration.ts` in the xwalk-keyboards web app.

## API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | None | Health check |
| `POST` | `/api/calibrate` | API key | Multipart frame upload → full calibration record |
| `POST` | `/api/calibrate-scheduled` | API key | No body — fetches a frame from 511NY, then calibrates |

Authentication is via the `X-API-Key` header when `CALIBRATION_AGENT_API_KEY` is set. The web app authenticates using a GCP identity token (Cloud Run IAM).

## Status model

| Status | Meaning | Publishes? |
|--------|---------|------------|
| `ok` | Fresh, validated, stripe counts match | ✓ |
| `degraded` | Visible but partially obstructed or low quality | ✗ |
| `no_crosswalk` | Camera re-aimed or fully obstructed | ✗ |
| `feed_down` | Source outage (placeholder image) | ✗ |
| `needs_review` | Stripe count mismatch or large change | ✗ |

Only `ok` runs overwrite the live calibration in GCS. Failed runs are recorded in BigQuery but leave the live calibration unchanged.

## Persistence

### BigQuery — historical record

One row per run in `xwalk-keyboards-01.calibration.runs`. This is the Looker Studio data source for operational dashboards (camera health timeline, drift charts, conditions breakdown, reasoning log).

### GCS — live calibration

The web client reads one JSON file on page load:

```
gs://xwalk-keyboards-01/calibration/current/camera_5056.json
```

Every run also archives its full JSON record and source frame to GCS history:

```
gs://xwalk-keyboards-01/calibration/history/camera_5056/<runId>.json
gs://xwalk-keyboards-01/calibration/history/camera_5056/<runId>.png
```

## Project structure

```
app/
  main.py               FastAPI HTTP surface (health, calibrate, calibrate-scheduled)
  agent.py              Gemini Pro calibration agent (unused in current pipeline)
  calibration_agent.py  ADK agent definition
  tools.py              Gemini Flash conditions triage + Roboflow stripe/boundary detection
  reference.py          Hand-authored View 5056 calibration (25 stripes, notes, polygons)
  coords.py             Normalized 0-1000 ↔ source pixel conversion
  schema.py             Gemini structured-output response schema
  detect.py             Detection utilities
  match.py              Stripe matching logic
  roboflow.py           Roboflow client helpers
  persist.py            BigQuery + GCS persistence
  storage.py            GCS storage helpers
docs/
  plan.md               Architecture plan and design decisions
images/                 Reference frames and comparison images
tests/
  compare_variants.py   Head-to-head geometry comparison
  overlay.py            Visual overlay of detected vs reference stripes
  run_local.py          Local calibration runner
```

## Setup

**Requirements:** Python ≥ 3.11, [uv](https://docs.astral.sh/uv/)

```bash
uv sync
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_CLOUD_PROJECT` | `xwalk-keyboards-01` | GCP project ID |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | Vertex AI region |
| `CALIBRATION_AGENT_API_KEY` | — | API key for request authentication |
| `CALIBRATION_TRIAGE_MODEL` | `gemini-2.5-flash` | Gemini model for conditions triage |
| `CALIBRATION_AGENT_MODEL` | `gemini-2.5-pro` | Gemini model for full calibration |
| `CALIBRATION_UPSCALE` | `4` | Upscale factor before sending to Gemini |
| `ROBOFLOW_API_KEY` | — | Roboflow API key for stripe/boundary detection |
| `CALIBRATION_STRIPE_WORKFLOW_URL` | Roboflow serverless URL | Stripe detection workflow |
| `CALIBRATION_BOUNDARY_WORKFLOW_URL` | Roboflow serverless URL | Boundary detection workflow |
| `CALIBRATION_SNAPSHOT_URL` | `https://511ny.org/map/Cctv/5056` | Camera snapshot URL for scheduled runs |
| `CALIBRATION_BUCKET` | `xwalk-keyboards-01` | GCS bucket for calibration data |
| `CALIBRATION_BQ_TABLE` | `xwalk-keyboards-01.calibration.runs` | BigQuery table for run history |
| `CALIBRATION_ACCESS_TOKEN` | — | Explicit access token for local development |

### Running locally

```bash
uv run uvicorn app.main:app --reload --port 8080
```

For local Vertex AI access, either set `CALIBRATION_ACCESS_TOKEN` to the output of `gcloud auth print-access-token`, or ensure Application Default Credentials are configured:

```bash
gcloud auth application-default set-quota-project xwalk-keyboards-01
```

### Running tests

```bash
uv run pytest
```

## Deployment

Deployed to Cloud Run in `us-central1` (project `xwalk-keyboards-01`):

```bash
gcloud config set project xwalk-keyboards-01
gcloud run deploy xwalk-camera-calibration-agent \
  --source . \
  --region us-central1
```

The Dockerfile uses `uv sync --no-dev` to exclude test dependencies (pytest, Pillow) from the runtime image.

## Design decisions

See [`docs/plan.md`](docs/plan.md) for the full architecture plan, including:

- Why Gemini Flash for triage and Roboflow for geometry (not VLM-only)
- Why BigQuery + GCS instead of Firestore or Cloud SQL
- The two-call architecture and cost gate
- Phase roadmap (Phase 0 → Phase 1 → Phase 2 → Phase 3)
