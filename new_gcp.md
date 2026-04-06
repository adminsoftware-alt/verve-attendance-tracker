# GCP Migration Guide - Full Stack on Google Cloud

## Overview

This guide covers migrating the Zoom Breakout Room Tracker from the current multi-platform setup (Vercel + Supabase + GCP) to a fully GCP-based architecture.

---

## Current Architecture

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Vercel         │────▶│  Cloud Run      │────▶│  BigQuery       │
│  (Frontend)     │     │  (API)          │     │  (Analytics)    │
│  vercel.app     │     │  variant-finance│     │  5 tables       │
└─────────────────┘     └─────────────────┘     └─────────────────┘
        │
        ▼
┌─────────────────┐
│  Supabase       │
│  (Login/Upload) │
│  PostgreSQL     │
└─────────────────┘
```

### Current Services

| Component | Platform | Details |
|-----------|----------|---------|
| Frontend UI | Vercel | `verve-attendance-tracker.vercel.app` |
| Backend API | GCP Cloud Run | `breakout-room-calibrator-1041741270489.us-central1.run.app` |
| Analytics DB | GCP BigQuery | `breakout_room_calibrator` dataset |
| User Auth | Supabase | `app_users` table |
| File Storage | Supabase | `attendance_days` JSON uploads |
| Scout Bot | GCP Compute Engine | Windows VM `34.47.178.82` |
| Scheduled Jobs | GCP Cloud Scheduler | QoS + Report jobs |

---

## Target Architecture (Full GCP)

```
┌─────────────────────────────────────────────────────────────────┐
│                      NEW GCP PROJECT                             │
│                                                                  │
│  ┌─────────────────┐     ┌─────────────────┐                    │
│  │  Cloud Run      │     │  Cloud Run      │                    │
│  │  (Frontend)     │────▶│  (API)          │                    │
│  │  React/Vite     │     │  Flask Backend  │                    │
│  └─────────────────┘     └─────────────────┘                    │
│           │                      │                               │
│           │                      ▼                               │
│           │              ┌─────────────────┐                    │
│           │              │  BigQuery       │                    │
│           │              │  (All Analytics)│                    │
│           │              └─────────────────┘                    │
│           │                                                      │
│           ▼                                                      │
│  ┌─────────────────┐     ┌─────────────────┐                    │
│  │  Cloud SQL      │     │  Cloud Storage  │                    │
│  │  (PostgreSQL)   │     │  (File Uploads) │                    │
│  │  Users + Auth   │     │                 │                    │
│  └─────────────────┘     └─────────────────┘                    │
│                                                                  │
│  ┌─────────────────┐     ┌─────────────────┐                    │
│  │  Compute Engine │     │  Cloud Scheduler│                    │
│  │  (Scout Bot VM) │     │  (Cron Jobs)    │                    │
│  └─────────────────┘     └─────────────────┘                    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## GCP Service Alternatives

### 1. Frontend Hosting (Vercel Replacement)

| Option | Best For | Cost | Complexity |
|--------|----------|------|------------|
| **Cloud Run** | React/Vite apps | ~$0-5/mo | Low |
| Firebase Hosting | Static sites | Free tier | Low |
| Cloud Storage + CDN | Pure static HTML/JS | ~$1/mo | Medium |
| App Engine | Full web apps | ~$5-20/mo | Medium |

**Recommendation: Cloud Run** - Same service as API, easy to manage, auto-scaling.

### 2. Database (Supabase Replacement)

| Option | Best For | Cost | Features |
|--------|----------|------|----------|
| **Cloud SQL (PostgreSQL)** | Relational data | ~$10-30/mo | Full Postgres, familiar SQL |
| Firestore | NoSQL, real-time | Free tier + usage | Auto-scaling, no SQL |
| BigQuery | Analytics only | ~$1-5/mo | Already using |
| Cloud Spanner | Global scale | ~$65+/mo | Overkill for this use case |

**Recommendation: Cloud SQL (PostgreSQL)** - Direct Supabase replacement, same SQL syntax.

### 3. Authentication (Supabase Auth Replacement)

| Option | Best For | Cost |
|--------|----------|------|
| Firebase Auth | OAuth, social login | Free tier |
| Identity Platform | Enterprise SSO | Free tier + usage |
| **Custom (Cloud SQL)** | Simple user table | Included in Cloud SQL |

**Recommendation: Custom auth with Cloud SQL** - Current app uses simple username/password, no need for complex auth.

### 4. File Storage (Supabase Storage Replacement)

| Option | Best For | Cost |
|--------|----------|------|
| **Cloud Storage** | All file uploads | ~$0.02/GB/mo |

---

## Cost Comparison

| Service | Current Cost | GCP-Only Cost |
|---------|--------------|---------------|
| Vercel | Free (trial) → $20/mo | $0 (Cloud Run) |
| Supabase | Free (trial) → $25/mo | $10-15 (Cloud SQL) |
| Cloud Run API | $50-80/mo | $50-80/mo |
| BigQuery | $1-5/mo | $1-5/mo |
| Scout Bot VM | $27/mo | $27/mo |
| Cloud Scheduler | $0.10/mo | $0.10/mo |
| Cloud Storage | - | $1/mo |
| **Total** | **$100-150/mo** | **$90-130/mo** |

**Savings: ~$20-45/mo** + no trial expiration concerns

---

## Migration Steps

### Phase 1: Preparation (Before Migration)

#### 1.1 Export BigQuery Data

```bash
# Set current project
gcloud config set project variant-finance-data-project

# Create export bucket (if needed)
gsutil mb -l us-central1 gs://zoom-tracker-export-temp/

# Export all tables
bq extract --destination_format=CSV \
  breakout_room_calibrator.room_snapshots \
  gs://zoom-tracker-export-temp/room_snapshots_*.csv

bq extract --destination_format=CSV \
  breakout_room_calibrator.participant_events \
  gs://zoom-tracker-export-temp/participant_events_*.csv

bq extract --destination_format=CSV \
  breakout_room_calibrator.teams \
  gs://zoom-tracker-export-temp/teams_*.csv

bq extract --destination_format=CSV \
  breakout_room_calibrator.team_members \
  gs://zoom-tracker-export-temp/team_members_*.csv

bq extract --destination_format=CSV \
  breakout_room_calibrator.room_mappings \
  gs://zoom-tracker-export-temp/room_mappings_*.csv

bq extract --destination_format=CSV \
  breakout_room_calibrator.qos_data \
  gs://zoom-tracker-export-temp/qos_data_*.csv
```

#### 1.2 Export Supabase Data

```sql
-- Run in Supabase SQL Editor, then export results as CSV
SELECT * FROM app_users;
SELECT * FROM attendance_days;
```

#### 1.3 Document Environment Variables

```bash
# Get current Cloud Run env vars
gcloud run services describe breakout-room-calibrator \
  --region us-central1 \
  --format='yaml(spec.template.spec.containers[0].env)'
```

**Required variables to save:**
- `ZOOM_CLIENT_ID`
- `ZOOM_CLIENT_SECRET`
- `ZOOM_WEBHOOK_SECRET`
- `ZOOM_ACCOUNT_ID`
- `SENDGRID_API_KEY`
- `REPORT_EMAIL_TO`
- `REPORT_EMAIL_FROM`

---

### Phase 2: New GCP Project Setup

#### 2.1 Create Project

```bash
# Create new project
gcloud projects create NEW_PROJECT_ID --name="Zoom Attendance Tracker"

# Set as active project
gcloud config set project NEW_PROJECT_ID

# Link billing account (required)
gcloud billing projects link NEW_PROJECT_ID --billing-account=BILLING_ACCOUNT_ID
```

#### 2.2 Enable Required APIs

```bash
gcloud services enable \
  run.googleapis.com \
  bigquery.googleapis.com \
  cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com \
  compute.googleapis.com \
  sqladmin.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com
```

#### 2.3 Create BigQuery Dataset

```bash
# Create dataset
bq mk --dataset --location=US NEW_PROJECT_ID:breakout_room_calibrator

# Create tables (schemas will be inferred from import, or create manually)
```

#### 2.4 Import BigQuery Data

```bash
# Copy export bucket to new project (or use same bucket if accessible)
gsutil cp -r gs://zoom-tracker-export-temp/* gs://NEW_PROJECT_BUCKET/import/

# Import tables
bq load --source_format=CSV --autodetect \
  breakout_room_calibrator.room_snapshots \
  gs://NEW_PROJECT_BUCKET/import/room_snapshots_*.csv

bq load --source_format=CSV --autodetect \
  breakout_room_calibrator.participant_events \
  gs://NEW_PROJECT_BUCKET/import/participant_events_*.csv

bq load --source_format=CSV --autodetect \
  breakout_room_calibrator.teams \
  gs://NEW_PROJECT_BUCKET/import/teams_*.csv

bq load --source_format=CSV --autodetect \
  breakout_room_calibrator.team_members \
  gs://NEW_PROJECT_BUCKET/import/team_members_*.csv
```

---

### Phase 3: Setup Cloud SQL (PostgreSQL)

#### 3.1 Create Instance

```bash
# Create PostgreSQL instance
gcloud sql instances create attendance-db \
  --database-version=POSTGRES_15 \
  --tier=db-f1-micro \
  --region=us-central1 \
  --root-password=YOUR_SECURE_PASSWORD \
  --storage-size=10GB \
  --storage-type=SSD

# Create database
gcloud sql databases create attendance --instance=attendance-db
```

#### 3.2 Create Tables

Connect to Cloud SQL and run:

```sql
-- Users table (from Supabase)
CREATE TABLE app_users (
  id SERIAL PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password TEXT NOT NULL,
  name TEXT NOT NULL,
  role TEXT DEFAULT 'user',
  email TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Default users
INSERT INTO app_users (username, password, name, role) VALUES
  ('admin', 'verve2026', 'Admin', 'admin'),
  ('hr1', 'verve2026', 'HR User 1', 'user'),
  ('hr2', 'verve2026', 'HR User 2', 'user'),
  ('manager1', 'verve2026', 'Manager 1', 'manager'),
  ('manager2', 'verve2026', 'Manager 2', 'manager');

-- Attendance uploads (from Supabase)
CREATE TABLE attendance_days (
  id SERIAL PRIMARY KEY,
  report_date DATE UNIQUE NOT NULL,
  employees JSONB NOT NULL,
  uploaded_by TEXT,
  uploaded_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fast lookups
CREATE INDEX idx_attendance_date ON attendance_days(report_date);
```

#### 3.3 Setup Connection

```bash
# Get connection name
gcloud sql instances describe attendance-db --format='value(connectionName)'
# Output: NEW_PROJECT_ID:us-central1:attendance-db

# For Cloud Run, use Cloud SQL connector (no public IP needed)
```

---

### Phase 4: Deploy Backend API (Cloud Run)

#### 4.1 Update app.py Configuration

```python
# Add Cloud SQL support to app.py
import os

# Configuration
GCP_PROJECT_ID = os.environ.get('GCP_PROJECT_ID', 'NEW_PROJECT_ID')
BQ_DATASET = os.environ.get('BQ_DATASET', 'breakout_room_calibrator')

# Cloud SQL connection (add if using Cloud SQL for users)
CLOUD_SQL_CONNECTION = os.environ.get('CLOUD_SQL_CONNECTION', '')
DB_USER = os.environ.get('DB_USER', 'postgres')
DB_PASS = os.environ.get('DB_PASS', '')
DB_NAME = os.environ.get('DB_NAME', 'attendance')
```

#### 4.2 Deploy API

```bash
cd /path/to/zoom+tracker

# Build React app first
cd breakout-calibrator && npm run build && cd ..

# Deploy to Cloud Run
gcloud run deploy breakout-room-calibrator \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --min-instances=1 \
  --set-env-vars="GCP_PROJECT_ID=NEW_PROJECT_ID" \
  --set-env-vars="ZOOM_CLIENT_ID=xxx" \
  --set-env-vars="ZOOM_CLIENT_SECRET=xxx" \
  --set-env-vars="ZOOM_WEBHOOK_SECRET=xxx" \
  --set-env-vars="ZOOM_ACCOUNT_ID=xhKbAsmnSM6pNYYYurmqIA" \
  --set-env-vars="SENDGRID_API_KEY=xxx" \
  --set-env-vars="REPORT_EMAIL_TO=xxx" \
  --add-cloudsql-instances=NEW_PROJECT_ID:us-central1:attendance-db
```

**Note the new Cloud Run URL** - you'll need this for the next steps.

---

### Phase 5: Deploy Frontend (Cloud Run)

#### 5.1 Create Frontend Dockerfile

Create `attedance_manager/Dockerfile`:

```dockerfile
# Build stage
FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .

# Update API URL before build
ARG API_URL
ENV VITE_API_URL=$API_URL

RUN npm run build

# Production stage
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/nginx.conf
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
```

#### 5.2 Create nginx.conf

Create `attedance_manager/nginx.conf`:

```nginx
events {
    worker_connections 1024;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    server {
        listen 8080;
        server_name _;
        root /usr/share/nginx/html;
        index index.html;

        # SPA routing - serve index.html for all routes
        location / {
            try_files $uri $uri/ /index.html;
        }

        # Cache static assets
        location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2)$ {
            expires 1y;
            add_header Cache-Control "public, immutable";
        }

        # Gzip
        gzip on;
        gzip_types text/plain text/css application/json application/javascript text/xml application/xml;
    }
}
```

#### 5.3 Update API URL

Update `attedance_manager/src/utils/zoomApi.js`:

```javascript
// Use environment variable or default to new Cloud Run URL
const ZOOM_API_BASE = import.meta.env.VITE_API_URL || 'https://NEW_CLOUD_RUN_URL';
```

#### 5.4 Deploy Frontend

```bash
cd /path/to/zoom+tracker/attedance_manager

# Deploy to Cloud Run
gcloud run deploy attendance-frontend \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8080 \
  --build-arg API_URL=https://NEW_API_CLOUD_RUN_URL
```

---

### Phase 6: Update External Services

#### 6.1 Update Zoom Webhook URL

1. Go to https://marketplace.zoom.us/develop/apps
2. Select your app → Features → Event Subscriptions
3. Update Event notification endpoint URL to: `https://NEW_API_CLOUD_RUN_URL/webhook`
4. Save changes

#### 6.2 Verify Webhook

```bash
# Test webhook endpoint
curl -X POST https://NEW_API_CLOUD_RUN_URL/webhook \
  -H "Content-Type: application/json" \
  -d '{"event": "endpoint.url_validation", "payload": {"plainToken": "test"}}'
```

---

### Phase 7: Setup Cloud Scheduler

```bash
# Daily QoS collection (9:30 AM IST)
gcloud scheduler jobs create http daily-qos-collection \
  --location=us-central1 \
  --schedule="30 9 * * *" \
  --uri="https://NEW_API_CLOUD_RUN_URL/qos/scheduled" \
  --http-method=POST \
  --time-zone="Asia/Kolkata" \
  --attempt-deadline=540s

# Daily attendance report (11:15 AM IST)
gcloud scheduler jobs create http daily-attendance-report \
  --location=us-central1 \
  --schedule="15 11 * * *" \
  --uri="https://NEW_API_CLOUD_RUN_URL/report/generate" \
  --http-method=POST \
  --time-zone="Asia/Kolkata" \
  --attempt-deadline=540s
```

---

### Phase 8: Migrate Scout Bot VM

#### Option A: Create New VM (Recommended)

```bash
# Create Windows VM
gcloud compute instances create scout-bot \
  --zone=asia-south1-a \
  --machine-type=e2-medium \
  --image-family=windows-2022 \
  --image-project=windows-cloud \
  --boot-disk-size=50GB \
  --boot-disk-type=pd-ssd

# Set Windows password
gcloud compute reset-windows-password scout-bot --zone=asia-south1-a --user=dataapps
```

Then:
1. RDP into the VM
2. Install Zoom Desktop Client
3. Sign in as Scout Bot user
4. Create scheduled task for auto-join

#### Option B: Move Existing VM

```bash
# In OLD project: Create snapshot
gcloud compute disks snapshot scout-bot \
  --zone=asia-south1-a \
  --snapshot-names=scout-bot-migration-snapshot \
  --project=variant-finance-data-project

# In NEW project: Create disk from snapshot
gcloud compute disks create scout-bot-disk \
  --source-snapshot=projects/variant-finance-data-project/global/snapshots/scout-bot-migration-snapshot \
  --zone=asia-south1-a

# Create VM with existing disk
gcloud compute instances create scout-bot \
  --zone=asia-south1-a \
  --machine-type=e2-medium \
  --disk=name=scout-bot-disk,boot=yes
```

---

### Phase 9: Setup CI/CD with Cloud Build (Optional)

#### 9.1 Create cloudbuild.yaml for API

```yaml
# cloudbuild.yaml (in zoom+tracker root)
steps:
  # Build React app
  - name: 'node:20'
    dir: 'breakout-calibrator'
    entrypoint: 'npm'
    args: ['ci']
  - name: 'node:20'
    dir: 'breakout-calibrator'
    entrypoint: 'npm'
    args: ['run', 'build']

  # Deploy to Cloud Run
  - name: 'gcr.io/cloud-builders/gcloud'
    args:
      - 'run'
      - 'deploy'
      - 'breakout-room-calibrator'
      - '--source=.'
      - '--region=us-central1'
      - '--allow-unauthenticated'
      - '--min-instances=1'
```

#### 9.2 Setup GitHub Trigger

```bash
# Connect GitHub repo
gcloud builds triggers create github \
  --repo-name=REPO_NAME \
  --repo-owner=GITHUB_USERNAME \
  --branch-pattern='^main$' \
  --build-config=cloudbuild.yaml
```

---

## Post-Migration Checklist

| Step | Task | Status |
|------|------|--------|
| 1 | BigQuery data imported | ☐ |
| 2 | Cloud SQL created with users | ☐ |
| 3 | Backend API deployed | ☐ |
| 4 | Frontend deployed | ☐ |
| 5 | Zoom webhook URL updated | ☐ |
| 6 | Cloud Scheduler jobs created | ☐ |
| 7 | Scout Bot VM migrated | ☐ |
| 8 | Test login functionality | ☐ |
| 9 | Test live dashboard | ☐ |
| 10 | Test team management | ☐ |
| 11 | Test report generation | ☐ |
| 12 | Verify webhook events | ☐ |
| 13 | Delete old resources (after validation) | ☐ |

---

## New URLs (After Migration)

| Service | URL |
|---------|-----|
| Frontend | `https://attendance-frontend-HASH.us-central1.run.app` |
| Backend API | `https://breakout-room-calibrator-HASH.us-central1.run.app` |
| Custom Domain (optional) | `https://attendance.yourdomain.com` |

---

## Environment Variables Reference

### Backend API (Cloud Run)

| Variable | Description |
|----------|-------------|
| `GCP_PROJECT_ID` | New GCP project ID |
| `BQ_DATASET` | `breakout_room_calibrator` |
| `ZOOM_CLIENT_ID` | Zoom Server-to-Server app client ID |
| `ZOOM_CLIENT_SECRET` | Zoom Server-to-Server app client secret |
| `ZOOM_WEBHOOK_SECRET` | Zoom webhook validation secret |
| `ZOOM_ACCOUNT_ID` | `xhKbAsmnSM6pNYYYurmqIA` |
| `SENDGRID_API_KEY` | SendGrid API key for emails |
| `REPORT_EMAIL_TO` | Report recipients |
| `REPORT_EMAIL_FROM` | Sender email |
| `CLOUD_SQL_CONNECTION` | `PROJECT:REGION:INSTANCE` |
| `DB_USER` | `postgres` |
| `DB_PASS` | Database password |
| `DB_NAME` | `attendance` |

### Frontend (Cloud Run)

| Variable | Description |
|----------|-------------|
| `VITE_API_URL` | Backend API URL |

---

## Rollback Plan

If migration fails:

1. **Vercel**: Still active, just redeploy from GitHub
2. **Supabase**: Data still exists, reconnect
3. **Old Cloud Run**: Don't delete until new one is verified
4. **BigQuery**: Export is preserved in Cloud Storage

---

## Estimated Timeline

| Phase | Task | Duration |
|-------|------|----------|
| 1 | Preparation & Export | 30 min |
| 2 | New Project Setup | 30 min |
| 3 | Cloud SQL Setup | 30 min |
| 4 | Backend Deployment | 15 min |
| 5 | Frontend Deployment | 15 min |
| 6 | External Services Update | 15 min |
| 7 | Cloud Scheduler | 10 min |
| 8 | Scout Bot Migration | 45-60 min |
| 9 | Testing | 30 min |
| **Total** | | **~4 hours** |

---

## Support Contacts

- GCP Support: https://cloud.google.com/support
- Zoom Developer Support: https://devforum.zoom.us/
- SendGrid Support: https://support.sendgrid.com/
