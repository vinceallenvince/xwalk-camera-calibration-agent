# XWALK Camera Calibration Agent

**Part of [XWALK KEYBOARDS](https://xwalkkeyboards.app)**

This agent keeps the crosswalk keyboard aligned with the painted stripes.
Traffic cameras drift from wind, thermal expansion, and occasional re-aims —
enough to shift the crosswalk tens of pixels in frame space. Without
recalibration, pedestrians walk the real crosswalk but miss the drifted
polygons, and the instrument goes silent. This agent detects where the stripes
are in the current camera frame and publishes their positions so the web app's
keyboard stays on the paint.

[![XWALK KEYBOARDS — fullscreen Realtime study](https://www.vinceallen.com/images/xwalk-keyboards/screenshot-02.png)](https://xwalkkeyboards.app)

## How it works

Cloud Scheduler triggers the agent every 15 minutes per camera. Each run:

1. **Fetches a frame** — a snapshot from [511NY](https://511ny.org) for the
   Manhattan cameras, or one keyframe from the City of Bellevue's video
   stream for the Bellevue cameras.
2. **Gemini 2.5 Flash classifies the frame** — is the crosswalk visible? What
   are the conditions (occlusion, shadows, dusk, glare)? If the feed is down or
   the camera has rotated away, the run stops here.
3. **Roboflow detects stripe polygons** — paint-accurate instance segmentation
   in ~1 second.
4. **Code clusters and indexes** — groups stripes into crosswalk segments by
   gap, numbers each one ordinally.
5. **Publishes to GCS** — the web app fetches this JSON on page load and every
   5 minutes for long sessions. Run history goes to BigQuery for dashboards.

```
Cloud Scheduler ──▶ Calibration Agent (Cloud Run)
                         │
                         ├─ Gemini Flash: classify frame
                         ├─ Roboflow: detect stripe polygons
                         ├─ Code: cluster + index
                         │
                         ├──▶ GCS: live calibration JSON
                         └──▶ BigQuery: run history
```

The pipeline is deterministic — no reasoning-loop agent. The only LLM call is
the triage classification. Orchestration is linear Python code.

## Stripe positions, not notes

The agent is camera-agnostic. It publishes where the paint is — segment names
and ordinal stripe positions — and no note names. The
[web app](https://github.com/vinceallenvince/xwalk-keyboards) owns the
musical contract: it anchors notes from a single per-camera base and numbers
stripes globally across segments, so the crossing plays an ascending chromatic
scale. The agent owns geometry; the app owns the music.

Stripe identities are not stable across runs, by design. A stripe the model
could not see does not leave a hole — its neighbours renumber, and the scale
transposes. A transposed keyboard is still a keyboard; stale geometry that no
longer sits on the paint is a broken one.

## Design

- **[Figma — System Design & Calibration Strategy](https://www.figma.com/board/A6NvdXlqnQF4ZXUJuwrGbD/XWALK-KEYBOARDS---System-Diagram?node-id=0-1&t=UvAg5BVHz0mrPyqh-1)** — architecture diagrams and calibration design
- **[Architecture](docs/architecture.md)** — full technical architecture and module dependency graph
- **[Plan](docs/plan.md)** — design decisions and phase roadmap

## Related repositories

- **[xwalk-keyboards](https://github.com/vinceallenvince/xwalk-keyboards)** — the web app that turns the calibration data into a playable instrument

## Development

**Requirements:** Python ≥ 3.11, [uv](https://docs.astral.sh/uv/)

```bash
uv sync                                           # install dependencies
uv run uvicorn app.main:app --reload --port 8080   # run locally
uv run pytest                                      # run tests
```

Environment variables are listed in
[`docs/architecture.md`](docs/architecture.md#environment-variables). Server-only
secrets (`ROBOFLOW_API_KEY`, `CALIBRATION_AGENT_API_KEY`) belong in Secret
Manager for Cloud Run deployments.

## Deployment

Deployed to Cloud Run in `us-central1` (project `xwalk-keyboards-01`). Merges
to `main` auto-deploy via GitHub Actions.
