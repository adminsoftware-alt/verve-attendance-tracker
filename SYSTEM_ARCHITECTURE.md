# Zoom Attendance Tracker - Complete System Architecture

This document explains the entire system: where services run, how data flows, how durations are calculated, problems we solved, and how to access everything.

---

## Table of Contents

1. [GCP Services Overview](#1-gcp-services-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Data Flow - How Attendance is Captured](#3-data-flow---how-attendance-is-captured)
4. [Duration Calculation - How Hours are Computed](#4-duration-calculation---how-hours-are-computed)
5. [The presence_intervals System](#5-the-presence_intervals-system)
6. [Scheduled Jobs - Automation](#6-scheduled-jobs---automation)
7. [Problems We Faced and Solutions](#7-problems-we-faced-and-solutions)
8. [API Endpoints Reference](#8-api-endpoints-reference)
9. [How to Access Everything](#9-how-to-access-everything)
10. [Troubleshooting Guide](#10-troubleshooting-guide)

---

## 1. GCP Services Overview

### Project Details
| Item | Value |
|------|-------|
| **GCP Project ID** | `verve-attendance-tracker` |
| **GCP Project Number** | `1073587167150` |
| **Region** | `us-central1` |
| **BigQuery Dataset** | `breakout_room_calibrator` |

### Deployed Services

| Service | Type | URL | Purpose |
|---------|------|-----|---------|
| **Backend API** | Cloud Run | `https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app` | Flask server - all API endpoints, Zoom SDK app |
| **Frontend UI** | Cloud Run | `https://attendance-frontend-4e5na4tdha-uc.a.run.app` | React app - login, Team View, reports |
| **Zoom SDK App** | Served from Backend | `https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/app` | React app that runs inside Zoom client |
| **Scout Bot VM** | Compute Engine | `scout-bot-2` (us-central1-a) | Windows VM running the Zoom client + SDK app |

### Why Cloud Run?
- **Auto-scaling**: Handles traffic spikes without manual intervention
- **Pay-per-use**: Only charged when requests come in
- **Always-on**: `min-instances=1` keeps backend warm (no cold starts)
- **Auto-deploy**: Push to GitHub `main` branch triggers Cloud Build automatically

### BigQuery Tables

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `room_snapshots_v2` | **PRIMARY SOURCE** - SDK polls every 30s, stores who is in which room | `event_date`, `snapshot_time`, `participant_name`, `room_name` |
| `participant_events_p` | Webhook events from Zoom (join/leave) | `event_type`, `event_timestamp`, `participant_name`, `room_name` |
| `presence_intervals` | **MATERIALIZED VIEW** - Pre-computed attendance intervals | `participant_key`, `room_category`, `duration_seconds`, `start_ts`, `end_ts` |
| `teams` | Team definitions | `team_id`, `team_name`, `manager_name` |
| `team_members` | Who belongs to which team | `team_id`, `participant_name`, `participant_email` |
| `app_users` | Login credentials | `username`, `password`, `role` |
| `room_mappings` | Legacy - UUID to room name mapping | `room_uuid`, `room_name` |
| `qos_data` | Camera/quality metrics from Dashboard API | `camera_on_minutes`, `attentiveness_score` |

---

## 2. Architecture Diagram

```
                                    ZOOM MEETING
                                         |
                    +--------------------+--------------------+
                    |                                         |
                    v                                         v
           SCOUT BOT VM                              ZOOM WEBHOOKS
           (Windows + Zoom Desktop)                  (Automatic from Zoom)
           Auto-joins at 9:30 AM IST                      |
                    |                                      |
                    | HR opens Zoom App                    |
                    | clicks "Start Monitoring"            |
                    v                                      v
           ZOOM SDK APP (/app)                    POST /webhook
           Polls every 30 seconds                 participant_joined
           getMeetingParticipants()               participant_left
           getBreakoutRoomList()                  breakout_room_joined
                    |                             breakout_room_left
                    |                                      |
                    v                                      v
           POST /monitor/snapshot              INSERT INTO participant_events_p
                    |                                      |
                    v                                      |
           INSERT INTO room_snapshots_v2                   |
                    |                                      |
                    +------------------+-------------------+
                                       |
                                       v
                            BUILD PRESENCE_INTERVALS
                            (Hourly for today, Nightly for yesterday)
                                       |
                    +------------------+-------------------+
                    |                  |                   |
                    v                  v                   v
               SNAPSHOT           SYNTHESIZED         WEBHOOK_FILL
               INTERVALS          MAIN ROOM           INTERVALS
               (from polling)     (gaps between       (when polling
                                  breakouts)          was offline)
                                       |
                                       v
                            PRESENCE_INTERVALS TABLE
                            (Single source of truth)
                                       |
                    +------------------+-------------------+
                    |                  |                   |
                    v                  v                   v
               DAY VIEW          TEAM VIEW            REPORTS
               /attendance/      /teams/{id}/         /report/
               summary_v2        report/monthly_v2    generate
```

---

## 3. Data Flow - How Attendance is Captured

### Step 1: Scout Bot Joins Meeting
- **What**: A Windows VM (`scout-bot-vm`) runs Zoom Desktop
- **When**: Scheduled task runs at 9:30 AM IST daily
- **How**: Auto-joins meeting ID `9034027764` using Zoom client
- **Why VM?**: Zoom SDK requires a participant in the meeting to poll data

### Step 2: HR Starts Monitoring
- **What**: HR person opens the Zoom App (inside Zoom client)
- **Where**: Click "Apps" in Zoom toolbar → Select our app
- **Action**: App auto-starts monitoring (no click needed anymore)
- **Result**: SDK begins polling every 30 seconds

### Step 3: SDK Polling (Every 30 Seconds)
```javascript
// What the SDK does every 30s:
const participants = await zoomSdk.getMeetingParticipants();
const breakoutRooms = await zoomSdk.getBreakoutRoomList();

// Sends to backend:
POST /monitor/snapshot
{
  "meeting_id": "9034027764",
  "participants": [
    {"name": "John Doe", "room": "Team Alpha", "uuid": "abc123"},
    {"name": "Jane Smith", "room": "Main Room", "uuid": "def456"},
    ...
  ]
}
```

### Step 4: Data Storage
Each snapshot is stored in BigQuery `room_snapshots_v2`:
```sql
INSERT INTO room_snapshots_v2 (
  snapshot_id,      -- UUID
  snapshot_time,    -- When captured (UTC timestamp)
  event_date,       -- IST date (for partitioning)
  meeting_id,       -- "9034027764"
  room_name,        -- "Team Alpha" or "Main Room"
  participant_name, -- "John Doe"
  participant_email,-- (often empty from SDK)
  participant_uuid  -- Zoom's internal ID
)
```

### Step 5: Webhook Events (Automatic)
Zoom automatically sends webhooks when participants join/leave:
```
POST /webhook
{
  "event": "meeting.participant_joined",
  "payload": {
    "participant": {"user_name": "John Doe"},
    "meeting": {"id": "9034027764"}
  }
}
```
Stored in `participant_events_p` table.

---

## 4. Duration Calculation - How Hours are Computed

### The Core Problem
Raw data is just "snapshots" - point-in-time captures every 30 seconds. To get duration, we need to:
1. Group consecutive snapshots into intervals
2. Handle gaps (when was the person actually present?)
3. Categorize rooms (main, breakout, break)
4. Deduplicate (same person, multiple polling sources)

### The Solution: presence_intervals

We pre-compute all durations into a `presence_intervals` table. This table is the **single source of truth** for all reports.

#### Interval Categories
| Category | Definition | Examples |
|----------|------------|----------|
| `main` | Main Room or rooms starting with "0." | "Main Room", "0.Main Room" |
| `breakout` | Any named breakout room | "Team Alpha", "Project X" |
| `break` | Rooms containing "break" | "Break Time", "Coffee Break" |

#### Duration Formula
```
duration_seconds = (number of 30s buckets in interval) × 30

Example:
- Person in "Team Alpha" from 10:00:00 to 10:05:00
- That's 10 consecutive 30-second snapshots
- Duration = 10 × 30 = 300 seconds = 5 minutes
```

#### Gap Handling
If snapshots are more than 5 minutes apart, we start a new interval:
```
10:00:00 - Team Alpha (bucket 1)
10:00:30 - Team Alpha (bucket 2)
10:01:00 - Team Alpha (bucket 3)
... gap of 10 minutes (no snapshots) ...
10:11:00 - Team Alpha (bucket 1 of NEW interval)
```

### Three Sources of Interval Data

#### 1. Snapshot Intervals (`source='snapshot'`)
Direct from SDK polling. Most reliable. Contains:
- Exact room names
- 30-second precision
- Isolation detection (alone_seconds)

#### 2. Synthesized Main Room (`source='synthesized_main'`)
When someone is in breakouts but gaps exist between them:
```
09:00 - Main Room (joined meeting)
09:30 - Team Alpha (breakout started)
10:30 - Main Room ← SYNTHESIZED (gap after breakout)
10:45 - Team Beta (another breakout)
11:30 - Main Room ← SYNTHESIZED (gap after breakout)
```
**Why?** Between breakout sessions, people are in Main Room but SDK might miss it.

#### 3. Webhook Fill (`source='webhook_fill'`)
When SDK polling was offline but webhooks captured presence:
```
Scenario: HR's laptop died at 11 AM, came back at 3 PM
- Snapshots: 9 AM - 11 AM only
- Webhooks: Show John joined at 9 AM, left at 5 PM
- Result: Credit John for 11 AM - 3 PM as Main Room (webhook_fill)
```
**Why?** Webhooks are automatic from Zoom; they don't require the app to be running.

### Total Duration Calculation
```sql
-- For Team View pivot:
SELECT
  participant_name,
  SUM(CASE WHEN room_category = 'main' THEN duration_seconds ELSE 0 END) / 60 AS main_minutes,
  SUM(CASE WHEN room_category = 'breakout' THEN duration_seconds ELSE 0 END) / 60 AS breakout_minutes,
  SUM(CASE WHEN room_category = 'break' THEN duration_seconds ELSE 0 END) / 60 AS break_minutes,
  SUM(duration_seconds) / 60 AS total_minutes,
  SUM(alone_seconds) / 60 AS isolation_minutes
FROM presence_intervals
WHERE event_date = '2026-06-05'
GROUP BY participant_name
```

---

## 5. The presence_intervals System

### Why We Built It
Before: Every report endpoint had its own ~150-line SQL query that re-derived durations from raw snapshots. They drifted apart with every bug fix, producing inconsistent numbers.

After: One `build_presence_intervals()` function materializes all intervals. Every report just does `SELECT SUM(duration_seconds) FROM presence_intervals`.

### The Build Process (app.py: `build_presence_intervals`)

```
Step 1: Pull bucketed snapshot data
        └─ Group snapshots into 30-second buckets
        └─ Deduplicate (same person in same bucket → keep one)
        └─ Calculate occupancy per room per bucket

Step 2: Pull webhook timestamps
        └─ Get join/leave times from participant_events_p
        └─ Build presence windows (when was person in meeting?)

Step 3: Build snapshot intervals
        └─ Group consecutive buckets (gap ≤ 5 min = same interval)
        └─ Calculate duration_seconds, alone_seconds
        └─ Tag source='snapshot'

Step 4: Synthesize Main Room from webhooks
        └─ Find gaps not covered by snapshot intervals
        └─ Credit those gaps as Main Room time
        └─ Tag source='synthesized_main'

Step 5: Webhook-fill for no-snapshot participants
        └─ People with webhook presence but zero snapshots
        └─ Credit their webhook window as Main Room
        └─ Tag source='webhook_fill'

Step 5b: Drop inherited overnight meeting
        └─ Detect if meeting ran past midnight
        └─ Find mass-exit boundary (occupancy drop)
        └─ Drop all intervals before that boundary
        └─ (Prevents 17-18h phantom days)

Step 6: Atomic partition replace
        └─ DELETE all rows for this date
        └─ INSERT new intervals
        └─ Done via load job (no streaming buffer issues)
```

### When Intervals Are Built

| Trigger | Schedule | What Gets Built |
|---------|----------|-----------------|
| **Hourly job** | Every hour 9 AM - 11 PM IST | Today's date |
| **Nightly job** | 00:30 AM IST | Yesterday's date |
| **On-demand** | When you open Team View | Today/yesterday if stale (>90 min old) |
| **Manual backfill** | POST /intervals/backfill | Any date range you specify |

---

## 6. Scheduled Jobs - Automation

### Cloud Scheduler Jobs (updated 2026-07-17)

| Job Name | Region | Schedule (IST) | Endpoint | Purpose |
|----------|--------|----------------|----------|---------|
| `intervals-rebuild-today-2min` | asia-east1 | every 2 min, 8:00–23:59 | POST `/intervals/rebuild` | Keep today's data fresh |
| `intervals-auto-build-sweep` | asia-east1 | every 15 min | POST `/intervals/auto-build` `{"days_back":35}` | Self-heal any of the last 35 days |
| `reconcile-zoom-nightly` | asia-east1 | 10:00 (**PAUSED** — Zoom S2S creds broken) | POST `/reconcile/zoom` | Cross-check vs Zoom's records |
| `hourly-sheets-update` | us-central1 | hourly 9–23 | POST `/sheets/update` | Sync to Google Sheets |
| `hourly-presence-intervals-today`, `daily-presence-intervals` | us-central1 | **PAUSED** (superseded by the 2-min + sweep jobs) | POST `/intervals/rebuild` | — |
| `email-monitor-alert` | us-central1 | **PAUSED** | POST `/alert/email/check` | Alert if monitoring dies |

### Why These Schedules?

**Hourly today rebuild (9 AM - 11 PM)**
- Team View shows current day's data
- Without this, opening the view mid-day would show partial/stale hours
- Hourly = never more than 1 hour behind

**Nightly yesterday rebuild (00:30 AM)**
- After midnight, "today" becomes "yesterday"
- Late webhook events may arrive after people leave
- This final rebuild captures everything

**Self-healing backstop (in code)**
- If a scheduler dies (like it did when URL was wrong)
- Opening Team View triggers rebuild if data is >90 min stale
- System auto-recovers without manual intervention

---

## 7. Problems We Faced and Solutions

### Problem 1: 17-18 Hour Phantom Days (Overnight Meeting Inflation)

**Symptom**: ~86% of people showed 17-18h attendance on some days

**Root Cause**: Zoom meetings can run 24+ hours. When a meeting ran past midnight:
- The post-midnight tail landed on the NEXT day's partition
- Everyone appeared to be "working" from 00:00 until the meeting actually ended (~8:56 AM)
- Then the real meeting started at 9 AM

**Solution**: Step 5b in `build_presence_intervals`:
1. Detect if ≥10 people are present at 00:00 IST (inherited meeting signal)
2. Find the "mass exit" boundary (occupancy drops to <50% of start level)
3. Drop all intervals starting before that boundary
4. Result: Only count the meeting that STARTS today

**Config**: `INHERITED_MIN_PARTICIPANTS=10`, `INHERITED_LEAVE_FRACTION=0.5`

---

### Problem 2: Report vs Live Undercount (Snapshot Outage)

**Symptom**: Report showed 5h when people actually worked 9h

**Root Cause**: SDK polling requires HR's laptop/app to be running. If HR disconnects:
- Snapshots stop (no data captured)
- But Zoom webhooks keep flowing (automatic)
- Snapshots only covered 9-11 AM; webhooks showed 9 AM - 6 PM

**Solution**: Step 4 webhook-fill synthesis:
- For each person, compare webhook presence windows vs snapshot coverage
- Credit any webhook-present time NOT covered by snapshots as Main Room
- Tagged `source='webhook_fill'` for auditability

---

### Problem 3: Team View Showing 1-2 Hours (Frozen Data)

**Symptom**: Team View pivot showed 1-2h for recent days, Day View showed correct 8-9h

**Root Cause**: Two compounding bugs:
1. **Dead scheduler URL**: The nightly rebuild job was POSTing to old URL `...-1073587167150...` which no longer existed. Yesterday never got rebuilt automatically.
2. **Lazy-only builder**: The pivot's auto-builder only built dates with ZERO rows. A day frozen from an early-morning lazy build (someone opened view at 11 AM with only 2h of data) was never refreshed.

**Solution**:
1. Fixed all scheduler jobs to use live URL `...-4e5na4tdha-uc...`
2. Created hourly today-rebuild job
3. Rewrote `_auto_build_dates_in_range` to rebuild today/yesterday if stale (>90 min old)
4. Skip future dates (they have no data; building them wastes 20s per load)

---

### Problem 4: 30-Second Bucket Duplication

**Symptom**: Double-counted attendance when multiple polling sources ran

**Root Cause**: HR's laptop AND Scout Bot VM both polling simultaneously. Same person, same second, two snapshot rows.

**Solution**: 30-second bucket deduplication in Step 1:
```sql
SELECT * FROM snapshots
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY participant_key, bucket30
  ORDER BY CASE WHEN room = 'Main Room' THEN 1 ELSE 0 END
) = 1
```
Within same person + same 30-second bucket, keep only one row (prefer breakout over Main Room).

---

### Problem 5: Name Matching Failures

**Symptom**: Team member showed 0 hours even though they attended

**Root Cause**: Zoom display names have variations:
- Registry: "John Doe"
- Zoom: "John Doe (iPhone)", "john doe", "John  Doe" (extra space)

**Solution**: Normalized name matching:
```python
def _sql_normalize_name(col):
    # Remove parenthetical suffixes: "John (iPhone)" → "John"
    # Lowercase, trim, collapse spaces
    return f"LOWER(TRIM(REGEXP_REPLACE({col}, r'\\s*\\([^)]*\\)\\s*$', '')))"
```
Team-member-to-intervals join uses normalized names on both sides.

---

## 8. API Endpoints Reference

### Authentication
| Endpoint | Method | Purpose | Example |
|----------|--------|---------|---------|
| `/auth/login` | POST | Validate credentials, returns a signed 12h token | `{"username":"<user>","password":"<password>"}` |
| `/auth/users` | GET | List all users | Returns array of users |

### Monitoring (SDK Data Ingestion)
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/monitor/snapshot` | POST | Receive SDK polling data (every 30s) |
| `/monitor/status` | GET | Check snapshot counts for today |
| `/monitor/health` | GET | Check if monitoring is active (HEALTHY/STALE/NO_DATA) |

### Team View (v2 - Uses presence_intervals)
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/teams/<id>/attendance_v2/<date>` | GET | Single day team attendance |
| `/teams/<id>/attendance/range_v2?start=X&end=Y` | GET | Date range attendance |
| `/teams/<id>/report/monthly_v2?year=Y&month=M` | GET | Monthly pivot (what Team View uses) |

### Day View (v2)
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/attendance/summary_v2/<date>` | GET | Full day attendance with room visits |

### Interval Management
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/intervals/rebuild` | POST | Build intervals for one date. Body: `{"date":"2026-06-05"}` or `{"days_ago":1}` |
| `/intervals/backfill` | POST | Build intervals for date range. Body: `{"start":"2026-06-01","end":"2026-06-05"}` |
| `/intervals/status?date=X` | GET | Check if intervals exist, when built, stats |

### Teams Management
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/teams` | GET | List all teams |
| `/teams` | POST | Create team |
| `/teams/<id>` | GET/PUT/DELETE | Team CRUD |
| `/teams/<id>/members` | POST | Add member |
| `/teams/<id>/members/<mid>` | DELETE | Remove member |

### Reports
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/report/generate` | POST | Generate and email CSV report |
| `/report/preview/<date>` | GET | Preview report data |

---

## 9. How to Access Everything

### Frontend (Team View, Login)
```
https://attendance-frontend-4e5na4tdha-uc.a.run.app
```
Login with your dashboard credentials (managed in the `app_users` table; passwords are bcrypt-hashed — ask an admin for access).

### Backend Health Check
```bash
curl https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/health
```

### Check Monitoring Status
```bash
curl https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/monitor/health
```

### Check Intervals for a Date
```bash
curl "https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/intervals/status?date=2026-06-05"
```

### Manually Rebuild a Date
```bash
curl -X POST https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/intervals/rebuild \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-06-05"}'
```

### Backfill Multiple Dates
```bash
curl -X POST https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/intervals/backfill \
  -H "Content-Type: application/json" \
  -d '{"start": "2026-06-01", "end": "2026-06-05"}'
```

### View Cloud Run Logs
```bash
gcloud run services logs read breakout-room-calibrator --region us-central1 --limit 100 --project=verve-attendance-tracker
```

### Check Scheduler Jobs
```bash
gcloud scheduler jobs list --location=us-central1 --project=verve-attendance-tracker
```

### Query BigQuery Directly
```bash
bq query --project_id=verve-attendance-tracker --use_legacy_sql=false '
SELECT event_date, COUNT(*) as intervals, COUNT(DISTINCT participant_key) as people
FROM `verve-attendance-tracker.breakout_room_calibrator.presence_intervals`
WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
GROUP BY event_date ORDER BY event_date
'
```

### Scout Bot VM Access
GCP Console → Compute Engine → `scout-bot-2` (us-central1-a). Connect via RDP;
credentials are in the team password manager (not stored in this repo).

---

## 10. Troubleshooting Guide

### "Team View shows 0 hours for someone"
1. Check if they're in the team: `/teams/<id>` → members
2. Check their name matches: compare registry name vs Zoom display name
3. Check if snapshots exist: query `room_snapshots_v2` for their name
4. Check if intervals exist: query `presence_intervals` for their name

### "Data looks stale / not updating"
1. Check monitoring health: `GET /monitor/health`
   - `HEALTHY`: SDK polling is active
   - `STALE`: Last snapshot >5 min ago (HR may have disconnected)
   - `NO_DATA`: No snapshots today (meeting not started or app not opened)
2. Check scheduler jobs: `gcloud scheduler jobs list`
3. Manually rebuild: `POST /intervals/rebuild {"date":"2026-06-05"}`

### "Durations seem inflated (17+ hours)"
1. Check if overnight meeting boundary was detected
2. Look at logs for `[BuildIntervals] inherited overnight meeting detected`
3. If not detected, check `INHERITED_MIN_PARTICIPANTS` threshold

### "Durations seem too low"
1. Check if SDK polling was running all day: query snapshot count by hour
2. Check webhook-fill: intervals with `source='webhook_fill'`
3. If no webhooks either, the person may have truly been absent

### "Build taking too long / timeout"
1. Don't rebuild more than 10 days at once (Cloud Run 300s timeout)
2. Use `/intervals/backfill` with smaller chunks
3. Each day takes ~17-27 seconds to build

---

## Summary

This system captures Zoom meeting attendance through SDK polling (every 30s) and webhooks, stores raw data in BigQuery, materializes it into `presence_intervals` for consistent duration calculations, and serves it through v2 API endpoints to the Team View frontend. Freshness is maintained by hourly/nightly Cloud Scheduler jobs plus a self-healing code backstop.

**Key URLs:**
- Frontend: `https://attendance-frontend-4e5na4tdha-uc.a.run.app`
- Backend: `https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app`
- Zoom App: `https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/app`

**Key Endpoints:**
- Team View data: `/teams/<id>/report/monthly_v2`
- Rebuild intervals: `/intervals/rebuild`
- Check status: `/intervals/status`, `/monitor/health`

**Key Tables:**
- Raw snapshots: `room_snapshots_v2`
- Raw webhooks: `participant_events_p`
- Computed intervals: `presence_intervals`
