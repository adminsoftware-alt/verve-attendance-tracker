# presence_intervals Migration — 27 July 2026

## What was done

All automated writers to `presence_intervals` have been **stopped**. The table is now frozen — only manual SQL updates will change it. Raw data ingestion (`participant_events_p`, `room_mappings`) continues unchanged.

### Paused scheduler jobs (asia-east1)

| Job | Schedule (IST) | Body | Purpose |
|-----|----------------|------|---------|
| `intervals-rebuild-today-2min` | `*/2 8-23 * * *` | `{}` | Rebuild today every 2 min |
| `intervals-auto-build-sweep` | `*/15 * * * *` | `{"days_back": 35}` | Self-heal last 35 days |
| `intervals-rebuild-yesterday-0600` | `0 6 * * *` | `{"days_ago":1}` | Rebuild yesterday at 06:00 |
| `intervals-rebuild-yesterday-1230` | `30 12 * * *` | `{"days_ago":1}` | Rebuild yesterday at 12:30 |

All target: `POST https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/intervals/rebuild` (or `/intervals/auto-build`)  
All have `attemptDeadline: 180s`, timezone `Asia/Kolkata` (or `Asia/Calcutta` for the 2-min job).

### Cloud Run env change

```
PAGELOAD_AUTO_BUILD=false   (was: unset, defaulting to true)
```
Revision: `breakout-room-calibrator-00067-zpt`

This prevents dashboard page loads from triggering rebuilds that would overwrite manual changes.

### Left running (not touched)

- `mapping-health-noon` — alert only, doesn't write intervals
- `/webhook` ingestion → `participant_events_p`
- Room Mapper → `room_mappings`

### Baseline at freeze time

```
+------------+---------------------+------+
| event_date |     last_built      |  n   |
+------------+---------------------+------+
| 2026-07-25 | 2026-07-26 17:15:07 |  194 |
| 2026-07-26 | 2026-07-27 10:45:07 |    6 |
| 2026-07-27 | 2026-07-27 12:10:08 | 1039 |
+------------+---------------------+------+
```
(last_built is UTC; add 5:30 for IST)

---

## Current state

- **Raw tables** (`participant_events_p`, `room_mappings`) — keep receiving live data from webhooks/Room Mapper.
- **`presence_intervals`** — frozen. You update it manually via BigQuery SQL. Dashboard reads it live (no cache), so changes appear on refresh.

---

## How to manually update presence_intervals

Run your own calculation in BigQuery console. Example structure:

```sql
BEGIN TRANSACTION;
  DELETE FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
  WHERE event_date = DATE '2026-07-27';

  INSERT INTO `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
    (interval_id, event_date, meeting_id, meeting_uuid, participant_key, participant_name,
     participant_email, room_name, room_category, start_ts, end_ts, duration_seconds,
     alone_seconds, snapshot_count, source, confidence, built_at)
  SELECT
    GENERATE_UUID() AS interval_id,
    ...  -- your logic here, reading participant_events_p + room_mappings
    CURRENT_TIMESTAMP() AS built_at
  FROM ...;
COMMIT TRANSACTION;
```

The transaction prevents partial writes or races.

---

## To restore automated builds (rollback)

### Resume scheduler jobs

```bash
gcloud scheduler jobs resume intervals-rebuild-today-2min      --location=asia-east1 --project=verve-attendance-tracker
gcloud scheduler jobs resume intervals-auto-build-sweep        --location=asia-east1 --project=verve-attendance-tracker
gcloud scheduler jobs resume intervals-rebuild-yesterday-0600  --location=asia-east1 --project=verve-attendance-tracker
gcloud scheduler jobs resume intervals-rebuild-yesterday-1230  --location=asia-east1 --project=verve-attendance-tracker
```

### Re-enable page-load auto-build

```bash
gcloud run services update breakout-room-calibrator --region=us-central1 \
  --update-env-vars PAGELOAD_AUTO_BUILD=true --project=verve-attendance-tracker
```

---

## To permanently delete the old jobs (after new schedule is working)

```bash
gcloud scheduler jobs delete intervals-rebuild-today-2min      --location=asia-east1 --project=verve-attendance-tracker
gcloud scheduler jobs delete intervals-auto-build-sweep        --location=asia-east1 --project=verve-attendance-tracker
gcloud scheduler jobs delete intervals-rebuild-yesterday-0600  --location=asia-east1 --project=verve-attendance-tracker
gcloud scheduler jobs delete intervals-rebuild-yesterday-1230  --location=asia-east1 --project=verve-attendance-tracker
```

---

## Next steps

1. Write your SQL logic reading `participant_events_p` + `room_mappings`.
2. Test it on one date, compare dashboard output vs expectations.
3. Once satisfied, set up a new Cloud Scheduler job (5-min interval) calling BigQuery directly.
