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

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│   Zoom Meeting  │────>│  Zoom Webhooks   │────>│  Cloud Run (app.py) │
│ (Breakout Rooms)│     │                  │     │                     │
└─────────────────┘     └──────────────────┘     └──────────┬──────────┘
                                                            │
┌─────────────────┐     ┌──────────────────┐               │
│  Zoom Apps SDK  │────>│  Calibration     │───────────────┘
│  (In-meeting)   │     │  Endpoints       │               │
└─────────────────┘     └──────────────────┘               │
                                                            v
                                                   ┌─────────────┐
                                                   │  BigQuery   │
                                                   │  (Storage)  │
                                                   └─────────────┘
```

## The UUID Mapping Problem & Solution

**Problem:** Zoom webhooks send room UUIDs like `n0a1FJhALeimJ5UPLUTxiw==` but we need room names like `1.1:It's Accrual World`. The SDK uses different UUID format `{E7F123FC-...}` than webhooks.

**Solution:** Scout Bot Calibration
1. A participant named "Scout Bot" joins the meeting
2. Host runs the Zoom App and clicks "Move Scout Bot"
3. Scout Bot clicks "Join" on each room popup
4. When Scout Bot enters a room, the webhook captures the webhook UUID
5. The app maps webhook UUID to the SDK room name
6. All future participant events use this mapping for proper room names

## Project Structure

```
zoom-tracker/
├── app.py                                 # Main Flask server
├── Dockerfile                             # Container config for Cloud Run
├── requirements.txt                       # Python dependencies
├── bigquery_schemas.sql                   # BigQuery table definitions
├── attendance_report_with_room_names.sql  # Main report query
├── report_generator.py                    # Daily report generation
└── breakout-calibrator/                   # Zoom Apps SDK React app
    ├── src/
    │   ├── components/CalibrationPanel.jsx
    │   ├── hooks/useZoomSdk.js
    │   └── services/
    │       ├── zoomService.js
    │       └── apiService.js
    ├── build/                             # Production build
    └── package.json
```

## Current Deployment

- **Cloud Run URL:** `https://breakout-room-calibrator-1041741270489.us-central1.run.app`
- **GCP Project:** `variant-finance-data-project`
- **BigQuery Dataset:** `breakout_room_calibrator`

## Zoom Credentials

| Type | ID |
|------|-----|
| Account ID | `xhKbAsmnSM6pNYYYurmqIA` |
| Server-to-Server Client ID | `TqtBGqTAS3W1Jgf9a41w` |
| Zoom App Client ID | `raEkn6HpTkWO_DCO3z5zGA` |

## Setup Guide

### 1. BigQuery Setup

```bash
bq mk --dataset variant-finance-data-project:breakout_room_calibrator
```

Run `bigquery_schemas.sql` in BigQuery Console to create tables.

### 2. Zoom Marketplace Setup

#### A. Server-to-Server OAuth App (for API calls)
1. Create "Server-to-Server OAuth" app
2. Add scopes: `meeting:read:admin`, `user:read:admin`
3. Note: Account ID, Client ID, Client Secret

#### B. User-Managed App (for Zoom Apps SDK)
1. Create "User-managed" app
2. Configure URLs:
   - **Home URL:** `https://breakout-room-calibrator-1041741270489.us-central1.run.app/app`
   - **OAuth Redirect URL:** `https://breakout-room-calibrator-1041741270489.us-central1.run.app/app`
3. Add scope: `zoomapp:inmeeting`
4. Enable "In-Meeting" feature
5. Add SDK capabilities:
   - `getBreakoutRoomList`
   - `getMeetingParticipants`
   - `assignParticipantToBreakoutRoom`
   - `changeBreakoutRoom`
   - `getMeetingContext`
   - `getMeetingUUID`
   - `getUserContext`

#### C. Configure Webhooks
1. **Webhook URL:** `https://breakout-room-calibrator-1041741270489.us-central1.run.app/webhook`
2. Add events:
   - `meeting.participant_joined`
   - `meeting.participant_left`
   - `meeting.participant_joined_breakout_room`
   - `meeting.participant_left_breakout_room`
   - `meeting.participant_video_on`
   - `meeting.participant_video_off`
   - `meeting.ended`

### 3. Deploy to Cloud Run

```bash
# Build React app
cd breakout-calibrator
npm install
npm run build
cd ..

# Deploy
gcloud run deploy breakout-room-calibrator \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars "GCP_PROJECT_ID=variant-finance-data-project,BQ_DATASET=breakout_room_calibrator,SCOUT_BOT_NAME=Scout Bot"
```

## Daily Usage

### Calibration Process (Required for Room Name Mapping)

**Setup:** Two people needed
- **Person 1 (Host):** Runs the Zoom App
- **Person 2 (Scout Bot):** Separate account named "Scout Bot"

**Steps:**
1. Host starts meeting with breakout rooms OPEN
2. Scout Bot account joins the meeting
3. Host opens Apps → "Breakout Room Calibrator"
4. Host clicks **"Move Scout Bot"**
5. Scout Bot clicks **"Join"** on each popup (10 sec per room)
6. Wait until all rooms are mapped

**Timing:** ~10 minutes for 66 rooms (10 seconds each)

### Verify Calibration

Run in BigQuery:
```sql
SELECT source, COUNT(*) as count
FROM `variant-finance-data-project.breakout_room_calibrator.room_mappings`
WHERE mapping_date = CURRENT_DATE()
GROUP BY source;
```

Should show both:
- `zoom_sdk_app` - SDK mappings
- `webhook_calibration` - Webhook UUID mappings

### Generate Reports

Run `attendance_report_with_room_names.sql` in BigQuery Console.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/webhook` | POST | Zoom webhook receiver |
| `/calibration/start` | POST | Start calibration |
| `/calibration/mapping` | POST | Receive room mappings |
| `/calibration/complete` | POST | Complete calibration |
| `/calibration/status` | GET | Check status |
| `/mappings` | GET | Get room mappings |
| `/app` | GET | Zoom SDK app |
| `/test/bigquery` | GET | Test BigQuery |
| `/debug/state` | GET | View meeting state |

## BigQuery Tables

| Table | Description |
|-------|-------------|
| `participant_events` | Join/leave/room events |
| `room_mappings` | UUID to room name mappings |
| `camera_events` | Video on/off events |
| `qos_data` | Quality/duration data |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GCP_PROJECT_ID` | Google Cloud project ID |
| `BQ_DATASET` | BigQuery dataset name |
| `ZOOM_WEBHOOK_SECRET` | Webhook verification secret |
| `ZOOM_ACCOUNT_ID` | Server-to-Server Account ID |
| `ZOOM_CLIENT_ID` | Server-to-Server Client ID |
| `ZOOM_CLIENT_SECRET` | Server-to-Server Client Secret |
| `SCOUT_BOT_NAME` | Bot name (default: "Scout Bot") |

## Known Issues & Limitations (Zoom SDK/Webhook)

### Issue 1: UUID Format Mismatch Between SDK and Webhooks

**Problem:** Zoom SDK and Webhooks use completely different UUID formats for the same room.
- SDK UUID format: `{E7F123FC-EE33-47D8-BC5E-C84FCD31E06F}` (GUID with braces)
- Webhook UUID format: `n0a1FJhALeimJ5UPLUTxiw==` (base64-like string)

**Impact:** Cannot directly match SDK room data with webhook events.

**Solution:** Scout Bot must physically enter each room during calibration. When Scout Bot joins a room:
1. The SDK knows which room name Scout Bot is moving to
2. The webhook sends the webhook UUID when Scout Bot enters
3. We capture and store: `webhook_uuid → room_name`

### Issue 2: SDK Cannot Force Participants Into Rooms

**Problem:** Zoom's security prevents the SDK from automatically moving participants into breakout rooms without their consent.
- `assignParticipantToBreakoutRoom()` sends an invitation popup
- `changeBreakoutRoom()` also shows a confirmation popup
- Participant MUST click "Join" to actually enter the room

**Impact:** Calibration cannot be fully automated. Someone must manually click "Join" on each popup.

**Solution:** Two-person calibration process:
1. Host clicks "Move Scout Bot" in the app
2. Scout Bot (separate account) clicks "Join" on each popup
3. Each room takes ~10 seconds (time to click + enter room)

### Issue 3: Webhooks Only Fire on Actual Room Entry

**Problem:** Zoom only sends `participant_joined_breakout_room` webhook when a participant **actually enters** the room, not when they are assigned/invited.

**Impact:** If Scout Bot doesn't click "Join", no webhook fires, and the UUID mapping is not captured.

**Solution:** Scout Bot MUST click "Join" on every room popup during calibration. There is no workaround for this Zoom limitation.

### Issue 4: REST API Does Not Return Breakout Room Names for PMR

**Problem:** The Zoom REST API endpoint `/meetings/{id}/breakout_rooms` returns 404 for Personal Meeting Rooms (PMR).

**Impact:** Cannot get room names via REST API for PMR meetings.

**Solution:** Must use Zoom Apps SDK (runs inside the meeting) to get room names via `getBreakoutRoomList()`.

### Summary: Why Physical Room Entry is Required

| What We Need | How We Get It | Limitation |
|--------------|---------------|------------|
| Room Names | SDK `getBreakoutRoomList()` | Only works inside meeting |
| SDK UUIDs | SDK `getBreakoutRoomList()` | Different format than webhooks |
| Webhook UUIDs | Webhook events | Only fires when someone enters room |
| UUID Mapping | Scout Bot enters each room | Must click "Join" manually |

**Bottom Line:** There is no way to automatically map webhook UUIDs to room names without someone physically entering each room. This is a Zoom platform limitation, not a bug in our code.

---

## Troubleshooting

### Room names show as "Room-XXXXXXXX"
- Webhook calibration not completed
- Run calibration with Scout Bot clicking "Join" on each popup
- Verify with: `SELECT source, COUNT(*) FROM room_mappings WHERE mapping_date = CURRENT_DATE() GROUP BY source`
- Must see both `zoom_sdk_app` AND `webhook_calibration` sources

### Calibration too fast / missing rooms
- Adjust `MOVE_DELAY_MS` in `breakout-calibrator/src/services/zoomService.js`
- Current setting: 10000ms (10 seconds)

### Webhook validation failed
- Check `ZOOM_WEBHOOK_SECRET` matches Zoom Marketplace
- Update via: `gcloud run services update breakout-room-calibrator --set-env-vars "ZOOM_WEBHOOK_SECRET=your-secret"`

### Check logs
```bash
gcloud run services logs read breakout-room-calibrator --region us-central1 --limit 100
```

## Key Files

| File | Purpose |
|------|---------|
| `app.py` | Main server - webhooks, calibration, API |
| `breakout-calibrator/src/services/zoomService.js` | Calibration timing (MOVE_DELAY_MS) |
| `breakout-calibrator/src/hooks/useZoomSdk.js` | Zoom SDK integration |
| `breakout-calibrator/src/components/CalibrationPanel.jsx` | Calibration UI |
| `attendance_report_with_room_names.sql` | Report query |
| `bigquery_schemas.sql` | Table definitions |


 curl -X POST "https://breakout-room-calibrator-1041741270489.us-central1.run.app/report/generate" -H "Content-Type: application/json" -d          '{"date": "2026-02-25"}'



  For any date:
  curl -X POST "https://breakout-room-calibrator-1041741270489.us-central1.run.app/report/generate" -H "Content-Type: application/json" -d
  '{"date": "YYYY-MM-DD"}'

  Without date (defaults to yesterday):
  curl -X POST "https://breakout-room-calibrator-1041741270489.us-central1.run.app/report/generate" -H "Content-Type: application/json" -d '{}'   

────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────