# Calibration Agent Plan — Revised

## Architecture

The agent runs as a Google ADK agent on Cloud Run. On each scheduled run it
makes two calls in sequence:

```
Cloud Scheduler (every 15 min)
  │
  ▼
ADK Agent (Cloud Run)
  │
  ├─ 1. Gemini 2.5 Flash  — analyse the frame
  │     "Is the crosswalk visible? What's the status?"
  │     Returns: status, conditions, reasoning (max 250 chars)
  │
  ├─ if status is ok or degraded:
  │
  ├─ 2. Roboflow Workflow  — detect the stripes
  │     Returns: N instance-segmentation polygons, paint-accurate
  │
  ├─ 3. Code: match detected polygons to reference stripeIndex/note
  │     by left-to-right order within each segment
  │
  ▼
  BigQuery: append one row per run
  GCS: write current calibration JSON (web client reads this)
```

Gemini is the triage gate. It sees the frame, decides whether the crosswalk is
visible and what conditions are present, and produces the `reasoning` field that
operators read. If it returns `no_crosswalk` or `feed_down`, the run stops — no
point calling Roboflow on a blank frame or an outage placeholder.

Roboflow does the geometry. It returns paint-accurate instance polygons in ~1s
for ~$0.001, and it is the only approach tested that produced correct geometry
on both crosswalks.

Code does the bookkeeping. Stripe identity (which bar is which note) is assigned
deterministically by position, not by the model. A count mismatch surfaces as an
explicit discrepancy, not a silent renumbering.

### Why Gemini Flash, not Pro

The triage call does not need grounding precision — it needs to look at a frame
and say "snow on the left crosswalk, camera has shifted slightly, confidence
0.85" in under 250 characters. Flash is fast, cheap, and well suited to
classification + short-form reasoning. Pro's extra cost and latency buy nothing
here; the geometry work that needed precision is Roboflow's job now.

### Why two calls, not one

- Roboflow cannot reason. It returns polygons and confidence scores, but it
  cannot explain *why* a stripe is missing ("occluded by a truck" vs "painted
  over" vs "camera re-aimed"), and it cannot detect conditions that are not in
  its training set (snow, glare, construction, a re-aim away from the
  intersection). The status field needs language.
- Gemini cannot ground precisely. Every VLM we tested — Gemini 2.5 Pro, Sol
  5.6, Gemini 3.1 Pro via consumer — produced geometry that was measurably
  worse than Roboflow's purpose-trained segmentation model on this specific
  task. Using it for geometry was the wrong tool.
- Gemini-first is a cost gate. If the feed is down or the crosswalk is gone,
  skipping Roboflow saves the detection call entirely. At 96 runs/day this is
  negligible, but it is architecturally clean: do not run detection on frames
  that cannot produce calibration.

## ADK tool structure

The agent is a Google ADK agent with two tools:

```python
@tool
def analyse_conditions(frame: bytes) -> ConditionsResult:
    """Gemini Flash: classify the frame, return status/conditions/reasoning."""

@tool
def detect_stripes(frame: bytes) -> DetectionResult:
    """Roboflow workflow: return matched stripe polygons."""
```

The agent's instruction says: "First call `analyse_conditions`. If status is
`no_crosswalk` or `feed_down`, stop and return the conditions. Otherwise call
`detect_stripes` and return both."

This keeps the orchestration in the agent's reasoning loop rather than hardcoded,
so it can adapt if the conditions call reveals something unexpected.

## Persistence: BigQuery + GCS

### BigQuery — the historical record, Looker Studio source

One table, one row per run. This is the dataset Looker Studio queries.

```sql
CREATE TABLE IF NOT EXISTS `xwalk-keyboards-01.calibration.runs` (
  run_id            STRING      NOT NULL,
  camera_id         INT64       NOT NULL,
  created_at        TIMESTAMP   NOT NULL,
  status            STRING      NOT NULL,  -- ok | degraded | no_crosswalk | feed_down | needs_review
  reasoning         STRING,                -- max 250 chars, from Gemini
  conditions        JSON,                  -- { crosswalkVisible, obstruction, cameraMoved, repaintSuspected }
  confidence        FLOAT64,
  stripe_count      INT64,                 -- how many stripes Roboflow found (before matching)
  visible_count     INT64,                 -- how many matched to reference
  expected_count    INT64,                 -- reference stripe count (25)
  count_match       BOOL,                  -- stripe_count == expected per segment
  max_confidence    FLOAT64,               -- highest stripe confidence
  min_confidence    FLOAT64,               -- lowest stripe confidence
  mean_confidence   FLOAT64,
  model             STRING,                -- gemini model used for conditions
  elapsed_ms        INT64,                 -- total wall-clock time
  gemini_tokens     INT64,                 -- token usage for the conditions call
  stripes           JSON,                  -- full polygon data, for drill-down
  matching_notes    JSON,                  -- any count mismatch details
  published         BOOL                   -- whether this run updated the live calibration
);
```

**Why BigQuery, not Firestore or Cloud SQL:**
- Looker Studio connects to BigQuery natively. Firestore requires an export
  pipeline; Cloud SQL requires a connector and an always-on instance.
- The query pattern is analytical (time-series, aggregation, filtering by
  status/conditions over weeks of data), not transactional.
- BigQuery is serverless and pay-per-query at this volume (~96 rows/day).
- The Storage Write API appends rows without a full table scan, and the
  `google-cloud-bigquery` client handles it in a few lines.

### GCS — the live calibration the web client reads

The web client needs exactly one thing: the current calibration for camera 5056.
That is a single JSON file, overwritten atomically on each successful run.

```
gs://xwalk-keyboards-01/calibration/current/camera_5056.json
```

The web app's Next.js API route reads this on page load (and periodically for
long-lived sessions). GCS is the right store for this: no connection pooling, no
VPC connector, no always-on instance, and object versioning gives rollback.

BigQuery is not a good live-read store — query latency is seconds, not
milliseconds, and the pricing model penalises frequent small reads.

### The two stores serve different consumers

| | BigQuery | GCS |
|---|---|---|
| Consumer | Looker Studio, operators, offline analysis | Web client, page load |
| Access pattern | Analytical queries over historical data | Single-key read of current state |
| Write pattern | Append one row per run | Overwrite one object per successful run |
| Latency | Seconds (acceptable for dashboards) | Milliseconds (required for page load) |

Both are written on every run. GCS is only overwritten when the run produces
a publishable calibration (status `ok` and gates pass).

## Looker Studio dashboard

The BigQuery table directly supports these views:

- **Camera health timeline.** Status over time, coloured by
  ok/degraded/no_crosswalk/feed_down. Shows uptime at a glance.
- **Drift chart.** Stripe count and confidence over time. A sudden drop in
  visible stripes or confidence signals a problem before it reaches the web
  client.
- **Conditions breakdown.** Pie/bar of obstruction types, camera movement
  frequency. Answers "how often is traffic occluding the crosswalk?"
- **Reasoning log.** A table of the last N `reasoning` strings, filterable by
  status. This is the human-readable audit trail.
- **Detection latency.** `elapsed_ms` over time, split by whether Roboflow was
  called (status ok/degraded) or skipped (no_crosswalk/feed_down).

## Status model and web client behaviour

Unchanged from the prior plan:

| Status | Meaning | Client |
|---|---|---|
| `ok` | Fresh, validated, counts match | Normal operation |
| `degraded` | Serving last-known-good; obstruction or low confidence | Run normally, quiet advisory |
| `no_crosswalk` | Camera re-aimed or fully obstructed | Explain the study is unavailable |
| `feed_down` | Source outage | Existing feed-unavailable treatment |
| `needs_review` | Count mismatch or large change | Serve last-known-good, advisory |

`reasoning` carries Gemini's explanation, so an operator sees "left crosswalk
stripes 1-6 occluded by stopped traffic" rather than "confidence 0.42".

## Phases

### Phase 0 — done

Built the agent, tested three detection approaches (Gemini anchored, cold
detect-then-match, Roboflow), and confirmed Roboflow produces paint-accurate
geometry with correct counts in <1s. VLM-only geometry was measurably worse
across all variants.

### Phase 1 — build and deploy

- Wire the ADK agent with two tools (Gemini Flash conditions + Roboflow
  detection).
- Create the BigQuery dataset and table.
- Write both stores on every run.
- Deploy to Cloud Run, add Cloud Scheduler.
- Build the Looker Studio dashboard over the BigQuery table.
- The web client continues using its baked-in calibration; it does not read from
  GCS yet. Every run is recorded, nothing is promoted.

### Phase 2 — go live

- The web client reads `gs://…/current/camera_5056.json` on page load via a
  same-origin Next.js route.
- Periodic re-fetch (5–15 min) for long-lived sessions.
- Successful runs overwrite the GCS object; failed runs leave it alone.
- The Looker Studio dashboard is the operational surface for monitoring.

### Phase 3 — refinements

- Extend to the twelve Orchestration snapshot cameras (schema is per-camera from
  day one).
- Inside/outside classification moved client-side, making calibration
  hot-swappable with no inference restart.
- Adaptive cadence based on the drift history visible in BigQuery.
- Gemini for reasoning about *why* a count mismatch happened (Roboflow says "I
  found 16", Gemini says "two stripes are under a bus").

## Open decisions

1. **Gemini Flash model ID.** `gemini-2.5-flash` is confirmed available in
   `us-central1`. Alternatively `gemini-2.0-flash` if cost is a concern — the
   classification task is simple.
2. **Publishing gate.** Phase 2: should a run auto-publish to GCS when status is
   `ok` and counts match, or require explicit approval? The dashboard makes
   either workflow viable.
3. **Cloud Scheduler vs Cloud Run jobs.** Scheduler invoking the service
   endpoint is simpler; a Cloud Run job is cleaner for a batch workload with no
   HTTP surface. Either works.
