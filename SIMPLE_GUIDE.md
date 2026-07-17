# Zoom Attendance Tracker - Simple Guide

## What This System Does

**One sentence:** Tracks who attended Zoom meetings and for how long.

---

## How It Works (3 Simple Steps)

```
ZOOM MEETING → DATA CAPTURED → TEAM VIEW SHOWS HOURS
```

### Step 1: Data Capture
- A bot (Scout Bot VM) joins the Zoom meeting automatically at 9:30 AM
- Every 30 seconds, it records: "Who is in which room right now?"
- This data goes to BigQuery (Google's database)

### Step 2: Data Processing
- Raw data = thousands of "snapshots" (who was where at each moment)
- System converts snapshots into "intervals" (John was in Room A from 10:00 to 11:30)
- Calculates total hours per person

### Step 3: View in Frontend
- Open: `https://attendance-frontend-4e5na4tdha-uc.a.run.app`
- Login with your dashboard username/password (ask an admin if you don't have one)
- Go to Team View → See hours for each person
- Hours marked ⚠ are estimated from webhook data (bot wasn't running)

---

## Where Things Live

| What | Where | URL |
|------|-------|-----|
| **Frontend** (what you see) | Google Cloud Run | `attendance-frontend-4e5na4tdha-uc.a.run.app` |
| **Backend** (API server) | Google Cloud Run | `breakout-room-calibrator-4e5na4tdha-uc.a.run.app` |
| **Database** | Google BigQuery | Project: `verve-attendance-tracker` |
| **Bot VM** | Google Compute Engine | `scout-bot-2` (us-central1-a) |

---

## Key Tables (Database)

| Table | What It Stores |
|-------|----------------|
| `room_snapshots_v2` | Raw data - who was in which room every 30 seconds |
| `presence_intervals` | Processed data - attendance intervals with durations |
| `teams` | List of teams |
| `team_members` | Who belongs to which team |

---

## How Hours Are Calculated

```
Raw: "John in Room A at 10:00:00, 10:00:30, 10:01:00, 10:01:30..."
     ↓
Processed: "John was in Room A from 10:00 to 10:02 = 2 minutes"
     ↓
Team View: "John - Total: 8 hours 30 minutes"
```

---

## Automatic Updates

| When | What Happens |
|------|--------------|
| Every 2 minutes (8 AM - midnight) | Rebuilds today's data |
| Every 15 minutes | Self-heals any of the last 35 days (missing/stale/late data) |
| When you open Team View | Auto-fixes if data is stale (backstop) |

---

## Common Commands

**Check if system is healthy:**
```
curl https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/health
```

**Manually refresh a date:**
```
curl -X POST https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app/intervals/rebuild -H "Content-Type: application/json" -d '{"date": "2026-06-05"}'
```

---

## If Something Goes Wrong

| Problem | Solution |
|---------|----------|
| Hours look wrong | Rebuild the date (command above) |
| Data not updating | Check if Scout Bot VM is running |
| Team View slow | Wait ~20 seconds, it's rebuilding data |

---

## That's It!

- **Data comes from:** Zoom SDK polling every 30 seconds
- **Data lives in:** BigQuery tables
- **You see it in:** Team View frontend
- **It updates:** Automatically every hour

Questions? Check the full guide: `SYSTEM_ARCHITECTURE.md`
