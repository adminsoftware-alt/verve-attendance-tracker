# Zoom Meeting Task Tracker - Solution Document

**Version:** 1.0  
**Date:** June 1, 2026  
**Prepared for:** Verve Internal Use  

---

## Executive Summary

**Goal:** Automatically capture tasks assigned during Zoom meetings (Hinglish conversations), transcribe them, extract actionable items, and track them in Google Sheets for daily follow-up.

**Challenge:** 4 hours of daily meetings = high transcription costs and complex recording requirements.

**Solution:** A lightweight recording system integrated with Sarvam AI (Hinglish transcription), Claude AI (task extraction), and Google Sheets (tracking).

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Solution Options Overview](#2-solution-options-overview)
3. [Option 1: Desktop Recorder App](#3-option-1-desktop-recorder-app)
4. [Option 2: Chrome Extension](#4-option-2-chrome-extension)
5. [Option 3: Zoom Local Recording + Watcher](#5-option-3-zoom-local-recording--watcher)
6. [Option 4: Zoom Cloud Recording + Webhook](#6-option-4-zoom-cloud-recording--webhook)
7. [Option 5: Custom Zoom App with Recording Bot](#7-option-5-custom-zoom-app-with-recording-bot)
8. [Backend Processing Pipeline](#8-backend-processing-pipeline)
9. [Cost Comparison](#9-cost-comparison)
10. [Technology Stack](#10-technology-stack)
11. [Google Sheet Structure](#11-google-sheet-structure)
12. [Implementation Roadmap](#12-implementation-roadmap)
13. [Recommendation](#13-recommendation)

---

## 1. Problem Statement

### Current Situation
- Manager conducts ~4 hours of Zoom meetings daily
- Conversations are in **Hinglish** (Hindi + English mix)
- Tasks are assigned verbally to employees
- No systematic tracking of assigned tasks
- Next-day follow-up is difficult without written records

### Requirements
| Requirement | Priority |
|-------------|----------|
| Record meeting audio | Must Have |
| Transcribe Hinglish accurately | Must Have |
| Extract tasks with assignee names | Must Have |
| Store in Google Sheets | Must Have |
| Minimal manual intervention | Should Have |
| Low operational cost | Should Have |
| Works with existing Zoom setup | Should Have |

### Constraints
- Zoom Business+ plan may not be available (no cloud recording API)
- Recording 4 hours daily = storage and cost considerations
- Hinglish requires specialized transcription (not standard English ASR)

---

## 2. Solution Options Overview

| Option | Recording Method | Install Required | Zoom Plan | Complexity | Cost/Month |
|--------|------------------|------------------|-----------|------------|------------|
| **1. Desktop Recorder App** | System audio capture | Yes (one-time) | Any | Low | ₹5,000 |
| **2. Chrome Extension** | Tab audio capture | Yes (extension) | Any (browser) | Low | ₹5,000 |
| **3. Local Recording + Watcher** | Zoom native local | Yes (script) | Any | Medium | ₹5,000 |
| **4. Cloud Recording + Webhook** | Zoom cloud | No | Business+ | Low | ₹5,500 |
| **5. Custom Recording Bot** | Meeting SDK | No | Any | High | ₹6,000 |

---

## 3. Option 1: Desktop Recorder App

### Overview
A lightweight desktop application installed on HR's computer that captures system audio (whatever plays through speakers/headphones).

### Architecture
```
┌─────────────────────────────────────────────────────────────┐
│                    HR's Computer                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  RECORDER APP (Python/Electron)                       │  │
│  │                                                       │  │
│  │   ┌─────────────┐    ┌─────────────┐                 │  │
│  │   │ 🔴 Start    │    │ ⏹️ Stop     │                 │  │
│  │   │  Recording  │    │  & Upload   │                 │  │
│  │   └─────────────┘    └─────────────┘                 │  │
│  │                                                       │  │
│  │   Status: ● Recording... (01:23:45)                  │  │
│  │                                                       │  │
│  └───────────────────────────────────────────────────────┘  │
│                           │                                  │
│     Captures system audio │ (Zoom meeting audio)            │
│                           ▼                                  │
│              [meeting_2026-06-01.mp3]                       │
└─────────────────────────────┬───────────────────────────────┘
                              │ Upload on Stop
                              ▼
                    ┌─────────────────┐
                    │  GCS Bucket     │
                    │  zoom-meetings/ │
                    └────────┬────────┘
                             │ Triggers
                             ▼
                    ┌─────────────────┐
                    │ Cloud Function  │
                    │ (Processing)    │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌──────────┐  ┌──────────┐  ┌──────────┐
        │ Sarvam   │  │ Claude   │  │ Google   │
        │ AI       │  │ AI       │  │ Sheets   │
        │          │  │          │  │          │
        │Transcribe│  │ Extract  │  │ Write    │
        │ Hinglish │  │ Tasks    │  │ Tasks    │
        └──────────┘  └──────────┘  └──────────┘
```

### How It Works
1. HR opens the recorder app before meeting
2. Clicks "Start Recording" when meeting begins
3. App captures all system audio (including Zoom)
4. HR clicks "Stop & Upload" when meeting ends
5. App compresses audio to MP3 and uploads to GCS
6. Backend pipeline processes automatically

### Technical Implementation

**Python Recorder App (Recommended)**
```python
# Core libraries needed:
# - sounddevice: Capture system audio
# - pydub: Audio processing/compression
# - google-cloud-storage: Upload to GCS
# - tkinter: Simple GUI

# Features:
# - Start/Stop buttons
# - Recording timer display
# - Auto-compression to MP3
# - Direct upload to GCS
# - Filename: meeting_YYYY-MM-DD_HH-MM.mp3
```

**Electron Recorder App (Alternative)**
```javascript
// Uses Web Audio API + desktopCapturer
// Cross-platform (Windows/Mac)
// Larger install size (~80MB)
// Better UI capabilities
```

### Pros & Cons

| Pros | Cons |
|------|------|
| Works with ANY Zoom plan | Requires one-time install |
| Full control over recording | HR must remember to start/stop |
| High quality audio capture | Only captures one computer's audio |
| Simple implementation | Needs system audio permissions |
| No Zoom API dependency | |

### Estimated Build Time
- Python App: 1-2 days
- Electron App: 2-3 days

---

## 4. Option 2: Chrome Extension

### Overview
A Chrome extension that captures audio from the Zoom browser tab. HR must join Zoom meeting via Chrome browser (not desktop app).

### Architecture
```
┌─────────────────────────────────────────────────────────────┐
│                    Chrome Browser                            │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  ZOOM WEB (zoom.us/wc/join)                           │  │
│  │  Meeting in browser tab                               │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  CHROME EXTENSION                                     │  │
│  │  [🔴 Record Tab] [⏹️ Stop]                            │  │
│  │  Uses chrome.tabCapture API                           │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────┬───────────────────────────────┘
                              │ Captured audio
                              ▼
                    ┌─────────────────┐
                    │  GCS Upload     │
                    └────────┬────────┘
                             │
                             ▼
                    [Same processing pipeline]
```

### How It Works
1. HR joins Zoom meeting in Chrome browser
2. Clicks extension icon → "Start Recording"
3. Extension captures tab audio using `chrome.tabCapture`
4. On stop, uploads to GCS
5. Processing pipeline runs

### Technical Implementation
```javascript
// manifest.json permissions:
// - tabCapture
// - storage
// - activeTab

// Uses MediaRecorder API
// Captures only the Zoom tab audio
// No system audio (cleaner recording)
```

### Pros & Cons

| Pros | Cons |
|------|------|
| No desktop app install | Must use Zoom in browser |
| Clean tab-only audio | Browser Zoom has fewer features |
| Easy to distribute | Chrome-only |
| Automatic updates | Tab must stay active |

### Estimated Build Time
- 2-3 days

---

## 5. Option 3: Zoom Local Recording + Watcher

### Overview
Uses Zoom's built-in local recording feature. A background script watches the Zoom recordings folder and auto-uploads new files.

### Architecture
```
┌─────────────────────────────────────────────────────────────┐
│                    HR's Computer                             │
│                                                              │
│  ┌─────────────────────┐    ┌────────────────────────────┐  │
│  │ ZOOM DESKTOP APP    │    │ WATCHER SCRIPT (background)│  │
│  │                     │    │                            │  │
│  │ HR clicks native    │    │ Monitors:                  │  │
│  │ "Record" button     │───▶│ C:\Users\HR\Documents\Zoom │  │
│  │                     │    │                            │  │
│  │ Saves locally when  │    │ On new file:               │  │
│  │ meeting ends        │    │ 1. Extract audio track     │  │
│  └─────────────────────┘    │ 2. Compress to MP3         │  │
│                             │ 3. Upload to GCS           │  │
│                             │ 4. Delete local file       │  │
│                             └─────────────┬──────────────┘  │
└───────────────────────────────────────────┬─────────────────┘
                                            │
                                            ▼
                                  [Processing Pipeline]
```

### How It Works
1. HR uses Zoom's normal "Record" button
2. Zoom saves MP4 to local folder when meeting ends
3. Watcher script detects new recording
4. Extracts audio, compresses, uploads
5. Optionally deletes local file to save space

### Technical Implementation
```python
# Watcher Script (runs as Windows service)
# Uses: watchdog, ffmpeg, google-cloud-storage

# Watches: C:\Users\{user}\Documents\Zoom\
# Triggers on: *.mp4 file creation
# Extracts: audio track only (smaller file)
# Uploads: to GCS bucket
```

### Pros & Cons

| Pros | Cons |
|------|------|
| Uses Zoom's own recording (best quality) | Needs background script |
| HR uses familiar Zoom interface | Slight delay (process after meeting) |
| Works offline (uploads later) | Local storage used temporarily |
| Any Zoom plan | Script must run continuously |

### Estimated Build Time
- 1 day

---

## 6. Option 4: Zoom Cloud Recording + Webhook

### Overview
Uses Zoom's cloud recording feature with webhook notification. Fully automated, no HR intervention needed.

### Requirements
- **Zoom Business+ plan** (required for cloud recording API)
- Webhook endpoint configured in Zoom Marketplace

### Architecture
```
┌─────────────────────────────────────────────────────────────┐
│                    ZOOM MEETING                              │
│                                                              │
│  Cloud recording enabled for account                        │
│  Automatically records all meetings                         │
└─────────────────────────────┬───────────────────────────────┘
                              │ Meeting ends
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    ZOOM CLOUD                                │
│                                                              │
│  Processes recording (5-30 min delay)                       │
│  Sends webhook: recording.completed                         │
└─────────────────────────────┬───────────────────────────────┘
                              │ Webhook
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  YOUR BACKEND (app.py)                                       │
│                                                              │
│  1. Receive webhook                                         │
│  2. Download recording via Zoom API                         │
│  3. Extract audio                                           │
│  4. Upload to GCS                                           │
│  5. Trigger processing pipeline                             │
└─────────────────────────────────────────────────────────────┘
```

### Webhook Payload Example
```json
{
  "event": "recording.completed",
  "payload": {
    "meeting": {
      "id": "9034027764",
      "topic": "Daily Standup"
    },
    "recording_files": [
      {
        "file_type": "M4A",
        "download_url": "https://zoom.us/rec/download/..."
      }
    ]
  }
}
```

### Pros & Cons

| Pros | Cons |
|------|------|
| Fully automated | Requires Business+ plan |
| No HR intervention | 5-30 min processing delay |
| Reliable (Zoom handles recording) | Zoom storage limits apply |
| Works even if HR forgets | Monthly Zoom cost increase |

### Estimated Build Time
- 1-2 days (webhook handler + download logic)

---

## 7. Option 5: Custom Zoom App with Recording Bot

### Overview
A dedicated bot participant that joins meetings and records audio using Zoom Meeting SDK. Most complex but most powerful.

### Architecture
```
┌─────────────────────────────────────────────────────────────┐
│                    ZOOM MEETING                              │
│                                                              │
│  Participants:                                              │
│  - Manager                                                  │
│  - Employees                                                │
│  - 🤖 Recording Bot (auto-joins)                            │
│                                                              │
└─────────────────────────────┬───────────────────────────────┘
                              │ Raw audio stream
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  RECORDING BOT SERVER (Cloud Run / VM)                       │
│                                                              │
│  - Uses Zoom Meeting SDK                                    │
│  - Joins meeting automatically                              │
│  - Captures raw audio stream                                │
│  - Streams or saves audio                                   │
│  - Leaves when meeting ends                                 │
└─────────────────────────────┬───────────────────────────────┘
                              │
                              ▼
                    [Processing Pipeline]
```

### Technical Requirements
- Zoom Meeting SDK license
- Server with audio processing capabilities
- Meeting SDK app in Zoom Marketplace

### Pros & Cons

| Pros | Cons |
|------|------|
| Fully automated | Complex implementation |
| Real-time streaming possible | Requires Meeting SDK license |
| Works with any plan | Needs dedicated server |
| Bot visible in participants | Higher maintenance |

### Estimated Build Time
- 1-2 weeks

---

## 8. Backend Processing Pipeline

This pipeline is common to ALL options above.

### Architecture
```
┌─────────────────────────────────────────────────────────────┐
│  CLOUD STORAGE (GCS)                                         │
│  Bucket: gs://verve-meeting-recordings/                      │
│                                                              │
│  New file uploaded: meeting_2026-06-01_10-30.mp3            │
└─────────────────────────────┬───────────────────────────────┘
                              │ Triggers Cloud Function
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  CLOUD FUNCTION: process-meeting-recording                   │
│                                                              │
│  Step 1: Download audio from GCS                            │
│                                                              │
│  Step 2: Split into chunks (if > 30 min)                    │
│          └─ Sarvam has file size/duration limits            │
│                                                              │
│  Step 3: Transcribe with Sarvam AI                          │
│          └─ Handles Hinglish (Hindi + English)              │
│          └─ Returns text with timestamps                    │
│                                                              │
│  Step 4: Extract tasks with Claude AI                       │
│          └─ Prompt: "Extract tasks, assignees, deadlines"   │
│          └─ Returns structured JSON                         │
│                                                              │
│  Step 5: Write to Google Sheets                             │
│          └─ Appends rows to tracking sheet                  │
│                                                              │
│  Step 6: Send notification (optional)                       │
│          └─ Email/Slack summary                             │
└─────────────────────────────────────────────────────────────┘
```

### Sarvam AI Integration
```python
import requests

def transcribe_with_sarvam(audio_path):
    """
    Transcribe Hinglish audio using Sarvam AI Saarika model
    """
    url = "https://api.sarvam.ai/speech-to-text"
    
    headers = {
        "Authorization": f"Bearer {SARVAM_API_KEY}"
    }
    
    with open(audio_path, "rb") as f:
        files = {"file": f}
        data = {
            "model": "saarika:v1",
            "language_code": "hi-IN",  # Handles Hinglish
            "with_timestamps": True
        }
        response = requests.post(url, headers=headers, files=files, data=data)
    
    return response.json()["transcript"]
```

### Claude AI Task Extraction
```python
import anthropic

def extract_tasks(transcript):
    """
    Extract tasks from meeting transcript using Claude
    """
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    
    prompt = f"""
    Analyze this meeting transcript and extract all tasks/action items.
    The conversation is in Hinglish (Hindi + English mix).
    
    For each task, identify:
    1. Task description (in English)
    2. Assignee name (who should do it)
    3. Deadline (if mentioned)
    4. Context (brief background)
    5. Priority (High/Medium/Low based on tone)
    
    Return as JSON array:
    [
      {{
        "task": "Complete Q2 report",
        "assignee": "Rahul",
        "deadline": "June 5",
        "context": "Discussed during Q2 review",
        "priority": "High"
      }}
    ]
    
    Transcript:
    {transcript}
    """
    
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}]
    )
    
    return json.loads(response.content[0].text)
```

### Google Sheets Integration
```python
from google.oauth2 import service_account
from googleapiclient.discovery import build

def write_to_sheet(tasks, meeting_date):
    """
    Append extracted tasks to Google Sheet
    """
    creds = service_account.Credentials.from_service_account_file(
        'service-account.json',
        scopes=['https://www.googleapis.com/auth/spreadsheets']
    )
    
    service = build('sheets', 'v4', credentials=creds)
    
    SPREADSHEET_ID = "your-spreadsheet-id"
    RANGE = "Tasks!A:G"
    
    rows = []
    for task in tasks:
        rows.append([
            meeting_date,
            task.get("task", ""),
            task.get("assignee", ""),
            task.get("deadline", ""),
            task.get("context", ""),
            task.get("priority", "Medium"),
            "Pending"  # Status
        ])
    
    body = {"values": rows}
    
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=RANGE,
        valueInputOption="RAW",
        body=body
    ).execute()
```

---

## 9. Cost Comparison

### Monthly Cost Breakdown (4 hrs/day, 22 working days)

| Component | Calculation | Cost (INR) | Cost (USD) |
|-----------|-------------|------------|------------|
| **Sarvam AI** | 240 min × 22 days × ₹0.75/min | ₹3,960 | $47 |
| **Claude API** | ~60k tokens × 22 days | ₹1,000 | $12 |
| **GCS Storage** | ~3 GB/month | ₹10 | $0.12 |
| **Cloud Functions** | ~22 invocations | Free | Free |
| **Google Sheets** | API calls | Free | Free |
| **Total (Processing)** | | **₹4,970** | **$59** |

### Additional Costs by Option

| Option | Additional Cost | Total Monthly |
|--------|-----------------|---------------|
| 1. Desktop App | ₹0 (one-time build) | ₹4,970 |
| 2. Chrome Extension | ₹0 (one-time build) | ₹4,970 |
| 3. Local + Watcher | ₹0 (one-time build) | ₹4,970 |
| 4. Cloud Recording | ₹2,000-5,000 (Zoom upgrade) | ₹7,000-10,000 |
| 5. Recording Bot | ₹3,000 (SDK + server) | ₹8,000 |

### Alternative Transcription Services

| Service | Cost/Minute | Monthly (5,280 min) | Hinglish Quality |
|---------|-------------|---------------------|------------------|
| **Sarvam AI** | ₹0.75 | ₹3,960 | Excellent |
| OpenAI Whisper | $0.006 | $31.68 (₹2,640) | Good |
| Google STT | $0.016 | $84.48 (₹7,040) | Good |
| Deepgram | $0.0043 | $22.70 (₹1,890) | Moderate |
| Assembly AI | $0.015 | $79.20 (₹6,600) | Good |

**Recommendation:** Sarvam AI for Hinglish accuracy, or OpenAI Whisper for lower cost.

---

## 10. Technology Stack

### Recording Layer

| Component | Technology | Purpose |
|-----------|------------|---------|
| Desktop Recorder | Python + sounddevice | Capture system audio |
| Audio Compression | pydub / ffmpeg | Convert to MP3 |
| Upload Client | google-cloud-storage | Send to GCS |

### Processing Layer

| Component | Technology | Purpose |
|-----------|------------|---------|
| Storage | Google Cloud Storage | Store recordings |
| Trigger | Cloud Functions | Event-driven processing |
| Transcription | Sarvam AI API | Hinglish speech-to-text |
| Task Extraction | Claude API | NLP task parsing |
| Output | Google Sheets API | Store tasks |

### Infrastructure

| Component | Service | Purpose |
|-----------|---------|---------|
| Backend | Cloud Run | API hosting |
| Functions | Cloud Functions | Event processing |
| Storage | GCS | Audio file storage |
| Database | BigQuery | Historical data |
| Scheduling | Cloud Scheduler | Daily reports |

---

## 11. Google Sheet Structure

### Sheet: "Meeting Tasks"

| Column | Description | Example |
|--------|-------------|---------|
| A: Date | Meeting date | 2026-06-01 |
| B: Time | Extraction time from transcript | 10:32 AM |
| C: Task | Task description | Complete Q2 sales report |
| D: Assignee | Person responsible | Rahul Sharma |
| E: Deadline | Due date (if mentioned) | June 5, 2026 |
| F: Context | Brief context | Discussed in Q2 review |
| G: Priority | High/Medium/Low | High |
| H: Status | Pending/In Progress/Done | Pending |
| I: Notes | Follow-up notes | - |

### Sheet: "Daily Summary"

| Column | Description |
|--------|-------------|
| A: Date | Meeting date |
| B: Total Tasks | Count of tasks extracted |
| C: By Assignee | Breakdown by person |
| D: Meeting Duration | Total recording time |
| E: Transcript Link | Link to full transcript |

### Conditional Formatting
- Red: Overdue tasks (Deadline < Today, Status != Done)
- Yellow: Due today
- Green: Completed tasks

---

## 12. Implementation Roadmap

### Phase 1: MVP (Week 1)
| Day | Task |
|-----|------|
| 1 | Set up GCS bucket + Cloud Function skeleton |
| 2 | Build Python desktop recorder app |
| 3 | Integrate Sarvam AI transcription |
| 4 | Integrate Claude task extraction |
| 5 | Set up Google Sheets + test end-to-end |

### Phase 2: Polish (Week 2)
| Day | Task |
|-----|------|
| 6 | Add error handling, retry logic |
| 7 | Build installer for recorder app |
| 8 | Create daily summary automation |
| 9 | Add Slack/Email notifications |
| 10 | Documentation + HR training |

### Phase 3: Enhancements (Optional)
- Speaker diarization (who said what)
- Action item follow-up reminders
- Historical search across meetings
- Dashboard for task analytics

---

## 13. Recommendation

### For Immediate Implementation

**Option 1: Desktop Recorder App** is recommended because:

1. **Works with ANY Zoom plan** - no upgrade needed
2. **Simple to build** - 1-2 days development
3. **Reliable** - captures high-quality system audio
4. **Low cost** - only API costs (~₹5,000/month)
5. **Full control** - no dependency on Zoom features

### Implementation Priority

```
┌─────────────────────────────────────────────────────────────┐
│  RECOMMENDED ARCHITECTURE                                    │
│                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
│  │  Python     │───▶│  GCS        │───▶│  Cloud      │     │
│  │  Recorder   │    │  Bucket     │    │  Function   │     │
│  │  App        │    │             │    │             │     │
│  └─────────────┘    └─────────────┘    └──────┬──────┘     │
│                                               │             │
│                          ┌────────────────────┼─────┐       │
│                          ▼                    ▼     ▼       │
│                    ┌──────────┐        ┌──────────┐        │
│                    │ Sarvam   │───────▶│ Claude   │        │
│                    │ AI       │        │ AI       │        │
│                    └──────────┘        └────┬─────┘        │
│                                             │              │
│                                             ▼              │
│                                      ┌──────────┐          │
│                                      │ Google   │          │
│                                      │ Sheets   │          │
│                                      └──────────┘          │
└─────────────────────────────────────────────────────────────┘
```

### Next Steps

1. **Confirm Sarvam AI access** - Get API key
2. **Create Google Sheet** - Set up structure
3. **Build recorder app** - 1-2 days
4. **Set up GCS + Cloud Function** - 1 day
5. **Test end-to-end** - 1 day
6. **Train HR** - 30 minutes

---

## Appendix A: API Keys Required

| Service | Key Type | Where to Get |
|---------|----------|--------------|
| Sarvam AI | API Key | https://sarvam.ai |
| Claude AI | API Key | https://console.anthropic.com |
| GCS | Service Account | GCP Console |
| Google Sheets | Service Account | GCP Console |

## Appendix B: Sample Prompts

### Task Extraction Prompt
```
You are analyzing a meeting transcript in Hinglish (Hindi + English mix).
Extract all action items, tasks, and assignments mentioned.

For each task, identify:
- What needs to be done (translate to English if in Hindi)
- Who should do it (name)
- When it should be done (deadline if mentioned)
- How urgent it is (based on speaker's tone/words)

Return as structured JSON only.
```

### Follow-up Prompt
```
Based on yesterday's tasks for {assignee}, generate a follow-up 
message in professional Hindi asking for status update.
```

---

## Appendix C: Glossary

| Term | Meaning |
|------|---------|
| Hinglish | Hindi + English mixed conversation |
| ASR | Automatic Speech Recognition |
| GCS | Google Cloud Storage |
| STT | Speech-to-Text |
| Diarization | Identifying who spoke when |

---

**Document End**

*For questions or implementation support, contact the development team.*
