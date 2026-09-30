# Pub/Sub webhook buffer: rollout guide

## Why

Before: `/webhook` wrote to BigQuery while Zoom waited. If the write failed we
logged it and still answered 200, so Zoom never resent, and **the event was lost**.

After: `/webhook` verifies the signature, publishes to Pub/Sub and answers 200.
Pub/Sub pushes each message to `/pubsub/zoom-events` on the **same** service.
If the BigQuery write fails, that endpoint answers 500 and Pub/Sub retries
(10 s to 10 min backoff, up to 10 attempts, then the dead-letter topic).

```
Zoom -> /webhook (verify) -> Pub/Sub topic -> push -> /pubsub/zoom-events -> handlers -> BigQuery
                                                       500 = retry;  10 failures = dead-letter
```

## Design decisions

| Decision | Why |
|---|---|
| Push back to the same service, not a separate worker | Handlers keep in-memory state (participant positions, mapping requests, dedup) that `/mapping/sync` needs, and that state assumes one process |
| Ordering key = one person (email, else normalised name) | A person's leave can't overtake their join. Different people still run in parallel |
| Only BigQuery write failures are retried | With ordering, a message that can never succeed would block that person's later events. Handler bugs and malformed messages are logged and acked, which is today's behaviour |
| Dedup marks are rolled back on a failed write | Otherwise the 60 s in-memory dedup would drop the retry as a "duplicate" (verified by a test) |
| Processed `messageId`s remembered for 1 hour | Pub/Sub is at-least-once. A redelivered message isn't stored twice |
| Publish failure falls back to inline processing | A Pub/Sub outage never drops an event |
| Publishing only when push auth is configured | Prevents every push being rejected and piling up in the dead-letter topic |
| Camera events not retried | The camera feature is inactive, and its failures would block people's join/leave events |

## Files

| File | Change |
|---|---|
| `zt_pubsub.py` | New: publisher, push auth (OIDC), envelope decode, processed-id cache, strict-store mode, counters |
| `app.py` | `/webhook` publishes when active; routing moved to `_dispatch_webhook_event()`; new `/pubsub/zoom-events` and `/pubsub/status`; `insert_participant_event` raises in strict mode; dedup rollback on `MeetingState` |
| `requirements.txt` | `google-cloud-pubsub==2.18.4` |
| `Dockerfile` | `COPY zt_pubsub.py .` |
| `scripts/setup_pubsub.sh` | One-time GCP setup |
| `tests/test_pubsub.py` | 21 tests |

## Rollout steps

**1. Run the tests locally**
```bash
uv run --no-project --python 3.11 --with-requirements requirements.txt --with pytest python -m pytest tests/ -q
```

**2. Deploy the code with the feature OFF.** Push to `main` as usual. Nothing
changes because `WEBHOOK_PUBSUB_ENABLED` is not set. Check:
```bash
curl https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/pubsub/status
# expect "enabled": false, "active": false
```

**2b. Cap the service at ONE instance.** Nothing sets `--max-instances` today
(not `cloudbuild.yaml` and not the docs). In-memory tracking assumes one process,
and a burst of Pub/Sub pushes (for example a backlog after an outage) could make
Cloud Run start a second instance and split that memory. Check and set:
```bash
gcloud run services describe breakout-room-calibrator --region=us-central1 --project=verve-attendance-tracker \
  --format='value(spec.template.metadata.annotations."autoscaling.knative.dev/maxScale")'
gcloud run services update breakout-room-calibrator --region=us-central1 --project=verve-attendance-tracker \
  --max-instances=1
```
With Pub/Sub on this is safe: if the one instance is busy, pushes are simply
retried a little later.

**3. Create the GCP resources** (topic, dead-letter topic, push subscription, service account, permissions):
```bash
bash scripts/setup_pubsub.sh
```

**4. Switch on** (outside working hours ideally):
```bash
gcloud run services update breakout-room-calibrator --region=us-central1 --project=verve-attendance-tracker \
  --update-env-vars=WEBHOOK_PUBSUB_ENABLED=true,PUBSUB_PUSH_AUDIENCE=https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/pubsub/zoom-events,PUBSUB_PUSH_SA_EMAIL=pubsub-push-invoker@verve-attendance-tracker.iam.gserviceaccount.com
```
Env vars survive later `gcloud run deploy --source` runs from Cloud Build.

**5. Verify** within 10 to 15 minutes of real traffic:
- `/pubsub/status`: `active: true`, `published` and `push_processed` rising together, `push_rejected_auth` = 0
- Logs show `-> Pub/Sub (<id>)` then `WEBHOOK EVENT:` lines
- New rows keep arriving in `participant_events_p`
- The dead-letter review subscription is empty:
  `gcloud pubsub subscriptions pull zoom-webhook-events-dlq-review --limit=5 --project=verve-attendance-tracker`
- Compare today's `presence_intervals` hours with a normal day for sanity

## Rollback
```bash
gcloud run services update breakout-room-calibrator --region=us-central1 --project=verve-attendance-tracker \
  --update-env-vars=WEBHOOK_PUBSUB_ENABLED=false
```
`/webhook` immediately goes back to inline processing. Messages already in the
queue keep being delivered to `/pubsub/zoom-events` and processed normally.

## Things to know
- `/pubsub/status` counters are per instance and reset on restart.
- Dead-lettered messages are kept for 7 days. To replay one, publish its data
  back to `zoom-webhook-events` (or fix the cause and use `gcloud pubsub
  subscriptions seek` on the main subscription).
- Processing is now a few hundred ms behind the webhook. Reports rebuild every
  few minutes, so this is not visible.
- Retries last about 50 minutes (10 attempts, backoff up to 10 min). A
  BigQuery outage longer than that sends messages to the dead-letter topic, and
  they must be replayed by hand. While a person's message is retrying, their
  later events wait behind it (per-person ordering), so their live view lags.
- The publish wait is capped at 2 s because Zoom expects a reply within 3 s. If
  a timed-out publish still goes through later, the event is processed twice
  (inline and via push). The 60 s dedup usually catches it; Point 3 closes it fully.
- Push tokens are verified once and then cached until they expire, so each
  push doesn't download Google's signing certs again.
- Point 3 (deterministic `event_id`) is now implemented (2026-09-29): a
  redelivery after a restart stores a row with the SAME id, which BigQuery's
  streaming dedup (`row_ids`) and the v15 builder SQL collapse. See
  `docs/IMPLEMENTATION-2026-09-29.md`.
- Cloud Build does not run the tests yet (Point 12).
