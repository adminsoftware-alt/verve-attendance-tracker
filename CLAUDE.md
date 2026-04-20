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
- Backend API: `https://breakout-room-calibrator-1073587167150.us-central1.run.app`
- Frontend UI: `https://attendance-frontend-1073587167150.us-central1.run.app`
- Zoom App Home: `https://breakout-room-calibrator-1073587167150.us-central1.run.app/app`

**GCP Project:** `verve-attendance-tracker` (Project #: 1073587167150)
**BigQuery Dataset:** `breakout_room_calibrator`
**GitHub Repo:** `adminsoftware-alt/verve-attendance-tracker`
**Current Revision:** 130

**Auto-Deploy:** Push to `main` triggers Cloud Build

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Scout Bot VM (GCP)                            │
│   IP: 34.47.178.82 | User: dataapps | Pass: ScoutBot2026        │
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
cd C:\Users\shash\Downloads\zoom+tracker
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
| `room_snapshots` | snapshot_id, snapshot_time, event_date, meeting_id, room_name, participant_name, participant_email, participant_uuid, inserted_at | **PRIMARY** - SDK polling data (every 30s) |
| `participant_events` | event_id, event_type, event_timestamp, event_date, meeting_id, meeting_uuid, participant_id, participant_name, participant_email, room_uuid, room_name, inserted_at | Webhook join/leave events |
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
- URL validation events don't have these headers (skip validation)

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

| Job | Schedule | Endpoint | Purpose |
|-----|----------|----------|---------|
| `daily-qos-collection` | 9:30 AM IST | `/qos/scheduled` | Collect QoS/camera data for yesterday's meeting |
| `daily-attendance-report` | 11:15 AM IST | `/report/generate` | Generate and email daily attendance CSV |

## Environment Variables

**Required in Cloud Run:**
```
ZOOM_CLIENT_ID        # Server-to-Server OAuth app
ZOOM_CLIENT_SECRET    # Server-to-Server OAuth app
ZOOM_WEBHOOK_SECRET   # Webhook validation
ZOOM_ACCOUNT_ID       # Account ID for API calls
GCP_PROJECT_ID        # variant-finance-data-project
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

## Bot Detection Logic (shared in zoomService.js)

```javascript
function isBotNameMatch(participantName, botName = 'Scout Bot') {
  const pName = participantName.toLowerCase();
  const normalizedBotName = botName.toLowerCase();

  // Exact match
  const isExactMatch = pName === normalizedBotName;
  // Contains the configured bot name
  const containsBotName = pName.includes(normalizedBotName);
  // Scout bot patterns
  const isScoutBot = pName.includes('scout bot') || pName.includes('scoutbot');
  const isScoutPattern = pName.startsWith('scout') && pName.includes('bot');

  return isExactMatch || containsBotName || isScoutBot || isScoutPattern;
}
```

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
curl -X POST "https://breakout-room-calibrator-1041741270489.us-central1.run.app/report/generate" \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-02-19"}'
```

### Check Camera Status
```bash
curl -X POST "https://breakout-room-calibrator-1041741270489.us-central1.run.app/test/camera-qos" \
  -H "Content-Type: application/json" \
  -d '{"meeting_id": "123456789", "search": "John"}'
```

### Force Reload Mappings
```bash
curl -X POST "https://breakout-room-calibrator-1041741270489.us-central1.run.app/calibration/reload"
```

### Check Health
```bash
curl "https://breakout-room-calibrator-1041741270489.us-central1.run.app/health"
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

- **Revision 78** (2026-03-27): Enhanced calibration UI with delay selector, live room view, recalibration, reset
- **Revision 77** (2026-03-05): Security & performance fixes
- **Revision 76**: Source='webhook_calibration' fix
- **Revision 75**: SDK verification before BQ save
- Earlier: Camera tracking, QoS pagination, calibration timing fixes

## graphify

This project has a graphify knowledge graph at graphify-out/.

Rules:
- Before answering architecture or codebase questions, read graphify-out/GRAPH_REPORT.md for god nodes and community structure
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- After modifying code files in this session, run `graphify update .` to keep the graph current (AST-only, no API cost)
