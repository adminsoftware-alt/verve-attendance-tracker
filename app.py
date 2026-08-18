"""
ZOOM BREAKOUT ROOM TRACKER - GCP CLOUD RUN + BIGQUERY
======================================================
Production-ready server for tracking:
- Participant joins/leaves
- Camera ON/OFF with exact timestamps
- Room visits with duration
- QoS data collection
- Dynamic room mapping per meeting

HR Scout Bot Flow:
1. Meeting starts at 9 AM
2. HR joins as "Scout Bot"
3. Opens Zoom App -> Click calibration -> Mappings stored
4. Scout Bot can leave after calibration
5. Webhooks capture all participant activity
6. Daily report generated and emailed
"""

from flask import Flask, request, jsonify, send_from_directory, Response, make_response
from flask_cors import CORS
from google.cloud import bigquery
from datetime import datetime, timedelta, timezone
import threading
import requests
import hmac
import hashlib
import json
import time
import os
import uuid as uuid_lib
import traceback
import urllib.parse

# Google Sheets API
from googleapiclient.discovery import build
from google.oauth2 import service_account
import google.auth

# ==============================================================================
# IST TIMEZONE HELPERS (UTC+5:30 - India Standard Time)
# ==============================================================================
from zt_helpers import *  # noqa: F401,F403 — split from app.py, see zt_helpers.py




def get_ist_date_from_utc(utc_dt):
    """Get IST date string from UTC datetime"""
    if utc_dt is None:
        return get_ist_date()
    ist_dt = utc_to_ist(utc_dt)
    return ist_dt.strftime('%Y-%m-%d')

def validate_date_format(date_str):
    """
    Validate date string is in YYYY-MM-DD format.
    Returns the validated date string or raises ValueError.
    SECURITY: Prevents SQL injection by ensuring only valid date format.
    """
    import re
    if not date_str:
        return get_ist_date()
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        raise ValueError(f"Invalid date format: {date_str}. Expected YYYY-MM-DD")
    # Additional validation: ensure it's a valid date
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        raise ValueError(f"Invalid date: {date_str}")
    return date_str

# ==============================================================================
# PARTICIPANT NAME NORMALIZATION
# ==============================================================================
# Zoom creates duplicate entries when someone rejoins:
#   "Aastha Chandwani", "Aastha Chandwani-1", "Aastha Chandwani-2"
#   "Aman Paul", "Aman Paul-5"
#   "Yashasvi Dhakate", "Yashasvi Dhakate_accurest"
# This normalizes names so all entries merge into one person.

import re as _re







def merge_live_rooms(rooms):
    """Merge duplicate participants within rooms for /attendance/live.
    Dedup key preference: UUID (stable across renames) → normalized name."""
    for room in rooms:
        if 'participants' not in room:
            continue
        seen = {}
        merged_participants = []
        for p in room['participants']:
            uuid = (p.get('participant_uuid') or '').strip()
            base = normalize_participant_name(p.get('participant_name', ''))
            key = uuid if uuid else base.lower().strip()
            if not key:
                continue
            if key in seen:
                # Pick better email
                if p.get('participant_email') and not seen[key].get('participant_email'):
                    seen[key]['participant_email'] = p['participant_email']
                continue
            p_copy = {**p, 'participant_name': base}
            seen[key] = p_copy
            merged_participants.append(p_copy)
        room['participants'] = merged_participants
        room['participant_count'] = len(merged_participants)
    return rooms


# ==============================================================================
# CONFIGURATION
# ==============================================================================

REACT_BUILD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'breakout-calibrator', 'build')
STATIC_PATH = os.path.join(REACT_BUILD_PATH, 'static')
app = Flask(__name__, static_folder=STATIC_PATH, static_url_path='/app/static')
import re
CORS(app, resources={r"/*": {"origins": re.compile(r"https://.*\.(zoom\.us|zoom\.com)$"), "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"], "allow_headers": ["Content-Type", "Authorization"]}})


# Headers for Zoom Apps - allow embedding
@app.after_request
def add_zoom_headers(response):
    # Do NOT set X-Frame-Options - allow Zoom to embed
    # CORS headers - Zoom domains + attendance/dashboard endpoints open for external apps
    origin = request.headers.get('Origin', '')
    path = request.path
    if origin and ('.zoom.us' in origin or '.zoom.com' in origin):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    elif origin and (path.startswith('/attendance/') or path.startswith('/dashboard') or path.startswith('/teams') or path.startswith('/auth/') or path.startswith('/data/') or path.startswith('/employees') or path.startswith('/admin/') or path.startswith('/aliases') or path.startswith('/monitor/') or path == '/chat' or path.startswith('/chat/')):
        # Allow external apps (attendance manager) to call attendance, team, auth & data APIs.
        # Authorization MUST be allowed here: the frontend attaches the signed
        # login token to every call, which turns them into preflighted requests.
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'

    # OWASP Security Headers (required by Zoom Apps)
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self' 'unsafe-inline' 'unsafe-eval' https: data: blob:; frame-ancestors https://*.zoom.us https://*.zoom.com"
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'

    return response

# Zoom Credentials - MUST be set via environment variables
# No default values to prevent accidental deployment without proper configuration
from zt_config import *  # noqa: F401,F403 — split from app.py, see zt_config.py

# Scout Bot Configuration

# ==============================================================================
# FIXED ROOM SEQUENCE - Rooms in the exact order Scout Bot visits them
# ==============================================================================
# This is the master list of room names in the order the bot moves through them.
# When calibration completes, webhooks are sorted by timestamp and matched to this sequence.
# Position 1 webhook = Room index 0, Position 2 webhook = Room index 1, etc.
# To update: Add/remove/reorder room names as needed.
# IMPORTANT: Bot visits rooms in this EXACT order - 1st room = index 0, etc.
FIXED_ROOM_SEQUENCE = [
    # Floor 1 rooms (1.1 to 1.34)
    "1.1:It's Accrual World",
    "1.2:Between The Spreadsheet",
    "1.3:Opera House",
    "1.4:Statue Of Liberty",
    "1.5:The Squad",
    "1.6:Visionary Vault - Team Kruta",
    "1.7:Inspiration Island - Team Kruta",
    "1.8:Life In The Math Lane",
    "1.9:Finance Pirates",
    "1.10:Number Nook - Team Ganesh",
    "1.11:Accountaholics",
    "1.12:The Forbidden City",
    "1.13:Dev's Professional Bungalow",
    "1.14:Innovation Station",
    "1.15:Precision Point",
    "1.16:Creative Corner - Team Dev",
    "1.17:Insight Lounge - Team Dev",
    "1.18:Synergy Space - Team Dev",
    "1.19:Numbers and Nuance",
    "1.20:Sales Wizard",
    "1.21:Sales Station",
    "1.22:Virtual Vista",
    "1.23:The Genius Lounge",
    "1.24:Emirates Palace",
    "1.25:Victoria Memorial",
    "1.26:Number Nexus",
    "1.27:Ledger Lounge",
    "1.28:The Capital Corner",
    "1.29:Meeting Room - Hawks Eye",
    "1.30:HR Connect Room",
    "1.31:HR Strategy Meeting Suite",
    "1.32:Interview Room - 1",
    "1.33:Interview Room - 2",
    "1.34:Interview/Meeting - Eagle Eyes",
    # Floor 2 (Vridam)
    "2.0:Vridam - Wellness Meeting Lounge",
    # Floor 3 rooms (Cloud/Accurest)
    "3.1:Cloud Gunners",
    "3.2:Cloud Knights",
    "3.3:Cloud Avengers",
    "3.4:Cloud Falcons",
    "3.5:Cloud Titans",
    "3.6:Cloud Guardians",
    "3.7:Inspiration Lounge /Meeting Room",
    "3.8:Agenda Chamber/Meeting Room",
    "3.9:ABAP AMS",
    # Floor 4 rooms (KPRC)
    "4.1:KPRC - Legal Eagle",
    "4.2:KPRC - Corporate Crest",
    "4.3:KPRC - Innovation Lounge",
    "4.4:KPRC - Decision Dome",
    "4.5:KPRC - Focus Zone",
    "4.6:KPRC - Strategic Space",
    # Floor 5 rooms (Accurest)
    "5.1:Accurest - HR Oasis",
    "5.2:Accurest-Meeting Room:Strategist",
    "5.3:Accurest - Meeting Room: Pioneer",
    "5.4:Accurest - Automation Crafters",
    "5.5:Accurest-Learning / Meeting room",
    "5.6:Accurest - Sales Lounge",
    "5.7:Accurest - Focus Lab",
    "5.8:Accurest - Pattern Inbound",
    "5.9:Accurest - Pattern Planning",
    "5.10:Accurest - Himal's Suite",
    "5.11:Accurest Insight : Team Shubham",
    "5.12:Accurest - Creators",
    "5.13:Accurest - Interview Room",
    # Special zones
    "6.0:Silence Zone",
    "7.0:Masti Ki Pathshala",
    "8.0:BREAK TIME - Tea/Lunch/ Dinner",
]

# Set to True to use FIXED_ROOM_SEQUENCE instead of dynamic room sequence
USE_FIXED_SEQUENCE = True

# GCP Configuration

# BigQuery Tables

# ==============================================================================
# SIGNED AUTH TOKENS (require_auth) — login issues an HMAC-signed token; every
# mutating endpoint validates it. Fixes the "all POST/PUT/DELETE endpoints
# unauthenticated" known issue without adding a session store: the token is
# stateless and verifiable by any Cloud Run instance.
# ==============================================================================
import base64 as _b64mod
from functools import wraps as _wraps

AUTH_TOKEN_SECRET = (os.environ.get('AUTH_TOKEN_SECRET')
                     or ZOOM_WEBHOOK_SECRET or ZOOM_CLIENT_SECRET or '')
AUTH_TOKEN_TTL_SECONDS = int(os.environ.get('AUTH_TOKEN_TTL_SECONDS', str(12 * 3600)))


def make_auth_token(username, role):
    payload = json.dumps(
        {'u': username, 'r': role, 'exp': int(time.time()) + AUTH_TOKEN_TTL_SECONDS},
        separators=(',', ':'))
    body = _b64mod.urlsafe_b64encode(payload.encode()).decode().rstrip('=')
    sig = hmac.new(AUTH_TOKEN_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_auth_token(token):
    """Return the token payload dict, or None if invalid/expired."""
    try:
        body, sig = token.split('.', 1)
        expected = hmac.new(AUTH_TOKEN_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64mod.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        if payload.get('exp', 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def require_auth(roles=None):
    """Decorator: endpoint requires a valid Bearer token (from /auth/login).
    Optional roles list restricts to those roles. Place BELOW @app.route.
    If no secret is configured at all (bare dev env), auth is skipped."""
    def deco(fn):
        @_wraps(fn)
        def wrapper(*args, **kwargs):
            if not AUTH_TOKEN_SECRET:
                return fn(*args, **kwargs)
            hdr = request.headers.get('Authorization', '')
            token = hdr[7:] if hdr.startswith('Bearer ') else request.headers.get('X-Auth-Token', '')
            payload = verify_auth_token(token) if token else None
            if payload is None:
                return jsonify({'success': False, 'error': 'Authentication required'}), 401
            if roles and payload.get('r') not in roles:
                return jsonify({'success': False, 'error': 'Insufficient role'}), 403
            request.auth_user = payload
            return fn(*args, **kwargs)
        return wrapper
    return deco




# Email Configuration
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
REPORT_EMAIL_FROM = os.environ.get('REPORT_EMAIL_FROM', 'reports@yourdomain.com')
REPORT_EMAIL_TO = os.environ.get('REPORT_EMAIL_TO', '')

# ==============================================================================
# EMAIL ALERT CONFIGURATION (Resend - Free 3000/month)
# ==============================================================================
# Resend API for sending email alerts
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
# Semicolon-separated list of recipient emails (semicolon avoids gcloud parsing issues)
_alert_emails_raw = os.environ.get('ALERT_EMAILS', '')
ALERT_EMAILS = [e.strip() for e in _alert_emails_raw.replace(',', ';').split(';') if e.strip()]
# Sender email (must be verified domain or use onboarding@resend.dev for testing)
ALERT_FROM_EMAIL = os.environ.get('ALERT_FROM_EMAIL', 'onboarding@resend.dev')

# Alert settings
ALERT_STALE_THRESHOLD_SECONDS = 30  # Alert if no data for 30+ seconds
ALERT_RATE_LIMIT_SECONDS = 300  # Max 1 alert per 5 minutes (for stale/error alerts)
ALERT_START_HOUR = 0  # 12 AM IST (24-hour monitoring)
ALERT_END_HOUR = 24  # 12 AM IST (24-hour monitoring)

# WhatsApp Alert Configuration
# Option 1: WAHA (self-hosted, unlimited, free) - Recommended
# Option 2: Callmebot (free but limited)
WAHA_API_URL = os.environ.get('WAHA_API_URL', '')  # e.g., http://34.47.178.82:3000
WAHA_SESSION = os.environ.get('WAHA_SESSION', 'default')  # WAHA session name

# Callmebot fallback
WHATSAPP_PHONE = os.environ.get('WHATSAPP_ALERT_PHONE', '')  # Your phone with country code, e.g., +919876543210
WHATSAPP_APIKEY = os.environ.get('WHATSAPP_ALERT_APIKEY', '')  # Callmebot API key
WHATSAPP_RATE_LIMIT_SECONDS = 60  # Max 1 WhatsApp alert per minute

# Rate limiting state - per alert type
_email_alert_state = {
    'stale': {'last_time': 0, 'count_today': 0},
    'bot_joined': {'last_time': 0, 'count_today': 0},
    'bot_left': {'last_time': 0, 'count_today': 0},
    'meeting_ended': {'last_time': 0, 'count_today': 0},
    'error': {'last_time': 0, 'count_today': 0},
    'recovered': {'last_time': 0, 'count_today': 0},
    'last_reset_date': None,
    'was_stale': False  # Track if we were in stale state
}

# Clients


# ==============================================================================
# IN-MEMORY STATE (Per Meeting - Reset on new meeting)
# ==============================================================================

class MeetingState:
    """State for current meeting - resets when new meeting starts"""

    def __init__(self):
        self._lock = threading.Lock()  # Thread safety for concurrent webhook requests
        self.previous_meeting_uuid = None  # Store previous meeting for QoS collection
        self.previous_meeting_id = None
        self.event_dedup_cache = {}  # Deduplication cache: hash -> timestamp
        self.dedup_ttl_seconds = 60  # Events with same hash within 60s are duplicates
        self._last_cache_cleanup = time.time()  # For periodic cache cleanup
        self.reset()

    def reset(self):
        self.meeting_id = None
        self.meeting_uuid = None
        self.meeting_date = None
        self.uuid_to_name = {}  # room_uuid -> room_name
        self.name_to_uuid = {}  # room_name -> room_uuid
        self.calibration_complete = False
        self.calibrated_at = None
        self.participant_states = {}  # participant_id -> {camera_on: bool, last_room: str, ...}
        self.scout_bot_current_room = None  # Track current room during calibration
        self.pending_room_moves = []  # Queue of (room_name, timestamp) for Scout Bot moves
        self.calibration_in_progress = False  # Flag to track if calibration is active
        # Calibration participant info (for "Move Myself" mode)
        self.calibration_mode = 'scout_bot'  # 'scout_bot' or 'self'
        self.calibration_participant_name = None  # Name of participant doing calibration
        self.calibration_participant_uuid = None  # UUID of participant doing calibration
        # SEQUENCE-BASED MATCHING: Room sequence and next expected index
        self.calibration_sequence = []  # Ordered list of room names ["Room 1", "Room 2", ...]
        self.calibration_next_index = 0  # Index of next expected webhook (0, 1, 2, ...)
        # WEBHOOK-PRIMARY MODE: Pending mapping requests (SDK resolves these)
        self.pending_mapping_requests = []  # List of {request_id, webhook_uuid, participant_name, participant_email, timestamp}
        # Webhook uuids whose mapping is confirmed saved in room_mappings (BQ).
        # /mapping/sync retries any in-memory mapping not in this set, so a
        # failed BQ insert self-heals on the next sync instead of being lost.
        self.persisted_webhook_uuids = set()
        # Correction ledger + dispute freeze (anti ping-pong, 2026-07-21):
        # last_uuid_corrections: uuid -> {'old','new','ts'}; if a later
        # correction proposes REVERSING a recent one, the uuid goes into
        # mapping_disputes and is frozen for the day (logged DISPUTED).
        self.last_uuid_corrections = {}
        self.mapping_disputes = {}
        # Where each participant is RIGHT NOW according to webhooks:
        # 'e:<email>' / 'n:<normalized name>' -> {'room_uuid', 'ts', 'meeting_uuid'}.
        # Lets /mapping/sync resolve unknown uuids from CURRENT state (SDK says
        # X is in room R now + webhooks say X is in uuid U now => U = R), so a
        # short bot session maps every occupied room at once — no fresh join
        # events needed. Set on breakout join, cleared on breakout leave.
        self.participant_current_breakout = {}
        # Meeting INSTANCE uuid of the most recent breakout webhook. When the
        # meeting ends and restarts (same numeric id, new instance uuid),
        # entries recorded under the old instance become untrustworthy on
        # workers that missed the participant's rejoin — sync resolution only
        # trusts entries from the current instance.
        self.last_breakout_instance_uuid = None
        # Note: Don't reset dedup cache on meeting reset - keep it for cross-meeting dedup
        print("[MeetingState] Reset for new meeting")

    def is_duplicate_event(self, participant_id, event_type, event_timestamp):
        """Check if this event is a duplicate (same event received twice from Zoom)"""
        # Create a hash of the event
        event_hash = f"{participant_id}:{event_type}:{event_timestamp}"
        now = time.time()

        with self._lock:
            # Clean old entries from cache periodically (every 60 seconds, not on every event)
            # This prevents memory issues with high-frequency events
            if now - self._last_cache_cleanup > 60:
                self.event_dedup_cache = {k: v for k, v in self.event_dedup_cache.items()
                                           if now - v < self.dedup_ttl_seconds}
                self._last_cache_cleanup = now

            # Check if we've seen this event recently
            if event_hash in self.event_dedup_cache:
                print(f"  -> DUPLICATE detected, skipping: {event_hash}")
                return True

            # Mark as seen
            self.event_dedup_cache[event_hash] = now
            return False

    def set_meeting(self, meeting_id, meeting_uuid=None):
        """Set current meeting, reset if different from previous"""
        today = get_ist_date()  # Use IST date for consistency with India timezone

        # Check if this is a new meeting
        if self.meeting_id != meeting_id or self.meeting_date != today:
            # Store previous meeting info for QoS collection
            old_uuid = self.meeting_uuid
            old_id = self.meeting_id

            print(f"[MeetingState] New meeting detected: {meeting_id}")

            # Trigger QoS collection for previous meeting (if exists)
            if old_uuid and old_uuid != meeting_uuid:
                print(f"[MeetingState] Previous meeting UUID: {old_uuid} - triggering QoS collection")
                self.previous_meeting_uuid = old_uuid
                self.previous_meeting_id = old_id
                # Trigger async QoS collection
                self._collect_previous_meeting_qos(old_uuid, old_id)

            # Check if this is a NEW DAY (different from stored meeting_date)
            # Only delete old mappings when transitioning to a NEW day
            if old_id and self.meeting_date and self.meeting_date != today:
                print(f"[MeetingState] New day detected ({self.meeting_date} -> {today}), cleaning up old mappings")
                self._cleanup_old_mappings()  # Clean mappings > 7 days old
            # NOTE: NEVER delete today's mappings - they persist for the report

            self.reset()
            self.meeting_id = meeting_id
            self.meeting_uuid = meeting_uuid
            self.meeting_date = today

            # Always load existing mappings from BigQuery after reset
            # This handles: server restart, container scaling, meeting switch
            # Pass meeting_id to avoid loading mappings from different meetings on same day
            print(f"[MeetingState] Loading existing mappings from BigQuery for meeting {meeting_id}...")
            loaded = self.load_mappings_from_bigquery(today, meeting_id=meeting_id)
            if loaded > 0:
                print(f"[MeetingState] Successfully loaded {loaded} mappings")
            else:
                print(f"[MeetingState] No mappings found in BigQuery for today/yesterday")

        if meeting_uuid and not self.meeting_uuid:
            self.meeting_uuid = meeting_uuid

    def _collect_previous_meeting_qos(self, meeting_uuid, meeting_id):
        """Collect QoS data AND camera data for previous meeting in background thread"""
        def collect_qos_async():
            print(f"[AutoQoS] Starting automatic QoS + Camera collection for previous meeting: {meeting_uuid}")
            time.sleep(30)  # Wait 30 seconds for Zoom to finalize data

            collected_count = 0
            error_count = 0

            # First, collect camera QoS data (Dashboard API - only available shortly after meeting)
            camera_data_map = {}
            try:
                # MUST use numeric meeting_id for Dashboard API - UUID does NOT work!
                if not meeting_id or not str(meeting_id).replace('-', '').isdigit():
                    print(f"[AutoQoS] WARNING: No numeric meeting_id available, skipping camera QoS")
                    camera_participants = []
                else:
                    print(f"[AutoQoS] Collecting camera data via Dashboard QoS API using numeric ID: {meeting_id}")
                    camera_participants = zoom_api.get_meeting_participants_qos(meeting_id)
                for cp in camera_participants:
                    user_name = cp.get('user_name', '')
                    email = cp.get('email', '')
                    camera_on_count = cp.get('camera_on_count', 0)
                    camera_on_minutes = cp.get('camera_on_minutes', 0)
                    camera_on_timestamps = cp.get('camera_on_timestamps', [])
                    # Key by name+email for matching
                    key = f"{user_name}|{email}".lower()
                    camera_data_map[key] = {
                        'count': camera_on_count,
                        'minutes': camera_on_minutes,
                        'timestamps': camera_on_timestamps,
                        'intervals': format_camera_intervals(camera_on_timestamps)
                    }
                print(f"[AutoQoS] Got camera data for {len(camera_data_map)} participants")
            except Exception as ce:
                print(f"[AutoQoS] Camera collection error (non-fatal): {ce}")

            try:
                participants = zoom_api.get_past_meeting_participants(meeting_uuid)

                if not participants and meeting_id:
                    participants = zoom_api.get_past_meeting_participants(meeting_id)

                if not participants:
                    print(f"[AutoQoS] No participants found for previous meeting")
                    return

                print(f"[AutoQoS] Processing {len(participants)} participants from previous meeting...")

                for p in participants:
                    try:
                        participant_id = safe_str(
                            p.get('user_id') or p.get('id') or p.get('participant_user_id'),
                            default='unknown'
                        )
                        participant_name = safe_str(
                            p.get('name') or p.get('user_name'),
                            default='Unknown'
                        )
                        participant_email = safe_str(
                            p.get('user_email') or p.get('email'),
                            default=''
                        )
                        duration_seconds = safe_int(p.get('duration', 0))
                        duration_minutes = duration_seconds // 60 if duration_seconds > 0 else 0

                        # Look up camera data using fuzzy matching
                        camera_info = find_camera_data(camera_data_map, participant_name, participant_email)
                        camera_on_count = camera_info.get('count', 0)
                        camera_on_minutes = camera_info.get('minutes', 0)
                        camera_on_intervals = camera_info.get('intervals', '')

                        # Calculate event_date from participant's join_time (not today's date)
                        # This ensures late-night meetings get correct date
                        join_time_str = safe_str(p.get('join_time', ''))
                        event_date = get_ist_date()  # Fallback
                        if join_time_str:
                            try:
                                # Parse ISO timestamp and convert to IST date
                                join_dt = datetime.fromisoformat(join_time_str.replace('Z', '+00:00'))
                                event_date = get_ist_date_from_utc(join_dt.replace(tzinfo=None))
                            except (ValueError, AttributeError):
                                pass  # Keep fallback

                        qos_data = {
                            'qos_id': str(uuid_lib.uuid4()),
                            'meeting_uuid': safe_str(meeting_uuid),
                            'participant_id': participant_id,
                            'participant_name': participant_name,
                            'participant_email': participant_email,
                            'join_time': join_time_str,
                            'leave_time': safe_str(p.get('leave_time', '')),
                            'duration_minutes': duration_minutes,
                            'attentiveness_score': str(p.get('attentiveness_score', '')),
                            'camera_on_count': camera_on_count,
                            'camera_on_minutes': camera_on_minutes,
                            'camera_on_intervals': camera_on_intervals,
                            'recorded_at': datetime.utcnow().isoformat(),
                            'event_date': event_date
                        }

                        if insert_qos_data(qos_data):
                            collected_count += 1
                        else:
                            error_count += 1

                    except Exception as pe:
                        error_count += 1
                        print(f"[AutoQoS] Error processing participant: {pe}")

                print(f"[AutoQoS] Collection complete: {collected_count} success, {error_count} errors")

            except Exception as e:
                print(f"[AutoQoS] Error: {e}")
                traceback.print_exc()

        thread = threading.Thread(target=collect_qos_async, daemon=True)
        thread.start()
        print(f"[AutoQoS] Background thread started for previous meeting QoS")

    def _cleanup_old_mappings(self):
        """Clean up mappings older than 7 days (keep recent for reports)"""
        try:
            client = get_bq_client()
            cutoff_date = (datetime.utcnow() - timedelta(days=7)).strftime('%Y-%m-%d')
            query = f"""
            DELETE FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
            WHERE mapping_date < @cutoff_date
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("cutoff_date", "STRING", cutoff_date)
                ]
            )
            client.query(query, job_config=job_config).result()
            print(f"[MeetingState] Cleaned up mappings older than {cutoff_date}")
        except Exception as e:
            print(f"[MeetingState] Error cleaning up old mappings: {e}")

    def load_mappings_from_bigquery(self, date=None, meeting_id=None):
        """Load today's mappings from BigQuery (after server restart).
        If meeting_id is provided, only load mappings for that specific meeting.
        """
        if date is None:
            date = get_ist_date()

        # Also check yesterday (handles overnight meetings - meeting 9AM to 9AM next day)
        yesterday = (get_ist_now() - timedelta(days=1)).strftime('%Y-%m-%d')

        try:
            client = get_bq_client()
            # Query both today and yesterday to handle timezone edge cases
            # Filter by meeting_id if provided (prevents cross-meeting mapping contamination)
            query_params = [
                bigquery.ScalarQueryParameter("date", "STRING", date),
                bigquery.ScalarQueryParameter("yesterday", "STRING", yesterday),
            ]
            meeting_filter = ""
            if meeting_id:
                # meeting_id column is INT64; compare as STRING so both sides coerce safely
                meeting_filter = " AND CAST(meeting_id AS STRING) = @meeting_id"
                query_params.append(bigquery.ScalarQueryParameter("meeting_id", "STRING", str(meeting_id)))
            query = f"""
            SELECT room_uuid, room_name, meeting_id, mapping_date
            FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
            WHERE mapping_date IN (@date, @yesterday){meeting_filter}
            ORDER BY mapping_date DESC, mapped_at DESC
            """
            job_config = bigquery.QueryJobConfig(query_parameters=query_params)
            results = client.query(query, job_config=job_config).result()

            count = 0
            for row in results:
                room_uuid = row.room_uuid
                room_name = row.room_name
                if room_uuid and room_name:
                    self.uuid_to_name[room_uuid] = room_name
                    self.name_to_uuid[room_name] = room_uuid
                    # Also store without braces
                    stripped = room_uuid.replace('{', '').replace('}', '')
                    if stripped != room_uuid:
                        self.uuid_to_name[stripped] = room_name
                    count += 1

                    if not self.meeting_id and row.meeting_id:
                        self.meeting_id = row.meeting_id

            if count > 0:
                self.calibration_complete = True
                self.meeting_date = date
                print(f"[MeetingState] Loaded {count} mappings from BigQuery for {date}/{yesterday}")

            return count
        except Exception as e:
            print(f"[MeetingState] Error loading mappings: {e}")
            traceback.print_exc()
            return 0

    def add_room_mapping(self, room_uuid, room_name):
        """Add a room mapping"""
        with self._lock:
            self.uuid_to_name[room_uuid] = room_name
            self.name_to_uuid[room_name] = room_uuid

            # Also store without braces
            stripped = room_uuid.replace('{', '').replace('}', '')
            if stripped != room_uuid:
                self.uuid_to_name[stripped] = room_name

            # Store lowercase version too
            self.uuid_to_name[room_uuid.lower()] = room_name
            self.uuid_to_name[stripped.lower()] = room_name

    def add_webhook_room_mapping(self, webhook_uuid, room_name):
        """Add a webhook UUID to room name mapping (different format from SDK)"""
        if webhook_uuid and room_name:
            with self._lock:
                self.uuid_to_name[webhook_uuid] = room_name
                # Also store first 8 chars as key
                short_key = webhook_uuid[:8] if len(webhook_uuid) >= 8 else webhook_uuid
                if short_key not in self.uuid_to_name:
                    self.uuid_to_name[short_key] = room_name

    def get_room_name(self, room_uuid):
        """Get room name from UUID"""
        if not room_uuid:
            return None

        # Try direct lookup
        if room_uuid in self.uuid_to_name:
            return self.uuid_to_name[room_uuid]

        # Try without braces
        stripped = room_uuid.replace('{', '').replace('}', '')
        return self.uuid_to_name.get(stripped)

    def get_participant_state(self, participant_id):
        """Get or create participant state"""
        if participant_id not in self.participant_states:
            self.participant_states[participant_id] = {
                'camera_on': False,
                'camera_on_since': None,
                'current_room': None,
                'joined_at': None
            }
        return self.participant_states[participant_id]

    def update_camera_state(self, participant_id, camera_on, timestamp):
        """Update camera state for participant"""
        state = self.get_participant_state(participant_id)

        if camera_on and not state['camera_on']:
            # Camera turned ON
            state['camera_on'] = True
            state['camera_on_since'] = timestamp
        elif not camera_on and state['camera_on']:
            # Camera turned OFF
            state['camera_on'] = False
            state['camera_on_since'] = None

        return state


# Global meeting state
meeting_state = MeetingState()


_initialized = False

def init_meeting_state():
    """Initialize meeting state - load today's mappings from BigQuery"""
    global _initialized
    if _initialized:
        return

    try:
        count = meeting_state.load_mappings_from_bigquery()
        if count > 0:
            print(f"[Startup] Restored {count} room mappings from BigQuery")
        else:
            print(f"[Startup] No existing mappings found for today")
        _initialized = True
    except Exception as e:
        print(f"[Startup] Could not load mappings: {e}")


# Initialize on module load (works with gunicorn)
# Delayed init - will run on first request if BigQuery not ready at startup
@app.before_request
def ensure_initialized():
    """Ensure mappings are loaded before handling requests"""
    global _initialized
    if not _initialized:
        init_meeting_state()


# ==============================================================================
# BIGQUERY FUNCTIONS
# ==============================================================================

def validate_and_clean_event(event_data, required_fields=None):
    """
    Validate and clean event data before BigQuery insert.
    Ensures all fields have proper types and no None values.
    """
    if required_fields is None:
        required_fields = ['event_id', 'event_type']

    cleaned = {}
    for key, value in event_data.items():
        # Convert None to appropriate defaults
        if value is None:
            if key.endswith('_id') or key.endswith('_uuid') or key.endswith('_name') or key.endswith('_email'):
                cleaned[key] = ''
            elif key.endswith('_seconds') or key.endswith('_minutes') or key.endswith('_count'):
                cleaned[key] = 0
            elif key == 'camera_on':
                cleaned[key] = False
            else:
                cleaned[key] = ''
        # Ensure strings are actually strings
        elif isinstance(value, str):
            cleaned[key] = value.strip()
        # Ensure numbers are proper type
        elif isinstance(value, bool):
            cleaned[key] = value
        elif isinstance(value, (int, float)):
            cleaned[key] = value
        else:
            cleaned[key] = str(value)

    # Validate required fields exist
    for field in required_fields:
        if field not in cleaned or not cleaned[field]:
            print(f"[Validation] Missing required field: {field}")
            return None

    return cleaned


# ==============================================================================
# CALIBRATION STATE PERSISTENCE (BigQuery)
# ==============================================================================

def save_calibration_state(meeting_id, meeting_uuid, state_data):
    """Save calibration state to BigQuery for persistence across restarts"""
    try:
        client = get_bq_client()
        table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_CALIBRATION_STATE_TABLE}"
        today = get_ist_date()

        row = {
            'state_id': f"{meeting_id}_{today}",
            'meeting_id': str(meeting_id),
            'meeting_uuid': meeting_uuid or '',
            'calibration_in_progress': state_data.get('calibration_in_progress', False),
            'calibration_mode': state_data.get('calibration_mode', 'scout_bot'),
            'calibration_participant_name': state_data.get('calibration_participant_name', ''),
            'current_room_index': state_data.get('current_room_index', 0),
            'total_rooms': state_data.get('total_rooms', 0),
            'room_sequence': json.dumps(state_data.get('room_sequence', [])),
            'started_at': state_data.get('started_at', datetime.utcnow().isoformat()),
            'updated_at': datetime.utcnow().isoformat(),
            'calibration_date': today,
            'completed': state_data.get('completed', False),
            'completed_at': state_data.get('completed_at', '')
        }

        # Use MERGE to upsert (insert or update)
        merge_query = f"""
        MERGE `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_CALIBRATION_STATE_TABLE}` AS target
        USING (SELECT @state_id as state_id) AS source
        ON target.state_id = source.state_id
        WHEN MATCHED THEN
            UPDATE SET
                calibration_in_progress = @calibration_in_progress,
                current_room_index = @current_room_index,
                total_rooms = @total_rooms,
                room_sequence = @room_sequence,
                updated_at = @updated_at,
                completed = @completed,
                completed_at = @completed_at
        WHEN NOT MATCHED THEN
            INSERT (state_id, meeting_id, meeting_uuid, calibration_in_progress, calibration_mode,
                    calibration_participant_name, current_room_index, total_rooms, room_sequence,
                    started_at, updated_at, calibration_date, completed, completed_at)
            VALUES (@state_id, @meeting_id, @meeting_uuid, @calibration_in_progress, @calibration_mode,
                    @calibration_participant_name, @current_room_index, @total_rooms, @room_sequence,
                    @started_at, @updated_at, @calibration_date, @completed, @completed_at)
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("state_id", "STRING", row['state_id']),
                bigquery.ScalarQueryParameter("meeting_id", "STRING", row['meeting_id']),
                bigquery.ScalarQueryParameter("meeting_uuid", "STRING", row['meeting_uuid']),
                bigquery.ScalarQueryParameter("calibration_in_progress", "BOOL", row['calibration_in_progress']),
                bigquery.ScalarQueryParameter("calibration_mode", "STRING", row['calibration_mode']),
                bigquery.ScalarQueryParameter("calibration_participant_name", "STRING", row['calibration_participant_name']),
                bigquery.ScalarQueryParameter("current_room_index", "INT64", row['current_room_index']),
                bigquery.ScalarQueryParameter("total_rooms", "INT64", row['total_rooms']),
                bigquery.ScalarQueryParameter("room_sequence", "STRING", row['room_sequence']),
                bigquery.ScalarQueryParameter("started_at", "STRING", row['started_at']),
                bigquery.ScalarQueryParameter("updated_at", "STRING", row['updated_at']),
                bigquery.ScalarQueryParameter("calibration_date", "STRING", row['calibration_date']),
                bigquery.ScalarQueryParameter("completed", "BOOL", row['completed']),
                bigquery.ScalarQueryParameter("completed_at", "STRING", row['completed_at']),
            ]
        )

        client.query(merge_query, job_config=job_config).result()
        print(f"[CalibrationState] Saved state: room {row['current_room_index']}/{row['total_rooms']}, completed={row['completed']}")
        return True

    except Exception as e:
        print(f"[CalibrationState] Error saving state: {e}")
        traceback.print_exc()
        return False


def load_calibration_state(meeting_id=None, date=None):
    """Load calibration state from BigQuery"""
    try:
        client = get_bq_client()
        target_date = date or get_ist_date()

        if meeting_id:
            query = f"""
            SELECT * FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_CALIBRATION_STATE_TABLE}`
            WHERE meeting_id = @meeting_id AND calibration_date = @target_date
            ORDER BY updated_at DESC
            LIMIT 1
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("meeting_id", "STRING", str(meeting_id)),
                    bigquery.ScalarQueryParameter("target_date", "STRING", target_date),
                ]
            )
        else:
            # Get latest calibration state for today
            query = f"""
            SELECT * FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_CALIBRATION_STATE_TABLE}`
            WHERE calibration_date = @target_date
            ORDER BY updated_at DESC
            LIMIT 1
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("target_date", "STRING", target_date),
                ]
            )

        results = list(client.query(query, job_config=job_config).result())

        if results:
            row = results[0]
            state = {
                'state_id': row.state_id,
                'meeting_id': row.meeting_id,
                'meeting_uuid': row.meeting_uuid,
                'calibration_in_progress': row.calibration_in_progress,
                'calibration_mode': row.calibration_mode,
                'calibration_participant_name': row.calibration_participant_name,
                'current_room_index': row.current_room_index,
                'total_rooms': row.total_rooms,
                'room_sequence': json.loads(row.room_sequence) if row.room_sequence else [],
                'started_at': row.started_at,
                'updated_at': row.updated_at,
                'calibration_date': row.calibration_date,
                'completed': row.completed,
                'completed_at': row.completed_at
            }
            print(f"[CalibrationState] Loaded state: room {state['current_room_index']}/{state['total_rooms']}, completed={state['completed']}")
            return state

        return None

    except Exception as e:
        print(f"[CalibrationState] Error loading state: {e}")
        # Table might not exist yet - that's OK
        return None


def update_calibration_progress(meeting_id, room_index):
    """Update only the current room index (lightweight update during calibration)"""
    try:
        client = get_bq_client()
        today = get_ist_date()
        state_id = f"{meeting_id}_{today}"

        update_query = f"""
        UPDATE `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_CALIBRATION_STATE_TABLE}`
        SET current_room_index = @room_index, updated_at = @updated_at
        WHERE state_id = @state_id
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("room_index", "INT64", room_index),
                bigquery.ScalarQueryParameter("updated_at", "STRING", datetime.utcnow().isoformat()),
                bigquery.ScalarQueryParameter("state_id", "STRING", state_id),
            ]
        )

        client.query(update_query, job_config=job_config).result()
        print(f"[CalibrationState] Updated progress: room {room_index}")
        return True

    except Exception as e:
        print(f"[CalibrationState] Error updating progress: {e}")
        return False


def complete_calibration_state(meeting_id):
    """Mark calibration as complete in BigQuery"""
    try:
        client = get_bq_client()
        today = get_ist_date()
        state_id = f"{meeting_id}_{today}"

        update_query = f"""
        UPDATE `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_CALIBRATION_STATE_TABLE}`
        SET completed = TRUE,
            completed_at = @completed_at,
            calibration_in_progress = FALSE,
            updated_at = @updated_at
        WHERE state_id = @state_id
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("completed_at", "STRING", datetime.utcnow().isoformat()),
                bigquery.ScalarQueryParameter("updated_at", "STRING", datetime.utcnow().isoformat()),
                bigquery.ScalarQueryParameter("state_id", "STRING", state_id),
            ]
        )

        client.query(update_query, job_config=job_config).result()
        print(f"[CalibrationState] Marked complete for meeting {meeting_id}")
        return True

    except Exception as e:
        print(f"[CalibrationState] Error marking complete: {e}")
        return False


def correct_calibration_by_timestamp(meeting_id=None, target_date=None):
    """
    POST-CALIBRATION CORRECTION:
    Sort Scout Bot webhooks by timestamp and match to room sequence.
    This fixes any out-of-order webhook issues.

    If USE_FIXED_SEQUENCE is True, uses FIXED_ROOM_SEQUENCE instead of dynamic sequence.
    This is the most reliable method - room names are predetermined!

    Returns: dict with correction results
    """
    try:
        client = get_bq_client()
        today = target_date or get_ist_date()

        print(f"\n{'='*60}")
        print(f"[CalibrationCorrect] Starting timestamp-based correction for {today}")

        # Step 1: Get room sequence - FIXED or dynamic
        # Always load state for meeting context (meeting_id, meeting_uuid)
        state = load_calibration_state(meeting_id, today) or {}

        if USE_FIXED_SEQUENCE and FIXED_ROOM_SEQUENCE:
            room_sequence = FIXED_ROOM_SEQUENCE
            print(f"[CalibrationCorrect] Using FIXED_ROOM_SEQUENCE ({len(room_sequence)} rooms)")
        else:
            # Fall back to dynamic sequence from calibration_state
            if not state.get('room_sequence'):
                print(f"[CalibrationCorrect] No room sequence found for {today}")
                return {'success': False, 'error': 'No room sequence found'}
            room_sequence = state['room_sequence']
            print(f"[CalibrationCorrect] Using dynamic room sequence ({len(room_sequence)} rooms)")

        # Step 2: Get all sequence_calibration mappings, sorted by mapped_at timestamp
        # The mapped_at timestamp reflects when the webhook was processed (close to arrival time)
        # This gives us the actual order Scout Bot visited the rooms
        mapping_query = f"""
        SELECT DISTINCT
            room_uuid,
            MIN(mapped_at) as first_mapped_at
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
        WHERE mapping_date = @target_date
            AND source = 'sequence_calibration'
            AND room_uuid IS NOT NULL
            AND room_uuid != ''
        GROUP BY room_uuid
        ORDER BY first_mapped_at ASC
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("target_date", "STRING", today),
            ]
        )

        results = list(client.query(mapping_query, job_config=job_config).result())
        webhook_uuids = [row.room_uuid for row in results]

        print(f"[CalibrationCorrect] Found {len(webhook_uuids)} sequence_calibration mappings to correct")

        if len(webhook_uuids) == 0:
            print(f"[CalibrationCorrect] No Scout Bot webhooks found")
            return {'success': False, 'error': 'No Scout Bot webhooks found'}

        # Step 3: Match sorted webhooks to room sequence
        # Position 0 webhook = Room 0 in sequence, etc.
        corrections = []
        for i, room_uuid in enumerate(webhook_uuids):
            if i < len(room_sequence):
                room_name = room_sequence[i]
                corrections.append({
                    'room_uuid': room_uuid,
                    'room_name': room_name,
                    'position': i + 1
                })
                print(f"  Position {i+1}: {room_uuid[:20]}... → {room_name}")

        print(f"[CalibrationCorrect] Matched {len(corrections)} rooms")

        # Step 4: Delete old sequence_calibration mappings for today
        delete_query = f"""
        DELETE FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
        WHERE mapping_date = @target_date
            AND source = 'timestamp_calibration'
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("target_date", "STRING", today),
            ]
        )
        client.query(delete_query, job_config=job_config).result()
        print(f"[CalibrationCorrect] Deleted old timestamp_calibration mappings")

        # Step 5: Insert corrected mappings with new source type
        table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}"
        rows_to_insert = []

        for corr in corrections:
            rows_to_insert.append({
                'mapping_id': str(uuid_lib.uuid4()),
                'meeting_id': str(meeting_id or state.get('meeting_id', '')),
                'meeting_uuid': state.get('meeting_uuid', ''),
                'room_uuid': corr['room_uuid'],
                'room_name': corr['room_name'],
                'room_index': corr['position'] - 1,
                'mapping_date': today,
                'mapped_at': datetime.utcnow().isoformat(),
                'source': 'timestamp_calibration'  # New source type - most accurate!
            })

        if rows_to_insert:
            errors = client.insert_rows_json(table_id, rows_to_insert)
            if errors:
                print(f"[CalibrationCorrect] Insert errors: {errors}")
            else:
                print(f"[CalibrationCorrect] Inserted {len(rows_to_insert)} corrected mappings")

        print(f"[CalibrationCorrect] CORRECTION COMPLETE")
        print(f"{'='*60}\n")

        return {
            'success': True,
            'date': today,
            'rooms_corrected': len(corrections),
            'total_in_sequence': len(room_sequence),
            'webhooks_found': len(webhook_uuids)
        }

    except Exception as e:
        print(f"[CalibrationCorrect] Error: {e}")
        traceback.print_exc()
        return {'success': False, 'error': str(e)}


def insert_participant_event(event_data):
    """Insert participant event into BigQuery with validation"""
    try:
        # Validate and clean data
        required = ['event_id', 'event_type', 'event_timestamp', 'event_date',
                   'meeting_id', 'participant_id', 'participant_name', 'inserted_at']
        cleaned_data = validate_and_clean_event(event_data, required)

        if not cleaned_data:
            print(f"[BigQuery] Validation failed for participant event")
            print(f"[BigQuery] Raw data: {json.dumps(event_data, indent=2)}")
            return False

        client = get_bq_client()
        table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_EVENTS_TABLE}"

        errors = client.insert_rows_json(table_id, [cleaned_data])
        if errors:
            print(f"[BigQuery] Insert error: {errors}")
            print(f"[BigQuery] Failed data: {json.dumps(cleaned_data, indent=2)}")
            return False

        return True
    except Exception as e:
        print(f"[BigQuery] Error: {e}")
        traceback.print_exc()
        return False


def insert_camera_event(event_data):
    """Insert camera on/off event into BigQuery with validation"""
    try:
        # Validate and clean data
        required = ['event_id', 'event_type', 'event_timestamp', 'event_date',
                   'meeting_id', 'participant_id', 'participant_name', 'inserted_at']
        cleaned_data = validate_and_clean_event(event_data, required)

        if not cleaned_data:
            print(f"[BigQuery] Validation failed for camera event")
            return False

        # Ensure duration_seconds is int or None
        if 'duration_seconds' in cleaned_data:
            val = cleaned_data['duration_seconds']
            if val is None or val == '':
                cleaned_data['duration_seconds'] = None  # BigQuery accepts NULL for INT64
            else:
                try:
                    cleaned_data['duration_seconds'] = int(val)
                except (ValueError, TypeError):
                    cleaned_data['duration_seconds'] = None

        client = get_bq_client()
        table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_CAMERA_TABLE}"

        errors = client.insert_rows_json(table_id, [cleaned_data])
        if errors:
            print(f"[BigQuery] Camera event error: {errors}")
            print(f"[BigQuery] Failed data: {json.dumps(cleaned_data, indent=2, default=str)}")
            return False

        return True
    except Exception as e:
        print(f"[BigQuery] Camera event error: {e}")
        traceback.print_exc()
        return False


def insert_room_mappings(mappings):
    """
    Insert or update room mappings in BigQuery with MERGE/UPSERT logic.

    DEDUPLICATION RULES:
    1. For same (meeting_id, room_uuid, source) - UPDATE existing row
    2. webhook_calibration source always wins over zoom_sdk_app
    3. Never store Room-XXXXX placeholder names
    4. Normalize room names (strip whitespace)
    """
    try:
        # Clean each mapping
        cleaned_mappings = []
        required = ['mapping_id', 'meeting_id', 'room_uuid', 'room_name', 'mapping_date', 'mapped_at']

        for mapping in mappings:
            # Skip placeholder room names - these indicate calibration failure
            room_name = mapping.get('room_name', '')
            if not room_name or room_name.startswith('Room-') or room_name == 'Unknown Room':
                print(f"[BigQuery] REJECTED placeholder room name: {room_name}")
                continue

            cleaned = validate_and_clean_event(mapping, required)
            if cleaned:
                # Ensure room_index is int
                if 'room_index' in cleaned:
                    try:
                        cleaned['room_index'] = int(cleaned['room_index']) if cleaned['room_index'] else 0
                    except (ValueError, TypeError):
                        cleaned['room_index'] = 0
                # Normalize room name
                cleaned['room_name'] = cleaned['room_name'].strip()
                cleaned_mappings.append(cleaned)
            else:
                print(f"[BigQuery] Skipping invalid mapping: {mapping}")

        if not cleaned_mappings:
            print(f"[BigQuery] No valid mappings to insert")
            return False

        client = get_bq_client()
        table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}"

        # Use MERGE to handle deduplication
        # For each mapping, check if (meeting_id, room_uuid, source) exists
        # If exists AND source matches: UPDATE
        # If exists with lower priority source: DELETE old, INSERT new
        # If not exists: INSERT

        inserted_count = 0
        updated_count = 0
        skipped_count = 0  # already present with same/better data — counts as success

        for mapping in cleaned_mappings:
            meeting_id = mapping['meeting_id']
            room_uuid = mapping['room_uuid']
            room_name = mapping['room_name']
            source = mapping.get('source', 'unknown')
            mapping_date = mapping['mapping_date']

            # Check for existing mapping with same room_uuid
            # CAST on the column side: meeting_id is INT64 in BQ (mapping_date
            # type also varies) — comparing to STRING params without the CAST
            # throws "No matching signature for operator =" and the whole
            # insert silently fails (root cause of empty room_mappings, fixed 2026-07-21)
            check_query = f"""
            SELECT mapping_id, room_name, source
            FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
            WHERE CAST(meeting_id AS STRING) = @meeting_id
              AND room_uuid = @room_uuid
              AND CAST(mapping_date AS STRING) = @mapping_date
            ORDER BY
              CASE WHEN source = 'webhook_calibration' THEN 0 ELSE 1 END,
              mapped_at DESC
            LIMIT 1
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("meeting_id", "STRING", meeting_id),
                    bigquery.ScalarQueryParameter("room_uuid", "STRING", room_uuid),
                    bigquery.ScalarQueryParameter("mapping_date", "STRING", mapping_date),
                ]
            )

            existing = list(client.query(check_query, job_config=job_config).result())

            if existing:
                existing_row = existing[0]
                existing_source = existing_row.source
                existing_name = existing_row.room_name

                # If existing is webhook_calibration and new is not, skip
                if existing_source == 'webhook_calibration' and source != 'webhook_calibration':
                    print(f"[BigQuery] SKIP: {room_name} - webhook_calibration mapping already exists")
                    skipped_count += 1
                    continue

                # If same source and same name, skip
                if existing_source == source and existing_name == room_name:
                    print(f"[BigQuery] SKIP: {room_name} - identical mapping exists")
                    skipped_count += 1
                    continue

                # Update existing mapping (new source wins or same source with new name)
                update_query = f"""
                UPDATE `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
                SET room_name = @room_name,
                    source = @source,
                    mapped_at = @mapped_at
                WHERE CAST(meeting_id AS STRING) = @meeting_id
                  AND room_uuid = @room_uuid
                  AND CAST(mapping_date AS STRING) = @mapping_date
                """
                update_config = bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("room_name", "STRING", room_name),
                        bigquery.ScalarQueryParameter("source", "STRING", source),
                        bigquery.ScalarQueryParameter("mapped_at", "STRING", mapping['mapped_at']),
                        bigquery.ScalarQueryParameter("meeting_id", "STRING", meeting_id),
                        bigquery.ScalarQueryParameter("room_uuid", "STRING", room_uuid),
                        bigquery.ScalarQueryParameter("mapping_date", "STRING", mapping_date),
                    ]
                )
                client.query(update_query, job_config=update_config).result()
                updated_count += 1
                print(f"[BigQuery] UPDATED: {room_name} (was: {existing_name}, source: {existing_source} -> {source})")
            else:
                # Insert new mapping
                errors = client.insert_rows_json(table_id, [mapping])
                if errors:
                    print(f"[BigQuery] Insert error for {room_name}: {errors}")
                else:
                    inserted_count += 1
                    print(f"[BigQuery] INSERTED: {room_name} ({source})")

        print(f"[BigQuery] Mappings: {inserted_count} inserted, {updated_count} updated, {skipped_count} already saved")
        # Success = every mapping is durably in BQ (freshly written OR already there).
        # A row that hit an insert error increments nothing -> False -> caller retries.
        return (inserted_count + updated_count + skipped_count) > 0
    except Exception as e:
        print(f"[BigQuery] Mapping error: {e}")
        traceback.print_exc()
        return False


def find_camera_data(camera_data_map, participant_name, participant_email):
    """
    Find camera data for a participant using fuzzy matching.

    Handles cases where:
    - Email is empty on one side
    - Name format differs (with/without middle name, Guest suffix)
    - Case differences
    """
    if not camera_data_map:
        return {}

    # Clean name - remove (Guest), (Host), etc. suffixes
    import re
    name_lower = (participant_name or '').lower().strip()
    name_lower = re.sub(r'\s*\([^)]*\)\s*$', '', name_lower).strip()  # Remove trailing (...)

    email_lower = (participant_email or '').lower().strip()

    # Try exact match first
    exact_key = f"{name_lower}|{email_lower}"
    if exact_key in camera_data_map:
        return camera_data_map[exact_key]

    # Try name-only match (email might be empty on one side)
    for key, data in camera_data_map.items():
        parts = key.split('|')
        key_name = parts[0] if parts else ''
        key_name = re.sub(r'\s*\([^)]*\)\s*$', '', key_name).strip()  # Remove (Guest) etc.
        key_email = parts[1] if len(parts) > 1 else ''

        # Exact name match with either side having empty email
        if key_name == name_lower:
            if not key_email or not email_lower or key_email == email_lower:
                return data

        # Partial name match (first name)
        if key_name and name_lower:
            key_first = key_name.split()[0] if key_name else ''
            name_first = name_lower.split()[0] if name_lower else ''
            if key_first == name_first and len(key_first) > 2:
                # First names match, check email
                if key_email == email_lower or not key_email or not email_lower:
                    return data

        # Email match (names might differ)
        if key_email and email_lower and key_email == email_lower:
            return data

    return {}


def format_camera_intervals(timestamps):
    """
    Format camera ON timestamps into IST time intervals.

    Input: List of UTC timestamp strings like ['2026-02-22T10:15:00Z', '2026-02-22T10:16:00Z', ...]
    Output: IST formatted intervals like '15:45-16:30, 17:00-18:15'

    Consecutive timestamps (within 2 min) are merged into intervals.
    """
    if not timestamps:
        return ''

    try:
        from datetime import timedelta
        import pytz

        ist = pytz.timezone('Asia/Kolkata')
        utc = pytz.UTC

        # Parse and sort timestamps
        parsed = []
        for ts in timestamps:
            try:
                if isinstance(ts, str):
                    # Handle various formats
                    ts = ts.replace('Z', '+00:00')
                    if '.' in ts:
                        dt = datetime.fromisoformat(ts.split('.')[0] + '+00:00')
                    else:
                        dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = utc.localize(dt)
                    parsed.append(dt)
            except Exception:
                continue

        if not parsed:
            return ''

        parsed.sort()

        # Merge consecutive timestamps into intervals (gap > 2 min = new interval)
        intervals = []
        current_start = parsed[0]
        current_end = parsed[0]

        for dt in parsed[1:]:
            if (dt - current_end).total_seconds() <= 120:  # Within 2 minutes
                current_end = dt
            else:
                intervals.append((current_start, current_end))
                current_start = dt
                current_end = dt

        # Don't forget the last interval
        intervals.append((current_start, current_end))

        # Format as IST time ranges
        formatted = []
        for start, end in intervals:
            start_ist = start.astimezone(ist)
            end_ist = end.astimezone(ist)
            # Add 1 minute to end to account for sample duration
            end_ist = end_ist + timedelta(minutes=1)
            formatted.append(f"{start_ist.strftime('%H:%M')}-{end_ist.strftime('%H:%M')}")

        return ', '.join(formatted)

    except Exception as e:
        print(f"[CameraFormat] Error formatting intervals: {e}")
        return ''


def format_minutes_as_hhmm(minutes):
    """Format minutes as Xh Ym format"""
    if not minutes or minutes <= 0:
        return '0m'
    hours = minutes // 60
    mins = minutes % 60
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def insert_qos_data(qos_data):
    """Insert QoS data into BigQuery with validation"""
    try:
        # Validate and clean data
        required = ['qos_id', 'meeting_uuid', 'recorded_at', 'event_date']
        cleaned_data = validate_and_clean_event(qos_data, required)

        if not cleaned_data:
            print(f"[BigQuery] Validation failed for QoS data")
            print(f"[BigQuery] Raw QoS data: {json.dumps(qos_data, indent=2)}")
            return False

        # Ensure duration_minutes is int
        if 'duration_minutes' in cleaned_data:
            try:
                val = cleaned_data['duration_minutes']
                cleaned_data['duration_minutes'] = int(val) if val is not None and val != '' else 0
            except (ValueError, TypeError):
                cleaned_data['duration_minutes'] = 0

        # Ensure camera_on_minutes is int
        if 'camera_on_minutes' in cleaned_data:
            try:
                val = cleaned_data['camera_on_minutes']
                cleaned_data['camera_on_minutes'] = int(val) if val is not None and val != '' else 0
            except (ValueError, TypeError):
                cleaned_data['camera_on_minutes'] = 0

        # Ensure camera_on_intervals is string
        if 'camera_on_intervals' in cleaned_data:
            if cleaned_data['camera_on_intervals'] is None:
                cleaned_data['camera_on_intervals'] = ''

        client = get_bq_client()
        table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_QOS_TABLE}"

        errors = client.insert_rows_json(table_id, [cleaned_data])
        if errors:
            print(f"[BigQuery] QoS insert error: {errors}")
            print(f"[BigQuery] Failed QoS data: {json.dumps(cleaned_data, indent=2)}")
            return False

        print(f"[BigQuery] QoS insert success for {cleaned_data.get('participant_name', 'unknown')}")
        return True
    except Exception as e:
        print(f"[BigQuery] QoS error: {e}")
        traceback.print_exc()
        return False


# ==============================================================================
# ZOOM API HELPERS
# ==============================================================================

from zt_zoom_api import *  # noqa: F401,F403 — split from app.py, see zt_zoom_api.py



# ==============================================================================
# WEBHOOK EVENT HANDLERS
# ==============================================================================

def is_scout_bot(participant_name, participant_email):
    """Check if participant is the scout bot.

    Name matching is EXACT (after trimming + stripping Zoom rejoin
    suffixes like "-1") — the old substring match meant a real person
    named e.g. "Priya Scout Bot Reviewer" silently vanished from
    attendance tracking (CLAUDE.md known issue)."""
    if participant_email and SCOUT_BOT_EMAIL:
        if participant_email.lower().strip() == SCOUT_BOT_EMAIL.lower().strip():
            return True
    if participant_name and SCOUT_BOT_NAME:
        cleaned = normalize_participant_name(participant_name).lower().strip()
        if cleaned == SCOUT_BOT_NAME.lower().strip():
            return True
    return False


def is_calibration_participant(participant_name, participant_email):
    """
    Check if participant is the calibration participant (for "Move Myself" mode).
    Returns True if:
    - Calibration is in progress AND
    - Participant matches the calibration participant OR is Scout Bot
    """
    # If no calibration in progress, only check for scout bot
    if not meeting_state.calibration_in_progress:
        return is_scout_bot(participant_name, participant_email)

    # Check if this is Scout Bot
    if is_scout_bot(participant_name, participant_email):
        return True

    # Check if this is the calibration participant (for "Move Myself" mode)
    if meeting_state.calibration_mode == 'self' and meeting_state.calibration_participant_name:
        cal_name = meeting_state.calibration_participant_name.lower().strip()
        webhook_name = (participant_name or '').lower().strip()

        if not webhook_name:
            return False

        # Check various matching strategies:
        # 1. Exact match
        if webhook_name == cal_name:
            return True
        # 2. Calibration name is substring of webhook name (e.g., "Shashank" in "Shashank Channawar")
        if cal_name in webhook_name:
            return True
        # 3. Webhook name is substring of calibration name (e.g., webhook truncated)
        if webhook_name in cal_name:
            return True
        # 4. First name match (first word matches)
        cal_first = cal_name.split()[0] if cal_name else ''
        webhook_first = webhook_name.split()[0] if webhook_name else ''
        if cal_first and webhook_first and cal_first == webhook_first:
            return True

    return False


def extract_participant_data(data):
    """
    Extract participant data from Zoom webhook with comprehensive fallbacks.
    Zoom webhooks can have different structures depending on event type.
    """
    payload = data.get('payload', {})
    obj = payload.get('object', {})
    participant = obj.get('participant', {})

    # If participant is empty, try alternate locations
    if not participant:
        participant = payload.get('participant', {})

    # Extract with multiple fallback field names
    participant_id = (
        participant.get('user_id') or
        participant.get('id') or
        participant.get('participant_user_id') or
        participant.get('participant_id') or
        obj.get('participant_user_id') or
        str(uuid_lib.uuid4())[:8]  # Last resort: generate temporary ID
    )

    participant_name = (
        participant.get('user_name') or
        participant.get('name') or
        participant.get('participant_name') or
        participant.get('display_name') or
        'Unknown'
    )

    participant_email = (
        participant.get('email') or
        participant.get('user_email') or
        participant.get('participant_email') or
        ''
    )

    meeting_id = str(obj.get('id', '') or obj.get('meeting_id', '') or payload.get('meeting_id', ''))
    meeting_uuid = obj.get('uuid', '') or obj.get('meeting_uuid', '') or payload.get('meeting_uuid', '')
    room_uuid = obj.get('breakout_room_uuid', '') or obj.get('room_uuid', '') or ''

    # Parse timestamp - Zoom sends event_ts in milliseconds (UTC)
    # IMPORTANT: Use utcfromtimestamp to ensure consistent UTC handling regardless of server timezone
    event_ts = data.get('event_ts', 0)
    if event_ts and event_ts > 0:
        try:
            # Handle both milliseconds and seconds
            if event_ts > 1e12:  # Milliseconds
                event_dt = datetime.utcfromtimestamp(event_ts / 1000)
            else:  # Seconds
                event_dt = datetime.utcfromtimestamp(event_ts)
        except (ValueError, OSError):
            event_dt = datetime.utcnow()
    else:
        event_dt = datetime.utcnow()

    # Convert event_dt to IST for consistent date calculation
    # Cloud Run uses UTC, but reports use IST dates. Storing event_date in IST
    # ensures events between 00:00-05:30 UTC (05:30-11:00 IST) aren't assigned to wrong day.
    event_dt_ist = event_dt + IST_OFFSET

    return {
        'participant_id': str(participant_id) if participant_id else '',
        'participant_name': str(participant_name) if participant_name else 'Unknown',
        'participant_email': str(participant_email) if participant_email else '',
        'meeting_id': meeting_id,
        'meeting_uuid': meeting_uuid,
        'room_uuid': room_uuid,
        'event_dt': event_dt,        # UTC - used for event_timestamp
        'event_date_ist': event_dt_ist.strftime('%Y-%m-%d')  # IST - used for event_date
    }


def handle_participant_joined(data):
    """Handle participant joined main meeting"""
    # Extract data with comprehensive fallbacks
    p = extract_participant_data(data)

    print(f"[ParticipantJoined] Extracted: id={p['participant_id']}, name={p['participant_name']}, meeting={p['meeting_id']}")

    # Scout bot joined - send alert but skip event storage
    if is_scout_bot(p['participant_name'], p['participant_email']):
        print(f"  -> Scout bot joined! Sending alert...")
        try:
            send_alert_bot_joined(p['participant_name'], p['meeting_id'])
        except Exception as e:
            print(f"  -> Alert error: {e}")
        return

    # Validate we have required data
    if not p['meeting_id']:
        print(f"  -> ERROR: Missing meeting_id, cannot store event")
        print(f"  -> Raw data: {json.dumps(data, indent=2)[:500]}")
        return

    # Check for duplicate event (Zoom sometimes sends same webhook twice)
    if meeting_state.is_duplicate_event(p['participant_id'], 'participant_joined', p['event_dt'].isoformat()):
        return

    # Set current meeting
    meeting_state.set_meeting(p['meeting_id'], p['meeting_uuid'])

    event_data = {
        'event_id': str(uuid_lib.uuid4()),
        'event_type': 'participant_joined',
        'event_timestamp': p['event_dt'].isoformat(),
        'event_date': p['event_date_ist'],
        'meeting_id': p['meeting_id'],
        'meeting_uuid': p['meeting_uuid'],
        'participant_id': p['participant_id'],
        'participant_name': p['participant_name'],
        'participant_email': p['participant_email'],
        'room_uuid': '',
        'room_name': 'Main Room',
        'inserted_at': datetime.utcnow().isoformat()
    }

    # Update participant state
    state = meeting_state.get_participant_state(p['participant_id'])
    state['joined_at'] = p['event_dt'].isoformat()
    state['current_room'] = 'Main Room'

    success = insert_participant_event(event_data)
    print(f"  -> JOIN: {p['participant_name']} {'[OK]' if success else '[FAILED]'}")


def handle_participant_left(data):
    """Handle participant left meeting"""
    p = extract_participant_data(data)

    print(f"[ParticipantLeft] Extracted: id={p['participant_id']}, name={p['participant_name']}")

    # Scout bot left - send alert but skip event storage
    if is_scout_bot(p['participant_name'], p['participant_email']):
        print(f"  -> Scout bot left! Sending alert...")
        try:
            send_alert_bot_left(p['participant_name'], p['meeting_id'])
        except Exception as e:
            print(f"  -> Alert error: {e}")
        return

    if not p['meeting_id']:
        print(f"  -> ERROR: Missing meeting_id")
        return

    # Check for duplicate event
    if meeting_state.is_duplicate_event(p['participant_id'], 'participant_left', p['event_dt'].isoformat()):
        return

    event_data = {
        'event_id': str(uuid_lib.uuid4()),
        'event_type': 'participant_left',
        'event_timestamp': p['event_dt'].isoformat(),
        'event_date': p['event_date_ist'],
        'meeting_id': p['meeting_id'],
        'meeting_uuid': p['meeting_uuid'],
        'participant_id': p['participant_id'],
        'participant_name': p['participant_name'],
        'participant_email': p['participant_email'],
        'room_uuid': '',
        'room_name': '',
        'inserted_at': datetime.utcnow().isoformat()
    }

    success = insert_participant_event(event_data)
    print(f"  -> LEAVE: {p['participant_name']} {'[OK]' if success else '[FAILED]'}")


# WEBHOOK-PRIMARY MODE: mapping-request queue tuning
MAPPING_REQUEST_TTL_S = 600      # drop unresolved requests after 10 min
MAPPING_REQUEST_RESERVE_S = 30   # don't re-serve a request handed to the SDK within 30s


# SYNC-STATE RESOLUTION (2026-07-21): lets a SHORT bot session map every
# occupied room at once by cross-matching the SDK sync payload ("X is in room R
# now") against webhook state ("X is in uuid U now") => U = R. Without this,
# rooms only get mapped when a join happens while the bot is online, forcing
# the VM to run 24/7. REVERT: set to False — behavior returns to exactly the
# pending-request-only flow (tracking dict is passive, harmless to leave on).
SYNC_STATE_RESOLUTION_ENABLED = True


def _bo_state_keys(participant_name, participant_email):
    """Lookup keys for participant_current_breakout: email (preferred) + name
    normalized the same way /mapping/sync normalizes the SDK payload
    (strip Zoom rejoin suffix '-N', lowercase)."""
    keys = []
    em = (participant_email or '').strip().lower()
    if em:
        keys.append('e:' + em)
    nm = _re.sub(r'-\d+$', '', (participant_name or '').strip().lower()).strip()
    if nm:
        keys.append('n:' + nm)
    return keys


def _track_breakout_state(participant_name, participant_email, room_uuid,
                          meeting_uuid=None):
    """Remember (or forget, when room_uuid is None) which webhook room uuid a
    participant is in right now. Consumed by /mapping/sync state resolution.
    meeting_uuid = the meeting INSTANCE uuid from the webhook — entries from a
    previous instance (meeting ended + restarted) are ignored by resolution."""
    keys = _bo_state_keys(participant_name, participant_email)
    if not keys:
        return
    with meeting_state._lock:
        if meeting_uuid:
            meeting_state.last_breakout_instance_uuid = meeting_uuid
        for k in keys:
            if room_uuid:
                meeting_state.participant_current_breakout[k] = {
                    'room_uuid': room_uuid, 'ts': time.time(),
                    'meeting_uuid': meeting_uuid or ''
                }
            else:
                meeting_state.participant_current_breakout.pop(k, None)


def _queue_mapping_request(room_uuid, participant_name, participant_email, meeting_id):
    """Queue a webhook-uuid -> room-name lookup for the SDK panel to resolve
    (webhook-primary mode). Deduped by webhook_uuid; bounded queue."""
    with meeting_state._lock:
        if any(req['webhook_uuid'] == room_uuid
               for req in meeting_state.pending_mapping_requests):
            return
        meeting_state.pending_mapping_requests.append({
            'request_id': str(uuid_lib.uuid4()),
            'webhook_uuid': room_uuid,
            'participant_name': participant_name,
            'participant_email': participant_email,
            'meeting_id': meeting_id,
            'timestamp': datetime.utcnow().isoformat(),
            'queued_ts': time.time(),
            'served_ts': None,
        })
        if len(meeting_state.pending_mapping_requests) > 100:
            meeting_state.pending_mapping_requests = meeting_state.pending_mapping_requests[-100:]


def handle_breakout_room_join(data):
    """Handle participant joined breakout room"""
    p = extract_participant_data(data)

    print(f"[BreakoutJoin] Extracted: id={p['participant_id']}, name={p['participant_name']}, room={p['room_uuid'][:20] if p['room_uuid'] else 'none'}...")

    if not p['meeting_id']:
        print(f"  -> ERROR: Missing meeting_id")
        return

    # Set current meeting
    meeting_state.set_meeting(p['meeting_id'], p['meeting_uuid'])

    room_uuid = p['room_uuid']

    # If this is calibration participant (Scout Bot or self), learn webhook UUID -> room name mapping
    if is_calibration_participant(p['participant_name'], p['participant_email']):
        cal_mode = meeting_state.calibration_mode
        cal_name = meeting_state.calibration_participant_name or 'Scout Bot'
        webhook_time = p['event_dt']
        print(f"  -> Calibration participant detected: {p['participant_name']} (mode: {cal_mode}, expected: {cal_name})")
        print(f"  -> Calibration in progress (memory): {meeting_state.calibration_in_progress}")

        # BUG FIX: If in-memory state says not in progress, check BigQuery!
        # This handles the case where webhook hits a different Cloud Run instance
        if not meeting_state.calibration_in_progress:
            print(f"  -> Memory says not in progress, checking BigQuery...")
            bq_state = load_calibration_state(p['meeting_id'])
            if bq_state and bq_state.get('calibration_in_progress') and not bq_state.get('completed'):
                print(f"  -> BigQuery says calibration IS in progress! Restoring state...")
                meeting_state.calibration_in_progress = True
                meeting_state.calibration_mode = bq_state.get('calibration_mode', 'scout_bot')
                meeting_state.calibration_participant_name = bq_state.get('calibration_participant_name', 'Scout Bot')
                meeting_state.calibration_next_index = bq_state.get('current_room_index', 0)
                room_names = bq_state.get('room_sequence', [])
                meeting_state.calibration_sequence = [
                    {'room_name': name, 'room_index': i, 'sdk_uuid': None, 'webhook_uuid': None, 'matched': i < meeting_state.calibration_next_index}
                    for i, name in enumerate(room_names)
                ]
                print(f"  -> Restored: next_index={meeting_state.calibration_next_index}, sequence_len={len(meeting_state.calibration_sequence)}")
            else:
                print(f"  -> BigQuery confirms: Calibration NOT in progress")
                print(f"  -> Calibration participant in breakout room, skipping event storage")
                return

        if not meeting_state.calibration_in_progress:
            print(f"  -> Calibration NOT in progress, skipping to protect existing mappings")
            return

        # =====================================================================
        # PURE POSITION-BASED MATCHING WITH SAFETY CHECKS
        # The nth webhook from calibration participant = nth room in sequence
        # Frontend waits for each webhook before moving to next room,
        # so there is ZERO ambiguity - webhook N always = room N
        #
        # SAFETY: Reject duplicate room_uuids (same room can't be mapped twice)
        # =====================================================================
        room_name = None
        matched_index = -1

        with meeting_state._lock:
            sequence = meeting_state.calibration_sequence
            next_idx = meeting_state.calibration_next_index

            print(f"  -> POSITION-BASED MATCHING: sequence={len(sequence)} rooms, next_index={next_idx}")

            # SAFETY CHECK: Reject duplicate room_uuid (stale/duplicate webhook)
            already_seen = any(
                entry.get('webhook_uuid') == room_uuid and entry.get('matched')
                for entry in sequence
            )
            if already_seen:
                print(f"  -> DUPLICATE REJECTED: room_uuid {room_uuid[:20]}... already mapped to another room")
                print(f"  -> Calibration participant in breakout room, skipping event storage")
                return

            if sequence and next_idx < len(sequence):
                entry = sequence[next_idx]
                room_name = entry['room_name']
                entry['webhook_uuid'] = room_uuid
                entry['matched'] = True
                matched_index = next_idx
                meeting_state.calibration_next_index = next_idx + 1

                remaining = len(sequence) - meeting_state.calibration_next_index
                print(f"  -> MATCH: webhook #{next_idx + 1} = {room_name}")
                print(f"  -> Webhook UUID: {room_uuid[:30] if room_uuid else 'None'}...")
                print(f"  -> Remaining: {remaining}")
            elif not sequence:
                print(f"  -> WARNING: No calibration sequence (calibration/start not called?)")
            else:
                print(f"  -> WARNING: All rooms already matched (index {next_idx} >= {len(sequence)})")

        if room_name and room_uuid and matched_index >= 0:
            meeting_state.add_webhook_room_mapping(room_uuid, room_name)
            print(f"  -> CALIBRATION SUCCESS: {room_uuid[:20]}... = {room_name}")

            try:
                today = get_ist_date()
                mapping_row = {
                    'mapping_id': str(uuid_lib.uuid4()),
                    'meeting_id': str(meeting_state.meeting_id),
                    'meeting_uuid': meeting_state.meeting_uuid or '',
                    'room_uuid': room_uuid,
                    'room_name': room_name,
                    'room_index': matched_index,
                    'mapping_date': today,
                    'mapped_at': datetime.utcnow().isoformat(),
                    'source': 'sequential_calibration'
                }
                success = insert_room_mappings([mapping_row])
                if success:
                    print(f"  -> SAVED to BigQuery: {room_name} = {room_uuid[:20]}...")
                    update_calibration_progress(meeting_state.meeting_id, meeting_state.calibration_next_index)
                else:
                    print(f"  -> WARNING: BigQuery insert failed for {room_name}")
            except Exception as e:
                print(f"  -> ERROR saving to BigQuery: {e}")
        else:
            print(f"  -> WARNING: Could not match webhook UUID")
            print(f"  -> room_name={room_name}, room_uuid={room_uuid[:20] if room_uuid else 'None'}")

        print(f"  -> Calibration participant in breakout room, skipping event storage")
        return

    # Check for duplicate event
    if meeting_state.is_duplicate_event(p['participant_id'], 'breakout_room_joined', p['event_dt'].isoformat()):
        return

    # Get room name from mapping
    room_name_known = False
    if room_uuid:
        room_name = meeting_state.get_room_name(room_uuid)
        if room_name:
            room_name_known = True
        else:
            # Unknown room_uuid - use placeholder and queue mapping request.
            # The SDK panel will look up which room this participant is in;
            # zt_intervals also resolves placeholders via room_mappings at
            # build time, so the event is safe to store as-is.
            room_name = f'Room-{room_uuid[:8]}'
            print(f"  -> Unknown room_uuid, queueing mapping request")
            _queue_mapping_request(room_uuid, p['participant_name'],
                                   p['participant_email'], p['meeting_id'])
    else:
        room_name = 'Unknown Room'
        print(f"  -> WARNING: No room_uuid in event data")

    event_data = {
        'event_id': str(uuid_lib.uuid4()),
        'event_type': 'breakout_room_joined',
        'event_timestamp': p['event_dt'].isoformat(),
        'event_date': p['event_date_ist'],
        'meeting_id': p['meeting_id'],
        'meeting_uuid': p['meeting_uuid'],
        'participant_id': p['participant_id'],
        'participant_name': p['participant_name'],
        'participant_email': p['participant_email'],
        'room_uuid': room_uuid,
        'room_name': room_name,
        'inserted_at': datetime.utcnow().isoformat()
    }

    # Update participant state
    state = meeting_state.get_participant_state(p['participant_id'])
    state['current_room'] = room_name

    # Track current webhook-room uuid for /mapping/sync state resolution
    _track_breakout_state(p['participant_name'], p['participant_email'],
                          room_uuid, p['meeting_uuid'])

    success = insert_participant_event(event_data)
    status_suffix = ' (mapping queued)' if room_uuid and not room_name_known else ''
    print(f"  -> ROOM JOIN: {p['participant_name']} -> {room_name}{status_suffix} {'[OK]' if success else '[FAILED]'}")


def handle_breakout_room_leave(data):
    """Handle participant left breakout room"""
    p = extract_participant_data(data)

    print(f"[BreakoutLeave] Extracted: id={p['participant_id']}, name={p['participant_name']}")

    # Skip calibration participant (Scout Bot or self)
    if is_calibration_participant(p['participant_name'], p['participant_email']):
        print(f"  -> Calibration participant left breakout room, skipping")
        return

    if not p['meeting_id']:
        print(f"  -> ERROR: Missing meeting_id")
        return

    # Check for duplicate event
    if meeting_state.is_duplicate_event(p['participant_id'], 'breakout_room_left', p['event_dt'].isoformat()):
        return

    room_uuid = p['room_uuid']
    room_name = meeting_state.get_room_name(room_uuid) if room_uuid else 'Unknown Room'
    if not room_name and room_uuid:
        room_name = f'Room-{room_uuid[:8]}'
        # WEBHOOK-PRIMARY MODE: unknown uuid on a LEAVE happens after server
        # restarts mid-day (mappings not yet reloaded). Queue a lookup; note
        # the participant has already LEFT this room, so the SDK lookup only
        # succeeds if someone else is still in it — sync-time resolution and
        # the build-time room_mappings join cover the rest.
        _queue_mapping_request(room_uuid, p['participant_name'],
                               p['participant_email'], p['meeting_id'])

    event_data = {
        'event_id': str(uuid_lib.uuid4()),
        'event_type': 'breakout_room_left',
        'event_timestamp': p['event_dt'].isoformat(),
        'event_date': p['event_date_ist'],
        'meeting_id': p['meeting_id'],
        'meeting_uuid': p['meeting_uuid'],
        'participant_id': p['participant_id'],
        'participant_name': p['participant_name'],
        'participant_email': p['participant_email'],
        'room_uuid': room_uuid,
        'room_name': room_name,
        'inserted_at': datetime.utcnow().isoformat()
    }

    # Participant is back in the main room (or leaving) — forget their breakout uuid
    _track_breakout_state(p['participant_name'], p['participant_email'],
                          None, p['meeting_uuid'])

    success = insert_participant_event(event_data)
    print(f"  -> ROOM LEAVE: {p['participant_name']} <- {room_name} {'[OK]' if success else '[FAILED]'}")


def handle_camera_event(data, camera_on):
    """Handle camera on/off event"""
    p = extract_participant_data(data)

    print(f"[CameraEvent] Extracted: id={p['participant_id']}, name={p['participant_name']}, on={camera_on}")

    # Skip scout bot
    if is_scout_bot(p['participant_name'], p['participant_email']):
        print(f"  -> Scout bot camera event, skipping")
        return

    if not p['meeting_id']:
        print(f"  -> ERROR: Missing meeting_id")
        return

    event_dt = p['event_dt']

    # Get current room for participant
    state = meeting_state.get_participant_state(p['participant_id'])
    current_room = state.get('current_room', 'Main Room') or 'Main Room'

    # Calculate duration if camera turning OFF
    duration_seconds = None
    if not camera_on and state.get('camera_on_since'):
        try:
            on_time = datetime.fromisoformat(state['camera_on_since'])
            duration_seconds = int((event_dt - on_time).total_seconds())
            # Sanity check - duration should be positive and reasonable
            if duration_seconds < 0:
                duration_seconds = 0
            elif duration_seconds > 86400:  # More than 24 hours
                duration_seconds = None  # Discard unreasonable value
        except Exception as e:
            print(f"  -> ERROR calculating duration: {e}")
            duration_seconds = None

    camera_event = {
        'event_id': str(uuid_lib.uuid4()),
        'event_type': 'camera_on' if camera_on else 'camera_off',
        'event_timestamp': event_dt.isoformat(),
        'event_date': p['event_date_ist'],
        'event_time': event_dt.strftime('%H:%M:%S'),
        'meeting_id': p['meeting_id'],
        'meeting_uuid': p['meeting_uuid'],
        'participant_id': p['participant_id'],
        'participant_name': p['participant_name'],
        'participant_email': p['participant_email'],
        'camera_on': camera_on,
        'room_name': current_room,
        'duration_seconds': duration_seconds,
        'inserted_at': datetime.utcnow().isoformat()
    }

    # Update state BEFORE insert so we track camera_on_since correctly
    meeting_state.update_camera_state(p['participant_id'], camera_on, event_dt.isoformat())

    success = insert_camera_event(camera_event)
    status = 'ON' if camera_on else 'OFF'
    duration_str = f" (was on for {duration_seconds}s)" if duration_seconds is not None else ""
    print(f"  -> CAMERA {status}: {p['participant_name']} at {event_dt.strftime('%H:%M:%S')}{duration_str} {'[OK]' if success else '[FAILED]'}")






def handle_meeting_ended(data):
    """Handle meeting ended - collect final QoS data"""
    payload = data.get('payload', {})
    obj = payload.get('object', {})
    meeting_uuid = obj.get('uuid', '')
    meeting_id = str(obj.get('id', ''))

    print(f"[Meeting] Meeting ended: {meeting_uuid}")
    print(f"[Meeting] Meeting ID: {meeting_id}")

    # Send meeting ended alert
    try:
        send_alert_meeting_ended(meeting_id, meeting_uuid)
    except Exception as e:
        print(f"[Meeting] Alert error: {e}")

    # Collect QoS data in background
    def collect_qos():
        time.sleep(45)  # Wait 45 seconds for Zoom to finalize QoS data (30-60s optimal)
        collected_count = 0
        error_count = 0

        try:
            # FIRST: Collect camera data from Dashboard QoS API (must do this quickly!)
            camera_data_map = {}
            try:
                # MUST use numeric meeting_id for Dashboard API - UUID does NOT work!
                if not meeting_id or not str(meeting_id).replace('-', '').isdigit():
                    print(f"[QoS] WARNING: No numeric meeting_id available, skipping camera QoS")
                    camera_participants = []
                else:
                    print(f"[QoS] Collecting camera data via Dashboard QoS API using numeric ID: {meeting_id}")
                    camera_participants = zoom_api.get_meeting_participants_qos(meeting_id)
                for cp in camera_participants:
                    user_name = cp.get('user_name', '')
                    email = cp.get('email', '')
                    camera_on_count = cp.get('camera_on_count', 0)
                    camera_on_minutes = cp.get('camera_on_minutes', 0)
                    camera_on_timestamps = cp.get('camera_on_timestamps', [])
                    key = f"{user_name}|{email}".lower()
                    camera_data_map[key] = {
                        'count': camera_on_count,
                        'minutes': camera_on_minutes,
                        'timestamps': camera_on_timestamps,
                        'intervals': format_camera_intervals(camera_on_timestamps)
                    }
                print(f"[QoS] Got camera data for {len(camera_data_map)} participants")
            except Exception as ce:
                print(f"[QoS] Camera collection error (non-fatal): {ce}")

            # Then get participant list
            participants = zoom_api.get_past_meeting_participants(meeting_uuid)

            if not participants:
                print(f"[QoS] No participants found via past_meeting API")
                # Try with meeting_id instead
                participants = zoom_api.get_past_meeting_participants(meeting_id)

            if not participants:
                print(f"[QoS] No participants found - API may require Business+ plan")
                return

            print(f"[QoS] Processing {len(participants)} participants...")
            print(f"[QoS] Sample raw data: {json.dumps(participants[0] if participants else {}, indent=2)}")

            for p in participants:
                try:
                    # Extract participant ID with fallbacks
                    participant_id = safe_str(
                        p.get('user_id') or p.get('id') or p.get('participant_user_id') or p.get('registrant_id'),
                        default='unknown'
                    )

                    # Extract name with fallbacks
                    participant_name = safe_str(
                        p.get('name') or p.get('user_name') or p.get('participant_name'),
                        default='Unknown'
                    )

                    # Extract email with fallbacks
                    participant_email = safe_str(
                        p.get('user_email') or p.get('email') or p.get('participant_email'),
                        default=''
                    )

                    # Zoom API returns 'duration' in SECONDS - convert to minutes
                    duration_seconds = safe_int(p.get('duration', 0))
                    duration_minutes = duration_seconds // 60 if duration_seconds > 0 else 0

                    # Extract times - handle various date formats
                    join_time = safe_str(p.get('join_time', ''))
                    leave_time = safe_str(p.get('leave_time', ''))

                    # Attentiveness score - may be string or number
                    attentiveness = p.get('attentiveness_score')
                    if attentiveness is None:
                        attentiveness_score = ''
                    elif isinstance(attentiveness, (int, float)):
                        attentiveness_score = str(attentiveness)
                    else:
                        attentiveness_score = safe_str(attentiveness)

                    # Look up camera data using fuzzy matching
                    camera_info = find_camera_data(camera_data_map, participant_name, participant_email)
                    camera_on_count = camera_info.get('count', 0)
                    camera_on_minutes = camera_info.get('minutes', 0)
                    camera_on_intervals = camera_info.get('intervals', '')

                    # Calculate event_date from participant's join_time (not today's date)
                    event_date = get_ist_date()  # Fallback
                    if join_time:
                        try:
                            join_dt = datetime.fromisoformat(join_time.replace('Z', '+00:00'))
                            event_date = get_ist_date_from_utc(join_dt.replace(tzinfo=None))
                        except (ValueError, AttributeError):
                            pass  # Keep fallback

                    qos_data = {
                        'qos_id': str(uuid_lib.uuid4()),
                        'meeting_uuid': safe_str(meeting_uuid),
                        'participant_id': participant_id,
                        'participant_name': participant_name,
                        'participant_email': participant_email,
                        'join_time': join_time,
                        'leave_time': leave_time,
                        'duration_minutes': duration_minutes,
                        'attentiveness_score': attentiveness_score,
                        'camera_on_count': camera_on_count,
                        'camera_on_minutes': camera_on_minutes,
                        'camera_on_intervals': camera_on_intervals,
                        'recorded_at': datetime.utcnow().isoformat(),
                        'event_date': event_date
                    }

                    # Log each insert for debugging
                    camera_str = f", camera={camera_on_minutes}min" if camera_on_minutes > 0 else ""
                    print(f"[QoS] Inserting: {participant_name} - duration={duration_minutes}min{camera_str}")

                    if insert_qos_data(qos_data):
                        collected_count += 1
                    else:
                        error_count += 1
                        print(f"[QoS] Failed to insert data for {participant_name}")

                except Exception as pe:
                    error_count += 1
                    print(f"[QoS] Error processing participant: {pe}")
                    print(f"[QoS] Raw participant data: {json.dumps(p, indent=2)}")

            print(f"[QoS] Collection complete: {collected_count} success, {error_count} errors")

        except Exception as e:
            print(f"[QoS] Collection error: {e}")
            traceback.print_exc()

    thread = threading.Thread(target=collect_qos, daemon=True)
    thread.start()


# ==============================================================================
# FLASK ROUTES
# ==============================================================================

@app.route('/')
@app.route('/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'Breakout Room Calibrator',
        'version': '2.1.0-split',
        'config': {
            'project': GCP_PROJECT_ID,
            'dataset': BQ_DATASET,
            'scout_bot': SCOUT_BOT_NAME
        },
        'current_meeting': {
            'meeting_id': meeting_state.meeting_id,
            'calibration_complete': meeting_state.calibration_complete,
            'rooms_mapped': len(meeting_state.uuid_to_name)
        },
        'timestamp': datetime.utcnow().isoformat()
    })


# ==============================================================================
# CHATBOT — natural-language attendance assistant (Gemini intent routing)
# ==============================================================================

@app.route('/chat', methods=['POST'])
@require_auth()
def chat_endpoint():
    """Take a free-text prompt + user context, dispatch to a chatbot intent.
    Always returns HTTP 200 with a JSON body so the UI can show the error
    text instead of a generic "API error 500"."""
    try:
        from chatbot import dispatch as _chat_dispatch
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'success': False, 'message': f'Chatbot module failed to load: {e}'})

    data = request.get_json(silent=True) or {}
    prompt = (data.get('prompt') or '').strip()
    confirm_token = data.get('confirm_token') or None
    if not prompt and not confirm_token:
        return jsonify({'success': False, 'message': 'prompt is required'})

    try:
        base_url = request.host_url.rstrip('/')
        ctx = {
            'client': get_bq_client(),
            'dataset_ref': f"{GCP_PROJECT_ID}.{BQ_DATASET}",
            'project_id': GCP_PROJECT_ID,
            'base_url': base_url,
            # Identity/role come from the VERIFIED token, never from the
            # request body (client-supplied role was a privilege-escalation
            # hole — CLAUDE.md known issue).
            'user': (getattr(request, 'auth_user', None) or {}).get('u')
                    or (data.get('user') or '').strip() or None,
            'role': (getattr(request, 'auth_user', None) or {}).get('r')
                    or (data.get('role') or '').strip() or None,
            # Forwarded so the chatbot's internal POSTs to our own protected
            # endpoints (/attendance/override, /employees/<id>/leave) carry
            # the caller's token and pass require_auth.
            'auth_header': request.headers.get('Authorization', ''),
        }
        history = data.get('history') if isinstance(data.get('history'), list) else None
        return jsonify(_chat_dispatch(prompt, ctx, confirm_token=confirm_token, history=history))
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'success': False, 'message': f'Chatbot dispatch error: {e}'})


# ==============================================================================
# MONITOR MODE - SDK Polling (replaces calibration)
# SDK getBreakoutRoomList() returns room names + participants directly.
# No UUID mapping needed. React app polls every 30s and sends snapshots here.
# ==============================================================================

# Cross-request snapshot dedup. When the MonitorPanel is open on >1 device
# (HR client + Scout Bot VM), each polls every 30s and writes its own row.
# Downstream queries already bucket-dedup, so reports are correct — but
# room_snapshots_v2 itself silently inflates, costing storage. Here we drop
# any row whose (meeting_id, participant_key, room_name, 30s_bucket) was
# already inserted by a previous request from this same instance within the
# last few minutes. Conservative on purpose: only identical (room, bucket)
# is treated as duplicate, so legitimate room transitions are never lost.
_snapshot_seen = {}            # key -> insert_ts
_SNAPSHOT_SEEN_TTL_SECS = 180  # 3 minutes
_snapshot_seen_last_clean = 0


def _snapshot_cleanup():
    global _snapshot_seen_last_clean
    now = time.time()
    if now - _snapshot_seen_last_clean < 60:
        return
    _snapshot_seen_last_clean = now
    cutoff = now - _SNAPSHOT_SEEN_TTL_SECS
    stale = [k for k, ts in _snapshot_seen.items() if ts < cutoff]
    for k in stale:
        _snapshot_seen.pop(k, None)


def _snapshot_bucket(ts):
    """Convert an ISO timestamp to its 30-second bucket (matches the
    DIV(UNIX_SECONDS(snapshot_time), 30) bucketing used in all downstream
    queries)."""
    try:
        if hasattr(ts, 'timestamp'):
            unix_s = int(ts.timestamp())
        else:
            unix_s = int(datetime.fromisoformat(str(ts)).timestamp())
    except Exception:
        unix_s = int(time.time())
    return unix_s // 30


@app.route('/monitor/snapshot', methods=['POST'])
def monitor_snapshot():
    """
    Receive a room snapshot from SDK polling.
    Called every 30s by React app running on Scout Bot's Zoom client.
    Stores who is in which room at this moment.
    """
    data = request.json or {}
    meeting_id = data.get('meeting_id', '')
    rooms = data.get('rooms', [])

    if not meeting_id or not rooms:
        return jsonify({'error': 'meeting_id and rooms required'}), 400

    # When the monitor starts while Scout Bot is inside a breakout room, the
    # SDK has no numeric meeting ID and sends the meeting UUID instead. The
    # BigQuery meeting_id column is INTEGER, so those inserts fail. Substitute
    # the numeric ID the server already learned from webhooks.
    if not str(meeting_id).strip().isdigit():
        fallback = str(meeting_state.meeting_id or os.environ.get('FIXED_MEETING_ID', '')).strip()
        if fallback.isdigit():
            print(f"[Monitor] Non-numeric meeting_id '{meeting_id}' from SDK - using {fallback}")
            meeting_id = fallback
        else:
            return jsonify({'error': f'meeting_id must be numeric, got: {meeting_id}'}), 400

    now = datetime.utcnow()
    # Optional client capture timestamp: the monitor app queues snapshots it
    # could not deliver (backend outage, Wi-Fi blip) and resends them later.
    # Honoring the ORIGINAL capture time keeps those rows in their true 30s
    # buckets instead of stamping them minutes late. Only accepted if it is
    # in the past and less than 24h old — otherwise fall back to server now.
    snap_dt = now
    captured_at = data.get('captured_at')
    if captured_at:
        try:
            from datetime import timezone as _tz
            cand = datetime.fromisoformat(str(captured_at).replace('Z', '+00:00'))
            if cand.tzinfo is not None:
                cand = cand.astimezone(_tz.utc).replace(tzinfo=None)
            age_s = (now - cand).total_seconds()
            if 0 <= age_s <= 86400:
                snap_dt = cand
        except Exception:
            pass
    today = (snap_dt + timedelta(minutes=330)).strftime('%Y-%m-%d')  # IST date of capture
    snapshot_time = snap_dt.isoformat()
    bucket = _snapshot_bucket(snap_dt)
    _snapshot_cleanup()

    # Dedupe: one row per participant per snapshot. During room transitions
    # the SDK may briefly list a participant in two rooms; storing both rows
    # causes overlapping visits in downstream reports. Prefer breakout rooms
    # over "Main Room" when a conflict exists.
    def _is_main_room(name):
        n = (name or '').lower()
        return n == 'main room' or n.startswith('0.main')

    by_participant = {}  # dedup_key -> {row dict}
    for room in rooms:
        room_name = room.get('room_name', '')
        if not room_name:
            continue

        for p in room.get('participants', []):
            p_name = p.get('name', '') or p.get('participant_name', '') or ''
            p_email = p.get('email', '') or p.get('participant_email', '') or ''
            p_uuid = p.get('uuid', '') or p.get('participant_uuid', '') or ''

            # Skip Scout Bot itself
            if 'scout' in p_name.lower() and 'bot' in p_name.lower():
                continue

            key = (p_uuid or '').strip().lower() or (p_name or '').strip().lower()
            if not key:
                continue

            existing = by_participant.get(key)
            if existing is not None:
                # Keep whichever entry is a breakout room. Skip the new one
                # unless it would promote a stale main-room pick.
                if _is_main_room(room_name) or not _is_main_room(existing['room_name']):
                    continue
                existing['room_name'] = room_name
                continue

            by_participant[key] = {
                'snapshot_id': str(uuid_lib.uuid4()),
                'snapshot_time': snapshot_time,
                'event_date': today,
                'meeting_id': str(meeting_id),
                'room_name': room_name,
                'participant_name': p_name,
                'participant_email': p_email,
                'participant_uuid': p_uuid,
            }

    # Cross-request dedup: drop rows whose (meeting, participant, room,
    # 30s-bucket) signature was already written by an earlier POST within
    # the TTL window. This kills duplicates from multiple MonitorPanel
    # instances at the source without affecting room-transition rows
    # (transitions have a different room_name → different key).
    deduped_rows = []
    seen_keys = []
    skipped_dups = 0
    for r in by_participant.values():
        seen_key = (r['meeting_id'], r['participant_uuid'] or r['participant_name'].lower().strip(),
                    r['room_name'], bucket)
        if seen_key in _snapshot_seen:
            skipped_dups += 1
            continue
        seen_keys.append(seen_key)
        deduped_rows.append(r)

    rows = deduped_rows
    total_participants = len(rows)

    if rows:
        try:
            client = get_bq_client()
            table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots_v2"
            errors = client.insert_rows_json(table_id, rows)
            if errors:
                print(f"[Monitor] BigQuery insert errors: {errors[:3]}")
                return jsonify({'success': False, 'error': str(errors[:3])}), 500
            # Mark rows as seen only AFTER a successful insert. Marking them
            # before poisoned the cache on failure: the client's retry of the
            # same 30s bucket hit the cache and was silently dropped,
            # permanently losing that window.
            now_seen = time.time()
            for sk in seen_keys:
                _snapshot_seen[sk] = now_seen
            extra = f" (skipped {skipped_dups} dup)" if skipped_dups else ""
            print(f"[Monitor] Saved snapshot: {len(rooms)} rooms, {total_participants} participants{extra}")
        except Exception as e:
            print(f"[Monitor] BigQuery error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500
    elif skipped_dups:
        print(f"[Monitor] Snapshot fully deduplicated (skipped {skipped_dups} rows from concurrent polling source)")

    return jsonify({
        'success': True,
        'rooms': len(rooms),
        'participants': total_participants,
        'deduplicated': skipped_dups,
        'snapshot_time': snapshot_time
    })


@app.route('/monitor/status', methods=['GET'])
def monitor_status():
    """Check how many snapshots exist for today"""
    today = get_ist_date()
    try:
        client = get_bq_client()
        query = f"""
        SELECT
          COUNT(DISTINCT snapshot_time) as snapshot_count,
          COUNT(DISTINCT room_name) as room_count,
          COUNT(DISTINCT COALESCE(NULLIF(participant_uuid, ''), NULLIF(participant_email, ''), participant_name)) as participant_count,
          MIN(snapshot_time) as first_snapshot,
          MAX(snapshot_time) as last_snapshot
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots_v2`
        WHERE event_date = '{today}'
        """
        result = list(client.query(query).result())
        row = result[0] if result else {}

        # Format timestamps in IST for easy reading
        first_snap = row.get('first_snapshot')
        last_snap = row.get('last_snapshot')
        first_ist = (first_snap + timedelta(hours=5, minutes=30)).strftime('%H:%M:%S') if first_snap else None
        last_ist = (last_snap + timedelta(hours=5, minutes=30)).strftime('%H:%M:%S') if last_snap else None

        return jsonify({
            'success': True,
            'date': today,
            'snapshots': row.get('snapshot_count', 0),
            'rooms': row.get('room_count', 0),
            'participants': row.get('participant_count', 0),
            'first_snapshot': str(first_snap) if first_snap else None,
            'last_snapshot': str(last_snap) if last_snap else None,
            'first_snapshot_ist': first_ist,
            'last_snapshot_ist': last_ist
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/monitor/sample', methods=['GET'])
def monitor_sample():
    """Get sample snapshot data for debugging"""
    today = get_ist_date()
    try:
        client = get_bq_client()
        query = f"""
        SELECT snapshot_time, room_name, participant_name, participant_email
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots_v2`
        WHERE event_date = '{today}'
        ORDER BY snapshot_time DESC
        LIMIT 50
        """
        results = list(client.query(query).result())
        return jsonify({
            'success': True,
            'date': today,
            'count': len(results),
            'data': [dict(r) for r in results]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/monitor/health', methods=['GET'])
def monitor_health():
    """
    End-to-end health check for the monitoring system.
    Returns whether snapshots are being received and how fresh they are.
    Call this from the VM to verify everything is working.

    Status:
      - HEALTHY: snapshots received within last 5 minutes
      - STALE: snapshots exist today but last one is >5 minutes old
      - NO_DATA: no snapshots today
      - ERROR: BigQuery query failed
    """
    today = get_ist_date()
    try:
        client = get_bq_client()
        query = f"""
        SELECT
          COUNT(DISTINCT snapshot_time) as snapshot_count,
          COUNT(DISTINCT room_name) as room_count,
          COUNT(DISTINCT COALESCE(NULLIF(participant_uuid, ''), NULLIF(participant_email, ''), participant_name)) as participant_count,
          MIN(snapshot_time) as first_snapshot,
          MAX(snapshot_time) as last_snapshot,
          TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(snapshot_time), SECOND) as seconds_since_last
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots_v2`
        WHERE event_date = '{today}'
        """
        result = list(client.query(query).result())
        row = result[0] if result else {}

        snapshot_count = row.get('snapshot_count', 0) or 0
        seconds_since = row.get('seconds_since_last', None)

        if snapshot_count == 0:
            status = 'NO_DATA'
            message = 'No snapshots received today. Is the Zoom App running?'
        elif seconds_since is not None and seconds_since <= 300:
            status = 'HEALTHY'
            message = f'Receiving snapshots. Last one {seconds_since}s ago.'
        else:
            status = 'STALE'
            mins_ago = int(seconds_since / 60) if seconds_since else '?'
            message = f'Last snapshot was {mins_ago} minutes ago. Check if Zoom App is still open.'

        # Check if we should send alert (during meeting hours IST: 9 AM - 8 PM)
        ist_hour = (datetime.utcnow() + timedelta(hours=5, minutes=30)).hour
        is_meeting_hours = 9 <= ist_hour <= 20
        should_alert = status in ('STALE', 'NO_DATA') and is_meeting_hours

        # Format timestamps in IST for easy reading
        first_snap = row.get('first_snapshot')
        last_snap = row.get('last_snapshot')
        first_ist = (first_snap + timedelta(hours=5, minutes=30)).strftime('%H:%M:%S') if first_snap else None
        last_ist = (last_snap + timedelta(hours=5, minutes=30)).strftime('%H:%M:%S') if last_snap else None
        current_ist = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime('%H:%M:%S')

        # Return 503 if not healthy (for GCP Uptime Check alerting)
        http_code = 200 if status == 'HEALTHY' else 503

        return jsonify({
            'status': status,
            'message': message,
            'date': today,
            'current_time_ist': current_ist,
            'snapshots_today': snapshot_count,
            'rooms_seen': row.get('room_count', 0) or 0,
            'participants_seen': row.get('participant_count', 0) or 0,
            'first_snapshot': str(first_snap) if first_snap else None,
            'last_snapshot': str(last_snap) if last_snap else None,
            'first_snapshot_ist': first_ist,
            'last_snapshot_ist': last_ist,
            'seconds_since_last': seconds_since,
            'is_meeting_hours': is_meeting_hours,
            'needs_attention': should_alert
        }), http_code
    except Exception as e:
        return jsonify({
            'status': 'ERROR',
            'message': str(e)
        }), 500


# ═══════════════════════════════════════════════════════
# EMAIL ALERTING (Resend - Free 3000/month)
# ═══════════════════════════════════════════════════════

def _reset_daily_alert_counters():
    """Reset daily counters if date changed."""
    global _email_alert_state
    today = get_ist_date()
    if _email_alert_state.get('last_reset_date') != today:
        for key in ['stale', 'bot_joined', 'bot_left', 'meeting_ended', 'error']:
            if key in _email_alert_state:
                _email_alert_state[key]['count_today'] = 0
        _email_alert_state['last_reset_date'] = today


def _can_send_alert(alert_type, rate_limit_seconds=60):
    """Check if alert can be sent (rate limiting per type)."""
    _reset_daily_alert_counters()
    if alert_type not in _email_alert_state:
        return True, 0
    now = time.time()
    last_time = _email_alert_state[alert_type].get('last_time', 0)
    time_since = now - last_time
    if time_since < rate_limit_seconds:
        return False, int(rate_limit_seconds - time_since)
    return True, 0


def _record_alert_sent(alert_type):
    """Record that an alert was sent."""
    global _email_alert_state
    if alert_type not in _email_alert_state:
        _email_alert_state[alert_type] = {'last_time': 0, 'count_today': 0}
    _email_alert_state[alert_type]['last_time'] = time.time()
    _email_alert_state[alert_type]['count_today'] += 1


def send_email_alert(subject, html_body):
    """
    Send email alert via Brevo API (primary) or Resend (fallback).
    Returns dict with results.
    """
    # Brevo API Configuration (no IP restrictions)
    BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')

    # Try Brevo API first
    if BREVO_API_KEY and ALERT_EMAILS:
        try:
            from_email = ALERT_FROM_EMAIL or 'alerts@verveadvisory.com'

            # Brevo API request
            response = requests.post(
                'https://api.brevo.com/v3/smtp/email',
                headers={
                    'api-key': BREVO_API_KEY,
                    'Content-Type': 'application/json'
                },
                json={
                    'sender': {'email': from_email},
                    'to': [{'email': e} for e in ALERT_EMAILS],
                    'subject': subject,
                    'htmlContent': html_body
                },
                timeout=10
            )

            success = response.status_code in [200, 201]
            if success:
                print(f"[EmailAlert] Sent via Brevo API to {len(ALERT_EMAILS)} recipients: OK")
                return {
                    'success': True,
                    'recipients': ALERT_EMAILS,
                    'recipient_count': len(ALERT_EMAILS),
                    'provider': 'brevo'
                }
            else:
                print(f"[EmailAlert] Brevo API error: {response.status_code} - {response.text[:200]}")
                raise Exception(f"Brevo API: {response.status_code}")

        except Exception as e:
            print(f"[EmailAlert] Brevo error: {e}")
            import traceback
            traceback.print_exc()
            print("[EmailAlert] Trying Resend fallback...")

    # Fallback to Resend
    if not RESEND_API_KEY:
        print("[EmailAlert] No email provider configured (SENDGRID_API_KEY or RESEND_API_KEY)")
        return {'success': False, 'error': 'No email provider configured'}

    if not ALERT_EMAILS:
        print("[EmailAlert] No email recipients configured")
        return {'success': False, 'error': 'No email recipients configured (ALERT_EMAILS)'}

    try:
        response = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {RESEND_API_KEY}',
                'Content-Type': 'application/json'
            },
            json={
                'from': ALERT_FROM_EMAIL,
                'to': ALERT_EMAILS,
                'subject': subject,
                'html': html_body
            },
            timeout=10
        )

        success = response.status_code == 200
        result = {
            'success': success,
            'status_code': response.status_code,
            'recipients': ALERT_EMAILS,
            'recipient_count': len(ALERT_EMAILS),
            'provider': 'resend'
        }

        if success:
            result['email_id'] = response.json().get('id')
            print(f"[EmailAlert] Sent via Resend to {len(ALERT_EMAILS)} recipients: OK")
        else:
            result['error'] = response.text[:200]
            print(f"[EmailAlert] Resend failed: {response.status_code} - {response.text[:100]}")

        return result

    except Exception as e:
        print(f"[EmailAlert] Error: {e}")
        return {'success': False, 'error': str(e)}


# ─────────────────────────────────────────────────────────
# WHATSAPP ALERT (via Callmebot - free service)
# Setup: Send "I allow callmebot to send me messages" to +34 644 71 81 99 on WhatsApp
# ─────────────────────────────────────────────────────────

_whatsapp_last_sent = 0  # Timestamp of last WhatsApp alert

def send_whatsapp_alert(message):
    """
    Send WhatsApp alert via WAHA (primary) or Callmebot (fallback).
    Rate limited to 1 message per WHATSAPP_RATE_LIMIT_SECONDS.
    """
    global _whatsapp_last_sent

    # Check if any provider is configured
    waha_configured = bool(WAHA_API_URL and WHATSAPP_PHONE)
    callmebot_configured = bool(WHATSAPP_PHONE and WHATSAPP_APIKEY)

    if not waha_configured and not callmebot_configured:
        print("[WhatsApp] Not configured (set WAHA_API_URL or WHATSAPP_ALERT_APIKEY)")
        return {'success': False, 'error': 'WhatsApp not configured'}

    # Rate limiting
    now = time.time()
    time_since = now - _whatsapp_last_sent
    if time_since < WHATSAPP_RATE_LIMIT_SECONDS:
        wait = int(WHATSAPP_RATE_LIMIT_SECONDS - time_since)
        print(f"[WhatsApp] Rate limited, wait {wait}s")
        return {'success': False, 'error': f'Rate limited, wait {wait}s'}

    # Try WAHA first (self-hosted, unlimited)
    if waha_configured:
        try:
            # Format phone number for WAHA (remove + and add @c.us)
            phone_clean = WHATSAPP_PHONE.replace('+', '').replace(' ', '')
            chat_id = f"{phone_clean}@c.us"

            response = requests.post(
                f"{WAHA_API_URL}/api/sendText",
                json={
                    "chatId": chat_id,
                    "text": message,
                    "session": WAHA_SESSION
                },
                timeout=10
            )

            if response.status_code == 200 or response.status_code == 201:
                _whatsapp_last_sent = now
                print(f"[WhatsApp] Alert sent via WAHA to {WHATSAPP_PHONE}")
                return {'success': True, 'phone': WHATSAPP_PHONE, 'provider': 'waha'}
            else:
                print(f"[WhatsApp] WAHA failed: {response.status_code} - {response.text[:100]}")
                # Fall through to Callmebot
        except Exception as e:
            print(f"[WhatsApp] WAHA error: {e}, trying Callmebot...")

    # Fallback to Callmebot
    if callmebot_configured:
        try:
            import urllib.parse
            encoded_msg = urllib.parse.quote(message)
            url = f"https://api.callmebot.com/whatsapp.php?phone={WHATSAPP_PHONE}&text={encoded_msg}&apikey={WHATSAPP_APIKEY}"

            response = requests.get(url, timeout=10)

            if response.status_code == 200 and 'Message queued' in response.text:
                _whatsapp_last_sent = now
                print(f"[WhatsApp] Alert sent via Callmebot to {WHATSAPP_PHONE}")
                return {'success': True, 'phone': WHATSAPP_PHONE, 'provider': 'callmebot'}
            else:
                print(f"[WhatsApp] Callmebot failed: {response.status_code} - {response.text[:100]}")
                return {'success': False, 'error': response.text[:100]}

        except Exception as e:
            print(f"[WhatsApp] Callmebot error: {e}")
            return {'success': False, 'error': str(e)}

    return {'success': False, 'error': 'All providers failed'}


def send_stale_whatsapp_alert(seconds_since):
    """Send WhatsApp alert when monitoring stops."""
    mins = int(seconds_since / 60) if seconds_since else 0
    ist_time = get_ist_now().strftime('%H:%M')

    message = f"🚨 ZOOM MONITORING STOPPED!\n\n⏰ Time: {ist_time} IST\n⏱️ Last data: {mins} min ago\n\n👉 Please check the Scout Bot VM and restart the Zoom app."

    return send_whatsapp_alert(message)


# ─────────────────────────────────────────────────────────
# ALERT: Bot Joined Meeting
# ─────────────────────────────────────────────────────────
def send_alert_bot_joined(participant_name, meeting_id):
    """Send alert when Scout Bot joins meeting."""
    can_send, wait_time = _can_send_alert('bot_joined', rate_limit_seconds=300)
    if not can_send:
        print(f"[EmailAlert] Bot joined alert rate limited, wait {wait_time}s")
        return {'sent': False, 'reason': f'Rate limited, wait {wait_time}s'}

    ist_time = get_ist_now().strftime('%I:%M %p')
    today = get_ist_date()
    subject = f"✅ Scout Bot Joined Meeting"
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 500px; padding: 20px; border: 2px solid #27ae60; border-radius: 10px;">
        <h2 style="color: #27ae60; margin-top: 0;">✅ Scout Bot Joined Meeting</h2>
        <p style="font-size: 16px; color: #333;"><strong>{participant_name}</strong> has joined the meeting.</p>
        <p style="background: #d4edda; padding: 15px; border-radius: 5px; border-left: 4px solid #27ae60;">
            <strong>👉 Action Required:</strong><br>
            Please open the Zoom App panel to start monitoring.
        </p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
        <table style="font-size: 14px; color: #666;">
            <tr><td><strong>Time:</strong></td><td>{ist_time} IST</td></tr>
            <tr><td><strong>Meeting ID:</strong></td><td>{meeting_id}</td></tr>
            <tr><td><strong>Date:</strong></td><td>{today}</td></tr>
        </table>
    </div>
    """
    result = send_email_alert(subject, html_body)
    if result.get('success'):
        _record_alert_sent('bot_joined')
    return result


# ─────────────────────────────────────────────────────────
# ALERT: Bot Left Meeting
# ─────────────────────────────────────────────────────────
def send_alert_bot_left(participant_name, meeting_id, reason=""):
    """Send alert when Scout Bot leaves meeting."""
    can_send, wait_time = _can_send_alert('bot_left', rate_limit_seconds=60)
    if not can_send:
        print(f"[EmailAlert] Bot left alert rate limited, wait {wait_time}s")
        return {'sent': False, 'reason': f'Rate limited, wait {wait_time}s'}

    ist_time = get_ist_now().strftime('%I:%M %p')
    today = get_ist_date()
    reason_text = f"<p style='color: #666;'>Reason: {reason}</p>" if reason else ""
    subject = f"⚠️ Scout Bot Left Meeting"
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 500px; padding: 20px; border: 2px solid #e67e22; border-radius: 10px;">
        <h2 style="color: #e67e22; margin-top: 0;">⚠️ Scout Bot Left Meeting</h2>
        <p style="font-size: 16px; color: #333;"><strong>{participant_name}</strong> has left the meeting.</p>
        {reason_text}
        <p style="background: #fff3cd; padding: 15px; border-radius: 5px; border-left: 4px solid #ffc107;">
            <strong>⚠️ Monitoring Stopped:</strong><br>
            No attendance data will be captured until bot rejoins.
        </p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
        <table style="font-size: 14px; color: #666;">
            <tr><td><strong>Time:</strong></td><td>{ist_time} IST</td></tr>
            <tr><td><strong>Meeting ID:</strong></td><td>{meeting_id}</td></tr>
            <tr><td><strong>Date:</strong></td><td>{today}</td></tr>
        </table>
    </div>
    """
    result = send_email_alert(subject, html_body)
    if result.get('success'):
        _record_alert_sent('bot_left')
    return result


# ─────────────────────────────────────────────────────────
# ALERT: Meeting Ended
# ─────────────────────────────────────────────────────────
def send_alert_meeting_ended(meeting_id, meeting_uuid=""):
    """Send alert when meeting ends."""
    can_send, wait_time = _can_send_alert('meeting_ended', rate_limit_seconds=60)
    if not can_send:
        print(f"[EmailAlert] Meeting ended alert rate limited, wait {wait_time}s")
        return {'sent': False, 'reason': f'Rate limited, wait {wait_time}s'}

    ist_time = get_ist_now().strftime('%I:%M %p')
    today = get_ist_date()
    subject = f"📋 Meeting Ended - {meeting_id}"
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 500px; padding: 20px; border: 2px solid #3498db; border-radius: 10px;">
        <h2 style="color: #3498db; margin-top: 0;">📋 Meeting Ended</h2>
        <p style="font-size: 16px; color: #333;">The Zoom meeting has ended.</p>
        <p style="background: #e3f2fd; padding: 15px; border-radius: 5px; border-left: 4px solid #3498db;">
            <strong>ℹ️ Info:</strong><br>
            Daily attendance report will be generated automatically.
        </p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
        <table style="font-size: 14px; color: #666;">
            <tr><td><strong>Time:</strong></td><td>{ist_time} IST</td></tr>
            <tr><td><strong>Meeting ID:</strong></td><td>{meeting_id}</td></tr>
            <tr><td><strong>Date:</strong></td><td>{today}</td></tr>
        </table>
    </div>
    """
    result = send_email_alert(subject, html_body)
    if result.get('success'):
        _record_alert_sent('meeting_ended')
    return result


# ─────────────────────────────────────────────────────────
# ALERT: App Closed / Stale Data
# ─────────────────────────────────────────────────────────
def send_alert_stale_data(seconds_since, status):
    """Send alert when monitoring data is stale (app closed)."""
    can_send, wait_time = _can_send_alert('stale', rate_limit_seconds=ALERT_RATE_LIMIT_SECONDS)
    if not can_send:
        print(f"[EmailAlert] Stale data alert rate limited, wait {wait_time}s")
        return {'sent': False, 'reason': f'Rate limited, wait {wait_time}s'}

    ist_time = get_ist_now().strftime('%I:%M %p')
    today = get_ist_date()

    if status == 'NO_DATA':
        alert_reason = 'No monitoring data received today'
    else:
        alert_reason = f'No data for {seconds_since} seconds'

    subject = f"🚨 Zoom Monitoring Alert - {status}"
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 500px; padding: 20px; border: 2px solid #e74c3c; border-radius: 10px;">
        <h2 style="color: #e74c3c; margin-top: 0;">🚨 Zoom Monitoring Alert</h2>
        <p style="font-size: 16px; color: #333;"><strong>{alert_reason}!</strong></p>
        <p style="color: #666;">Scout Bot may have joined the meeting, but the Zoom App panel is closed.</p>
        <p style="background: #fff3cd; padding: 15px; border-radius: 5px; border-left: 4px solid #ffc107;">
            <strong>👉 Action Required:</strong><br>
            Please open the Zoom App panel to start monitoring.
        </p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
        <table style="font-size: 14px; color: #666;">
            <tr><td><strong>Time:</strong></td><td>{ist_time} IST</td></tr>
            <tr><td><strong>Status:</strong></td><td style="color: #e74c3c;">{status}</td></tr>
            <tr><td><strong>Date:</strong></td><td>{today}</td></tr>
        </table>
    </div>
    """
    result = send_email_alert(subject, html_body)
    if result.get('success'):
        _record_alert_sent('stale')
    return result


# ─────────────────────────────────────────────────────────
# ALERT: System Error
# ─────────────────────────────────────────────────────────
def send_alert_error(error_type, error_message, context=""):
    """Send alert for system errors."""
    can_send, wait_time = _can_send_alert('error', rate_limit_seconds=ALERT_RATE_LIMIT_SECONDS)
    if not can_send:
        print(f"[EmailAlert] Error alert rate limited, wait {wait_time}s")
        return {'sent': False, 'reason': f'Rate limited, wait {wait_time}s'}

    ist_time = get_ist_now().strftime('%I:%M %p')
    today = get_ist_date()
    context_html = f"<p style='color: #666;'>Context: {context}</p>" if context else ""
    subject = f"❌ System Error - {error_type}"
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 500px; padding: 20px; border: 2px solid #e74c3c; border-radius: 10px;">
        <h2 style="color: #e74c3c; margin-top: 0;">❌ System Error</h2>
        <p style="font-size: 16px; color: #333;"><strong>{error_type}</strong></p>
        <p style="background: #fce4ec; padding: 15px; border-radius: 5px; border-left: 4px solid #e74c3c; font-family: monospace; font-size: 12px; word-break: break-all;">
            {error_message[:500]}
        </p>
        {context_html}
        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
        <table style="font-size: 14px; color: #666;">
            <tr><td><strong>Time:</strong></td><td>{ist_time} IST</td></tr>
            <tr><td><strong>Date:</strong></td><td>{today}</td></tr>
        </table>
    </div>
    """
    result = send_email_alert(subject, html_body)
    if result.get('success'):
        _record_alert_sent('error')
    return result


# ─────────────────────────────────────────────────────────
# ALERT: Monitoring Resumed (App Reopened)
# ─────────────────────────────────────────────────────────
def send_alert_recovered():
    """Send confirmation email when monitoring resumes after being stale."""
    can_send, wait_time = _can_send_alert('recovered', rate_limit_seconds=60)
    if not can_send:
        print(f"[EmailAlert] Recovery alert rate limited, wait {wait_time}s")
        return {'sent': False, 'reason': f'Rate limited, wait {wait_time}s'}

    ist_time = get_ist_now().strftime('%I:%M %p')
    today = get_ist_date()
    subject = f"✅ Monitoring Resumed - App Reopened"
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 500px; padding: 20px; border: 2px solid #27ae60; border-radius: 10px;">
        <h2 style="color: #27ae60; margin-top: 0;">✅ Monitoring Resumed</h2>
        <p style="font-size: 16px; color: #333;"><strong>Zoom App panel is now open and monitoring!</strong></p>
        <p style="background: #d4edda; padding: 15px; border-radius: 5px; border-left: 4px solid #27ae60;">
            <strong>✓ All Good:</strong><br>
            Attendance data is now being captured.
        </p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
        <table style="font-size: 14px; color: #666;">
            <tr><td><strong>Time:</strong></td><td>{ist_time} IST</td></tr>
            <tr><td><strong>Status:</strong></td><td style="color: #27ae60;">HEALTHY</td></tr>
            <tr><td><strong>Date:</strong></td><td>{today}</td></tr>
        </table>
    </div>
    """
    result = send_email_alert(subject, html_body)
    if result.get('success'):
        _record_alert_sent('recovered')
    return result


# ─────────────────────────────────────────────────────────
# Check and Send Stale Alert (called by Cloud Scheduler)
# ─────────────────────────────────────────────────────────
def check_and_send_stale_alert():
    """
    Check if monitoring is stale and send email alert if needed.
    Called by Cloud Scheduler every minute.
    Also sends recovery email when monitoring resumes after being stale.
    """
    global _email_alert_state
    today = get_ist_date()

    try:
        client = get_bq_client()
        query = f"""
        SELECT
          COUNT(*) as snapshot_count,
          TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(snapshot_time), SECOND) as seconds_since_last
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots_v2`
        WHERE event_date = '{today}'
        """
        result = list(client.query(query).result())
        row = result[0] if result else {}

        snapshot_count = row.get('snapshot_count', 0) or 0
        seconds_since = row.get('seconds_since_last', None)

        # Determine status
        is_stale = False
        status = 'HEALTHY'

        if snapshot_count == 0:
            is_stale = True
            status = 'NO_DATA'
        elif seconds_since is not None and seconds_since > ALERT_STALE_THRESHOLD_SECONDS:
            is_stale = True
            status = 'STALE'

        was_stale = _email_alert_state.get('was_stale', False)

        # If now healthy but was stale, send recovery email
        if not is_stale and was_stale:
            _email_alert_state['was_stale'] = False
            print(f"[EmailAlert] Monitoring recovered! Sending confirmation...")
            recovery_result = send_alert_recovered()
            return {
                'alert_sent': recovery_result.get('success', False),
                'alert_type': 'recovered',
                'reason': 'Monitoring resumed after being stale',
                'status': status,
                'seconds_since_last': seconds_since,
                'send_result': recovery_result
            }

        # If healthy, nothing to do
        if not is_stale:
            return {
                'alert_sent': False,
                'reason': 'Monitoring is healthy',
                'status': status,
                'seconds_since_last': seconds_since
            }

        # Mark as stale and send stale alert (Email + WhatsApp)
        _email_alert_state['was_stale'] = True
        send_result = send_alert_stale_data(seconds_since, status)

        # Also send WhatsApp alert for immediate notification
        whatsapp_result = send_stale_whatsapp_alert(seconds_since)

        return {
            'alert_sent': send_result.get('success', False) or whatsapp_result.get('success', False),
            'alert_type': 'stale',
            'status': status,
            'seconds_since_last': seconds_since,
            'email_result': send_result,
            'whatsapp_result': whatsapp_result
        }

    except Exception as e:
        print(f"[EmailAlert] Error checking health: {e}")
        return {
            'alert_sent': False,
            'reason': f'Error: {str(e)}',
            'status': 'ERROR'
        }


@app.route('/alert/email/check', methods=['GET', 'POST'])
def email_alert_check():
    """
    Cloud Scheduler calls this every minute.
    Checks if monitoring is stale and sends email alert if needed.
    """
    result = check_and_send_stale_alert()
    print(f"[EmailAlert] Check result: {result}")
    return jsonify(result)


@app.route('/alert/email/test', methods=['POST'])
def email_alert_test():
    """
    Send a test email alert to verify configuration.
    Does NOT respect rate limiting (for testing only).
    Pass ?use_report_recipients=1 to test with report email recipients.
    """
    ist_time = get_ist_now().strftime('%I:%M %p')
    subject = "✅ Test Alert - Zoom Monitoring System"
    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 500px; padding: 20px; border: 2px solid #27ae60; border-radius: 10px;">
        <h2 style="color: #27ae60; margin-top: 0;">✅ Test Alert</h2>
        <p style="font-size: 16px; color: #333;">This is a test message from the Zoom Monitoring System.</p>
        <p style="background: #d4edda; padding: 15px; border-radius: 5px; border-left: 4px solid #27ae60;">
            <strong>Email alerts are working!</strong><br>
            You will receive alerts for:
            <ul style="margin: 10px 0;">
                <li>Bot joined meeting</li>
                <li>Bot left meeting</li>
                <li>Meeting ended</li>
                <li>App closed (no data)</li>
                <li>System errors</li>
            </ul>
        </p>
        <hr style="border: none; border-top: 1px solid #eee; margin: 20px 0;">
        <p style="font-size: 14px; color: #666;">Time: {ist_time} IST</p>
    </div>
    """

    # Allow testing with report module directly
    use_report = request.args.get('use_report_recipients', '0') == '1'
    if use_report:
        # Call report_generator's send function directly
        import report_generator as rg
        try:
            # Create a minimal report object
            test_report = {
                'report_date': get_ist_date(),
                'generated_at': get_ist_now().isoformat(),
                'total_participants': 0,
                'participants': [],
                'csv_content': 'Test,Email\nTest Alert,test@test.com\n'
            }
            success = rg.send_report_email(test_report, get_ist_date())
            return jsonify({
                'test': True,
                'mode': 'report_module_direct',
                'success': success,
                'report_to': rg.REPORT_EMAIL_TO,
                'report_from': rg.REPORT_EMAIL_FROM,
                'api_key_len': len(rg.SENDGRID_API_KEY) if rg.SENDGRID_API_KEY else 0
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            return jsonify({
                'test': True,
                'mode': 'report_module_direct',
                'error': str(e)
            })

    result = send_email_alert(subject, html_body)
    return jsonify({
        'test': True,
        'recipients_configured': len(ALERT_EMAILS),
        'recipients': ALERT_EMAILS,
        'result': result
    })


@app.route('/alert/email/status', methods=['GET'])
def email_alert_status():
    """
    Get email alert configuration and status.
    """
    _reset_daily_alert_counters()
    ist_now = get_ist_now()
    ist_hour = ist_now.hour

    # Get status for each alert type
    alert_types_status = {}
    for alert_type in ['stale', 'bot_joined', 'bot_left', 'meeting_ended', 'error']:
        if alert_type in _email_alert_state:
            can_send, wait_time = _can_send_alert(alert_type, 60)
            alert_types_status[alert_type] = {
                'count_today': _email_alert_state[alert_type].get('count_today', 0),
                'can_send': can_send,
                'wait_seconds': wait_time
            }

    # Determine which provider will be used
    brevo_configured = bool(os.environ.get('BREVO_API_KEY'))
    provider = 'brevo' if brevo_configured else ('resend' if RESEND_API_KEY else 'none')

    return jsonify({
        'configured': (brevo_configured or bool(RESEND_API_KEY)) and len(ALERT_EMAILS) > 0,
        'provider': provider,
        'brevo_configured': brevo_configured,
        'resend_configured': bool(RESEND_API_KEY),
        'recipients': ALERT_EMAILS,
        'recipient_count': len(ALERT_EMAILS),
        'from_email': ALERT_FROM_EMAIL,
        'settings': {
            'stale_threshold_seconds': ALERT_STALE_THRESHOLD_SECONDS,
            'rate_limit_seconds': ALERT_RATE_LIMIT_SECONDS,
            'alert_hours': '24/7'
        },
        'alert_types': alert_types_status,
        'current_hour_ist': ist_hour
    })


# ─────────────────────────────────────────────────────────
# WHATSAPP ALERT ENDPOINTS
# ─────────────────────────────────────────────────────────

@app.route('/alert/whatsapp/test', methods=['POST'])
def whatsapp_alert_test():
    """Send a test WhatsApp message to verify configuration."""
    ist_time = get_ist_now().strftime('%H:%M')
    message = f"✅ TEST: Zoom Monitoring WhatsApp Alerts Working!\n\n⏰ Time: {ist_time} IST\n\nYou will receive alerts when monitoring stops."

    # Bypass rate limiting for test
    global _whatsapp_last_sent
    _whatsapp_last_sent = 0

    result = send_whatsapp_alert(message)
    return jsonify(result)


@app.route('/alert/whatsapp/status', methods=['GET'])
def whatsapp_alert_status():
    """Get WhatsApp alert configuration status."""
    global _whatsapp_last_sent
    now = time.time()
    time_since = now - _whatsapp_last_sent if _whatsapp_last_sent > 0 else None

    waha_configured = bool(WAHA_API_URL and WHATSAPP_PHONE)
    callmebot_configured = bool(WHATSAPP_PHONE and WHATSAPP_APIKEY)

    return jsonify({
        'configured': waha_configured or callmebot_configured,
        'providers': {
            'waha': {
                'configured': waha_configured,
                'url': WAHA_API_URL[:30] + '...' if WAHA_API_URL and len(WAHA_API_URL) > 30 else WAHA_API_URL,
                'session': WAHA_SESSION
            },
            'callmebot': {
                'configured': callmebot_configured
            }
        },
        'phone': WHATSAPP_PHONE[:6] + '****' if WHATSAPP_PHONE else None,
        'rate_limit_seconds': WHATSAPP_RATE_LIMIT_SECONDS,
        'seconds_since_last': int(time_since) if time_since else None,
        'can_send': time_since is None or time_since >= WHATSAPP_RATE_LIMIT_SECONDS
    })


@app.route('/alert/check-fast', methods=['GET', 'POST'])
def fast_alert_check():
    """
    Fast health check for 30-second monitoring.
    Call this from Cloud Scheduler every 30 seconds during meeting hours.
    Only sends WhatsApp alerts (not email) to avoid spam.
    """
    today = get_ist_date()
    try:
        client = get_bq_client()
        query = f"""
        SELECT
          MAX(snapshot_time) as last_snapshot,
          TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(snapshot_time), SECOND) as seconds_since_last
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots_v2`
        WHERE event_date = '{today}'
        """
        result = list(client.query(query).result())
        row = result[0] if result else {}

        seconds_since = row.get('seconds_since_last', None)

        # If no data or stale > 60 seconds, send WhatsApp alert
        if seconds_since is None or seconds_since > 60:
            whatsapp_result = send_stale_whatsapp_alert(seconds_since or 0)
            return jsonify({
                'status': 'STALE',
                'seconds_since_last': seconds_since,
                'whatsapp_sent': whatsapp_result.get('success', False),
                'whatsapp_result': whatsapp_result
            })

        return jsonify({
            'status': 'HEALTHY',
            'seconds_since_last': seconds_since
        })

    except Exception as e:
        print(f"[FastCheck] Error: {e}")
        return jsonify({'status': 'ERROR', 'error': str(e)}), 500


# Rate limiter for signature error logging
_sig_error_state = {'count': 0, 'last_log': 0}

def validate_webhook_signature(request_obj):
    """
    Validate Zoom webhook signature using HMAC-SHA256.
    Returns (valid, error_message) tuple.
    """
    if not ZOOM_WEBHOOK_SECRET:
        # If no secret configured, skip validation (dev mode)
        print("[Webhook] WARNING: ZOOM_WEBHOOK_SECRET not set, skipping signature validation")
        return True, None

    signature = request_obj.headers.get('x-zm-signature', '')
    timestamp = request_obj.headers.get('x-zm-request-timestamp', '')

    if not signature or not timestamp:
        # SECURITY: missing headers used to be a free pass, which let anyone
        # who found the URL inject fake attendance events. Only
        # endpoint.url_validation is exempt — and the caller handles that
        # BEFORE calling this function.
        return False, "Missing signature headers"

    # Zoom signature format: v0=HMAC-SHA256(secret, timestamp + payload)
    raw_body = request_obj.data.decode('utf-8') if request_obj.data else ''
    message = f"v0:{timestamp}:{raw_body}"

    expected_sig = 'v0=' + hmac.new(
        key=ZOOM_WEBHOOK_SECRET.encode('utf-8'),
        msg=message.encode('utf-8'),
        digestmod=hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_sig):
        # Rate-limited logging - only log once per minute
        _sig_error_state['count'] += 1
        now = time.time()
        if now - _sig_error_state['last_log'] > 60:
            print(f"[Webhook] Signature mismatch: {_sig_error_state['count']} errors (likely duplicate webhook subscription)")
            _sig_error_state['last_log'] = now
            _sig_error_state['count'] = 0
        return False, "Invalid webhook signature"

    # Check timestamp freshness (within 5 minutes)
    try:
        ts = int(timestamp)
        now = int(time.time())
        if abs(now - ts) > 300:
            return False, "Webhook timestamp too old"
    except ValueError:
        return False, "Invalid timestamp format"

    return True, None


@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    """Main Zoom webhook endpoint"""
    if request.method == 'GET':
        return jsonify({'status': 'Webhook ready'})

    # Get raw data for logging
    try:
        data = request.json
    except Exception as e:
        print(f"[Webhook] ERROR: Failed to parse JSON: {e}")
        print(f"[Webhook] Raw body: {request.data[:500] if request.data else 'empty'}")
        return jsonify({'error': 'Invalid JSON'}), 400

    if not data:
        print(f"[Webhook] ERROR: Empty request body")
        return jsonify({'error': 'Empty body'}), 400

    event = data.get('event', '')

    # Validate signature FIRST — for every event INCLUDING url_validation
    # (Zoom signs those too). Answering url_validation unsigned would be an
    # HMAC oracle: an attacker could submit plainToken = "v0:<ts>:<body>"
    # and receive exactly the digest needed to forge that body's signature.
    valid, error = validate_webhook_signature(request)
    if not valid:
        return jsonify({'error': error}), 401

    # Handle URL validation (endpoint registration handshake)
    if event == 'endpoint.url_validation':
        plain_token = data.get('payload', {}).get('plainToken', '')
        encrypted_token = hmac.new(
            key=ZOOM_WEBHOOK_SECRET.encode('utf-8'),
            msg=plain_token.encode('utf-8'),
            digestmod=hashlib.sha256
        ).hexdigest()
        print(f"[Webhook] URL validation successful")
        return jsonify({
            'plainToken': plain_token,
            'encryptedToken': encrypted_token
        })

    print(f"\n{'='*60}")
    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] WEBHOOK EVENT: {event}")
    print(f"{'='*60}")

    # Log raw payload for debugging (first 500 chars)
    raw_str = json.dumps(data)
    if len(raw_str) > 500:
        print(f"[Webhook] Payload (truncated): {raw_str[:500]}...")
    else:
        print(f"[Webhook] Payload: {raw_str}")

    # Route events to handlers with error catching
    try:
        if event == 'meeting.participant_joined':
            handle_participant_joined(data)

        elif event == 'meeting.participant_left':
            handle_participant_left(data)

        elif event == 'meeting.participant_joined_breakout_room':
            handle_breakout_room_join(data)

        elif event == 'meeting.participant_left_breakout_room':
            handle_breakout_room_leave(data)

        elif event in ['meeting.participant_video_on', 'meeting.participant_video_started']:
            handle_camera_event(data, camera_on=True)

        elif event in ['meeting.participant_video_off', 'meeting.participant_video_stopped']:
            handle_camera_event(data, camera_on=False)

        elif event == 'meeting.ended':
            handle_meeting_ended(data)

        else:
            print(f"[Webhook] Unhandled event type: {event}")

    except Exception as e:
        print(f"[Webhook] ERROR handling {event}: {e}")
        import traceback
        traceback.print_exc()
        # Still return success to Zoom so it doesn't retry
        return jsonify({'status': 'error logged', 'event': event}), 200

    return jsonify({'status': 'success'})


# ==============================================================================
# CALIBRATION ENDPOINTS (For Zoom SDK App)
# ==============================================================================

@app.route('/calibration/start', methods=['POST'])
def calibration_start():
    """Start calibration session with SEQUENCE-BASED matching and BigQuery persistence"""
    data = request.json or {}
    meeting_id = data.get('meeting_id')
    meeting_uuid = data.get('meeting_uuid')
    force_restart = data.get('force_restart', False)  # Force restart even if incomplete exists

    # Calibration participant info (for "Move Myself" mode)
    calibration_mode = data.get('calibration_mode', 'scout_bot')
    calibration_participant_name = data.get('calibration_participant_name', '')
    calibration_participant_uuid = data.get('calibration_participant_uuid', '')

    # SEQUENCE-BASED MATCHING: Get ordered room list
    # Frontend sends rooms in the order they will be visited
    room_sequence = data.get('room_sequence', [])  # [{room_name, room_uuid}, ...]

    if not meeting_id:
        return jsonify({'error': 'meeting_id required'}), 400

    # RESUME SUPPORT (positional matching seed)
    # Frontend sends start_from_index (0 = fresh start). When provided it is
    # authoritative: calibration_next_index is seeded with it so the FIRST
    # webhook after resume matches room #start_from_index.
    resume_from = 0
    start_from_index = data.get('start_from_index')
    if start_from_index is not None:
        try:
            resume_from = max(0, int(start_from_index))
        except (TypeError, ValueError):
            resume_from = 0
        if room_sequence and resume_from >= len(room_sequence):
            print(f"[Calibration] start_from_index {resume_from} out of range ({len(room_sequence)} rooms) - starting fresh")
            resume_from = 0
        elif resume_from > 0:
            print(f"[Calibration] Frontend requested RESUME from room {resume_from + 1}")
    else:
        # Legacy clients: auto-resume from persisted state
        existing_state = load_calibration_state(meeting_id)
        if existing_state and not force_restart:
            if existing_state.get('calibration_in_progress') and not existing_state.get('completed'):
                # Incomplete calibration found - can resume
                resume_from = existing_state.get('current_room_index', 0)
                total_rooms = existing_state.get('total_rooms', 0)
                print(f"[Calibration] RESUME available: {resume_from}/{total_rooms} rooms completed")

                # If room sequence matches, we can resume
                if len(room_sequence) == total_rooms:
                    print(f"[Calibration] Room count matches - resuming from room {resume_from + 1}")
                else:
                    print(f"[Calibration] Room count changed ({len(room_sequence)} vs {total_rooms}) - starting fresh")
                    resume_from = 0

    # Reset state for new calibration
    meeting_state.set_meeting(meeting_id, meeting_uuid)
    meeting_state.calibration_complete = False
    meeting_state.calibration_in_progress = True
    meeting_state.pending_room_moves = []  # Legacy, kept for compatibility

    # Clear sequence state for clean start
    meeting_state.calibration_sequence = []
    meeting_state.calibration_next_index = 0
    print(f"[Calibration] State reset: next_index=0, sequence cleared")

    # Store calibration participant info
    meeting_state.calibration_mode = calibration_mode
    meeting_state.calibration_participant_name = calibration_participant_name
    meeting_state.calibration_participant_uuid = calibration_participant_uuid

    # SINGLE SOURCE OF TRUTH: Use the room sequence sent by frontend.
    # Frontend sorts SDK rooms by prefix (1.1, 1.2, ..., 2.0, 3.1, ...) and sends them here.
    # Backend uses this EXACT list for position-based matching.
    # NO hardcoded FIXED_ROOM_SEQUENCE - it can get out of sync with actual Zoom rooms.
    meeting_state.calibration_sequence = []
    meeting_state.calibration_next_index = resume_from

    if room_sequence and len(room_sequence) > 0:
        print(f"[Calibration] Using frontend SDK room sequence ({len(room_sequence)} rooms)")
        for idx, room in enumerate(room_sequence):
            room_name = room.get('room_name') or room.get('name') or room.get('breakoutRoomName')
            room_uuid = room.get('room_uuid') or room.get('uuid') or room.get('breakoutRoomId')
            if room_name:
                meeting_state.calibration_sequence.append({
                    'room_name': room_name,
                    'room_index': idx,
                    'sdk_uuid': room_uuid,
                    'webhook_uuid': None,
                    'matched': False
                })
                if room_uuid:
                    meeting_state.add_room_mapping(room_uuid, room_name)
    elif USE_FIXED_SEQUENCE and FIXED_ROOM_SEQUENCE:
        # Fallback ONLY if frontend sends no rooms (shouldn't happen)
        print(f"[Calibration] WARNING: No frontend rooms, falling back to FIXED_ROOM_SEQUENCE ({len(FIXED_ROOM_SEQUENCE)} rooms)")
        for i, room_name in enumerate(FIXED_ROOM_SEQUENCE):
            meeting_state.calibration_sequence.append({
                'room_name': room_name,
                'room_index': i,
                'sdk_uuid': None,
                'webhook_uuid': None,
                'matched': False
            })

    # SAVE CALIBRATION STATE TO BIGQUERY (persistence)
    state_data = {
        'calibration_in_progress': True,
        'calibration_mode': calibration_mode,
        'calibration_participant_name': calibration_participant_name or SCOUT_BOT_NAME,
        'current_room_index': resume_from,
        'total_rooms': len(meeting_state.calibration_sequence),
        'room_sequence': [r['room_name'] for r in meeting_state.calibration_sequence],
        'started_at': datetime.utcnow().isoformat(),
        'completed': False,
        'completed_at': ''
    }
    save_calibration_state(meeting_id, meeting_uuid, state_data)

    print(f"\n{'='*60}")
    print(f"[Calibration] STARTED for meeting {meeting_id}")
    print(f"[Calibration] Mode: {calibration_mode}")
    print(f"[Calibration] SEQUENCE-BASED MATCHING ENABLED")
    if resume_from > 0:
        print(f"[Calibration] RESUMING from room {resume_from + 1}")
    print(f"[Calibration] Room sequence ({len(meeting_state.calibration_sequence)} rooms):")
    for i, room in enumerate(meeting_state.calibration_sequence):
        status = "✓ DONE" if i < resume_from else ""
        print(f"  Position {i+1}: {room['room_name']} {status}")
    if calibration_mode == 'self':
        print(f"[Calibration] Participant: {calibration_participant_name}")
    else:
        print(f"[Calibration] Using Scout Bot: {SCOUT_BOT_NAME}")
    print(f"[Calibration] State saved to BigQuery")
    print(f"{'='*60}\n")

    return jsonify({
        'success': True,
        'message': 'Calibration started with sequence-based matching',
        'meeting_id': meeting_id,
        'calibration_mode': calibration_mode,
        'calibration_participant': calibration_participant_name or SCOUT_BOT_NAME,
        'room_count': len(meeting_state.calibration_sequence),
        'sequence_matching': True,
        'resume_from': resume_from,
        'persisted': True
    })


@app.route('/calibration/mapping', methods=['POST'])
def calibration_mapping():
    """Receive room mappings from Zoom SDK App"""
    data = request.json or {}
    meeting_id = data.get('meeting_id')
    meeting_uuid = data.get('meeting_uuid')
    room_mapping = data.get('room_mapping', [])

    if not meeting_id or not room_mapping:
        return jsonify({'error': 'meeting_id and room_mapping required'}), 400

    # Update meeting state
    meeting_state.set_meeting(meeting_id, meeting_uuid)

    # Store mappings in memory and track pending room moves for webhook UUID learning
    for room in room_mapping:
        room_uuid = room.get('room_uuid', '')
        room_name = room.get('room_name', '')
        if room_uuid and room_name:
            meeting_state.add_room_mapping(room_uuid, room_name)
            # Track the current room Scout Bot is moving to
            meeting_state.scout_bot_current_room = room_name
            # Add to pending moves queue with timestamp (for matching webhooks)
            move_time = datetime.utcnow()
            meeting_state.pending_room_moves.append({
                'room_name': room_name,
                'sdk_uuid': room_uuid,
                'timestamp': move_time,
                'matched': False
            })
            print(f"[Calibration] Scout Bot moving to: {room_name} (pending webhook match)")

    # Store in BigQuery
    today = get_ist_date()
    bq_rows = [{
        'mapping_id': str(uuid_lib.uuid4()),
        'meeting_id': str(meeting_id),
        'meeting_uuid': meeting_uuid or '',
        'room_uuid': room.get('room_uuid', ''),
        'room_name': room.get('room_name', ''),
        'room_index': room.get('room_index', 0),
        'mapping_date': today,
        'mapped_at': datetime.utcnow().isoformat(),
        'source': 'zoom_sdk_app'
    } for room in room_mapping if room.get('room_uuid') and room.get('room_name')]

    if bq_rows:
        insert_room_mappings(bq_rows)

    print(f"[Calibration] Received {len(room_mapping)} room mappings, {len(meeting_state.pending_room_moves)} pending webhook matches")
    for room in room_mapping[:5]:
        print(f"  - {room.get('room_name')} = {room.get('room_uuid', '')[:20]}...")
    if len(room_mapping) > 5:
        print(f"  ... and {len(room_mapping) - 5} more")

    return jsonify({
        'success': True,
        'mappings_received': len(room_mapping),
        'total_stored': len(meeting_state.uuid_to_name),
        'pending_webhook_matches': len([m for m in meeting_state.pending_room_moves if not m['matched']])
    })


@app.route('/calibration/pending', methods=['GET'])
def calibration_pending():
    """
    Check if a room's webhook has been received.
    Used by React app to poll and wait for webhook confirmation.
    Pure position-based: frontend waits for each webhook before moving to next room.
    """
    room_name = request.args.get('room_name')
    sequence = meeting_state.calibration_sequence

    if not sequence:
        return jsonify({
            'matched': False,
            'total_pending': 0,
            'total_matched': 0,
            'error': 'No calibration sequence active'
        })

    total_matched = len([m for m in sequence if m.get('matched')])
    total_pending = len(sequence) - total_matched

    # If room_name is specified, check if that specific room is matched
    if room_name:
        room_matched = any(
            m.get('room_name') == room_name and m.get('matched')
            for m in sequence
        )
        return jsonify({
            'room_name': room_name,
            'matched': room_matched,
            'total_pending': total_pending,
            'total_matched': total_matched
        })

    # Return full status
    pending_moves = [{
        'room_name': room.get('room_name'),
        'matched': room.get('matched', False),
        'webhook_uuid': room.get('webhook_uuid') if room.get('matched') else None
    } for room in sequence]

    return jsonify({
        'pending_moves': pending_moves,
        'total_pending': total_pending,
        'total_matched': total_matched
    })


@app.route('/calibration/complete', methods=['POST'])
def calibration_complete():
    """Mark calibration as complete"""
    data = request.json or {}
    meeting_id = data.get('meeting_id')
    success = data.get('success', True)
    total_rooms = data.get('total_rooms', 0)
    mapped_rooms = data.get('mapped_rooms', 0)

    meeting_state.calibration_complete = success
    meeting_state.calibrated_at = datetime.utcnow().isoformat()
    meeting_state.calibration_in_progress = False

    # Count matches from sequence
    sequence = meeting_state.calibration_sequence
    webhook_matches = len([m for m in sequence if m.get('matched')])
    unmatched = len([m for m in sequence if not m.get('matched')])

    # MARK CALIBRATION COMPLETE IN BIGQUERY
    if meeting_id or meeting_state.meeting_id:
        complete_calibration_state(meeting_id or meeting_state.meeting_id)

    print(f"\n{'='*60}")
    print(f"[Calibration] COMPLETE - {mapped_rooms}/{total_rooms} rooms")
    print(f"[Calibration] Position-based matching: {webhook_matches} matched, {unmatched} unmatched")
    print(f"[Calibration] Total mappings in memory: {len(meeting_state.uuid_to_name)}")
    if sequence:
        for i, room in enumerate(sequence):
            status = "MATCHED" if room.get('matched') else "PENDING"
            uuid_preview = room.get('webhook_uuid', '')[:20] + '...' if room.get('webhook_uuid') else 'N/A'
            print(f"  {i+1}. {room.get('room_name')}: {status} (UUID: {uuid_preview})")
    print(f"{'='*60}\n")

    return jsonify({
        'success': True,
        'message': 'Calibration complete',
        'webhook_uuid_matches': webhook_matches,
        'unmatched_rooms': unmatched,
        'persisted': True
    })


@app.route('/calibration/verify', methods=['POST'])
def calibration_verify():
    """
    Frontend calls this AFTER webhook confirmed for a room.
    With position-based matching, the mapping is already saved to BigQuery
    when the webhook arrives. This endpoint just confirms it.
    """
    data = request.json or {}
    room_name = data.get('room_name')

    if not room_name:
        return jsonify({'error': 'room_name required'}), 400

    # Find the matched entry in calibration sequence
    matched_entry = None
    for entry in meeting_state.calibration_sequence:
        if entry.get('room_name') == room_name and entry.get('matched'):
            matched_entry = entry
            break

    if not matched_entry:
        print(f"[Calibration] Verify: no match found for {room_name}")
        return jsonify({
            'success': False,
            'error': f'No match found for room: {room_name}'
        }), 404

    webhook_uuid = matched_entry.get('webhook_uuid', '')
    print(f"[Calibration] VERIFIED: {room_name} = {webhook_uuid[:20]}...")

    return jsonify({
        'success': True,
        'room_name': room_name,
        'webhook_uuid': webhook_uuid,
        'verified': True
    })


@app.route('/calibration/status', methods=['GET'])
def calibration_status():
    """Get current calibration status - supports resume functionality"""
    meeting_id = request.args.get('meeting_id') or meeting_state.meeting_id

    # First check in-memory state
    in_progress = meeting_state.calibration_in_progress
    current_index = meeting_state.calibration_next_index
    total_rooms = len(meeting_state.calibration_sequence)

    # If in-memory state is empty, check BigQuery for persisted state
    if not in_progress and meeting_id:
        bq_state = load_calibration_state(meeting_id)
        if bq_state and bq_state.get('calibration_in_progress') and not bq_state.get('completed'):
            in_progress = True
            current_index = bq_state.get('current_room_index', 0)
            total_rooms = bq_state.get('total_rooms', 66)
            print(f"[calibration/status] Found resumable state in BQ: index={current_index}/{total_rooms}")

    return jsonify({
        'meeting_id': meeting_state.meeting_id,
        'calibration_complete': meeting_state.calibration_complete,
        'calibrated_at': meeting_state.calibrated_at,
        'rooms_mapped': len(meeting_state.uuid_to_name),
        'room_names': list(meeting_state.name_to_uuid.keys())[:20],
        # Resume support fields
        'calibration_in_progress': in_progress,
        'current_room_index': current_index,
        'total_rooms': total_rooms
    })


@app.route('/calibration/correct', methods=['POST'])
def calibration_correct():
    """
    Manual trigger for timestamp-based calibration correction.
    Call this after calibration to fix any out-of-order webhook issues.
    """
    data = request.json or {}
    meeting_id = data.get('meeting_id') or meeting_state.meeting_id
    target_date = data.get('date') or get_ist_date()

    result = correct_calibration_by_timestamp(meeting_id, target_date)

    if result.get('success'):
        return jsonify(result)
    else:
        return jsonify(result), 400


@app.route('/calibration/fix-by-index', methods=['POST'])
def calibration_fix_by_index():
    """
    Fix room_name values in BigQuery based on room_index and FIXED_ROOM_SEQUENCE.
    This corrects any mismatched room names by using the authoritative sequence.

    Use this after calibration if validation shows mismatches.
    """
    data = request.json or {}
    try:
        target_date = validate_date_format(data.get('date'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    dry_run = data.get('dry_run', True)  # Default to dry run for safety

    try:
        client = bigquery.Client(project=GCP_PROJECT_ID)

        # First, get current mappings to identify what needs fixing
        query = f"""
        SELECT
            mapping_id,
            room_uuid,
            room_name,
            room_index,
            source
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
        WHERE mapping_date = @target_date
          AND room_index IS NOT NULL
          AND room_index >= 0
          AND room_index < {len(FIXED_ROOM_SEQUENCE)}
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("target_date", "STRING", target_date)
            ]
        )
        results = list(client.query(query, job_config=job_config).result())

        fixes_needed = []
        already_correct = 0

        for row in results:
            room_index = row.room_index
            stored_name = row.room_name
            expected_name = FIXED_ROOM_SEQUENCE[room_index]

            if stored_name != expected_name:
                fixes_needed.append({
                    'mapping_id': row.mapping_id,
                    'room_uuid': row.room_uuid,
                    'room_index': room_index,
                    'old_name': stored_name,
                    'new_name': expected_name
                })
            else:
                already_correct += 1

        if dry_run:
            return jsonify({
                'dry_run': True,
                'date': target_date,
                'fixes_needed': len(fixes_needed),
                'already_correct': already_correct,
                'fixes_preview': fixes_needed[:20],  # Show first 20
                'message': 'Set dry_run=false to apply fixes'
            })

        # Apply fixes using MERGE/UPDATE
        if fixes_needed:
            # Build CASE statement for updates
            updates_applied = 0
            for fix in fixes_needed:
                update_query = f"""
                UPDATE `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
                SET room_name = '{fix['new_name'].replace("'", "''")}'
                WHERE mapping_id = '{fix['mapping_id']}'
                """
                try:
                    client.query(update_query).result()
                    updates_applied += 1
                except Exception as e:
                    print(f"[FixByIndex] Error updating {fix['mapping_id']}: {e}")

            # Also update in-memory state
            for fix in fixes_needed:
                if fix['room_uuid'] in meeting_state.uuid_to_name:
                    meeting_state.uuid_to_name[fix['room_uuid']] = fix['new_name']

            return jsonify({
                'success': True,
                'date': target_date,
                'fixes_applied': updates_applied,
                'fixes_needed': len(fixes_needed),
                'already_correct': already_correct,
                'message': f'Fixed {updates_applied} room names based on FIXED_ROOM_SEQUENCE'
            })
        else:
            return jsonify({
                'success': True,
                'date': target_date,
                'fixes_needed': 0,
                'already_correct': already_correct,
                'message': 'All room names already match FIXED_ROOM_SEQUENCE'
            })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'error': str(e),
            'date': target_date
        }), 500


@app.route('/calibration/fixed-sequence', methods=['GET'])
def get_fixed_sequence():
    """
    Get the current FIXED_ROOM_SEQUENCE configuration.
    This is the master list of room names in the order Scout Bot visits them.
    """
    return jsonify({
        'use_fixed_sequence': USE_FIXED_SEQUENCE,
        'total_rooms': len(FIXED_ROOM_SEQUENCE),
        'sequence': FIXED_ROOM_SEQUENCE
    })


@app.route('/calibration/validate', methods=['GET', 'POST'])
def calibration_validate():
    """
    Validate mapping accuracy by comparing multiple sources:
    1. FIXED_ROOM_SEQUENCE (authoritative)
    2. BigQuery room_mappings (calibration data)
    3. Cross-reference room_index with room_name

    Returns discrepancies and accuracy metrics.
    """
    data = request.json or {}
    try:
        target_date = validate_date_format(data.get('date') or request.args.get('date'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    try:
        client = bigquery.Client(project=GCP_PROJECT_ID)

        # Get all mappings for target date
        query = f"""
        SELECT
            room_uuid,
            room_name,
            room_index,
            source,
            mapping_date,
            mapped_at
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
        WHERE mapping_date = @target_date
        ORDER BY room_index, mapped_at
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("target_date", "STRING", target_date)
            ]
        )
        results = list(client.query(query, job_config=job_config).result())

        # Analyze mappings
        validation_results = {
            'date': target_date,
            'total_mappings': len(results),
            'fixed_sequence_total': len(FIXED_ROOM_SEQUENCE),
            'matches': [],
            'mismatches': [],
            'missing_from_calibration': [],
            'extra_in_calibration': [],
            'accuracy_percent': 0
        }

        # Track which rooms from fixed sequence were found
        found_indices = set()
        match_count = 0

        for row in results:
            room_uuid = row.room_uuid
            stored_name = row.room_name
            room_index = row.room_index
            source = row.source

            # Get expected name from FIXED_ROOM_SEQUENCE
            expected_name = None
            if room_index is not None and 0 <= room_index < len(FIXED_ROOM_SEQUENCE):
                expected_name = FIXED_ROOM_SEQUENCE[room_index]
                found_indices.add(room_index)

            entry = {
                'room_uuid': room_uuid[:20] + '...' if room_uuid and len(room_uuid) > 20 else room_uuid,
                'room_index': room_index,
                'stored_name': stored_name,
                'expected_name': expected_name,
                'source': source
            }

            if expected_name and stored_name == expected_name:
                validation_results['matches'].append(entry)
                match_count += 1
            elif expected_name and stored_name != expected_name:
                entry['issue'] = f"Name mismatch: stored '{stored_name}' vs expected '{expected_name}'"
                validation_results['mismatches'].append(entry)
            elif room_index is None or room_index >= len(FIXED_ROOM_SEQUENCE):
                entry['issue'] = f"Invalid room_index: {room_index}"
                validation_results['extra_in_calibration'].append(entry)

        # Find rooms in FIXED_ROOM_SEQUENCE not in calibration
        for idx, name in enumerate(FIXED_ROOM_SEQUENCE):
            if idx not in found_indices:
                validation_results['missing_from_calibration'].append({
                    'room_index': idx,
                    'room_name': name,
                    'issue': 'Not found in calibration'
                })

        # Calculate accuracy
        if len(FIXED_ROOM_SEQUENCE) > 0:
            validation_results['accuracy_percent'] = round(
                (match_count / len(FIXED_ROOM_SEQUENCE)) * 100, 1
            )

        # Summary
        validation_results['summary'] = {
            'correct': match_count,
            'mismatched': len(validation_results['mismatches']),
            'missing': len(validation_results['missing_from_calibration']),
            'extra': len(validation_results['extra_in_calibration'])
        }

        return jsonify(validation_results)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'error': str(e),
            'date': target_date
        }), 500


@app.route('/calibration/health', methods=['GET', 'POST'])
def calibration_health():
    """
    Health check for calibration status.
    Called by Cloud Scheduler at 9:30 AM to verify calibration is complete.
    Sends email alert if calibration is incomplete or failed.
    """
    send_alert = request.args.get('alert', 'true').lower() == 'true'
    target_date = request.args.get('date') or get_ist_date()

    try:
        # Load calibration state from BigQuery
        state = load_calibration_state(date=target_date)

        # Count room mappings with sequence_calibration source
        client = get_bq_client()
        mapping_query = f"""
        SELECT
            COUNT(*) as total_mappings,
            COUNTIF(source = 'sequence_calibration') as sequence_mappings,
            COUNTIF(source = 'zoom_sdk_app') as sdk_mappings
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
        WHERE mapping_date = @target_date
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("target_date", "STRING", target_date),
            ]
        )
        results = list(client.query(mapping_query, job_config=job_config).result())

        total_mappings = results[0].total_mappings if results else 0
        sequence_mappings = results[0].sequence_mappings if results else 0
        sdk_mappings = results[0].sdk_mappings if results else 0

        # Determine health status
        calibration_started = state is not None
        calibration_completed = state.get('completed', False) if state else False
        calibration_in_progress = state.get('calibration_in_progress', False) if state else False
        current_room = state.get('current_room_index', 0) if state else 0
        total_rooms = state.get('total_rooms', 0) if state else 0

        # Health criteria:
        # - HEALTHY: calibration completed AND sequence_mappings > 0
        # - WARNING: calibration in progress (not yet complete)
        # - CRITICAL: calibration started but failed OR no calibration at all
        if calibration_completed and sequence_mappings > 0:
            health_status = 'HEALTHY'
            message = f'Calibration complete: {sequence_mappings} rooms mapped with webhook UUIDs'
        elif calibration_in_progress:
            health_status = 'WARNING'
            message = f'Calibration in progress: {current_room}/{total_rooms} rooms done'
        elif calibration_started and not calibration_completed:
            health_status = 'CRITICAL'
            message = f'Calibration incomplete: {current_room}/{total_rooms} rooms done, then stopped'
        elif total_mappings > 0 and sequence_mappings == 0:
            health_status = 'WARNING'
            message = f'No webhook UUID mappings - only SDK mappings ({sdk_mappings}). Reports may show Room-XXXXX'
        else:
            health_status = 'CRITICAL'
            message = 'No calibration data for today'

        # Alerting moved to GCP Cloud Monitoring (no SendGrid alerts)
        alert_sent = False

        response = {
            'date': target_date,
            'health_status': health_status,
            'message': message,
            'calibration': {
                'started': calibration_started,
                'completed': calibration_completed,
                'in_progress': calibration_in_progress,
                'current_room': current_room,
                'total_rooms': total_rooms
            },
            'mappings': {
                'total': total_mappings,
                'sequence_calibration': sequence_mappings,
                'sdk_only': sdk_mappings
            },
            'alert_sent': alert_sent
        }

        print(f"[CalibrationHealth] {health_status}: {message}")
        return jsonify(response), 200 if health_status == 'HEALTHY' else 503

    except Exception as e:
        print(f"[CalibrationHealth] Error: {e}")
        traceback.print_exc()
        return jsonify({
            'date': target_date,
            'health_status': 'ERROR',
            'message': str(e),
            'alert_sent': False
        }), 500


@app.route('/debug/bq-mappings', methods=['GET'])
def debug_bq_mappings():
    """Debug endpoint to check BigQuery mappings directly"""
    try:
        client = get_bq_client()
        today = get_ist_date()
        yesterday = (get_ist_now() - timedelta(days=1)).strftime('%Y-%m-%d')

        # Query for today AND yesterday (timezone edge case)
        query = f"""
        SELECT mapping_date, room_uuid, room_name, meeting_id, source, mapped_at
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
        WHERE mapping_date IN (@today, @yesterday)
        ORDER BY mapped_at DESC
        LIMIT 100
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("today", "STRING", today),
                bigquery.ScalarQueryParameter("yesterday", "STRING", yesterday)
            ]
        )
        results = list(client.query(query, job_config=job_config).result())

        mappings = []
        for row in results:
            mappings.append({
                'date': row.mapping_date,
                'room_name': row.room_name,
                'room_uuid': row.room_uuid[:20] + '...' if len(row.room_uuid) > 20 else row.room_uuid,
                'meeting_id': row.meeting_id,
                'source': row.source,
                'mapped_at': row.mapped_at
            })

        # Also count total mappings ever
        count_query = f"""
        SELECT mapping_date, COUNT(*) as cnt
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
        GROUP BY mapping_date
        ORDER BY mapping_date DESC
        LIMIT 10
        """
        count_results = list(client.query(count_query).result())
        date_counts = {str(row.mapping_date): row.cnt for row in count_results}

        return jsonify({
            'today_utc': today,
            'yesterday_utc': yesterday,
            'in_memory_count': len(meeting_state.uuid_to_name),
            'in_memory_rooms': list(meeting_state.name_to_uuid.keys())[:30],
            'bigquery_mappings': mappings,
            'bigquery_count': len(mappings),
            'mappings_by_date': date_counts
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/calibration/reload', methods=['POST'])
def calibration_reload():
    """Force reload mappings from BigQuery"""
    try:
        today = get_ist_date()
        count = meeting_state.load_mappings_from_bigquery(today)
        return jsonify({
            'success': True,
            'mappings_loaded': count,
            'in_memory_count': len(meeting_state.uuid_to_name),
            'room_names': list(meeting_state.name_to_uuid.keys())[:30]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/calibration/abort', methods=['POST'])
def calibration_abort():
    """
    Abort calibration and DELETE all mappings saved during this session.
    Called when calibration fails midway to prevent duplicate/partial records.
    This ensures a clean state for the next calibration attempt.
    """
    data = request.json or {}
    meeting_id = data.get('meeting_id') or meeting_state.meeting_id
    today = get_ist_date()

    print(f"\n{'='*60}")
    print(f"[Calibration] ABORT requested for meeting {meeting_id}")
    print(f"{'='*60}\n")

    deleted_count = 0

    if meeting_id:
        try:
            client = get_bq_client()
            # Delete ALL calibration mappings for this meeting + today
            # This removes sequential_calibration, pending_move_calibration, and zoom_sdk_app
            delete_query = f"""
            DELETE FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
            WHERE meeting_id = @meeting_id
              AND mapping_date = @today
              AND source IN ('sequential_calibration', 'pending_move_calibration', 'zoom_sdk_app')
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("meeting_id", "STRING", str(meeting_id)),
                    bigquery.ScalarQueryParameter("today", "STRING", today),
                ]
            )
            job = client.query(delete_query, job_config=job_config)
            job.result()  # Wait for completion
            deleted_count = job.num_dml_affected_rows or 0
            print(f"[Calibration] Deleted {deleted_count} calibration mappings from BigQuery")
        except Exception as e:
            print(f"[Calibration] Error deleting mappings: {e}")

    # Reset in-memory calibration state
    meeting_state.calibration_in_progress = False
    meeting_state.calibration_complete = False
    meeting_state.calibration_sequence = []
    meeting_state.calibration_next_index = 0
    meeting_state.pending_room_moves = []
    meeting_state.scout_bot_current_room = None
    meeting_state.uuid_to_name = {}
    meeting_state.name_to_uuid = {}

    # Mark as aborted in BigQuery state
    if meeting_id:
        try:
            complete_calibration_state(meeting_id)
        except Exception:
            pass

    print(f"[Calibration] Abort complete - all session data cleared")

    return jsonify({
        'success': True,
        'message': 'Calibration aborted - all session mappings deleted',
        'deleted_mappings': deleted_count
    })


@app.route('/calibration/reset', methods=['POST'])
def calibration_reset():
    """
    Full reset of calibration state.
    Call this to stop ongoing calibration and start fresh.
    """
    data = request.json or {}
    clear_bigquery = data.get('clear_bigquery', False)
    meeting_id = data.get('meeting_id') or meeting_state.meeting_id

    print(f"\n{'='*60}")
    print(f"[Calibration] RESET requested")
    print(f"[Calibration] Clear BigQuery: {clear_bigquery}")
    print(f"{'='*60}\n")

    # Reset in-memory state COMPLETELY
    old_meeting_id = meeting_state.meeting_id
    meeting_state.calibration_in_progress = False
    meeting_state.calibration_complete = False
    meeting_state.calibration_sequence = []
    meeting_state.calibration_next_index = 0
    meeting_state.pending_room_moves = []
    meeting_state.scout_bot_current_room = None
    # CRITICAL: Clear the actual room mappings!
    meeting_state.uuid_to_name = {}
    meeting_state.name_to_uuid = {}
    print(f"[Calibration] Cleared all in-memory mappings")

    # Optionally clear BigQuery mappings for ALL dates (not just today)
    if clear_bigquery and meeting_id:
        try:
            client = get_bq_client()
            # Delete ALL mappings for this meeting, not just today
            delete_query = f"""
            DELETE FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
            WHERE meeting_id = @meeting_id
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("meeting_id", "STRING", str(meeting_id)),
                ]
            )
            client.query(delete_query, job_config=job_config).result()
            print(f"[Calibration] Cleared ALL BigQuery mappings for meeting {meeting_id}")
        except Exception as e:
            print(f"[Calibration] Error clearing BigQuery: {e}")

    return jsonify({
        'success': True,
        'message': 'Calibration reset complete',
        'previous_meeting_id': old_meeting_id,
        'bigquery_cleared': clear_bigquery
    })


@app.route('/calibration/live-rooms', methods=['GET'])
def calibration_live_rooms():
    """
    Get current breakout room participant data from BigQuery events.
    This shows who is currently in each room based on join/leave events.
    Used for manual verification of room mappings.
    """
    meeting_id = request.args.get('meeting_id') or meeting_state.meeting_id
    today = get_ist_date()

    if not meeting_id:
        return jsonify({'error': 'No meeting_id available'}), 400

    try:
        client = get_bq_client()

        # Query to get current room occupancy
        # A participant is "in" a room if their last event for that room was a join
        query = f"""
        WITH latest_events AS (
            SELECT
                participant_name,
                participant_email,
                room_uuid,
                room_name,
                event_type,
                event_timestamp,
                ROW_NUMBER() OVER (
                    PARTITION BY participant_id, room_uuid
                    ORDER BY event_timestamp DESC
                ) as rn
            FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_EVENTS_TABLE}`
            WHERE event_date = @today
              AND meeting_id = @meeting_id
              AND event_type IN ('breakout_room_joined', 'breakout_room_left')
        ),
        current_in_rooms AS (
            SELECT
                room_uuid,
                room_name,
                participant_name,
                participant_email,
                event_timestamp as joined_at
            FROM latest_events
            WHERE rn = 1 AND event_type = 'breakout_room_joined'
        )
        SELECT
            room_uuid,
            room_name,
            ARRAY_AGG(STRUCT(participant_name, participant_email, joined_at)) as participants
        FROM current_in_rooms
        GROUP BY room_uuid, room_name
        ORDER BY room_name
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("today", "STRING", today),
                bigquery.ScalarQueryParameter("meeting_id", "STRING", str(meeting_id)),
            ]
        )

        results = list(client.query(query, job_config=job_config).result())

        # First get mapping status to fix room names
        mapping_query = f"""
        SELECT DISTINCT room_uuid, room_name, source
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
        WHERE mapping_date = @today AND meeting_id = @meeting_id
        """
        mapping_results = list(client.query(mapping_query, job_config=job_config).result())
        mapped_uuids = {r.room_uuid: {'name': r.room_name, 'source': r.source} for r in mapping_results}

        rooms = []
        for row in results:
            room_uuid = row.room_uuid
            # Use mapped room name if available, otherwise fall back to stored name
            if room_uuid in mapped_uuids:
                room_name = mapped_uuids[room_uuid]['name']
            else:
                # Also check in-memory mappings
                room_name = meeting_state.get_room_name(room_uuid) or row.room_name

            rooms.append({
                'room_uuid': room_uuid,
                'room_name': room_name,
                'participants': [
                    {
                        'name': p['participant_name'],
                        'email': p['participant_email'],
                        'joined_at': p['joined_at']
                    }
                    for p in row.participants
                ],
                'participant_count': len(row.participants)
            })

        return jsonify({
            'success': True,
            'meeting_id': meeting_id,
            'date': today,
            'rooms': rooms,
            'total_rooms': len(rooms),
            'mapped_rooms': len(mapped_uuids),
            'mapping_status': mapped_uuids,
            'calibration_in_progress': meeting_state.calibration_in_progress,
            'calibration_sequence_progress': f"{meeting_state.calibration_next_index}/{len(meeting_state.calibration_sequence)}"
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/calibration/recalibrate-room', methods=['POST'])
def calibration_recalibrate_room():
    """
    Re-calibrate a specific room.
    Used when a room mapping is incorrect - delete old mapping and prepare for new webhook.
    """
    data = request.json or {}
    room_name = data.get('room_name')
    room_uuid = data.get('room_uuid')  # SDK UUID
    meeting_id = data.get('meeting_id') or meeting_state.meeting_id

    if not room_name:
        return jsonify({'error': 'room_name required'}), 400

    today = get_ist_date()

    print(f"[Calibration] Re-calibrating room: {room_name}")

    try:
        # Step 1: Delete existing mappings for this room name
        client = get_bq_client()
        delete_query = f"""
        DELETE FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
        WHERE mapping_date = @today
          AND meeting_id = @meeting_id
          AND room_name = @room_name
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("today", "STRING", today),
                bigquery.ScalarQueryParameter("meeting_id", "STRING", str(meeting_id)),
                bigquery.ScalarQueryParameter("room_name", "STRING", room_name),
            ]
        )
        client.query(delete_query, job_config=job_config).result()
        print(f"[Calibration] Deleted old mappings for: {room_name}")

        # Step 2: Clear from in-memory state
        with meeting_state._lock:
            # Remove from uuid_to_name if exists
            uuids_to_remove = [uuid for uuid, name in meeting_state.uuid_to_name.items() if name == room_name]
            for uuid in uuids_to_remove:
                del meeting_state.uuid_to_name[uuid]
            # Remove from name_to_uuid
            if room_name in meeting_state.name_to_uuid:
                del meeting_state.name_to_uuid[room_name]

        # Step 3: Find room index in FIXED_ROOM_SEQUENCE
        room_index = None
        if USE_FIXED_SEQUENCE and FIXED_ROOM_SEQUENCE:
            for i, name in enumerate(FIXED_ROOM_SEQUENCE):
                if name == room_name:
                    room_index = i
                    break

        # Step 4: Set up for single room calibration
        # Add to pending_room_moves so next webhook from scout bot gets matched
        meeting_state.pending_room_moves.append({
            'room_name': room_name,
            'sdk_uuid': room_uuid,
            'timestamp': datetime.utcnow(),
            'matched': False,
            'recalibration': True
        })

        # Set calibration in progress (but for single room)
        meeting_state.calibration_in_progress = True
        meeting_state.scout_bot_current_room = room_name

        print(f"[Calibration] Ready for re-calibration webhook for: {room_name}")

        return jsonify({
            'success': True,
            'message': f'Room "{room_name}" ready for re-calibration. Move Scout Bot to this room now.',
            'room_name': room_name,
            'room_index': room_index,
            'instructions': [
                '1. Move Scout Bot to this specific room',
                '2. Wait for Scout Bot to click "Join"',
                '3. Webhook will capture the correct UUID',
                '4. Call /calibration/verify to confirm'
            ]
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/calibration/single-room-complete', methods=['POST'])
def calibration_single_room_complete():
    """
    Complete a single room re-calibration.
    Called after Scout Bot has entered the room and webhook was received.
    """
    data = request.json or {}
    room_name = data.get('room_name')
    meeting_id = data.get('meeting_id') or meeting_state.meeting_id

    if not room_name:
        return jsonify({'error': 'room_name required'}), 400

    # Check if webhook was matched
    matched_move = None
    for move in meeting_state.pending_room_moves:
        if move.get('room_name') == room_name and move.get('matched') and move.get('recalibration'):
            matched_move = move
            break

    if not matched_move:
        return jsonify({
            'success': False,
            'error': f'No webhook received for room: {room_name}',
            'hint': 'Make sure Scout Bot clicked "Join" in the breakout room dialog'
        }), 404

    webhook_uuid = matched_move.get('webhook_uuid')

    # Save to BigQuery
    try:
        today = get_ist_date()
        room_index = None
        if USE_FIXED_SEQUENCE and FIXED_ROOM_SEQUENCE:
            for i, name in enumerate(FIXED_ROOM_SEQUENCE):
                if name == room_name:
                    room_index = i
                    break

        mapping_row = {
            'mapping_id': str(uuid_lib.uuid4()),
            'meeting_id': str(meeting_id),
            'meeting_uuid': meeting_state.meeting_uuid or '',
            'room_uuid': webhook_uuid,
            'room_name': room_name,
            'room_index': room_index if room_index is not None else 0,
            'mapping_date': today,
            'mapped_at': datetime.utcnow().isoformat(),
            'source': 'recalibration'  # Mark as recalibration
        }
        success = insert_room_mappings([mapping_row])

        # Clean up
        meeting_state.pending_room_moves = [
            m for m in meeting_state.pending_room_moves
            if not (m.get('room_name') == room_name and m.get('recalibration'))
        ]
        meeting_state.calibration_in_progress = False

        if success:
            print(f"[Calibration] Re-calibration SUCCESS: {room_name} = {webhook_uuid[:20]}...")
            return jsonify({
                'success': True,
                'message': f'Room "{room_name}" re-calibrated successfully',
                'room_name': room_name,
                'webhook_uuid': webhook_uuid
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to save mapping to BigQuery'
            }), 500

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/calibration/mapping-summary', methods=['GET'])
def calibration_mapping_summary():
    """
    Get a summary of all room mappings comparing FIXED_ROOM_SEQUENCE with actual mappings.
    Useful for identifying missing or incorrect mappings.
    """
    meeting_id = request.args.get('meeting_id') or meeting_state.meeting_id
    today = get_ist_date()

    try:
        client = get_bq_client()

        # Get all mappings for today
        query = f"""
        SELECT room_uuid, room_name, room_index, source, mapped_at
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
        WHERE mapping_date = @today
          AND meeting_id = @meeting_id
        ORDER BY room_index, mapped_at DESC
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("today", "STRING", today),
                bigquery.ScalarQueryParameter("meeting_id", "STRING", str(meeting_id) if meeting_id else ''),
            ]
        )
        results = list(client.query(query, job_config=job_config).result())

        # Build mapping lookup
        mapped_rooms = {}
        for row in results:
            if row.room_name not in mapped_rooms:
                mapped_rooms[row.room_name] = {
                    'room_uuid': row.room_uuid,
                    'room_index': row.room_index,
                    'source': row.source,
                    'mapped_at': row.mapped_at
                }

        # Compare with FIXED_ROOM_SEQUENCE
        summary = []
        for i, expected_name in enumerate(FIXED_ROOM_SEQUENCE):
            mapping = mapped_rooms.get(expected_name)
            summary.append({
                'index': i,
                'expected_name': expected_name,
                'mapped': mapping is not None,
                'webhook_uuid': mapping['room_uuid'][:20] + '...' if mapping else None,
                'source': mapping['source'] if mapping else None,
                'status': 'OK' if mapping else 'MISSING'
            })

        # Count stats
        mapped_count = len([s for s in summary if s['mapped']])
        missing_count = len([s for s in summary if not s['mapped']])

        return jsonify({
            'success': True,
            'meeting_id': meeting_id,
            'date': today,
            'total_expected': len(FIXED_ROOM_SEQUENCE),
            'mapped_count': mapped_count,
            'missing_count': missing_count,
            'rooms': summary,
            'calibration_in_progress': meeting_state.calibration_in_progress
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/calibration/check-room-mapped', methods=['GET'])
def check_room_mapped():
    """
    Check if a specific room already has a webhook UUID mapping.
    Used by frontend to skip already-calibrated rooms during calibration.

    Query params:
    - room_name: Name of the room to check
    - meeting_id: Optional meeting ID (defaults to current)

    Returns:
    - mapped: True if room has webhook UUID mapping
    - source: Source of mapping (webhook_calibration, pending_move_calibration, etc.)
    - can_skip: True if room can be safely skipped (has reliable mapping)
    """
    room_name = request.args.get('room_name')
    meeting_id = request.args.get('meeting_id') or meeting_state.meeting_id

    if not room_name:
        return jsonify({'error': 'room_name required'}), 400

    try:
        today = get_ist_date()

        # Check BigQuery for existing webhook mapping
        # Only consider reliable sources (webhook-based, not SDK-only)
        query = """
        SELECT room_uuid, source, mapped_at
        FROM `{project}.{dataset}.room_mappings`
        WHERE room_name = @room_name
          AND mapping_date = @mapping_date
          AND source IN ('webhook_calibration', 'pending_move_calibration', 'sequence_calibration', 'webhook_primary_sdk_lookup')
        ORDER BY mapped_at DESC
        LIMIT 1
        """.format(project=GCP_PROJECT_ID, dataset=BQ_DATASET)

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("room_name", "STRING", room_name),
                bigquery.ScalarQueryParameter("mapping_date", "DATE", today),
            ]
        )

        result = bq_client.query(query, job_config=job_config).result()
        rows = list(result)

        if rows:
            row = rows[0]
            print(f"[check-room-mapped] {room_name}: MAPPED (source={row.source})")
            return jsonify({
                'room_name': room_name,
                'mapped': True,
                'can_skip': True,
                'source': row.source,
                'room_uuid': row.room_uuid[:20] + '...' if row.room_uuid else None
            })
        else:
            print(f"[check-room-mapped] {room_name}: NOT MAPPED")
            return jsonify({
                'room_name': room_name,
                'mapped': False,
                'can_skip': False,
                'source': None
            })

    except Exception as e:
        print(f"[check-room-mapped] Error: {e}")
        # On error, return not mapped to be safe (will calibrate)
        return jsonify({
            'room_name': room_name,
            'mapped': False,
            'can_skip': False,
            'error': str(e)
        })


@app.route('/mappings', methods=['GET'])
def get_mappings():
    """Get current room mappings"""
    return jsonify({
        'meeting_id': meeting_state.meeting_id,
        'calibration_complete': meeting_state.calibration_complete,
        'mappings': [
            {'room_name': name, 'room_uuid': uuid}
            for name, uuid in meeting_state.name_to_uuid.items()
        ],
        'total': len(meeting_state.name_to_uuid)
    })


# ==============================================================================
# WEBHOOK-PRIMARY MODE: ROOM MAPPING ENDPOINTS
# ==============================================================================
# These endpoints support the webhook-primary architecture where:
# - Webhooks are the PRIMARY data source (exact timestamps)
# - SDK is ONLY used to map room UUIDs to room names
# - Bot sits idle, responds to mapping requests
# ==============================================================================

@app.route('/mapping/sync', methods=['POST'])
def mapping_sync():
    """
    SDK sends full room list with SDK UUIDs.
    Used for proactive room name sync (runs every ~1 minute).

    Payload:
    {
        "meeting_id": "123456789",
        "meeting_uuid": "abc123...",
        "rooms": [
            {"room_name": "Team Alpha", "sdk_uuid": "e7f123fc...", "participant_count": 5},
            ...
        ],
        "synced_at": "2026-07-20T10:30:00Z"
    }

    Note: This stores SDK UUIDs, not webhook UUIDs. The actual mapping happens
    when /mapping/resolve is called with a webhook_uuid and we match by participant.
    """
    try:
        data = request.get_json() or {}
        meeting_id = data.get('meeting_id') or meeting_state.meeting_id
        meeting_uuid = data.get('meeting_uuid', '')
        rooms = data.get('rooms', [])

        if not rooms:
            return jsonify({'success': True, 'message': 'No rooms to sync', 'count': 0})

        # DO NOT call set_meeting() here: the SDK panel's meeting id can be the
        # meeting UUID while webhooks carry the numeric id. set_meeting treats a
        # mismatch as a NEW meeting and reset()s — wiping uuid_to_name and the
        # pending mapping queue on every sync. Webhooks own meeting identity;
        # only fill it in if nothing set it yet.
        if not meeting_state.meeting_id and meeting_id:
            meeting_state.meeting_id = meeting_id
            meeting_state.meeting_uuid = meeting_state.meeting_uuid or meeting_uuid
            meeting_state.meeting_date = get_ist_date()

        # Store room names (SDK UUIDs are different from webhook UUIDs, so we just
        # cache the room list for reference - actual mapping uses participant lookup)
        for room in rooms:
            room_name = room.get('room_name')
            sdk_uuid = room.get('sdk_uuid', '')
            if room_name and sdk_uuid:
                # Store SDK UUID mapping (useful for debugging, not for webhook matching)
                meeting_state.uuid_to_name[f"sdk:{sdk_uuid}"] = room_name

        # SYNC-TIME RESOLUTION: the payload embeds each room's current
        # participants, so any pending webhook-uuid request whose participant
        # appears in a room can be resolved right here — no round trip through
        # /mapping/pending. This also heals the multi-worker case: whichever
        # gunicorn worker receives this sync resolves ITS OWN pending queue.
        def _norm_pname(n):
            return _re.sub(r'-\d+$', '', (n or '').strip().lower()).strip()

        room_by_person = {}
        for room in rooms:
            rname = room.get('room_name')
            if not rname:
                continue
            for part in (room.get('participants') or []):
                nm = _norm_pname(part.get('name'))
                em = (part.get('email') or '').strip().lower()
                if nm:
                    room_by_person[('n', nm)] = rname
                if em:
                    room_by_person[('e', em)] = rname

        matched = []
        if room_by_person:
            with meeting_state._lock:
                remaining = []
                for req in meeting_state.pending_mapping_requests:
                    rname = (room_by_person.get(('e', (req.get('participant_email') or '').strip().lower()))
                             or room_by_person.get(('n', _norm_pname(req.get('participant_name')))))
                    if rname:
                        matched.append((req, rname))
                    else:
                        remaining.append(req)
                meeting_state.pending_mapping_requests = remaining

        # DIAGNOSTIC: keep the raw payload so /mapping/last-sync can show
        # exactly what the SDK claims (who is in which room) — used to verify
        # whether getBreakoutRoomList reports ACTUAL positions or stale
        # ASSIGNMENTS (ground-truth check, 2026-07-21).
        with meeting_state._lock:
            meeting_state.last_sync_payload = {
                'received_at_utc': datetime.utcnow().isoformat(),
                'meeting_id': meeting_id,
                'rooms': rooms,
            }

        # Memory updates happen HERE (instant — webhooks resolve names right
        # away). ALL BigQuery persistence runs in a background thread: the
        # first sync after a restart does a ~50-mapping warm-up backfill
        # (sequential BQ checks, ~1s each) which blew past the panel's 15s
        # request timeout when done inline (seen live 2026-07-21).
        today = get_ist_date()
        for req, rname in matched:
            meeting_state.add_webhook_room_mapping(req['webhook_uuid'], rname)
            print(f"[mapping/sync] Resolved from sync payload: {req['webhook_uuid'][:20]}... -> {rname}")

        # Snapshot state now; the thread works off this consistent copy.
        with meeting_state._lock:
            bo_state = dict(meeting_state.participant_current_breakout)
            uuid_names = dict(meeting_state.uuid_to_name)
            current_instance = meeting_state.last_breakout_instance_uuid

        def _persist_sync_bq():
            state_resolved = 0
            state_corrected = 0
            try:
                def _persist_mapping(wu, rname):
                    return insert_room_mappings([{
                        'mapping_id': str(uuid_lib.uuid4()),
                        'meeting_id': str(meeting_state.meeting_id or meeting_id or ''),
                        'meeting_uuid': meeting_state.meeting_uuid or '',
                        'room_uuid': wu,
                        'room_name': rname,
                        'room_index': -1,
                        'mapping_date': today,
                        'mapped_at': datetime.utcnow().isoformat(),
                        'source': 'webhook_primary_sdk_lookup'
                    }])

                # 1) Persist pending-request matches (memory already updated)
                for req, rname in matched:
                    if _persist_mapping(req['webhook_uuid'], rname):
                        with meeting_state._lock:
                            meeting_state.persisted_webhook_uuids.add(req['webhook_uuid'])

                # 2) STATE RESOLUTION (2026-07-21): map every occupied room in
                # ONE sync by cross-matching CURRENT positions — SDK payload
                # says "person P is in room R now", webhook state says "P is in
                # uuid U now" => U = R. Works no matter when the bot comes
                # online — enables short daily panel sessions instead of a
                # 24/7 VM. A uuid claimed for two DIFFERENT rooms is skipped
                # this round; next sync settles it.
                # REVERT: SYNC_STATE_RESOLUTION_ENABLED = False (defined above
                # _queue_mapping_request).
                if SYNC_STATE_RESOLUTION_ENABLED and room_by_person:
                    # STABILITY GUARD: only trust a position webhooks have
                    # shown stable for 2+ min — mapping someone mid-hop is how
                    # wrong labels get saved (UEWhtBkXN92Q case, 2026-07-21).
                    # FRESHNESS CAP: ignore positions older than 30 min — a
                    # participant whose move-webhook was never delivered keeps
                    # a permanently-stale entry and would fight corrections
                    # forever (1.1<->8.0 ping-pong observed on a single
                    # worker, 2026-07-21 evening).
                    MAPPING_STABLE_MIN_S = 120
                    MAPPING_FRESH_MAX_S = 1800
                    now_ts = time.time()
                    with meeting_state._lock:
                        disputes = dict(meeting_state.mapping_disputes)
                        prior_corrections = dict(meeting_state.last_uuid_corrections)
                    claims = {}       # unknown uuid -> claimed room names
                    corrections = {}  # mapped uuid that DISAGREES -> claims
                    for (kind, ident), rname in room_by_person.items():
                        entry = bo_state.get(kind + ':' + ident)
                        if not entry:
                            continue
                        wu = entry.get('room_uuid')
                        if not wu:
                            continue
                        # RESTART GUARD: ignore positions recorded under a
                        # previous meeting instance (meeting ended+restarted).
                        if (current_instance and entry.get('meeting_uuid')
                                and entry['meeting_uuid'] != current_instance):
                            continue
                        age_s = now_ts - (entry.get('ts') or 0)
                        if age_s < MAPPING_STABLE_MIN_S or age_s > MAPPING_FRESH_MAX_S:
                            continue
                        current = (uuid_names.get(wu) or '').strip()
                        if not current:
                            claims.setdefault(wu, set()).add(rname)
                        elif current != rname.strip():
                            if wu in disputes:
                                continue  # frozen for the day (see below)
                            # VERIFY-AND-CORRECT: existing mapping contradicts
                            # what the SDK sees for a stable participant.
                            corrections.setdefault(wu, set()).add(rname)

                    for wu, names in claims.items():
                        if len(names) != 1:
                            print(f"[mapping/sync] State-resolution SKIP {wu[:20]}...: "
                                  f"conflicting rooms {sorted(names)}")
                            continue
                        rname = next(iter(names))
                        meeting_state.add_webhook_room_mapping(wu, rname)
                        if _persist_mapping(wu, rname):
                            with meeting_state._lock:
                                meeting_state.persisted_webhook_uuids.add(wu)
                        state_resolved += 1
                        print(f"[mapping/sync] State-resolved: {wu[:20]}... -> {rname}")

                    for wu, names in corrections.items():
                        if len(names) != 1:
                            print(f"[mapping/sync] Correction SKIP {wu[:20]}...: "
                                  f"conflicting rooms {sorted(names)}")
                            continue
                        rname = next(iter(names))
                        old_name = uuid_names.get(wu)
                        # ANTI PING-PONG: a correction that REVERSES a recent
                        # one means two participants' webhook states disagree
                        # about this uuid (one is stale). Truth is undecidable
                        # automatically -> freeze the uuid for the day, keep
                        # whichever name is currently saved, log DISPUTED.
                        prior = prior_corrections.get(wu)
                        if (prior and prior.get('old') == rname
                                and now_ts - (prior.get('ts') or 0) < 3600):
                            with meeting_state._lock:
                                meeting_state.mapping_disputes[wu] = now_ts
                            print(f"[mapping/sync] DISPUTED (frozen for today): {wu[:20]}... "
                                  f"oscillating between '{rname}' and '{prior.get('new')}'")
                            continue
                        # BQ first: memory is only corrected once the mapping
                        # row is durably fixed — otherwise the build-time JOIN
                        # (which prefers room_mappings) would keep re-applying
                        # the stale name. If the UPDATE fails (e.g. row still
                        # in streaming buffer), we retry every sync.
                        if _persist_mapping(wu, rname):
                            meeting_state.add_webhook_room_mapping(wu, rname)
                            with meeting_state._lock:
                                meeting_state.persisted_webhook_uuids.add(wu)
                                meeting_state.last_uuid_corrections[wu] = {
                                    'old': old_name, 'new': rname, 'ts': now_ts}
                            state_corrected += 1
                            print(f"[mapping/sync] CORRECTED: {wu[:20]}... "
                                  f"'{old_name}' -> '{rname}'")
                        else:
                            print(f"[mapping/sync] Correction PENDING {wu[:20]}... "
                                  f"'{old_name}' -> '{rname}' (BQ save failed, retrying next sync)")

                # 3) BACKFILL: persist any in-memory webhook-uuid mapping not
                # yet confirmed in BQ. Retries every sync until saved.
                with meeting_state._lock:
                    unpersisted = [
                        (u, n) for u, n in meeting_state.uuid_to_name.items()
                        if len(u) > 8 and not u.startswith('sdk:')
                        and u not in meeting_state.persisted_webhook_uuids
                    ]
                backfilled = 0
                for wu, rname in unpersisted:
                    if _persist_mapping(wu, rname):
                        with meeting_state._lock:
                            meeting_state.persisted_webhook_uuids.add(wu)
                        backfilled += 1
                if unpersisted:
                    print(f"[mapping/sync] Backfilled {backfilled}/{len(unpersisted)} in-memory mapping(s) to BigQuery")

                if state_resolved or state_corrected:
                    print(f"[mapping/sync] BQ persistence done: "
                          f"{state_resolved} state-resolved, {state_corrected} corrected")
            except Exception:
                traceback.print_exc()
            finally:
                with meeting_state._lock:
                    meeting_state._mapping_bq_busy = False

        # One persistence thread at a time per worker; a skipped round is
        # harmless — the next sync (60s) re-snapshots and retries everything.
        with meeting_state._lock:
            busy = getattr(meeting_state, '_mapping_bq_busy', False)
            if not busy:
                meeting_state._mapping_bq_busy = True
        if busy:
            print("[mapping/sync] BQ persistence still running from a previous sync - skipped this round")
        else:
            threading.Thread(target=_persist_sync_bq, daemon=True).start()

        print(f"[mapping/sync] Synced {len(rooms)} rooms for meeting {meeting_id} "
              f"({len(matched)} pending request(s) resolved; BQ persistence in background)")

        return jsonify({
            'success': True,
            'count': len(rooms),
            'resolved': len(matched),
            'meeting_id': meeting_id
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/mapping/pending', methods=['GET'])
def mapping_pending():
    """
    SDK checks for pending mapping requests.
    When webhook has unknown room_uuid, it queues a request here.

    Query params:
    - meeting_id: Meeting ID to filter requests

    Returns:
    {
        "pending": [
            {
                "request_id": "uuid",
                "webhook_uuid": "6kAkE8jO...",
                "participant_name": "John Doe",
                "participant_email": "john@example.com",
                "timestamp": "2026-07-20T10:30:00Z"
            },
            ...
        ]
    }
    """
    try:
        now = time.time()

        with meeting_state._lock:
            # Expire stale requests (participant probably left / moved on;
            # zt_intervals resolves leftover placeholders via room_mappings
            # at build time anyway).
            meeting_state.pending_mapping_requests = [
                req for req in meeting_state.pending_mapping_requests
                if now - req.get('queued_ts', now) < MAPPING_REQUEST_TTL_S
            ]

            # Serve requests not currently being worked on. Requests are NOT
            # deleted here — only /mapping/resolve (or sync-time resolution,
            # or TTL expiry) removes them, so a crashed SDK lookup retries.
            pending = []
            for req in meeting_state.pending_mapping_requests:
                served = req.get('served_ts')
                if served and now - served < MAPPING_REQUEST_RESERVE_S:
                    continue  # recently handed to the SDK, give it time
                req['served_ts'] = now
                pending.append({
                    'request_id': req['request_id'],
                    'webhook_uuid': req['webhook_uuid'],
                    'participant_name': req['participant_name'],
                    'participant_email': req['participant_email'],
                    'meeting_id': req.get('meeting_id'),
                    'timestamp': req.get('timestamp'),
                })
                if len(pending) >= 10:
                    break

        if pending:
            print(f"[mapping/pending] Returning {len(pending)} pending requests")
        return jsonify({'pending': pending})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/mapping/resolve', methods=['POST'])
def mapping_resolve():
    """
    SDK resolves a mapping request - tells us which room a participant is in.

    Payload:
    {
        "request_id": "uuid",
        "webhook_uuid": "6kAkE8jO...",
        "room_name": "Team Alpha",  // null if participant not in any breakout
        "sdk_uuid": "e7f123fc...",  // SDK's room UUID (for reference)
        "meeting_id": "123456789"
    }
    """
    try:
        data = request.get_json() or {}
        request_id = data.get('request_id')
        webhook_uuid = data.get('webhook_uuid')
        room_name = data.get('room_name')
        sdk_uuid = data.get('sdk_uuid', '')
        meeting_id = data.get('meeting_id') or meeting_state.meeting_id

        if not webhook_uuid:
            return jsonify({'error': 'webhook_uuid required'}), 400

        # Remove the request from the pending queue (whatever the outcome —
        # if room_name is null the participant is in Main Room / already moved,
        # and re-serving the same lookup would just spin forever).
        with meeting_state._lock:
            meeting_state.pending_mapping_requests = [
                req for req in meeting_state.pending_mapping_requests
                if req['webhook_uuid'] != webhook_uuid
            ]

        if room_name:
            # Store the mapping: webhook_uuid -> room_name
            meeting_state.add_webhook_room_mapping(webhook_uuid, room_name)

            # Also save to BigQuery for persistence
            today = get_ist_date()
            mapping_row = {
                'mapping_id': str(uuid_lib.uuid4()),
                'meeting_id': str(meeting_id),
                'meeting_uuid': meeting_state.meeting_uuid or '',
                'room_uuid': webhook_uuid,
                'room_name': room_name,
                'room_index': -1,  # Unknown index in webhook-primary mode
                'mapping_date': today,
                'mapped_at': datetime.utcnow().isoformat(),
                'source': 'webhook_primary_sdk_lookup'
            }
            saved = insert_room_mappings([mapping_row])
            if saved:
                with meeting_state._lock:
                    meeting_state.persisted_webhook_uuids.add(webhook_uuid)

            print(f"[mapping/resolve] Mapped {webhook_uuid[:20]}... -> {room_name}"
                  + ('' if saved else ' (BQ save FAILED - will retry on next sync)'))

            # Update any events that were stored with Room-XXXXX placeholder
            # This is async/best-effort - events are still valid with placeholder names
            try:
                _update_events_with_room_name(webhook_uuid, room_name, today)
            except Exception as e:
                print(f"[mapping/resolve] Warning: Could not update existing events: {e}")
        else:
            print(f"[mapping/resolve] No room found for {webhook_uuid[:20]}... (participant may be in Main Room)")

        return jsonify({
            'success': True,
            'webhook_uuid': webhook_uuid[:20] + '...' if webhook_uuid else None,
            'room_name': room_name
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/mapping/request', methods=['POST'])
def mapping_request():
    """
    Queue a mapping request for the SDK to resolve.
    Called internally when webhook has unknown room_uuid.

    Payload:
    {
        "webhook_uuid": "6kAkE8jO...",
        "participant_name": "John Doe",
        "participant_email": "john@example.com",
        "meeting_id": "123456789"
    }
    """
    try:
        data = request.get_json() or {}
        webhook_uuid = data.get('webhook_uuid')
        participant_name = data.get('participant_name')
        participant_email = data.get('participant_email', '')
        meeting_id = data.get('meeting_id') or meeting_state.meeting_id

        if not webhook_uuid or not participant_name:
            return jsonify({'error': 'webhook_uuid and participant_name required'}), 400

        request_entry = {
            'request_id': str(uuid_lib.uuid4()),
            'webhook_uuid': webhook_uuid,
            'participant_name': participant_name,
            'participant_email': participant_email,
            'meeting_id': meeting_id,
            'timestamp': datetime.utcnow().isoformat()
        }

        with meeting_state._lock:
            # Avoid duplicate requests for same webhook_uuid
            existing = any(
                req['webhook_uuid'] == webhook_uuid
                for req in meeting_state.pending_mapping_requests
            )
            if not existing:
                meeting_state.pending_mapping_requests.append(request_entry)
                # Keep queue bounded
                if len(meeting_state.pending_mapping_requests) > 100:
                    meeting_state.pending_mapping_requests = meeting_state.pending_mapping_requests[-100:]

        print(f"[mapping/request] Queued mapping request for {webhook_uuid[:20]}... ({participant_name})")

        return jsonify({
            'success': True,
            'request_id': request_entry['request_id']
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/mapping/last-sync', methods=['GET'])
def mapping_last_sync():
    """
    DIAGNOSTIC (2026-07-21): show the raw room->participants payload from the
    panel's most recent /mapping/sync. Used to ground-truth whether the SDK
    reports ACTUAL current positions or stale ASSIGNMENTS.

    GET /mapping/last-sync            -> full payload
    GET /mapping/last-sync?name=anj   -> only rooms containing that person
    """
    try:
        with meeting_state._lock:
            payload = getattr(meeting_state, 'last_sync_payload', None)
        if not payload:
            return jsonify({'success': True, 'message': 'No sync received yet on this worker (2 workers - retry once)'})
        name_filter = (request.args.get('name') or '').strip().lower()
        rooms = payload.get('rooms', [])
        if name_filter:
            rooms = [
                {'room_name': r.get('room_name'),
                 'participants': [p for p in (r.get('participants') or [])
                                  if name_filter in (p.get('name') or '').lower()]}
                for r in rooms
                if any(name_filter in (p.get('name') or '').lower()
                       for p in (r.get('participants') or []))
            ]
        return jsonify({
            'success': True,
            'received_at_utc': payload.get('received_at_utc'),
            'meeting_id': payload.get('meeting_id'),
            'room_count': len(payload.get('rooms', [])),
            'rooms': rooms
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/mapping/health', methods=['GET', 'POST'])
def mapping_health():
    """
    Daily safety net for the HR-runs-the-panel workflow (no 24/7 bot).
    Cloud Scheduler calls this around noon IST. If NO room mappings were
    saved today, nobody has opened the Room Mapper app yet -> email an
    alert so an HR opens it for 5 minutes. Hours are never at risk
    (webhooks capture everything); only room LABELS stay placeholders
    until someone runs the panel — mapping is retroactive for the day.

    GET/POST /mapping/health           -> check + email if unhealthy
    GET/POST /mapping/health?alert=false -> check only, never email
    """
    today = get_ist_date()
    send_alert = (request.args.get('alert', 'true').lower() != 'false')
    try:
        client = get_bq_client()
        q = f"""
        SELECT
          (SELECT COUNT(*)
           FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
           WHERE CAST(mapping_date AS STRING) = '{today}') AS mappings_today,
          (SELECT COUNT(DISTINCT room_uuid)
           FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_EVENTS_TABLE}`
           WHERE event_date = '{today}'
             AND room_name LIKE 'Room-%'
             AND room_uuid NOT IN (
               SELECT room_uuid
               FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
               WHERE CAST(mapping_date AS STRING) = '{today}'
             )) AS unresolved_rooms
        """
        row = list(client.query(q).result())[0]
        mappings_today = int(row.mappings_today or 0)
        unresolved_rooms = int(row.unresolved_rooms or 0)
        healthy = mappings_today > 0

        alerted = False
        if not healthy and send_alert:
            html = (
                f"<h3>Zoom Tracker: no room mappings saved today ({today})</h3>"
                f"<p>Nobody has opened the <b>Room Mapper</b> Zoom app yet today, "
                f"so {unresolved_rooms} room(s) are still showing as "
                f"placeholder names (Room-xxxxxxxx) in reports.</p>"
                f"<p><b>Attendance hours are NOT affected</b> — webhooks track "
                f"everything. Only the room names need mapping.</p>"
                f"<p><b>Fix (5 minutes, any co-host):</b> in the Zoom meeting, "
                f"open Apps &rarr; Room Mapper, wait ~2 minutes until the log "
                f"shows rooms synced, then close it. This retroactively fixes "
                f"the whole day.</p>"
            )
            result = send_email_alert(
                f"[Zoom Tracker] Room Mapper not run today ({today})", html)
            alerted = bool(result.get('success'))

        return jsonify({
            'success': True,
            'date': today,
            'healthy': healthy,
            'mappings_today': mappings_today,
            'unresolved_placeholder_rooms': unresolved_rooms,
            'alert_sent': alerted
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


def _update_events_with_room_name(webhook_uuid, room_name, event_date):
    """
    Update participant_events that have Room-XXXXX placeholder with actual room name.
    Best-effort ONLY: BigQuery rejects UPDATEs on rows still in the streaming
    buffer (up to ~90 min after insert), so this usually fails for fresh events.
    That is fine — zt_intervals resolves placeholder names via a room_mappings
    JOIN at build time, so reports are correct regardless.
    """
    try:
        # Only update events from today with the placeholder name
        placeholder = f'Room-{webhook_uuid[:8]}'

        update_query = f"""
        UPDATE `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_EVENTS_TABLE}`
        SET room_name = @room_name
        WHERE event_date = @event_date
          AND room_uuid = @webhook_uuid
          AND room_name = @placeholder
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("room_name", "STRING", room_name),
                bigquery.ScalarQueryParameter("event_date", "DATE", event_date),
                bigquery.ScalarQueryParameter("webhook_uuid", "STRING", webhook_uuid),
                bigquery.ScalarQueryParameter("placeholder", "STRING", placeholder),
            ]
        )

        bq_client.query(update_query, job_config=job_config).result()
        print(f"[_update_events_with_room_name] Updated events: {placeholder} -> {room_name}")

    except Exception as e:
        # Non-fatal - events still have the webhook_uuid for later mapping
        print(f"[_update_events_with_room_name] Warning: {e}")


# ==============================================================================
# REPORT ENDPOINTS
# ==============================================================================

@app.route('/report/generate', methods=['POST'])
def generate_report():
    """Manually trigger report generation - defaults to YESTERDAY's data"""
    data = request.json or {}
    # Default to yesterday (not today) - report_generator handles this correctly
    report_date = data.get('date')  # None = yesterday in report_generator

    try:
        from report_generator import generate_daily_report, send_report_email, get_yesterday_ist

        # If no date provided, use yesterday (via report_generator default)
        report = generate_daily_report(report_date)
        # Get actual date used for response
        actual_date = report_date or get_yesterday_ist()

        if SENDGRID_API_KEY and REPORT_EMAIL_TO:
            send_report_email(report, actual_date)
            return jsonify({
                'success': True,
                'message': f'Report generated and sent to {REPORT_EMAIL_TO}',
                'date': actual_date
            })
        else:
            return jsonify({
                'success': True,
                'message': 'Report generated (email not configured)',
                'date': actual_date,
                'report': report
            })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/report/preview/<date>', methods=['GET'])
def preview_report(date):
    """Preview report data for a date"""
    try:
        from report_generator import generate_daily_report
        report = generate_daily_report(date)
        return jsonify(report)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/report/live/<date>', methods=['GET'])
def live_attendance_report(date):
    """
    Generate live attendance report for ONGOING meetings.
    Shows participants with join times even if they haven't left yet.
    Use this when meeting is still in progress.

    GET /report/live/2026-03-31
    """
    import re
    from report_generator import FIXED_ROOM_SEQUENCE

    # Validate date format
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return jsonify({'error': f'Invalid date format: {date}. Expected YYYY-MM-DD'}), 400

    try:
        client = get_bq_client()

        # Query to get all participant join events for today with room history
        query = f"""
        WITH
        -- Room name mappings
        room_name_map AS (
          SELECT room_uuid, room_name,
            ROW_NUMBER() OVER (
              PARTITION BY room_uuid
              ORDER BY
                CASE WHEN source = 'sequential_calibration' THEN 0
                     WHEN source = 'webhook_calibration' THEN 1
                     ELSE 2 END,
                mapped_at DESC
            ) as rn
          FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
          WHERE mapping_date = @target_date
        ),
        -- All events for today
        all_events AS (
          SELECT
            participant_id,
            participant_name,
            participant_email,
            event_type,
            event_timestamp,
            room_uuid,
            room_name as event_room_name,
            SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*SZ', event_timestamp) as event_ts
          FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_EVENTS_TABLE}`
          WHERE event_date = @target_date
            AND participant_name NOT LIKE '%Scout%'
        ),
        -- First main room join per participant
        first_joins AS (
          SELECT
            participant_email,
            MIN(CASE WHEN event_type = 'participant_joined' THEN event_ts END) as first_join_ts
          FROM all_events
          GROUP BY participant_email
        ),
        -- Current room per participant (latest breakout_room_joined)
        current_rooms AS (
          SELECT
            e.participant_email,
            e.room_uuid,
            COALESCE(
              CASE WHEN e.event_room_name IS NOT NULL
                   AND e.event_room_name != ''
                   AND NOT STARTS_WITH(e.event_room_name, 'Room-')
                   THEN e.event_room_name END,
              rm.room_name,
              e.event_room_name
            ) as current_room,
            e.event_ts as room_joined_ts
          FROM (
            SELECT *, ROW_NUMBER() OVER (
              PARTITION BY participant_email
              ORDER BY event_ts DESC
            ) as rn
            FROM all_events
            WHERE event_type = 'breakout_room_joined'
          ) e
          LEFT JOIN room_name_map rm ON e.room_uuid = rm.room_uuid AND rm.rn = 1
          WHERE e.rn = 1
        ),
        -- Participant names (pick most common)
        participant_names AS (
          SELECT
            participant_email,
            ARRAY_AGG(participant_name ORDER BY cnt DESC LIMIT 1)[OFFSET(0)] as participant_name
          FROM (
            SELECT participant_email, participant_name, COUNT(*) as cnt
            FROM all_events
            WHERE participant_email IS NOT NULL AND participant_email != ''
            GROUP BY participant_email, participant_name
          )
          GROUP BY participant_email
        )
        SELECT
          pn.participant_name as Name,
          pn.participant_email as Email,
          FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(fj.first_join_ts, INTERVAL 330 MINUTE)) as Joined_IST,
          COALESCE(cr.current_room, 'Main Room') as Current_Room,
          FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(cr.room_joined_ts, INTERVAL 330 MINUTE)) as Room_Joined_IST
        FROM participant_names pn
        LEFT JOIN first_joins fj ON pn.participant_email = fj.participant_email
        LEFT JOIN current_rooms cr ON pn.participant_email = cr.participant_email
        WHERE pn.participant_email IS NOT NULL AND pn.participant_email != ''
        ORDER BY pn.participant_name
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("target_date", "STRING", date)
            ]
        )

        results = list(client.query(query, job_config=job_config).result())

        # Build CSV content
        import io
        import csv
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Name', 'Email', 'Joined_IST', 'Current_Room', 'Room_Joined_IST'])

        participants = []
        for row in results:
            participants.append(dict(row.items()))
            writer.writerow([
                row.get('Name', '') or '',
                row.get('Email', '') or '',
                row.get('Joined_IST', '') or '',
                row.get('Current_Room', '') or 'Main Room',
                row.get('Room_Joined_IST', '') or ''
            ])

        return jsonify({
            'report_date': date,
            'report_type': 'live_attendance',
            'generated_at': datetime.utcnow().isoformat(),
            'total_participants': len(participants),
            'participants': participants,
            'csv_content': output.getvalue()
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ==============================================================================
# MANUAL QOS COLLECTION
# ==============================================================================

@app.route('/qos/collect', methods=['POST'])
def collect_qos_manual():
    """
    Manually collect QoS data for a meeting.
    Use this when meeting.ended webhook is not received.

    POST /qos/collect
    Body: {"meeting_uuid": "xxx"} or {"meeting_id": "123456"}
    """
    data = request.json or {}
    meeting_uuid = data.get('meeting_uuid', '')
    meeting_id = data.get('meeting_id', '')

    if not meeting_uuid and not meeting_id:
        return jsonify({'error': 'meeting_uuid or meeting_id required'}), 400

    print(f"[QoS] Manual collection triggered")
    print(f"[QoS] Meeting UUID: {meeting_uuid}")
    print(f"[QoS] Meeting ID: {meeting_id}")

    collected_count = 0
    error_count = 0
    participants_data = []

    try:
        # First, collect camera data via Dashboard QoS API
        camera_data_map = {}
        try:
            # Use numeric meeting_id for Dashboard API
            qos_meeting_id = meeting_id if meeting_id and str(meeting_id).replace('-', '').isdigit() else None
            if qos_meeting_id:
                print(f"[QoS] Collecting camera data via Dashboard QoS API...")
                camera_participants = zoom_api.get_meeting_participants_qos(qos_meeting_id)
                for cp in camera_participants:
                    user_name = cp.get('user_name', '')
                    email = cp.get('email', '')
                    camera_on_count = cp.get('camera_on_count', 0)
                    camera_on_minutes = cp.get('camera_on_minutes', 0)
                    camera_on_timestamps = cp.get('camera_on_timestamps', [])
                    key = f"{user_name}|{email}".lower()
                    camera_data_map[key] = {
                        'count': camera_on_count,
                        'minutes': camera_on_minutes,
                        'timestamps': camera_on_timestamps,
                        'intervals': format_camera_intervals(camera_on_timestamps)
                    }
                print(f"[QoS] Got camera data for {len(camera_data_map)} participants")
            else:
                print(f"[QoS] No numeric meeting_id - skipping camera data collection")
        except Exception as ce:
            print(f"[QoS] Camera collection error (non-fatal): {ce}")

        # Try with meeting_uuid first
        participants = []
        if meeting_uuid:
            participants = zoom_api.get_past_meeting_participants(meeting_uuid)

        # Fallback to meeting_id
        if not participants and meeting_id:
            participants = zoom_api.get_past_meeting_participants(meeting_id)

        if not participants:
            return jsonify({
                'success': False,
                'error': 'No participants found - meeting may still be in progress or API requires Business+ plan',
                'meeting_uuid': meeting_uuid,
                'meeting_id': meeting_id
            }), 404

        print(f"[QoS] Found {len(participants)} participants")

        for p in participants:
            try:
                participant_id = safe_str(
                    p.get('user_id') or p.get('id') or p.get('participant_user_id'),
                    default='unknown'
                )
                participant_name = safe_str(
                    p.get('name') or p.get('user_name'),
                    default='Unknown'
                )
                participant_email = safe_str(
                    p.get('user_email') or p.get('email'),
                    default=''
                )

                # Duration in seconds from API, convert to minutes
                duration_seconds = safe_int(p.get('duration', 0))
                duration_minutes = duration_seconds // 60 if duration_seconds > 0 else 0

                join_time = safe_str(p.get('join_time', ''))
                leave_time = safe_str(p.get('leave_time', ''))

                # Look up camera data using fuzzy matching
                camera_info = find_camera_data(camera_data_map, participant_name, participant_email)
                camera_on_count = camera_info.get('count', 0)
                camera_on_minutes = camera_info.get('minutes', 0)
                camera_on_intervals = camera_info.get('intervals', '')

                qos_data = {
                    'qos_id': str(uuid_lib.uuid4()),
                    'meeting_uuid': safe_str(meeting_uuid or meeting_id),
                    'participant_id': participant_id,
                    'participant_name': participant_name,
                    'participant_email': participant_email,
                    'join_time': join_time,
                    'leave_time': leave_time,
                    'duration_minutes': duration_minutes,
                    'attentiveness_score': safe_str(p.get('attentiveness_score', '')),
                    'camera_on_count': camera_on_count,
                    'camera_on_minutes': camera_on_minutes,
                    'camera_on_intervals': camera_on_intervals,
                    'recorded_at': datetime.utcnow().isoformat(),
                    'event_date': get_ist_date()
                }

                if insert_qos_data(qos_data):
                    collected_count += 1
                    participants_data.append({
                        'name': participant_name,
                        'email': participant_email,
                        'duration_minutes': duration_minutes
                    })
                else:
                    error_count += 1

            except Exception as pe:
                error_count += 1
                print(f"[QoS] Error processing participant: {pe}")

        return jsonify({
            'success': True,
            'collected': collected_count,
            'errors': error_count,
            'participants': participants_data[:20],  # First 20 for preview
            'total_participants': len(participants)
        })

    except Exception as e:
        print(f"[QoS] Manual collection error: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/qos/status', methods=['GET'])
def qos_status():
    """Check QoS data status for recent dates"""
    try:
        client = get_bq_client()
        query = f"""
        SELECT
            event_date,
            COUNT(*) as records,
            COUNT(DISTINCT participant_name) as participants
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_QOS_TABLE}`
        WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
        GROUP BY event_date
        ORDER BY event_date DESC
        """
        results = list(client.query(query).result())

        return jsonify({
            'success': True,
            'qos_data': [dict(row.items()) for row in results]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/qos/delete', methods=['POST'])
@require_auth(roles=['admin', 'superadmin'])
def qos_delete():
    """Delete QoS data for a specific date to allow recollection"""
    data = request.json or {}
    target_date = data.get('date')

    if not target_date:
        return jsonify({'error': 'date required'}), 400

    # Validate date format to prevent SQL injection
    try:
        datetime.strptime(target_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400

    try:
        client = get_bq_client()
        query = f"""
        DELETE FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_QOS_TABLE}`
        WHERE event_date = @target_date
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("target_date", "STRING", target_date)
            ]
        )
        job = client.query(query, job_config=job_config)
        job.result()

        return jsonify({
            'success': True,
            'message': f'Deleted QoS data for {target_date}',
            'rows_deleted': job.num_dml_affected_rows
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/qos/update-camera', methods=['POST'])
def qos_update_camera():
    """Update camera_on_count for existing QoS records from Dashboard API"""
    data = request.json or {}
    target_date = data.get('date')
    meeting_uuid = data.get('meeting_uuid')

    if not target_date:
        return jsonify({'error': 'date required'}), 400

    # Validate date format to prevent SQL injection
    try:
        datetime.strptime(target_date, '%Y-%m-%d')
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400

    try:
        client = get_bq_client()

        # Get meeting UUID and ID if not provided
        meeting_id = data.get('meeting_id')
        if not meeting_uuid:
            query = f"""
            SELECT DISTINCT meeting_uuid, meeting_id
            FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_EVENTS_TABLE}`
            WHERE event_date = @target_date
              AND meeting_uuid IS NOT NULL
            LIMIT 1
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("target_date", "STRING", target_date)
                ]
            )
            results = list(client.query(query, job_config=job_config).result())
            if not results:
                return jsonify({'error': f'No meeting found for {target_date}'}), 404
            meeting_uuid = results[0].meeting_uuid
            meeting_id = results[0].meeting_id

        # MUST use numeric meeting_id for Dashboard API - UUID does NOT work!
        if not meeting_id or not str(meeting_id).replace('-', '').isdigit():
            return jsonify({'error': 'No numeric meeting_id available - Dashboard QoS API requires numeric ID'}), 400

        print(f"[UpdateCamera] Fetching camera data for meeting using numeric ID: {meeting_id}")

        # Get camera data from Dashboard QoS API
        camera_data_map = {}
        try:
            camera_participants = zoom_api.get_meeting_participants_qos(meeting_id)
            for cp in camera_participants:
                user_name = cp.get('user_name', '')
                email = cp.get('email', '')
                camera_on_count = cp.get('camera_on_count', 0)
                camera_on_minutes = cp.get('camera_on_minutes', 0)
                camera_on_timestamps = cp.get('camera_on_timestamps', [])
                key = f"{user_name}|{email}".lower()
                camera_data_map[key] = {
                    'count': camera_on_count,
                    'minutes': camera_on_minutes,
                    'intervals': format_camera_intervals(camera_on_timestamps)
                }
            print(f"[UpdateCamera] Got camera data for {len(camera_data_map)} participants")
        except Exception as ce:
            return jsonify({'error': f'Camera API error: {ce}'}), 500

        # Update each participant's camera data
        updated = 0
        for key, camera_info in camera_data_map.items():
            count = camera_info.get('count', 0)
            minutes = camera_info.get('minutes', 0)
            intervals = camera_info.get('intervals', '').replace("'", "''")  # Escape quotes for SQL

            if count > 0 or minutes > 0:
                parts = key.split('|')
                name = parts[0] if parts else ''
                email = parts[1] if len(parts) > 1 else ''

                update_query = f"""
                UPDATE `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_QOS_TABLE}`
                SET camera_on_count = @count,
                    camera_on_minutes = @minutes,
                    camera_on_intervals = @intervals
                WHERE event_date = @target_date
                  AND LOWER(participant_name) = @name
                  AND LOWER(participant_email) = @email
                """
                job_config = bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("count", "INT64", count),
                        bigquery.ScalarQueryParameter("minutes", "FLOAT64", minutes),
                        bigquery.ScalarQueryParameter("intervals", "STRING", intervals),
                        bigquery.ScalarQueryParameter("target_date", "STRING", target_date),
                        bigquery.ScalarQueryParameter("name", "STRING", name.lower()),
                        bigquery.ScalarQueryParameter("email", "STRING", email.lower()),
                    ]
                )
                try:
                    job = client.query(update_query, job_config=job_config)
                    job.result()
                    updated += job.num_dml_affected_rows or 0
                except Exception as ue:
                    print(f"[UpdateCamera] Update error for {name}: {ue}")

        return jsonify({
            'success': True,
            'message': f'Updated camera data for {target_date}',
            'meeting_uuid': meeting_uuid,
            'participants_with_camera': len([k for k, v in camera_data_map.items() if v.get('count', 0) > 0]),
            'rows_updated': updated
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/qos/scheduled', methods=['POST'])
def qos_scheduled_collection():
    """
    Scheduled QoS collection - called by Cloud Scheduler.
    Finds yesterday's meeting UUID from BigQuery and collects QoS data.

    Can also be called with a specific date:
    POST /qos/scheduled
    Body: {"date": "2026-02-18"} (optional, defaults to yesterday)
    """
    data = request.json or {}
    target_date = data.get('date')

    if not target_date:
        # Default to yesterday in IST (not UTC - IST is 5:30 ahead)
        # This ensures correct date around midnight IST
        target_date = (get_ist_now() - timedelta(days=1)).strftime('%Y-%m-%d')
    else:
        # Validate date format to prevent SQL injection
        try:
            datetime.strptime(target_date, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400

    print(f"[ScheduledQoS] Starting collection for date: {target_date}")

    try:
        client = get_bq_client()

        # Find meeting UUID(s) and ID(s) from participant_events for that date
        query = f"""
        SELECT DISTINCT meeting_uuid, meeting_id
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_EVENTS_TABLE}`
        WHERE event_date = @target_date
          AND meeting_uuid IS NOT NULL
          AND meeting_uuid != ''
        LIMIT 5
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("target_date", "STRING", target_date)
            ]
        )
        results = list(client.query(query, job_config=job_config).result())

        if not results:
            return jsonify({
                'success': False,
                'error': f'No meetings found for date {target_date}',
                'date': target_date
            }), 404

        # Store both UUID and numeric ID
        meetings = [(row.meeting_uuid, row.meeting_id) for row in results]
        print(f"[ScheduledQoS] Found {len(meetings)} meeting(s)")

        # Check if QoS already collected for this date
        check_query = f"""
        SELECT COUNT(*) as count
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_QOS_TABLE}`
        WHERE event_date = @target_date
        """
        check_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("target_date", "STRING", target_date)
            ]
        )
        check_result = list(client.query(check_query, job_config=check_config).result())[0]
        existing_count = check_result.count

        if existing_count > 50:
            print(f"[ScheduledQoS] QoS already collected: {existing_count} records")
            return jsonify({
                'success': True,
                'message': f'QoS already collected for {target_date}',
                'existing_records': existing_count,
                'date': target_date
            })

        # Collect QoS for each meeting
        total_collected = 0
        total_errors = 0
        results_detail = []

        for meeting_uuid, meeting_id in meetings:
            print(f"[ScheduledQoS] Collecting for meeting: {meeting_uuid} (ID: {meeting_id})")

            try:
                # First, collect camera data from Dashboard QoS API
                camera_data_map = {}
                try:
                    # MUST use numeric meeting_id for Dashboard API - UUID does NOT work!
                    if not meeting_id or not str(meeting_id).replace('-', '').isdigit():
                        print(f"[ScheduledQoS] WARNING: No numeric meeting_id for {meeting_uuid}, skipping camera QoS")
                        camera_participants = []
                    else:
                        print(f"[ScheduledQoS] Collecting camera data via Dashboard QoS API using numeric ID: {meeting_id}")
                        camera_participants = zoom_api.get_meeting_participants_qos(meeting_id)
                    for cp in camera_participants:
                        user_name = cp.get('user_name', '')
                        email = cp.get('email', '')
                        camera_on_count = cp.get('camera_on_count', 0)
                        camera_on_minutes = cp.get('camera_on_minutes', 0)
                        camera_on_timestamps = cp.get('camera_on_timestamps', [])
                        key = f"{user_name}|{email}".lower()
                        camera_data_map[key] = {
                            'count': camera_on_count,
                            'minutes': camera_on_minutes,
                            'timestamps': camera_on_timestamps,
                            'intervals': format_camera_intervals(camera_on_timestamps)
                        }
                    print(f"[ScheduledQoS] Got camera data for {len(camera_data_map)} participants")
                except Exception as ce:
                    print(f"[ScheduledQoS] Camera collection error (non-fatal): {ce}")

                participants = zoom_api.get_past_meeting_participants(meeting_uuid)

                if not participants:
                    results_detail.append({
                        'meeting_uuid': meeting_uuid,
                        'status': 'no_participants'
                    })
                    continue

                collected = 0
                errors = 0

                for p in participants:
                    try:
                        participant_id = safe_str(
                            p.get('user_id') or p.get('id') or p.get('participant_user_id'),
                            default='unknown'
                        )
                        participant_name = safe_str(
                            p.get('name') or p.get('user_name'),
                            default='Unknown'
                        )
                        participant_email = safe_str(
                            p.get('user_email') or p.get('email'),
                            default=''
                        )
                        duration_seconds = safe_int(p.get('duration', 0))
                        duration_minutes = duration_seconds // 60 if duration_seconds > 0 else 0

                        # Look up camera data (now a dict with count, minutes, intervals)
                        # Look up camera data using fuzzy matching
                        camera_info = find_camera_data(camera_data_map, participant_name, participant_email)
                        camera_on_count = camera_info.get('count', 0)
                        camera_on_minutes = camera_info.get('minutes', 0)
                        camera_on_intervals = camera_info.get('intervals', '')

                        qos_data = {
                            'qos_id': str(uuid_lib.uuid4()),
                            'meeting_uuid': safe_str(meeting_uuid),
                            'participant_id': participant_id,
                            'participant_name': participant_name,
                            'participant_email': participant_email,
                            'join_time': safe_str(p.get('join_time', '')),
                            'leave_time': safe_str(p.get('leave_time', '')),
                            'duration_minutes': duration_minutes,
                            'attentiveness_score': str(p.get('attentiveness_score', '')),
                            'camera_on_count': camera_on_count,
                            'camera_on_minutes': camera_on_minutes,
                            'camera_on_intervals': camera_on_intervals,
                            'recorded_at': datetime.utcnow().isoformat(),
                            'event_date': target_date  # Use target date, not today
                        }

                        if insert_qos_data(qos_data):
                            collected += 1
                        else:
                            errors += 1

                    except Exception as pe:
                        errors += 1
                        print(f"[ScheduledQoS] Error: {pe}")

                total_collected += collected
                total_errors += errors
                results_detail.append({
                    'meeting_uuid': meeting_uuid,
                    'collected': collected,
                    'errors': errors
                })

            except Exception as me:
                print(f"[ScheduledQoS] Meeting error: {me}")
                results_detail.append({
                    'meeting_uuid': meeting_uuid,
                    'status': 'error',
                    'error': str(me)
                })

        print(f"[ScheduledQoS] Complete: {total_collected} collected, {total_errors} errors")

        # Cleanup old QoS data (older than 2 days)
        cleanup_deleted = 0
        try:
            cleanup_date = (datetime.utcnow() - timedelta(days=2)).strftime('%Y-%m-%d')
            cleanup_query = f"""
            DELETE FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_QOS_TABLE}`
            WHERE event_date < @cleanup_date
            """
            cleanup_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("cleanup_date", "STRING", cleanup_date)
                ]
            )
            cleanup_job = client.query(cleanup_query, job_config=cleanup_config)
            cleanup_job.result()
            cleanup_deleted = cleanup_job.num_dml_affected_rows or 0
            print(f"[ScheduledQoS] Cleanup: Deleted {cleanup_deleted} old QoS records (before {cleanup_date})")
        except Exception as ce:
            print(f"[ScheduledQoS] Cleanup error (non-fatal): {ce}")

        return jsonify({
            'success': True,
            'date': target_date,
            'meetings_processed': len(meetings),
            'total_collected': total_collected,
            'total_errors': total_errors,
            'cleanup_deleted': cleanup_deleted,
            'details': results_detail
        })

    except Exception as e:
        print(f"[ScheduledQoS] Error: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ==============================================================================
# ZOOM SDK APP (STATIC FILES)
# ==============================================================================

# Zoom App OAuth credentials (User-managed app)
ZOOM_APP_CLIENT_ID = os.environ.get('ZOOM_APP_CLIENT_ID', 'raEkn6HpTkWO_DCO3z5zGA')
ZOOM_APP_CLIENT_SECRET = os.environ.get('ZOOM_APP_CLIENT_SECRET', '')

@app.route('/app')
@app.route('/app/')
def serve_zoom_app():
    """Serve Zoom SDK app - handle OAuth callback if code present"""
    # Check if this is an OAuth callback with authorization code
    code = request.args.get('code')
    if code:
        print(f"[OAuth] Received authorization code: {code[:20]}...")
        # For Zoom Apps SDK, we don't need to exchange the code here
        # The SDK handles authentication internally
        # Just serve the app and let SDK initialize

    # Serve the React app
    return send_from_directory(REACT_BUILD_PATH, 'index.html')


@app.route('/app/<path:path>', methods=['GET', 'POST'])
def serve_zoom_app_static(path):
    """Serve Zoom SDK app static files or forward API calls"""
    # Forward API calls to actual endpoints
    if path.startswith('calibration/'):
        if request.method == 'POST':
            # Forward to calibration endpoints
            if path == 'calibration/start':
                return calibration_start()
            elif path == 'calibration/mapping':
                return calibration_mapping()
            elif path == 'calibration/complete':
                return calibration_complete()
        elif request.method == 'GET':
            if path == 'calibration/status':
                return calibration_status()

    # Serve static files
    return send_from_directory(REACT_BUILD_PATH, path)


# ==============================================================================
# DEBUG ENDPOINTS
# ==============================================================================

@app.route('/debug/state', methods=['GET'])
def debug_state():
    """Debug current state"""
    return jsonify({
        'meeting': {
            'id': meeting_state.meeting_id,
            'uuid': meeting_state.meeting_uuid,
            'date': meeting_state.meeting_date,
            'calibration_complete': meeting_state.calibration_complete
        },
        'rooms_mapped': len(meeting_state.uuid_to_name),
        'participants_tracked': len(meeting_state.participant_states),
        'participant_states': {
            k: v for k, v in list(meeting_state.participant_states.items())[:10]
        }
    })


@app.route('/debug/rooms', methods=['GET'])
def debug_rooms():
    """Get all participants grouped by room with names - for accuracy verification"""
    today = get_ist_date()

    # Query BigQuery for latest room each participant is in
    query = f"""
    WITH latest_room_events AS (
      SELECT
        participant_name,
        participant_email,
        room_name,
        event_type,
        event_timestamp,
        ROW_NUMBER() OVER (
          PARTITION BY participant_id
          ORDER BY event_timestamp DESC
        ) as rn
      FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_EVENTS_TABLE}`
      WHERE event_date = @today
        AND event_type IN ('breakout_room_joined', 'breakout_room_left')
        AND participant_name NOT LIKE '%Scout%'
    ),
    current_rooms AS (
      SELECT
        participant_name,
        CASE
          WHEN event_type = 'breakout_room_joined' THEN room_name
          ELSE 'Main Room'
        END as current_room
      FROM latest_room_events
      WHERE rn = 1
    )
    SELECT current_room, STRING_AGG(participant_name, ', ' ORDER BY participant_name) as participants
    FROM current_rooms
    GROUP BY current_room
    ORDER BY current_room
    """

    try:
        client = get_bq_client()
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("today", "STRING", today)
            ]
        )
        results = list(client.query(query, job_config=job_config).result())

        rooms = {}
        total = 0
        for row in results:
            room = row.current_room or 'Unknown'
            participants = row.participants.split(', ') if row.participants else []
            rooms[room] = participants
            total += len(participants)

        return jsonify({
            'meeting_id': meeting_state.meeting_id,
            'date': today,
            'total_rooms': len(rooms),
            'total_participants': total,
            'rooms': rooms
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/debug/reset', methods=['POST'])
@require_auth(roles=['admin', 'superadmin'])
def debug_reset():
    """Reset meeting state (for testing)"""
    meeting_state.reset()
    return jsonify({'status': 'reset', 'message': 'State cleared'})


@app.route('/test/bigquery', methods=['GET'])
def test_bigquery():
    """Test BigQuery connection and show config"""
    results = {
        'config': {
            'project_id': GCP_PROJECT_ID,
            'dataset': BQ_DATASET,
            'events_table': BQ_EVENTS_TABLE,
            'camera_table': BQ_CAMERA_TABLE,
            'qos_table': BQ_QOS_TABLE,
            'mappings_table': BQ_MAPPINGS_TABLE
        },
        'tables': {}
    }

    if not GCP_PROJECT_ID:
        results['error'] = 'GCP_PROJECT_ID not configured!'
        return jsonify(results), 500

    try:
        client = get_bq_client()

        # Test each table - use partition filter for tables that require it
        today = get_ist_date()

        for table_name, table_var in [
            ('participant_events', BQ_EVENTS_TABLE),
            ('camera_events', BQ_CAMERA_TABLE),
            ('qos_data', BQ_QOS_TABLE),
            ('room_mappings', BQ_MAPPINGS_TABLE)
        ]:
            try:
                # camera_events requires partition filter
                if table_var == BQ_CAMERA_TABLE:
                    query = f"SELECT COUNT(*) as count FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{table_var}` WHERE event_date = @today"
                    job_config = bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("today", "STRING", today)
                        ]
                    )
                    result = list(client.query(query, job_config=job_config).result())
                else:
                    query = f"SELECT COUNT(*) as count FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{table_var}`"
                    result = list(client.query(query).result())
                count = result[0]['count'] if result else 0
                results['tables'][table_name] = {'status': 'OK', 'count': count}
            except Exception as te:
                results['tables'][table_name] = {'status': 'ERROR', 'error': str(te)}

        results['status'] = 'BigQuery OK'
        return jsonify(results)

    except Exception as e:
        results['status'] = 'ERROR'
        results['error'] = str(e)
        return jsonify(results), 500


@app.route('/test/webhook-insert', methods=['POST'])
@require_auth(roles=['admin', 'superadmin'])
def test_webhook_insert():
    """
    Test endpoint to simulate a webhook and verify BigQuery insert.
    POST with optional JSON body to test with custom data.
    """
    test_data = request.json or {}

    # Create test event
    test_event = {
        'event_id': str(uuid_lib.uuid4()),
        'event_type': test_data.get('event_type', 'test_event'),
        'event_timestamp': datetime.utcnow().isoformat(),
        'event_date': get_ist_date(),
        'meeting_id': test_data.get('meeting_id', 'test_meeting_123'),
        'meeting_uuid': test_data.get('meeting_uuid', 'test_uuid_123'),
        'participant_id': test_data.get('participant_id', 'test_participant'),
        'participant_name': test_data.get('participant_name', 'Test User'),
        'participant_email': test_data.get('participant_email', 'test@example.com'),
        'room_uuid': test_data.get('room_uuid', ''),
        'room_name': test_data.get('room_name', 'Test Room'),
        'inserted_at': datetime.utcnow().isoformat()
    }

    print(f"[TEST] Inserting test event: {json.dumps(test_event, indent=2)}")

    success = insert_participant_event(test_event)

    return jsonify({
        'test_event': test_event,
        'insert_success': success,
        'config': {
            'project_id': GCP_PROJECT_ID,
            'dataset': BQ_DATASET,
            'table': BQ_EVENTS_TABLE
        }
    }), 200 if success else 500


@app.route('/test/qos-insert', methods=['POST'])
@require_auth(roles=['admin', 'superadmin'])
def test_qos_insert():
    """Test QoS data insert with sample data"""
    test_data = request.json or {}

    qos_event = {
        'qos_id': str(uuid_lib.uuid4()),
        'meeting_uuid': test_data.get('meeting_uuid', 'test_meeting_uuid'),
        'participant_id': test_data.get('participant_id', 'test_participant'),
        'participant_name': test_data.get('participant_name', 'Test User'),
        'participant_email': test_data.get('participant_email', 'test@example.com'),
        'join_time': test_data.get('join_time', datetime.utcnow().isoformat()),
        'leave_time': test_data.get('leave_time', datetime.utcnow().isoformat()),
        'duration_minutes': test_data.get('duration_minutes', 45),
        'attentiveness_score': test_data.get('attentiveness_score', '95'),
        'recorded_at': datetime.utcnow().isoformat(),
        'event_date': get_ist_date()
    }

    print(f"[TEST] Inserting test QoS: {json.dumps(qos_event, indent=2)}")

    success = insert_qos_data(qos_event)

    return jsonify({
        'qos_event': qos_event,
        'insert_success': success,
        'config': {
            'project_id': GCP_PROJECT_ID,
            'dataset': BQ_DATASET,
            'table': BQ_QOS_TABLE
        }
    }), 200 if success else 500


@app.route('/test/camera-qos', methods=['GET', 'POST'])
def test_camera_qos():
    """
    Test Dashboard QoS API to get camera status via video_output stats.

    GET: Use current meeting ID
    POST: {"meeting_id": "123456"} to specify meeting

    Requires: Business+ plan and dashboard_meetings:read:admin scope
    """
    data = request.json or {}
    meeting_id = data.get('meeting_id') or meeting_state.meeting_id

    if not meeting_id:
        return jsonify({
            'success': False,
            'error': 'No meeting_id provided and no active meeting',
            'hint': 'POST with {"meeting_id": "your_meeting_id"}'
        }), 400

    # Page limit for quick searches (default 20 pages = 200 participants)
    page_limit = data.get('page_limit', 20)
    print(f"[TestCameraQoS] Fetching camera data for meeting: {meeting_id} (max {page_limit} pages)")

    try:
        # Optional search parameter
        search_name = data.get('search', '').lower()

        participants = zoom_api.get_meeting_participants_qos(meeting_id, max_pages=page_limit)

        if not participants:
            return jsonify({
                'success': False,
                'error': 'No QoS data returned - may require Business+ plan or dashboard_meetings:read:admin scope',
                'meeting_id': meeting_id
            }), 404

        # Get sample raw QoS entry for debugging
        sample_raw_qos = None
        if participants and participants[0].get('user_qos'):
            sample_raw_qos = participants[0]['user_qos'][0]

        # Format results
        camera_data = []
        for p in participants:
            camera_on_timestamps = p.get('camera_on_timestamps', [])
            user_qos = p.get('user_qos', [])

            # Check if any video_output exists in user_qos
            has_video_output = any(qe.get('video_output') for qe in user_qos)

            camera_data.append({
                'user_id': p.get('user_id'),
                'user_name': p.get('user_name'),
                'email': p.get('email', ''),
                'join_time': p.get('join_time'),
                'leave_time': p.get('leave_time'),
                'camera_on_periods': p.get('camera_on_periods', []),
                'camera_on_count': p.get('camera_on_count', 0),
                'camera_on_minutes': p.get('camera_on_minutes', 0),
                'camera_on_timestamps': camera_on_timestamps,
                'camera_on_intervals_ist': format_camera_intervals(camera_on_timestamps),
                'raw_user_qos_count': len(user_qos),
                'has_video_output': has_video_output
            })

        # Filter by search if provided
        if search_name:
            camera_data = [p for p in camera_data if search_name in p.get('user_name', '').lower() or search_name in p.get('email', '').lower()]
            return jsonify({
                'success': True,
                'meeting_id': meeting_id,
                'search': search_name,
                'matches_found': len(camera_data),
                'camera_data': camera_data,
                'note': 'Filtered by search term'
            })

        return jsonify({
            'success': True,
            'meeting_id': meeting_id,
            'total_participants': len(camera_data),
            'sample_raw_qos_entry': sample_raw_qos,  # For debugging - see actual Zoom response
            'participants_with_camera': sum(1 for p in camera_data if p['camera_on_count'] > 0),
            'camera_data': camera_data[:50],  # Return 50 for preview, use search for specific
            'note': 'Use {"search": "name"} to find specific participant'
        })

    except Exception as e:
        print(f"[TestCameraQoS] Error: {e}")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e),
            'meeting_id': meeting_id
        }), 500


# ==============================================================================
# ATTENDANCE DASHBOARD - Live View + Heatmap + Direct BigQuery Access
# ==============================================================================

@app.route('/attendance/live', methods=['GET'])
def attendance_live():
    """
    Real-time: Who's in which room RIGHT NOW, from presence_intervals
    (rebuilt every 5 min by the BQ scheduled query calling
    sp_build_presence_intervals).

    GET /attendance/live
    GET /attendance/live?date=2026-04-03

    Default date is the BUSINESS date (05:00->05:00 IST): at 01:00 IST an
    ongoing shift is filed under yesterday's business date, so querying the
    midnight-based "today" would show an empty office.
    """
    target_date = request.args.get('date', get_business_date())
    try:
        target_date = validate_date_format(target_date)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    try:
        client = get_bq_client()
        # "Now" = the latest interval end_ts (the last 5-min build).
        # Anyone whose interval ends within 90s of that is still present;
        # people who left earlier have older end_ts and drop out.
        query = f"""
            WITH ivs AS (
              SELECT
                participant_name,
                participant_email,
                room_name,
                start_ts,
                end_ts,
                COALESCE(NULLIF(LOWER(TRIM(participant_email)), ''),
                         LOWER(TRIM(participant_name))) AS pkey
              FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{PRESENCE_INTERVALS_TABLE}`
              WHERE event_date = '{target_date}'
                AND participant_name NOT LIKE '%Scout%'
                AND room_name IS NOT NULL AND room_name != ''
            ),
            latest AS (SELECT MAX(end_ts) AS max_time FROM ivs),
            all_rooms AS (SELECT DISTINCT room_name FROM ivs),
            current_state AS (
              SELECT
                room_name,
                participant_name,
                participant_email,
                CAST(NULL AS STRING) AS participant_uuid,
                end_ts AS snapshot_time
              FROM ivs CROSS JOIN latest
              WHERE end_ts >= TIMESTAMP_SUB(latest.max_time, INTERVAL 90 SECOND)
              QUALIFY ROW_NUMBER() OVER (
                PARTITION BY pkey ORDER BY end_ts DESC, start_ts DESC
              ) = 1
            )
            SELECT
              ar.room_name,
              ARRAY_AGG(
                STRUCT(cs.participant_name, cs.participant_email, cs.participant_uuid)
              ) as participants,
              COUNTIF(cs.participant_name IS NOT NULL) as participant_count,
              MAX(cs.snapshot_time) as snapshot_time
            FROM all_rooms ar
            LEFT JOIN current_state cs ON ar.room_name = cs.room_name
            GROUP BY ar.room_name
            ORDER BY ar.room_name
            """
        results = list(client.query(query).result())

        rooms = []
        total_people = 0
        occupied_count = 0
        for row in results:
            count = row.get('participant_count', 0)
            # Filter out null participant entries from LEFT JOIN
            participants = [dict(p) for p in row.get('participants', []) if p.get('participant_name')]
            rooms.append({
                'room_name': row.get('room_name', ''),
                'participant_count': count,
                'participants': participants
            })
            total_people += count
            if count > 0:
                occupied_count += 1

        snapshot_time = ''
        for row in results:
            st = row.get('snapshot_time')
            if st:
                snapshot_time = str(st)
                break

        # Merge duplicate participant names within rooms
        rooms = merge_live_rooms(rooms)
        total_people = sum(r['participant_count'] for r in rooms)
        occupied_count = sum(1 for r in rooms if r['participant_count'] > 0)

        # Staleness check: the table is rebuilt every 5 min by the BQ
        # scheduled query, so allow 2x that (plus build runtime) before
        # flagging STALE — a 5-min threshold would flap on every cycle.
        data_status = 'NO_DATA'
        stale_seconds = None
        if snapshot_time:
            try:
                from datetime import datetime
                st_parsed = datetime.fromisoformat(snapshot_time.replace('Z', '+00:00')) if 'T' in snapshot_time else datetime.strptime(snapshot_time, '%Y-%m-%d %H:%M:%S.%f %Z') if ' ' in snapshot_time else None
                if st_parsed is None:
                    st_parsed = datetime.strptime(snapshot_time[:19], '%Y-%m-%d %H:%M:%S')
                age = (datetime.utcnow() - st_parsed.replace(tzinfo=None)).total_seconds()
                stale_seconds = int(age)
                data_status = 'HEALTHY' if age < 660 else 'STALE'
            except Exception:
                data_status = 'UNKNOWN'

        return jsonify({
            'success': True,
            'date': target_date,
            'snapshot_time': snapshot_time,
            'data_status': data_status,
            'stale_seconds': stale_seconds,
            'total_rooms': len(rooms),
            'total_rooms_occupied': occupied_count,
            'total_participants': total_people,
            'rooms': rooms
        })

    except Exception as e:
        print(f"[Attendance] Live error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/attendance/summary', methods=['GET'])
@app.route('/attendance/heatmap', methods=['GET'])
@app.route('/attendance/heatmap/<date>', methods=['GET'])
def attendance_heatmap(date=None):
    """
    Room utilization heatmap: participant count per room per 15-min slot.
    Shows which rooms are overcrowded vs empty over time.

    GET /attendance/heatmap/2026-04-03
    GET /attendance/heatmap?date=2026-04-03&interval=30
    """
    if date is None:
        date = request.args.get('date', get_business_date())
    try:
        date = validate_date_format(date)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    interval = request.args.get('interval', '15')
    try:
        interval = int(interval)
        if interval not in (5, 10, 15, 30, 60):
            interval = 15
    except ValueError:
        interval = 15

    try:
        client = get_bq_client()
        # Occupancy from presence_intervals — each interval contributes to
        # every time slot it overlaps; same data every report reads.
        head = f"""
        WITH ivs AS (
          SELECT
            room_name,
            COALESCE(NULLIF(LOWER(TRIM(participant_email)), ''),
                     LOWER(TRIM(participant_name))) as participant_key,
            TIMESTAMP_ADD(start_ts, INTERVAL 330 MINUTE) as start_ist,
            TIMESTAMP_ADD(end_ts, INTERVAL 330 MINUTE) as end_ist
          FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{PRESENCE_INTERVALS_TABLE}`
          WHERE event_date = '{date}'
            AND participant_name NOT LIKE '%Scout%'
            AND room_name IS NOT NULL AND room_name != ''
        ),
        -- Expand each interval into the {interval}-min slots it overlaps
        time_bucketed AS (
          SELECT
            room_name,
            participant_key,
            FORMAT_TIMESTAMP('%H:%M', slot) as time_slot
          FROM ivs,
          UNNEST(GENERATE_TIMESTAMP_ARRAY(
            TIMESTAMP_SECONDS(DIV(UNIX_SECONDS(start_ist), {interval} * 60) * {interval} * 60),
            end_ist,
            INTERVAL {interval} MINUTE
          )) AS slot
        ),
            """
        query = head + f"""
        -- Count distinct participants per room per slot (by UUID-based key)
        room_slot_counts AS (
          SELECT
            room_name,
            time_slot,
            COUNT(DISTINCT participant_key) as participant_count
          FROM time_bucketed
          GROUP BY room_name, time_slot
        ),
        -- Room summary stats
        room_stats AS (
          SELECT
            room_name,
            MAX(participant_count) as peak_count,
            AVG(participant_count) as avg_count,
            COUNT(DISTINCT time_slot) as active_slots
          FROM room_slot_counts
          GROUP BY room_name
        ),
        -- All time slots
        all_slots AS (
          SELECT DISTINCT time_slot FROM room_slot_counts
        )
        SELECT
          rs.room_name,
          rs.peak_count,
          ROUND(rs.avg_count, 1) as avg_count,
          rs.active_slots,
          ARRAY_AGG(
            STRUCT(rsc.time_slot, rsc.participant_count)
            ORDER BY rsc.time_slot
          ) as time_slots
        FROM room_stats rs
        JOIN room_slot_counts rsc ON rs.room_name = rsc.room_name
        GROUP BY rs.room_name, rs.peak_count, rs.avg_count, rs.active_slots
        ORDER BY rs.peak_count DESC, rs.room_name
        """
        results = list(client.query(query).result())

        # Build heatmap data
        rooms = []
        all_time_slots = set()
        for row in results:
            slots = {}
            for s in row.get('time_slots', []):
                slot_key = s.get('time_slot', '')
                slots[slot_key] = s.get('participant_count', 0)
                all_time_slots.add(slot_key)

            rooms.append({
                'room_name': row.get('room_name', ''),
                'peak_count': row.get('peak_count', 0),
                'avg_count': float(row.get('avg_count', 0)),
                'active_slots': row.get('active_slots', 0),
                'time_slots': slots
            })

        sorted_slots = sorted(all_time_slots)

        return jsonify({
            'success': True,
            'date': date,
            'interval_minutes': interval,
            'time_slots': sorted_slots,
            'total_rooms': len(rooms),
            'rooms': rooms
        })

    except Exception as e:
        print(f"[Attendance] Heatmap error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# TEAM MANAGEMENT - CRUD for teams and members
# ==============================================================================

def ensure_team_tables():
    """Create teams and team_members tables if they don't exist"""
    client = get_bq_client()
    dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

    # Teams table
    teams_sql = f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.{BQ_TEAMS_TABLE}` (
        team_id STRING NOT NULL,
        team_name STRING NOT NULL,
        manager_name STRING,
        manager_email STRING,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    client.query(teams_sql).result()

    # Team members table
    members_sql = f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` (
        member_id STRING NOT NULL,
        team_id STRING NOT NULL,
        participant_name STRING NOT NULL,
        participant_email STRING,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    client.query(members_sql).result()

    # Employee registry - master list of all known participants
    registry_sql = f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.employee_registry` (
        employee_id STRING NOT NULL,
        participant_name STRING NOT NULL,
        display_name STRING,
        participant_email STRING,
        status STRING DEFAULT 'active',
        category STRING DEFAULT 'employee',
        team_id STRING,
        notes STRING,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    client.query(registry_sql).result()

    # Participant aliases - maps a Zoom display name variant to a roster
    # member, so "Anurag_Accurest" links to team member "Anurag Gaigowal"
    # without renaming either. Used by the v2 team identity bridge.
    aliases_sql = f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.participant_aliases` (
        alias_id STRING NOT NULL,
        alias_name STRING NOT NULL,
        member_name STRING NOT NULL,
        member_email STRING,
        created_by STRING,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    client.query(aliases_sql).result()

    # Team holidays - per-team holiday dates (so different teams can have
    # different holiday calendars)
    holidays_sql = f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.{BQ_TEAM_HOLIDAYS_TABLE}` (
        holiday_id STRING NOT NULL,
        team_id STRING NOT NULL,
        holiday_date DATE NOT NULL,
        description STRING,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    client.query(holidays_sql).result()

    # Employee leave - individual employee leave/holiday dates
    leave_sql = f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.{BQ_EMPLOYEE_LEAVE_TABLE}` (
        leave_id STRING NOT NULL,
        employee_id STRING NOT NULL,
        employee_name STRING NOT NULL,
        leave_date DATE NOT NULL,
        leave_type STRING DEFAULT 'leave',
        description STRING,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    client.query(leave_sql).result()

    # Attendance overrides - manual corrections to attendance data
    overrides_sql = f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.{BQ_ATTENDANCE_OVERRIDES_TABLE}` (
        override_id STRING NOT NULL,
        employee_id STRING,
        employee_name STRING NOT NULL,
        event_date DATE NOT NULL,
        first_seen_ist STRING,
        last_seen_ist STRING,
        status STRING,
        active_mins INT64,
        break_mins INT64,
        isolation_mins INT64,
        notes STRING,
        created_by STRING,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    client.query(overrides_sql).result()
    print("[Teams] Tables ensured")


_team_tables_ensured = False

def ensure_team_tables_once():
    global _team_tables_ensured
    if not _team_tables_ensured:
        ensure_team_tables()
        _team_tables_ensured = True


@app.route('/teams', methods=['GET'])
def list_teams():
    """List all teams with member counts"""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        query = f"""
        SELECT t.team_id, t.team_name, t.manager_name, t.manager_email,
               t.created_at, t.updated_at,
               COUNT(m.member_id) as member_count
        FROM `{dataset_ref}.{BQ_TEAMS_TABLE}` t
        LEFT JOIN `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` m ON t.team_id = m.team_id
        GROUP BY t.team_id, t.team_name, t.manager_name, t.manager_email, t.created_at, t.updated_at
        ORDER BY t.team_name
        """
        rows = list(client.query(query).result())
        teams = []
        for r in rows:
            teams.append({
                'team_id': r.team_id,
                'team_name': r.team_name,
                'manager_name': r.manager_name or '',
                'manager_email': r.manager_email or '',
                'member_count': r.member_count,
                'created_at': r.created_at.isoformat() if r.created_at else None,
                'updated_at': r.updated_at.isoformat() if r.updated_at else None
            })
        return jsonify({'success': True, 'teams': teams})
    except Exception as e:
        print(f"[Teams] List error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/fix-managers', methods=['POST'])
@require_auth(roles=['admin', 'superadmin'])
def fix_team_managers():
    """Set manager_name = team_name for all teams where manager is empty.
    This enables manager-based filtering for team logins.
    """
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # Update all teams: set manager_name = team_name where manager is empty
        query = f"""
        UPDATE `{dataset_ref}.{BQ_TEAMS_TABLE}`
        SET manager_name = team_name, updated_at = CURRENT_TIMESTAMP()
        WHERE manager_name IS NULL OR TRIM(manager_name) = ''
        """
        job = client.query(query)
        job.result()
        updated_count = job.num_dml_affected_rows or 0

        print(f"[Teams] Fixed {updated_count} team manager_names")
        return jsonify({
            'success': True,
            'message': f'Updated {updated_count} teams',
            'updated_count': updated_count
        })
    except Exception as e:
        print(f"[Teams] Fix managers error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams', methods=['POST'])
@require_auth()
def create_team():
    """Create a new team"""
    try:
        ensure_team_tables_once()
        data = request.json or {}
        team_name = (data.get('team_name') or '').strip()
        if not team_name:
            return jsonify({'success': False, 'error': 'team_name is required'}), 400

        manager_name = (data.get('manager_name') or '').strip()
        manager_email = (data.get('manager_email') or '').strip()
        team_id = str(uuid_lib.uuid4())

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        table_ref = f"{dataset_ref}.{BQ_TEAMS_TABLE}"

        rows = [{'team_id': team_id, 'team_name': team_name,
                 'manager_name': manager_name, 'manager_email': manager_email,
                 'created_at': datetime.utcnow().isoformat(),
                 'updated_at': datetime.utcnow().isoformat()}]
        errors = client.insert_rows_json(table_ref, rows)
        if errors:
            return jsonify({'success': False, 'error': str(errors)}), 500

        print(f"[Teams] Created team '{team_name}' ({team_id})")
        return jsonify({'success': True, 'team_id': team_id, 'team_name': team_name})
    except Exception as e:
        print(f"[Teams] Create error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>', methods=['GET'])
def get_team(team_id):
    """Get team details with all members"""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # Get team info
        team_q = f"""
        SELECT team_id, team_name, manager_name, manager_email, created_at, updated_at
        FROM `{dataset_ref}.{BQ_TEAMS_TABLE}`
        WHERE team_id = @team_id
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]
        )
        team_rows = list(client.query(team_q, job_config=job_config).result())
        if not team_rows:
            return jsonify({'success': False, 'error': 'Team not found'}), 404

        t = team_rows[0]

        # Get members
        members_q = f"""
        SELECT member_id, participant_name, participant_email, added_at
        FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}`
        WHERE team_id = @team_id
        ORDER BY participant_name
        """
        member_rows = list(client.query(members_q, job_config=job_config).result())
        members = [{'member_id': m.member_id, 'participant_name': m.participant_name,
                     'participant_email': m.participant_email or '',
                     'added_at': m.added_at.isoformat() if m.added_at else None}
                    for m in member_rows]

        return jsonify({
            'success': True,
            'team': {
                'team_id': t.team_id, 'team_name': t.team_name,
                'manager_name': t.manager_name or '', 'manager_email': t.manager_email or '',
                'members': members
            }
        })
    except Exception as e:
        print(f"[Teams] Get error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>', methods=['PUT'])
@require_auth()
def update_team(team_id):
    """Update team name/manager"""
    try:
        ensure_team_tables_once()
        data = request.json or {}
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        updates = []
        params = [bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]

        if 'team_name' in data:
            updates.append("team_name = @team_name")
            params.append(bigquery.ScalarQueryParameter("team_name", "STRING", str(data['team_name'] or '').strip()))
        if 'manager_name' in data:
            updates.append("manager_name = @manager_name")
            params.append(bigquery.ScalarQueryParameter("manager_name", "STRING", str(data['manager_name'] or '').strip()))
        if 'manager_email' in data:
            updates.append("manager_email = @manager_email")
            params.append(bigquery.ScalarQueryParameter("manager_email", "STRING", str(data['manager_email'] or '').strip()))

        if not updates:
            return jsonify({'success': False, 'error': 'No fields to update'}), 400

        updates.append("updated_at = CURRENT_TIMESTAMP()")

        query = f"""
        UPDATE `{dataset_ref}.{BQ_TEAMS_TABLE}`
        SET {', '.join(updates)}
        WHERE team_id = @team_id
        """
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        client.query(query, job_config=job_config).result()

        print(f"[Teams] Updated team {team_id}")
        return jsonify({'success': True})
    except Exception as e:
        print(f"[Teams] Update error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>', methods=['DELETE'])
@require_auth(roles=['admin', 'superadmin'])
def delete_team(team_id):
    """Delete team and all its members"""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]
        )

        # Delete members first
        client.query(
            f"DELETE FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` WHERE team_id = @team_id",
            job_config=job_config
        ).result()

        # Delete team
        client.query(
            f"DELETE FROM `{dataset_ref}.{BQ_TEAMS_TABLE}` WHERE team_id = @team_id",
            job_config=job_config
        ).result()

        print(f"[Teams] Deleted team {team_id}")
        return jsonify({'success': True})
    except Exception as e:
        print(f"[Teams] Delete error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>/members', methods=['POST'])
@require_auth()
def add_team_member(team_id):
    """Add a member to a team"""
    try:
        ensure_team_tables_once()
        data = request.json or {}
        participant_name = (data.get('participant_name') or '').strip()
        if not participant_name:
            return jsonify({'success': False, 'error': 'participant_name is required'}), 400

        participant_email = (data.get('participant_email') or '').strip()

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        table_ref = f"{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}"

        # Check for duplicate member (case-insensitive name match)
        dup_check_query = f"""
            SELECT member_id FROM `{table_ref}`
            WHERE team_id = @team_id AND LOWER(TRIM(participant_name)) = LOWER(@name)
            LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
                bigquery.ScalarQueryParameter("name", "STRING", participant_name)
            ]
        )
        dup_result = list(client.query(dup_check_query, job_config=job_config).result())
        if dup_result:
            return jsonify({'success': False, 'error': f"'{participant_name}' is already a member of this team"}), 409

        member_id = str(uuid_lib.uuid4())
        rows = [{'member_id': member_id, 'team_id': team_id,
                 'participant_name': participant_name,
                 'participant_email': participant_email,
                 'added_at': datetime.utcnow().isoformat()}]
        errors = client.insert_rows_json(table_ref, rows)
        if errors:
            return jsonify({'success': False, 'error': str(errors)}), 500

        print(f"[Teams] Added member '{participant_name}' to team {team_id}")
        return jsonify({'success': True, 'member_id': member_id})
    except Exception as e:
        print(f"[Teams] Add member error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>/members/<member_id>', methods=['DELETE'])
@require_auth()
def remove_team_member(team_id, member_id):
    """Remove a member from a team"""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
                bigquery.ScalarQueryParameter("member_id", "STRING", member_id)
            ]
        )
        client.query(
            f"DELETE FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` WHERE team_id = @team_id AND member_id = @member_id",
            job_config=job_config
        ).result()

        print(f"[Teams] Removed member {member_id} from team {team_id}")
        return jsonify({'success': True})
    except Exception as e:
        print(f"[Teams] Remove member error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/members/all', methods=['GET'])
def list_all_team_members():
    """Every roster member across every team — feeds the 'link alias to
    member' dropdown in the Team View unmatched-participants banner."""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        q = f"""
        SELECT tm.participant_name AS member_name,
               tm.participant_email AS member_email,
               tm.team_id,
               ANY_VALUE(t.team_name) AS team_name
        FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` tm
        LEFT JOIN `{dataset_ref}.{BQ_TEAMS_TABLE}` t ON t.team_id = tm.team_id
        GROUP BY tm.participant_name, tm.participant_email, tm.team_id
        ORDER BY member_name
        """
        rows = list(client.query(q).result())
        members = [{'member_name': r.member_name,
                    'member_email': r.member_email or '',
                    'team_id': r.team_id,
                    'team_name': r.team_name or ''} for r in rows]
        return jsonify({'success': True, 'members': members})
    except Exception as e:
        print(f"[Teams] all-members error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/aliases', methods=['GET'])
def list_aliases():
    """List participant aliases (Zoom name variant -> roster member)."""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        rows = list(client.query(
            f"SELECT alias_id, alias_name, member_name, member_email FROM `{dataset_ref}.participant_aliases` ORDER BY alias_name"
        ).result())
        return jsonify({'success': True, 'aliases': [
            {'alias_id': r.alias_id, 'alias_name': r.alias_name,
             'member_name': r.member_name, 'member_email': r.member_email or ''}
            for r in rows
        ]})
    except Exception as e:
        print(f"[Aliases] list error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/aliases', methods=['POST'])
@require_auth()
def create_alias():
    """Link a Zoom display-name variant to a roster member.
    Body: {"alias_name": "Anurag_Accurest", "member_name": "Anurag Gaigowal",
           "member_email": "" (optional)}
    Takes effect for TODAY on the next view load (today is always rebuilt);
    past days pick it up when rebuilt/backfilled."""
    try:
        ensure_team_tables_once()
        data = request.json or {}
        alias_name = (data.get('alias_name') or '').strip()
        member_name = (data.get('member_name') or '').strip()
        member_email = (data.get('member_email') or '').strip()
        if not alias_name or not member_name:
            return jsonify({'success': False, 'error': 'alias_name and member_name are required'}), 400
        if alias_name.lower() == member_name.lower():
            return jsonify({'success': False, 'error': 'alias and member name are identical — no alias needed'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        alias_id = str(uuid_lib.uuid4())
        # DML insert (not streaming) so a later DELETE works immediately —
        # streaming-buffer rows cannot be deleted for ~90 minutes.
        q = f"""
        INSERT INTO `{dataset_ref}.participant_aliases`
            (alias_id, alias_name, member_name, member_email, created_at)
        VALUES (@alias_id, @alias_name, @member_name, @member_email, CURRENT_TIMESTAMP())
        """
        client.query(q, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("alias_id", "STRING", alias_id),
            bigquery.ScalarQueryParameter("alias_name", "STRING", alias_name),
            bigquery.ScalarQueryParameter("member_name", "STRING", member_name),
            bigquery.ScalarQueryParameter("member_email", "STRING", member_email),
        ])).result()
        print(f"[Aliases] Linked '{alias_name}' -> '{member_name}'")
        return jsonify({'success': True, 'alias_id': alias_id})
    except Exception as e:
        print(f"[Aliases] create error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/aliases/<alias_id>', methods=['DELETE'])
@require_auth()
def delete_alias(alias_id):
    """Remove an alias link."""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        client.query(
            f"DELETE FROM `{dataset_ref}.participant_aliases` WHERE alias_id = @alias_id",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("alias_id", "STRING", alias_id),
            ])
        ).result()
        return jsonify({'success': True})
    except Exception as e:
        print(f"[Aliases] delete error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>/members/bulk', methods=['POST'])
@require_auth()
def bulk_add_team_members(team_id):
    """Bulk add members from CSV data. Accepts JSON array of {name, email} objects.
    Deduplicates against existing members. Normalizes names for matching."""
    try:
        ensure_team_tables_once()
        data = request.json or {}
        members_list = data.get('members', [])
        if not members_list:
            return jsonify({'success': False, 'error': 'members array is required'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        table_ref = f"{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}"

        # Get existing members for dedup
        existing_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]
        )
        existing = list(client.query(
            f"SELECT participant_name FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` WHERE team_id = @team_id",
            job_config=existing_config
        ).result())
        existing_names = {normalize_participant_name(m.participant_name).lower().strip() for m in existing}

        # Add new members, skip duplicates
        added = 0
        skipped = 0
        rows_to_insert = []
        for m in members_list:
            name = (m.get('name') or m.get('participant_name') or '').strip()
            email = (m.get('email') or m.get('participant_email') or '').strip()
            if not name:
                continue
            normalized = normalize_participant_name(name)
            key = normalized.lower().strip()
            if key in existing_names:
                skipped += 1
                continue
            existing_names.add(key)
            rows_to_insert.append({
                'member_id': str(uuid_lib.uuid4()),
                'team_id': team_id,
                'participant_name': normalized,
                'participant_email': email,
                'added_at': datetime.utcnow().isoformat()
            })
            added += 1

        if rows_to_insert:
            errors = client.insert_rows_json(table_ref, rows_to_insert)
            if errors:
                return jsonify({'success': False, 'error': str(errors)}), 500

        print(f"[Teams] Bulk add to {team_id}: {added} added, {skipped} skipped (duplicates)")
        return jsonify({'success': True, 'added': added, 'skipped': skipped})
    except Exception as e:
        print(f"[Teams] Bulk add error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/bulk-import', methods=['POST'])
@require_auth(roles=['admin', 'superadmin'])
def bulk_import_teams_and_members():
    """Bulk import from CSV: auto-create teams + add members.
    Accepts JSON: { members: [{name, email, team_name, manager_name?, manager_email?}] }
    Teams are auto-created if they don't exist. Members are deduplicated."""
    try:
        ensure_team_tables_once()
        data = request.json or {}
        members_list = data.get('members', [])
        if not members_list:
            return jsonify({'success': False, 'error': 'members array is required'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # Get existing teams
        existing_teams = {}
        for r in client.query(f"SELECT team_id, team_name FROM `{dataset_ref}.{BQ_TEAMS_TABLE}`").result():
            existing_teams[r.team_name.lower().strip()] = r.team_id

        # Group members by team
        teams_to_create = {}
        for m in members_list:
            team_name = (m.get('team_name') or m.get('team') or '').strip()
            if not team_name:
                continue
            key = team_name.lower().strip()
            if key not in teams_to_create:
                teams_to_create[key] = {
                    'team_name': team_name,
                    'manager_name': (m.get('manager_name') or m.get('manager') or '').strip(),
                    'manager_email': (m.get('manager_email') or '').strip(),
                    'members': []
                }
            name = (m.get('name') or m.get('participant_name') or '').strip()
            email = (m.get('email') or m.get('participant_email') or '').strip()
            if name:
                teams_to_create[key]['members'].append({'name': normalize_participant_name(name), 'email': email})

        teams_created = 0
        members_added = 0
        members_skipped = 0

        for key, team_data in teams_to_create.items():
            # Create team if not exists
            if key in existing_teams:
                team_id = existing_teams[key]
            else:
                team_id = str(uuid_lib.uuid4())
                errors = client.insert_rows_json(f"{dataset_ref}.{BQ_TEAMS_TABLE}", [{
                    'team_id': team_id, 'team_name': team_data['team_name'],
                    'manager_name': team_data['manager_name'],
                    'manager_email': team_data['manager_email'],
                    'created_at': datetime.utcnow().isoformat(),
                    'updated_at': datetime.utcnow().isoformat()
                }])
                if not errors:
                    existing_teams[key] = team_id
                    teams_created += 1

            # Get existing members of this team
            member_config = bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]
            )
            existing_members = {normalize_participant_name(m.participant_name).lower().strip()
                                for m in client.query(
                f"SELECT participant_name FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` WHERE team_id = @team_id",
                job_config=member_config
            ).result()}

            # Add new members
            rows_to_insert = []
            for m in team_data['members']:
                mkey = m['name'].lower().strip()
                if mkey in existing_members:
                    members_skipped += 1
                    continue
                existing_members.add(mkey)
                rows_to_insert.append({
                    'member_id': str(uuid_lib.uuid4()),
                    'team_id': team_id,
                    'participant_name': m['name'],
                    'participant_email': m['email'],
                    'added_at': datetime.utcnow().isoformat()
                })
                members_added += 1

            if rows_to_insert:
                client.insert_rows_json(f"{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}", rows_to_insert)

        print(f"[Teams] Bulk import: {teams_created} teams created, {members_added} members added, {members_skipped} skipped")
        return jsonify({
            'success': True,
            'teams_created': teams_created,
            'members_added': members_added,
            'members_skipped': members_skipped
        })
    except Exception as e:
        print(f"[Teams] Bulk import error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/participants', methods=['GET'])
def list_known_participants():
    """Get distinct participants from recent snapshots (for adding to teams)
    Query params:
        days: lookback period in days (default 90, max 365)
    """
    try:
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # Allow custom lookback period (default 90 days, max 365)
        days = min(int(request.args.get('days', 90)), 365)

        # event_date is a DATE column — compare directly. The previous
        # SAFE.PARSE_DATE('%Y-%m-%d', event_date) treated it as a STRING,
        # which BigQuery rejects at compile time (no DATE->STRING coercion),
        # so this endpoint 500'd on every call.
        query = f"""
        SELECT DISTINCT participant_name, participant_email
        FROM `{dataset_ref}.room_snapshots_v2`
        WHERE event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)
          AND LOWER(participant_name) NOT LIKE '%scout%'
          AND participant_name IS NOT NULL AND participant_name != ''
        ORDER BY participant_name
        """
        rows = list(client.query(query).result())
        # Normalize names and deduplicate
        seen = {}
        for r in rows:
            base = normalize_participant_name(r.participant_name)
            key = base.lower().strip()
            if key not in seen:
                seen[key] = {'participant_name': base, 'participant_email': r.participant_email or ''}
            elif r.participant_email and not seen[key]['participant_email']:
                seen[key]['participant_email'] = r.participant_email
        participants = sorted(seen.values(), key=lambda x: x['participant_name'])
        return jsonify({'success': True, 'participants': participants})
    except Exception as e:
        print(f"[Teams] Participants list error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# TEAM ATTENDANCE - Break time, Isolation, Team-wise view
# ==============================================================================

@app.route('/teams/compare', methods=['GET'])
def compare_teams():
    """Compare multiple teams side-by-side. Query params: ids (comma-sep), date"""
    try:
        ensure_team_tables_once()
        team_ids_str = request.args.get('ids', '')
        if not team_ids_str:
            return jsonify({'success': False, 'error': 'ids parameter required (comma-separated team IDs)'}), 400
        team_ids = [tid.strip() for tid in team_ids_str.split(',') if tid.strip()]
        report_date = validate_date_format(request.args.get('date'))

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        results = []
        for team_id in team_ids:
            # Team info
            team_config = bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]
            )
            team_rows = list(client.query(
                f"SELECT team_id, team_name, manager_name FROM `{dataset_ref}.{BQ_TEAMS_TABLE}` WHERE team_id = @team_id",
                job_config=team_config
            ).result())
            if not team_rows:
                continue
            t = team_rows[0]

            # Members
            all_members = list(client.query(
                f"SELECT participant_name FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` WHERE team_id = @team_id",
                job_config=team_config
            ).result())
            total_members = len(all_members)
            member_names_lower = {m.participant_name.lower().strip() for m in all_members}

            # Stats for the date
            stats_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
                    bigquery.ScalarQueryParameter("report_date", "DATE", report_date)
                ]
            )
            # Hours from presence_intervals (one source of hours). Roster
            # bridge matches on normalized name OR email, mirroring the v2
            # team endpoints.
            norm_tm = _sql_normalize_name('tm.participant_name')
            stats_query = f"""
            WITH team_members AS (
                SELECT participant_name, participant_email
                FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` WHERE team_id = @team_id
            ),
            per_person AS (
                SELECT
                    pi.participant_key,
                    TIMESTAMP_ADD(MIN(pi.start_ts), INTERVAL 330 MINUTE) AS first_seen,
                    TIMESTAMP_ADD(MAX(pi.end_ts), INTERVAL 330 MINUTE) AS last_seen,
                    CEILING(SUM(IF(pi.room_category != 'break', pi.duration_seconds, 0)) / 60.0) AS active_mins,
                    CEILING(SUM(IF(pi.room_category = 'break', pi.duration_seconds, 0)) / 60.0) AS break_mins
                FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}` pi
                INNER JOIN team_members tm
                    ON pi.participant_key = {norm_tm}
                    OR {_sql_normalize_name('pi.participant_name')} = {norm_tm}
                    OR (NULLIF(LOWER(TRIM(pi.participant_email)), '') IS NOT NULL
                        AND NULLIF(LOWER(TRIM(pi.participant_email)), '') = NULLIF(LOWER(TRIM(tm.participant_email)), ''))
                WHERE pi.event_date = @report_date
                GROUP BY pi.participant_key
            )
            SELECT
                COUNT(*) as present,
                ROUND(AVG(active_mins), 0) as avg_active,
                ROUND(AVG(break_mins), 0) as avg_break,
                FORMAT_TIMESTAMP('%H:%M', MIN(first_seen)) as earliest_arrival,
                FORMAT_TIMESTAMP('%H:%M', MAX(last_seen)) as latest_departure
            FROM per_person
            """
            stats_rows = list(client.query(stats_query, job_config=stats_config).result())
            sr = stats_rows[0] if stats_rows else None

            present_count = int(sr.present) if sr and sr.present else 0
            results.append({
                'team_id': t.team_id,
                'team_name': t.team_name,
                'manager_name': t.manager_name or '',
                'total_members': total_members,
                'present': present_count,
                'absent': total_members - present_count,
                'attendance_pct': round(present_count / total_members * 100) if total_members else 0,
                'avg_active_mins': int(sr.avg_active) if sr and sr.avg_active else 0,
                'avg_break_mins': int(sr.avg_break) if sr and sr.avg_break else 0,
                'earliest_arrival': sr.earliest_arrival if sr else None,
                'latest_departure': sr.latest_departure if sr else None
            })

        return jsonify({
            'success': True,
            'date': report_date,
            'teams': results
        })
    except Exception as e:
        print(f"[Teams] Compare error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>/report/monthly', methods=['GET'])
def team_monthly_report(team_id):
    """Generate monthly CSV report for a team. Query params: year, month

    USES ATTENDANCE_SUMMARY DATA DIRECTLY - guarantees matching values with Day View.
    Calls the same logic as /attendance/summary for each date, then filters by team.
    """
    try:
        ensure_team_tables_once()
        year = request.args.get('year', str(get_ist_now().year))
        month = request.args.get('month', str(get_ist_now().month))

        year = int(year)
        month = int(month)
        if month < 1 or month > 12:
            return jsonify({'success': False, 'error': 'Invalid month'}), 400

        # Date range for the month
        from calendar import monthrange
        from datetime import date as date_cls, timedelta
        _, last_day = monthrange(year, month)

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # Get team info
        team_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]
        )
        team_rows = list(client.query(
            f"SELECT team_name, manager_name FROM `{dataset_ref}.{BQ_TEAMS_TABLE}` WHERE team_id = @team_id",
            job_config=team_config
        ).result())
        if not team_rows:
            return jsonify({'success': False, 'error': 'Team not found'}), 404
        team_name = team_rows[0].team_name

        # Get team members for filtering
        members_rows = list(client.query(
            f"SELECT participant_name, participant_email FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` WHERE team_id = @team_id",
            job_config=team_config
        ).result())

        # Build lookup sets for team member matching (case-insensitive)
        # Bug fix: Only add email if non-empty to avoid matching all empty-email participants
        team_member_names = set()
        team_member_emails = set()
        team_member_info = {}  # name_key -> (display_name, email)
        for m in members_rows:
            name_key = normalize_participant_name(m.participant_name).lower().strip()
            team_member_names.add(name_key)
            email = (m.participant_email or '').lower().strip()
            if email:  # Only add non-empty emails
                team_member_emails.add(email)
            team_member_info[name_key] = (m.participant_name, m.participant_email or '')

        # Helper to check if a participant is a team member
        def is_team_member(name, email):
            name_key = normalize_participant_name(name).lower().strip()
            email_key = (email or '').lower().strip()
            return name_key in team_member_names or (email_key and email_key in team_member_emails)

        # Attendance data for the whole month from presence_intervals — the ONE
        # source of hours (same table Day View / Team View v2 read), so these
        # exports can never disagree with them. Webhook fill / synthesis is
        # already materialized as interval rows, so main_room_fill_mins is 0.
        start_date = f"{year}-{month:02d}-01"
        end_date = f"{year}-{month:02d}-{last_day:02d}"

        # Ensure the month's intervals exist (no-op when the scheduler owns
        # freshness / PAGELOAD_AUTO_BUILD=false).
        try:
            _auto_build_dates_in_range(start_date, end_date)
        except Exception as be:
            print(f"[TeamMonthly] auto-build warning: {be}")

        query = f"""
        SELECT
          pi.event_date,
          ANY_VALUE(pi.participant_name) AS participant_name,
          COALESCE(MAX(pi.participant_email), '') AS participant_email,
          FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MIN(pi.start_ts), INTERVAL 330 MINUTE)) AS first_seen_ist,
          FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MAX(pi.end_ts), INTERVAL 330 MINUTE)) AS last_seen_ist,
          CAST(CEILING(SUM(IF(pi.room_category != 'break', pi.duration_seconds, 0)) / 60.0) AS INT64) AS total_mins,
          CAST(CEILING(SUM(IF(pi.room_category = 'breakout', pi.duration_seconds, 0)) / 60.0) AS INT64) AS breakout_mins,
          CAST(CEILING(SUM(IF(pi.room_category = 'break', pi.duration_seconds, 0)) / 60.0) AS INT64) AS break_mins,
          CAST(CEILING(SUM(pi.alone_seconds) / 60.0) AS INT64) AS isolation_mins,
          0 AS main_room_fill_mins
        FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}` pi
        WHERE pi.event_date BETWEEN @start_date AND @end_date
        GROUP BY pi.event_date, pi.participant_key
        ORDER BY participant_name, pi.event_date
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
                bigquery.ScalarQueryParameter("end_date", "DATE", end_date)
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())

        # Aggregate by name+date (handles multiple UUIDs for same person)
        aggregated = {}  # (name_key, date) -> aggregated stats
        for r in rows:
            # Skip if not a team member
            if not is_team_member(r.participant_name, r.participant_email):
                continue

            name_key = normalize_participant_name(r.participant_name).lower().strip()
            date_str = str(r.event_date)
            key = (name_key, date_str)

            snap_total_mins = int(r.total_mins or 0)
            breakout_mins = int(r.breakout_mins or 0)
            break_mins = int(r.break_mins or 0)
            isolation_mins = int(r.isolation_mins or 0)
            # Webhook-derived Main Room time NOT captured by SDK snapshots.
            # Capped at 600 mins to match day view's per-slice cap — keeps
            # historical days (no Main Room snapshots in BQ) consistent with
            # the day view total. Going forward, SDK now records Main Room
            # participants so this fill will be ~0.
            main_room_fill = int(r.main_room_fill_mins or 0) if hasattr(r, 'main_room_fill_mins') else 0
            if snap_total_mins > 0:
                main_room_fill = min(main_room_fill, 600)
            total_mins = snap_total_mins + main_room_fill

            if key not in aggregated:
                display_name, member_email = team_member_info.get(name_key, (r.participant_name, r.participant_email or ''))
                aggregated[key] = {
                    'date': date_str,
                    'name': display_name,
                    'email': member_email or r.participant_email or '',
                    'first_seen_ist': r.first_seen_ist,
                    'last_seen_ist': r.last_seen_ist,
                    'active_minutes': 0,
                    'breakout_minutes': 0,
                    'main_room_minutes': 0,
                    'break_minutes': 0,
                    'isolation_minutes': 0
                }

            # Sum up the values (total_mins = snapshot interval-sum + webhook-derived Main Room fill)
            aggregated[key]['active_minutes'] += total_mins
            aggregated[key]['breakout_minutes'] += breakout_mins
            # Main room = total - breakout (snapshot main room + webhook fill)
            aggregated[key]['main_room_minutes'] += max(0, total_mins - breakout_mins)
            aggregated[key]['break_minutes'] += break_mins
            aggregated[key]['isolation_minutes'] += isolation_mins
            # Update first/last seen
            if r.first_seen_ist and (not aggregated[key]['first_seen_ist'] or r.first_seen_ist < aggregated[key]['first_seen_ist']):
                aggregated[key]['first_seen_ist'] = r.first_seen_ist
            if r.last_seen_ist and (not aggregated[key]['last_seen_ist'] or r.last_seen_ist > aggregated[key]['last_seen_ist']):
                aggregated[key]['last_seen_ist'] = r.last_seen_ist

        # Build final data list
        data = []
        for agg in aggregated.values():
            # active_minutes already has the correct total from interval-sum
            # Don't subtract break_minutes here - it's calculated separately for display
            total_mins = agg['active_minutes']
            total_mins = max(0, min(total_mins, 720))  # Cap at 12 hours
            agg['active_minutes'] = total_mins

            # Bug fix: Use lowercase status to match team_attendance endpoint
            status = 'present' if total_mins >= 300 else 'half_day' if total_mins >= 240 else 'absent'
            agg['status'] = status
            data.append(agg)

        # Sort by name, then date
        data.sort(key=lambda x: (x['name'].lower(), x['date']))

        # Check if download=csv requested
        if request.args.get('format') == 'csv':
            import csv
            import io
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(['Date', 'Name', 'Email', 'Status', 'First_Seen_IST', 'Last_Seen_IST',
                             'Total_Minutes', 'Breakout_Minutes', 'Main_Room_Minutes', 'Break_Minutes', 'Isolation_Minutes'])
            for r in data:
                writer.writerow([r['date'], r['name'], r['email'],
                                 r['status'], r['first_seen_ist'], r['last_seen_ist'], r['active_minutes'],
                                 r['breakout_minutes'], r['main_room_minutes'], r['break_minutes'], r['isolation_minutes']])

            csv_content = output.getvalue()
            filename = f"team_{team_name.replace(' ', '_')}_{year}_{month:02d}.csv"
            return Response(
                csv_content,
                mimetype='text/csv',
                headers={'Content-Disposition': f'attachment; filename={filename}'}
            )

        # Summary per member across the month
        member_summary = {}
        for r in data:
            clean_name = normalize_participant_name(r['name'])
            key = clean_name.lower().strip()
            if not key:
                continue
            if key not in member_summary:
                member_summary[key] = {
                    'name': clean_name, 'email': r['email'],
                    'days_present': 0, 'total_active_mins': 0,
                    'total_break_mins': 0, 'total_isolation_mins': 0
                }
            if r['status'] in ('present', 'half_day'):
                member_summary[key]['days_present'] += 1
            member_summary[key]['total_active_mins'] += r['active_minutes']
            member_summary[key]['total_break_mins'] += r['break_minutes']
            member_summary[key]['total_isolation_mins'] += r['isolation_minutes']

        all_member_names = {m.participant_name: (m.participant_email or '') for m in members_rows}

        # Count working days in the month (exclude weekends)
        from datetime import date as date_cls
        working_days = 0
        for d in range(1, last_day + 1):
            dt = date_cls(year, month, d)
            if dt.weekday() < 5:  # Mon-Fri
                working_days += 1

        # Employee Report Card CSV
        if request.args.get('format') == 'employee_csv':
            import csv
            import io
            output = io.StringIO()
            writer = csv.writer(output)

            # Group daily data by employee (normalized name to collapse renamers)
            emp_data = {}
            for r in data:
                key = normalize_participant_name(r['name']).lower().strip()
                if key:
                    emp_data.setdefault(key, []).append(r)

            for name in sorted(all_member_names.keys()):
                lookup_key = normalize_participant_name(name).lower().strip()
                emp_rows = sorted(emp_data.get(lookup_key, []), key=lambda x: x['date'])
                summary = member_summary.get(lookup_key, {})
                email = all_member_names.get(name, '')
                days_present = summary.get('days_present', 0)
                total_active = summary.get('total_active_mins', 0)
                total_break = summary.get('total_break_mins', 0)
                total_iso = summary.get('total_isolation_mins', 0)
                days_absent = working_days - days_present
                att_pct = round(days_present / working_days * 100) if working_days else 0
                avg_hours = round(total_active / days_present) if days_present else 0
                avg_break = round(total_break / days_present) if days_present else 0

                def fmt(m):
                    if not m: return '0m'
                    return f'{m // 60}h {m % 60}m' if m >= 60 else f'{m}m'

                # Report Card Header
                writer.writerow([])
                writer.writerow(['=' * 60])
                writer.writerow([f'EMPLOYEE REPORT CARD - {["","Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][month]} {year}'])
                writer.writerow(['=' * 60])
                writer.writerow([f'Name: {name}'])
                writer.writerow([f'Email: {email}'])
                writer.writerow([f'Team: {team_name}'])
                writer.writerow([])

                # Summary section
                writer.writerow(['SUMMARY'])
                writer.writerow(['-' * 40])
                writer.writerow(['Working Days', 'Present', 'Half Day', 'Absent', 'Attendance %'])
                present_days = len([r for r in emp_rows if r.get('status') == 'present'])
                half_days = len([r for r in emp_rows if r.get('status') == 'half_day'])
                absent_days = working_days - present_days - half_days
                writer.writerow([working_days, present_days, half_days, absent_days, f'{att_pct}%'])
                writer.writerow([])
                writer.writerow(['Avg Daily Hours', 'Total Break', 'Avg Break/Day', 'Total Isolation'])
                writer.writerow([fmt(avg_hours), fmt(total_break), fmt(avg_break), fmt(total_iso)])
                writer.writerow([])

                # Day-wise breakdown
                writer.writerow(['DAY-WISE BREAKDOWN'])
                writer.writerow(['-' * 40])
                writer.writerow(['Date', 'Day', 'Status', 'In', 'Out', 'Total', 'Breakout', 'Main Room', 'Break', 'Isolation'])

                # Build set of dates that have data
                data_dates = {r['date'] for r in emp_rows}

                for d in range(1, last_day + 1):
                    dt = date_cls(year, month, d)
                    date_str = f'{year}-{month:02d}-{d:02d}'
                    day_name = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][dt.weekday()]
                    is_weekend = dt.weekday() >= 5

                    if date_str in data_dates:
                        r = next(x for x in emp_rows if x['date'] == date_str)
                        writer.writerow([
                            date_str, day_name, r.get('status', ''),
                            r.get('first_seen_ist', '-'), r.get('last_seen_ist', '-'),
                            fmt(r.get('active_minutes', 0)),
                            fmt(r.get('breakout_minutes', 0)),
                            fmt(r.get('main_room_minutes', 0)),
                            fmt(r.get('break_minutes', 0)),
                            fmt(r.get('isolation_minutes', 0))
                        ])
                    elif is_weekend:
                        writer.writerow([date_str, day_name, 'Weekend', '-', '-', '-', '-', '-', '-', '-'])
                    else:
                        writer.writerow([date_str, day_name, 'Absent', '-', '-', '0m', '0m', '0m', '0m', '0m'])

                writer.writerow([])

            csv_content = output.getvalue()
            filename = f"team_{team_name.replace(' ', '_')}_employee_reports_{year}_{month:02d}.csv"
            return Response(csv_content, mimetype='text/csv',
                            headers={'Content-Disposition': f'attachment; filename={filename}'})

        # Team Summary CSV: all members side-by-side
        if request.args.get('format') == 'team_summary_csv':
            import csv
            import io
            output = io.StringIO()
            writer = csv.writer(output)

            writer.writerow([f'TEAM SUMMARY - {["","Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][month]} {year}'])
            writer.writerow([f'Team: {team_name}'])
            writer.writerow([f'Working Days: {working_days}'])
            writer.writerow([])

            writer.writerow(['Name', 'Email', 'Present', 'Half Day', 'Absent', 'Attendance %',
                             'Total Hours', 'Avg Hours/Day', 'Total Break', 'Avg Break/Day', 'Total Isolation'])

            # Group data by employee (normalized name to collapse renamers)
            emp_data = {}
            for r in data:
                key = normalize_participant_name(r['name']).lower().strip()
                if key:
                    emp_data.setdefault(key, []).append(r)

            for name in sorted(all_member_names.keys()):
                lookup_key = normalize_participant_name(name).lower().strip()
                emp_rows = emp_data.get(lookup_key, [])
                email = all_member_names.get(name, '')
                present_days = len([r for r in emp_rows if r.get('status') == 'present'])
                half_days = len([r for r in emp_rows if r.get('status') == 'half_day'])
                absent_days = working_days - present_days - half_days
                att_pct = round((present_days + half_days) / working_days * 100) if working_days else 0
                total_active = sum(r.get('active_minutes', 0) for r in emp_rows)
                total_break = sum(r.get('break_minutes', 0) for r in emp_rows)
                total_iso = sum(r.get('isolation_minutes', 0) for r in emp_rows)
                working_count = present_days + half_days
                avg_active = round(total_active / working_count) if working_count else 0
                avg_break = round(total_break / working_count) if working_count else 0

                def fmt(m):
                    if not m: return '0m'
                    return f'{m // 60}h {m % 60}m' if m >= 60 else f'{m}m'

                writer.writerow([name, email, present_days, half_days, absent_days, f'{att_pct}%',
                                 fmt(total_active), fmt(avg_active), fmt(total_break), fmt(avg_break), fmt(total_iso)])

            csv_content = output.getvalue()
            filename = f"team_{team_name.replace(' ', '_')}_summary_{year}_{month:02d}.csv"
            return Response(csv_content, mimetype='text/csv',
                            headers={'Content-Disposition': f'attachment; filename={filename}'})

        # Load holidays for this team + month so frontend can mark them
        holidays_list = []
        try:
            hol_rows = list(client.query(
                f"""
                SELECT holiday_id,
                       FORMAT_DATE('%Y-%m-%d', holiday_date) AS holiday_date,
                       description
                FROM `{dataset_ref}.{BQ_TEAM_HOLIDAYS_TABLE}`
                WHERE team_id = @team_id
                  AND EXTRACT(YEAR FROM holiday_date) = @year
                  AND EXTRACT(MONTH FROM holiday_date) = @month
                ORDER BY holiday_date
                """,
                job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
                    bigquery.ScalarQueryParameter("year", "INT64", year),
                    bigquery.ScalarQueryParameter("month", "INT64", month),
                ])
            ).result())
            holidays_list = [{
                'holiday_id': h.holiday_id,
                'date': h.holiday_date,
                'description': h.description or '',
            } for h in hol_rows]
        except Exception as he:
            # If the table doesn't exist yet for this project, just return empty
            print(f"[Teams] Holiday lookup warning: {he}")

        return jsonify({
            'success': True,
            'team_id': team_id,
            'team_name': team_name,
            'year': year,
            'month': month,
            'start_date': start_date,
            'end_date': end_date,
            'daily_data': data,
            'member_summary': list(member_summary.values()),
            'holidays': holidays_list,
        })
    except Exception as e:
        print(f"[Teams] Monthly report error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# TEAM HOLIDAYS - per-team holiday calendar
# ==============================================================================

@app.route('/teams/<team_id>/holidays', methods=['GET'])
def list_team_holidays(team_id):
    """List holidays for a team. Optional ?year=&month= to filter."""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        year = request.args.get('year')
        month = request.args.get('month')

        where = ["team_id = @team_id"]
        params = [bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]
        if year:
            where.append("EXTRACT(YEAR FROM holiday_date) = @year")
            params.append(bigquery.ScalarQueryParameter("year", "INT64", int(year)))
        if month:
            where.append("EXTRACT(MONTH FROM holiday_date) = @month")
            params.append(bigquery.ScalarQueryParameter("month", "INT64", int(month)))

        query = f"""
        SELECT holiday_id, team_id,
               FORMAT_DATE('%Y-%m-%d', holiday_date) AS holiday_date,
               description
        FROM `{dataset_ref}.{BQ_TEAM_HOLIDAYS_TABLE}`
        WHERE {' AND '.join(where)}
        ORDER BY holiday_date
        """
        rows = list(client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())
        holidays = [{
            'holiday_id': r.holiday_id,
            'team_id': r.team_id,
            'date': r.holiday_date,
            'description': r.description or '',
        } for r in rows]
        return jsonify({'success': True, 'holidays': holidays})
    except Exception as e:
        print(f"[Holidays] List error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>/holidays', methods=['POST'])
@require_auth()
def add_team_holiday(team_id):
    """Add a holiday for a team. Body: {date: 'YYYY-MM-DD', description?}."""
    try:
        ensure_team_tables_once()
        data = request.get_json(force=True) or {}
        date = data.get('date')
        description = data.get('description', '')
        if not date:
            return jsonify({'success': False, 'error': 'date required'}), 400
        # Validate date format
        try:
            datetime.strptime(date, '%Y-%m-%d')
        except Exception:
            return jsonify({'success': False, 'error': 'Invalid date format (expected YYYY-MM-DD)'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        table_ref = f"{dataset_ref}.{BQ_TEAM_HOLIDAYS_TABLE}"

        holiday_id = str(uuid_lib.uuid4())
        row = {
            'holiday_id': holiday_id,
            'team_id': team_id,
            'holiday_date': date,
            'description': description,
            'created_at': datetime.utcnow().isoformat(),
        }
        errors = client.insert_rows_json(table_ref, [row])
        if errors:
            return jsonify({'success': False, 'error': f'Insert failed: {errors}'}), 500

        return jsonify({'success': True, 'holiday_id': holiday_id, 'date': date})
    except Exception as e:
        print(f"[Holidays] Add error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>/holidays/<holiday_id>', methods=['DELETE'])
@require_auth()
def delete_team_holiday(team_id, holiday_id):
    """Remove a holiday."""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        query = f"""
        DELETE FROM `{dataset_ref}.{BQ_TEAM_HOLIDAYS_TABLE}`
        WHERE team_id = @team_id AND holiday_id = @holiday_id
        """
        client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
            bigquery.ScalarQueryParameter("holiday_id", "STRING", holiday_id),
        ])).result()
        return jsonify({'success': True})
    except Exception as e:
        print(f"[Holidays] Delete error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>/holidays/<holiday_id>', methods=['PUT'])
@require_auth()
def update_team_holiday(team_id, holiday_id):
    """Update a holiday. Body: {date?: 'YYYY-MM-DD', description?}."""
    try:
        ensure_team_tables_once()
        data = request.get_json(force=True) or {}
        new_date = data.get('date')
        new_desc = data.get('description')

        if not new_date and new_desc is None:
            return jsonify({'success': False, 'error': 'Provide date or description to update'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        sets = []
        params = [
            bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
            bigquery.ScalarQueryParameter("holiday_id", "STRING", holiday_id),
        ]
        if new_date:
            try:
                datetime.strptime(new_date, '%Y-%m-%d')
            except:
                return jsonify({'success': False, 'error': 'Invalid date format'}), 400
            sets.append("holiday_date = @new_date")
            params.append(bigquery.ScalarQueryParameter("new_date", "DATE", new_date))
        if new_desc is not None:
            sets.append("description = @new_desc")
            params.append(bigquery.ScalarQueryParameter("new_desc", "STRING", new_desc))

        query = f"""
        UPDATE `{dataset_ref}.{BQ_TEAM_HOLIDAYS_TABLE}`
        SET {', '.join(sets)}
        WHERE team_id = @team_id AND holiday_id = @holiday_id
        """
        client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
        return jsonify({'success': True})
    except Exception as e:
        print(f"[Holidays] Update error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/holidays-summary', methods=['GET'])
def get_teams_holidays_summary():
    """Get holiday counts for all teams. Optional: ?year=&month="""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        year = request.args.get('year', datetime.now().year)
        month = request.args.get('month', datetime.now().month)

        query = f"""
        SELECT t.team_id, t.team_name, COUNT(h.holiday_id) as holiday_count
        FROM `{dataset_ref}.{BQ_TEAMS_TABLE}` t
        LEFT JOIN `{dataset_ref}.{BQ_TEAM_HOLIDAYS_TABLE}` h
          ON t.team_id = h.team_id
          AND EXTRACT(YEAR FROM h.holiday_date) = @year
          AND EXTRACT(MONTH FROM h.holiday_date) = @month
        GROUP BY t.team_id, t.team_name
        ORDER BY t.team_name
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("year", "INT64", int(year)),
            bigquery.ScalarQueryParameter("month", "INT64", int(month)),
        ])
        rows = list(client.query(query, job_config=job_config).result())

        summary = {}
        for row in rows:
            summary[row.team_id] = {
                'team_name': row.team_name,
                'holiday_count': row.holiday_count
            }

        return jsonify({'success': True, 'summary': summary, 'year': int(year), 'month': int(month)})
    except Exception as e:
        print(f"[HolidaysSummary] Error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# EMPLOYEE LEAVE - Individual employee leave/holidays
# ==============================================================================

@app.route('/employees/leave', methods=['GET'])
def list_all_employee_leave():
    """List all employee leave records. Optional filters: ?year=&month=&employee_id="""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        year = request.args.get('year')
        month = request.args.get('month')
        employee_id = request.args.get('employee_id')

        conditions = []
        params = []
        if year:
            conditions.append("EXTRACT(YEAR FROM leave_date) = @year")
            params.append(bigquery.ScalarQueryParameter("year", "INT64", int(year)))
        if month:
            conditions.append("EXTRACT(MONTH FROM leave_date) = @month")
            params.append(bigquery.ScalarQueryParameter("month", "INT64", int(month)))
        if employee_id:
            conditions.append("employee_id = @employee_id")
            params.append(bigquery.ScalarQueryParameter("employee_id", "STRING", employee_id))

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"""
        SELECT leave_id, employee_id, employee_name, leave_date, leave_type, description, created_at
        FROM `{dataset_ref}.{BQ_EMPLOYEE_LEAVE_TABLE}`
        {where_clause}
        ORDER BY leave_date DESC
        """
        rows = list(client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())

        leave_list = [{
            'leave_id': r.leave_id,
            'employee_id': r.employee_id,
            'employee_name': r.employee_name,
            'date': str(r.leave_date),
            'leave_type': r.leave_type or 'leave',
            'description': r.description or '',
        } for r in rows]

        return jsonify({'success': True, 'leave': leave_list})
    except Exception as e:
        print(f"[Leave] List error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/<employee_id>/leave', methods=['GET'])
def list_employee_leave(employee_id):
    """List leave for a specific employee. Optional: ?year=&month="""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        year = request.args.get('year')
        month = request.args.get('month')

        conditions = ["employee_id = @employee_id"]
        params = [bigquery.ScalarQueryParameter("employee_id", "STRING", employee_id)]

        if year:
            conditions.append("EXTRACT(YEAR FROM leave_date) = @year")
            params.append(bigquery.ScalarQueryParameter("year", "INT64", int(year)))
        if month:
            conditions.append("EXTRACT(MONTH FROM leave_date) = @month")
            params.append(bigquery.ScalarQueryParameter("month", "INT64", int(month)))

        query = f"""
        SELECT leave_id, employee_id, employee_name, leave_date, leave_type, description, created_at
        FROM `{dataset_ref}.{BQ_EMPLOYEE_LEAVE_TABLE}`
        WHERE {' AND '.join(conditions)}
        ORDER BY leave_date DESC
        """
        rows = list(client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())

        leave_list = [{
            'leave_id': r.leave_id,
            'employee_id': r.employee_id,
            'employee_name': r.employee_name,
            'date': str(r.leave_date),
            'leave_type': r.leave_type or 'leave',
            'description': r.description or '',
        } for r in rows]

        return jsonify({'success': True, 'leave': leave_list})
    except Exception as e:
        print(f"[Leave] Employee list error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/<employee_id>/leave', methods=['POST'])
@require_auth()
def add_employee_leave(employee_id):
    """Add leave for an employee. Body: {date: 'YYYY-MM-DD', leave_type?, description?}"""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        data = request.json or {}
        date = data.get('date')
        if not date or not validate_date_format(date):
            return jsonify({'success': False, 'error': 'Invalid date format. Use YYYY-MM-DD'}), 400

        leave_type = data.get('leave_type', 'leave')
        description = data.get('description', '')

        # Get employee name
        emp_query = f"SELECT participant_name FROM `{dataset_ref}.employee_registry` WHERE employee_id = @employee_id"
        emp_rows = list(client.query(emp_query, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("employee_id", "STRING", employee_id)
        ])).result())

        if not emp_rows:
            return jsonify({'success': False, 'error': 'Employee not found'}), 404
        employee_name = emp_rows[0].participant_name

        # Check for duplicate
        check_query = f"""
        SELECT COUNT(*) as cnt FROM `{dataset_ref}.{BQ_EMPLOYEE_LEAVE_TABLE}`
        WHERE employee_id = @employee_id AND leave_date = @date
        """
        check_rows = list(client.query(check_query, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("employee_id", "STRING", employee_id),
            bigquery.ScalarQueryParameter("date", "DATE", date),
        ])).result())
        if check_rows and check_rows[0].cnt > 0:
            return jsonify({'success': False, 'error': 'Leave already exists for this date'}), 400

        table_ref = f"{dataset_ref}.{BQ_EMPLOYEE_LEAVE_TABLE}"
        leave_id = str(uuid_lib.uuid4())
        row = {
            'leave_id': leave_id,
            'employee_id': employee_id,
            'employee_name': employee_name,
            'leave_date': date,
            'leave_type': leave_type,
            'description': description,
            'created_at': datetime.utcnow().isoformat(),
        }
        errors = client.insert_rows_json(table_ref, [row])
        if errors:
            return jsonify({'success': False, 'error': f'Insert failed: {errors}'}), 500

        return jsonify({'success': True, 'leave_id': leave_id, 'date': date})
    except Exception as e:
        print(f"[Leave] Add error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/<employee_id>/leave/<leave_id>', methods=['DELETE'])
@require_auth()
def delete_employee_leave(employee_id, leave_id):
    """Remove leave record."""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        query = f"""
        DELETE FROM `{dataset_ref}.{BQ_EMPLOYEE_LEAVE_TABLE}`
        WHERE employee_id = @employee_id AND leave_id = @leave_id
        """
        client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("employee_id", "STRING", employee_id),
            bigquery.ScalarQueryParameter("leave_id", "STRING", leave_id),
        ])).result()
        return jsonify({'success': True})
    except Exception as e:
        print(f"[Leave] Delete error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/<employee_id>/leave/<leave_id>', methods=['PUT'])
@require_auth()
def update_employee_leave(employee_id, leave_id):
    """Update leave record. Body: {date?, leave_type?, description?}."""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        data = request.json or {}
        new_date = data.get('date')
        new_type = data.get('leave_type')
        new_desc = data.get('description')

        if not new_date and not new_type and new_desc is None:
            return jsonify({'success': False, 'error': 'Provide date, leave_type, or description to update'}), 400

        sets = []
        params = [
            bigquery.ScalarQueryParameter("employee_id", "STRING", employee_id),
            bigquery.ScalarQueryParameter("leave_id", "STRING", leave_id),
        ]
        if new_date:
            if not validate_date_format(new_date):
                return jsonify({'success': False, 'error': 'Invalid date format'}), 400
            sets.append("leave_date = @new_date")
            params.append(bigquery.ScalarQueryParameter("new_date", "DATE", new_date))
        if new_type:
            sets.append("leave_type = @new_type")
            params.append(bigquery.ScalarQueryParameter("new_type", "STRING", new_type))
        if new_desc is not None:
            sets.append("description = @new_desc")
            params.append(bigquery.ScalarQueryParameter("new_desc", "STRING", new_desc))

        query = f"""
        UPDATE `{dataset_ref}.{BQ_EMPLOYEE_LEAVE_TABLE}`
        SET {', '.join(sets)}
        WHERE employee_id = @employee_id AND leave_id = @leave_id
        """
        client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
        return jsonify({'success': True})
    except Exception as e:
        print(f"[Leave] Update error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/leave/bulk', methods=['POST'])
@require_auth()
def add_bulk_leave():
    """Add leave for multiple employees. Body: {date: 'YYYY-MM-DD', employee_ids: [...], leave_type?, description?}"""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        data = request.json or {}
        date = data.get('date')
        employee_ids = data.get('employee_ids', [])

        if not date or not validate_date_format(date):
            return jsonify({'success': False, 'error': 'Invalid date format'}), 400
        if not employee_ids:
            return jsonify({'success': False, 'error': 'No employees specified'}), 400

        leave_type = data.get('leave_type', 'leave')
        description = data.get('description', '')

        # Get employee names
        emp_query = f"""
        SELECT employee_id, participant_name FROM `{dataset_ref}.employee_registry`
        WHERE employee_id IN UNNEST(@ids)
        """
        emp_rows = list(client.query(emp_query, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("ids", "STRING", employee_ids)
        ])).result())
        emp_map = {r.employee_id: r.participant_name for r in emp_rows}

        # Get existing leave for these employees on this date
        existing_query = f"""
        SELECT employee_id FROM `{dataset_ref}.{BQ_EMPLOYEE_LEAVE_TABLE}`
        WHERE employee_id IN UNNEST(@ids) AND leave_date = @date
        """
        existing_rows = list(client.query(existing_query, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("ids", "STRING", employee_ids),
            bigquery.ScalarQueryParameter("date", "DATE", date),
        ])).result())
        existing_ids = {r.employee_id for r in existing_rows}

        # Insert new records
        table_ref = f"{dataset_ref}.{BQ_EMPLOYEE_LEAVE_TABLE}"
        rows = []
        added = []
        skipped = []
        for emp_id in employee_ids:
            if emp_id in existing_ids:
                skipped.append(emp_id)
                continue
            if emp_id not in emp_map:
                skipped.append(emp_id)
                continue
            rows.append({
                'leave_id': str(uuid_lib.uuid4()),
                'employee_id': emp_id,
                'employee_name': emp_map[emp_id],
                'leave_date': date,
                'leave_type': leave_type,
                'description': description,
                'created_at': datetime.utcnow().isoformat(),
            })
            added.append(emp_id)

        if rows:
            errors = client.insert_rows_json(table_ref, rows)
            if errors:
                return jsonify({'success': False, 'error': f'Insert failed: {errors}'}), 500

        return jsonify({'success': True, 'added': len(added), 'skipped': len(skipped)})
    except Exception as e:
        print(f"[Leave] Bulk add error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# ATTENDANCE OVERRIDES - Manual corrections to attendance data
# ==============================================================================

@app.route('/attendance/override', methods=['POST'])
@require_auth()
def add_attendance_override():
    """Add or update attendance override for an employee on a date.
    Uses DELETE + INSERT pattern to avoid BigQuery streaming buffer issues.
    Body: {
        employee_name: string (required),
        employee_id?: string,
        event_date: 'YYYY-MM-DD' (required),
        first_seen_ist?: 'HH:MM',
        last_seen_ist?: 'HH:MM',
        status?: 'present'|'half_day'|'absent'|'leave',
        active_mins?: int,
        break_mins?: int,
        isolation_mins?: int,
        notes?: string,
        created_by?: string
    }
    """
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        data = request.json or {}
        employee_name = data.get('employee_name')
        event_date = data.get('event_date')

        if not employee_name:
            return jsonify({'success': False, 'error': 'employee_name required'}), 400
        if not event_date or not validate_date_format(event_date):
            return jsonify({'success': False, 'error': 'Invalid event_date format'}), 400

        # Try to delete any existing override (ignore errors from streaming buffer)
        try:
            delete_query = f"""
            DELETE FROM `{dataset_ref}.{BQ_ATTENDANCE_OVERRIDES_TABLE}`
            WHERE LOWER(TRIM(employee_name)) = LOWER(TRIM(@emp_name)) AND event_date = @date
            """
            client.query(delete_query, job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("emp_name", "STRING", employee_name),
                bigquery.ScalarQueryParameter("date", "DATE", event_date),
            ])).result()
        except Exception as del_err:
            # Ignore streaming buffer errors - we'll just insert a new record
            print(f"[Override] Delete skipped (streaming buffer): {del_err}")

        # Always insert a new record
        override_id = str(uuid_lib.uuid4())
        table_ref = f"{dataset_ref}.{BQ_ATTENDANCE_OVERRIDES_TABLE}"
        row = {
            'override_id': override_id,
            'employee_id': data.get('employee_id', ''),
            'employee_name': employee_name,
            'event_date': event_date,
            'first_seen_ist': data.get('first_seen_ist'),
            'last_seen_ist': data.get('last_seen_ist'),
            'status': data.get('status'),
            'active_mins': data.get('active_mins'),
            'break_mins': data.get('break_mins'),
            'isolation_mins': data.get('isolation_mins'),
            'notes': data.get('notes', ''),
            'created_by': data.get('created_by', ''),
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat(),
        }
        errors = client.insert_rows_json(table_ref, [row])
        if errors:
            return jsonify({'success': False, 'error': f'Insert failed: {errors}'}), 500

        return jsonify({'success': True, 'override_id': override_id})
    except Exception as e:
        print(f"[Override] Add/Update error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/attendance/override/<override_id>', methods=['DELETE'])
@require_auth()
def delete_attendance_override(override_id):
    """Remove an attendance override."""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        query = f"""
        DELETE FROM `{dataset_ref}.{BQ_ATTENDANCE_OVERRIDES_TABLE}`
        WHERE override_id = @override_id
        """
        client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("override_id", "STRING", override_id),
        ])).result()
        return jsonify({'success': True})
    except Exception as e:
        print(f"[Override] Delete error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/attendance/overrides', methods=['GET'])
def list_attendance_overrides():
    """List overrides. Optional: ?date=YYYY-MM-DD or ?employee_name="""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        date = request.args.get('date')
        emp_name = request.args.get('employee_name')

        conditions = []
        params = []
        if date:
            conditions.append("event_date = @date")
            params.append(bigquery.ScalarQueryParameter("date", "DATE", date))
        if emp_name:
            conditions.append("LOWER(TRIM(employee_name)) = LOWER(TRIM(@emp_name))")
            params.append(bigquery.ScalarQueryParameter("emp_name", "STRING", emp_name))

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"""
        SELECT * FROM `{dataset_ref}.{BQ_ATTENDANCE_OVERRIDES_TABLE}`
        {where_clause}
        ORDER BY event_date DESC, employee_name
        """
        rows = list(client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())

        overrides = [{
            'override_id': r.override_id,
            'employee_id': r.employee_id or '',
            'employee_name': r.employee_name,
            'event_date': str(r.event_date),
            'first_seen_ist': r.first_seen_ist,
            'last_seen_ist': r.last_seen_ist,
            'status': r.status,
            'active_mins': r.active_mins,
            'break_mins': r.break_mins,
            'isolation_mins': r.isolation_mins,
            'notes': r.notes or '',
            'created_by': r.created_by or '',
        } for r in rows]

        return jsonify({'success': True, 'overrides': overrides})
    except Exception as e:
        print(f"[Override] List error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# ATTENDANCE CONFLICT DETECTION - Find mismatches between leave/holiday and snapshots
# ==============================================================================

# Grace rules: configurable thresholds for attendance status
# Default: <3hr = Absent, 3-6hr = Half Day, >6hr = Present
DEFAULT_GRACE_RULES = {
    'min_hours_present': 360,    # 6 hours in minutes for "Present"
    'half_day_threshold': 180,   # 3 hours in minutes for "Half Day"
    'absent_threshold': 180,     # Below this = Absent
    'expected_arrival': '09:30',
    'expected_departure': '18:30',
    'late_grace_minutes': 15,
    'early_logout_grace_minutes': 15,
}


def calculate_status_with_grace(active_mins, rules=None):
    """Calculate attendance status based on grace rules.
    < 3hr (180 min) = Absent
    3-6hr (180-360 min) = Half Day
    > 6hr (360 min) = Present
    """
    if rules is None:
        rules = DEFAULT_GRACE_RULES
    min_present = rules.get('min_hours_present', 360)
    half_day = rules.get('half_day_threshold', 180)

    if active_mins >= min_present:
        return 'present'
    elif active_mins >= half_day:
        return 'half_day'
    else:
        return 'absent'


@app.route('/admin/conflicts', methods=['GET'])
def detect_attendance_conflicts():
    """Detect conflicts between leave/holiday records and actual snapshots.
    Returns cases where:
    1. Leave marked but snapshots exist (person was present)
    2. Holiday but snapshots exist (person worked on holiday)
    Query params: ?date=YYYY-MM-DD or ?year=&month=
    """
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        date = request.args.get('date')
        year = request.args.get('year')
        month = request.args.get('month')

        if date:
            start_date = date
            end_date = date
        elif year and month:
            from calendar import monthrange
            _, last_day = monthrange(int(year), int(month))
            start_date = f"{year}-{int(month):02d}-01"
            end_date = f"{year}-{int(month):02d}-{last_day:02d}"
        else:
            # Default: current month
            now = get_ist_now()
            from calendar import monthrange
            _, last_day = monthrange(now.year, now.month)
            start_date = f"{now.year}-{now.month:02d}-01"
            end_date = f"{now.year}-{now.month:02d}-{last_day:02d}"

        # Find leave records with actual snapshots (conflict: leave marked but present)
        leave_conflict_query = f"""
        WITH leave_records AS (
            SELECT employee_name, leave_date, leave_type
            FROM `{dataset_ref}.{BQ_EMPLOYEE_LEAVE_TABLE}`
            WHERE leave_date >= @start_date AND leave_date <= @end_date
        ),
        snapshot_presence AS (
            SELECT
                LOWER(TRIM(participant_name)) as name_key,
                event_date,
                -- Bucket to 30s windows so multi-source polls don't double-count.
                COUNT(DISTINCT DIV(UNIX_SECONDS(snapshot_time), 30)) as snapshot_count,
                CEILING(COUNT(DISTINCT DIV(UNIX_SECONDS(snapshot_time), 30)) * 0.5) as approx_mins
            FROM `{dataset_ref}.room_snapshots_v2`
            WHERE event_date >= @start_date AND event_date <= @end_date
              AND participant_name IS NOT NULL AND participant_name != ''
              AND LOWER(participant_name) NOT LIKE '%scout%'
            GROUP BY name_key, event_date
            HAVING COUNT(DISTINCT DIV(UNIX_SECONDS(snapshot_time), 30)) >= 6  -- At least 3 minutes of snapshots
        )
        SELECT
            lr.employee_name,
            FORMAT_DATE('%Y-%m-%d', lr.leave_date) as date,
            lr.leave_type,
            sp.snapshot_count,
            sp.approx_mins,
            'leave_but_present' as conflict_type
        FROM leave_records lr
        INNER JOIN snapshot_presence sp
            ON LOWER(TRIM(lr.employee_name)) = sp.name_key
            AND lr.leave_date = SAFE.PARSE_DATE('%Y-%m-%d', sp.event_date)
        ORDER BY lr.leave_date DESC, lr.employee_name
        """

        params = [
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
        ]
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        leave_conflicts = list(client.query(leave_conflict_query, job_config=job_config).result())

        # Find holiday conflicts (someone worked on a team holiday)
        holiday_conflict_query = f"""
        WITH holidays AS (
            SELECT team_id, holiday_date
            FROM `{dataset_ref}.{BQ_TEAM_HOLIDAYS_TABLE}`
            WHERE holiday_date >= @start_date AND holiday_date <= @end_date
        ),
        team_members AS (
            SELECT tm.team_id, tm.participant_name, t.team_name
            FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` tm
            JOIN `{dataset_ref}.{BQ_TEAMS_TABLE}` t ON tm.team_id = t.team_id
        ),
        snapshot_presence AS (
            SELECT
                LOWER(TRIM(participant_name)) as name_key,
                event_date,
                COUNT(*) as snapshot_count
            FROM `{dataset_ref}.room_snapshots_v2`
            WHERE event_date >= @start_date AND event_date <= @end_date
              AND participant_name IS NOT NULL AND participant_name != ''
              AND LOWER(participant_name) NOT LIKE '%scout%'
            GROUP BY name_key, event_date
            HAVING COUNT(*) >= 6
        )
        SELECT
            tm.participant_name as employee_name,
            FORMAT_DATE('%Y-%m-%d', h.holiday_date) as date,
            tm.team_name,
            sp.snapshot_count,
            'holiday_but_present' as conflict_type
        FROM holidays h
        INNER JOIN team_members tm ON h.team_id = tm.team_id
        INNER JOIN snapshot_presence sp
            ON LOWER(TRIM(tm.participant_name)) = sp.name_key
            AND h.holiday_date = SAFE.PARSE_DATE('%Y-%m-%d', sp.event_date)
        ORDER BY h.holiday_date DESC, tm.participant_name
        """
        holiday_conflicts = list(client.query(holiday_conflict_query, job_config=job_config).result())

        conflicts = []
        for r in leave_conflicts:
            conflicts.append({
                'employee_name': r.employee_name,
                'date': r.date,
                'conflict_type': 'leave_but_present',
                'leave_type': r.leave_type,
                'snapshot_count': r.snapshot_count,
                'approx_mins': r.approx_mins,
                'suggestion': f'Remove leave or add attendance override',
            })
        for r in holiday_conflicts:
            conflicts.append({
                'employee_name': r.employee_name,
                'date': r.date,
                'conflict_type': 'holiday_but_present',
                'team_name': r.team_name,
                'snapshot_count': r.snapshot_count,
                'suggestion': f'Person worked on team holiday - may need attendance credit',
            })

        return jsonify({
            'success': True,
            'start_date': start_date,
            'end_date': end_date,
            'conflicts': conflicts,
            'total': len(conflicts),
        })
    except Exception as e:
        print(f"[Conflicts] Detection error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/grace-rules', methods=['GET'])
def get_grace_rules():
    """Get current grace rules configuration."""
    return jsonify({
        'success': True,
        'rules': DEFAULT_GRACE_RULES,
        'status_thresholds': {
            'present': '>= 6 hours (360 mins)',
            'half_day': '3-6 hours (180-360 mins)',
            'absent': '< 3 hours (180 mins)',
        }
    })


@app.route('/admin/teams-leave-summary', methods=['GET'])
def teams_leave_summary():
    """Get team-wise leave summary for all teams (box view like TeamView).
    Shows per-team: total members, on leave today, present today, absent.
    Query params: ?date=YYYY-MM-DD (defaults to today)
    """
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        date = request.args.get('date', get_ist_date())
        if not validate_date_format(date):
            return jsonify({'success': False, 'error': 'Invalid date format'}), 400

        # Get all teams with member counts
        teams_query = f"""
        SELECT
            t.team_id,
            t.team_name,
            t.manager_name,
            COUNT(DISTINCT tm.participant_name) as total_members
        FROM `{dataset_ref}.{BQ_TEAMS_TABLE}` t
        LEFT JOIN `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` tm ON t.team_id = tm.team_id
        GROUP BY t.team_id, t.team_name, t.manager_name
        ORDER BY t.team_name
        """
        teams = list(client.query(teams_query).result())

        # Get leave records for this date
        leave_query = f"""
        SELECT
            tm.team_id,
            el.employee_name,
            el.leave_type
        FROM `{dataset_ref}.{BQ_EMPLOYEE_LEAVE_TABLE}` el
        JOIN `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` tm
            ON LOWER(TRIM(el.employee_name)) = LOWER(TRIM(tm.participant_name))
        WHERE el.leave_date = @date
        """
        leave_rows = list(client.query(leave_query, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("date", "DATE", date)]
        )).result())

        # Group leave by team
        leave_by_team = {}
        for r in leave_rows:
            if r.team_id not in leave_by_team:
                leave_by_team[r.team_id] = []
            leave_by_team[r.team_id].append({
                'name': r.employee_name,
                'leave_type': r.leave_type,
            })

        # Get snapshot presence for this date (who is actually present)
        presence_query = f"""
        SELECT
            tm.team_id,
            tm.participant_name,
            COUNT(DISTINCT DIV(UNIX_SECONDS(s.snapshot_time), 30)) as snapshot_count,
            CEILING(SUM(
                CASE
                    WHEN prev_time IS NULL THEN 0
                    WHEN TIMESTAMP_DIFF(s.snapshot_time, prev_time, SECOND) <= 300 THEN
                        TIMESTAMP_DIFF(s.snapshot_time, prev_time, SECOND) / 60.0
                    ELSE 0  -- Long gap = session boundary, not active time
                END
            )) as active_mins
        FROM (
            SELECT
                participant_name,
                snapshot_time,
                LAG(snapshot_time) OVER (PARTITION BY participant_name ORDER BY snapshot_time) as prev_time
            FROM `{dataset_ref}.room_snapshots_v2`
            WHERE event_date = @date
              AND participant_name IS NOT NULL
              AND LOWER(participant_name) NOT LIKE '%scout%'
        ) s
        JOIN `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` tm
            ON LOWER(TRIM(s.participant_name)) = LOWER(TRIM(tm.participant_name))
        GROUP BY tm.team_id, tm.participant_name
        """
        presence_rows = list(client.query(presence_query, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("date", "DATE", date)]
        )).result())

        # Group presence by team
        presence_by_team = {}
        for r in presence_rows:
            if r.team_id not in presence_by_team:
                presence_by_team[r.team_id] = []
            status = calculate_status_with_grace(r.active_mins or 0)
            presence_by_team[r.team_id].append({
                'name': r.participant_name,
                'active_mins': r.active_mins or 0,
                'status': status,
            })

        # Check for team holidays
        holiday_query = f"""
        SELECT team_id FROM `{dataset_ref}.{BQ_TEAM_HOLIDAYS_TABLE}`
        WHERE holiday_date = @date
        """
        holiday_rows = list(client.query(holiday_query, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("date", "DATE", date)]
        )).result())
        teams_on_holiday = {r.team_id for r in holiday_rows}

        # Build response
        result = []
        for t in teams:
            tid = t.team_id
            team_leave = leave_by_team.get(tid, [])
            team_presence = presence_by_team.get(tid, [])
            is_holiday = tid in teams_on_holiday

            present_count = len([p for p in team_presence if p['status'] == 'present'])
            half_day_count = len([p for p in team_presence if p['status'] == 'half_day'])
            on_leave_count = len(team_leave)

            result.append({
                'team_id': tid,
                'team_name': t.team_name,
                'manager_name': t.manager_name or '',
                'total_members': t.total_members or 0,
                'is_holiday': is_holiday,
                'present': present_count,
                'half_day': half_day_count,
                'on_leave': on_leave_count,
                'absent': max(0, (t.total_members or 0) - present_count - half_day_count - on_leave_count),
                'leave_details': team_leave,
                'presence_details': team_presence,
            })

        return jsonify({
            'success': True,
            'date': date,
            'teams': result,
            'total_teams': len(result),
        })
    except Exception as e:
        print(f"[Admin] Teams leave summary error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# HISTORICAL TRENDS - Month-over-month analysis
# ==============================================================================

BQ_LEAVE_TABLE = 'team_leave_records'

def ensure_leave_table():
    """Create leave records table if it doesn't exist"""
    client = get_bq_client()
    table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_LEAVE_TABLE}"
    schema = [
        bigquery.SchemaField("leave_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("team_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("member_name", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("leave_date", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("leave_type", "STRING", mode="REQUIRED"),  # 'planned', 'sick', 'emergency', 'unexcused'
        bigquery.SchemaField("reason", "STRING"),
        bigquery.SchemaField("approved_by", "STRING"),
        bigquery.SchemaField("created_at", "TIMESTAMP")
    ]
    table = bigquery.Table(table_id, schema=schema)
    try:
        client.get_table(table_id)
    except:
        client.create_table(table)
        print(f"[Leave] Created table {table_id}")


@app.route('/teams/<team_id>/leave', methods=['GET'])
def get_team_leave(team_id):
    """Get leave records for a team
    Query params:
        start_date: YYYY-MM-DD (default: start of month)
        end_date: YYYY-MM-DD (default: today)
    """
    try:
        ensure_leave_table()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        now = get_ist_now()
        start_date = request.args.get('start_date', f"{now.year}-{now.month:02d}-01")
        end_date = request.args.get('end_date', now.strftime('%Y-%m-%d'))

        query = f"""
        SELECT leave_id, member_name, leave_date, leave_type, reason, approved_by, created_at
        FROM `{dataset_ref}.{BQ_LEAVE_TABLE}`
        WHERE team_id = @team_id AND leave_date >= @start_date AND leave_date <= @end_date
        ORDER BY leave_date, member_name
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
                bigquery.ScalarQueryParameter("start_date", "STRING", start_date),
                bigquery.ScalarQueryParameter("end_date", "STRING", end_date)
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())
        records = [{
            'leave_id': r.leave_id,
            'member_name': r.member_name,
            'leave_date': r.leave_date,
            'leave_type': r.leave_type,
            'reason': r.reason or '',
            'approved_by': r.approved_by or '',
            'created_at': r.created_at.isoformat() if r.created_at else None
        } for r in rows]

        return jsonify({'success': True, 'records': records})
    except Exception as e:
        print(f"[Leave] Get error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>/leave', methods=['POST'])
@require_auth()
def add_team_leave(team_id):
    """Add a leave record for a team member
    Body: {member_name, leave_date, leave_type, reason?, approved_by?}
    """
    try:
        ensure_leave_table()
        data = request.json or {}
        member_name = (data.get('member_name') or '').strip()
        leave_date = (data.get('leave_date') or '').strip()
        leave_type = (data.get('leave_type') or 'planned').strip().lower()
        reason = (data.get('reason') or '').strip()
        approved_by = (data.get('approved_by') or '').strip()

        if not member_name or not leave_date:
            return jsonify({'success': False, 'error': 'member_name and leave_date required'}), 400

        valid_types = ['planned', 'sick', 'emergency', 'unexcused', 'holiday', 'wfh']
        if leave_type not in valid_types:
            return jsonify({'success': False, 'error': f'leave_type must be one of: {valid_types}'}), 400

        client = get_bq_client()
        table_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_LEAVE_TABLE}"

        # Check for existing leave on same date
        dup_query = f"""
        SELECT leave_id FROM `{table_ref}`
        WHERE team_id = @team_id AND LOWER(TRIM(member_name)) = LOWER(@name) AND leave_date = @date
        LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
                bigquery.ScalarQueryParameter("name", "STRING", member_name),
                bigquery.ScalarQueryParameter("date", "STRING", leave_date)
            ]
        )
        existing = list(client.query(dup_query, job_config=job_config).result())
        if existing:
            return jsonify({'success': False, 'error': 'Leave record already exists for this date'}), 409

        leave_id = str(uuid_lib.uuid4())
        rows = [{
            'leave_id': leave_id,
            'team_id': team_id,
            'member_name': member_name,
            'leave_date': leave_date,
            'leave_type': leave_type,
            'reason': reason,
            'approved_by': approved_by,
            'created_at': datetime.utcnow().isoformat()
        }]
        errors = client.insert_rows_json(table_ref, rows)
        if errors:
            return jsonify({'success': False, 'error': str(errors)}), 500

        print(f"[Leave] Added {leave_type} leave for {member_name} on {leave_date}")
        return jsonify({'success': True, 'leave_id': leave_id})
    except Exception as e:
        print(f"[Leave] Add error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>/leave/<leave_id>', methods=['DELETE'])
@require_auth()
def delete_team_leave(team_id, leave_id):
    """Delete a leave record"""
    try:
        ensure_leave_table()
        client = get_bq_client()
        table_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_LEAVE_TABLE}"

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
                bigquery.ScalarQueryParameter("leave_id", "STRING", leave_id)
            ]
        )
        client.query(f"DELETE FROM `{table_ref}` WHERE team_id = @team_id AND leave_id = @leave_id",
                     job_config=job_config).result()

        return jsonify({'success': True})
    except Exception as e:
        print(f"[Leave] Delete error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


BQ_TEAM_TAGS_TABLE = 'team_tags'

def ensure_team_tags_table():
    """Create team tags table if it doesn't exist"""
    client = get_bq_client()
    table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_TEAM_TAGS_TABLE}"
    schema = [
        bigquery.SchemaField("team_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("tag_key", "STRING", mode="REQUIRED"),  # e.g., 'department', 'project', 'location'
        bigquery.SchemaField("tag_value", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("updated_at", "TIMESTAMP")
    ]
    table = bigquery.Table(table_id, schema=schema)
    try:
        client.get_table(table_id)
    except:
        client.create_table(table)
        print(f"[Tags] Created table {table_id}")


@app.route('/teams/<team_id>/tags', methods=['GET'])
def get_team_tags(team_id):
    """Get all tags for a team"""
    try:
        ensure_team_tags_table()
        client = get_bq_client()
        table_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_TEAM_TAGS_TABLE}"

        query = f"SELECT tag_key, tag_value FROM `{table_ref}` WHERE team_id = @team_id"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]
        )
        rows = list(client.query(query, job_config=job_config).result())
        tags = {r.tag_key: r.tag_value for r in rows}

        return jsonify({'success': True, 'team_id': team_id, 'tags': tags})
    except Exception as e:
        print(f"[Tags] Get error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>/tags', methods=['POST', 'PUT'])
@require_auth()
def set_team_tags(team_id):
    """Set tags for a team (upsert behavior)
    Body: {tags: {department: 'Engineering', project: 'Alpha', location: 'Remote'}}
    """
    try:
        ensure_team_tags_table()
        data = request.json or {}
        tags = data.get('tags', {})

        if not tags:
            return jsonify({'success': False, 'error': 'tags object required'}), 400

        client = get_bq_client()
        table_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_TEAM_TAGS_TABLE}"

        # Delete existing tags for this team and re-insert
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]
        )
        client.query(f"DELETE FROM `{table_ref}` WHERE team_id = @team_id", job_config=job_config).result()

        # Insert new tags
        rows = []
        for key, value in tags.items():
            if value:  # Skip empty values
                rows.append({
                    'team_id': team_id,
                    'tag_key': key.strip().lower(),
                    'tag_value': str(value).strip(),
                    'updated_at': datetime.utcnow().isoformat()
                })

        if rows:
            errors = client.insert_rows_json(table_ref, rows)
            if errors:
                return jsonify({'success': False, 'error': str(errors)}), 500

        print(f"[Tags] Updated tags for team {team_id}: {list(tags.keys())}")
        return jsonify({'success': True, 'tags_count': len(rows)})
    except Exception as e:
        print(f"[Tags] Set error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>/tags/<tag_key>', methods=['DELETE'])
@require_auth()
def delete_team_tag(team_id, tag_key):
    """Delete a specific tag from a team"""
    try:
        ensure_team_tags_table()
        client = get_bq_client()
        table_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_TEAM_TAGS_TABLE}"

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
                bigquery.ScalarQueryParameter("tag_key", "STRING", tag_key.lower())
            ]
        )
        client.query(f"DELETE FROM `{table_ref}` WHERE team_id = @team_id AND tag_key = @tag_key",
                     job_config=job_config).result()

        return jsonify({'success': True})
    except Exception as e:
        print(f"[Tags] Delete error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/by-tag', methods=['GET'])
def list_teams_by_tag():
    """List teams filtered by tag
    Query params:
        tag_key: the tag to filter by (e.g., 'department')
        tag_value: the value to match (e.g., 'Engineering')
    """
    try:
        ensure_team_tags_table()
        ensure_team_tables_once()
        tag_key = request.args.get('tag_key', '').strip().lower()
        tag_value = request.args.get('tag_value', '').strip()

        if not tag_key:
            return jsonify({'success': False, 'error': 'tag_key required'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        query = f"""
        SELECT t.team_id, t.team_name, t.manager_name, tt.tag_value
        FROM `{dataset_ref}.{BQ_TEAMS_TABLE}` t
        INNER JOIN `{dataset_ref}.{BQ_TEAM_TAGS_TABLE}` tt ON t.team_id = tt.team_id
        WHERE tt.tag_key = @tag_key
        """
        params = [bigquery.ScalarQueryParameter("tag_key", "STRING", tag_key)]

        if tag_value:
            query += " AND LOWER(tt.tag_value) = LOWER(@tag_value)"
            params.append(bigquery.ScalarQueryParameter("tag_value", "STRING", tag_value))

        query += " ORDER BY t.team_name"

        job_config = bigquery.QueryJobConfig(query_parameters=params)
        rows = list(client.query(query, job_config=job_config).result())

        teams = [{
            'team_id': r.team_id,
            'team_name': r.team_name,
            'manager_name': r.manager_name,
            f'{tag_key}': r.tag_value
        } for r in rows]

        return jsonify({'success': True, 'teams': teams, 'filter': {'tag_key': tag_key, 'tag_value': tag_value}})
    except Exception as e:
        print(f"[Tags] Filter error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# AUTH ENDPOINTS (BigQuery-based)
# ==============================================================================

# In-memory rate limiter for /auth/login. Per-instance only (Cloud Run with
# min-instances=1 makes this effective enough for the threat model: human
# credential-stuffing on a small admin app). Sliding 60-second window.
_login_attempts = {}   # key: (ip, username) -> list[unix_ts]
_login_lockouts = {}   # key: (ip, username) -> unlock_ts
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECS = 60
_LOGIN_LOCKOUT_SECS = 300  # 5 minutes after 5 failures


def _login_rate_limit_check(ip, username):
    """Return (allowed, retry_after_secs).
    Tracks per-(ip, username) failed attempts in a 60s sliding window;
    after 5 failures locks the pair out for 5 minutes."""
    now = time.time()
    key = (ip, (username or '').lower().strip())

    # Active lockout?
    unlock = _login_lockouts.get(key)
    if unlock and now < unlock:
        return False, int(unlock - now)
    if unlock:
        _login_lockouts.pop(key, None)

    # Trim window
    attempts = [t for t in _login_attempts.get(key, []) if t > now - _LOGIN_WINDOW_SECS]
    _login_attempts[key] = attempts
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        _login_lockouts[key] = now + _LOGIN_LOCKOUT_SECS
        return False, _LOGIN_LOCKOUT_SECS
    return True, 0


def _login_record_failure(ip, username):
    key = (ip, (username or '').lower().strip())
    _login_attempts.setdefault(key, []).append(time.time())


def _login_record_success(ip, username):
    # Successful login clears any counter for this pair
    key = (ip, (username or '').lower().strip())
    _login_attempts.pop(key, None)
    _login_lockouts.pop(key, None)


@app.route('/auth/login', methods=['POST'])
def auth_login():
    """Login endpoint - validates username/password against BigQuery users table"""
    try:
        data = request.get_json() or {}
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()

        if not username or not password:
            return jsonify({'success': False, 'error': 'Username and password required'}), 400

        # Use the original client IP from the proxy (Cloud Run sets X-Forwarded-For).
        # Fall back to remote_addr for local dev.
        ip = (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
              or request.remote_addr or 'unknown')

        allowed, retry_after = _login_rate_limit_check(ip, username)
        if not allowed:
            resp = jsonify({
                'success': False,
                'error': f'Too many login attempts. Try again in {retry_after} seconds.',
            })
            resp.status_code = 429
            resp.headers['Retry-After'] = str(retry_after)
            return resp

        client = bigquery.Client(project=GCP_PROJECT_ID)
        # Fetch by username only; the password is verified in Python so it
        # can be a bcrypt hash. Legacy plaintext rows still work and are
        # UPGRADED to bcrypt on their first successful login.
        query = f"""
            SELECT user_id, username, name, role, email, password
            FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.app_users`
            WHERE LOWER(TRIM(username)) = LOWER(@username)
            ORDER BY CASE role WHEN 'superadmin' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END
            LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("username", "STRING", username),
            ]
        )
        results = list(client.query(query, job_config=job_config).result())

        user = results[0] if results else None
        password_ok = False
        legacy_upgrade = False
        if user is not None:
            stored = (user.password or '').strip()
            if stored.startswith(('$2a$', '$2b$', '$2y$')):
                try:
                    import bcrypt as _bc
                    password_ok = _bc.checkpw(password.encode('utf-8'), stored.encode('utf-8'))
                except Exception:
                    password_ok = False
            else:
                # Legacy plaintext row: constant-time compare, then upgrade.
                password_ok = hmac.compare_digest(stored, password)
                legacy_upgrade = password_ok

        if not password_ok:
            _login_record_failure(ip, username)
            return jsonify({'success': False, 'error': 'Invalid username or password'}), 401

        if legacy_upgrade:
            try:
                import bcrypt as _bc
                new_hash = _bc.hashpw(password.encode('utf-8'), _bc.gensalt()).decode('utf-8')
                client.query(
                    f"UPDATE `{GCP_PROJECT_ID}.{BQ_DATASET}.app_users` "
                    f"SET password = @p WHERE user_id = @uid",
                    job_config=bigquery.QueryJobConfig(query_parameters=[
                        bigquery.ScalarQueryParameter("p", "STRING", new_hash),
                        bigquery.ScalarQueryParameter("uid", "INT64", user.user_id),
                    ])
                ).result()
                print(f"[Auth] Upgraded '{username}' to bcrypt hash")
            except Exception as ue:
                # Non-fatal (e.g. row still in streaming buffer) — retried
                # automatically on the next login.
                print(f"[Auth] bcrypt upgrade deferred for '{username}': {ue}")

        _login_record_success(ip, username)
        return jsonify({
            'success': True,
            # Signed token: the frontend sends this as Authorization: Bearer
            # on every mutating request; require_auth validates it.
            'token': make_auth_token(user.username, user.role),
            'user': {
                'id': user.user_id,
                'username': user.username,
                'name': user.name,
                'role': user.role,
                'email': user.email
            }
        })
    except Exception as e:
        print(f"[Auth] Login error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/auth/users', methods=['GET'])
def auth_list_users():
    """List all users (admin only in production)"""
    try:
        client = bigquery.Client(project=GCP_PROJECT_ID)
        query = f"""
            SELECT user_id, username, name, role, email
            FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.app_users`
            ORDER BY user_id
        """
        results = list(client.query(query).result())
        users = [{'id': r.user_id, 'username': r.username, 'name': r.name, 'role': r.role, 'email': r.email} for r in results]
        return jsonify({'success': True, 'users': users})
    except Exception as e:
        print(f"[Auth] List users error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/auth/users', methods=['POST'])
@require_auth(roles=['admin', 'superadmin'])
def auth_create_user():
    """Create a new user"""
    try:
        data = request.get_json() or {}
        username = (data.get('username') or '').strip()
        password = (data.get('password') or '').strip()
        name = (data.get('name') or '').strip()
        role = (data.get('role') or 'hr').strip()
        email = (data.get('email') or '').strip()

        if not username or not password or not name:
            return jsonify({'success': False, 'error': 'username, password, and name are required'}), 400
        if role not in ('admin', 'hr', 'manager', 'superadmin'):
            return jsonify({'success': False, 'error': 'role must be admin, hr, manager, or superadmin'}), 400

        client = bigquery.Client(project=GCP_PROJECT_ID)

        # Reject duplicate usernames: login fetches ONE row per username
        # (highest role), so a duplicate would permanently shadow the other.
        dup = list(client.query(
            f"SELECT 1 FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.app_users` "
            f"WHERE LOWER(TRIM(username)) = LOWER(@u) LIMIT 1",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("u", "STRING", username)])
        ).result())
        if dup:
            return jsonify({'success': False, 'error': 'Username already exists'}), 409

        # Generate numeric user_id (timestamp-based for uniqueness)
        import time
        user_id = int(time.time() * 1000) % 2147483647  # Keep within INT range

        # Never store plaintext: hash at creation (login verifies bcrypt).
        import bcrypt as _bc
        stored_pw = _bc.hashpw(password.encode('utf-8'), _bc.gensalt()).decode('utf-8')

        errors = client.insert_rows_json(
            f"{GCP_PROJECT_ID}.{BQ_DATASET}.app_users",
            [{'user_id': user_id, 'username': username, 'password': stored_pw,
              'name': name, 'role': role, 'email': email}]
        )
        if errors:
            return jsonify({'success': False, 'error': str(errors)}), 500

        print(f"[Auth] Created user '{username}' ({role})")
        return jsonify({'success': True, 'user_id': user_id})
    except Exception as e:
        print(f"[Auth] Create user error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/auth/users/<user_id>', methods=['DELETE'])
@require_auth(roles=['admin', 'superadmin'])
def auth_delete_user(user_id):
    """Delete a user"""
    try:
        client = bigquery.Client(project=GCP_PROJECT_ID)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("user_id", "STRING", user_id)]
        )
        client.query(
            f"DELETE FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.app_users` WHERE user_id = @user_id",
            job_config=job_config
        ).result()
        print(f"[Auth] Deleted user {user_id}")
        return jsonify({'success': True})
    except Exception as e:
        print(f"[Auth] Delete user error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# ATTENDANCE DATA ENDPOINTS (Replaces Supabase)
# ==============================================================================

@app.route('/data/attendance', methods=['GET'])
def data_get_all_attendance():
    """Get all attendance data (report_date + employees JSON)"""
    try:
        client = bigquery.Client(project=GCP_PROJECT_ID)
        query = f"""
            SELECT report_date, employees, uploaded_by, uploaded_at
            FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.attendance_reports`
            ORDER BY report_date
        """
        results = list(client.query(query).result())
        dates = []
        for r in results:
            emp_data = r.employees
            if isinstance(emp_data, str):
                try:
                    emp_data = json.loads(emp_data)
                except:
                    pass
            dates.append({
                'report_date': str(r.report_date),
                'employees': emp_data,
                'uploaded_by': r.uploaded_by,
                'uploaded_at': str(r.uploaded_at) if r.uploaded_at else None
            })
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        print(f"[Data] Get all attendance error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/data/attendance/dates', methods=['GET'])
def data_get_attendance_dates():
    """Get list of dates with attendance data"""
    try:
        client = bigquery.Client(project=GCP_PROJECT_ID)
        query = f"""
            SELECT report_date
            FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.attendance_reports`
            ORDER BY report_date
        """
        results = list(client.query(query).result())
        dates = [str(r.report_date) for r in results]
        return jsonify({'success': True, 'dates': dates})
    except Exception as e:
        print(f"[Data] Get dates error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/data/attendance/<date>', methods=['GET'])
def data_get_day_attendance(date):
    """Get attendance data for a specific date"""
    try:
        client = bigquery.Client(project=GCP_PROJECT_ID)
        query = f"""
            SELECT employees
            FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.attendance_reports`
            WHERE report_date = @date
            LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("date", "STRING", date)]
        )
        results = list(client.query(query, job_config=job_config).result())
        if not results:
            return jsonify({'success': False, 'error': 'Date not found'}), 404

        emp_data = results[0].employees
        if isinstance(emp_data, str):
            try:
                emp_data = json.loads(emp_data)
            except:
                pass
        return jsonify({'success': True, 'employees': emp_data})
    except Exception as e:
        print(f"[Data] Get day attendance error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/data/attendance', methods=['POST'])
@require_auth()
def data_save_attendance():
    """Save/update attendance data for a date"""
    try:
        data = request.get_json() or {}
        report_date = data.get('report_date')
        employees = data.get('employees')
        uploaded_by = data.get('uploaded_by', 'unknown')

        if not report_date or employees is None:
            return jsonify({'success': False, 'error': 'report_date and employees required'}), 400

        client = bigquery.Client(project=GCP_PROJECT_ID)

        # Check if date exists
        check_query = f"""
            SELECT id FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.attendance_reports`
            WHERE report_date = @date LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("date", "STRING", report_date)]
        )
        existing = list(client.query(check_query, job_config=job_config).result())

        employees_json = json.dumps(employees) if not isinstance(employees, str) else employees

        if existing:
            # Update
            update_query = f"""
                UPDATE `{GCP_PROJECT_ID}.{BQ_DATASET}.attendance_reports`
                SET employees = @employees,
                    uploaded_by = @uploaded_by,
                    uploaded_at = CURRENT_TIMESTAMP()
                WHERE report_date = @date
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("employees", "STRING", employees_json),
                    bigquery.ScalarQueryParameter("uploaded_by", "STRING", uploaded_by),
                    bigquery.ScalarQueryParameter("date", "STRING", report_date),
                ]
            )
            client.query(update_query, job_config=job_config).result()
        else:
            # Insert
            table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.attendance_reports"
            rows = [{
                'report_date': report_date,
                'employees': employees_json,
                'uploaded_by': uploaded_by,
                'uploaded_at': datetime.utcnow().isoformat()
            }]
            errors = client.insert_rows_json(table_id, rows)
            if errors:
                return jsonify({'success': False, 'error': str(errors)}), 500

        return jsonify({'success': True})
    except Exception as e:
        print(f"[Data] Save attendance error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/data/attendance/<date>', methods=['DELETE'])
@require_auth()
def data_delete_attendance(date):
    """Delete attendance data for a date"""
    try:
        client = bigquery.Client(project=GCP_PROJECT_ID)
        query = f"""
            DELETE FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.attendance_reports`
            WHERE report_date = @date
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("date", "STRING", date)]
        )
        client.query(query, job_config=job_config).result()
        return jsonify({'success': True})
    except Exception as e:
        print(f"[Data] Delete attendance error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# EMPLOYEE MANAGEMENT - CRUD, visitor tracking, employee detail
# ==============================================================================

@app.route('/employees', methods=['GET'])
def list_employees():
    """List all employees from registry. Query: ?search=name&category=employee&status=active&team_id=xxx"""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        search = request.args.get('search', '').strip()
        category = request.args.get('category', '')
        status = request.args.get('status', '')
        team_id = request.args.get('team_id', '')

        query = f"SELECT * FROM `{dataset_ref}.employee_registry` WHERE 1=1"
        params = []

        if search:
            query += " AND (LOWER(participant_name) LIKE @search OR LOWER(display_name) LIKE @search OR LOWER(participant_email) LIKE @search)"
            params.append(bigquery.ScalarQueryParameter("search", "STRING", f"%{search.lower()}%"))
        if category:
            query += " AND category = @category"
            params.append(bigquery.ScalarQueryParameter("category", "STRING", category))
        if status:
            query += " AND status = @status"
            params.append(bigquery.ScalarQueryParameter("status", "STRING", status))
        if team_id:
            query += " AND team_id = @team_id"
            params.append(bigquery.ScalarQueryParameter("team_id", "STRING", team_id))

        query += " ORDER BY participant_name"
        job_config = bigquery.QueryJobConfig(query_parameters=params) if params else None
        rows = list(client.query(query, job_config=job_config).result() if job_config else client.query(query).result())

        employees = [{
            'employee_id': r.employee_id,
            'participant_name': r.participant_name,
            'display_name': r.display_name or r.participant_name,
            'participant_email': r.participant_email or '',
            'status': r.status or 'active',
            'category': r.category or 'employee',
            'team_id': r.team_id or '',
            'notes': r.notes or '',
        } for r in rows]

        return jsonify({'success': True, 'employees': employees, 'total': len(employees)})
    except Exception as e:
        print(f"[Employees] List error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees', methods=['POST'])
@require_auth()
def create_employee():
    """Add employee to registry"""
    try:
        ensure_team_tables_once()
        data = request.json or {}
        name = (data.get('participant_name') or data.get('name') or '').strip()
        if not name:
            return jsonify({'success': False, 'error': 'name is required'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        emp_id = str(uuid_lib.uuid4())

        rows = [{
            'employee_id': emp_id,
            'participant_name': normalize_participant_name(name),
            'display_name': (data.get('display_name') or name).strip(),
            'participant_email': (data.get('email') or data.get('participant_email') or '').strip(),
            'status': (data.get('status') or 'active').strip(),
            'category': (data.get('category') or 'employee').strip(),
            'team_id': (data.get('team_id') or '').strip(),
            'notes': (data.get('notes') or '').strip(),
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat(),
        }]
        errors = client.insert_rows_json(f"{dataset_ref}.employee_registry", rows)
        if errors:
            return jsonify({'success': False, 'error': str(errors)}), 500

        return jsonify({'success': True, 'employee_id': emp_id})
    except Exception as e:
        print(f"[Employees] Create error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/<employee_id>', methods=['PUT'])
@require_auth()
def update_employee(employee_id):
    """Update employee: rename, change status, category, notes"""
    try:
        ensure_team_tables_once()
        data = request.json or {}
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        updates = []
        params = [bigquery.ScalarQueryParameter("employee_id", "STRING", employee_id)]

        for field in ['participant_name', 'display_name', 'participant_email', 'status', 'category', 'team_id', 'notes']:
            if field in data:
                updates.append(f"{field} = @{field}")
                params.append(bigquery.ScalarQueryParameter(field, "STRING", str(data[field] or '').strip()))

        if not updates:
            return jsonify({'success': False, 'error': 'No fields to update'}), 400

        updates.append("updated_at = CURRENT_TIMESTAMP()")
        query = f"UPDATE `{dataset_ref}.employee_registry` SET {', '.join(updates)} WHERE employee_id = @employee_id"
        client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()

        return jsonify({'success': True})
    except Exception as e:
        print(f"[Employees] Update error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/<employee_id>', methods=['DELETE'])
@require_auth(roles=['admin', 'superadmin'])
def delete_employee(employee_id):
    """Delete employee from registry"""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("employee_id", "STRING", employee_id)]
        )
        client.query(f"DELETE FROM `{dataset_ref}.employee_registry` WHERE employee_id = @employee_id",
                      job_config=job_config).result()
        return jsonify({'success': True})
    except Exception as e:
        print(f"[Employees] Delete error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


def ensure_employee_registry_entry(client, dataset_ref, employee_name, employee_email='', team_id='', notes=''):
    """Ensure an employee exists in the registry and optionally in team_members."""
    normalized_name = normalize_participant_name(employee_name or '')
    if not normalized_name:
        raise ValueError('employee_name is required')

    existing = list(client.query(
        f"""
        SELECT employee_id, participant_email, team_id
        FROM `{dataset_ref}.employee_registry`
        WHERE LOWER(TRIM(participant_name)) = LOWER(TRIM(@name))
        LIMIT 1
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("name", "STRING", normalized_name)
        ])
    ).result())

    created = False
    employee_id = None
    if existing:
        employee_id = existing[0].employee_id
    else:
        employee_id = str(uuid_lib.uuid4())
        registry_row = {
            'employee_id': employee_id,
            'participant_name': normalized_name,
            'display_name': normalized_name,
            'participant_email': (employee_email or '').strip(),
            'status': 'active',
            'category': 'employee',
            'team_id': (team_id or '').strip(),
            'notes': (notes or '').strip(),
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat(),
        }
        errors = client.insert_rows_json(f"{dataset_ref}.employee_registry", [registry_row])
        if errors:
            raise RuntimeError(str(errors))
        created = True

    if team_id:
        member_exists = list(client.query(
            f"""
            SELECT member_id
            FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}`
            WHERE team_id = @team_id
              AND LOWER(TRIM(participant_name)) = LOWER(TRIM(@name))
            LIMIT 1
            """,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
                bigquery.ScalarQueryParameter("name", "STRING", normalized_name),
            ])
        ).result())
        if not member_exists:
            client.insert_rows_json(f"{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}", [{
                'member_id': str(uuid_lib.uuid4()),
                'team_id': team_id,
                'participant_name': normalized_name,
                'participant_email': (employee_email or '').strip(),
                'added_at': datetime.utcnow().isoformat(),
            }])

    return employee_id, created, normalized_name


def apply_daily_attendance_overrides(client, dataset_ref, employee_name, daily, source_name, created_by='system-assign'):
    """Copy monthly daily attendance rows to attendance_overrides for one employee."""
    normalized_name = normalize_participant_name(employee_name or '')
    if not normalized_name:
        raise ValueError('employee_name is required')

    overrides_created = 0
    for day in daily or []:
        date_str = day.get('date')
        if not date_str:
            continue

        delete_query = f"""
        DELETE FROM `{dataset_ref}.{BQ_ATTENDANCE_OVERRIDES_TABLE}`
        WHERE LOWER(TRIM(employee_name)) = LOWER(TRIM(@emp_name)) AND event_date = @date
        """
        client.query(delete_query, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("emp_name", "STRING", normalized_name),
            bigquery.ScalarQueryParameter("date", "DATE", date_str),
        ])).result()

        override_row = {
            'override_id': str(uuid_lib.uuid4()),
            'employee_id': None,
            'employee_name': normalized_name,
            'event_date': date_str,
            'first_seen_ist': day.get('first_seen_ist', ''),
            'last_seen_ist': day.get('last_seen_ist', ''),
            'status': day.get('status', 'Present'),
            'active_mins': day.get('active_minutes', 0),
            'break_mins': day.get('break_minutes', 0),
            'isolation_mins': day.get('isolation_minutes', 0),
            'notes': f'Assigned from: {source_name}',
            'created_by': created_by,
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat(),
        }
        errors = client.insert_rows_json(f"{dataset_ref}.{BQ_ATTENDANCE_OVERRIDES_TABLE}", [override_row])
        if errors:
            raise RuntimeError(str(errors))
        overrides_created += 1

    return overrides_created


def mark_source_participant_handled(client, dataset_ref, source_name, notes):
    """Insert a placeholder registry row so handled unrecognized names stop reappearing."""
    source_name = (source_name or '').strip()
    if not source_name:
        return

    existing = list(client.query(
        f"""
        SELECT employee_id
        FROM `{dataset_ref}.employee_registry`
        WHERE LOWER(TRIM(participant_name)) = LOWER(TRIM(@name))
        LIMIT 1
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("name", "STRING", source_name)
        ])
    ).result())
    if existing:
        return

    client.insert_rows_json(f"{dataset_ref}.employee_registry", [{
        'employee_id': str(uuid_lib.uuid4()),
        'participant_name': source_name,
        'display_name': source_name,
        'participant_email': '',
        'status': 'inactive',
        'category': 'assigned',
        'team_id': '',
        'notes': notes,
        'created_at': datetime.utcnow().isoformat(),
        'updated_at': datetime.utcnow().isoformat(),
    }])


@app.route('/employees/assign-attendance', methods=['POST'])
@require_auth()
def assign_unrecognized_attendance():
    """Assign an unrecognized participant's daily attendance to one employee."""
    try:
        ensure_team_tables_once()
        data = request.get_json() or {}
        source_name = (data.get('source_name') or '').strip()
        employee = data.get('employee', {}) or {}
        daily = data.get('daily', []) or []
        mark_source = bool(data.get('mark_source', True))

        employee_name = (employee.get('name') or '').strip()
        employee_email = (employee.get('email') or '').strip()
        team_id = (employee.get('team_id') or '').strip()

        if not source_name:
            return jsonify({'success': False, 'error': 'source_name is required'}), 400
        if not employee_name:
            return jsonify({'success': False, 'error': 'employee.name is required'}), 400
        if not daily:
            return jsonify({'success': False, 'error': 'daily attendance data is required'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        employee_id, created, normalized_name = ensure_employee_registry_entry(
            client,
            dataset_ref,
            employee_name,
            employee_email,
            team_id,
            f'Assigned from unrecognized participant: {source_name}',
        )
        overrides_created = apply_daily_attendance_overrides(
            client,
            dataset_ref,
            normalized_name,
            daily,
            source_name,
            'system-assign',
        )

        if mark_source:
            mark_source_participant_handled(
                client,
                dataset_ref,
                source_name,
                f'Attendance assigned to: {normalized_name}',
            )

        return jsonify({
            'success': True,
            'employee_id': employee_id,
            'employee_created': created,
            'overrides_created': overrides_created,
            'message': f'Assigned {len(daily)} attendance day(s) from {source_name} to {normalized_name}',
        })
    except Exception as e:
        print(f"[AssignAttendance] Error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/split-shared-attendance', methods=['POST'])
@require_auth()
def split_shared_attendance():
    """
    Split attendance from a shared session (e.g., "Shashank & Satyam & Ram")
    into N employees. All employees get the same attendance data for the
    dates in the shared session.

    Request body (new form — N employees):
    {
        "shared_name": "Shashank & Satyam & Ram",
        "employees": [
            { "name": "Shashank", "email": "...", "team_id": "..." },
            { "name": "Satyam",   "email": "...", "team_id": "..." },
            { "name": "Ram",      "email": "...", "team_id": "..." }
        ],
        "daily": [ { "date": "2026-04-01", "first_seen_ist": "09:30", ... }, ... ],
        "apply_attendance": true
    }

    Also accepts the legacy 2-person form (employee1 / employee2).
    """
    try:
        ensure_team_tables_once()
        data = request.get_json() or {}
        shared_name = (data.get('shared_name') or '').strip()
        daily = data.get('daily', [])
        apply_attendance = bool(data.get('apply_attendance', True))

        # Accept both the new `employees` array and the old employee1/employee2 pair.
        employees_payload = data.get('employees')
        if not employees_payload:
            emp1 = data.get('employee1')
            emp2 = data.get('employee2')
            employees_payload = [e for e in (emp1, emp2) if e]

        if not shared_name:
            return jsonify({'success': False, 'error': 'shared_name is required'}), 400
        if not isinstance(employees_payload, list) or len(employees_payload) < 2:
            return jsonify({'success': False, 'error': 'At least two employees are required'}), 400
        if apply_attendance and not daily:
            return jsonify({'success': False, 'error': 'No daily attendance data provided'}), 400
        # All employees must have a name
        missing = [i for i, e in enumerate(employees_payload) if not (e or {}).get('name', '').strip()]
        if missing:
            return jsonify({'success': False, 'error': f'All employees must have a name (missing for index {missing})'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        employees_created = []
        overrides_created = 0
        assigned_names = []

        for emp in employees_payload:
            emp_name = (emp.get('name') or '').strip()
            emp_email = (emp.get('email') or '').strip()
            team_id = (emp.get('team_id') or '').strip()
            if not emp_name:
                continue

            _, created, normalized_name = ensure_employee_registry_entry(
                client,
                dataset_ref,
                emp_name,
                emp_email,
                team_id,
                f'Split from shared session: {shared_name}',
            )
            if created:
                employees_created.append(normalized_name)
            assigned_names.append(normalized_name)

            if apply_attendance:
                overrides_created += apply_daily_attendance_overrides(
                    client,
                    dataset_ref,
                    normalized_name,
                    daily,
                    shared_name,
                    'system-split',
                )

        mark_source_participant_handled(
            client,
            dataset_ref,
            shared_name,
            f'Split into: {", ".join(assigned_names)}',
        )

        return jsonify({
            'success': True,
            'employees_created': employees_created,
            'overrides_created': overrides_created,
            'assigned_to': assigned_names,
            'message': (
                f'Shared participant split to {len(assigned_names)} employees: {", ".join(assigned_names)}'
                + (f' with attendance copied for {len(daily)} days' if apply_attendance else ' without copying attendance')
            )
        })

    except Exception as e:
        print(f"[Split] Error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/sync-from-teams', methods=['POST'])
@require_auth(roles=['admin', 'superadmin'])
def sync_employees_from_teams():
    """Populate employee_registry from existing team_members. Run once to initialize."""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # Get all team members not already in registry
        query = f"""
        SELECT tm.member_id, tm.team_id, tm.participant_name, tm.participant_email, t.team_name
        FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` tm
        LEFT JOIN `{dataset_ref}.{BQ_TEAMS_TABLE}` t ON tm.team_id = t.team_id
        WHERE LOWER(TRIM(tm.participant_name)) NOT IN (
            SELECT LOWER(TRIM(participant_name)) FROM `{dataset_ref}.employee_registry`
        )
        """
        rows = list(client.query(query).result())

        if not rows:
            return jsonify({'success': True, 'added': 0, 'message': 'All team members already in registry'})

        inserts = []
        for r in rows:
            inserts.append({
                'employee_id': str(uuid_lib.uuid4()),
                'participant_name': normalize_participant_name(r.participant_name),
                'display_name': normalize_participant_name(r.participant_name),
                'participant_email': r.participant_email or '',
                'status': 'active',
                'category': 'employee',
                'team_id': r.team_id or '',
                'notes': f'Team: {r.team_name}' if r.team_name else '',
                'created_at': datetime.utcnow().isoformat(),
                'updated_at': datetime.utcnow().isoformat(),
            })

        errors = client.insert_rows_json(f"{dataset_ref}.employee_registry", inserts)
        if errors:
            print(f"[Employees] Sync errors: {errors[:3]}")

        return jsonify({'success': True, 'added': len(inserts)})
    except Exception as e:
        print(f"[Employees] Sync error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/unrecognized/<date>', methods=['GET'])
def list_unrecognized_participants(date):
    """Find participants in Zoom on a date who are NOT in the employee registry.
    Uses smart multi-pass matching: exact, normalized, team-keyword-stripped, first-name."""
    try:
        ensure_team_tables_once()
        report_date = validate_date_format(date)
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # 1. Get all zoom participants for the date (from presence_intervals —
        # the webhook-built source of truth; snapshots stopped in July 2026)
        zoom_query = f"""
        SELECT DISTINCT participant_name, participant_email
        FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}`
        WHERE event_date = @date
          AND participant_name IS NOT NULL AND participant_name != ''
          AND LOWER(participant_name) NOT LIKE '%scout%'
        ORDER BY participant_name
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("date", "DATE", report_date)]
        )
        zoom_rows = list(client.query(zoom_query, job_config=job_config).result())

        # 2. Get registered employees with team info
        reg_query = f"""
        SELECT e.participant_name, e.display_name, e.team_id, t.team_name
        FROM `{dataset_ref}.employee_registry` e
        LEFT JOIN `{dataset_ref}.teams` t ON e.team_id = t.team_id
        """
        reg_rows = list(client.query(reg_query).result())

        # 3. Build matching structures
        reg_names = set()  # all known lowercase names
        reg_first_names = {}  # first_name -> [full_names]
        for r in reg_rows:
            name_low = (r.participant_name or '').lower().strip()
            if not name_low:
                continue
            reg_names.add(name_low)
            normed = normalize_participant_name(r.participant_name).lower().strip()
            reg_names.add(normed)
            if r.display_name:
                reg_names.add(r.display_name.lower().strip())
            # Index by first name
            first = name_low.split()[0] if name_low.split() else ''
            if first and len(first) >= 3:
                reg_first_names.setdefault(first, []).append(name_low)

        # 4. Extract team keywords from team names
        # Only use identifiers from non-person team names (skip "Team Aaron" etc.)
        team_keywords = set()
        seen_teams = set()
        for r in reg_rows:
            tn = r.team_name
            if not tn or tn in seen_teams:
                continue
            seen_teams.add(tn)
            # Skip "Team <PersonName>" — those names collide with first names
            if tn.lower().startswith('team '):
                continue
            # Only take the FIRST word (org name like KPRC, Accurest, Vridam)
            # Skip person names like Kuldeep, Pawan, Yogendra
            first_word = tn.replace('-', ' ').split()[0].lower() if tn.split() else ''
            if first_word and first_word not in ('team', 'client', 'sir') and len(first_word) >= 3:
                team_keywords.add(first_word)

        def _strip_team_and_clean(name):
            """Strip team keywords, professional prefixes, and invisible chars from a name."""
            n = name
            for kw in team_keywords:
                pat = _re.escape(kw)
                # Suffix: Name_KPRC, Name-KPRC, Name - KPRC, Name KPRC
                n = _re.sub(r'[\s_-]+' + pat + r'$', '', n, flags=_re.IGNORECASE)
                # Prefix: KPRC_Name, KPRC-Name, KPRC Name
                n = _re.sub(r'^' + pat + r'[\s_-]+', '', n, flags=_re.IGNORECASE)
            # Professional prefixes (CA, CS, Dr, Er)
            n = _re.sub(r'^(ca|cs|dr|er)\s+', '', n, flags=_re.IGNORECASE)
            # Soft hyphens and zero-width chars
            n = _re.sub(r'[\u00ad\u200b\u200c\u200d]', '', n)
            return n.strip()

        def is_recognized(raw_name):
            """Multi-pass matching against employee registry."""
            # Pass 1: Exact match on raw name
            raw_low = raw_name.lower().strip()
            if raw_low in reg_names:
                return True

            # Pass 2: Strip team keywords from RAW name first (handles KPRC_Aditi, Vridam Nayana)
            stripped_raw = _strip_team_and_clean(raw_low)
            if stripped_raw and stripped_raw in reg_names:
                return True

            # Pass 3: Normalized match (strips rejoin suffixes like -2, _text)
            normed = normalize_participant_name(raw_name).lower().strip()
            if normed in reg_names:
                return True

            # Pass 4: Strip team keywords from normalized name
            cleaned = _strip_team_and_clean(normed)
            if cleaned and cleaned in reg_names:
                return True

            # Pass 5: Check if any registered full name is contained in the zoom name
            for rn in reg_names:
                if len(rn) >= 4 and rn in raw_low:
                    return True

            # Pass 6: First-name match (single-word name matches a registered first name)
            best_name = stripped_raw or cleaned
            if best_name and ' ' not in best_name and len(best_name) >= 3:
                if best_name in reg_first_names:
                    return True

            return False

        # 5. Filter unrecognized
        unrecognized = []
        for zp in zoom_rows:
            raw = zp.participant_name
            if is_recognized(raw):
                continue
            # Best display name: try team-strip on raw first, then normalize
            stripped = _strip_team_and_clean(raw.lower().strip())
            normed = normalize_participant_name(raw)
            # Pick whichever is more informative (longer, non-empty)
            display = stripped if stripped and len(stripped) > len(_strip_team_and_clean(normed.lower())) else _strip_team_and_clean(normed.lower())
            if not display:
                display = normed
            # Title case the display name
            display = display.title() if display == display.lower() else display
            unrecognized.append({
                'participant_name': raw,
                'normalized_name': display.strip(),
                'participant_email': zp.participant_email or '',
            })

        return jsonify({
            'success': True,
            'date': report_date,
            'unrecognized': unrecognized,
            'count': len(unrecognized)
        })
    except Exception as e:
        print(f"[Employees] Unrecognized error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/unrecognized-monthly', methods=['GET'])
def list_unrecognized_monthly():
    """Monthly view of unrecognized participants with full attendance stats.
    Query params: year, month.
    Returns per-person: total active/break/isolation mins, days present, and a
    daily[] array mirroring /employees/<id>/attendance/<month>."""
    try:
        ensure_team_tables_once()
        year = int(request.args.get('year', get_ist_now().year))
        month = int(request.args.get('month', get_ist_now().month))
        if month < 1 or month > 12:
            return jsonify({'success': False, 'error': 'Invalid month'}), 400

        from calendar import monthrange
        _, last_day = monthrange(year, month)
        start_date = f"{year}-{month:02d}-01"
        end_date = f"{year}-{month:02d}-{last_day:02d}"

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # Per-day, per-name stats from presence_intervals (one source of
        # hours, same as classified-monthly). active_mins keeps the legacy
        # semantic for non-employees: BREAKOUT-room time only — Main Room
        # lobby time never counted toward presence.
        query = f"""
        SELECT
            pi.event_date,
            pi.participant_key AS name_key,
            ANY_VALUE(pi.participant_name) AS participant_name,
            COALESCE(MAX(pi.participant_email), '') AS participant_email,
            FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MIN(pi.start_ts), INTERVAL 330 MINUTE)) AS first_seen_ist,
            FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MAX(pi.end_ts), INTERVAL 330 MINUTE)) AS last_seen_ist,
            CAST(CEILING(SUM(IF(pi.room_category = 'breakout', pi.duration_seconds, 0)) / 60.0) AS INT64) AS active_mins,
            CAST(CEILING(SUM(IF(pi.room_category = 'break', pi.duration_seconds, 0)) / 60.0) AS INT64) AS break_mins,
            CAST(CEILING(SUM(pi.alone_seconds) / 60.0) AS INT64) AS isolation_mins
        FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}` pi
        WHERE pi.event_date BETWEEN @start_date AND @end_date
          AND pi.participant_name IS NOT NULL AND pi.participant_name != ''
          AND LOWER(pi.participant_name) NOT LIKE '%scout%'
        GROUP BY pi.event_date, pi.participant_key
        ORDER BY name_key, pi.event_date
        """

        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
        ])
        rows = list(client.query(query, job_config=job_config).result())

        # Load registry to decide which name_keys are unrecognized.
        reg_rows = list(client.query(
            f"""SELECT e.participant_name, e.display_name, e.participant_email, e.team_id, t.team_name
                FROM `{dataset_ref}.employee_registry` e
                LEFT JOIN `{dataset_ref}.teams` t ON e.team_id = t.team_id"""
        ).result())

        reg_names = set()
        reg_first_names = {}
        reg_emails = set()
        for r in reg_rows:
            name_low = (r.participant_name or '').lower().strip()
            if name_low:
                reg_names.add(name_low)
            normed = normalize_participant_name(r.participant_name or '').lower().strip()
            if normed:
                reg_names.add(normed)
            if r.display_name:
                reg_names.add(r.display_name.lower().strip())
            first = name_low.split()[0] if name_low.split() else ''
            if first and len(first) >= 3:
                reg_first_names.setdefault(first, []).append(name_low)
            if getattr(r, 'participant_email', None):
                reg_emails.add(r.participant_email.lower().strip())

        # ── Email bridge ────────────────────────────────────────────
        # Every (name_key, email) pair seen in presence_intervals this month,
        # so a participant is recognised if their email alone matches a
        # registered employee (handles renames to brand-new strings).
        # presence_intervals carries no Zoom UUID — identity there is already
        # the normalized name + email, so the old UUID bridge is gone.
        bridge_rows = list(client.query(
            f"""
            SELECT DISTINCT
              participant_key AS name_key,
              LOWER(TRIM(COALESCE(participant_email, ''))) AS email_key
            FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}`
            WHERE event_date BETWEEN @start_date AND @end_date
              AND participant_name IS NOT NULL AND participant_name != ''
            """,
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
                bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
            ]),
        ).result())

        email_to_names = {}
        name_to_emails = {}
        for b in bridge_rows:
            nk = b.name_key or ''
            ek = b.email_key or ''
            if nk and ek:
                name_to_emails.setdefault(nk, set()).add(ek)
                email_to_names.setdefault(ek, set()).add(nk)

        team_keywords = set()
        seen_teams = set()
        for r in reg_rows:
            tn = r.team_name
            if not tn or tn in seen_teams:
                continue
            seen_teams.add(tn)
            if tn.lower().startswith('team '):
                continue
            first_word = tn.replace('-', ' ').split()[0].lower() if tn.split() else ''
            if first_word and first_word not in ('team', 'client', 'sir') and len(first_word) >= 3:
                team_keywords.add(first_word)

        def _strip_team_and_clean(name):
            n = name
            for kw in team_keywords:
                pat = _re.escape(kw)
                n = _re.sub(r'[\s_-]+' + pat + r'$', '', n, flags=_re.IGNORECASE)
                n = _re.sub(r'^' + pat + r'[\s_-]+', '', n, flags=_re.IGNORECASE)
            n = _re.sub(r'^(ca|cs|dr|er)\s+', '', n, flags=_re.IGNORECASE)
            n = _re.sub(r'[\u00ad\u200b\u200c\u200d]', '', n)
            return n.strip()

        def _name_in_registry(raw_name):
            """Original name-only recognition (multi-pass)."""
            raw_low = (raw_name or '').lower().strip()
            if not raw_low:
                return True  # empty = ignore, treat as recognised
            if raw_low in reg_names:
                return True
            stripped_raw = _strip_team_and_clean(raw_low)
            if stripped_raw and stripped_raw in reg_names:
                return True
            normed = normalize_participant_name(raw_name).lower().strip()
            if normed in reg_names:
                return True
            cleaned = _strip_team_and_clean(normed)
            if cleaned and cleaned in reg_names:
                return True
            for rn in reg_names:
                if len(rn) >= 4 and rn in raw_low:
                    return True
            best_name = stripped_raw or cleaned
            if best_name and ' ' not in best_name and len(best_name) >= 3:
                if best_name in reg_first_names:
                    return True
            return False

        # Seed "known" sets from registry + name matches.
        known_emails = set(reg_emails)
        known_name_keys = set()

        for name_key in name_to_emails.keys():
            if _name_in_registry(name_key):
                known_name_keys.add(name_key)
                for e in name_to_emails.get(name_key, set()):
                    if e: known_emails.add(e)

        # Transitive closure over emails: if an email is known, every name
        # that ever used it is known too (and vice versa).
        changed = True
        while changed:
            changed = False
            for e in list(known_emails):
                for nk in email_to_names.get(e, set()):
                    if nk and nk not in known_name_keys:
                        known_name_keys.add(nk); changed = True
                        for e2 in name_to_emails.get(nk, set()):
                            if e2 and e2 not in known_emails:
                                known_emails.add(e2)

        def is_recognized_row(name_key, raw_name):
            """Augmented recognition: name match, then email bridge."""
            if not name_key:
                return True
            if name_key in known_name_keys:
                return True
            # Email-based bridging: is any email tied to this name known?
            for e in name_to_emails.get(name_key, set()):
                if e and e in known_emails:
                    return True
            # Fallback to multi-pass name heuristic (handles team-keyword
            # strips, first-name matches for brand-new names).
            return _name_in_registry(raw_name)

        # Group per name_key, attach daily list. Skip recognized people.
        by_person = {}
        for r in rows:
            key = r.name_key
            if key in by_person:
                person = by_person[key]
            else:
                # Decide on first sighting whether they are unrecognized.
                raw = r.participant_name
                if is_recognized_row(key, raw):
                    by_person[key] = None  # sentinel: skip rest of their rows
                    continue
                normed = normalize_participant_name(raw)
                stripped = _strip_team_and_clean(raw.lower().strip())
                display = stripped or _strip_team_and_clean(normed.lower()) or normed
                if display == display.lower():
                    display = display.title()
                person = {
                    'name_key': key,
                    'participant_name': raw,
                    'display_name': display.strip(),
                    'participant_email': r.participant_email or '',
                    'days_present': 0,
                    'total_active_mins': 0,
                    'total_break_mins': 0,
                    'total_isolation_mins': 0,
                    'daily': [],
                }
                by_person[key] = person

            if person is None:
                continue

            active = int(r.active_mins or 0)
            break_mins = int(r.break_mins or 0)
            iso_mins = int(r.isolation_mins or 0)
            status = 'present' if active >= 300 else 'half_day' if active >= 240 else 'absent'
            person['daily'].append({
                'date': str(r.event_date),
                'first_seen_ist': r.first_seen_ist,
                'last_seen_ist': r.last_seen_ist,
                'active_minutes': active,
                'break_minutes': break_mins,
                'isolation_minutes': iso_mins,
                'status': status,
            })
            person['days_present'] += 1 if active > 0 else 0
            person['total_active_mins'] += active
            person['total_break_mins'] += break_mins
            person['total_isolation_mins'] += iso_mins
            # Keep the "best" email seen
            if (r.participant_email or '') and not person['participant_email']:
                person['participant_email'] = r.participant_email

        unrecognized = [p for p in by_person.values() if p is not None]
        unrecognized.sort(key=lambda p: -p['total_active_mins'])

        return jsonify({
            'success': True,
            'year': year,
            'month': month,
            'start_date': start_date,
            'end_date': end_date,
            'unrecognized': unrecognized,
            'count': len(unrecognized),
        })
    except Exception as e:
        print(f"[Employees] Unrecognized monthly error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/classified-monthly', methods=['GET'])
def list_classified_monthly():
    """Monthly attendance tracking for REGISTERED non-employee participants
    (visitors/vendors/interviews/others). Query params:
      year, month, categories=visitor,vendor,interview,other.
    Same shape as /employees/unrecognized-monthly so the frontend can reuse
    the same row + daily-breakdown component."""
    try:
        ensure_team_tables_once()
        year = int(request.args.get('year', get_ist_now().year))
        month = int(request.args.get('month', get_ist_now().month))
        if month < 1 or month > 12:
            return jsonify({'success': False, 'error': 'Invalid month'}), 400
        categories_param = request.args.get('categories', 'visitor,vendor,interview,other')
        categories = [c.strip().lower() for c in categories_param.split(',') if c.strip()]
        if not categories:
            return jsonify({'success': False, 'error': 'categories required'}), 400

        from calendar import monthrange
        _, last_day = monthrange(year, month)
        start_date = f"{year}-{month:02d}-01"
        end_date = f"{year}-{month:02d}-{last_day:02d}"

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # 1. Pull registry entries in the requested categories.
        reg_query = f"""
        SELECT employee_id, participant_name, display_name, participant_email,
               category, team_id, status
        FROM `{dataset_ref}.employee_registry`
        WHERE LOWER(category) IN UNNEST(@cats)
          AND (status IS NULL OR status = '' OR status = 'active')
        """
        reg_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("cats", "STRING", categories)
        ])
        reg_rows = list(client.query(reg_query, job_config=reg_config).result())

        if not reg_rows:
            return jsonify({
                'success': True,
                'year': year, 'month': month,
                'start_date': start_date, 'end_date': end_date,
                'participants': [], 'count': 0,
            })

        # Build name_key -> registry_info lookup. Match via raw name, display
        # name, and normalized form so renamers / suffix variants still map.
        name_to_reg = {}
        def _reg_info(r):
            return {
                'employee_id': r.employee_id,
                'participant_name': r.participant_name,
                'display_name': r.display_name or r.participant_name,
                'participant_email': r.participant_email or '',
                'category': (r.category or 'other').lower(),
                'team_id': r.team_id or '',
            }
        for r in reg_rows:
            info = _reg_info(r)
            for n in (r.participant_name, r.display_name):
                if not n:
                    continue
                for key in (n.lower().strip(), normalize_participant_name(n).lower().strip()):
                    if key and key not in name_to_reg:
                        name_to_reg[key] = info

        # 2. Per-day stats from presence_intervals (one source of hours).
        # active_mins keeps the v1 semantic for visitors: BREAKOUT-room time
        # only (Main Room lobby time never counted toward visitor presence).
        # participant_key is the normalized lower-cased name, which the
        # registry lookup below already handles via its normalized keys.
        try:
            _auto_build_dates_in_range(start_date, end_date)
        except Exception as be:
            print(f"[ClassifiedMonthly] auto-build warning: {be}")

        query = f"""
        SELECT
            pi.event_date,
            pi.participant_key AS name_key,
            ANY_VALUE(pi.participant_name) AS participant_name,
            COALESCE(MAX(pi.participant_email), '') AS participant_email,
            FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MIN(pi.start_ts), INTERVAL 330 MINUTE)) AS first_seen_ist,
            FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MAX(pi.end_ts), INTERVAL 330 MINUTE)) AS last_seen_ist,
            CAST(CEILING(SUM(IF(pi.room_category = 'breakout', pi.duration_seconds, 0)) / 60.0) AS INT64) AS active_mins,
            CAST(CEILING(SUM(IF(pi.room_category = 'break', pi.duration_seconds, 0)) / 60.0) AS INT64) AS break_mins,
            CAST(CEILING(SUM(pi.alone_seconds) / 60.0) AS INT64) AS isolation_mins
        FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}` pi
        WHERE pi.event_date BETWEEN @start_date AND @end_date
        GROUP BY pi.event_date, pi.participant_key
        ORDER BY name_key, pi.event_date
        """

        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
        ])
        snap_rows = list(client.query(query, job_config=job_config).result())

        # 3. Group per registered person, attaching their daily rows.
        by_person = {}
        for r in snap_rows:
            reg = name_to_reg.get(r.name_key)
            if not reg:
                normed = normalize_participant_name(r.participant_name or '').lower().strip()
                if normed:
                    reg = name_to_reg.get(normed)
            if not reg:
                continue  # snapshot not in our category filter

            emp_id = reg['employee_id']
            person = by_person.get(emp_id)
            if person is None:
                person = {
                    'employee_id': emp_id,
                    'participant_name': reg['participant_name'],
                    'display_name': reg['display_name'],
                    'participant_email': reg['participant_email'],
                    'category': reg['category'],
                    'team_id': reg['team_id'],
                    'days_present': 0,
                    'total_active_mins': 0,
                    'total_break_mins': 0,
                    'total_isolation_mins': 0,
                    'daily': [],
                }
                by_person[emp_id] = person

            active = int(r.active_mins or 0)
            break_mins = int(r.break_mins or 0)
            iso_mins = int(r.isolation_mins or 0)
            status = 'present' if active >= 300 else 'half_day' if active >= 240 else 'absent'
            person['daily'].append({
                'date': str(r.event_date),
                'first_seen_ist': r.first_seen_ist,
                'last_seen_ist': r.last_seen_ist,
                'active_minutes': active,
                'break_minutes': break_mins,
                'isolation_minutes': iso_mins,
                'status': status,
            })
            if active > 0:
                person['days_present'] += 1
            person['total_active_mins'] += active
            person['total_break_mins'] += break_mins
            person['total_isolation_mins'] += iso_mins
            if (r.participant_email or '') and not person['participant_email']:
                person['participant_email'] = r.participant_email

        # 4. Also include registered people with zero attendance this month.
        for r in reg_rows:
            if r.employee_id in by_person:
                continue
            by_person[r.employee_id] = {
                'employee_id': r.employee_id,
                'participant_name': r.participant_name,
                'display_name': r.display_name or r.participant_name,
                'participant_email': r.participant_email or '',
                'category': (r.category or 'other').lower(),
                'team_id': r.team_id or '',
                'days_present': 0,
                'total_active_mins': 0,
                'total_break_mins': 0,
                'total_isolation_mins': 0,
                'daily': [],
            }

        participants = list(by_person.values())
        # Sort by total active time descending so most-present first.
        participants.sort(key=lambda p: (-p['total_active_mins'], p['display_name'].lower()))

        return jsonify({
            'success': True,
            'year': year, 'month': month,
            'start_date': start_date, 'end_date': end_date,
            'participants': participants,
            'count': len(participants),
        })
    except Exception as e:
        print(f"[Employees] Classified monthly error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/<employee_id>/attendance/<date>', methods=['GET'])
def employee_attendance_detail(employee_id, date):
    """Get detailed attendance for one employee for a month.
    date format: YYYY-MM (will fetch full month)"""
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # Get employee info
        emp_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("employee_id", "STRING", employee_id)]
        )
        emp_rows = list(client.query(
            f"SELECT * FROM `{dataset_ref}.employee_registry` WHERE employee_id = @employee_id",
            job_config=emp_config
        ).result())
        if not emp_rows:
            return jsonify({'success': False, 'error': 'Employee not found'}), 404

        emp = emp_rows[0]
        emp_name = emp.participant_name
        emp_email = (getattr(emp, 'participant_email', '') or '').strip().lower()

        # Parse year-month
        parts = date.split('-')
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else get_ist_now().month
        from calendar import monthrange
        _, last_day = monthrange(year, month)
        start_date = f"{year}-{month:02d}-01"
        end_date = f"{year}-{month:02d}-{last_day:02d}"

        # Daily data from presence_intervals (one source of hours).
        # Match by normalized name OR email so Zoom display-name drift
        # ("Sayli S" vs "Sayli Sonone") doesn't drop the user's data.
        try:
            _auto_build_dates_in_range(start_date, end_date)
        except Exception as be:
            print(f"[EmployeeDetail] auto-build warning: {be}")

        norm_emp = _sql_normalize_name('@emp_name')
        query = f"""
        SELECT
            pi.event_date,
            FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MIN(pi.start_ts), INTERVAL 330 MINUTE)) AS first_seen_ist,
            FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MAX(pi.end_ts), INTERVAL 330 MINUTE)) AS last_seen_ist,
            CAST(CEILING(SUM(IF(pi.room_category != 'break', pi.duration_seconds, 0)) / 60.0) AS INT64) AS active_mins,
            CAST(CEILING(SUM(IF(pi.room_category = 'break', pi.duration_seconds, 0)) / 60.0) AS INT64) AS break_mins,
            CAST(CEILING(SUM(pi.alone_seconds) / 60.0) AS INT64) AS isolation_mins
        FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}` pi
        WHERE pi.event_date BETWEEN @start_date AND @end_date
          AND (
            pi.participant_key = {norm_emp}
            OR {_sql_normalize_name('pi.participant_name')} = {norm_emp}
            OR (@emp_email != '' AND LOWER(TRIM(pi.participant_email)) = @emp_email)
          )
        GROUP BY pi.event_date
        ORDER BY pi.event_date
        """

        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
            bigquery.ScalarQueryParameter("emp_name", "STRING", emp_name),
            bigquery.ScalarQueryParameter("emp_email", "STRING", emp_email),
        ])
        rows = list(client.query(query, job_config=job_config).result())

        daily = []
        total_active = 0
        total_break = 0
        total_iso = 0
        days_present = 0
        for r in rows:
            active = r.active_mins or 0
            brk = int(r.break_mins or 0)
            iso = int(r.isolation_mins or 0)
            status = 'present' if active >= 300 else 'half_day' if active >= 240 else 'absent'
            if status in ('present', 'half_day'):
                days_present += 1
            total_active += active
            total_break += brk
            total_iso += iso
            daily.append({
                'date': str(r.event_date),
                'first_seen_ist': r.first_seen_ist,
                'last_seen_ist': r.last_seen_ist,
                'active_minutes': active,
                'break_minutes': brk,
                'isolation_minutes': iso,
                'status': status,
            })

        # CSV download
        if request.args.get('format') == 'csv':
            import csv, io
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([f'Employee: {emp_name}'])
            writer.writerow([f'Period: {start_date} to {end_date}'])
            writer.writerow([f'Days Present: {days_present}', f'Total Active: {total_active}m',
                             f'Total Break: {total_break}m', f'Total Isolation: {total_iso}m'])
            writer.writerow([])
            writer.writerow(['Date', 'Status', 'First Seen', 'Last Seen', 'Active (min)', 'Break (min)', 'Isolation (min)'])
            for d in daily:
                writer.writerow([d['date'], d['status'], d['first_seen_ist'], d['last_seen_ist'],
                                 d['active_minutes'], d['break_minutes'], d['isolation_minutes']])
            return Response(output.getvalue(), mimetype='text/csv',
                            headers={'Content-Disposition': f'attachment; filename={emp_name.replace(" ", "_")}_{year}_{month:02d}.csv'})

        return jsonify({
            'success': True,
            'employee': {
                'employee_id': emp.employee_id,
                'name': emp_name,
                'display_name': emp.display_name or emp_name,
                'email': emp.participant_email or '',
                'status': emp.status,
                'category': emp.category,
            },
            'period': f'{start_date} to {end_date}',
            'days_present': days_present,
            'total_active_mins': total_active,
            'total_break_mins': total_break,
            'total_isolation_mins': total_iso,
            'daily': daily,
        })
    except Exception as e:
        print(f"[Employees] Detail error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/employees/<employee_id>/report/yearly', methods=['GET'])
def employee_yearly_report(employee_id):
    """Generate yearly summary for a single employee.
    Query params: year (defaults to current year)
    Returns: 12-month summary with working days, present/half/absent, attendance %,
             total hours, avg login/logout, break hours, isolation hours
    """
    try:
        ensure_team_tables_once()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # Get employee info
        emp_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("employee_id", "STRING", employee_id)]
        )
        emp_rows = list(client.query(
            f"SELECT * FROM `{dataset_ref}.employee_registry` WHERE employee_id = @employee_id",
            job_config=emp_config
        ).result())
        if not emp_rows:
            return jsonify({'success': False, 'error': 'Employee not found'}), 404

        emp = emp_rows[0]
        emp_name = emp.participant_name
        emp_email = (getattr(emp, 'participant_email', '') or '').strip().lower()

        # Get year parameter (default to current year)
        year = int(request.args.get('year', get_ist_now().year))

        # Get team holidays if employee belongs to a team
        holidays_set = set()
        if emp.team_id:
            try:
                hol_query = f"""
                SELECT holiday_date FROM `{dataset_ref}.team_holidays`
                WHERE team_id = @team_id AND EXTRACT(YEAR FROM holiday_date) = @year
                """
                hol_config = bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ScalarQueryParameter("team_id", "STRING", emp.team_id),
                    bigquery.ScalarQueryParameter("year", "INT64", year),
                ])
                hol_rows = list(client.query(hol_query, job_config=hol_config).result())
                for h in hol_rows:
                    holidays_set.add(str(h.holiday_date))
            except Exception:
                pass  # team_holidays table might not exist

        # Yearly data from presence_intervals (one source of hours), with
        # per-day rows the Python below aggregates into months. Match by
        # normalized name OR email (display-name drift safe).
        start_date = f"{year}-01-01"
        end_date = f"{year}-12-31"

        norm_emp = _sql_normalize_name('@emp_name')
        query = f"""
        SELECT
            pi.event_date,
            EXTRACT(MONTH FROM pi.event_date) AS month_num,
            CAST(CEILING(SUM(IF(pi.room_category != 'break', pi.duration_seconds, 0)) / 60.0) AS INT64) AS active_mins,
            EXTRACT(HOUR FROM TIMESTAMP_ADD(MIN(pi.start_ts), INTERVAL 330 MINUTE)) * 60
              + EXTRACT(MINUTE FROM TIMESTAMP_ADD(MIN(pi.start_ts), INTERVAL 330 MINUTE)) AS login_mins,
            EXTRACT(HOUR FROM TIMESTAMP_ADD(MAX(pi.end_ts), INTERVAL 330 MINUTE)) * 60
              + EXTRACT(MINUTE FROM TIMESTAMP_ADD(MAX(pi.end_ts), INTERVAL 330 MINUTE)) AS logout_mins,
            CAST(CEILING(SUM(IF(pi.room_category = 'break', pi.duration_seconds, 0)) / 60.0) AS INT64) AS break_mins,
            CAST(CEILING(SUM(pi.alone_seconds) / 60.0) AS INT64) AS isolation_mins
        FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}` pi
        WHERE pi.event_date BETWEEN @start_date AND @end_date
          AND (
            pi.participant_key = {norm_emp}
            OR {_sql_normalize_name('pi.participant_name')} = {norm_emp}
            OR (@emp_email != '' AND LOWER(TRIM(pi.participant_email)) = @emp_email)
          )
        GROUP BY pi.event_date
        ORDER BY pi.event_date
        """

        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
            bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
            bigquery.ScalarQueryParameter("emp_name", "STRING", emp_name),
            bigquery.ScalarQueryParameter("emp_email", "STRING", emp_email),
        ])
        rows = list(client.query(query, job_config=job_config).result())

        # Calculate working days per month (weekdays only, excluding holidays)
        from calendar import monthrange
        import datetime as dt

        month_names = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                       'July', 'August', 'September', 'October', 'November', 'December']

        # Initialize monthly stats
        monthly_stats = {}
        for m in range(1, 13):
            _, days_in_month = monthrange(year, m)
            working_days = 0
            for d in range(1, days_in_month + 1):
                date_obj = dt.date(year, m, d)
                date_str = date_obj.strftime('%Y-%m-%d')
                # Skip weekends (Mon=0, Sat=5, Sun=6)
                if date_obj.weekday() < 5 and date_str not in holidays_set:
                    working_days += 1
            monthly_stats[m] = {
                'month': m,
                'month_name': month_names[m],
                'working_days': working_days,
                'present_days': 0,
                'half_days': 0,
                'absent_days': 0,
                'total_active_mins': 0,
                'login_mins_sum': 0,
                'logout_mins_sum': 0,
                'days_with_login': 0,
                'total_break_mins': 0,
                'total_isolation_mins': 0,
            }

        # Process daily rows
        for r in rows:
            m = r.month_num
            active = r.active_mins or 0
            brk = int(r.break_mins or 0)
            iso = int(r.isolation_mins or 0)
            login_m = r.login_mins or 0
            logout_m = r.logout_mins or 0

            if active >= 300:
                monthly_stats[m]['present_days'] += 1
            elif active >= 240:
                monthly_stats[m]['half_days'] += 1

            monthly_stats[m]['total_active_mins'] += active
            monthly_stats[m]['total_break_mins'] += brk
            monthly_stats[m]['total_isolation_mins'] += iso

            if login_m > 0:
                monthly_stats[m]['login_mins_sum'] += login_m
                monthly_stats[m]['logout_mins_sum'] += logout_m
                monthly_stats[m]['days_with_login'] += 1

        # Build monthly summary
        monthly_summary = []
        yearly_totals = {
            'working_days': 0,
            'present_days': 0,
            'half_days': 0,
            'absent_days': 0,
            'total_hours': 0,
            'total_break_hours': 0,
            'total_isolation_hours': 0,
        }

        for m in range(1, 13):
            ms = monthly_stats[m]
            days_attended = ms['present_days'] + ms['half_days']
            absent = max(0, ms['working_days'] - days_attended)
            attendance_pct = round((days_attended / ms['working_days'] * 100), 1) if ms['working_days'] > 0 else 0
            total_hours = round(ms['total_active_mins'] / 60, 1)
            break_hours = round(ms['total_break_mins'] / 60, 1)
            isolation_hours = round(ms['total_isolation_mins'] / 60, 1)

            # Calculate average login/logout times
            avg_login = ''
            avg_logout = ''
            if ms['days_with_login'] > 0:
                avg_login_mins = ms['login_mins_sum'] / ms['days_with_login']
                avg_logout_mins = ms['logout_mins_sum'] / ms['days_with_login']
                avg_login = f"{int(avg_login_mins // 60):02d}:{int(avg_login_mins % 60):02d}"
                avg_logout = f"{int(avg_logout_mins // 60):02d}:{int(avg_logout_mins % 60):02d}"

            monthly_summary.append({
                'month': m,
                'month_name': ms['month_name'],
                'working_days': ms['working_days'],
                'present_days': ms['present_days'],
                'half_days': ms['half_days'],
                'absent_days': absent,
                'attendance_pct': attendance_pct,
                'total_hours': total_hours,
                'avg_login': avg_login,
                'avg_logout': avg_logout,
                'break_hours': break_hours,
                'isolation_hours': isolation_hours,
            })

            # Accumulate yearly totals
            yearly_totals['working_days'] += ms['working_days']
            yearly_totals['present_days'] += ms['present_days']
            yearly_totals['half_days'] += ms['half_days']
            yearly_totals['absent_days'] += absent
            yearly_totals['total_hours'] += total_hours
            yearly_totals['total_break_hours'] += break_hours
            yearly_totals['total_isolation_hours'] += isolation_hours

        # Calculate yearly attendance percentage
        yearly_attended = yearly_totals['present_days'] + yearly_totals['half_days']
        yearly_totals['attendance_pct'] = round(
            (yearly_attended / yearly_totals['working_days'] * 100), 1
        ) if yearly_totals['working_days'] > 0 else 0
        yearly_totals['total_hours'] = round(yearly_totals['total_hours'], 1)
        yearly_totals['total_break_hours'] = round(yearly_totals['total_break_hours'], 1)
        yearly_totals['total_isolation_hours'] = round(yearly_totals['total_isolation_hours'], 1)

        return jsonify({
            'success': True,
            'employee_id': employee_id,
            'employee_name': emp_name,
            'display_name': emp.display_name or emp_name,
            'year': year,
            'monthly_summary': monthly_summary,
            'yearly_totals': yearly_totals,
        })

    except Exception as e:
        print(f"[Employees] Yearly report error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# RUN SERVER
# ==============================================================================

# ─── SUPERADMIN DATA EDITOR ───────────────────────────────────
# Only role=superadmin can access. Direct BigQuery DML, no audit trail.

@app.route('/admin/update-role', methods=['POST'])
@require_auth(roles=['superadmin'])
def admin_update_role():
    """Update a user's role (superadmin only)."""
    try:
        data = request.get_json() or {}
        user_id = data.get('user_id')
        new_role = data.get('role', '').strip()
        if not user_id or not new_role:
            return jsonify({'success': False, 'error': 'user_id and role required'}), 400
        if new_role not in ('admin', 'hr', 'manager', 'superadmin'):
            return jsonify({'success': False, 'error': 'Invalid role'}), 400
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        q = f"UPDATE `{dataset_ref}.app_users` SET role = @role WHERE user_id = @uid"
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("role", "STRING", new_role),
            bigquery.ScalarQueryParameter("uid", "INT64", int(user_id)),
        ])
        client.query(q, job_config=job_config).result()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/snapshots', methods=['GET'])
@require_auth(roles=['superadmin'])
def admin_search_snapshots():
    """Search room_snapshots_v2 by date and optional participant name. Superadmin only."""
    try:
        date_str = request.args.get('date')
        search = request.args.get('search', '').strip()
        if not date_str:
            return jsonify({'success': False, 'error': 'date parameter required'}), 400
        report_date = validate_date_format(date_str)
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        params = [bigquery.ScalarQueryParameter("date", "STRING", report_date)]
        where_extra = ""
        if search:
            where_extra = " AND LOWER(participant_name) LIKE LOWER(@search)"
            params.append(bigquery.ScalarQueryParameter("search", "STRING", f"%{search}%"))

        query = f"""
        SELECT snapshot_id, snapshot_time, event_date, meeting_id, room_name,
               participant_name, participant_email, participant_uuid
        FROM `{dataset_ref}.room_snapshots_v2`
        WHERE event_date = @date{where_extra}
        ORDER BY participant_name, snapshot_time
        LIMIT 2000
        """
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        rows = list(client.query(query, job_config=job_config).result())

        snapshots = []
        for r in rows:
            snapshots.append({
                'snapshot_id': r.snapshot_id,
                'snapshot_time': r.snapshot_time.isoformat() if r.snapshot_time else '',
                'event_date': r.event_date,
                'meeting_id': r.meeting_id,
                'room_name': r.room_name,
                'participant_name': r.participant_name,
                'participant_email': r.participant_email or '',
                'participant_uuid': r.participant_uuid or '',
            })

        # Group by participant + room for a summary view
        summary = {}
        for s in snapshots:
            key = f"{s['participant_name']}||{s['room_name']}"
            if key not in summary:
                summary[key] = {
                    'participant_name': s['participant_name'],
                    'room_name': s['room_name'],
                    'first_seen': s['snapshot_time'],
                    'last_seen': s['snapshot_time'],
                    'snapshot_count': 0,
                    'snapshot_ids': [],
                }
            summary[key]['last_seen'] = s['snapshot_time']
            summary[key]['snapshot_count'] += 1
            summary[key]['snapshot_ids'].append(s['snapshot_id'])

        return jsonify({
            'success': True,
            'date': report_date,
            'snapshots': snapshots,
            'summary': list(summary.values()),
            'total': len(snapshots),
        })
    except Exception as e:
        print(f"[Admin] Snapshot search error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/snapshots/edit', methods=['PUT'])
@require_auth(roles=['superadmin'])
def admin_edit_snapshots():
    """Edit room_name or participant_name on snapshot rows. Superadmin only, no audit."""
    try:
        data = request.get_json() or {}
        snapshot_ids = data.get('snapshot_ids', [])
        new_room = data.get('room_name')
        new_name = data.get('participant_name')
        new_time = data.get('snapshot_time')

        if not snapshot_ids:
            return jsonify({'success': False, 'error': 'snapshot_ids required'}), 400
        if not new_room and not new_name and not new_time:
            return jsonify({'success': False, 'error': 'Provide room_name, participant_name, or snapshot_time to update'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # Build SET clause
        sets = []
        params = []
        if new_room is not None:
            sets.append("room_name = @room_name")
            params.append(bigquery.ScalarQueryParameter("room_name", "STRING", new_room))
        if new_name is not None:
            sets.append("participant_name = @part_name")
            params.append(bigquery.ScalarQueryParameter("part_name", "STRING", new_name))
        if new_time is not None:
            sets.append("snapshot_time = @snap_time")
            params.append(bigquery.ScalarQueryParameter("snap_time", "TIMESTAMP", new_time))

        # Use parameterized ARRAY to prevent SQL injection
        params.append(bigquery.ArrayQueryParameter("snapshot_ids", "STRING", snapshot_ids))
        query = f"""
        UPDATE `{dataset_ref}.room_snapshots_v2`
        SET {', '.join(sets)}
        WHERE snapshot_id IN UNNEST(@snapshot_ids)
        """
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        result = client.query(query, job_config=job_config).result()
        modified = result.num_dml_affected_rows if hasattr(result, 'num_dml_affected_rows') else len(snapshot_ids)

        print(f"[Admin] Edited {modified} snapshots: {sets}")
        return jsonify({'success': True, 'modified': modified})
    except Exception as e:
        print(f"[Admin] Snapshot edit error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/snapshots/delete', methods=['DELETE'])
@require_auth(roles=['superadmin'])
def admin_delete_snapshots():
    """Delete snapshot rows. Superadmin only, no audit."""
    try:
        data = request.get_json() or {}
        snapshot_ids = data.get('snapshot_ids', [])
        if not snapshot_ids:
            return jsonify({'success': False, 'error': 'snapshot_ids required'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        # Use parameterized ARRAY to prevent SQL injection
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("snapshot_ids", "STRING", snapshot_ids)
        ])
        query = f"DELETE FROM `{dataset_ref}.room_snapshots_v2` WHERE snapshot_id IN UNNEST(@snapshot_ids)"
        result = client.query(query, job_config=job_config).result()
        deleted = result.num_dml_affected_rows if hasattr(result, 'num_dml_affected_rows') else len(snapshot_ids)

        print(f"[Admin] Deleted {deleted} snapshots")
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        print(f"[Admin] Snapshot delete error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/snapshots/add', methods=['POST'])
@require_auth(roles=['superadmin'])
def admin_add_snapshot():
    """Insert new snapshot rows. Superadmin only."""
    try:
        data = request.get_json() or {}
        rows_to_add = data.get('rows', [])
        if not rows_to_add:
            return jsonify({'success': False, 'error': 'rows array required'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        import uuid as _uuid
        bq_rows = []
        for r in rows_to_add:
            bq_rows.append({
                'snapshot_id': str(_uuid.uuid4()),
                # get_ist_now(): `IST` tzinfo never existed — datetime.now(IST)
                # raised NameError on every call (route was broken since birth)
                'snapshot_time': r.get('snapshot_time', get_ist_now().isoformat()),
                'event_date': r.get('event_date', get_ist_now().strftime('%Y-%m-%d')),
                'meeting_id': r.get('meeting_id', ''),
                'room_name': r.get('room_name', 'Main Meeting'),
                'participant_name': r.get('participant_name', ''),
                'participant_email': r.get('participant_email', ''),
                'participant_uuid': r.get('participant_uuid', ''),
                'inserted_at': get_ist_now().isoformat(),
            })

        errors = client.insert_rows_json(f"{dataset_ref}.room_snapshots_v2", bq_rows)
        if errors:
            return jsonify({'success': False, 'error': str(errors)}), 500

        print(f"[Admin] Added {len(bq_rows)} snapshots")
        return jsonify({'success': True, 'added': len(bq_rows)})
    except Exception as e:
        print(f"[Admin] Snapshot add error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/events', methods=['GET'])
@require_auth(roles=['superadmin'])
def admin_search_events():
    """Search participant_events by date and optional participant name. Superadmin only."""
    try:
        date_str = request.args.get('date')
        search = request.args.get('search', '').strip()
        if not date_str:
            return jsonify({'success': False, 'error': 'date parameter required'}), 400
        report_date = validate_date_format(date_str)
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        params = [bigquery.ScalarQueryParameter("date", "STRING", report_date)]
        where_extra = ""
        if search:
            where_extra = " AND LOWER(participant_name) LIKE LOWER(@search)"
            params.append(bigquery.ScalarQueryParameter("search", "STRING", f"%{search}%"))

        query = f"""
        SELECT event_id, event_type, event_timestamp, event_date, meeting_id, meeting_uuid,
               participant_id, participant_name, participant_email, room_uuid, room_name
        FROM `{dataset_ref}.{BQ_EVENTS_TABLE}`
        WHERE event_date = @date{where_extra}
        ORDER BY participant_name, event_timestamp
        LIMIT 2000
        """
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        rows = list(client.query(query, job_config=job_config).result())

        events = []
        for r in rows:
            events.append({
                'event_id': r.event_id,
                'event_type': r.event_type,
                'event_timestamp': r.event_timestamp.isoformat() if r.event_timestamp else '',
                'event_date': r.event_date,
                'meeting_id': r.meeting_id or '',
                'participant_name': r.participant_name or '',
                'participant_email': r.participant_email or '',
                'room_name': r.room_name or '',
            })

        return jsonify({'success': True, 'date': report_date, 'events': events, 'total': len(events)})
    except Exception as e:
        print(f"[Admin] Event search error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/events/edit', methods=['PUT'])
@require_auth(roles=['superadmin'])
def admin_edit_events():
    """Edit participant_events rows. Superadmin only, no audit."""
    try:
        data = request.get_json() or {}
        event_ids = data.get('event_ids', [])
        new_room = data.get('room_name')
        new_name = data.get('participant_name')
        new_time = data.get('event_timestamp')
        new_type = data.get('event_type')

        if not event_ids:
            return jsonify({'success': False, 'error': 'event_ids required'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        sets = []
        params = []
        if new_room is not None:
            sets.append("room_name = @room_name")
            params.append(bigquery.ScalarQueryParameter("room_name", "STRING", new_room))
        if new_name is not None:
            sets.append("participant_name = @part_name")
            params.append(bigquery.ScalarQueryParameter("part_name", "STRING", new_name))
        if new_time is not None:
            sets.append("event_timestamp = @evt_time")
            params.append(bigquery.ScalarQueryParameter("evt_time", "TIMESTAMP", new_time))
        if new_type is not None:
            sets.append("event_type = @evt_type")
            params.append(bigquery.ScalarQueryParameter("evt_type", "STRING", new_type))

        if not sets:
            return jsonify({'success': False, 'error': 'No fields to update'}), 400

        id_list = ", ".join([f"'{eid}'" for eid in event_ids])
        query = f"""
        UPDATE `{dataset_ref}.{BQ_EVENTS_TABLE}`
        SET {', '.join(sets)}
        WHERE event_id IN ({id_list})
        """
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        result = client.query(query, job_config=job_config).result()
        modified = result.num_dml_affected_rows if hasattr(result, 'num_dml_affected_rows') else len(event_ids)

        print(f"[Admin] Edited {modified} events: {sets}")
        return jsonify({'success': True, 'modified': modified})
    except Exception as e:
        print(f"[Admin] Event edit error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/events/delete', methods=['DELETE'])
@require_auth(roles=['superadmin'])
def admin_delete_events():
    """Delete participant_events rows. Superadmin only, no audit."""
    try:
        data = request.get_json() or {}
        event_ids = data.get('event_ids', [])
        if not event_ids:
            return jsonify({'success': False, 'error': 'event_ids required'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        id_list = ", ".join([f"'{eid}'" for eid in event_ids])
        query = f"DELETE FROM `{dataset_ref}.{BQ_EVENTS_TABLE}` WHERE event_id IN ({id_list})"
        result = client.query(query).result()
        deleted = result.num_dml_affected_rows if hasattr(result, 'num_dml_affected_rows') else len(event_ids)

        print(f"[Admin] Deleted {deleted} events")
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        print(f"[Admin] Event delete error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# WEBHOOK-ONLY REPORT (TEST ENDPOINT)
# ==============================================================================
# This endpoint generates a simple attendance report using ONLY webhook data
# from participant_events table. No SDK polling data, no room names.
# Used to test the low-cost webhook-only approach before migration.
# ==============================================================================

@app.route('/report/webhook-preview/<date_str>', methods=['GET'])
def webhook_only_report_preview(date_str):
    """
    Generate a simple attendance report using ONLY webhook data.
    Returns: Name, Email, First Join (IST), Last Leave (IST), Total Active Duration

    IMPORTANT: A real break ONLY occurs when participant_left → participant_joined with gap > 5 min.
    """
    try:
        # Validate date format
        try:
            datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        query = f"""
        WITH all_events AS (
            SELECT
                participant_name,
                COALESCE(NULLIF(participant_email, ''), '') as participant_email,
                event_type,
                TIMESTAMP_ADD(CAST(event_timestamp AS TIMESTAMP), INTERVAL 330 MINUTE) as event_time_ist
            FROM `{dataset_ref}.{BQ_EVENTS_TABLE}`
            WHERE event_date = @report_date
              AND participant_name IS NOT NULL
              AND participant_name != ''
              AND LOWER(participant_name) NOT LIKE '%scout%'
        ),
        events_with_prev AS (
            SELECT
                participant_name,
                participant_email,
                event_type,
                event_time_ist,
                LAG(event_time_ist) OVER (PARTITION BY participant_name ORDER BY event_time_ist) as prev_event_time
            FROM all_events
        ),
        events_with_session AS (
            SELECT
                participant_name,
                participant_email,
                event_type,
                event_time_ist,
                SUM(CASE
                    WHEN prev_event_time IS NULL THEN 1
                    WHEN event_type IN ('participant_joined', 'meeting.participant_joined')
                         AND TIMESTAMP_DIFF(event_time_ist, prev_event_time, MINUTE) > 5 THEN 1
                    ELSE 0
                END) OVER (PARTITION BY participant_name ORDER BY event_time_ist) as session_id
            FROM events_with_prev
        ),
        session_times AS (
            SELECT
                participant_name,
                MAX(participant_email) as participant_email,
                session_id,
                MIN(event_time_ist) as session_start,
                MAX(event_time_ist) as session_end
            FROM events_with_session
            GROUP BY participant_name, session_id
        ),
        current_time_ist AS (
            SELECT TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 330 MINUTE) as now_ist
        ),
        sessions AS (
            SELECT
                st.participant_name,
                st.participant_email,
                st.session_start,
                CASE
                    WHEN TIMESTAMP_DIFF(ct.now_ist, st.session_end, MINUTE) < 90
                    THEN ct.now_ist
                    ELSE st.session_end
                END as session_end,
                CASE
                    WHEN TIMESTAMP_DIFF(ct.now_ist, st.session_end, MINUTE) < 90
                    THEN TIMESTAMP_DIFF(ct.now_ist, st.session_start, MINUTE)
                    ELSE TIMESTAMP_DIFF(st.session_end, st.session_start, MINUTE)
                END as session_minutes
            FROM session_times st
            CROSS JOIN current_time_ist ct
            WHERE st.session_start IS NOT NULL AND st.session_end IS NOT NULL
        ),
        participant_summary AS (
            SELECT
                participant_name,
                MAX(participant_email) as participant_email,
                MIN(session_start) as first_join_ist,
                MAX(session_end) as last_leave_ist,
                SUM(session_minutes) as total_active_minutes
            FROM sessions
            GROUP BY participant_name
        )
        SELECT
            participant_name as name,
            participant_email as email,
            FORMAT_TIMESTAMP('%H:%M', first_join_ist) as join_time_ist,
            FORMAT_TIMESTAMP('%H:%M', last_leave_ist) as leave_time_ist,
            COALESCE(total_active_minutes, 0) as duration_minutes,
            CASE
                WHEN total_active_minutes IS NOT NULL AND total_active_minutes > 0
                THEN CONCAT(
                    CAST(FLOOR(total_active_minutes / 60) AS STRING), 'h ',
                    CAST(MOD(total_active_minutes, 60) AS STRING), 'm'
                )
                ELSE '0h 0m'
            END as duration_formatted
        FROM participant_summary
        WHERE first_join_ist IS NOT NULL
        ORDER BY participant_name
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("report_date", "STRING", date_str)
            ]
        )

        rows = list(client.query(query, job_config=job_config).result())

        participants = []
        for row in rows:
            participants.append({
                'name': row.name,
                'email': row.email or '',
                'join_time_ist': row.join_time_ist or '',
                'leave_time_ist': row.leave_time_ist or '',
                'duration_minutes': row.duration_minutes or 0,
                'duration_formatted': row.duration_formatted or '0h 0m'
            })

        # Generate CSV content
        csv_lines = ['Name,Email,Join_Time_IST,Leave_Time_IST,Duration_Minutes,Duration']
        for p in participants:
            csv_lines.append(f'"{p["name"]}","{p["email"]}",{p["join_time_ist"]},{p["leave_time_ist"]},{p["duration_minutes"]},{p["duration_formatted"]}')

        csv_content = '\r\n'.join(csv_lines)

        return jsonify({
            'success': True,
            'date': date_str,
            'source': 'webhook_only (participant_events table)',
            'note': 'TEST ENDPOINT - No room names, no break tracking. Simple join/leave times only.',
            'participant_count': len(participants),
            'participants': participants,
            'csv_content': csv_content
        })

    except Exception as e:
        print(f"[Webhook Report] Error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/report/webhook-csv/<date_str>', methods=['GET'])
def webhook_only_report_csv(date_str):
    """
    Download webhook-only attendance report as CSV file.
    Can be directly imported into Google Sheets.

    IMPORTANT: A real break ONLY occurs when participant_left → participant_joined with gap > 5 min.
    """
    try:
        # Validate date format
        try:
            datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return "Invalid date format. Use YYYY-MM-DD", 400

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        query = f"""
        WITH all_events AS (
            SELECT
                participant_name,
                COALESCE(NULLIF(participant_email, ''), '') as participant_email,
                event_type,
                TIMESTAMP_ADD(CAST(event_timestamp AS TIMESTAMP), INTERVAL 330 MINUTE) as event_time_ist
            FROM `{dataset_ref}.{BQ_EVENTS_TABLE}`
            WHERE event_date = @report_date
              AND participant_name IS NOT NULL
              AND participant_name != ''
              AND LOWER(participant_name) NOT LIKE '%scout%'
        ),
        events_with_prev AS (
            SELECT
                participant_name,
                participant_email,
                event_type,
                event_time_ist,
                LAG(event_time_ist) OVER (PARTITION BY participant_name ORDER BY event_time_ist) as prev_event_time
            FROM all_events
        ),
        events_with_session AS (
            SELECT
                participant_name,
                participant_email,
                event_type,
                event_time_ist,
                SUM(CASE
                    WHEN prev_event_time IS NULL THEN 1
                    WHEN event_type IN ('participant_joined', 'meeting.participant_joined')
                         AND TIMESTAMP_DIFF(event_time_ist, prev_event_time, MINUTE) > 5 THEN 1
                    ELSE 0
                END) OVER (PARTITION BY participant_name ORDER BY event_time_ist) as session_id
            FROM events_with_prev
        ),
        session_times AS (
            SELECT
                participant_name,
                MAX(participant_email) as participant_email,
                session_id,
                MIN(event_time_ist) as session_start,
                MAX(event_time_ist) as session_end
            FROM events_with_session
            GROUP BY participant_name, session_id
        ),
        current_time_ist AS (
            SELECT TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 330 MINUTE) as now_ist
        ),
        sessions AS (
            SELECT
                st.participant_name,
                st.participant_email,
                st.session_start,
                CASE
                    WHEN TIMESTAMP_DIFF(ct.now_ist, st.session_end, MINUTE) < 90
                    THEN ct.now_ist
                    ELSE st.session_end
                END as session_end,
                CASE
                    WHEN TIMESTAMP_DIFF(ct.now_ist, st.session_end, MINUTE) < 90
                    THEN TIMESTAMP_DIFF(ct.now_ist, st.session_start, MINUTE)
                    ELSE TIMESTAMP_DIFF(st.session_end, st.session_start, MINUTE)
                END as session_minutes
            FROM session_times st
            CROSS JOIN current_time_ist ct
            WHERE st.session_start IS NOT NULL AND st.session_end IS NOT NULL
        ),
        participant_summary AS (
            SELECT
                participant_name,
                MAX(participant_email) as participant_email,
                MIN(session_start) as first_join_ist,
                MAX(session_end) as last_leave_ist,
                SUM(session_minutes) as total_active_minutes
            FROM sessions
            GROUP BY participant_name
        )
        SELECT
            participant_name as name,
            participant_email as email,
            FORMAT_TIMESTAMP('%H:%M', first_join_ist) as join_time_ist,
            FORMAT_TIMESTAMP('%H:%M', last_leave_ist) as leave_time_ist,
            COALESCE(total_active_minutes, 0) as duration_minutes,
            CASE
                WHEN total_active_minutes IS NOT NULL AND total_active_minutes > 0
                THEN CONCAT(
                    CAST(FLOOR(total_active_minutes / 60) AS STRING), 'h ',
                    CAST(MOD(total_active_minutes, 60) AS STRING), 'm'
                )
                ELSE '0h 0m'
            END as duration_formatted
        FROM participant_summary
        WHERE first_join_ist IS NOT NULL
        ORDER BY participant_name
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("report_date", "STRING", date_str)
            ]
        )

        rows = list(client.query(query, job_config=job_config).result())

        # Generate CSV content
        csv_lines = ['Name,Email,Join_Time_IST,Leave_Time_IST,Duration_Minutes,Duration']
        for row in rows:
            name = (row.name or '').replace('"', '""')
            email = (row.email or '').replace('"', '""')
            csv_lines.append(f'"{name}","{email}",{row.join_time_ist or ""},{row.leave_time_ist or ""},{row.duration_minutes or 0},{row.duration_formatted or "0h 0m"}')

        csv_content = '\r\n'.join(csv_lines)

        # Return as downloadable CSV file
        response = make_response(csv_content)
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        response.headers['Content-Disposition'] = f'attachment; filename=attendance_webhook_{date_str}.csv'
        return response

    except Exception as e:
        print(f"[Webhook CSV] Error: {e}")
        traceback.print_exc()
        return f"Error: {str(e)}", 500


# ==============================================================================
# GOOGLE SHEETS INTEGRATION - Auto-updating Master Attendance Sheet
# ==============================================================================
# Updates a Google Sheet with attendance data hourly during work hours (9 AM - 12 AM)
# Sheet format: Date | Team | Name | Email | Join Time | Leave Time | Duration
# ==============================================================================

GOOGLE_SHEETS_ID = '1Ljx1mWYv15McWzuMULIzi9mi0exkrvN_YRnVhjUNH8E'
GOOGLE_SHEETS_SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

def get_sheets_service():
    """Get Google Sheets API service using default credentials (Cloud Run service account)"""
    try:
        credentials, project = google.auth.default(scopes=GOOGLE_SHEETS_SCOPES)
        service = build('sheets', 'v4', credentials=credentials, cache_discovery=False)
        return service
    except Exception as e:
        print(f"[Sheets] Error getting service: {e}")
        traceback.print_exc()
        return None


def get_attendance_for_sheet(date_str, start_hour=9, end_hour=24):
    """
    Get attendance data for Google Sheets.
    Includes ALL events from the date (no hour filtering - meeting can run past midnight).

    Key fixes:
    1. No hour filter - captures late night workers
    2. Uses current time for participants still in meeting (last event < 30 min ago)
    3. New session only when participant_joined after 5+ min gap
    """
    try:
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        query = f"""
        WITH all_events AS (
            SELECT
                pe.participant_name,
                COALESCE(NULLIF(pe.participant_email, ''), '') as participant_email,
                pe.event_type,
                TIMESTAMP_ADD(CAST(pe.event_timestamp AS TIMESTAMP), INTERVAL 330 MINUTE) as event_time_ist
            FROM `{dataset_ref}.{BQ_EVENTS_TABLE}` pe
            WHERE pe.event_date = @report_date
              AND pe.participant_name IS NOT NULL
              AND pe.participant_name != ''
              AND LOWER(pe.participant_name) NOT LIKE '%scout%'
              -- Filter hour >= 9 to exclude previous day's late-night events (00:00-08:59)
              AND EXTRACT(HOUR FROM TIMESTAMP_ADD(CAST(pe.event_timestamp AS TIMESTAMP), INTERVAL 330 MINUTE)) >= 9
        ),
        events_with_prev AS (
            SELECT
                participant_name,
                participant_email,
                event_type,
                event_time_ist,
                LAG(event_time_ist) OVER (PARTITION BY participant_name ORDER BY event_time_ist) as prev_event_time
            FROM all_events
        ),
        -- New session when participant_joined with gap > 5 min from any previous event
        events_with_session AS (
            SELECT
                participant_name,
                participant_email,
                event_type,
                event_time_ist,
                SUM(CASE
                    WHEN prev_event_time IS NULL THEN 1
                    WHEN event_type IN ('participant_joined', 'meeting.participant_joined')
                         AND TIMESTAMP_DIFF(event_time_ist, prev_event_time, MINUTE) > 5 THEN 1
                    ELSE 0
                END) OVER (PARTITION BY participant_name ORDER BY event_time_ist) as session_id
            FROM events_with_prev
        ),
        session_times AS (
            SELECT
                participant_name,
                MAX(participant_email) as participant_email,
                session_id,
                MIN(event_time_ist) as session_start,
                MAX(event_time_ist) as session_end
            FROM events_with_session
            GROUP BY participant_name, session_id
        ),
        -- Current time in IST for active participant detection
        current_time_ist AS (
            SELECT TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 330 MINUTE) as now_ist
        ),
        sessions AS (
            SELECT
                st.participant_name,
                st.participant_email,
                st.session_start,
                -- If last event < 30 min ago, use current time (still in meeting)
                CASE
                    WHEN TIMESTAMP_DIFF(ct.now_ist, st.session_end, MINUTE) < 90
                    THEN ct.now_ist
                    ELSE st.session_end
                END as session_end,
                CASE
                    WHEN TIMESTAMP_DIFF(ct.now_ist, st.session_end, MINUTE) < 90
                    THEN TIMESTAMP_DIFF(ct.now_ist, st.session_start, MINUTE)
                    ELSE TIMESTAMP_DIFF(st.session_end, st.session_start, MINUTE)
                END as session_minutes
            FROM session_times st
            CROSS JOIN current_time_ist ct
            WHERE st.session_start IS NOT NULL AND st.session_end IS NOT NULL
        ),
        -- Aggregate per participant: first join, last leave, total ACTIVE minutes
        participant_summary AS (
            SELECT
                participant_name,
                MAX(participant_email) as participant_email,
                MIN(session_start) as first_join_ist,
                MAX(session_end) as last_leave_ist,
                SUM(session_minutes) as total_active_minutes
            FROM sessions
            GROUP BY participant_name
        ),
        team_lookup AS (
            SELECT
                tm.participant_name,
                tm.participant_email as team_email,
                t.team_name
            FROM `{dataset_ref}.team_members` tm
            JOIN `{dataset_ref}.teams` t ON tm.team_id = t.team_id
        )
        SELECT
            @report_date as report_date,
            COALESCE(tl.team_name, 'Unassigned') as team_name,
            ps.participant_name as name,
            ps.participant_email as email,
            FORMAT_TIMESTAMP('%H:%M', ps.first_join_ist) as join_time_ist,
            FORMAT_TIMESTAMP('%H:%M', ps.last_leave_ist) as leave_time_ist,
            COALESCE(ps.total_active_minutes, 0) as duration_minutes,
            CASE
                WHEN ps.total_active_minutes IS NOT NULL AND ps.total_active_minutes > 0
                THEN CONCAT(
                    CAST(FLOOR(ps.total_active_minutes / 60) AS STRING), 'h ',
                    CAST(MOD(ps.total_active_minutes, 60) AS STRING), 'm'
                )
                ELSE '0h 0m'
            END as duration_formatted
        FROM participant_summary ps
        LEFT JOIN team_lookup tl ON (
            LOWER(TRIM(ps.participant_name)) = LOWER(TRIM(tl.participant_name))
            OR (ps.participant_email != '' AND LOWER(TRIM(ps.participant_email)) = LOWER(TRIM(tl.team_email)))
        )
        WHERE ps.first_join_ist IS NOT NULL
        ORDER BY tl.team_name, ps.participant_name
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("report_date", "STRING", date_str)
            ]
        )

        rows = list(client.query(query, job_config=job_config).result())
        return rows

    except Exception as e:
        print(f"[Sheets] Error getting attendance: {e}")
        traceback.print_exc()
        return []


def get_or_create_date_sheet(service, spreadsheet_id, date_str):
    """
    Get or create a sheet tab for a specific date.
    Sheet name format: DD-MM-YY (e.g., "21-05-26" for 2026-05-21)
    Returns the sheet name.
    """
    # Convert YYYY-MM-DD to DD-MM-YY
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        sheet_name = dt.strftime('%d-%m-%y')
    except:
        sheet_name = date_str.replace('-', '')[-6:]  # Fallback

    # Get existing sheets
    spreadsheet = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing_sheets = [s['properties']['title'] for s in spreadsheet.get('sheets', [])]

    # Create sheet if it doesn't exist
    if sheet_name not in existing_sheets:
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    'requests': [{
                        'addSheet': {
                            'properties': {
                                'title': sheet_name,
                                'index': 0  # Add at the beginning
                            }
                        }
                    }]
                }
            ).execute()
            print(f"[Sheets] Created new sheet tab: {sheet_name}")
        except Exception as e:
            print(f"[Sheets] Error creating sheet: {e}")

    return sheet_name


@app.route('/sheets/update', methods=['POST'])
@require_auth()
def update_google_sheet():
    """
    Update Google Sheet with attendance data.
    Creates a separate sheet tab for each date (format: DD-MM-YY).
    Called hourly by Cloud Scheduler during work hours (9 AM - 12 AM IST).

    Query params:
    - date: Date to report (default: today)
    - mode: 'live' (9 AM to current time) or 'final' (9 AM to 11:59 PM)
    """
    try:
        data = request.get_json() or {}
        date_str = data.get('date', get_ist_date())
        mode = data.get('mode', 'live')

        # Determine time range
        if mode == 'final':
            start_hour = 9
            end_hour = 24  # Up to midnight
        else:
            start_hour = 9
            current_hour = get_ist_now().hour
            end_hour = min(current_hour + 1, 24)  # Up to current hour

        print(f"[Sheets] Updating sheet for {date_str}, mode={mode}, hours={start_hour}-{end_hour}")

        # Get attendance data
        rows = get_attendance_for_sheet(date_str, start_hour, end_hour)

        # Get Sheets service
        service = get_sheets_service()
        if not service:
            return jsonify({'success': False, 'error': 'Could not connect to Google Sheets'}), 500

        # Get or create sheet tab for this date
        sheet_name = get_or_create_date_sheet(service, GOOGLE_SHEETS_ID, date_str)

        if not rows:
            # Still create the sheet with headers even if no data
            service.spreadsheets().values().update(
                spreadsheetId=GOOGLE_SHEETS_ID,
                range=f"'{sheet_name}'!A1:F1",
                valueInputOption='RAW',
                body={'values': [['Team', 'Name', 'Email', 'Join Time', 'Leave Time', 'Duration']]}
            ).execute()
            return jsonify({
                'success': True,
                'message': f'No attendance data found. Created empty sheet: {sheet_name}',
                'date': date_str,
                'sheet_name': sheet_name,
                'mode': mode,
                'rows_updated': 0
            })

        sheet = service.spreadsheets()

        # Clear the sheet and add headers
        try:
            sheet.values().clear(
                spreadsheetId=GOOGLE_SHEETS_ID,
                range=f"'{sheet_name}'!A:F"
            ).execute()
        except Exception as e:
            print(f"[Sheets] Error clearing sheet: {e}")

        # Prepare data with headers (no Date column since sheet name is the date)
        all_rows = [['Team', 'Name', 'Email', 'Join Time', 'Leave Time', 'Duration']]
        for row in rows:
            all_rows.append([
                row.team_name,
                row.name,
                row.email or '',
                row.join_time_ist or '',
                row.leave_time_ist or '',
                row.duration_formatted or '0h 0m'
            ])

        # Write all data
        sheet.values().update(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range=f"'{sheet_name}'!A1",
            valueInputOption='RAW',
            body={'values': all_rows}
        ).execute()

        print(f"[Sheets] Updated {len(rows)} rows in sheet '{sheet_name}'")

        return jsonify({
            'success': True,
            'message': f'Updated sheet "{sheet_name}" with {len(rows)} attendance records',
            'date': date_str,
            'sheet_name': sheet_name,
            'mode': mode,
            'time_range': f'{start_hour}:00 - {end_hour}:00 IST',
            'rows_updated': len(rows),
            'sheet_url': f'https://docs.google.com/spreadsheets/d/{GOOGLE_SHEETS_ID}'
        })

    except Exception as e:
        print(f"[Sheets] Update error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/sheets/status', methods=['GET'])
def google_sheet_status():
    """Check Google Sheets connection and return sheet info"""
    try:
        service = get_sheets_service()
        if not service:
            return jsonify({'success': False, 'error': 'Could not connect to Google Sheets'}), 500

        sheet = service.spreadsheets()

        # Get sheet metadata
        metadata = sheet.get(spreadsheetId=GOOGLE_SHEETS_ID).execute()
        title = metadata.get('properties', {}).get('title', 'Unknown')

        # Get row count
        result = sheet.values().get(
            spreadsheetId=GOOGLE_SHEETS_ID,
            range='A:A'
        ).execute()
        row_count = len(result.get('values', []))

        return jsonify({
            'success': True,
            'connected': True,
            'sheet_id': GOOGLE_SHEETS_ID,
            'sheet_title': title,
            'row_count': row_count,
            'sheet_url': f'https://docs.google.com/spreadsheets/d/{GOOGLE_SHEETS_ID}'
        })

    except Exception as e:
        print(f"[Sheets] Status error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# PRESENCE INTERVALS - v2 report rebuild (Phase 1)
# ==============================================================================
# Single source of truth for duration aggregation. Built once per IST date from
# room_snapshots_v2 + participant_events, then all team/attendance reports become
# trivial GROUP BY SUM queries against this table.
#
# Categories per row: 'main' | 'breakout' | 'break' (exclusive — every minute
# belongs to exactly one). Isolation is tracked as alone_seconds per interval,
# NOT as a separate category — Total = main + breakout + break with no double
# counting.
#
# Sources: 'snapshot' (real SDK data), 'synthesized_main' (Main Room time
# inferred between/around breakouts), 'webhook_fill' (when SDK missed but
# Zoom webhooks confirmed presence).
# ==============================================================================

from zt_intervals import *  # noqa: F401,F403 — split from app.py, see zt_intervals.py
# When a participant's LEAVE webhook never arrived (laptop died, Zoom dropped
# the event) we still credit them (join -> monitoring end) so a whole day isn't
# lost — but capped tighter than a normal fill, because "no leave event" could
# also mean they left and we never heard. 4h default = at most a half-day of
# benefit-of-the-doubt, never a 10h phantom.
# Page-load auto-build kill switch (PAGELOAD_AUTO_BUILD, zt_intervals.py).
# DEFAULT 'false' since 2026-08-05: every view is a pure reader and never
# triggers a build, so a paused Cloud Scheduler stays paused instead of being
# revived by whoever opens the dashboard. Builds run only when something calls
# /intervals/rebuild, /intervals/auto-build, or /intervals/backfill. Set the
# env var to 'true' to restore the legacy lazy-build-on-view behavior.
# --- Inherited-meeting (overnight-spillover) guard ------------------------
# Zoom meetings can run 24h+. When one is left running across IST midnight its
# tail lands on the NEXT day's partition starting at ~00:00, with the whole
# cohort "frozen" in their rooms (flat occupancy, no joins/leaves) until the
# meeting finally ends in a single mass-exit; the real meeting for the day then
# starts afterwards as people actually join (occupancy ramps up from ~1). We
# detect that mass-exit boundary from global per-bucket occupancy and drop
# every interval that STARTS before it. This implements "count from the meeting
# that starts today; skip the post-midnight tail of yesterday's meeting." On a
# normal day nobody is present at 00:00, so nothing is dropped. Tunable via env.


# In-process guard so "always rebuild today" endpoints don't hammer BigQuery:
# a Dashboard load fires one attendance_v2 call PER TEAM, and each used to run
# a full build_presence_intervals (5+ queries + a load job). One rebuild per
# TODAY_REBUILD_MIN_INTERVAL_S per process keeps every view fresh to within
# ~3 minutes while cutting BQ query volume by the number of teams.
# On build failure, retry sooner than the full interval but NOT immediately:
# during a BQ incident every request would otherwise re-run the full 5-query
# build, amplifying the outage.




def _whole_minutes_from_seconds(seconds):
    """Convert one interval's duration to whole minutes, rounding UP.

    ZOOM-REPORT PARITY (2026-07-22, user decision): Zoom's official
    attendance report bills every session as full minutes rounded UP
    (a 20m10s session = 21 min). Our reports must reconcile with that
    document, so each interval bills the same way, and totals are the
    SUM of billed intervals — never round the total itself. SQL twin:
    _sql_billed_seconds in zt_intervals.py."""
    secs = int(seconds or 0)
    return -(-secs // 60)  # ceiling division












@app.route('/intervals/rebuild', methods=['POST'])
def intervals_rebuild():
    """Build (or rebuild) presence_intervals for one IST date.

    Body options (pick one):
      {"date":     "YYYY-MM-DD"}   explicit date
      {"days_ago": N}              N days before today IST (1 = yesterday)
      {}                           defaults to today IST

    The days_ago form lets Cloud Scheduler post a static body every night
    to rebuild yesterday (since Scheduler can't compute dynamic dates).
    """
    try:
        data = request.get_json(silent=True) or {}
        if 'days_ago' in data:
            days_ago = int(data['days_ago'])
            target = datetime.strptime(get_ist_date(), '%Y-%m-%d').date() - timedelta(days=days_ago)
            date_str = target.isoformat()
        else:
            date_str = validate_date_format(data.get('date'))
        # Today goes through the process-wide guard so the 2-min scheduler and
        # dashboard page-load rebuilds can't run two builds concurrently
        # (overlapping DELETE+INSERT duplicated every interval, 2026-07-21).
        if date_str == get_ist_date():
            ran = _rebuild_today_guarded(date_str)
            return jsonify({'success': True, 'date': date_str, 'built': ran,
                            'skipped': not ran,
                            'reason': None if ran else 'built recently (guard)'})
        result = build_presence_intervals(date_str)
        return jsonify({'success': True, **result})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        print(f"[Intervals] Build error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/intervals/build-sql', methods=['GET', 'POST'])
def intervals_build_sql():
    """WEBHOOK-PRIMARY: build intervals for one IST date ENTIRELY in BigQuery
    (JS-UDF state machine, no Python processing), writing to the SEPARATE
    presence_intervals_sql table, then compare per-participant daily totals
    against the production presence_intervals table.

    Params (query string or JSON body):
      date=YYYY-MM-DD   explicit date (default: today IST)
      days_ago=N        N days before today IST
      compare=false     skip the comparison step

    Production reports are NOT affected — this is a side-by-side verifier.
    Once webhook-only rows match the Python builder for a few days, the
    Python builder can be retired in favor of this query.
    """
    try:
        data = request.get_json(silent=True) or {}
        args = request.args
        days_ago = data.get('days_ago', args.get('days_ago'))
        date_in = data.get('date', args.get('date'))
        if days_ago is not None:
            target = (datetime.strptime(get_ist_date(), '%Y-%m-%d').date()
                      - timedelta(days=int(days_ago)))
            date_str = target.isoformat()
        else:
            date_str = date_in or get_ist_date()

        result = build_presence_intervals_sql(date_str)

        do_compare = str(data.get('compare', args.get('compare', 'true'))).lower() != 'false'
        comparison = compare_sql_vs_python(date_str) if do_compare else None

        return jsonify({'success': True, 'build': result, 'comparison': comparison})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        print(f"[Intervals] SQL build error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/intervals/auto-build', methods=['POST'])
def intervals_auto_build():
    """Scheduler-owned self-healing sweep: build any date in the window that
    is missing, stale, or whose source data arrived after its last build.
    Same logic pages used to trigger lazily — now runnable in the background
    so pages can be pure readers (set PAGELOAD_AUTO_BUILD=false).

    Body: {"days_back": N (default 35), "max_builds": M (default 15)}
    """
    try:
        data = request.get_json(silent=True) or {}
        days_back = min(int(data.get('days_back', 35)), 370)
        max_builds = min(int(data.get('max_builds', 15)), 50)
        end_d = get_ist_date()
        start_d = (datetime.strptime(end_d, '%Y-%m-%d').date()
                   - timedelta(days=days_back)).isoformat()
        built, still_missing = _auto_build_dates_in_range(
            start_d, end_d, max_builds=max_builds, force=True)
        return jsonify({'success': True, 'start': start_d, 'end': end_d,
                        'built': built, 'still_missing': still_missing})
    except Exception as e:
        print(f"[AutoBuildSweep] error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/reconcile/zoom', methods=['GET', 'POST'])
def reconcile_zoom():
    """Nightly safety net: compare our computed hours (presence_intervals)
    against Zoom's own Reports API per participant, flag big mismatches.

    Zoom's per-participant duration is authoritative for TOTAL in-meeting
    time (main + breakouts combined), so any bug in our pipeline — missed
    webhooks, bad synthesis, identity glitches — shows up here as a delta.

    GET query / POST body params:
      date=YYYY-MM-DD  or  days_ago=N   (default: yesterday IST)
      threshold_pct    flag if |delta| > this % of the larger value (default 10)
      min_minutes      ignore people under this on both sides (default 15)
    """
    try:
        data = request.get_json(silent=True) or {}
        args = {**request.args.to_dict(), **data}
        if args.get('date'):
            date_str = validate_date_format(args['date'])
        else:
            days_ago = int(args.get('days_ago', 1))
            date_str = (datetime.strptime(get_ist_date(), '%Y-%m-%d').date()
                        - timedelta(days=days_ago)).isoformat()
        threshold_pct = float(args.get('threshold_pct', 10))
        min_minutes = float(args.get('min_minutes', 15))

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # --- Zoom's numbers: every meeting seen in webhooks that day --------
        mtg_q = f"""
        SELECT DISTINCT meeting_uuid, meeting_id
        FROM `{dataset_ref}.{BQ_EVENTS_TABLE}`
        WHERE event_date = @d AND meeting_uuid IS NOT NULL AND meeting_uuid != ''
        LIMIT 5
        """
        meetings = list(client.query(mtg_q, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", date_str)]
        )).result())
        if not meetings:
            return jsonify({'success': False, 'date': date_str,
                            'error': 'No meetings found in webhook events for this date'}), 404

        zoom_secs = {}    # participant_key -> total seconds per Zoom
        zoom_names = {}   # participant_key -> display name
        zoom_emails = {}  # lower(email) -> participant_key
        for m in meetings:
            participants = zoom_api.get_past_meeting_participants(m.meeting_uuid) or []
            if not participants and m.meeting_id:
                participants = zoom_api.get_past_meeting_participants(str(m.meeting_id)) or []
            for p in participants:
                pname = safe_str(p.get('name') or p.get('user_name'), default='')
                pemail = safe_str(p.get('user_email') or p.get('email'), default='').strip().lower()
                if not pname or is_scout_bot(pname, pemail):
                    continue
                key = normalize_participant_name(pname).strip().lower()
                zoom_secs[key] = zoom_secs.get(key, 0) + safe_int(p.get('duration', 0))
                zoom_names.setdefault(key, pname)
                if pemail:
                    zoom_emails[pemail] = key

        # --- Our numbers: presence_intervals totals --------------------------
        ours_q = f"""
        SELECT participant_key,
               ANY_VALUE(participant_name) AS participant_name,
               LOWER(TRIM(MAX(participant_email))) AS participant_email,
               SUM(duration_seconds) AS total_seconds,
               MIN(confidence) AS min_confidence
        FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}`
        WHERE event_date = @d
        GROUP BY participant_key
        """
        ours_rows = list(client.query(ours_q, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", date_str)]
        )).result())

        flagged, matched, only_ours, only_zoom = [], 0, [], []
        seen_zoom_keys = set()
        for r in ours_rows:
            zkey = r.participant_key
            if zkey not in zoom_secs and r.participant_email:
                zkey = zoom_emails.get(r.participant_email, zkey)  # email bridge
            if zkey not in zoom_secs and r.participant_name:
                # our key may be an email; fall back to the normalized name
                nk = normalize_participant_name(r.participant_name).strip().lower()
                if nk in zoom_secs:
                    zkey = nk
            if zkey in zoom_secs:
                seen_zoom_keys.add(zkey)
                matched += 1
                ours_min = round(r.total_seconds / 60, 1)
                zoom_min = round(zoom_secs[zkey] / 60, 1)
                if max(ours_min, zoom_min) < min_minutes:
                    continue
                delta = ours_min - zoom_min
                pct = abs(delta) / max(ours_min, zoom_min) * 100
                if pct > threshold_pct:
                    flagged.append({
                        'participant': r.participant_name,
                        'ours_minutes': ours_min,
                        'zoom_minutes': zoom_min,
                        'delta_minutes': round(delta, 1),
                        'delta_pct': round(pct, 1),
                        'min_confidence': r.min_confidence,
                    })
            elif r.total_seconds / 60 >= min_minutes:
                only_ours.append({'participant': r.participant_name,
                                  'ours_minutes': round(r.total_seconds / 60, 1)})
        for zkey, secs in zoom_secs.items():
            if zkey not in seen_zoom_keys and secs / 60 >= min_minutes:
                only_zoom.append({'participant': zoom_names.get(zkey, zkey),
                                  'zoom_minutes': round(secs / 60, 1)})

        flagged.sort(key=lambda x: -abs(x['delta_minutes']))
        result = {
            'success': True,
            'date': date_str,
            'meetings_checked': len(meetings),
            'zoom_participants': len(zoom_secs),
            'ours_participants': len(ours_rows),
            'matched': matched,
            'threshold_pct': threshold_pct,
            'flagged_count': len(flagged),
            'flagged': flagged,
            'only_in_ours': only_ours,
            'only_in_zoom': only_zoom,
        }
        print(f"[Reconcile {date_str}] matched={matched} flagged={len(flagged)} "
              f"only_ours={len(only_ours)} only_zoom={len(only_zoom)}")
        return jsonify(result)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        print(f"[Reconcile] error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/intervals/status', methods=['GET'])
def intervals_status():
    """Visibility into materialized intervals for a date.

    Query: ?date=YYYY-MM-DD (defaults to today IST)
    """
    try:
        date_str = validate_date_format(request.args.get('date'))
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        q = f"""
        SELECT
          COUNT(*) AS interval_count,
          COUNT(DISTINCT participant_key) AS participant_count,
          COUNTIF(source = 'snapshot')         AS source_snapshot,
          COUNTIF(source = 'synthesized_main') AS source_synthesized,
          COUNTIF(source = 'webhook_fill')     AS source_webhook_fill,
          COUNTIF(source = 'webhook_room')     AS source_webhook_room,
          COUNTIF(room_category = 'main')      AS cat_main,
          COUNTIF(room_category = 'breakout')  AS cat_breakout,
          COUNTIF(room_category = 'break')     AS cat_break,
          SUM(duration_seconds)                AS total_seconds,
          SUM(alone_seconds)                   AS total_alone_seconds,
          MIN(start_ts) AS first_start_ts,
          MAX(end_ts)   AS last_end_ts,
          MAX(built_at) AS last_built_at
        FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}`
        WHERE event_date = @date
        """
        rows = list(client.query(q, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("date", "DATE", date_str)]
        )).result())
        r = rows[0] if rows else None
        if not r or (r.interval_count or 0) == 0:
            return jsonify({
                'success': True,
                'date': date_str,
                'built': False,
                'message': 'No intervals materialized for this date. POST /intervals/rebuild to build.'
            })
        return jsonify({
            'success': True,
            'date': date_str,
            'built': True,
            'interval_count': r.interval_count,
            'participant_count': r.participant_count,
            'by_source': {
                'snapshot': r.source_snapshot,
                'synthesized_main': r.source_synthesized,
                'webhook_fill': r.source_webhook_fill,
            },
            'by_category': {
                'main': r.cat_main,
                'breakout': r.cat_breakout,
                'break': r.cat_break,
            },
            'total_minutes': round((r.total_seconds or 0) / 60.0, 1),
            'total_alone_minutes': round((r.total_alone_seconds or 0) / 60.0, 1),
            'first_start_utc': r.first_start_ts.isoformat() if r.first_start_ts else None,
            'last_end_utc':   r.last_end_ts.isoformat() if r.last_end_ts else None,
            'last_built_at_utc': r.last_built_at.isoformat() if r.last_built_at else None,
        })
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        print(f"[Intervals] Status error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ── "All Members" pseudo-team ───────────────────────────────────────────────
# Team View's team dropdown offers, alongside the real teams, an entry that
# reports on EVERY person who appeared in the Zoom meeting instead of on a
# roster. It rides the same /teams/<team_id>/... routes under a sentinel id,
# so the frontend, the CSV links and the Excel export need no special-casing
# beyond the label.
ALL_MEMBERS_TEAM_ID = '__all__'
ALL_MEMBERS_TEAM_NAME = 'All Members'


def _scout_bot_name_key():
    """Scout Bot's name in the same normalized form `_sql_normalize_name`
    produces, so SQL can exclude it by exact match. Substring matching was
    dropped in 2026-07 — a real person whose display name merely contained
    "Scout Bot" used to vanish from tracking."""
    return normalize_participant_name(SCOUT_BOT_NAME or '').lower().strip()


def _sql_identity_ctes(dataset_ref, date_where, is_all, with_date):
    """CTE block ending in `member_bridge` — the map from presence_intervals
    participants onto the people a Team View report lists. Shared by the three
    v2 endpoints, which previously each carried their own copy.

    date_where  presence_intervals filter, e.g. "event_date = @date"
    with_date   carry event_date through the bridge — range and monthly report
                per day; the daily endpoint aggregates the whole day at once
    is_all      the "All Members" pseudo-team. There is no roster, so every
                participant becomes their own member, keyed on participant_key
                (the builder has already merged one person's rejoin variants
                onto a single key). The Scout Bot is dropped.

    In team mode the block also defines `team_members`, which the daily
    endpoint outer-joins against so absent members still get a row.
    """
    dsel = 'event_date,' if with_date else ''          # inside distinct_keys
    dbridge = ' dk.event_date,' if with_date else ''   # inside member_bridge

    if is_all:
        return f"""
        distinct_keys AS (
            SELECT DISTINCT {dsel} participant_key, participant_name, participant_email
            FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}`
            WHERE {date_where}
              AND participant_name IS NOT NULL AND participant_name != ''
              AND {_sql_normalize_name('participant_name')} != @bot_key
        ),
        key_identity AS (
            -- One display name/email per person for the WHOLE report, chosen
            -- across every day in it. Picking per day would split someone who
            -- rejoined as "Priya-1" on Tuesday into two rows in the summary.
            SELECT participant_key,
                   MIN(participant_name)  AS member_name,
                   MAX(participant_email) AS member_email
            FROM distinct_keys
            GROUP BY participant_key
        ),
        member_bridge AS (
            SELECT DISTINCT
                ki.member_name, ki.member_email,{dbridge}
                dk.participant_key
            FROM distinct_keys dk
            JOIN key_identity ki ON ki.participant_key = dk.participant_key
        )"""

    norm_pi = _sql_normalize_name('dk.participant_name')
    norm_tm = _sql_normalize_name('tm.member_name')
    return f"""
        team_members AS (
            SELECT participant_name AS member_name, participant_email AS member_email
            FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}`
            WHERE team_id = @team_id
        ),
        distinct_keys AS (
            SELECT DISTINCT {dsel} participant_key, participant_name, participant_email
            FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}`
            WHERE {date_where}
              AND participant_name IS NOT NULL AND participant_name != ''
        ),
        aliases AS (
            -- Admin-confirmed links: Zoom display-name variant -> roster
            -- member. participant_key is already the normalized name, so
            -- alias_key compares against it directly.
            SELECT DISTINCT
                {_sql_normalize_name('alias_name')} AS alias_key,
                {_sql_normalize_name('member_name')} AS member_key
            FROM `{dataset_ref}.participant_aliases`
        ),
        member_keys AS (
            -- Each roster member's set of acceptable name keys: their own
            -- normalized name plus any aliased Zoom-name variants. Expanded
            -- BEFORE the bridge join because BigQuery rejects EXISTS
            -- subqueries inside join predicates.
            SELECT member_name, member_email, {norm_tm} AS match_key
            FROM team_members tm
            UNION DISTINCT
            SELECT tm.member_name, tm.member_email, al.alias_key AS match_key
            FROM team_members tm
            JOIN aliases al ON al.member_key = {norm_tm}
        ),
        member_bridge AS (
            SELECT DISTINCT
                mk.member_name, mk.member_email,{dbridge}
                dk.participant_key
            FROM member_keys mk
            JOIN distinct_keys dk
              ON (
                {norm_pi} = mk.match_key
                OR (
                  NULLIF(LOWER(TRIM(dk.participant_email)), '') IS NOT NULL
                  AND NULLIF(LOWER(TRIM(dk.participant_email)), '') = NULLIF(LOWER(TRIM(mk.member_email)), '')
                )
              )
        )"""


def _team_identity_params(team_id, is_all):
    """The query parameter that the identity CTEs reference — @team_id for a
    real team, @bot_key for All Members. Declaring both would leave one unused
    in every query."""
    if is_all:
        return [bigquery.ScalarQueryParameter("bot_key", "STRING", _scout_bot_name_key())]
    return [bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]


def _lookup_team_name(client, dataset_ref, team_id, is_all, with_manager=False):
    """Team name (and optionally manager) for the report header. The All
    Members pseudo-team has no row in the teams table."""
    if is_all:
        return (ALL_MEMBERS_TEAM_NAME, '') if with_manager else ALL_MEMBERS_TEAM_NAME
    cols = 'team_name, manager_name' if with_manager else 'team_name'
    rows = list(client.query(
        f"SELECT {cols} FROM `{dataset_ref}.{BQ_TEAMS_TABLE}` WHERE team_id = @team_id",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]
        )).result())
    if not rows:
        return ('Unknown', '') if with_manager else 'Unknown'
    return (rows[0].team_name, rows[0].manager_name) if with_manager else rows[0].team_name


@app.route('/teams/<team_id>/attendance_v2/<date>', methods=['GET'])
def team_attendance_v2(team_id, date):
    """v2 team attendance — reads from presence_intervals.

    Same JSON shape as /teams/<team_id>/attendance/<date> for diff parity.
    """
    try:
        report_date = validate_date_format(date)
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # PURE READER (2026-08-05): opening Team View no longer triggers an
        # interval build. Freshness is owned entirely by Cloud Scheduler
        # (intervals-rebuild-today-2min + intervals-auto-build-sweep); a page
        # view just reads what those jobs materialized. Use POST
        # /intervals/rebuild or /intervals/backfill to build a date on demand.
        _ensure_presence_intervals_table()

        # Team-member -> presence_intervals bridge uses NORMALIZED names so
        # Zoom rejoin variants ("Kajal Yadav-1") link to the "Kajal Yadav"
        # roster row. Also matches by email when present. One member may
        # match multiple participant_keys (one per rejoin variant) — all
        # intervals get summed under the member's row. For the All Members
        # pseudo-team there is no roster and every participant is their own
        # member; see _sql_identity_ctes.
        is_all = (team_id == ALL_MEMBERS_TEAM_ID)
        identity_ctes = _sql_identity_ctes(dataset_ref, 'event_date = @date',
                                          is_all=is_all, with_date=False)

        # All Members lists exactly the people who showed up, so there is no
        # roster to outer-join for absentees.
        if is_all:
            roster_join = "FROM per_member pm"
            roster_cols = "pm.member_name,\n            pm.member_email,"
            roster_order = "ORDER BY pm.member_name"
        else:
            roster_join = ("FROM team_members tm\n"
                           "        LEFT JOIN per_member pm\n"
                           "          ON pm.member_name = tm.member_name\n"
                           "         AND COALESCE(pm.member_email, '') = COALESCE(tm.member_email, '')")
            roster_cols = "tm.member_name,\n            tm.member_email,"
            roster_order = "ORDER BY tm.member_name"

        q = f"""
        WITH {identity_ctes},
        per_member AS (
            SELECT
                mb.member_name,
                mb.member_email,
                ANY_VALUE(pi.participant_name) AS display_name,
                MAX(pi.participant_email) AS display_email,
                -- Zoom-report parity: each interval bills UP to full minutes
                SUM(IF(pi.room_category='main',     {_sql_billed_seconds('pi.duration_seconds')}, 0)) AS main_seconds,
                SUM(IF(pi.room_category='breakout', {_sql_billed_seconds('pi.duration_seconds')}, 0)) AS breakout_seconds,
                SUM(IF(pi.room_category='break',    {_sql_billed_seconds('pi.duration_seconds')}, 0)) AS break_seconds,
                SUM(pi.alone_seconds) AS isolation_seconds,
                SUM({_sql_billed_seconds('pi.duration_seconds')}) AS total_seconds,
                -- LOWCONF cutoff 0.6 -> 0.4 (2026-08-08). The BQ builder
                -- (sp_build_presence_intervals v11) emits only 0.35 (open
                -- segment: no closing webhook, ending guessed) or 0.5 (closed
                -- by a real webhook). At 0.6 EVERY row counted as low
                -- confidence, so every person on every day showed the
                -- "⚠ estimated" marker and it carried no information.
                -- 0.4 flags only genuinely guessed endings.
                SUM(IF(pi.confidence < 0.4, {_sql_billed_seconds('pi.duration_seconds')}, 0)) AS lowconf_seconds,
                MIN(pi.start_ts) AS first_seen_utc,
                MAX(pi.end_ts)   AS last_seen_utc
            FROM member_bridge mb
            JOIN `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}` pi
              ON pi.event_date = @date
              AND pi.participant_key = mb.participant_key
            GROUP BY mb.member_name, mb.member_email
        )
        SELECT
            {roster_cols}
            pm.display_name AS participant_name,
            pm.display_email AS participant_email,
            COALESCE(SAFE_DIVIDE(pm.lowconf_seconds, pm.total_seconds), 0) > 0.5 AS estimated,
            {_sql_whole_minutes('pm.main_seconds')} AS main_room_mins,
            {_sql_whole_minutes('pm.breakout_seconds')} AS breakout_mins,
            {_sql_whole_minutes('pm.break_seconds')} AS break_minutes,
            {_sql_whole_minutes('pm.isolation_seconds')} AS isolation_minutes,
            {_sql_whole_minutes('pm.total_seconds')} AS total_duration_mins,
            FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(pm.first_seen_utc, INTERVAL 330 MINUTE)) AS first_seen_ist,
            FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(pm.last_seen_utc,  INTERVAL 330 MINUTE)) AS last_seen_ist
        {roster_join}
        {roster_order}
        """
        rows = list(client.query(q, job_config=bigquery.QueryJobConfig(
            query_parameters=_team_identity_params(team_id, is_all) + [
                bigquery.ScalarQueryParameter("date", "DATE", report_date),
            ]
        )).result())

        team_name, manager_name = _lookup_team_name(
            client, dataset_ref, team_id, is_all, with_manager=True)

        def _status(active):
            # Status is judged on break-EXCLUSIVE minutes, matching v1:
            # 5h parked in the Break Time room must not count as 'present'.
            if active >= 300:
                return 'present'
            if active >= 240:
                return 'half_day'
            return 'absent'

        participants = []
        for r in rows:
            display_name = r.participant_name or r.member_name
            display_email = r.participant_email or r.member_email or ''
            total = int(r.total_duration_mins or 0)
            brk = int(r.break_minutes or 0)
            participants.append({
                'name': display_name,
                'email': display_email,
                'first_seen_ist': r.first_seen_ist,
                'last_seen_ist': r.last_seen_ist,
                'total_duration_mins': total,
                'breakout_mins': int(r.breakout_mins or 0),
                'main_room_mins': int(r.main_room_mins or 0),
                'break_minutes': brk,
                'isolation_minutes': int(r.isolation_minutes or 0),
                'status': _status(total - brk),
                # True when >50% of the day's time came from low-confidence
                # (webhook-estimated) intervals — the UI shows a ⚠ marker.
                'estimated': bool(r.estimated),
            })

        # Match v1 post-processing: merge Zoom rejoin variants ("Aastha-1"
        # → "Aastha") and same-email duplicates. Without this, a person who
        # rejoined mid-meeting appears as two participants and one of them
        # (the one not in the team roster) gets dropped silently. See
        # app.py:8184-8185 for v1's equivalent calls.
        # The merge helpers don't carry the estimated flag — capture which
        # people had it BEFORE merging, restore by OR after.
        _est_keys = {normalize_participant_name(p['name']).lower().strip()
                     for p in participants if p.get('estimated')}
        participants = merge_participants_by_name(participants, mode='team')
        participants = collapse_by_email(participants, mode='team')

        # Re-derive status from merged totals (statuses summed via mode='team'
        # but rank-merge happens during merge; recompute defensively).
        for p in participants:
            p['status'] = _status(
                int(p.get('total_duration_mins') or 0) - int(p.get('break_minutes') or 0)
            )
            if normalize_participant_name(p.get('name') or '').lower().strip() in _est_keys:
                p['estimated'] = True

        # Name-drift detector: participants seen in the meeting today who
        # match NO roster member of ANY team. These are the people whose
        # hours silently vanish from Team View (nickname, typo, missing
        # email) while Day View still shows them — surface them so an admin
        # can fix the roster name instead of the person showing absent.
        # Opt-in (?include_unmatched=1): the Dashboard fires this endpoint
        # once per team, and the check is global — running it N times per
        # page load would waste BigQuery scans for identical results.
        # Meaningless for All Members, which lists every participant by
        # definition — nobody can be missing from it.
        unmatched = []
        want_unmatched = request.args.get('include_unmatched') == '1' and not is_all
        if want_unmatched:
          try:
            norm_dk = _sql_normalize_name('dk.participant_name')
            norm_am = _sql_normalize_name('am.member_name')
            um_q = f"""
            WITH all_members AS (
                SELECT DISTINCT participant_name AS member_name,
                                participant_email AS member_email
                FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}`
                UNION DISTINCT
                -- Aliased Zoom names are matched, not drift — exclude them
                SELECT alias_name AS member_name,
                       member_email
                FROM `{dataset_ref}.participant_aliases`
            ),
            dk AS (
                SELECT DISTINCT participant_name, participant_email
                FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}`
                WHERE event_date = @date
                  AND participant_name IS NOT NULL AND participant_name != ''
            )
            SELECT dk.participant_name, dk.participant_email
            FROM dk
            LEFT JOIN all_members am
              ON (
                {norm_dk} = {norm_am}
                OR (
                  NULLIF(LOWER(TRIM(dk.participant_email)), '') IS NOT NULL
                  AND NULLIF(LOWER(TRIM(dk.participant_email)), '') = NULLIF(LOWER(TRIM(am.member_email)), '')
                )
              )
            WHERE am.member_name IS NULL
            ORDER BY dk.participant_name
            """
            um_rows = client.query(um_q, job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("date", "DATE", report_date)]
            )).result()
            unmatched = [
                {'name': r.participant_name, 'email': r.participant_email or ''}
                for r in um_rows
            ]
          except Exception as e:
            print(f"[TeamV2] unmatched-participant check failed: {e}")

        return jsonify({
            'success': True,
            'team_id': team_id,
            'team_name': team_name,
            'manager_name': manager_name,
            'date': report_date,
            'source': 'presence_intervals_v2',
            'participants': participants,
            'unmatched_participants': unmatched,
        })
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        print(f"[TeamV2] error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500




@app.route('/teams/<team_id>/attendance/range_v2', methods=['GET'])
def team_attendance_range_v2(team_id):
    """v2 team attendance over a date range. Reads from presence_intervals.

    Query: ?start=YYYY-MM-DD&end=YYYY-MM-DD[&format=csv]
    Same JSON shape as v1 /teams/<id>/attendance/range so the frontend swaps
    cleanly. PURE READER — never builds intervals (see below).
    """
    try:
        start_date = validate_date_format(request.args.get('start'))
        end_date = validate_date_format(request.args.get('end'))
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # PURE READER (2026-08-05): opening Team View no longer triggers an
        # interval build. Cloud Scheduler owns freshness; for dates outside its
        # 35-day sweep, run POST /intervals/backfill once.
        built, still_missing = 0, 0

        is_all = (team_id == ALL_MEMBERS_TEAM_ID)
        identity_ctes = _sql_identity_ctes(dataset_ref, 'event_date BETWEEN @start AND @end',
                                           is_all=is_all, with_date=True)
        q = f"""
        WITH {identity_ctes},
        per_day AS (
            SELECT
                mb.member_name, mb.member_email, mb.event_date,
                ANY_VALUE(pi.participant_name) AS display_name,
                MAX(pi.participant_email) AS display_email,
                -- Zoom-report parity: each interval bills UP to full minutes
                SUM(IF(pi.room_category='main',     {_sql_billed_seconds('pi.duration_seconds')}, 0)) AS main_seconds,
                SUM(IF(pi.room_category='breakout', {_sql_billed_seconds('pi.duration_seconds')}, 0)) AS breakout_seconds,
                SUM(IF(pi.room_category='break',    {_sql_billed_seconds('pi.duration_seconds')}, 0)) AS break_seconds,
                SUM(pi.alone_seconds) AS isolation_seconds,
                SUM({_sql_billed_seconds('pi.duration_seconds')}) AS total_seconds,
                -- LOWCONF cutoff 0.6 -> 0.4 (2026-08-08). The BQ builder
                -- (sp_build_presence_intervals v11) emits only 0.35 (open
                -- segment: no closing webhook, ending guessed) or 0.5 (closed
                -- by a real webhook). At 0.6 EVERY row counted as low
                -- confidence, so every person on every day showed the
                -- "⚠ estimated" marker and it carried no information.
                -- 0.4 flags only genuinely guessed endings.
                SUM(IF(pi.confidence < 0.4, {_sql_billed_seconds('pi.duration_seconds')}, 0)) AS lowconf_seconds,
                MIN(pi.start_ts) AS first_seen_utc,
                MAX(pi.end_ts)   AS last_seen_utc
            FROM member_bridge mb
            JOIN `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}` pi
              ON pi.event_date = mb.event_date AND pi.participant_key = mb.participant_key
            GROUP BY mb.member_name, mb.member_email, mb.event_date
        )
        SELECT
            pd.member_name AS name, pd.member_email AS email,
            CAST(pd.event_date AS STRING) AS date,
            COALESCE(SAFE_DIVIDE(pd.lowconf_seconds, pd.total_seconds), 0) > 0.5 AS estimated,
            {_sql_whole_minutes('pd.main_seconds')} AS main_room_minutes,
            {_sql_whole_minutes('pd.breakout_seconds')} AS breakout_minutes,
            {_sql_whole_minutes('pd.break_seconds')} AS break_minutes,
            {_sql_whole_minutes('pd.isolation_seconds')} AS isolation_minutes,
            -- active = everything EXCEPT Break Time rooms (matches v1 and the
            -- frontend's "active hours" labels); total keeps break included.
            {_sql_whole_minutes('pd.total_seconds - pd.break_seconds')} AS active_minutes,
            {_sql_whole_minutes('pd.total_seconds')} AS total_minutes,
            FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(pd.first_seen_utc, INTERVAL 330 MINUTE)) AS first_seen_ist,
            FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(pd.last_seen_utc,  INTERVAL 330 MINUTE)) AS last_seen_ist
        FROM per_day pd
        WHERE COALESCE(pd.total_seconds, 0) > 0
        ORDER BY pd.member_name, pd.event_date
        """
        rows = list(client.query(q, job_config=bigquery.QueryJobConfig(
            query_parameters=_team_identity_params(team_id, is_all) + [
                bigquery.ScalarQueryParameter("start", "DATE", start_date),
                bigquery.ScalarQueryParameter("end", "DATE", end_date),
            ]
        )).result())

        team_name = _lookup_team_name(client, dataset_ref, team_id, is_all)

        # All team members for absence handling. All Members has no roster —
        # its headcount is however many people actually showed up, counted
        # from member_summary once that is built below.
        all_members = []
        if not is_all:
            members_q = f"SELECT participant_name, participant_email FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` WHERE team_id = @team_id"
            all_members = list(client.query(members_q, job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]
            )).result())

        def _status(total):
            return 'present' if total >= 300 else ('half_day' if total >= 240 else 'absent')

        daily_data = []
        for r in rows:
            active = int(r.active_minutes or 0)
            daily_data.append({
                'date': r.date,
                'name': r.name,
                'email': r.email or '',
                'first_seen_ist': r.first_seen_ist,
                'last_seen_ist': r.last_seen_ist,
                'active_minutes': active,
                'total_minutes': int(r.total_minutes or 0),
                'breakout_minutes': int(r.breakout_minutes or 0),
                'main_room_minutes': int(r.main_room_minutes or 0),
                'break_minutes': int(r.break_minutes or 0),
                'isolation_minutes': int(r.isolation_minutes or 0),
                # Status on break-EXCLUSIVE minutes (matches v1 and Team View daily)
                'status': _status(active),
                # >50% of the day from low-confidence (webhook-estimated)
                # intervals — UI shows a ⚠ marker on the cell.
                'estimated': bool(r.estimated),
            })

        # Member summary keyed by normalized name (collapses rejoin variants)
        member_summary = {}
        for r in daily_data:
            clean = normalize_participant_name(r['name'])
            key = clean.lower().strip()
            if not key:
                continue
            if key not in member_summary:
                member_summary[key] = {
                    'name': clean, 'email': r['email'],
                    'days_present': 0, 'total_active_mins': 0,
                    'total_break_mins': 0, 'total_isolation_mins': 0,
                }
            if r['status'] in ('present', 'half_day'):
                member_summary[key]['days_present'] += 1
            member_summary[key]['total_active_mins']    += r['active_minutes']
            member_summary[key]['total_break_mins']     += r['break_minutes']
            member_summary[key]['total_isolation_mins'] += r['isolation_minutes']

        # CSV export
        if request.args.get('format') == 'csv':
            import csv, io
            output = io.StringIO()
            writer = csv.writer(output)
            # Total_Minutes appended LAST to preserve column positions for
            # existing consumers. NOTE: Active_Minutes now excludes break time
            # (it previously included it — that was the bug).
            writer.writerow(['Date','Name','Email','Status','First_Seen_IST','Last_Seen_IST',
                             'Active_Minutes','Break_Minutes','Isolation_Minutes','Total_Minutes'])
            for r in daily_data:
                writer.writerow([r['date'], r['name'], r['email'], r['status'],
                                 r['first_seen_ist'], r['last_seen_ist'],
                                 r['active_minutes'], r['break_minutes'], r['isolation_minutes'], r['total_minutes']])
            return Response(output.getvalue(), mimetype='text/csv',
                            headers={'Content-Disposition': f'attachment; filename=team_{team_name.replace(" ","_")}_{start_date}_to_{end_date}.csv'})

        return jsonify({
            'success': True,
            'team_id': team_id,
            'team_name': team_name,
            'start_date': start_date,
            'end_date': end_date,
            'total_members': len(member_summary) if is_all else len(all_members),
            'daily_data': daily_data,
            'member_summary': list(member_summary.values()),
            'source': 'presence_intervals_v2',
            'auto_built_dates': built,
            'unbuilt_dates_remaining': still_missing,
        })
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        print(f"[RangeV2] error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>/report/monthly_v2', methods=['GET'])
def team_monthly_report_v2(team_id):
    """v2 monthly report. Reads from presence_intervals.

    Query: ?year=YYYY&month=M[&format=csv]
    Same JSON+CSV shape as v1 /teams/<id>/report/monthly.
    """
    try:
        year  = int(request.args.get('year'))
        month = int(request.args.get('month'))
        if not (1 <= month <= 12):
            return jsonify({'success': False, 'error': 'month must be 1..12'}), 400

        from calendar import monthrange
        start_date = f"{year:04d}-{month:02d}-01"
        end_date   = f"{year:04d}-{month:02d}-{monthrange(year, month)[1]:02d}"

        # Reuse range_v2 logic. We could call it directly, but copying the
        # data path here avoids HTTP overhead and lets us shape the response
        # exactly like v1 monthly.
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # PURE READER (2026-08-05): opening Team View no longer triggers an
        # interval build. Cloud Scheduler owns freshness; for dates outside its
        # 35-day sweep, run POST /intervals/backfill once.
        built, still_missing = 0, 0

        is_all = (team_id == ALL_MEMBERS_TEAM_ID)
        identity_ctes = _sql_identity_ctes(dataset_ref, 'event_date BETWEEN @start AND @end',
                                           is_all=is_all, with_date=True)
        q = f"""
        WITH {identity_ctes},
        per_day AS (
            SELECT
                mb.member_name, mb.member_email, mb.event_date,
                -- Zoom-report parity: each interval bills UP to full minutes
                SUM(IF(pi.room_category='main',     {_sql_billed_seconds('pi.duration_seconds')}, 0)) AS main_seconds,
                SUM(IF(pi.room_category='breakout', {_sql_billed_seconds('pi.duration_seconds')}, 0)) AS breakout_seconds,
                SUM(IF(pi.room_category='break',    {_sql_billed_seconds('pi.duration_seconds')}, 0)) AS break_seconds,
                SUM(pi.alone_seconds)             AS isolation_seconds,
                SUM({_sql_billed_seconds('pi.duration_seconds')}) AS total_seconds,
                -- LOWCONF cutoff 0.6 -> 0.4 (2026-08-08). The BQ builder
                -- (sp_build_presence_intervals v11) emits only 0.35 (open
                -- segment: no closing webhook, ending guessed) or 0.5 (closed
                -- by a real webhook). At 0.6 EVERY row counted as low
                -- confidence, so every person on every day showed the
                -- "⚠ estimated" marker and it carried no information.
                -- 0.4 flags only genuinely guessed endings.
                SUM(IF(pi.confidence < 0.4, {_sql_billed_seconds('pi.duration_seconds')}, 0)) AS lowconf_seconds,
                MIN(pi.start_ts) AS first_seen_utc,
                MAX(pi.end_ts)   AS last_seen_utc
            FROM member_bridge mb
            JOIN `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}` pi
              ON pi.event_date = mb.event_date AND pi.participant_key = mb.participant_key
            GROUP BY mb.member_name, mb.member_email, mb.event_date
        )
        SELECT
            pd.member_name AS name, pd.member_email AS email,
            CAST(pd.event_date AS STRING) AS date,
            COALESCE(SAFE_DIVIDE(pd.lowconf_seconds, pd.total_seconds), 0) > 0.5 AS estimated,
            {_sql_whole_minutes('pd.main_seconds')} AS main_room_minutes,
            {_sql_whole_minutes('pd.breakout_seconds')} AS breakout_minutes,
            {_sql_whole_minutes('pd.break_seconds')} AS break_minutes,
            {_sql_whole_minutes('pd.isolation_seconds')} AS isolation_minutes,
            -- active = everything EXCEPT Break Time rooms (matches v1 and the
            -- frontend's "active hours" labels); total keeps break included.
            {_sql_whole_minutes('pd.total_seconds - pd.break_seconds')} AS active_minutes,
            {_sql_whole_minutes('pd.total_seconds')} AS total_minutes,
            FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(pd.first_seen_utc, INTERVAL 330 MINUTE)) AS first_seen_ist,
            FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(pd.last_seen_utc,  INTERVAL 330 MINUTE)) AS last_seen_ist
        FROM per_day pd
        WHERE COALESCE(pd.total_seconds, 0) > 0
        ORDER BY pd.member_name, pd.event_date
        """
        rows = list(client.query(q, job_config=bigquery.QueryJobConfig(
            query_parameters=_team_identity_params(team_id, is_all) + [
                bigquery.ScalarQueryParameter("start", "DATE", start_date),
                bigquery.ScalarQueryParameter("end",   "DATE", end_date),
            ]
        )).result())

        team_name = _lookup_team_name(client, dataset_ref, team_id, is_all)

        def _status(total):
            return 'present' if total >= 300 else ('half_day' if total >= 240 else 'absent')

        data = []
        for r in rows:
            active = int(r.active_minutes or 0)
            data.append({
                'date': r.date,
                'name': r.name,
                'email': r.email or '',
                'first_seen_ist': r.first_seen_ist,
                'last_seen_ist': r.last_seen_ist,
                'active_minutes': active,
                'total_minutes': int(r.total_minutes or 0),
                'breakout_minutes': int(r.breakout_minutes or 0),
                'main_room_minutes': int(r.main_room_minutes or 0),
                'break_minutes': int(r.break_minutes or 0),
                'isolation_minutes': int(r.isolation_minutes or 0),
                # Status on break-EXCLUSIVE minutes (matches v1 and Team View daily)
                'status': _status(active),
                # >50% of the day from low-confidence (webhook-estimated)
                # intervals — UI shows a ⚠ marker on the cell.
                'estimated': bool(r.estimated),
            })
        data.sort(key=lambda x: (x['name'].lower(), x['date']))

        if request.args.get('format') == 'csv':
            import csv, io
            output = io.StringIO()
            writer = csv.writer(output)
            # Active_Minutes goes LAST: existing spreadsheets/scripts ingest
            # this CSV by position, so new columns must only append.
            writer.writerow(['Date','Name','Email','Status','First_Seen_IST','Last_Seen_IST',
                             'Total_Minutes','Breakout_Minutes','Main_Room_Minutes','Break_Minutes','Isolation_Minutes','Active_Minutes'])
            for r in data:
                writer.writerow([r['date'], r['name'], r['email'], r['status'],
                                 r['first_seen_ist'], r['last_seen_ist'], r['total_minutes'],
                                 r['breakout_minutes'], r['main_room_minutes'], r['break_minutes'], r['isolation_minutes'], r['active_minutes']])
            return Response(output.getvalue(), mimetype='text/csv',
                            headers={'Content-Disposition': f'attachment; filename=team_{team_name.replace(" ","_")}_{year}_{month:02d}.csv'})

        # Member summary
        member_summary = {}
        for r in data:
            clean = normalize_participant_name(r['name'])
            key = clean.lower().strip()
            if not key:
                continue
            if key not in member_summary:
                member_summary[key] = {
                    'name': clean, 'email': r['email'],
                    'days_present': 0, 'total_active_mins': 0,
                    'total_break_mins': 0, 'total_isolation_mins': 0,
                }
            if r['status'] in ('present', 'half_day'):
                member_summary[key]['days_present'] += 1
            member_summary[key]['total_active_mins']    += r['active_minutes']
            member_summary[key]['total_break_mins']     += r['break_minutes']
            member_summary[key]['total_isolation_mins'] += r['isolation_minutes']

        # Working days in the month (Mon-Fri)
        from datetime import date as date_cls
        working_days = 0
        for d in range(1, monthrange(year, month)[1] + 1):
            if date_cls(year, month, d).weekday() < 5:
                working_days += 1

        return jsonify({
            'success': True,
            'team_id': team_id,
            'team_name': team_name,
            'year': year,
            'month': month,
            'working_days': working_days,
            'daily_data': data,
            'member_summary': list(member_summary.values()),
            'source': 'presence_intervals_v2',
            'auto_built_dates': built,
            'unbuilt_dates_remaining': still_missing,
        })
    except (TypeError, ValueError) as e:
        return jsonify({'success': False, 'error': f'invalid year/month: {e}'}), 400
    except Exception as e:
        print(f"[MonthlyV2] error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# ROOM OVERRIDES — human corrections that outrank every automatic room name
# ==============================================================================
# Zoom never sends a room name in its breakout webhooks; the server resolves
# one from memory when the webhook lands and freezes that guess into
# participant_events_p. When the guess is wrong the damage is real: on
# 2026-08-11 a work room was stamped with the break room's name and 187
# minutes of working time were filed as break.
#
# These endpoints write to `room_overrides`, which sp_build_presence_intervals
# reads as priority 0/1 — ahead of room_mappings and ahead of the event
# stream. The override is an INPUT to the builder, never an edit to
# presence_intervals: that table is deleted and rebuilt every 5 minutes, so
# anything written into it directly would vanish on the next tick. Writing to
# room_overrides instead means the correction is applied by every future
# rebuild, including backfills of past days.

ROOM_OVERRIDES_TABLE = 'room_overrides'
VALID_ROOM_CATEGORIES = ('break', 'breakout', 'main')


def _ensure_room_overrides_table():
    """Create room_overrides if absent. Mirrors 08_room_overrides_v13.sql so a
    fresh environment works without running the SQL file by hand."""
    client = get_bq_client()
    dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
    client.query(f"""
    CREATE TABLE IF NOT EXISTS `{dataset_ref}.{ROOM_OVERRIDES_TABLE}` (
        override_id   STRING NOT NULL,
        room_uuid     STRING NOT NULL,
        mapping_date  DATE,
        room_name     STRING,
        room_category STRING,
        note          STRING,
        set_by        STRING,
        set_at        TIMESTAMP,
        active        BOOL
    )
    """).result()


def _rebuild_dates_via_procedure(dates):
    """CALL the BigQuery builder for each date so the override takes effect
    immediately instead of waiting for the 5-minute scheduled query.
    Returns (rebuilt, errors)."""
    client = get_bq_client()
    dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
    rebuilt, errors = [], []
    for d in dates:
        if not d:
            continue
        try:
            client.query(
                f"CALL `{dataset_ref}.sp_build_presence_intervals`(@d)",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", d)]
                )).result()
            rebuilt.append(str(d))
        except Exception as e:
            # A concurrent build of the same day aborts one of the two; the
            # next scheduled tick picks it up, so this is not fatal.
            print(f"[RoomOverride] rebuild failed for {d}: {e}")
            errors.append({'date': str(d), 'error': str(e)[:200]})
    return rebuilt, errors


@app.route('/rooms/overrides', methods=['GET'])
@require_auth()
def list_room_overrides():
    """Every override, newest first. ?active=1 for live ones only."""
    try:
        _ensure_room_overrides_table()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        only_active = request.args.get('active') == '1'
        where = "WHERE COALESCE(active, TRUE)" if only_active else ""
        rows = list(client.query(f"""
            SELECT override_id, room_uuid, CAST(mapping_date AS STRING) AS mapping_date,
                   room_name, room_category, note, set_by,
                   CAST(set_at AS STRING) AS set_at, COALESCE(active, TRUE) AS active
            FROM `{dataset_ref}.{ROOM_OVERRIDES_TABLE}`
            {where}
            ORDER BY set_at DESC
            LIMIT 500
        """).result())
        return jsonify({'success': True, 'overrides': [dict(r) for r in rows]})
    except Exception as e:
        print(f"[RoomOverride] list error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/rooms/override', methods=['POST'])
@require_auth(roles=['admin', 'superadmin'])
def create_room_override():
    """Record what a room actually is, then rebuild the affected day.

    Body: {room_uuid, room_name?, room_category?, mapping_date?, note?}
      mapping_date omitted/null -> applies whenever this room UUID appears.
      At least one of room_name / room_category must be given.

    Restricted to admin/superadmin on purpose: marking the break room as a
    work room raises EVERYBODY's working hours, including the caller's, and
    would be invisible in a report. Every change is stamped with who made it.
    """
    try:
        data = request.get_json(silent=True) or {}
        room_uuid = (data.get('room_uuid') or '').strip()
        room_name = (data.get('room_name') or '').strip()
        room_category = (data.get('room_category') or '').strip().lower()
        note = (data.get('note') or '').strip()
        raw_date = (data.get('mapping_date') or '').strip()

        if not room_uuid:
            return jsonify({'success': False, 'error': 'room_uuid is required'}), 400
        if not room_name and not room_category:
            return jsonify({'success': False,
                            'error': 'give room_name, room_category, or both'}), 400
        if room_category and room_category not in VALID_ROOM_CATEGORIES:
            return jsonify({'success': False,
                            'error': f'room_category must be one of {VALID_ROOM_CATEGORIES}'}), 400

        mapping_date = validate_date_format(raw_date) if raw_date else None

        _ensure_room_overrides_table()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        actor = ''
        payload = getattr(request, 'auth_user', None) or {}
        actor = payload.get('u') or payload.get('e') or 'unknown'

        override_id = str(uuid_lib.uuid4())
        # Parameterised INSERT ... SELECT rather than insert_rows_json: rows
        # streamed via the insert API sit in a write buffer and are not
        # guaranteed visible to the very next query, and this endpoint rebuilds
        # immediately afterwards. A DML insert is readable straight away.
        client.query(f"""
            INSERT INTO `{dataset_ref}.{ROOM_OVERRIDES_TABLE}`
              (override_id, room_uuid, mapping_date, room_name, room_category,
               note, set_by, set_at, active)
            SELECT @override_id, @room_uuid, @mapping_date,
                   NULLIF(@room_name, ''), NULLIF(@room_category, ''),
                   NULLIF(@note, ''), @set_by, CURRENT_TIMESTAMP(), TRUE
        """, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("override_id", "STRING", override_id),
            bigquery.ScalarQueryParameter("room_uuid", "STRING", room_uuid),
            bigquery.ScalarQueryParameter("mapping_date", "DATE", mapping_date),
            bigquery.ScalarQueryParameter("room_name", "STRING", room_name),
            bigquery.ScalarQueryParameter("room_category", "STRING", room_category),
            bigquery.ScalarQueryParameter("note", "STRING", note),
            bigquery.ScalarQueryParameter("set_by", "STRING", actor),
        ])).result()

        # Which day(s) to recompute. A dated override touches that day. An
        # undated one touches every day this room UUID actually appears on,
        # capped so one click cannot trigger a month of rebuilds.
        if mapping_date:
            dates = [mapping_date]
        else:
            drows = list(client.query(f"""
                SELECT DISTINCT event_date
                FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}`
                WHERE room_uuid = @room_uuid
                  AND event_date >= DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL 45 DAY)
                ORDER BY event_date DESC
                LIMIT 15
            """, job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("room_uuid", "STRING", room_uuid)
            ])).result())
            dates = [r.event_date for r in drows]

        rebuilt, errors = _rebuild_dates_via_procedure(dates)

        return jsonify({
            'success': True,
            'override_id': override_id,
            'room_uuid': room_uuid,
            'room_name': room_name or None,
            'room_category': room_category or None,
            'mapping_date': str(mapping_date) if mapping_date else None,
            'set_by': actor,
            'rebuilt_dates': rebuilt,
            'rebuild_errors': errors,
        })
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        print(f"[RoomOverride] create error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/rooms/override/<override_id>', methods=['DELETE'])
@require_auth(roles=['admin', 'superadmin'])
def retire_room_override(override_id):
    """Retire an override (active = FALSE) and rebuild the days it touched.
    Never deletes the row — the record of who changed what has to survive."""
    try:
        _ensure_room_overrides_table()
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        rows = list(client.query(f"""
            SELECT room_uuid, mapping_date
            FROM `{dataset_ref}.{ROOM_OVERRIDES_TABLE}`
            WHERE override_id = @id
        """, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("id", "STRING", override_id)
        ])).result())
        if not rows:
            return jsonify({'success': False, 'error': 'override not found'}), 404

        client.query(f"""
            UPDATE `{dataset_ref}.{ROOM_OVERRIDES_TABLE}`
            SET active = FALSE
            WHERE override_id = @id
        """, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("id", "STRING", override_id)
        ])).result()

        target = rows[0].mapping_date
        dates = [target] if target else []
        if not dates:
            drows = list(client.query(f"""
                SELECT DISTINCT event_date
                FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}`
                WHERE room_uuid = @room_uuid
                  AND event_date >= DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL 45 DAY)
                ORDER BY event_date DESC
                LIMIT 15
            """, job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("room_uuid", "STRING", rows[0].room_uuid)
            ])).result())
            dates = [r.event_date for r in drows]

        rebuilt, errors = _rebuild_dates_via_procedure(dates)
        return jsonify({'success': True, 'override_id': override_id,
                        'rebuilt_dates': rebuilt, 'rebuild_errors': errors})
    except Exception as e:
        print(f"[RoomOverride] retire error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/attendance/summary_v2/<date>', methods=['GET'])
def attendance_summary_v2(date):
    """v2 Day View Full. Reads from presence_intervals.

    Same JSON shape as v1 /attendance/summary/<date>: one entry per
    participant with first_seen, last_seen, total_duration_mins, and
    room_visits (ordered intervals).
    """
    try:
        report_date = validate_date_format(date)
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # Auto-build behaviour — OFF by default (PAGELOAD_AUTO_BUILD, 2026-08-05):
        # viewing a day must not trigger a build. Builds happen only via the
        # explicit endpoints (/intervals/rebuild, /intervals/auto-build,
        # /intervals/backfill). With the flag on, the old behavior returns:
        #   - TODAY's date: rebuild so Day View reflects fresh snapshots
        #   - past dates: build only if not yet materialized
        _ensure_presence_intervals_table()
        if PAGELOAD_AUTO_BUILD:
            if report_date == get_business_date():
                try:
                    _rebuild_today_guarded(report_date)
                except Exception as e:
                    print(f"[SummaryV2] Today auto-build failed for {report_date}: {e}")
            else:
                check_q = f"""
                SELECT COUNT(*) AS n FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}`
                WHERE event_date = @date
                """
                check_rows = list(client.query(check_q, job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("date", "DATE", report_date)]
                )).result())
                if check_rows and (check_rows[0].n or 0) == 0:
                    print(f"[SummaryV2] No intervals for {report_date} — auto-building")
                    try:
                        build_presence_intervals(report_date)
                    except Exception as e:
                        print(f"[SummaryV2] Auto-build failed for {report_date}: {e}")

        # Pull all intervals for the date, ordered by participant + start_ts
        # room_uuid (added v13) is what lets My Day offer "this room was
        # actually X" — you cannot correct a room you cannot identify.
        q = f"""
        SELECT
          participant_key,
          participant_name,
          participant_email,
          room_uuid,
          room_name,
          room_category,
          start_ts,
          end_ts,
          duration_seconds,
          alone_seconds,
          source
        FROM `{dataset_ref}.{PRESENCE_INTERVALS_TABLE}`
        WHERE event_date = @date
        ORDER BY participant_key, start_ts
        """
        rows = list(client.query(q, job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("date", "DATE", report_date)]
        )).result())

        # Group by participant_key, then collapse rejoin variants via normalized name
        by_key = {}
        for r in rows:
            k = r.participant_key
            if k not in by_key:
                by_key[k] = {
                    'name': r.participant_name,
                    'email': r.participant_email or '',
                    'intervals': []
                }
            by_key[k]['intervals'].append(r)

        # Collapse rejoin variants: merge participants whose normalized name + email match
        # (matches v1 monthly/range merge_participants_by_name behaviour)
        merged = {}
        for k, info in by_key.items():
            clean = normalize_participant_name(info['name'])
            email = (info['email'] or '').strip().lower()
            mkey = (clean.lower().strip(), email)
            if mkey not in merged:
                merged[mkey] = {
                    'name': clean,
                    'email': info['email'],
                    'intervals': []
                }
            merged[mkey]['intervals'].extend(info['intervals'])

        def _fmt_ist(ts):
            if ts is None:
                return None
            # ts is a UTC TIMESTAMP from BQ — convert to IST string
            ist = ts + IST_OFFSET
            return ist.strftime('%H:%M')

        participants = []
        for (name_key, _), info in merged.items():
            ivs = sorted(info['intervals'], key=lambda x: x.start_ts)
            if not ivs:
                continue
            first_seen = min(iv.start_ts for iv in ivs)
            last_seen  = max(iv.end_ts   for iv in ivs)
            total_seconds = sum(iv.duration_seconds or 0 for iv in ivs)
            isolation_seconds = sum(iv.alone_seconds or 0 for iv in ivs)
            room_visits = []
            for iv in ivs:
                # NOTE: field name MUST be room_duration_mins to match v1 and
                # the frontend's transformSummaryToEmployees in zoomApi.js
                # (which sums room_duration_mins to compute total minutes).
                room_visits.append({
                    'room_uuid':         getattr(iv, 'room_uuid', None) or '',
                    'room_name':         iv.room_name,
                    'room_category':     iv.room_category,
                    'room_joined_ist':   _fmt_ist(iv.start_ts),
                    'room_left_ist':     _fmt_ist(iv.end_ts),
                    'room_duration_mins': _whole_minutes_from_seconds(iv.duration_seconds),
                    'room_duration_seconds': int(iv.duration_seconds or 0),
                    'source':            iv.source,
                })
            participants.append({
                'name':                info['name'],
                'email':               info['email'] or '',
                'first_seen_ist':      _fmt_ist(first_seen),
                'last_seen_ist':       _fmt_ist(last_seen),
                # Zoom parity: total = SUM of per-room billed minutes,
                # exactly how Zoom's report totals its session rows
                'total_duration_mins': sum(v['room_duration_mins'] for v in room_visits),
                'total_duration_seconds': int(total_seconds),
                'isolation_minutes':   _whole_minutes_from_seconds(isolation_seconds),
                'room_visits':         room_visits,
            })

        participants.sort(key=lambda x: x['name'].lower())
        return jsonify({
            'success': True,
            'date': report_date,
            'source': 'presence_intervals_v2',
            'participants': participants,
            'total_participants': len(participants),
        })
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        print(f"[SummaryV2] error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/intervals/backfill', methods=['POST'])
def intervals_backfill():
    """Build presence_intervals for many dates in one request.

    Body options:
      {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
      {"days": 30}   — last N IST days (excluding today)
      {"dates": ["YYYY-MM-DD", ...]}

    Returns per-date status + summary counts.
    """
    try:
        data = request.get_json(silent=True) or {}
        dates = []
        if 'dates' in data and isinstance(data['dates'], list):
            for d in data['dates']:
                dates.append(validate_date_format(d))
        elif 'start' in data and 'end' in data:
            s = datetime.strptime(validate_date_format(data['start']), '%Y-%m-%d').date()
            e = datetime.strptime(validate_date_format(data['end']),   '%Y-%m-%d').date()
            cur = s
            while cur <= e:
                dates.append(cur.isoformat())
                cur += timedelta(days=1)
        elif 'days' in data:
            n = int(data['days'])
            today = datetime.strptime(get_ist_date(), '%Y-%m-%d').date()
            for i in range(1, n + 1):
                dates.append((today - timedelta(days=i)).isoformat())
            dates.sort()
        else:
            return jsonify({'success': False, 'error': 'specify start+end, days, or dates'}), 400

        results = []
        for d in dates:
            try:
                r = build_presence_intervals(d)
                results.append({'date': d, 'success': True,
                                'intervals': r['intervals_built'],
                                'participants': r['participants'],
                                'elapsed_s': r['elapsed_seconds']})
            except Exception as e:
                print(f"[Backfill] {d} failed: {e}")
                results.append({'date': d, 'success': False, 'error': str(e)[:200]})

        ok = sum(1 for r in results if r.get('success'))
        return jsonify({
            'success': True,
            'requested': len(dates),
            'succeeded': ok,
            'failed': len(results) - ok,
            'total_intervals': sum(r.get('intervals', 0) for r in results if r.get('success')),
            'results': results,
        })
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        print(f"[Backfill] error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))

    print("=" * 60)
    print("BREAKOUT ROOM CALIBRATOR v2.0")
    print("=" * 60)

    # Load existing mappings from BigQuery (survives server restart)
    init_meeting_state()
    print(f"Port: {port}")
    print(f"GCP Project: {GCP_PROJECT_ID}")
    print(f"BigQuery Dataset: {BQ_DATASET}")
    print(f"Scout Bot Name: {SCOUT_BOT_NAME}")
    print(f"Webhook Secret: {'configured (' + str(len(ZOOM_WEBHOOK_SECRET)) + ' chars)' if ZOOM_WEBHOOK_SECRET else 'NOT SET'}")
    print()
    print("FLOW:")
    print("1. Start meeting at 9 AM")
    print("2. HR joins as 'Scout Bot'")
    print("3. Open Zoom App -> Run Calibration")
    print("4. Scout Bot can leave after calibration")
    print("5. Webhooks capture all participant activity")
    print("6. Daily report generated at 9:15 AM")
    print("=" * 60)

    app.run(host='0.0.0.0', port=port, debug=False)
