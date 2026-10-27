# Calibration Agent Shutdown Plan

Status: **complete, 2026-10-27.** Steps 1–6 are done. Step 7 (archiving the
repo) is not planned. The inventory below records the system as it was on
2026-10-26, before shutdown.

The live-detection features of XWALK KEYBOARDS are being retired. The site
keeps its homepage (now a YouTube video of recorded crossings, see the
[Figma](https://www.figma.com/design/VgqJ1zX1eByz9c8Ui8otpY/XWALK-KEYBOARDS?node-id=506-449&m=dev)),
the about page, and the camera registry. Nothing on the site will need fresh
calibration, so this service and the jobs that drive it can stop.

The web changes are tracked in a separate session. This plan covers the
calibration agent, plus the web dependencies that gate its steps.

## Guiding principles

- **The web stops reading before the agent's data goes away.** Pausing the
  agent only freezes `calibration/current/`. Deleting those files while the
  web still reads them changes what the homepage shows (see "Web
  dependencies").
- **Pause, soak, then delete.** Pausing a scheduler job can be undone with one
  command. Deleting the service or images is harder to undo. Each destructive
  step follows a reversible one that has had time to prove nothing depends on
  it.
- **Delete only what is this service's alone.** The project also runs the
  `xwalk-keyboards` web service, which shares the secrets, the image
  repository, the source bucket, and the runtime service account.
- **Keep the record.** The BigQuery run history and the GCS archive are kept
  unless a later decision says otherwise.

## Live inventory (2026-10-26)

### Belongs to the calibration agent: shut down

| Resource | Detail |
| --- | --- |
| Cloud Scheduler, `us-central1` | 7 jobs, `ENABLED`, `*/15 * * * *` UTC: `calibrate-camera-5056`, `-5059`, `-5072`, `-80003`, `-80007`, `-80009`, `-80027`. They authenticate by OIDC as `21826886868-compute@developer.gserviceaccount.com`. No other regions have jobs. |
| Cloud Run service `xwalk-camera-calibration-agent` | `us-central1`, runs as the default compute SA. `roles/run.invoker` is granted to `github-deploy@`. |
| GitHub Actions `deploy.yml` | Redeploys the service on every push to `main`. There are no Cloud Build triggers; this workflow is the only deploy path. |
| Images | 34 images under `cloud-run-source-deploy/xwalk-camera-calibration-agent`. The repository itself is shared. |
| Roboflow stripe workflow | Called only by this service. |
| **Roboflow key on the service** | `ROBOFLOW_API_KEY` is a **plaintext env var** on the service, not a Secret Manager reference. Anyone with `run.services.get` can read it. It disappears when the service is deleted, but the key must be revoked separately (step 5). |

Traffic over the last 7 days: 4,708 requests, all from
`Google-Cloud-Scheduler` to `/api/calibrate-scheduled`. Nothing else calls the
service.

### Shared or unrelated: do NOT delete as part of this plan

| Resource | Why it stays |
| --- | --- |
| Secret `roboflow-api-key` | Read by the `xwalk-keyboards` web service (realtime WebRTC detection). It is not used by the agent. |
| Secret `crosswalk-agent-api-key` | Read by the web service to call `crosswalk-score-agent`, a different service in another project. Despite the name, it is not this agent's key. `CALIBRATION_AGENT_API_KEY` is not set on the agent; it relies on Cloud Run IAM. |
| Artifact Registry repo `cloud-run-source-deploy` | Also holds 93 `xwalk-keyboards` images. |
| Bucket `run-sources-xwalk-keyboards-01-us-central1` | Source uploads for both services. |
| SA `21826886868-compute@` | Runtime SA for both services and the scheduler's OIDC identity. |
| SA `github-deploy@`, WIF pool `github-pool` | Shared with the xwalk-keyboards repo. |
| Cloud Run job `carla-hls-vpc-probe` | An unscheduled CARLA HLS probe, unrelated to calibration. It last ran 2026-09-14. Out of scope; clean up separately if it's dead. |

### Data

| Store | Size |
| --- | --- |
| BigQuery `calibration.runs` | 29,357 rows, ~290 MB |
| GCS `calibration/history/` | ~5.7 GB (JSON + frames) |
| GCS `calibration/current/` | 7 small JSON files, read by the web app |

## Web dependencies (for the web session)

In xwalk-keyboards `main`, three routes touch the agent or its output:

| Route | Used by | What happens once the agent stops |
| --- | --- | --- |
| `POST /api/calibration/recalibrate` | `realtime-camera.tsx` | Calls the agent's Cloud Run URL directly. It breaks once the service is deleted. No calls in the last 7 days. |
| `GET /api/calibration/[cameraId]` | `use-calibration.ts` (realtime keyboard) | Serves frozen geometry after the pause, and fails after `current/` is deleted. |
| `GET /api/calibration/status` | `use-camera-links.ts` → `home-experience.tsx` | **Homepage camera links.** After the pause, each camera's last status is frozen. A camera last seen as `no_crosswalk` stays hidden for good. After `current/` is deleted, the hook falls back to `defaultCameraLinks()`. |

The homepage and camera registry are being kept, so the web session needs to
decide whether camera links still depend on calibration status. If they
don't, that dependency should be removed. Either way, all three routes must
stop being called before step 4.

## Steps

### 1. Pause the scheduler (reversible, safe now)

**Done 2026-10-27 17:08Z.** All 7 jobs are `PAUSED`. The last run landed at
17:01:06Z. At 17:18Z, past the 17:15 window, BigQuery, the Cloud Run request
log, and GCS history all showed nothing new.

```bash
for c in 5056 5059 5072 80003 80007 80009 80027; do
  gcloud scheduler jobs pause calibrate-camera-$c \
    --location us-central1 --project xwalk-keyboards-01
done
```

This stops all Gemini and Roboflow spend from the agent. The site keeps
serving the last published calibration.

Verify: after the next quarter-hour, there are no new rows in
`calibration.runs` and no new Cloud Run request logs on the service.

Rollback: the same loop with `resume`.

### 2. Stop redeploys from this repo

**Done 2026-10-27.** Merged in #21. The merge triggered no deploy.

In one PR, before any other PR lands on `main` in this repo:

- Delete `.github/workflows/deploy.yml`, or change its trigger to
  `workflow_dispatch` only. Otherwise the merge itself redeploys the service.
- Mark the README and CLAUDE.md as retired, with a pointer to this plan.

### 3. Soak while the web changes ship (about one week)

**Cut short 2026-10-27.** xwalk-keyboards #66 deleted all three calibration
routes and the Roboflow realtime routes, and it went live as revision
`xwalk-keyboards-00099-mg8` at 18:26Z. The agent had no requests after the
pause.

- Watch the service's request logs. With the scheduler paused there should be
  none. A hit on `/api/calibrate` means the web's recalibrate route is still
  in use.
- The web session confirms that none of the three routes above are still
  called in production.

### 4. Remove the agent (destructive, after the soak)

**Done 2026-10-27.** All 5 items below were completed and verified:

- The 7 scheduler jobs are deleted, and none remain in `us-central1`.
- The service is deleted. `xwalk-keyboards` is the only Cloud Run service
  left.
- The `xwalk-camera-calibration-agent` image package is deleted.
  `cloud-run-source-deploy` now holds only `xwalk-keyboards`.
- This repo's `workloadIdentityUser` member is removed. The xwalk-keyboards
  member is intact.
- All 9 objects in `calibration/current/` are deleted, including stale
  `camera_5062` and `camera_90014`. Soft delete can restore them until
  2026-11-03.

All 29,918 frames in `calibration/history/` are still present. The homepage
returned 200 afterwards.

1. Delete the 7 scheduler jobs:
   `gcloud scheduler jobs delete calibrate-camera-<id> --location us-central1`
2. Delete the service:
   `gcloud run services delete xwalk-camera-calibration-agent --region us-central1`
   This also removes the plaintext key and `github-deploy@`'s invoker binding
   on it.
3. Delete this service's images only:
   `gcloud artifacts docker images delete us-central1-docker.pkg.dev/xwalk-keyboards-01/cloud-run-source-deploy/xwalk-camera-calibration-agent --delete-tags`
4. Remove this repo's `roles/iam.workloadIdentityUser` binding on
   `github-deploy@` (the `principalSet://…/attribute.repository/vinceallenvince/xwalk-camera-calibration-agent`
   member). Leave the SA and the pool alone.
5. Delete `gs://xwalk-keyboards-01/calibration/current/`, so a future reader
   can't mistake frozen calibration for live.

### 5. Roboflow

**Done 2026-10-27.**

- The web service dropped every `ROBOFLOW_*` and `CROSSWALK_AGENT_*` env var.
  Revision `xwalk-keyboards-00102-md8` serves 100% of traffic with only
  `CARLA_HLS_BASE_URL` set.
- Both `roboflow-api-key` and `crosswalk-agent-api-key` were then deleted.
  The project has no secrets left.
- The homepage returned 200 afterwards.
- The Roboflow key was handled in the Roboflow dashboard, which isn't
  verifiable from GCP.

- Delete or disable the stripe-segmentation workflow
  (`CALIBRATION_STRIPE_WORKFLOW_URL`).
- **Revoke the key that was in the agent's env var.** It was exposed in
  plaintext. If it is the same key as the `roboflow-api-key` secret, the web
  app's realtime detection uses it too. In that case, revoke it when realtime
  detection is turned off, or rotate it and update the secret first.
- If realtime detection is also retired, consider downgrading or cancelling
  the Roboflow plan.

### 6. Data

**Done 2026-10-27: kept as recommended.** BigQuery `calibration.runs`
(29,357 rows) and all 29,918 frames in `calibration/history/` remain in
Standard storage. The bucket has no lifecycle rules. A move to Coldline was
not made.

Recommended default: keep it.

- `calibration/history/` (~5.7 GB): keep. Optionally add a lifecycle rule
  moving it to Coldline. It's likely source material for the homepage video
  and the project record.
- BigQuery `calibration.runs`: keep. Storage cost is negligible.
- Looker Studio: leave it as a frozen record, or add a "retired 2026-10" note.

### 7. Archive the repo

**Not planned.** The repo stays open (not archived) as of 2026-10-27.

After step 4, archive `vinceallenvince/xwalk-camera-calibration-agent` on
GitHub and close out the Linear work.

## Open questions (resolved)

1. Homepage camera status: resolved by xwalk-keyboards #66, which removed the
   status route entirely.
2. Roboflow key: moot. Both the secret and the web references were removed
   before the key was handled.
3. Colder storage for `calibration/history/`: not now. It stays in Standard
   storage.
