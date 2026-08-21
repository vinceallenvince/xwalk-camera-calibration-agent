# xwalk-camera-calibration-agent

Relocates the [XWALK KEYBOARDS](https://github.com/vinceallenvince/xwalk-keyboards) Realtime crosswalk calibration onto a current camera frame. Deployed to Google Cloud Run and triggered by Cloud Scheduler every 15 minutes.

Traffic cameras drift over time from wind, thermal expansion, and occasional re-aims. This agent detects the current stripe positions and publishes updated calibration data so the web app's keyboard stays aligned with the painted crosswalk. Cameras are registered in `app/cameras.py` (currently 511NY View 5056 — West Street at W. 34 St — and View 5072 — West Street at Chambers St, Manhattan); each gets its own Cloud Scheduler job addressing `/api/calibrate-scheduled?cameraId=NNNN`.

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
  ├─ 2. Roboflow Workflow — detect stripe polygons
  │     Returns: instance-segmentation polygons, paint-accurate
  │
  ├─ 3. Code: group stripes into segments, index each one
  │
  ▼
  BigQuery: append one row per run (Looker Studio source)
  GCS: write current calibration JSON (web client reads this)
```

**Gemini** is the triage gate — it decides whether the crosswalk is visible and produces the `reasoning` field operators read. If it returns `no_crosswalk` or `feed_down`, the run stops.

**Roboflow** does the geometry — paint-accurate instance segmentation polygons in ~1s. This is the only approach tested that produced correct geometry on both crosswalks.

**Code** does the bookkeeping — it groups the detected bars into crosswalk segments and hands back a position for each. It never decides what a bar sounds like.

## Stripe positions, not notes

The agent is camera-agnostic. The production path holds no reference calibration, expects no particular number of stripes, and emits no note names. Pointing it at a new camera needs no hand-authored geometry.

Each published stripe carries a **`segment`** (which crosswalk run it belongs to) and a **`stripeIndex`** (its ordinal position within that run, 0-based). Both are derived from the detections alone:

- Stripe centroids are projected onto their principal axis and sorted.
- A gap wider than 25% of the frame width starts a new segment — the separation between crosswalk runs is an order of magnitude larger than the spacing within one (medians of ~136px vs ~9px on a 352px frame).
- Segments are named positionally along that axis: `segment0`, `segment1`, …

**The client owns the music.** The web app anchors notes from a single per-camera base and numbers stripes globally across segments, so it plays an ascending scale as you cross. The agent only reports where the paint is.

> **Identities wobble, deliberately.** Indexes are ordinal, so a bar the model could not see does not leave a hole — its neighbours renumber, and the scale transposes. The agent used to defend against this with boundary-anchored indexing and run-over-run segment continuity; that machinery was deleted in favour of a simpler pipeline, on the grounds that a transposed keyboard is still a keyboard and nobody crossing the street knows the mapping. Freshness — keys sitting on the paint — is the guarantee that was kept. See [`app/geometry.py`](app/geometry.py).

## API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | None | Health check |
| `POST` | `/api/calibrate` | API key | Multipart frame upload → full calibration record |
| `POST` | `/api/calibrate-scheduled` | API key | No body — fetches a frame from the camera's snapshot source, then calibrates. `?cameraId=NNNN` selects the camera; defaults to `CALIBRATION_CAMERA_ID` |

Authentication is via the `X-API-Key` header when `CALIBRATION_AGENT_API_KEY` is set. The web app authenticates using a GCP identity token (Cloud Run IAM).

## Status model

| Status | Meaning | Publishes? |
|--------|---------|------------|
| `ok` | The crosswalks in view are clearly visible, normal conditions | ✓ |
| `degraded` | Visible but conditions reduced — occlusion, shadows, dusk light, glare, or a view that no longer matches the registered scene | ✓ |
| `no_crosswalk` | No painted crosswalk visible at all — aimed somewhere without one, or fully obstructed | ✗ |
| `feed_down` | Source outage (placeholder image) | ✗ |

There is no `needs_review` status. A repositioned camera or re-striped paint is not an emergency in a camera-agnostic pipeline — the next run simply measures the new scene. Those observations live in `conditions.cameraMoved` and `conditions.repaintSuspected` instead, and a re-aim that still shows crosswalks reports `degraded`, never `no_crosswalk`.

Alongside `status`, triage reports two independent `conditions` axes: **`occlusion`** — what is physically covering the paint (`vehicle`, `construction`, `snow_cover`, …) — and **`visibility`** — the lighting or atmospheric factor reducing paint contrast (`shadows`, `dusk`, `glare`, `rain`, …). They are separate because they fail differently: an occluded stripe is hidden from any model, while low-contrast light quietly degrades detection recall. A week of run history showed dusk and late-day tree shadows cost more stripes than parked vehicles, while a streetlit night detects best of all — so `visibility` distinguishes `dusk` from `dark`, and neither is folded into an "obstruction".

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
  "stripeIndex": 7,        // ordinal position within the segment, 0-based
  "segment": "segment0",   // crosswalk run, numbered along the axis
  "polygon": [[x, y]],     // instance-segmentation outline
  "confidence": 0.93
}
```

Only detected stripes appear; there are no placeholder entries. Crosswalk boundary polygons are not published — the client hulls each segment's stripes when it needs an outline. `referenceFrame` gives the frame these coordinates were measured in, and consumers scale from it.

Every run also archives its full JSON record and source frame to GCS history:

```
gs://xwalk-keyboards-01/calibration/history/camera_5056/<runId>.json
gs://xwalk-keyboards-01/calibration/history/camera_5056/<runId>.png
```

## Project structure

```
app/
  main.py               FastAPI HTTP surface (health, calibrate, calibrate-scheduled)
  cameras.py            Per-camera registry (snapshot source + triage scene description)
  tools.py              Gemini Flash conditions triage + Roboflow stripe detection
  geometry.py           Camera-agnostic stripe placement (axis, gap clustering, indexing)
  persist.py            BigQuery + GCS persistence
  coords.py             Normalized 0-1000 ↔ source pixel conversion, image size sniffing
  storage.py            GCS storage helpers
docs/
  plan.md               Architecture plan and design decisions
images/                 Reference frames and comparison images
tests/
  test_geometry.py      Segment clustering and ordinal stripe indexing
  test_cameras.py       Per-camera registry and triage prompt specialization
  overlay.py            Draw an archived run's polygons over its frame
```

The production pipeline is `main.py` → `tools.py` → `geometry.py` → `persist.py`. There is no reasoning-loop agent: orchestration is deterministic code in `main.py`, and the only LLM prompt in the system is the triage instruction in `tools.py`. The earlier Gemini-geometry experiments (`agent.py`, `detect.py`, `match.py`, `schema.py`, `roboflow.py`, the hand-authored `reference.py` calibration, and the ADK `calibration_agent.py`) were removed once the Roboflow pipeline shipped; they live in git history.

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
| `ROBOFLOW_API_KEY` | — | Roboflow API key for stripe detection |
| `CALIBRATION_STRIPE_WORKFLOW_URL` | Roboflow serverless URL | Stripe detection workflow |
| `CALIBRATION_CAMERA_ID` | `5056` | Default camera for scheduled runs without `?cameraId` |
| `CALIBRATION_SNAPSHOT_URL_TEMPLATE` | `https://511ny.org/map/Cctv/{camera_id}` | Snapshot URL pattern for cameras without an explicit `snapshot_url` in the registry |
| `CALIBRATION_SNAPSHOT_URL` | — | Legacy override of the default camera's snapshot URL; prefer the registry/template |
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

## Adding a camera

The pipeline itself is camera-agnostic; onboarding a camera is configuration:

1. **Register it** in `app/cameras.py` — camera ID, human-readable name, and a one-sentence scene description (what the frame shows when everything is normal; the triage prompt judges frames against it). There is no geometry to declare: segments are discovered from the detections every run. Cameras on 511NY need no `snapshot_url` — the template derives it from the ID. An unregistered camera still calibrates, with a generic triage prompt.
2. **Schedule it** — create a Cloud Scheduler job targeting `/api/calibrate-scheduled?cameraId=NNNN`. One job per camera.
3. **Wire the client** — add the camera to `LIVE_CAMERAS` in xwalk-keyboards (stream URL, segment anchors, reference calibration).

## Design decisions

See [`docs/plan.md`](docs/plan.md) for the full architecture plan, including:

- Why Gemini Flash for triage and Roboflow for geometry (not VLM-only)
- Why BigQuery + GCS instead of Firestore or Cloud SQL
- The two-call architecture and cost gate
- Phase roadmap (Phase 0 → Phase 1 → Phase 2 → Phase 3)
