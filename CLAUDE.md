# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A Cloud Run service that keeps [xwalk-keyboards](https://github.com/vinceallenvince/xwalk-keyboards)
aligned with the painted crosswalk: Cloud Scheduler triggers it every 15
minutes, it classifies the camera frame (Gemini Flash), detects stripe
polygons (Roboflow), groups them into crosswalk segments and indexes each one
(`geometry.py`), and publishes the result to GCS/BigQuery.

Read [docs/architecture.md](docs/architecture.md) before making non-trivial
changes — it documents the pipeline stages, data shapes, and design decisions.

## Commands

```bash
uv run pytest                                  # tests (add --no-sync if dependency sync fails)
uv run uvicorn app.main:app --reload --port 8080   # local server
gcloud run deploy xwalk-camera-calibration-agent --source . --region us-central1
```

Local Vertex AI access: set `CALIBRATION_ACCESS_TOKEN` to `gcloud auth
print-access-token` output, or use Application Default Credentials.

## Architecture in one breath

`main.py` → `tools.py` → `geometry.py` → `persist.py`. Orchestration is
**deterministic Python** in `main.run_calibration()` — there is no
reasoning-loop agent. The only LLM prompt in the system is
`conditions_instruction()` in `tools.py`, specialized per camera from the
registry in `cameras.py`. `coords.py` sniffs image sizes. Nothing carries
state between runs.

## Load-bearing contracts

- **The client owns the music.** The agent publishes geometry only:
  `stripeIndex` (ordinal position within a segment), `segment`, `polygon`. The
  web app anchors notes from a per-camera base and numbers globally across
  segments. Never emit note names here.
- **Stripe identities are allowed to wobble (VIN-44, 2026-08-21).** Indexes
  are ordinal within a segment, so a stripe the model missed renumbers its
  neighbours rather than leaving a hole. Segments are re-derived every run by
  gap-clustering the detections. Nothing is measured against a boundary and
  nothing is remembered between runs. A renumbering transposes the scale, and
  that was judged an acceptable price for deleting the machinery that
  defended against it — do not reintroduce stability heuristics without
  reopening that decision.
- **No per-camera geometry in `cameras.py`.** Crosswalk counts and detection
  thresholds were both deleted; a camera declares only where to fetch a frame
  and what the scene looks like. Onboarding is scene description + schedule.
- **The clustering threshold is empirical.** `SEGMENT_GAP_FRACTION = 0.25` of
  frame width was chosen by replaying 341 archived runs across both cameras.
  Changing it needs a fresh replay, not a hunch — and note that a threshold
  relative to the median stripe gap was tried and is measurably worse.
- **Publishing is gated on detection, not classification.** Any run with ≥1
  detected stripe publishes. Partial reads are correct reads; never
  reintroduce an expected-count gate.
- **Status enum is `ok | degraded | no_crosswalk | feed_down`.** There is no
  `needs_review`. Triage reports two independent conditions axes:
  `occlusion` (physical) and `visibility` (lighting) — keep them separate.

## Consumers to keep in mind

- Web app reads `gs://xwalk-keyboards-01/calibration/current/camera_NNNN.json`
  (via a Next.js route, every 5 min). It uses `stripes`/`status` and ignores
  `conditions`. Boundary polygons are no longer published — the client hulls
  each segment's stripes itself.
- BigQuery `xwalk-keyboards-01.calibration.runs` feeds Looker Studio. Row
  shape changes in `persist.py` need a matching table schema.
- Every run archives its JSON + source frame to GCS history; inspect any run
  with `uv run python tests/overlay.py <frame.png> <record.json>`.

## Workflow

- Work is tracked in Linear (VIN-*). Branch from `main`, open a PR, one
  concern per commit. Merging does not deploy — Cloud Run deploys are manual.
- Debugging production: BigQuery for run history (status/reasoning/published),
  GCS history for the exact frame and record of any run, `tests/overlay.py`
  for eyeballing geometry.
