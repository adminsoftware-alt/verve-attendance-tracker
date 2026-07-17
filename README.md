# Zoom Breakout Room Tracker

A production system for tracking participant attendance in Zoom meetings and breakout
rooms. Deployed on Google Cloud Run with BigQuery storage. The Zoom SDK app polls every
30 seconds to capture who is in which room; Zoom webhooks provide a second independent
event stream; a background pipeline merges both into `presence_intervals` — the single
source of truth every report reads from.

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
│              Scout Bot VM (GCP, Windows, scout-bot-2)            │
│   Zoom Desktop client + SDK monitoring app                       │
│   Joins the meeting; the in-client Zoom App polls rooms /30s     │
└───────────────────────────┬─────────────────────────────────────┘
                            │ POST /monitor/snapshot (every 30s)
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                Cloud Run (verve-attendance-tracker)              │
│                                                                  │
│  attendance-frontend        breakout-room-calibrator             │
│  (React + Vite)             (Flask, app.py + zt_* modules)       │
│  - Login (12h HMAC token)   - /webhook   (Zoom-signed events)    │
│  - Team/Day/Month views     - /monitor/* (SDK snapshots)         │
│  - CSV / Excel exports      - /intervals/* (presence builder)    │
│                             - /teams/* /employees/* (reports)    │
│                             - /auth/*  (token issue/verify)      │
└──────────────┬──────────────────────────────────────────────────┘
               │                    ▲
               ▼                    │ every 2 min / 15 min (Cloud Scheduler)
┌──────────────────────────┐        │
│        BigQuery          │────────┘
│ room_snapshots_v2 (bot)  │
│ participant_events_p     │   Both partitioned by date. Merged by
│  (webhooks)              │   build_presence_intervals() into:
│ presence_intervals ──────┼─► single source of truth for ALL hours
└──────────────────────────┘
```

**Data flow:** bot snapshots are primary; webhook events fill gaps (bot off / crashed
→ hours are estimated and flagged ⚠ in the UI). People are identified by email when
known, display name otherwise.

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
├── breakout-calibrator/      # React Zoom App (SDK monitoring) — served at /app
├── attedance_manager/        # React dashboard frontend
├── scripts/create_teams.py   # One-off team import script
└── vm-setup/                 # Scout Bot VM setup scripts (Windows)
```

---

## BigQuery Tables

### Dataset: `verve-attendance-tracker.breakout_room_calibrator`

| Table | Purpose |
|-------|---------|
| `presence_intervals` | **SOURCE OF TRUTH** — merged per-person room intervals; every report reads this. Partitioned by `event_date`. |
| `room_snapshots_v2` | Bot SDK polling data (every 30s). Partitioned. |
| `participant_events_p` | Webhook join/leave events. Partitioned + clustered. Replaced unpartitioned `participant_events` on 2026-07-17 — always reference via `BQ_EVENTS_TABLE`. |
| `room_mappings` | Room UUID → name (calibration). `meeting_id` is INT64. |
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
| Bot / SDK app | `/monitor/snapshot`, `/calibration/*` | open (no login context exists) |
| Builders (scheduler-called) | `/intervals/rebuild`, `/intervals/auto-build` | open |
| Reports | `/teams/<id>/report/monthly`, `/employees/*`, CSVs | reads `presence_intervals` |

---

## Background Jobs (Cloud Scheduler)

| Job | Region | Schedule (IST) | Purpose |
|-----|--------|----------------|---------|
| `intervals-rebuild-today-2min` | asia-east1 | every 2 min, 8:00–23:59 | keeps today fresh |
| `intervals-auto-build-sweep` | asia-east1 | every 15 min | self-heals last 35 days |
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

# Monitoring active?
curl "https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/monitor/health"
```

---

## Known Issues / Current State (2026-07-17)

- **Zoom REST API is broken**: the Cloud Run Zoom credentials are not from a
  Server-to-Server OAuth app → token requests fail (`unsupported_grant_type`).
  Consequences (accepted for now): no nightly Zoom reconcile cross-check, no
  camera/QoS data. Fix: create an S2S app in Zoom Marketplace (scopes
  `meeting:read:admin`, `report:read:admin`, `dashboard_meetings:read:admin`),
  update `ZOOM_CLIENT_ID/SECRET/ACCOUNT_ID` env vars, resume the
  `reconcile-zoom-nightly` job.
- **Session token in sessionStorage** (not httpOnly cookie) — XSS = takeover;
  token expires in 12h. Planned fix.
- **IST-midnight date attribution** on overnight meetings — needs design.
- Scout Bot VM auto-rejoin-on-boot does **not** work reliably in practice —
  VM/Zoom on-off is handled manually.

## Version History (recent)

| Date | Changes |
|------|---------|
| 2026-07-17 | Events table switched to partitioned `participant_events_p` (code-pointer swap, zero downtime); old table dropped; duplicate builder jobs paused; BigQuery now ~free-tier |
| 2026-07-16 | Major day (see `docs/CHANGES-2026-07-16.md`): webhook-fallback hours rebuild, single source of hours, auth on 45 endpoints, bcrypt passwords, email-based identity, ⚠ estimated flags, module split (`zt_*`), scheduler jobs, webhook signature hardening |
| 2026-05-11 | Break detection rework, name/email matching, login rate limit |
| 2026-04-06 | GCP migration, team management, trends, leave, tags (rev 128–130) |

---

## Cost (2026-07 estimate)

| Service | ~Monthly |
|---------|---------|
| Scout Bot VM (Windows license is most of it) | ~$115–125 |
| Cloud Run (2 services, 1 min-instance) | ~$10–20 |
| BigQuery | ~free tier after partitioning |
| **Total** | **~$125–145** |

---

## Critical Knowledge

1. **`presence_intervals` is the source of truth** — never compute hours from raw
   snapshots in new code; read the intervals table.
2. **Always use `BQ_EVENTS_TABLE`** (zt_config) — never hardcode the events table name.
3. **Bot data primary, webhooks fallback** — bot-off days are estimated + flagged ⚠.
4. **Identity = email when known**, display name otherwise; consumers match on both.
5. **IST dates** — all `event_date` fields are IST (UTC+5:30).
6. **The meeting never "ends" during the day** — one recurring instance runs ~24h and
   ends ~9 AM IST; anything calling Zoom's past-meeting APIs must account for this.
7. **New top-level .py file ⇒ Dockerfile COPY line** — or the service 503s on boot.
8. **Auth**: 12h HMAC token from `/auth/login`; frontend attaches it everywhere;
   webhook/bot/scheduler endpoints are deliberately open (no login context).
