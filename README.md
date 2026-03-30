# Zoom Breakout Room Tracker

A complete solution for tracking participant attendance in Zoom breakout rooms with proper room name resolution. Deployed on Google Cloud Run with BigQuery storage.

## Overview

This system captures participant activity in Zoom meetings and breakout rooms, storing data in BigQuery for reporting. It solves the challenge of mapping Zoom's internal room UUIDs to human-readable room names.

### Key Features
- Real-time participant join/leave tracking via Zoom Webhooks
- Breakout room visit tracking with proper room names
- Camera on/off event tracking
- QoS data collection (duration, attentiveness)
- Daily attendance reports with actual room names (not UUIDs)

---

## LATEST UPDATE (2026-03-26) - Fixed Room Sequence

### Problem Solved
Webhooks arriving out of order caused wrong room mappings (e.g., Manasvi More shown in 1.13 but actually in 1.16)

### Solution: Hardcoded FIXED_ROOM_SEQUENCE
- All 66 room names hardcoded in exact order
- Bot visits rooms in order (1.1 -> 1.2 -> 1.3...)
- When calibration completes, webhooks sorted by timestamp
- 1st webhook = Room 1.1, 2nd webhook = Room 1.2, etc.
- **Cannot produce wrong mappings!**

### 3-Layer Calibration Logic

**Layer 1: Real-time Sequence Matching**
- As bot moves, webhooks matched by position
- Saved with `source='sequence_calibration'`

**Layer 2: Timestamp Correction (Auto-runs on complete)**
- Sorts ALL webhooks by timestamp
- Re-matches to FIXED_ROOM_SEQUENCE
- Saved with `source='timestamp_calibration'` (HIGHEST priority)

**Layer 3: Fixed Sequence Fallback**
- Uses hardcoded 66 room names
- Deterministic and reliable

### Report Priority
```
1. timestamp_calibration  <- Uses FIXED sequence (BEST)
2. sequence_calibration   <- Real-time matching
3. webhook_calibration    <- Legacy
4. zoom_sdk_app           <- SDK fallback
```

---

## Fixed Room Sequence (66 Rooms)

```
Floor 1 (1.1 to 1.34)
---------------------
1.1:It's Accrual World
1.2:Between The Spreadsheet
1.3:Opera House
1.4:Statue Of Liberty
1.5:The Squad
1.6:Visionary Vault - Team Kruta
1.7:Inspiration Island - Team Kruta
1.8:Life In The Math Lane
1.9:Finance Pirates
1.10:Number Nook - Team Ganesh
1.11:Accountaholics
1.12:The Forbidden City
1.13:Dev's Professional Bungalow
1.14:Innovation Station
1.15:Precision Point
1.16:Creative Corner - Team Dev
1.17:Insight Lounge - Team Dev
1.18:Synergy Space - Team Dev
1.19:Numbers and Nuance
1.20:Sales Wizard
1.21:Sales Station
1.22:Virtual Vista
1.23:The Genius Lounge
1.24:Emirates Palace
1.25:Victoria Memorial
1.26:Number Nexus
1.27:Ledger Lounge
1.28:The Capital Corner
1.29:Meeting Room - Hawks Eye
1.30:HR Connect Room
1.31:HR Strategy Meeting Suite
1.32:Interview Room - 1
1.33:Interview Room - 2
1.34:Interview/Meeting - Eagle Eyes

Floor 2 (Vridam)
----------------
2.0:Vridam - Wellness Meeting Lounge

Floor 3 (Cloud Teams)
---------------------
3.1:Cloud Gunners
3.2:Cloud Knights
3.3:Cloud Avengers
3.4:Cloud Falcons
3.5:Cloud Titans
3.6:Cloud Guardians
3.7:Inspiration Lounge /Meeting Room
3.8:Agenda Chamber/Meeting Room
3.9:ABAP AMS

Floor 4 (KPRC)
--------------
4.1:KPRC - Legal Eagle
4.2:KPRC - Corporate Crest
4.3:KPRC - Innovation Lounge
4.4:KPRC - Decision Dome
4.5:KPRC - Focus Zone
4.6:KPRC - Strategic Space

Floor 5 (Accurest)
------------------
5.1:Accurest - HR Oasis
5.2:Accurest-Meeting Room:Strategist
5.3:Accurest - Meeting Room: Pioneer
5.4:Accurest - Automation Crafters
5.5:Accurest-Learning / Meeting room
5.6:Accurest - Sales Lounge
5.7:Accurest - Focus Lab
5.8:Accurest - Pattern Inbound
5.9:Accurest - Pattern Planning
5.10:Accurest - Himal's Suite
5.11:Accurest Insight : Team Shubham
5.12:Accurest - Creators
5.13:Accurest - Interview Room

Special Zones
-------------
6.0:Silence Zone
7.0:Masti Ki Pathshala
8.0:BREAK TIME - Tea/Lunch/ Dinner
```

---

## Current Deployment

- **Cloud Run URL:** `https://breakout-room-calibrator-r3wh42mg6q-uc.a.run.app`
- **GCP Project:** `variant-finance-data-project`
- **BigQuery Dataset:** `breakout_room_calibrator`
- **Current Revision:** 93

---

## Architecture

```
+------------------------------------------------------------------+
|                         Cloud Run                                 |
|  +------------------+    +-----------------------------------+    |
|  |  React App       |    |  Flask Server (app.py)            |    |
|  |  (Zoom SDK)      |----|  - /webhook (Zoom events)         |    |
|  |  /app endpoint   |    |  - /calibration/* (Scout Bot)     |    |
|  +------------------+    |  - /report/* (CSV generation)     |    |
|                          |  - /qos/* (Camera tracking)        |    |
|                          +-----------------------------------+    |
+------------------------------------------------------------------+
                                    |
                    +---------------+---------------+
                    v               v               v
            +--------------+ +----------+  +--------------+
            |   BigQuery   | | Zoom API |  |  SendGrid    |
            |   5 tables   | |  (QoS)   |  |  (Reports)   |
            +--------------+ +----------+  +--------------+
```

---

## Deployment Commands

```bash
# Full deployment
cd C:\Users\shash\Downloads\zoom+tracker
cd breakout-calibrator && npm run build && cd ..
gcloud run deploy breakout-room-calibrator --source . --region us-central1 --allow-unauthenticated --min-instances=1 --max-instances=1

# View logs
gcloud run services logs read breakout-room-calibrator --region us-central1 --limit 100

# Tail logs real-time
gcloud run services logs tail breakout-room-calibrator --region us-central1

# Get current URL
gcloud run services describe breakout-room-calibrator --region us-central1 --format="value(status.url)"
```

---

## API Endpoints

### Calibration
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/calibration/start` | POST | Start calibration session |
| `/calibration/mapping` | POST | Receive room mappings from SDK |
| `/calibration/pending` | GET | Check pending room moves |
| `/calibration/complete` | POST | Mark complete (triggers timestamp correction) |
| `/calibration/verify` | POST | Verify bot location |
| `/calibration/status` | GET | Get current status |
| `/calibration/correct` | POST | Manual timestamp correction |
| `/calibration/fixed-sequence` | GET | View hardcoded room sequence |
| `/calibration/reload` | POST | Force reload mappings |

### Reports
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/report/generate` | POST | Generate and email daily CSV |
| `/report/preview/<date>` | GET | Preview report data |

### QoS / Camera
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/qos/collect` | POST | Manual QoS collection |
| `/qos/scheduled` | POST | Scheduled collection |
| `/webhook` | POST | Zoom webhook events |
| `/health` | GET | Health check |
| `/mappings` | GET | Get current room mappings |

---

## Cloud Scheduler Jobs

| Job | Schedule (UTC) | IST Time | Endpoint |
|-----|----------------|----------|----------|
| `daily-qos-collection` | 03:45 | 9:15 AM | `/qos/scheduled` |
| `daily-attendance-report` | 04:30 | 10:00 AM | `/report/generate` |

---

## BigQuery Tables

| Table | Purpose |
|-------|---------|
| `participant_events` | Join/leave events |
| `room_mappings` | UUID -> room name (source priority matters) |
| `camera_events` | Camera ON/OFF |
| `qos_data` | Dashboard API metrics |
| `calibration_state` | Calibration persistence for resume |

---

## How Calibration Works Now

1. **User starts calibration** from Zoom App UI
2. **Scout Bot moves** through rooms in order (1.1 -> 1.2 -> 1.3...)
3. **Each move** triggers a webhook with room_uuid
4. **On completion**, `correct_calibration_by_timestamp()` runs:
   - Sorts all Scout Bot webhooks by arrival timestamp
   - 1st webhook = `1.1:It's Accrual World`
   - 2nd webhook = `1.2:Between The Spreadsheet`
   - ...continues for all 66 rooms
5. **Report uses** `timestamp_calibration` source (highest priority)

**Result:** Deterministic, accurate room mappings every time!

---

## Troubleshooting

### Cloud Run URL Changed
If URL changes after deployment:
1. Get new URL: `gcloud run services describe breakout-room-calibrator --region us-central1 --format="value(status.url)"`
2. Update Zoom webhook URL in Marketplace app
3. Update Cloud Scheduler jobs:
```bash
gcloud scheduler jobs update http daily-qos-collection --location=us-central1 --uri='NEW_URL/qos/scheduled'
gcloud scheduler jobs update http daily-attendance-report --location=us-central1 --uri='NEW_URL/report/generate'
```

### Webhook 401 Errors
- Check for duplicate webhook subscriptions in Zoom Marketplace
- Ensure only ONE subscription exists with correct URL
- Verify webhook secret matches `ZOOM_WEBHOOK_SECRET` env var

### Calibration Not Matching Webhooks
If logs show `Calibration NOT in progress`:
1. Stop current calibration
2. Click "Start Calibration" in Zoom App UI
3. This properly initializes the backend state

### BigQuery Streaming Buffer Error
```
UPDATE or DELETE statement over table would affect rows in the streaming buffer
```
- Recently inserted rows can't be modified
- Wait ~30 minutes or use TRUNCATE TABLE
- The system handles this gracefully

### Room names show as "Room-XXXXXXXX"
- Webhook calibration not completed
- Run calibration with Scout Bot
- Verify with BigQuery query

---

## Session Log (2026-03-26)

### Issues Found and Fixed

1. **Room mapping inaccuracy**
   - Problem: Webhooks arriving out of order
   - Solution: Hardcoded FIXED_ROOM_SEQUENCE + timestamp sorting

2. **Cloud Run URL changed**
   - Old: `https://breakout-room-calibrator-1041741270489.us-central1.run.app`
   - New: `https://breakout-room-calibrator-r3wh42mg6q-uc.a.run.app`
   - Updated: Zoom webhook URL, Cloud Scheduler jobs

3. **Duplicate webhook subscriptions**
   - Caused 401 errors on new URL
   - Solution: Remove old subscriptions from Zoom Marketplace

4. **Calibration state not persisting**
   - `calibration_in_progress: False` during active calibration
   - Solution: Must start from Zoom App UI to initialize properly

### Deployment History
- Revision 93: Fixed room sequence (66 rooms) + timestamp correction
- Revision 92: Increased calibration delays (5s move, 25s timeout)
- Revision 91: SQL injection fixes, calibration state persistence

---

## Critical Knowledge

1. **Camera webhooks DON'T exist** - Use Dashboard QoS API
2. **UUID mismatch** - SDK uses GUIDs, webhooks use base64-like
3. **QoS page_size max is 10** - Code fetches 200 pages
4. **Send mapping BEFORE moving bot** - Frontend -> backend -> move
5. **IST dates** - All event_date stored in IST (UTC+5:30)
6. **Single instance required** - `--max-instances=1` prevents state issues

---

## Environment Variables

Required in Cloud Run:
```
ZOOM_CLIENT_ID        # Server-to-Server OAuth app
ZOOM_CLIENT_SECRET    # Server-to-Server OAuth app
ZOOM_WEBHOOK_SECRET   # Webhook validation
ZOOM_ACCOUNT_ID       # Account ID for API calls
GCP_PROJECT_ID        # variant-finance-data-project
SENDGRID_API_KEY      # For email reports
REPORT_EMAIL_TO       # Recipients (comma separated)
```

Optional:
```
SCOUT_BOT_NAME        # Default: "Scout Bot"
SCOUT_BOT_EMAIL       # Email for bot matching
BQ_DATASET            # Default: "breakout_room_calibrator"
REPORT_EMAIL_FROM     # Sender email for reports
```

---

## Files Structure

| File | Purpose |
|------|---------|
| `app.py` | Main Flask server (~2800 lines) |
| `report_generator.py` | Daily CSV report generation |
| `breakout-calibrator/` | React app (Zoom SDK) |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Cloud Run deployment |
| `CLAUDE.md` | Claude Code instructions |
| `README.md` | This documentation |

---

## Report Generation Commands

```bash
# For specific date:
curl -X POST "https://breakout-room-calibrator-r3wh42mg6q-uc.a.run.app/report/generate" \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-03-26"}'

# Without date (defaults to yesterday):
curl -X POST "https://breakout-room-calibrator-r3wh42mg6q-uc.a.run.app/report/generate" \
  -H "Content-Type: application/json" \
  -d '{}'
```

---

## The UUID Mapping Problem and Solution

**Problem:** Zoom webhooks send room UUIDs like `n0a1FJhALeimJ5UPLUTxiw==` but we need room names like `1.1:It's Accrual World`. The SDK uses different UUID format `{E7F123FC-...}` than webhooks.

**Solution:** Scout Bot Calibration with Fixed Sequence
1. A participant named "Scout Bot" joins the meeting
2. Host runs the Zoom App and clicks "Move Scout Bot"
3. Scout Bot clicks "Join" on each room popup
4. When Scout Bot enters a room, the webhook captures the webhook UUID
5. On completion, webhooks sorted by timestamp and matched to FIXED_ROOM_SEQUENCE
6. All future participant events use this mapping for proper room names

---

## Zoom Credentials

| Type | ID |
|------|-----|
| Account ID | `xhKbAsmnSM6pNYYYurmqIA` |
| Server-to-Server Client ID | `TqtBGqTAS3W1Jgf9a41w` |
| Zoom App Client ID | `raEkn6HpTkWO_DCO3z5zGA` |

---

## Known Limitations (Zoom Platform)

1. **UUID Format Mismatch** - SDK uses GUIDs, webhooks use base64-like strings
2. **Cannot Force Participants** - SDK sends invite popup, user must click "Join"
3. **Webhooks on Entry Only** - Only fires when participant actually enters room
4. **REST API 404 for PMR** - Must use SDK for Personal Meeting Rooms

**Bottom Line:** There is no way to automatically map webhook UUIDs to room names without someone physically entering each room. This is a Zoom platform limitation.
