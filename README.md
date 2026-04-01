# Zoom Breakout Room Tracker

A complete solution for tracking participant attendance in Zoom breakout rooms using SDK Monitoring. Deployed on Google Cloud Run with BigQuery storage.

---

## Project Overview

This system captures participant activity in Zoom meetings and breakout rooms. The Zoom SDK polls every 30 seconds to capture who is in which room, storing data in BigQuery for daily reports.

**Key Achievement:** No calibration needed! SDK provides room names directly.

---

## Quick Reference

### URLs
| Service | URL |
|---------|-----|
| Cloud Run (Primary) | `https://breakout-room-calibrator-1041741270489.us-central1.run.app` |
| Cloud Run (Alternate) | `https://breakout-room-calibrator-r3wh42mg6q-uc.a.run.app` |
| Zoom App Home URL | `https://breakout-room-calibrator-1041741270489.us-central1.run.app/app` |

### IDs & Credentials
| Item | Value |
|------|-------|
| GCP Project ID | `variant-finance-data-project` |
| BigQuery Dataset | `breakout_room_calibrator` |
| Zoom Account ID | `xhKbAsmnSM6pNYYYurmqIA` |
| Zoom Server-to-Server Client ID | `TqtBGqTAS3W1Jgf9a41w` |
| Zoom App Client ID | `raEkn6HpTkWO_DCO3z5zGA` |
| Zoom App ID | `RRYgo_e2QE697_mxkp3tzg` |
| Meeting ID | `9034027764` |
| Current Revision | **124** |

### Scout Bot VM
| Setting | Value |
|---------|-------|
| VM Name | `scout-bot-vm` |
| External IP | `34.47.178.82` |
| Zone | `asia-south1-a` |
| Machine Type | `e2-medium` (2 vCPU, 4GB RAM) |
| OS | Windows Server |
| Username | `dataapps` |
| Password | `ScoutBot2026` |

---

## Daily Workflow

```
1. Meeting starts (e.g., 9:30 AM IST)
   └── Scout Bot VM auto-joins meeting (scheduled task)

2. HR connects via RDP (1 minute only)
   └── IP: 34.47.178.82
   └── Username: dataapps
   └── Password: ScoutBot2026
   └── Clicks: Apps → Breakout Room Calibrator
   └── Monitoring auto-starts
   └── HR disconnects (VM continues independently)

3. SDK polls every 30 seconds
   └── Captures: room names + participants
   └── Saves to: BigQuery room_snapshots table

4. 11:15 AM IST - Report auto-generated
   └── Cloud Scheduler triggers /report/generate
   └── CSV emailed to recipients
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Scout Bot VM (GCP)                            │
│   Windows + Zoom Desktop                                         │
│   Auto-joins meeting via scheduled task                          │
│   HR clicks app once → monitoring starts                         │
└───────────────────────────┬─────────────────────────────────────┘
                            │ SDK polls every 30s
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Cloud Run (Flask + React)                     │
│                                                                  │
│  React App (/app)          Flask Server (app.py)                │
│  - MonitorPanel.jsx        - /monitor/snapshot (save data)      │
│  - useZoomSdk.js           - /report/generate (daily CSV)       │
│  - Polls SDK every 30s     - /webhook (Zoom events)             │
└─────────────────────────────────────────────────────────────────┘
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
    ┌──────────────┐ ┌──────────┐  ┌──────────────┐
    │   BigQuery   │ │ Zoom SDK │  │  SendGrid    │
    │              │ │          │  │              │
    │ room_snapshots│ │ Polling  │  │ Email CSV    │
    │ participant_  │ │ Data     │  │ Reports      │
    │ events       │ │          │  │              │
    └──────────────┘ └──────────┘  └──────────────┘
```

---

## BigQuery Tables

### Dataset: `variant-finance-data-project.breakout_room_calibrator`

### 1. room_snapshots (PRIMARY - SDK Monitoring)
```sql
CREATE TABLE room_snapshots (
  snapshot_id STRING NOT NULL,
  snapshot_time TIMESTAMP NOT NULL,
  event_date STRING NOT NULL,          -- IST date (YYYY-MM-DD)
  meeting_id STRING NOT NULL,
  room_name STRING NOT NULL,           -- Actual room name from SDK
  participant_name STRING,
  participant_email STRING,            -- Often empty (SDK limitation)
  participant_uuid STRING,
  inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
);
```
**Source:** SDK polls every 30s → POST `/monitor/snapshot`

### 2. participant_events (Webhooks)
```sql
CREATE TABLE participant_events (
  event_id STRING,
  event_type STRING,                   -- participant_joined, participant_left
  event_timestamp STRING,              -- Exact time from Zoom
  event_date STRING,                   -- IST date
  meeting_id STRING,
  meeting_uuid STRING,
  participant_id STRING,
  participant_name STRING,
  participant_email STRING,
  room_uuid STRING,                    -- Base64-like UUID
  room_name STRING,                    -- Mapped name (if calibrated)
  inserted_at TIMESTAMP
);
```
**Source:** Zoom webhooks → POST `/webhook`

### 3. room_mappings (Legacy - Calibration)
```sql
CREATE TABLE room_mappings (
  mapping_id STRING,
  meeting_id STRING,
  meeting_uuid STRING,
  room_uuid STRING,
  room_name STRING,
  room_index INTEGER,
  mapping_date DATE,
  mapped_at TIMESTAMP,
  source STRING                        -- webhook_calibration, sequential_calibration
);
```

### 4. qos_data (Camera/Quality)
```sql
CREATE TABLE qos_data (
  qos_id STRING,
  meeting_uuid STRING,
  participant_id STRING,
  participant_name STRING,
  participant_email STRING,
  join_time STRING,
  leave_time STRING,
  duration_minutes FLOAT,
  attentiveness_score FLOAT,
  camera_on_count INTEGER,
  camera_on_minutes FLOAT,
  camera_on_intervals STRING,
  recorded_at TIMESTAMP,
  event_date STRING
);
```
**Source:** Dashboard QoS API → `/qos/scheduled`

### 5. camera_events
```sql
CREATE TABLE camera_events (
  event_id STRING,
  event_type STRING,                   -- camera_on, camera_off
  event_timestamp STRING,
  event_date STRING,
  meeting_id STRING,
  participant_name STRING,
  camera_on BOOLEAN,
  room_name STRING,
  duration_seconds INTEGER,
  inserted_at TIMESTAMP
);
```

---

## API Endpoints

### Monitor Mode (SDK Polling)
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/monitor/snapshot` | POST | Receive SDK polling data → saves to room_snapshots |
| `/monitor/status` | GET | Check snapshot counts for today |
| `/monitor/health` | GET | Check if monitoring is active (HEALTHY/STALE/NO_DATA) |
| `/monitor/sample` | GET | View sample snapshot data (last 50 rows) |

### Reports
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/report/generate` | POST | Generate and email daily CSV |
| `/report/preview/<date>` | GET | Preview report data as JSON |

### Webhooks
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/webhook` | POST | Receive Zoom webhook events |

### QoS / Camera
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/qos/scheduled` | POST | Scheduled QoS collection |
| `/qos/collect` | POST | Manual QoS collection |

### Health & Debug
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Service health check |
| `/mappings` | GET | Get current room mappings |
| `/debug/bq-mappings` | GET | Check BigQuery mappings |

---

## Cloud Scheduler Jobs

| Job | UTC Time | IST Time | Endpoint | Purpose |
|-----|----------|----------|----------|---------|
| `daily-qos-collection` | 04:00 | 9:30 AM | `/qos/scheduled` | Collect camera data |
| `daily-attendance-report` | 05:45 | 11:15 AM | `/report/generate` | Email daily CSV |

---

## Commands Reference

### Deploy to Cloud Run
```bash
cd C:\Users\shash\Downloads\zoom+tracker
cd breakout-calibrator && npm run build && cd ..
gcloud run deploy breakout-room-calibrator --source . --region us-central1 --allow-unauthenticated --min-instances=1
```

### View Logs
```bash
# Recent logs
gcloud run services logs read breakout-room-calibrator --region us-central1 --limit 100

# Real-time logs
gcloud run services logs tail breakout-room-calibrator --region us-central1
```

### Check Service Status
```bash
gcloud run services describe breakout-room-calibrator --region us-central1 --format="value(status.url)"
```

### Generate Report (curl)
```bash
curl -X POST "https://breakout-room-calibrator-1041741270489.us-central1.run.app/report/generate" \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-04-01"}'
```

### Check Monitor Status
```bash
curl "https://breakout-room-calibrator-1041741270489.us-central1.run.app/monitor/status"
```

### Check Monitor Health
```bash
curl "https://breakout-room-calibrator-1041741270489.us-central1.run.app/monitor/health"
```

### View Sample Snapshots
```bash
curl "https://breakout-room-calibrator-1041741270489.us-central1.run.app/monitor/sample"
```

### Preview Report
```bash
curl "https://breakout-room-calibrator-1041741270489.us-central1.run.app/report/preview/2026-04-01"
```

---

## Scout Bot VM Setup

### Connect via RDP
```
IP: 34.47.178.82
Username: dataapps
Password: ScoutBot2026
```

### VM Automation Files
```
C:\ScoutBot\join_meeting.bat    -- Auto-join script
Scheduled Task: ScoutBot-JoinMeeting
```

### Change Scheduled Task Time
```cmd
schtasks /change /tn "ScoutBot-JoinMeeting" /st 09:30
```

### Check Scheduled Task
```cmd
schtasks /query /tn "ScoutBot-JoinMeeting"
```

### VM Auto-login (Registry)
```
HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon
- AutoAdminLogon = 1
- DefaultUserName = dataapps
- DefaultPassword = ScoutBot2026
```

### join_meeting.bat Content
```batch
@echo off
timeout /t 60 /nobreak
start "" "zoommtg://zoom.us/join?confno=9034027764"
```

---

## How SDK Monitoring Works

### Data Flow
```
1. Scout Bot VM joins Zoom meeting
2. HR opens Zoom App (MonitorPanel)
3. App auto-starts monitoring (if host/co-host)
4. Every 30 seconds:
   a. getBreakoutRoomList() → room names + UUIDs
   b. getMeetingParticipants() → who's in which room
   c. Build snapshot data
   d. POST /monitor/snapshot → Cloud Run
   e. Save to BigQuery room_snapshots table
```

### Room Transition Detection (SQL)
```sql
-- Compare consecutive snapshots for each participant
SELECT
  participant_name,
  room_name,
  snapshot_time,
  LAG(room_name) OVER (PARTITION BY participant_name ORDER BY snapshot_time) as prev_room
FROM room_snapshots

-- When room_name != prev_room → room transition detected!
```

### Why No Calibration Needed
```
OLD METHOD (Calibration):
- Webhooks send UUIDs like "n0a1FJhALeimJ5UPLUTxiw=="
- Need Scout Bot to visit each room to map UUID → name
- Takes 30+ minutes for 66 rooms

NEW METHOD (SDK Monitoring):
- SDK's getBreakoutRoomList() returns room names directly!
- No UUID mapping needed
- Just poll and store
```

---

## Report Format (Rev 124+)

### One Row Per Room Visit
Each room visit is a separate row (not concatenated with `->`)

### CSV Columns
| Column | Description | Source |
|--------|-------------|--------|
| Name | Participant name | Snapshots |
| Email | Participant email | Webhooks |
| Main_Joined_IST | Meeting join time | Webhooks |
| Main_Left_IST | Meeting leave time | Webhooks |
| Room | Breakout room name | Snapshots |
| Room_Joined_IST | Room entry time | Snapshots |
| Room_Left_IST | Room exit time | Snapshots |
| Duration_Minutes | Time in this room | Calculated |

### Example Output
```csv
Name,Email,Main_Joined_IST,Main_Left_IST,Room,Room_Joined_IST,Room_Left_IST,Duration_Minutes
Abhishek Rathi,abhishek.rathi@verveadvisory.com,10:21,18:33,1.8:Life In The Math Lane,13:55,14:04,9
Abhishek Rathi,abhishek.rathi@verveadvisory.com,10:21,18:33,8.0:BREAK TIME - Tea/Lunch/ Dinner,14:05,15:13,68
Abhishek Rathi,abhishek.rathi@verveadvisory.com,10:21,18:33,1.3:Opera House,15:13,15:18,4
Abhishek Rathi,abhishek.rathi@verveadvisory.com,10:21,18:33,1.1:It's Accrual World,15:32,18:16,163
```

### Key Features
- **No 0-minute entries** - Transition artifacts filtered out
- **Same-room visits merged** - Consecutive visits combined
- **Main times from webhooks** - Accurate meeting join/leave
- **Room times from SDK** - 30-second polling precision

---

## Environment Variables (Cloud Run)

### Required
```
ZOOM_CLIENT_ID=TqtBGqTAS3W1Jgf9a41w
ZOOM_CLIENT_SECRET=<secret>
ZOOM_WEBHOOK_SECRET=<secret>
ZOOM_ACCOUNT_ID=xhKbAsmnSM6pNYYYurmqIA
GCP_PROJECT_ID=variant-finance-data-project
SENDGRID_API_KEY=<key>
REPORT_EMAIL_TO=scout@verveadvisory.com;devendra.mandhana@verveadvisory.com
```

### Optional
```
SCOUT_BOT_NAME=Scout Bot
BQ_DATASET=breakout_room_calibrator
REPORT_EMAIL_FROM=reports@verveadvisory.com
```

---

## Files Structure

```
zoom+tracker/
├── app.py                    # Flask server (webhooks, monitoring, reports)
├── report_generator.py       # Daily CSV generation from snapshots
├── requirements.txt          # Python dependencies
├── Dockerfile               # Cloud Run deployment
├── CLAUDE.md                # Claude Code instructions
├── README.md                # This file
├── breakout-calibrator/     # React app (Zoom SDK)
│   ├── src/
│   │   ├── components/
│   │   │   ├── MonitorPanel.jsx      # SDK polling UI
│   │   │   ├── CalibrationPanel.jsx  # Legacy calibration UI
│   │   │   └── StatusMessage.jsx
│   │   ├── hooks/
│   │   │   └── useZoomSdk.js         # Zoom SDK methods
│   │   ├── services/
│   │   │   ├── apiService.js
│   │   │   └── zoomService.js
│   │   └── App.jsx
│   └── package.json
└── vm-setup/
    ├── setup_scout_bot.ps1   # PowerShell setup script
    └── quick_setup.bat       # Batch setup script
```

---

## Fixed Room Sequence (66 Rooms)

```
Floor 1 (1.1 - 1.34)
--------------------
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

Floor 2
-------
2.0:Vridam - Wellness Meeting Lounge

Floor 3 (Cloud Teams)
---------------------
3.1:Cloud Gunners
3.2:Cloud Knights
3.3:Cloud Avengers
3.4:Cloud Falcons
3.5:Cloud Titans
3.6:Cloud Guardians
3.7:Inspiration Lounge/Meeting Room
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
5.2:Accurest - Meeting Room: Strategist
5.3:Accurest - Meeting Room: Pioneer
5.4:Accurest - Automation Crafters
5.5:Accurest - Learning/Meeting room
5.6:Accurest - Sales Lounge
5.7:Accurest - Focus Lab
5.8:Accurest - Pattern Inbound
5.9:Accurest - Pattern Planning
5.10:Accurest - Himal's Suite
5.11:Accurest Insight: Team Shubham
5.12:Accurest - Creators
5.13:Accurest - Interview Room

Special Zones
-------------
6.0:Silence Zone
7.0:Masti Ki Pathshala
8.0:BREAK TIME - Tea/Lunch/Dinner
```

---

## Troubleshooting

### Monitor shows "STALE" or "NO_DATA"
- Check if Zoom App is open on Scout Bot VM
- Verify Scout Bot is host/co-host
- Reconnect via RDP and click the app again

### Report shows empty Room History
- Ensure monitoring started at meeting start
- Check `/monitor/sample` for data
- Verify `room_snapshots` table has data

### VM not joining meeting
- Check scheduled task: `schtasks /query /tn "ScoutBot-JoinMeeting"`
- Verify Zoom is installed and logged in
- Check `C:\ScoutBot\join_meeting.bat` exists

### App not visible in Zoom
- Add Scout Bot's email to Test Users in Zoom Marketplace
- Or use direct install URL: `https://marketplace.zoom.us/apps/RRYgo_e2QE697_mxkp3tzg`

### DNS/Connection errors
- Both URLs work: try the alternate URL
- Check internet connectivity

---

## Version History

| Rev | Date | Changes |
|-----|------|---------|
| 124 | 2026-04-01 | Fixed TIMESTAMP cast for webhook times |
| 123 | 2026-04-01 | Added webhook_times CTE back |
| 122 | 2026-04-01 | Added Main_Joined/Left_IST from webhooks |
| 121 | 2026-04-01 | Filter 0-duration entries, re-merge same-room visits |
| 120 | 2026-04-01 | Changed to one row per room visit format |
| 119 | 2026-04-01 | Report uses webhooks + snapshots combined |
| 118 | 2026-04-01 | SDK monitoring mode, simplified report |
| 117 | 2026-04-01 | Fixed participant count (use name when email empty) |
| 116 | 2026-04-01 | Added room_snapshots table, MonitorPanel component |
| 111 | 2026-03-26 | Fixed room sequence calibration |
| 105 | 2026-03-25 | Resume calibration feature |

---

## Cost Estimates (GCP)

### Monthly Breakdown
| Service | Cost | Notes |
|---------|------|-------|
| Cloud Run | ~$50-80/month | With min-instances=1 (always warm) |
| Cloud Build | ~$5-10/month | Depends on deployment frequency |
| Scout Bot VM | ~$27/month | e2-medium (2 vCPU, 4GB), 24/7 |
| BigQuery | ~$1-5/month | Storage + queries |
| Container Scanning | ~$5/month | Automatic security scans |
| **Total** | **~$90-130/month** | |

### Cost Optimization Tips
1. **Reduce Cloud Run cost** - Set min-instances=0 (first request slower):
   ```bash
   gcloud run services update breakout-room-calibrator --region us-central1 --min-instances=0
   ```

2. **Stop VM after hours** - Save ~60% on VM costs:
   ```bash
   # Stop VM (e.g., 7 PM IST)
   gcloud compute instances stop scout-bot-vm --zone=asia-south1-a

   # Start VM (e.g., 9 AM IST)
   gcloud compute instances start scout-bot-vm --zone=asia-south1-a
   ```

3. **Reduce deployments** - Each deployment costs ~$0.10-0.20 in Cloud Build

### Check Current Billing
```
https://console.cloud.google.com/billing
```

---

## Critical Knowledge

1. **SDK provides room names** - No calibration needed!
2. **Emails often empty** - SDK limitation, use participant_name
3. **Bot must join at meeting start** - For accurate timing
4. **VM runs independently** - HR can disconnect after opening app
5. **IST dates** - All event_date stored in IST (UTC+5:30)
6. **Two URLs work** - Primary and alternate Cloud Run URLs both valid
7. **Camera webhooks don't exist** - Use Dashboard QoS API instead
