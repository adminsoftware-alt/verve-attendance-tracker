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
| **Frontend (UI)** | `https://attendance-frontend-1073587167150.us-central1.run.app` |
| **Backend API** | `https://breakout-room-calibrator-1073587167150.us-central1.run.app` |
| **Zoom App Home** | `https://breakout-room-calibrator-1073587167150.us-central1.run.app/app` |

### IDs & Credentials
| Item | Value |
|------|-------|
| GCP Project ID | `verve-attendance-tracker` |
| GCP Project Number | `1073587167150` |
| BigQuery Dataset | `breakout_room_calibrator` |
| GitHub Repo | `adminsoftware-alt/verve-attendance-tracker` |
| Zoom Account ID | `xhKbAsmnSM6pNYYYurmqIA` |
| Zoom App ID | `RRYgo_e2QE697_mxkp3tzg` |
| Meeting ID | `9034027764` |
| Current Revision | **130** |

### Login Credentials (BigQuery app_users table)
| Username | Password | Role |
|----------|----------|------|
| admin | verve2026 | admin |
| hr1 | verve2026 | user |
| hr2 | verve2026 | user |
| manager1 | verve2026 | manager |
| manager2 | verve2026 | manager |

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
│                Cloud Run Services (verve-attendance-tracker)     │
│                                                                  │
│  attendance-frontend        breakout-room-calibrator            │
│  (React + Vite)             (Flask + React SDK App)             │
│  - Login UI                 - /monitor/* (SDK polling)          │
│  - Team Management          - /teams/* (Teams API)              │
│  - Reports Dashboard        - /auth/* (BigQuery auth)           │
│                             - /data/* (Attendance data)         │
└─────────────────────────────────────────────────────────────────┘
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
    ┌──────────────┐ ┌──────────┐  ┌──────────────┐
    │   BigQuery   │ │ Zoom SDK │  │  SendGrid    │
    │  11 tables   │ │ Polling  │  │ Email CSV    │
    └──────────────┘ └──────────┘  └──────────────┘
```

---

## Files Structure

```
zoom+tracker/
├── app.py                    # Flask server (all endpoints)
├── report_generator.py       # Daily CSV generation
├── requirements.txt          # Python dependencies
├── Dockerfile               # Backend Cloud Run deployment
├── cloudbuild.yaml          # Auto-deploy config (GitHub trigger)
├── CLAUDE.md                # Claude Code instructions
├── README.md                # This file
├── breakout-calibrator/     # React app (Zoom SDK) - served at /app
│   ├── src/
│   │   ├── components/
│   │   │   ├── MonitorPanel.jsx      # SDK polling UI
│   │   │   ├── CalibrationPanel.jsx  # Legacy calibration
│   │   │   └── StatusMessage.jsx
│   │   ├── hooks/
│   │   │   └── useZoomSdk.js         # Zoom SDK methods
│   │   └── services/
│   │       └── apiService.js
│   └── package.json
├── attedance_manager/       # React frontend (attendance UI)
│   ├── src/
│   │   ├── components/      # Login, Dashboard, Team views
│   │   └── utils/
│   │       ├── storage.js   # Auth & data API calls
│   │       └── zoomApi.js   # Zoom tracker API calls
│   ├── Dockerfile           # Frontend Cloud Run deployment
│   ├── nginx.conf          # SPA routing config
│   └── package.json
└── vm-setup/
    ├── setup_scout_bot.ps1   # PowerShell setup script
    └── quick_setup.bat       # Batch setup script
```

---

## BigQuery Tables

### Dataset: `verve-attendance-tracker.breakout_room_calibrator`

| Table | Purpose |
|-------|---------|
| `room_snapshots` | **PRIMARY** - SDK polling data (every 30s) |
| `participant_events` | Webhook join/leave events |
| `room_mappings` | UUID -> room name (legacy calibration) |
| `qos_data` | Dashboard QoS metrics |
| `camera_events` | Camera ON/OFF events |
| `teams` | Team definitions |
| `team_members` | Team membership |
| `team_tags` | Custom team metadata (department, project, etc.) |
| `team_leave_records` | Leave/absence tracking |
| `app_users` | Login credentials |
| `attendance_reports` | Uploaded attendance data (from frontend) |

---

## API Endpoints

### Auth (BigQuery-based)
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/auth/login` | POST | Validate username/password |
| `/auth/users` | GET | List all users |

### Attendance Data
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/data/attendance` | GET | Get all attendance data |
| `/data/attendance/<date>` | GET | Get specific date |
| `/data/attendance` | POST | Save attendance data |
| `/data/attendance/<date>` | DELETE | Delete attendance data |
| `/data/attendance/dates` | GET | List dates with data |

### Monitor Mode (SDK Polling)
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/monitor/snapshot` | POST | Receive SDK polling data |
| `/monitor/status` | GET | Check snapshot counts |
| `/monitor/health` | GET | Check if monitoring active |

### Live Attendance API
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/attendance/live?date=YYYY-MM-DD` | GET | Who's in which room NOW |
| `/attendance/summary/<date>` | GET | Full attendance with room visits |
| `/attendance/heatmap/<date>?interval=15` | GET | Room occupancy per time slot |

### Teams Management
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/teams` | GET | List all teams |
| `/teams` | POST | Create team |
| `/teams/<id>` | GET/PUT/DELETE | Team CRUD |
| `/teams/<id>/members` | POST | Add member (with duplicate check) |
| `/teams/<id>/members/<mid>` | DELETE | Remove member |
| `/teams/participants?days=90` | GET | List known participants (for adding) |

### Team Attendance
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/teams/<id>/attendance/<date>` | GET | Daily team attendance with break/isolation |
| `/teams/<id>/attendance-with-leave/<date>` | GET | Attendance with leave status |
| `/teams/<id>/report/monthly` | GET | Monthly report (JSON or CSV) |
| `/teams/<id>/trends?months=6` | GET | Historical trends (month-over-month) |

### Leave Management
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/teams/<id>/leave` | GET | List leave records |
| `/teams/<id>/leave` | POST | Add leave record |
| `/teams/<id>/leave/<leave_id>` | DELETE | Delete leave record |

### Team Tags (Custom Metadata)
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/teams/<id>/tags` | GET | Get team tags |
| `/teams/<id>/tags` | POST/PUT | Set team tags |
| `/teams/<id>/tags/<key>` | DELETE | Delete tag |
| `/teams/by-tag?tag_key=&tag_value=` | GET | Filter teams by tag |

### Reports
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/report/generate` | POST | Generate and email daily CSV |
| `/report/preview/<date>` | GET | Preview report data |

---

## Deployment

### Auto-Deploy (Cloud Build)
Push to `main` branch triggers automatic deployment:
1. Builds React SDK app (`breakout-calibrator`)
2. Deploys backend to Cloud Run
3. Builds React frontend (`attedance_manager`)
4. Deploys frontend to Cloud Run

```bash
# Just push to main - Cloud Build handles the rest!
git add .
git commit -m "Your changes"
git push origin main
```

### Manual Deploy
```bash
# Backend (includes Zoom SDK app)
cd C:\Users\shash\Downloads\zoom+tracker
cd breakout-calibrator && npm run build && cd ..
gcloud.cmd run deploy breakout-room-calibrator --source . --region us-central1 --allow-unauthenticated --min-instances=1 --project=verve-attendance-tracker

# Frontend (attendance manager)
cd attedance_manager
gcloud.cmd run deploy attendance-frontend --source . --region us-central1 --allow-unauthenticated --port 8080 --project=verve-attendance-tracker
```

### View Logs
```bash
gcloud.cmd run services logs read breakout-room-calibrator --region us-central1 --limit 100 --project=verve-attendance-tracker
```

---

## Test Commands

```bash
# Health check
curl "https://breakout-room-calibrator-1073587167150.us-central1.run.app/health"

# Login test
curl -X POST "https://breakout-room-calibrator-1073587167150.us-central1.run.app/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"verve2026"}'

# Generate report
curl -X POST "https://breakout-room-calibrator-1073587167150.us-central1.run.app/report/generate" \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-04-06"}'

# Check monitor
curl "https://breakout-room-calibrator-1073587167150.us-central1.run.app/monitor/health"
```

---

## Scout Bot VM

### Connect via RDP
```
IP: 34.47.178.82
Username: dataapps
Password: ScoutBot2026
```

### Change Scheduled Task Time
```cmd
schtasks /change /tn "ScoutBot-JoinMeeting" /st 09:30
```

---

## Version History

| Rev | Date | Changes |
|-----|------|---------|
| 130 | 2026-04-06 | Bug fixes (duplicate members, break time, 90-day pool) + new features (Historical Trends, Leave Management, Team Tags) |
| 129 | 2026-04-06 | Full GCP migration - new project, frontend on Cloud Run, BigQuery auth |
| 128 | 2026-04-06 | Team management endpoints |
| 126 | 2026-04-03 | Fixed meeting ID extraction |
| 125 | 2026-04-03 | Added JSON attendance API |
| 124 | 2026-04-01 | One row per room visit format |

---

## Cost Estimates (Monthly)

| Service | Cost |
|---------|------|
| Cloud Run (2 services) | ~$60-100 |
| Scout Bot VM | ~$27 |
| Cloud Build | ~$5-10 |
| BigQuery | ~$1-5 |
| **Total** | **~$95-145** |

---

## Critical Knowledge

1. **SDK provides room names** - No calibration needed!
2. **Emails often empty** - SDK limitation, use participant_name
3. **Camera webhooks DON'T exist** - Use Dashboard QoS API
4. **VM runs independently** - HR can disconnect after opening app
5. **IST dates** - All event_date stored in IST (UTC+5:30)
6. **Full GCP stack** - No Vercel, no Supabase
7. **Auth via BigQuery** - app_users table for login
8. **Auto-deploy** - Push to main triggers Cloud Build
