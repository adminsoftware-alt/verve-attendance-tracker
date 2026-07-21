# Zoom Breakout Room Tracker

A production system for tracking participant attendance in Zoom meetings and breakout
rooms. Deployed on Google Cloud Run with BigQuery storage.

**Since 2026-07-21 the system is webhook-primary AND all-BigQuery:**

- **Zoom webhooks** (exact-to-the-second join/leave events) are the only attendance
  source — nothing needs to run on your side for capture; Zoom pushes directly to
  Cloud Run 24/7.
- **Hours are computed entirely inside BigQuery** — a JS-UDF state-machine script
  rebuilds `presence_intervals` (the single source of truth every report reads)
  every 2 minutes in ~10s, atomically (DELETE+INSERT inside a transaction).
  No interval math happens in Python for dates >= 2026-07-21; the Python builder
  remains only for rebuilding historical (snapshot-era) dates.
- **Room names come from a 5-minute daily human step**: any co-host opens the
  Room Mapper Zoom app (~lunch time, when occupancy peaks incl. the BREAK TIME
  room), waits for the sync log, closes it. Mapping is retroactive for the whole
  day (resolved by UUID at build time). The 24/7 Scout Bot VM is being retired.
- Mappings are **self-verifying**: every panel sync cross-checks SDK positions
  against webhook positions (only positions stable 2–30 min are trusted), fixes
  wrong labels automatically, and freezes any label two sources fight over
  (`DISPUTED` in logs) rather than flip-flopping.

---

## Quick Reference

### URLs
| Service | URL |
|---------|-----|
| **Frontend (UI)** | `https://attendance-frontend-4e5na4tdha-uc.a.run.app` |
| **Backend API** | `https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app` |
| **Zoom App Home** | `https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/app` |

### IDs
| Item | Value |
|------|-------|
| GCP Project ID | `verve-attendance-tracker` |
| GCP Project Number | `1073587167150` |
| BigQuery Dataset | `breakout_room_calibrator` |
| GitHub Repo | `adminsoftware-alt/verve-attendance-tracker` |
| Meeting ID | `9034027764` (recurring; one instance ≈ 24h, ends ~9 AM IST daily) |

### Credentials
No credentials are stored in this repo.
- **Dashboard logins**: `app_users` table in BigQuery — passwords are **bcrypt-hashed**;
  manage users via the dashboard (admin role) or `/auth/users` endpoints.
- **Scout Bot VM**: access via GCP Console → Compute Engine → `scout-bot-2` (RDP);
  credentials live in the team password manager.
- **Zoom app secrets**: Cloud Run environment variables (see CLAUDE.md).

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          ZOOM MEETING                            │
│  Participants only — no bot required.                            │
│  Once a day (~5 min): any CO-HOST opens the Room Mapper Zoom     │
│  app (RoomMapperPanel.jsx) so room UUIDs get human names.        │
│  RULE: only ONE Room Mapper panel open at a time.                │
└──────────┬───────────────────────────────┬──────────────────────┘
           │ webhooks 24/7 (PRIMARY:       │ SDK, 5 min/day:
           │ exact join/leave timestamps)  │ room UUID → room NAME
           ▼                               ▼
┌─────────────────────────────────────────────────────────────────┐
│         Cloud Run (verve-attendance-tracker, 1 worker)           │
│                                                                  │
│  attendance-frontend        breakout-room-calibrator             │
│  (React + Vite)             (Flask, app.py + zt_* modules)       │
│  - Login (12h HMAC token)   - /webhook    (Zoom-signed events)   │
│  - Live/Team/Day/Month      - /mapping/*  (name resolution +     │
│  - CSV / Excel exports      -    verify/correct + health alert)  │
│                             - /intervals/* (build triggers)      │
│                             - /teams/* /employees/* (reports)    │
└──────────────┬──────────────────────────────────────────────────┘
               │ triggers only — the MATH runs inside BigQuery
               ▼
┌─────────────────────────────────────────────────────────────────┐
│  BigQuery = the calculation engine (every 2 min, ~10s, atomic)  │
│                                                                  │
│  participant_events_p   raw webhook events (exact times)         │
│  room_mappings          webhook room UUID → name                 │
│         │  JS-UDF state-machine script (transaction)             │
│         ▼                                                        │
│  presence_intervals ──► SINGLE SOURCE OF TRUTH for ALL hours     │
│                         (read by Live/Team/Day views + reports)  │
│  room_snapshots_v2      historical only (pre-2026-07-21 days)    │
│  presence_intervals_sql legacy verification table                │
└─────────────────────────────────────────────────────────────────┘
```

**Data flow (webhook-primary + all-BQ since 2026-07-21):** every join/leave/breakout
event is stored with its exact timestamp — capture never depends on anything running.
When a webhook carries an unknown room UUID the event stores a `Room-xxxxxxxx`
placeholder; the daily Room Mapper session names every occupied room in one sync by
cross-matching SDK positions against webhook positions (positions trusted only when
stable 2–30 min; wrong labels auto-corrected; oscillating labels frozen as DISPUTED
for the day). Names are applied retroactively at build time via the `room_mappings`
JOIN — reports always show real room names, and a mapping fixed at 2 PM re-labels the
whole day. People are identified by email when known, display name otherwise —
**people joining under shared/team display names cannot be tracked as individuals.**

---

## Files Structure

```
verve-attendance-tracker/
├── app.py                    # Flask server — all routes (~13k lines)
├── zt_config.py              # Env settings, BigQuery table names, BQ client
├── zt_helpers.py             # IST time, name normalization, merge utils
├── zt_zoom_api.py            # Zoom REST client (currently unusable — see Known Issues)
├── zt_intervals.py           # presence_intervals pipeline (the core subsystem)
├── chatbot.py                # /chat natural-language assistant
├── report_generator.py       # Daily CSV email report
├── requirements.txt          # Python dependencies
├── Dockerfile                # Backend image — NOTE: new top-level .py files need a COPY line!
├── cloudbuild.yaml           # Auto-deploy on push to main
├── CLAUDE.md                 # Claude Code instructions (the deep reference)
├── docs/                     # Change logs (see CHANGES-2026-07-16.md)
├── breakout-calibrator/      # React Zoom App — served at /app; RoomMapperPanel (active,
│                             #   webhook-primary) + MonitorPanel (legacy, kept for rollback)
├── attedance_manager/        # React dashboard frontend
├── scripts/create_teams.py   # One-off team import script
└── vm-setup/                 # Scout Bot VM setup scripts (Windows)
```

---

## BigQuery Tables

### Dataset: `verve-attendance-tracker.breakout_room_calibrator`

| Table | Purpose |
|-------|---------|
| `presence_intervals` | **SOURCE OF TRUTH** — per-person room intervals; every report + the Live Dashboard read this. For dates >= 2026-07-21 it is built **entirely inside BigQuery** (JS-UDF script, every 2 min, atomic transaction). Partitioned by `event_date`. |
| `presence_intervals_sql` | Output of the same BQ builder when run in verification mode (`/intervals/build-sql`). Kept for engine comparisons; production now writes straight to `presence_intervals`. |
| `participant_events_p` | **PRIMARY** — webhook join/leave events with exact timestamps. Partitioned + clustered. Replaced unpartitioned `participant_events` on 2026-07-17 — always reference via `BQ_EVENTS_TABLE`. `event_timestamp` is TIMESTAMP; `meeting_id` is INT64. |
| `room_mappings` | Webhook room UUID → room name (`source='webhook_primary_sdk_lookup'`). Written by the Room Mapper panel flows (pending-request resolve, sync state-resolution, verify-and-correct, backfill). `meeting_id` is INT64 — queries must CAST (a STRING comparison here silently broke every insert until 2026-07-21). |
| `room_snapshots_v2` | Legacy bot SDK polling data (every 30s). Not written since 2026-07-20; still read for **historical dates only** (pre-cutover reports + Python builder). Keep it. |
| `teams`, `team_members`, `team_tags` | Team definitions and metadata |
| `team_holidays`, `employee_leave`, `team_leave_records` | Leave/holiday tracking |
| `attendance_overrides` | Manual attendance corrections |
| `app_users` | Dashboard logins (bcrypt-hashed passwords) |
| `attendance_reports` | Uploaded attendance data |
| `camera_events`, `qos_data`, `calibration_state` | Legacy / currently unused (Zoom API broken — see Known Issues) |

---

## API Overview

Full endpoint tables live in `CLAUDE.md`. The essentials:

| Area | Endpoints | Auth |
|------|-----------|------|
| Login | `POST /auth/login` → signed 12h token | open |
| Mutating endpoints (~45) | create/edit/delete everything | `@require_auth` Bearer token |
| Admin data browser / user mgmt | `/admin/*`, `/auth/users*` | admin / superadmin |
| Webhook | `POST /webhook` | Zoom HMAC signature (all events, incl. url_validation) |
| Room mapping | `/mapping/sync` (panel sends rooms+occupants; resolves, verifies, corrects, backfills — BQ writes run in a background thread), `/mapping/pending`, `/mapping/resolve`, `/mapping/request` | open (no login context exists) |
| Mapping health | `GET/POST /mapping/health` — noon scheduler alert if nobody ran the panel; `?alert=false` = check only | open |
| Mapping diagnostics | `GET /mapping/last-sync?name=X` — raw SDK payload from the last panel sync (ground-truth debugging) | open |
| Live dashboard | `/attendance/live`, `/attendance/heatmap` — read `presence_intervals` for dates >= 2026-07-21 (snapshots for older dates) | open |
| Builders (scheduler-called) | `/intervals/rebuild` (today goes through a once-per-3-min guard), `/intervals/auto-build` | open |
| BQ builder (verification mode) | `GET/POST /intervals/build-sql?date=…` — writes `presence_intervals_sql` + comparison | open |
| Reports | `/teams/<id>/report/monthly`, `/employees/*`, CSVs | reads `presence_intervals` |

---

## Background Jobs (Cloud Scheduler)

| Job | Region | Schedule (IST) | Purpose |
|-----|--------|----------------|---------|
| `intervals-rebuild-today-2min` | asia-east1 | every 2 min, 8:00–23:59 | keeps today fresh (BQ engine) |
| `intervals-auto-build-sweep` | asia-east1 | every 15 min | self-heals last 35 days |
| `mapping-health-noon` | asia-east1 | 12:00 daily | emails alert if the Room Mapper panel wasn't run today |
| `reconcile-zoom-nightly` | asia-east1 | 10:00 — **PAUSED** | needs working Zoom S2S creds |
| `hourly-sheets-update` | us-central1 | hourly 9–23 | Google Sheets export |
| `hourly-presence-intervals-today`, `daily-presence-intervals` | us-central1 | **PAUSED** | superseded by the 2-min + sweep jobs |

---

## Deployment

Push to `main` → Cloud Build builds both React apps and deploys both Cloud Run services.

```bash
git add . && git commit -m "Your changes" && git push origin main
```

**Rule:** any new top-level `.py` file needs a `COPY` line in the Dockerfile
(learned the hard way — 15-min outage on 2026-07-16).

```bash
# View logs
gcloud run services logs read breakout-room-calibrator --region us-central1 --limit 100 --project=verve-attendance-tracker
```

---

## Test Commands

```bash
# Health check
curl "https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/health"

# Were room mappings saved today? (what the noon alert checks)
curl "https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/mapping/health?alert=false"

# What did the Room Mapper panel last report? (ground-truth debugging)
curl "https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/mapping/last-sync?name=anjali"

# Is the BQ engine building? (built_at should be < 2 min old during meeting hours)
# -> run in BigQuery console:
#    SELECT MAX(built_at) FROM breakout_room_calibrator.presence_intervals
#    WHERE event_date = CURRENT_DATE('Asia/Kolkata')
```

---

## Operating Rules (learned the hard way, 2026-07-21)

1. **Single gunicorn worker only** (`--workers 1` in the Dockerfile). With 2
   workers each process saw half the webhooks; their in-memory participant
   positions disagreed and mapping corrections ping-ponged. Never raise the
   worker count unless tracking state moves to shared storage.
2. **Only ONE Room Mapper panel open at a time.** Two panels feed alternating
   partial claims and destabilize corrections.
3. **Daily 5-min panel run at lunch** (peak occupancy incl. BREAK TIME room —
   break detection depends on that room's name). After a mid-day meeting
   restart, run the panel once more (new rooms = new UUIDs).
4. **Individual tracking needs individual display names** — people joining as
   "TEAM 1" / shared accounts cannot get personal hours.
5. **Never delete `room_mappings` or `room_snapshots_v2`** — mappings fix
   forward (overwrite), snapshots serve all pre-cutover history.

### Rollback switches

| What | How |
|---|---|
| Hours engine back to Python | env `SQL_BUILDER_CUTOVER_DATE=9999-12-31` |
| Mapping auto-resolution off | `SYNC_STATE_RESOLUTION_ENABLED = False` (app.py) |
| Panel back to old bot polling | swap one import in `breakout-calibrator/src/App.js` (MonitorPanel intact) |

## Known Issues / Current State (2026-07-21)

- **Watch items**: labels frozen as `DISPUTED` in logs keep their current name
  for the day and settle on the next day's clean mappings. The noon health
  alert does not re-fire after a mid-day meeting restart (manual awareness).
- **Zoom REST API is broken**: the Cloud Run Zoom credentials are not from a
  Server-to-Server OAuth app → token requests fail (`unsupported_grant_type`).
  Consequences (accepted for now): no nightly Zoom reconcile cross-check, no
  camera/QoS data. Fix: create an S2S app in Zoom Marketplace (scopes
  `meeting:read:admin`, `report:read:admin`, `dashboard_meetings:read:admin`),
  update `ZOOM_CLIENT_ID/SECRET/ACCOUNT_ID` env vars, resume the
  `reconcile-zoom-nightly` job.
- **Session token in sessionStorage** (not httpOnly cookie) — XSS = takeover;
  token expires in 12h. Planned fix.
- **Scout Bot VM (`scout-bot-2`)**: being retired — keep STOPPED (disk only,
  ~$4–5/mo) for one clean week, then delete (~$115–120/mo saving). The bot's
  Zoom account must be named exactly "Scout Bot" to be excluded from tracking
  (it was found renamed "Scout S" and got counted as an employee).

## Version History (recent)

| Date | Changes |
|------|---------|
| 2026-07-21 | **All-BQ cutover + bot elimination + hardening day.** (1) `room_mappings` inserts were silently failing since day one (STRING param vs INT64 column in the dedup query) — fixed with CASTs; sync now also backfills any in-memory mapping to BQ. (2) Sync-state resolution: panel sync cross-matches SDK vs webhook positions → maps every occupied room in one 5-min session; enables ANY co-host to run the panel; 24/7 VM retired. (3) Verify-and-correct with 2-min stability guard + 30-min freshness cap + dispute freeze (anti ping-pong); restart guard (positions tagged with meeting instance uuid). (4) **Production `presence_intervals` built entirely in BigQuery** for dates >= 2026-07-21 (`SQL_BUILDER_CUTOVER_DATE`); duplicated-intervals race fixed with BEGIN/COMMIT TRANSACTION + guarded rebuild endpoint. (5) Live Dashboard + heatmap read `presence_intervals` (snapshots dead post-cutover). (6) `/mapping/health` + noon scheduler alert; `/mapping/last-sync` diagnostics. (7) Gunicorn 2 workers → 1 (split-brain fix). (8) Panel timeout fixed (sync BQ writes moved to background thread) |
| 2026-07-20 | **Webhook-primary architecture**: webhooks = attendance source (exact timestamps), SDK = room-name mapping only via new Room Mapper panel (bot idle, no polling, no bot movement); `/mapping/*` endpoints; placeholder room names resolved at build time via `room_mappings` JOIN; **pure-BigQuery interval builder** (`/intervals/build-sql`, JS-UDF state machine → `presence_intervals_sql`) verified against the Python builder on Jul 17–19 (all webhook-only participants matched; 2 Jul-17 diffs were stale pre-fix Python rows). Also: IST midnight-crossing fix (skip webhook-only events before 08:00 IST — phantom-hours bug) |
| 2026-07-17 | Events table switched to partitioned `participant_events_p` (code-pointer swap, zero downtime); old table dropped; duplicate builder jobs paused; BigQuery now ~free-tier |
| 2026-07-16 | Major day (see `docs/CHANGES-2026-07-16.md`): webhook-fallback hours rebuild, single source of hours, auth on 45 endpoints, bcrypt passwords, email-based identity, ⚠ estimated flags, module split (`zt_*`), scheduler jobs, webhook signature hardening |
| 2026-05-11 | Break detection rework, name/email matching, login rate limit |
| 2026-04-06 | GCP migration, team management, trends, leave, tags (rev 128–130) |

---

## Cost (2026-07 estimate)

| Service | ~Monthly |
|---------|---------|
| Scout Bot VM — **being retired** (stopped = disk only; delete after one clean week) | ~$4–5 → $0 |
| Cloud Run (2 services, 1 min-instance) | ~$10–20 |
| BigQuery (incl. the 2-min build engine) | ~free tier |
| **Total** | **~$15–25** (was ~$125–145 with the 24/7 VM) |

---

## Critical Knowledge

1. **`presence_intervals` is the source of truth** — never compute hours from raw
   events/snapshots in new code; read the intervals table. For dates >= 2026-07-21
   it is produced by the BigQuery JS-UDF engine (no Python interval math).
2. **Always use `BQ_EVENTS_TABLE`** (zt_config) — never hardcode the events table name.
3. **Webhooks primary, SDK = room names only** — SDK/webhook room UUIDs are different
   formats; names are mapped by participant position cross-match, never by UUID
   conversion. Snapshot data still wins for historical (pre-cutover) days.
4. **Identity = email when known**, display name otherwise; consumers match on both.
5. **IST dates** — all `event_date` fields are IST (UTC+5:30).
6. **The meeting never "ends" during the day** — one recurring instance runs ~24h and
   ends ~9 AM IST; a mid-day restart creates NEW room UUIDs (run the panel again).
7. **New top-level .py file ⇒ Dockerfile COPY line** — or the service 503s on boot.
8. **Auth**: 12h HMAC token from `/auth/login`; frontend attaches it everywhere;
   webhook/bot/scheduler endpoints are deliberately open (no login context).
9. **Single gunicorn worker, single Room Mapper panel** — both are hard rules; see
   Operating Rules. In-memory state (tracking, dedup, pending queues) assumes one
   process.
10. **`room_mappings.meeting_id` is INT64** — always CAST when comparing to STRING
    params; a mismatched comparison silently kills the whole insert path.
