# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A Cloud Run service that keeps [xwalk-keyboards](https://github.com/vinceallenvince/xwalk-keyboards)
aligned with the painted crosswalk: Cloud Scheduler triggers it every 15
minutes, it classifies the camera frame (Gemini Flash), detects stripe and
boundary polygons (Roboflow), places each stripe along its crosswalk
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
registry in `cameras.py`. `continuity.py` keeps segment names stable across
runs; `coords.py` sniffs image sizes.

## Load-bearing contracts

- **The client owns the music.** The agent publishes geometry only:
  `stripeIndex` (slot along the crosswalk), `segment` (crosswalk name),
  `polygon`. The web app maps index→note and segment→pitch anchor. Never emit
  note names here.
- **Index stability is the whole product.** A silent renumbering transposes
  the instrument. Indexes are measured from the boundary's leading edge in
  units of the median stripe pitch — see `geometry.py`'s docstring before
  touching placement.
- **Segment names must mean the same physical crosswalk run over run.**
  `reconcile_segments()` matches detections to the published baseline by bbox
  IoU; a previously published crosswalk going unmatched is a regression that
  holds the publish. Baseline segments beyond the camera's
  `expected_crosswalks` are phantoms and retire without regression — do not
  "fix" that into a hold, it deadlocks publishing (2026-08-16).
- **Publishing is gated on detection, not classification.** Any run with ≥1
  detected stripe and no continuity regression publishes. Partial reads are
  correct reads; never reintroduce an expected-count gate.
- **Status enum is `ok | degraded | no_crosswalk | feed_down`.** There is no
  `needs_review`. Triage reports two independent conditions axes:
  `occlusion` (physical) and `visibility` (lighting) — keep them separate.

## Consumers to keep in mind

- Web app reads `gs://xwalk-keyboards-01/calibration/current/camera_NNNN.json`
  (via a Next.js route, every 5 min). It uses `crosswalks`/`stripes`/`status`
  and ignores `conditions`. The flattened `leftCrosswalk`/`rightCrosswalk`
  aliases exist for older readers — keep publishing them.
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
