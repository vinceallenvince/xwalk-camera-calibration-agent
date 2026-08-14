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

**Code** does the bookkeeping — it places each detected bar on its crosswalk and hands back a position. It never decides what a bar sounds like.

## Stripe positions, not notes

The agent is camera-agnostic. The production path holds no reference calibration, expects no particular number of stripes, and emits no note names. Pointing it at a new camera needs no hand-authored geometry.

Each published stripe carries a **`stripeIndex`**: its slot position along the crosswalk, measured from the boundary's leading edge in units of the stripe pitch. Two properties make that usable as an identity:

- It is measured against the **crosswalk boundary**, not the detection list, so a bar keeps its index when a vehicle hides its neighbours.
- The pitch is the **median** gap between detections, which survives missing bars — one absent stripe makes a single double-width gap, and the median ignores it.

Gaps in the index sequence are meaningful: they are bars the model could not see in that frame.

Crosswalk segments are named by their left-to-right order in the frame (`left`, `right`, then `segment3`…), so nothing depends on where a particular camera's crosswalks sit.

**The client owns the music.** Mapping an index to a pitch is the web app's job — see `SCALE_BY_SEGMENT` in `src/lib/use-calibration.ts` in xwalk-keyboards. The agent only reports where the paint is.

> **Known limit.** If a detected boundary grows or shrinks by a whole stripe pitch, every index shifts by one — genuinely indistinguishable from a crosswalk with one more bar at its leading edge. Sub-pitch noise, the realistic case, is cancelled by phase-snapping to the detections' own lattice. Treat the index as a stable *relative* position, not an absolute anchor. See [`app/geometry.py`](app/geometry.py).

`app/reference.py` still backs the alternate Gemini geometry paths (`agent.py`, `detect.py`, `match.py`), which are not part of the production pipeline.

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
| `ok` | Both crosswalks clearly visible, normal conditions | ✓ |
| `degraded` | Visible but partially obstructed or low quality | ✓ |
| `needs_review` | Camera appears repositioned, or the paint re-striped | ✓ |
| `no_crosswalk` | Camera re-aimed away, or fully obstructed | ✗ |
| `feed_down` | Source outage (placeholder image) | ✗ |

Publishing is gated on **detection, not classification**. Gemini has already rejected frames with no crosswalk in them, so any stripes Roboflow returns describe real paint — a run publishes whenever it detected at least one stripe.

This is deliberate. Occlusion varies frame to frame, and a partial read is still a correct read: every stripe carries its own position, so the client renders what it is given without needing a complete set. An earlier version required the detected stripe count to match a 25-stripe reference exactly, which rejected **346 consecutive runs** between 2026-08-11 and 2026-08-14 while the camera drifted — the web app kept serving a stale calibration that no longer sat on the paint.

Runs that publish nothing are still recorded in BigQuery and archived to GCS history, leaving the live calibration untouched.

## Persistence

### BigQuery — historical record

One row per run in `xwalk-keyboards-01.calibration.runs`. This is the Looker Studio data source for operational dashboards (camera health timeline, drift charts, conditions breakdown, reasoning log).

### GCS — live calibration

The web client reads one JSON file on page load:

```
gs://xwalk-keyboards-01/calibration/current/camera_5056.json
```

Each stripe in it is pure geometry — a position and an outline, in source pixels:

```jsonc
{
  "stripeIndex": 7,        // slot along the crosswalk, 0-based, per segment
  "segment": "left",       // crosswalk name, left-to-right in the frame
  "polygon": [[x, y]],     // instance-segmentation outline
  "confidence": 0.93
}
```

Only detected stripes appear; there are no placeholder entries. `referenceFrame` gives the frame these coordinates were measured in, and consumers scale from it.

Every run also archives its full JSON record and source frame to GCS history:

```
gs://xwalk-keyboards-01/calibration/history/camera_5056/<runId>.json
gs://xwalk-keyboards-01/calibration/history/camera_5056/<runId>.png
```

## Project structure

```
app/
  main.py               FastAPI HTTP surface (health, calibrate, calibrate-scheduled)
  tools.py              Gemini Flash conditions triage + Roboflow stripe/boundary detection
  geometry.py           Camera-agnostic stripe placement (axis, pitch, slot indexing)
  persist.py            BigQuery + GCS persistence
  coords.py             Normalized 0-1000 ↔ source pixel conversion, image size sniffing
  storage.py            GCS storage helpers
  schema.py             Gemini structured-output response schema
  calibration_agent.py  ADK agent definition
  reference.py          Hand-authored View 5056 calibration — alternate paths only
  agent.py              Gemini Pro calibration agent (unused in current pipeline)
  detect.py             Detection utilities (unused in current pipeline)
  match.py              Stripe matching logic (unused in current pipeline)
  roboflow.py           Roboflow client helpers (unused in current pipeline)
docs/
  plan.md               Architecture plan and design decisions
images/                 Reference frames and comparison images
tests/
  test_geometry.py      Stripe placement under occlusion and boundary jitter
  compare_variants.py   Head-to-head geometry comparison
  overlay.py            Visual overlay of detected vs reference stripes
  run_local.py          Local calibration runner
```

The production pipeline is `main.py` → `tools.py` → `geometry.py` → `persist.py`. The files marked unused are the earlier Gemini-geometry experiments, kept for reference.

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
