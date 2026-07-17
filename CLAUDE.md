# CLAUDE.md

This file provides complete guidance to Claude Code when working with this repository. **Read this instead of exploring code.**

## Project Overview

**Zoom Breakout Room Tracker** - A production system deployed on Google Cloud Run that:
- Tracks participant activity in Zoom breakout rooms via **SDK Monitoring** (polls every 30s)
- Captures camera ON/OFF status via Dashboard QoS API
- **No calibration needed** - SDK provides room names directly
- Generates daily attendance CSV reports with IST timestamps
- Scout Bot VM auto-joins meetings; HR clicks app once to start monitoring

**Cloud Run URLs:**
- Backend API: `https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app`
- Frontend UI: `https://attendance-frontend-4e5na4tdha-uc.a.run.app`
- Zoom App Home: `https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/app`

**GCP Project:** `verve-attendance-tracker` (Project #: 1073587167150)
**BigQuery Dataset:** `breakout_room_calibrator`
**GitHub Repo:** `adminsoftware-alt/verve-attendance-tracker`
**Deploys:** auto from `main` via Cloud Build (revision number changes every push)

**Auto-Deploy:** Push to `main` triggers Cloud Build

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Scout Bot VM (GCP)                            │
│   VM: scout-bot-2 (us-central1-a) — creds in password manager   │
│   Auto-joins meeting → HR clicks app → Monitoring starts         │
└───────────────────────────┬─────────────────────────────────────┘
                            │ SDK polls every 30s
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                         Cloud Run                                │
│  ┌─────────────────┐    ┌──────────────────────────────────┐   │
│  │  React App      │    │  Flask Server (app.py)           │   │
│  │  (MonitorPanel) │────│  - /monitor/* (SDK polling)      │   │
│  │  /app endpoint  │    │  - /webhook (Zoom events)        │   │
│  └─────────────────┘    │  - /report/* (CSV generation)    │   │
│                         │  - /qos/* (Camera tracking)       │   │
│                         └──────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
            ┌──────────────┐ ┌──────────┐  ┌──────────────┐
            │   BigQuery   │ │ Zoom API │  │  SendGrid    │
            │   5 tables   │ │  (QoS)   │  │  (Reports)   │
            └──────────────┘ └──────────┘  └──────────────┘
```

## File Structure

| File | Purpose |
|------|---------|
| `app.py` | Main Flask server - webhooks, monitoring, QoS, reports |
| `report_generator.py` | Daily CSV report from snapshots + webhooks |
| `breakout-calibrator/` | React app using Zoom Apps SDK |
| `breakout-calibrator/src/components/MonitorPanel.jsx` | **SDK polling UI (primary)** |
| `breakout-calibrator/src/components/CalibrationPanel.jsx` | Legacy calibration UI |
| `breakout-calibrator/src/hooks/useZoomSdk.js` | Zoom SDK methods |
| `breakout-calibrator/src/services/apiService.js` | Backend API communication |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Cloud Run deployment config |

## Build and Deploy Commands

```bash
# AUTO-DEPLOY: Push to main triggers Cloud Build (preferred method)
git add . && git commit -m "message" && git push origin main

# Manual deployment - Backend (build React + deploy to Cloud Run)
cd C:\Users\shash\Downloads\zoom_tracker_project\verve-attendance-tracker
cd breakout-calibrator && npm run build && cd ..
gcloud.cmd run deploy breakout-room-calibrator --source . --region us-central1 --allow-unauthenticated --min-instances=1 --project=verve-attendance-tracker

# Manual deployment - Frontend
cd attedance_manager
gcloud.cmd run deploy attendance-frontend --source . --region us-central1 --allow-unauthenticated --port 8080 --project=verve-attendance-tracker

# React app only (local dev)
cd breakout-calibrator && npm start

# View Cloud Run logs
gcloud.cmd run services logs read breakout-room-calibrator --region us-central1 --limit 100 --project=verve-attendance-tracker

# Tail logs in real-time
gcloud.cmd run services logs tail breakout-room-calibrator --region us-central1 --project=verve-attendance-tracker
```

## BigQuery Tables

| Table | Schema | Purpose |
|-------|--------|---------|
| `room_snapshots_v2` | snapshot_id, snapshot_time, event_date, meeting_id, room_name, participant_name, participant_email, participant_uuid, inserted_at | **PRIMARY** - SDK polling data (every 30s); date-partitioned |
| `presence_intervals` | participant_key, participant_name, participant_email, room_name, room_category, start_ts, end_ts, duration_seconds, confidence, event_date | **SOURCE OF TRUTH for hours** — built by `zt_intervals.build_presence_intervals`; every report reads this; date-partitioned |
| `participant_events_p` | event_id, event_type, event_timestamp, event_date, meeting_id, meeting_uuid, participant_id, participant_name, participant_email, room_uuid, room_name, inserted_at | Webhook join/leave events (partitioned by event_date, clustered; replaced unpartitioned `participant_events` on 2026-07-17 — always reference via `BQ_EVENTS_TABLE`) |
| `room_mappings` | mapping_id, meeting_id, meeting_uuid, room_uuid, room_name, room_index, mapping_date, mapped_at, source | UUID -> room name (legacy calibration) |
| `camera_events` | event_id, event_type, event_timestamp, event_date, event_time, meeting_id, meeting_uuid, participant_id, participant_name, participant_email, camera_on, room_name, duration_seconds, inserted_at | Camera ON/OFF events |
| `qos_data` | qos_id, meeting_uuid, participant_id, participant_name, participant_email, join_time, leave_time, duration_minutes, attentiveness_score, camera_on_count, camera_on_minutes, camera_on_intervals, recorded_at, event_date | Quality of Service metrics from Dashboard API |

## API Endpoints Reference

### Monitor Mode (SDK Polling) - PRIMARY
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/monitor/snapshot` | POST | Receive SDK polling data → saves to room_snapshots |
| `/monitor/status` | GET | Check snapshot counts for today |
| `/monitor/health` | GET | Check if monitoring active (HEALTHY/STALE/NO_DATA) |
| `/monitor/sample` | GET | View sample snapshot data (last 50 rows) |

### Webhook
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/webhook` | POST | Receives Zoom webhook events (participant_joined, participant_left, breakout_room_joined, breakout_room_left, meeting.ended) |

### Calibration (Legacy)
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/calibration/start` | POST | Start calibration session for meeting |
| `/calibration/mapping` | POST | Receive room mappings from Zoom SDK (BEFORE moving bot) |
| `/calibration/verify` | POST | Frontend confirms SDK verified bot location - triggers BigQuery save |
| `/calibration/complete` | POST | Mark calibration as complete |
| `/calibration/pending` | GET | Check pending room moves and match status |
| `/calibration/status` | GET | Get current calibration status |
| `/calibration/reload` | POST | Force reload mappings from BigQuery |
| `/calibration/reset` | POST | Full reset of calibration state, optionally clear BigQuery |
| `/calibration/live-rooms` | GET | Get current room participant data for manual verification |
| `/calibration/recalibrate-room` | POST | Prepare a specific room for re-calibration |
| `/calibration/single-room-complete` | POST | Complete a single room re-calibration |
| `/calibration/mapping-summary` | GET | Compare FIXED_ROOM_SEQUENCE with actual mappings |

### Reports
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/report/generate` | POST | Generate and email daily CSV (defaults to yesterday) |
| `/report/preview/<date>` | GET | Preview report data for a date |

### QoS / Camera
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/qos/collect` | POST | Manual QoS collection for a meeting |
| `/qos/scheduled` | POST | Scheduled collection (Cloud Scheduler calls this) |
| `/qos/status` | GET | Check QoS data status for recent dates |
| `/qos/delete` | POST | Delete QoS data for a date (for recollection) |
| `/qos/update-camera` | POST | Update camera data from Dashboard API |
| `/test/camera-qos` | POST | Search participant camera status |

### Debug
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Health check |
| `/mappings` | GET | Get current room mappings |
| `/debug/bq-mappings` | GET | Check BigQuery mappings directly |

## Critical Design Decisions

### 1. Camera Tracking via Dashboard QoS API
**IMPORTANT:** Zoom does NOT provide `meeting.participant_video_started/stopped` webhooks. Camera status must be obtained from Dashboard QoS API:
- Endpoint: `/metrics/meetings/{id}/participants/qos`
- Camera ON: `video_output` has bitrate/resolution
- Camera OFF: `video_output` is empty/null
- **Requires:** Business+ plan and `dashboard_meetings:read:admin` scope
- **Max page_size:** 10 (Zoom limit)
- **Pagination:** Code fetches up to 200 pages (2000 participants max)

### 2. Calibration Flow (Scout Bot)
The core problem: Zoom SDK uses GUIDs (`{E7F123FC-EE33-47D8-BC5E-C84FCD31E06F}`) but webhooks use base64-like UUIDs (`6kAkE8jOgeGj5m2DPy9/`). Solution:

1. Scout Bot joins meeting and is made co-host
2. React app opens calibration panel
3. For each breakout room:
   - React app sends mapping to `/calibration/mapping` **BEFORE** moving bot
   - React app moves Scout Bot to room via SDK
   - Zoom sends webhook with webhook UUID
   - Flask matches webhook UUID to pending room name
   - React app verifies bot location via SDK
   - React app calls `/calibration/verify` to trigger BigQuery save
4. Mappings stored with `source='webhook_calibration'` (preferred) or `source='zoom_sdk_app'`

### 3. Two Calibration Modes
- **Scout Bot mode:** Host moves a dedicated "Scout Bot" participant through rooms
- **Self mode:** User moves themselves through rooms (for non-hosts)

### 4. Webhook Signature Validation
- Zoom sends `x-zm-signature` and `x-zm-request-timestamp` headers
- Signature: `v0=HMAC-SHA256(secret, "v0:{timestamp}:{body}")`
- Must check timestamp freshness (within 5 minutes)
- **Every request must be signed — including `endpoint.url_validation`** (hardened
  2026-07-16; missing headers → 401; Zoom signs all events so nothing legitimate breaks)

### 5. IST Timezone Handling
- All event_date fields stored in IST (UTC+5:30)
- Ensures events between 00:00-05:30 UTC aren't assigned to wrong day
- Reports use IST timestamps

### 6. Server Restart Handling
- On Cloud Run restart, mappings load from BigQuery on first request
- Mappings persist across container restarts
- Only deleted when switching to a DIFFERENT meeting

### 7. Deduplication
- Event dedup cache with 60-second TTL
- Prevents duplicate webhook processing (Zoom sometimes sends twice)
- Cache cleanup runs every 60 seconds (not per-event)

### 8. Pending Moves Pruning
- Verified entries auto-removed after 5 minutes
- Prevents memory leaks from stale calibration data

## Cloud Scheduler Jobs

| Job | Region | Schedule (IST) | Endpoint | Purpose |
|-----|--------|----------------|----------|---------|
| `intervals-rebuild-today-2min` | asia-east1 | every 2 min, 8:00–23:59 | `/intervals/rebuild` | Keep today fresh |
| `intervals-auto-build-sweep` | asia-east1 | every 15 min | `/intervals/auto-build` | Self-heal last 35 days |
| `reconcile-zoom-nightly` | asia-east1 | 10:00 — **PAUSED** (Zoom S2S creds broken) | `/reconcile/zoom` | Cross-check vs Zoom records |
| `hourly-sheets-update` | us-central1 | hourly 9–23 | `/sheets/update` | Google Sheets export |
| `hourly-presence-intervals-today`, `daily-presence-intervals`, `email-monitor-alert` | us-central1 | **PAUSED** | — | Superseded / disabled |

## Environment Variables

**Required in Cloud Run:**
```
ZOOM_CLIENT_ID        # Server-to-Server OAuth app
ZOOM_CLIENT_SECRET    # Server-to-Server OAuth app
ZOOM_WEBHOOK_SECRET   # Webhook validation
ZOOM_ACCOUNT_ID       # Account ID for API calls
GCP_PROJECT_ID        # verve-attendance-tracker
SENDGRID_API_KEY      # For email reports
REPORT_EMAIL_TO       # Recipients (comma or semicolon separated)
```

**Optional:**
```
SCOUT_BOT_NAME        # Default: "Scout Bot"
SCOUT_BOT_EMAIL       # Email for bot matching
BQ_DATASET            # Default: "breakout_room_calibrator"
REPORT_EMAIL_FROM     # Sender email for reports
```

## Zoom API Methods Used

```python
# OAuth token (Server-to-Server)
POST https://zoom.us/oauth/token?grant_type=account_credentials&account_id={ACCOUNT_ID}

# Past meeting participants (Report API)
GET https://api.zoom.us/v2/past_meetings/{meeting_uuid}/participants
GET https://api.zoom.us/v2/report/meetings/{meeting_uuid}/participants

# Dashboard QoS (for camera status)
GET https://api.zoom.us/v2/metrics/meetings/{meeting_id}/participants/qos
```

## Report CSV Format

```
Name, Email, Main_Joined_IST, Main_Left_IST, Total_Duration, Room_History
"John Doe", "john@example.com", "09:15", "11:30", "2h 15m", "Room A [09:20-10:00] -> Room B [10:05-11:25]"
```

- One row per participant
- Times in IST (HH:MM format)
- Duration as "Xh Ym"
- Room history shows join/leave times per room

## Bot Detection Logic (exact-match since 2026-07-16)

Backend `is_scout_bot()` and calibrator `isBotNameMatch()` both use **exact name
match** after stripping Zoom rejoin suffixes (e.g. `"Scout Bot-1"` → `"Scout Bot"`),
case-insensitive. Substring matching was removed — a real person whose display name
merely contained "Scout Bot" used to vanish from tracking.

## In-Memory State (MeetingState class)

```python
meeting_state = MeetingState()  # Global instance

# Key attributes:
meeting_state.meeting_id          # Current meeting ID
meeting_state.meeting_uuid        # Current meeting UUID
meeting_state.uuid_to_name        # Dict: room_uuid -> room_name
meeting_state.name_to_uuid        # Dict: room_name -> room_uuid
meeting_state.pending_room_moves  # List of {room_name, sdk_uuid, matched, webhook_uuid, verified}
meeting_state.calibration_in_progress  # Boolean
meeting_state.calibration_mode    # 'scout_bot' or 'self'
meeting_state.event_dedup_cache   # Dict: event_hash -> timestamp
```

## Common Operations

### Manual Report Generation
```bash
curl -X POST "https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/report/generate" \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-02-19"}'
```

### Check Camera Status
```bash
curl -X POST "https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/test/camera-qos" \
  -H "Content-Type: application/json" \
  -d '{"meeting_id": "123456789", "search": "John"}'
```

### Force Reload Mappings
```bash
curl -X POST "https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/calibration/reload"
```

### Check Health
```bash
curl "https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/health"
```

## Troubleshooting

### Room names show as "Room-XXXXX" in reports
- Calibration webhooks didn't match SDK mappings
- Check if `source='webhook_calibration'` exists in `room_mappings` table
- May need to re-run calibration during a live meeting

### Camera data missing
- Dashboard QoS API requires Business+ plan
- Only available for ~30 days after meeting
- Requires `dashboard_meetings:read:admin` scope

### Webhook signature errors
- Check `ZOOM_WEBHOOK_SECRET` matches Zoom app configuration
- May indicate duplicate webhook subscriptions (check Zoom Marketplace)

### Duplicate events
- Normal - Zoom sometimes sends webhooks twice
- Dedup cache handles this automatically

### Mappings not loading after restart
- Check BigQuery connectivity
- Verify `mapping_date` matches today/yesterday
- Use `/calibration/reload` to force reload

## React App Structure (breakout-calibrator/)

```
src/
├── components/
│   ├── CalibrationPanel.jsx    # Main calibration UI
│   ├── StatusMessage.jsx       # Status display
│   ├── ProgressIndicator.jsx   # Progress bar
│   └── RoomList.jsx           # Room display
├── services/
│   ├── zoomService.js         # SDK calibration logic
│   └── apiService.js          # Backend API calls
├── hooks/
│   └── useZoomSdk.js          # Zoom SDK hook
└── App.jsx                    # Root component
```

## Security Features




1. **Webhook signature validation** - HMAC-SHA256 with timestamp freshness
2. **CORS restricted** - Only Zoom domains allowed
3. **No default credentials** - All env vars must be explicitly set
4. **OWASP headers** - Strict-Transport-Security, X-Content-Type-Options, CSP
5. **API timeouts** - All requests have 30s timeout

## Version History

- **2026-07-17**: Events table switched to partitioned `participant_events_p` via code-pointer swap (zero downtime; old table dropped; always use `BQ_EVENTS_TABLE`). Temp `/admin/ops/partition-events` endpoint deleted. Duplicate us-central1 builder jobs paused. `reconcile-zoom-nightly` moved to 10:00 IST then paused (S2S creds broken). `load_mappings_from_bigquery` INT64/STRING mismatch fixed. Docs refreshed; credentials removed from repo docs; dead Supabase files removed.
- **2026-07-16**: Major overhaul (see `docs/CHANGES-2026-07-16.md`): webhook-fallback rebuild for bot-off days, single source of hours (`presence_intervals`), auth on 45 endpoints (12h HMAC token), bcrypt passwords, email-based identity, ⚠ estimated flags, module split (`zt_config/zt_helpers/zt_zoom_api/zt_intervals`), scheduler jobs, webhook signature hardening, exact-match bot detection.
- **2026-05-11** (post-audit cleanup): Replaced gap-based break detection with "Break Time room presence only" across every team / employee endpoint. Capture Main Room participants in SDK snapshots. 30-second bucket dedup for break/isolation counts so multi-source polling (HR client + VM) doesn't double-count. Match team members by name OR email (Zoom display-name drift no longer drops users). Auto-start the SDK monitor without manual click. Day View Duration now includes Break Time. Server-side dedup on `/monitor/snapshot` insert. Per-IP `/auth/login` rate limit (5 tries per 60s, 5-min lockout). Wall-clock cap on Zoom API pagination.
- **Revision 78** (2026-03-27): Enhanced calibration UI with delay selector, live room view, recalibration, reset
- **Revision 77** (2026-03-05): Security & performance fixes
- **Revision 76**: Source='webhook_calibration' fix
- **Revision 75**: SDK verification before BQ save
- Earlier: Camera tracking, QoS pagination, calibration timing fixes

## Known Issues (audited 2026-05-11, **not yet fixed** — needs discussion)

These are tracked from a project-wide audit. Each touches surface area that affects production data or auth flow, so they were not changed without explicit approval.

- ~~Plaintext passwords in `app_users`~~ **FIXED 2026-07-16**: bcrypt at creation; legacy plaintext rows auto-upgrade on next successful login.
- **Zoom REST API broken (open, accepted 2026-07-17)**: Cloud Run Zoom creds are not a Server-to-Server OAuth app → token fails `unsupported_grant_type`. No reconcile cross-check, no QoS/camera data. `reconcile-zoom-nightly` PAUSED. Fix = create S2S app (scopes `meeting:read:admin`, `report:read:admin`, `dashboard_meetings:read:admin`), update env vars, resume job.
- ~~`DEFAULT_USERS` fallback in the JS bundle~~ **FIXED 2026-07-16**: removed; login is server-side only.
- ~~Webhook signature can be bypassed by omitting headers~~ **FIXED 2026-07-16**: missing `x-zm-signature`/`x-zm-request-timestamp` now 401s; only `endpoint.url_validation` is exempt.
- ~~`/chat` accepts client-supplied `role`~~ **FIXED 2026-07-16**: role/user come from the verified auth token when present.
- ~~All POST/PUT/DELETE endpoints unauthenticated~~ **FIXED 2026-07-16**: `require_auth` (HMAC-signed Bearer token from `/auth/login`) on 45 mutating routes; admin/superadmin roles enforced on user-management + `/admin/*` + test/debug endpoints. Deliberately left open: `/webhook` (Zoom-signed), `/monitor/snapshot` + `/calibration/*` (bot/SDK app, no login context), scheduler-called builders (`/intervals/*`, `/qos/scheduled`, `/report/generate`, `/reconcile/zoom`), and `/admin/ops/partition-events` (temporary — DELETE after the 2026-07-17 partition swap completes).
- **Session + auth token in `sessionStorage`** instead of httpOnly cookie. XSS = account takeover. (Token expires after 12h, limiting blast radius.)
- **IST midnight timezone drift** on multi-meeting day boundaries (deferred — touches date math used by every report query).
- ~~Scout Bot detection is substring match~~ **FIXED 2026-07-16**: exact match (after rejoin-suffix strip) in backend + calibrator.
- **`/qos/status` broken**: `qos_data` table has no named schema (`string_field_0..13`) → `Unrecognized name: event_date`. Dead feature (see Zoom API item); low priority.

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)
