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

from flask import Flask, request, jsonify, send_from_directory, Response
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

# ==============================================================================
# IST TIMEZONE HELPERS (UTC+5:30 - India Standard Time)
# ==============================================================================
IST_OFFSET = timedelta(hours=5, minutes=30)

def get_ist_now():
    """Get current datetime in IST"""
    return datetime.utcnow() + IST_OFFSET

def get_ist_date():
    """Get current date in IST (YYYY-MM-DD)"""
    return get_ist_now().strftime('%Y-%m-%d')

def utc_to_ist(utc_dt):
    """Convert UTC datetime to IST datetime"""
    if utc_dt is None:
        return None
    return utc_dt + IST_OFFSET

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
    elif origin and (path.startswith('/attendance/') or path.startswith('/dashboard') or path.startswith('/teams')):
        # Allow external apps (attendance manager) to call attendance & team APIs
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'

    # OWASP Security Headers (required by Zoom Apps)
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self' 'unsafe-inline' 'unsafe-eval' https: data: blob:; frame-ancestors https://*.zoom.us https://*.zoom.com"
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'

    return response

# Zoom Credentials - MUST be set via environment variables
# No default values to prevent accidental deployment without proper configuration
ZOOM_WEBHOOK_SECRET = os.environ.get('ZOOM_WEBHOOK_SECRET', '').strip()
ZOOM_ACCOUNT_ID = os.environ.get('ZOOM_ACCOUNT_ID', '')
ZOOM_CLIENT_ID = os.environ.get('ZOOM_CLIENT_ID', '')
ZOOM_CLIENT_SECRET = os.environ.get('ZOOM_CLIENT_SECRET', '')

# Scout Bot Configuration
SCOUT_BOT_NAME = os.environ.get('SCOUT_BOT_NAME', 'Scout Bot')
SCOUT_BOT_EMAIL = os.environ.get('SCOUT_BOT_EMAIL', '')

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
GCP_PROJECT_ID = os.environ.get('GCP_PROJECT_ID', '')
BQ_DATASET = os.environ.get('BQ_DATASET', 'breakout_room_calibrator')

# BigQuery Tables
BQ_EVENTS_TABLE = 'participant_events'
BQ_MAPPINGS_TABLE = 'room_mappings'
BQ_CAMERA_TABLE = 'camera_events'
BQ_QOS_TABLE = 'qos_data'
BQ_CALIBRATION_STATE_TABLE = 'calibration_state'
BQ_TEAMS_TABLE = 'teams'
BQ_TEAM_MEMBERS_TABLE = 'team_members'

# Email Configuration
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
REPORT_EMAIL_FROM = os.environ.get('REPORT_EMAIL_FROM', 'reports@yourdomain.com')
REPORT_EMAIL_TO = os.environ.get('REPORT_EMAIL_TO', '')

# Clients
bq_client = None

def get_bq_client():
    global bq_client
    if bq_client is None:
        bq_client = bigquery.Client(project=GCP_PROJECT_ID)
    return bq_client

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
                meeting_filter = " AND meeting_id = @meeting_id"
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

        for mapping in cleaned_mappings:
            meeting_id = mapping['meeting_id']
            room_uuid = mapping['room_uuid']
            room_name = mapping['room_name']
            source = mapping.get('source', 'unknown')
            mapping_date = mapping['mapping_date']

            # Check for existing mapping with same room_uuid
            check_query = f"""
            SELECT mapping_id, room_name, source
            FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
            WHERE meeting_id = @meeting_id
              AND room_uuid = @room_uuid
              AND mapping_date = @mapping_date
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
                    continue

                # If same source and same name, skip
                if existing_source == source and existing_name == room_name:
                    print(f"[BigQuery] SKIP: {room_name} - identical mapping exists")
                    continue

                # Update existing mapping (new source wins or same source with new name)
                update_query = f"""
                UPDATE `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}`
                SET room_name = @room_name,
                    source = @source,
                    mapped_at = @mapped_at
                WHERE meeting_id = @meeting_id
                  AND room_uuid = @room_uuid
                  AND mapping_date = @mapping_date
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

        print(f"[BigQuery] Mappings: {inserted_count} inserted, {updated_count} updated")
        return inserted_count > 0 or updated_count > 0
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

class ZoomAPI:
    """Helper for Zoom API calls"""

    def __init__(self):
        self.access_token = None
        self.token_expires = 0

    def get_access_token(self):
        """Get OAuth token (cached)"""
        now = time.time()
        if self.access_token and now < self.token_expires - 60:
            return self.access_token

        if not all([ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET]):
            raise ValueError("Zoom API credentials not configured")

        url = f"https://zoom.us/oauth/token?grant_type=account_credentials&account_id={ZOOM_ACCOUNT_ID}"
        response = requests.post(
            url,
            auth=(ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET),
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30
        )

        if response.status_code != 200:
            raise Exception(f"Token error: {response.text}")

        data = response.json()
        self.access_token = data['access_token']
        self.token_expires = now + data.get('expires_in', 3600)
        return self.access_token

    def _api_get_with_retry(self, url, headers, params, max_retries=3):
        """Make a GET request with rate limit (429) retry and exponential backoff."""
        for attempt in range(max_retries):
            response = requests.get(url, headers=headers, params=params, timeout=30)
            if response.status_code == 429:
                retry_after = int(response.headers.get('Retry-After', 1))
                wait_time = max(retry_after, 2 ** attempt)
                print(f"[ZoomAPI] Rate limited (429), retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            return response
        # Return last response even if still 429
        return response

    def get_past_meeting_participants(self, meeting_uuid, page_size=300):
        """
        Get past meeting participants - includes duration and basic QoS
        NOW WITH PAGINATION SUPPORT - fetches ALL pages

        IMPORTANT: Zoom API returns 'duration' in SECONDS, not minutes!
        The caller must convert to minutes if needed.

        Returns list of participant dicts with fields:
        - id/user_id: Participant ID
        - name/user_name: Display name
        - user_email/email: Email (may be empty)
        - join_time: ISO timestamp
        - leave_time: ISO timestamp
        - duration: Duration in SECONDS (not minutes!)
        - attentiveness_score: May not be present (requires Business+ plan)
        """
        all_participants = []

        try:
            token = self.get_access_token()
            headers = {'Authorization': f'Bearer {token}'}

            # Build list of URL patterns to try (will add pagination params later)
            url_patterns = []

            # Method 1: Double-encoded UUID (required for UUIDs with / or //)
            encoded_uuid = requests.utils.quote(requests.utils.quote(meeting_uuid, safe=''), safe='')
            url_patterns.append(
                (f"https://api.zoom.us/v2/past_meetings/{encoded_uuid}/participants", "past_meetings (double-encoded)")
            )

            # Method 2: Single-encoded UUID
            encoded_uuid2 = requests.utils.quote(meeting_uuid, safe='')
            if encoded_uuid2 != encoded_uuid:
                url_patterns.append(
                    (f"https://api.zoom.us/v2/past_meetings/{encoded_uuid2}/participants", "past_meetings (single-encoded)")
                )

            # Method 3: Raw UUID (for simple meeting IDs)
            if meeting_uuid and not any(c in meeting_uuid for c in ['/', '+', '=']):
                url_patterns.append(
                    (f"https://api.zoom.us/v2/past_meetings/{meeting_uuid}/participants", "past_meetings (raw)")
                )

            # Method 4: Report API (may have more data, requires Zoom Pro+)
            url_patterns.append(
                (f"https://api.zoom.us/v2/report/meetings/{encoded_uuid2}/participants", "report API")
            )

            # Try each method with pagination
            for base_url, method_name in url_patterns:
                try:
                    all_participants = []
                    next_page_token = None
                    page_count = 0
                    max_pages = 50  # Safety limit
                    auth_retries = 0  # Track 401 retries to prevent infinite loop
                    max_auth_retries = 3

                    while page_count < max_pages:
                        # Build URL with pagination params
                        params = {'page_size': page_size}
                        if next_page_token:
                            params['next_page_token'] = next_page_token

                        print(f"[ZoomAPI] Trying {method_name} (page {page_count + 1})...")
                        response = self._api_get_with_retry(base_url, headers, params)

                        if response.status_code == 200:
                            data = response.json()
                            participants = data.get('participants', [])

                            if participants:
                                all_participants.extend(participants)
                                print(f"[ZoomAPI] Page {page_count + 1}: got {len(participants)} participants (total: {len(all_participants)})")

                                # Check for more pages
                                next_page_token = data.get('next_page_token', '')
                                page_count += 1

                                if not next_page_token:
                                    # No more pages
                                    print(f"[ZoomAPI] SUCCESS via {method_name}: {len(all_participants)} total participants")

                                    # Log first participant for debugging
                                    if all_participants:
                                        sample = all_participants[0]
                                        print(f"[ZoomAPI] Sample participant fields: {list(sample.keys())}")
                                        duration = sample.get('duration', 'N/A')
                                        print(f"[ZoomAPI] Sample duration value: {duration} (type: {type(duration).__name__})")

                                    return all_participants
                            else:
                                # No participants on first page
                                break

                        elif response.status_code == 404:
                            print(f"[ZoomAPI] {method_name}: Meeting not found (404)")
                            break
                        elif response.status_code == 400:
                            print(f"[ZoomAPI] {method_name}: Bad request (400) - {response.text[:200]}")
                            break
                        elif response.status_code == 401:
                            auth_retries += 1
                            if auth_retries > max_auth_retries:
                                print(f"[ZoomAPI] {method_name}: Too many 401 errors ({auth_retries}), giving up")
                                break
                            print(f"[ZoomAPI] {method_name}: Unauthorized (401) - refreshing token (retry {auth_retries}/{max_auth_retries})")
                            self.access_token = None
                            self.token_expires = 0
                            token = self.get_access_token()
                            headers = {'Authorization': f'Bearer {token}'}
                            # Retry same page
                            continue
                        else:
                            print(f"[ZoomAPI] {method_name}: {response.status_code} - {response.text[:200]}")
                            break

                    # If we collected any participants, return them
                    if all_participants:
                        print(f"[ZoomAPI] SUCCESS via {method_name}: {len(all_participants)} total participants")
                        return all_participants

                except requests.exceptions.RequestException as re:
                    print(f"[ZoomAPI] {method_name}: Request error - {re}")

            print(f"[ZoomAPI] All methods failed for meeting: {meeting_uuid}")
            return []

        except Exception as e:
            print(f"[ZoomAPI] Past meeting error: {e}")
            traceback.print_exc()
            return []

    def get_meeting_participants_qos(self, meeting_id, max_pages=200):
        """
        Get QoS data for meeting participants using Dashboard Metrics API.
        This includes video_output data which indicates camera status.

        IMPORTANT: Requires Business/Education/Enterprise plan and
        dashboard_meetings:read:admin scope.

        Args:
            meeting_id: The meeting ID
            max_pages: Maximum pages to fetch (default 200 = 2000 participants)
                       Use smaller value for quick searches

        Returns list of participants with video_output stats.
        When camera is ON: video_output has bitrate, resolution, etc.
        When camera is OFF: video_output is empty/null
        """
        all_participants = []

        try:
            token = self.get_access_token()
            headers = {'Authorization': f'Bearer {token}'}

            # Dashboard Metrics API endpoint
            # Works for both live and past meetings (within last 30 days)
            encoded_id = requests.utils.quote(requests.utils.quote(str(meeting_id), safe=''), safe='')
            base_url = f"https://api.zoom.us/v2/metrics/meetings/{encoded_id}/participants/qos"

            next_page_token = None
            page_count = 0

            print(f"[ZoomAPI] Fetching QoS data for meeting {meeting_id}...")

            auth_retries = 0
            max_auth_retries = 3

            while page_count < max_pages:
                params = {'page_size': 10}  # Max 10 per page for QoS API
                if next_page_token:
                    params['next_page_token'] = next_page_token

                response = self._api_get_with_retry(base_url, headers, params)

                if response.status_code == 200:
                    data = response.json()
                    participants = data.get('participants', [])

                    if participants:
                        # Log first participant's raw QoS structure for debugging
                        if page_count == 0 and participants:
                            first_p = participants[0]
                            print(f"[ZoomAPI] Participant fields: {list(first_p.keys())}")
                            user_qos_sample = first_p.get('user_qos', [])
                            if user_qos_sample:
                                print(f"[ZoomAPI] QoS entry fields: {list(user_qos_sample[0].keys())}")
                                print(f"[ZoomAPI] FULL QoS entry: {json.dumps(user_qos_sample[0], indent=2)}")
                            else:
                                print(f"[ZoomAPI] WARNING: No user_qos data in participant")

                        # Extract camera status from video_output with timestamps
                        for p in participants:
                            user_qos = p.get('user_qos', [])
                            camera_on_periods = []
                            camera_on_timestamps = []  # List of datetime strings when camera was ON

                            # Debug: Log first participant's QoS structure
                            if page_count == 0 and participants.index(p) == 0 and user_qos:
                                sample_qos = user_qos[0]
                                print(f"[ZoomAPI] Sample QoS date_time: {sample_qos.get('date_time', 'NOT FOUND')}")
                                print(f"[ZoomAPI] Sample QoS video_output: {sample_qos.get('video_output', 'NOT FOUND')}")

                            for qos_entry in user_qos:
                                video_output = qos_entry.get('video_output', {})
                                # Try multiple field names for timestamp
                                datetime_qos = (
                                    qos_entry.get('date_time') or
                                    qos_entry.get('datetime') or
                                    qos_entry.get('time') or
                                    qos_entry.get('timestamp') or
                                    ''
                                )

                                # FIX: Check if video_output exists with resolution OR bitrate > 0
                                # bitrate can be 0 or "0" which would fail truthiness check
                                camera_is_on = False
                                if video_output:
                                    resolution = video_output.get('resolution', '')
                                    bitrate = video_output.get('bitrate', 0)
                                    # Camera ON if resolution exists OR bitrate > 0
                                    try:
                                        bitrate_val = int(bitrate) if bitrate else 0
                                    except (ValueError, TypeError):
                                        bitrate_val = 0
                                    camera_is_on = bool(resolution) or bitrate_val > 0

                                if camera_is_on:
                                    # Camera was ON during this period
                                    camera_on_periods.append({
                                        'datetime': datetime_qos,
                                        'bitrate': video_output.get('bitrate'),
                                        'resolution': video_output.get('resolution'),
                                        'frame_rate': video_output.get('frame_rate')
                                    })
                                    if datetime_qos:
                                        camera_on_timestamps.append(datetime_qos)

                            p['camera_on_periods'] = camera_on_periods
                            p['camera_on_count'] = len(camera_on_periods)
                            p['camera_on_timestamps'] = camera_on_timestamps

                            # Debug: Log first participant with camera data
                            if camera_on_periods and page_count == 0:
                                user_name = p.get('user_name', 'Unknown')
                                print(f"[ZoomAPI] {user_name}: {len(camera_on_periods)} camera periods, {len(camera_on_timestamps)} timestamps")
                                if camera_on_timestamps:
                                    print(f"[ZoomAPI] Sample timestamp: {camera_on_timestamps[0]}")

                            # Calculate actual camera ON duration from timestamps
                            camera_on_minutes = 0
                            if camera_on_timestamps and len(camera_on_timestamps) >= 2:
                                try:
                                    # Parse timestamps and calculate duration from intervals
                                    from datetime import datetime as dt
                                    parsed_times = []
                                    for ts in camera_on_timestamps:
                                        if isinstance(ts, str):
                                            ts = ts.replace('Z', '+00:00')
                                            if '.' in ts:
                                                parsed_times.append(dt.fromisoformat(ts.split('.')[0]))
                                            else:
                                                parsed_times.append(dt.fromisoformat(ts.replace('+00:00', '')))
                                    if parsed_times:
                                        parsed_times.sort()
                                        # Calculate total duration considering gaps > 2 min as breaks
                                        total_seconds = 0
                                        interval_start = parsed_times[0]
                                        prev_time = parsed_times[0]
                                        for curr_time in parsed_times[1:]:
                                            gap = (curr_time - prev_time).total_seconds()
                                            if gap > 120:  # Gap > 2 min = new interval
                                                total_seconds += (prev_time - interval_start).total_seconds() + 60  # Add 1 min for last sample
                                                interval_start = curr_time
                                            prev_time = curr_time
                                        # Add final interval
                                        total_seconds += (prev_time - interval_start).total_seconds() + 60
                                        camera_on_minutes = max(1, int(total_seconds / 60))
                                except Exception as e:
                                    print(f"[ZoomAPI] Error calculating camera duration: {e}")
                                    camera_on_minutes = len(camera_on_periods)  # Fallback
                            elif camera_on_periods:
                                camera_on_minutes = len(camera_on_periods)  # Fallback if only 1 sample

                            p['camera_on_minutes'] = camera_on_minutes

                        all_participants.extend(participants)
                        print(f"[ZoomAPI] QoS Page {page_count + 1}: {len(participants)} participants")

                    next_page_token = data.get('next_page_token', '')
                    page_count += 1

                    if not next_page_token:
                        break

                elif response.status_code == 400:
                    print(f"[ZoomAPI] QoS API: Bad request - {response.text[:200]}")
                    break
                elif response.status_code == 401:
                    auth_retries += 1
                    if auth_retries > max_auth_retries:
                        print(f"[ZoomAPI] QoS API: Too many 401 errors ({auth_retries}), giving up")
                        break
                    print(f"[ZoomAPI] QoS API: Unauthorized - refreshing token (attempt {auth_retries}/{max_auth_retries})")
                    self.access_token = None
                    token = self.get_access_token()
                    headers = {'Authorization': f'Bearer {token}'}
                    continue
                elif response.status_code == 403:
                    print(f"[ZoomAPI] QoS API: Forbidden - requires Business+ plan or dashboard_meetings:read:admin scope")
                    print(f"[ZoomAPI] Response: {response.text[:300]}")
                    break
                elif response.status_code == 404:
                    print(f"[ZoomAPI] QoS API: Meeting not found")
                    break
                else:
                    print(f"[ZoomAPI] QoS API: {response.status_code} - {response.text[:200]}")
                    break

            # Count participants with camera data and timestamps
            with_camera = sum(1 for p in all_participants if p.get('camera_on_count', 0) > 0)
            with_timestamps = sum(1 for p in all_participants if p.get('camera_on_timestamps'))
            print(f"[ZoomAPI] QoS: Got {len(all_participants)} participants, {with_camera} with camera, {with_timestamps} with timestamps")
            return all_participants

        except Exception as e:
            print(f"[ZoomAPI] QoS API error: {e}")
            traceback.print_exc()
            return []

zoom_api = ZoomAPI()


# ==============================================================================
# WEBHOOK EVENT HANDLERS
# ==============================================================================

def is_scout_bot(participant_name, participant_email):
    """Check if participant is the scout bot"""
    if participant_email and SCOUT_BOT_EMAIL:
        if participant_email.lower() == SCOUT_BOT_EMAIL.lower():
            return True
    if participant_name and SCOUT_BOT_NAME:
        if SCOUT_BOT_NAME.lower() in participant_name.lower():
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

    # Skip scout bot
    if is_scout_bot(p['participant_name'], p['participant_email']):
        print(f"  -> Scout bot joined, skipping event storage")
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

    # Skip scout bot
    if is_scout_bot(p['participant_name'], p['participant_email']):
        print(f"  -> Scout bot left, skipping")
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
    if room_uuid:
        room_name = meeting_state.get_room_name(room_uuid) or f'Room-{room_uuid[:8]}'
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

    success = insert_participant_event(event_data)
    print(f"  -> ROOM JOIN: {p['participant_name']} -> {room_name} {'[OK]' if success else '[FAILED]'}")


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


def safe_int(value, default=0):
    """Safely convert value to int, handling None and empty strings"""
    if value is None or value == '':
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def safe_str(value, default=''):
    """Safely convert value to string, handling None"""
    if value is None:
        return default
    return str(value).strip() if value else default


def handle_meeting_ended(data):
    """Handle meeting ended - collect final QoS data"""
    payload = data.get('payload', {})
    obj = payload.get('object', {})
    meeting_uuid = obj.get('uuid', '')
    meeting_id = str(obj.get('id', ''))

    print(f"[Meeting] Meeting ended: {meeting_uuid}")
    print(f"[Meeting] Meeting ID: {meeting_id}")

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
        'version': '2.0.0',
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
# MONITOR MODE - SDK Polling (replaces calibration)
# SDK getBreakoutRoomList() returns room names + participants directly.
# No UUID mapping needed. React app polls every 30s and sends snapshots here.
# ==============================================================================

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

    now = datetime.utcnow()
    today = get_ist_date()
    snapshot_time = now.isoformat()

    rows = []
    total_participants = 0

    for room in rooms:
        room_name = room.get('room_name', '')
        if not room_name:
            continue

        participants = room.get('participants', [])
        for p in participants:
            p_name = p.get('name', '') or p.get('participant_name', '') or ''
            p_email = p.get('email', '') or p.get('participant_email', '') or ''
            p_uuid = p.get('uuid', '') or p.get('participant_uuid', '') or ''

            # Skip Scout Bot itself
            if 'scout' in p_name.lower() and 'bot' in p_name.lower():
                continue

            rows.append({
                'snapshot_id': str(uuid_lib.uuid4()),
                'snapshot_time': snapshot_time,
                'event_date': today,
                'meeting_id': str(meeting_id),
                'room_name': room_name,
                'participant_name': p_name,
                'participant_email': p_email,
                'participant_uuid': p_uuid
            })
            total_participants += 1

    if rows:
        try:
            client = get_bq_client()
            table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots"
            errors = client.insert_rows_json(table_id, rows)
            if errors:
                print(f"[Monitor] BigQuery insert errors: {errors[:3]}")
                return jsonify({'success': False, 'error': str(errors[:3])}), 500
            print(f"[Monitor] Saved snapshot: {len(rooms)} rooms, {total_participants} participants")
        except Exception as e:
            print(f"[Monitor] BigQuery error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({
        'success': True,
        'rooms': len(rooms),
        'participants': total_participants,
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
          COUNT(DISTINCT COALESCE(NULLIF(participant_email, ''), participant_name)) as participant_count,
          MIN(snapshot_time) as first_snapshot,
          MAX(snapshot_time) as last_snapshot
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots`
        WHERE event_date = '{today}'
        """
        result = list(client.query(query).result())
        row = result[0] if result else {}
        return jsonify({
            'success': True,
            'date': today,
            'snapshots': row.get('snapshot_count', 0),
            'rooms': row.get('room_count', 0),
            'participants': row.get('participant_count', 0),
            'first_snapshot': str(row.get('first_snapshot', '')),
            'last_snapshot': str(row.get('last_snapshot', ''))
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
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots`
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
          COUNT(DISTINCT COALESCE(NULLIF(participant_email, ''), participant_name)) as participant_count,
          MIN(snapshot_time) as first_snapshot,
          MAX(snapshot_time) as last_snapshot,
          TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(snapshot_time), SECOND) as seconds_since_last
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots`
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

        return jsonify({
            'status': status,
            'message': message,
            'date': today,
            'snapshots_today': snapshot_count,
            'rooms_seen': row.get('room_count', 0) or 0,
            'participants_seen': row.get('participant_count', 0) or 0,
            'first_snapshot': str(row.get('first_snapshot', '')),
            'last_snapshot': str(row.get('last_snapshot', '')),
            'seconds_since_last': seconds_since,
            'is_meeting_hours': is_meeting_hours,
            'needs_attention': should_alert
        })
    except Exception as e:
        return jsonify({
            'status': 'ERROR',
            'message': str(e)
        }), 500


# ═══════════════════════════════════════════════════════
# SCOUT BOT HEALTH ALERT (called by Cloud Scheduler)
# ═══════════════════════════════════════════════════════

# Cooldown: don't send more than 1 alert per 30 minutes
_alert_state = {'last_sent': 0, 'last_status': 'HEALTHY'}

@app.route('/monitor/alert', methods=['POST', 'GET'])
def monitor_alert():
    """
    Checks snapshot health and sends email alert to HR if Scout Bot
    appears to have left the meeting or stopped sending snapshots.

    Called by Cloud Scheduler every 5 minutes during meeting hours.
    Has 30-minute cooldown to avoid email spam.

    Alerts when:
      - STALE: No snapshot for >5 minutes (bot may have disconnected)
      - NO_DATA: No snapshots at all today (bot never joined)

    Also alerts when bot RECOVERS (was down, now healthy again).
    """
    import time

    today = get_ist_date()
    ist_now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    ist_hour = ist_now.hour
    ist_time_str = ist_now.strftime('%H:%M IST')

    # Only alert during meeting hours (9 AM - 8 PM IST)
    if not (9 <= ist_hour <= 20):
        return jsonify({
            'action': 'skipped',
            'reason': f'Outside meeting hours ({ist_time_str})',
            'alert_sent': False
        })

    try:
        client = get_bq_client()
        query = f"""
        SELECT
          COUNT(DISTINCT snapshot_time) as snapshot_count,
          COUNT(DISTINCT COALESCE(NULLIF(participant_email, ''), participant_name)) as participant_count,
          MAX(snapshot_time) as last_snapshot,
          TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(snapshot_time), SECOND) as seconds_since_last,
          MAX(participant_name) as last_participant_seen
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots`
        WHERE event_date = '{today}'
        """
        result = list(client.query(query).result())
        row = result[0] if result else {}

        snapshot_count = row.get('snapshot_count', 0) or 0
        seconds_since = row.get('seconds_since_last', None)
        participant_count = row.get('participant_count', 0) or 0

        # Determine status
        if snapshot_count == 0:
            status = 'NO_DATA'
            problem = 'No snapshots received today. Scout Bot may not have joined the meeting.'
            action = 'Check if the Scout Bot VM is running and the scheduled task triggered correctly.'
        elif seconds_since is not None and seconds_since <= 300:
            status = 'HEALTHY'
            problem = None
            action = None
        else:
            status = 'STALE'
            mins_ago = int(seconds_since / 60) if seconds_since else '?'
            problem = f'Last snapshot was {mins_ago} minutes ago. Scout Bot may have left the meeting or the Zoom App crashed.'
            action = 'Check the Scout Bot VM. The bot may need to rejoin the meeting, or the Zoom App needs to be reopened.'

        now = time.time()
        cooldown = 1800  # 30 minutes
        prev_status = _alert_state['last_status']

        # Send alert if: problem detected AND cooldown expired
        # Also send recovery notification if was down and now healthy
        alert_sent = False
        alert_type = None

        if status in ('STALE', 'NO_DATA') and (now - _alert_state['last_sent']) > cooldown:
            # Send problem alert
            alert_sent = _send_scout_alert(
                date=today,
                status=status,
                time_str=ist_time_str,
                problem=problem,
                action=action,
                snapshot_count=snapshot_count,
                participant_count=participant_count,
                seconds_since=seconds_since,
                alert_type='problem'
            )
            if alert_sent:
                _alert_state['last_sent'] = now
                alert_type = 'problem'

        elif status == 'HEALTHY' and prev_status in ('STALE', 'NO_DATA'):
            # Send recovery notification
            alert_sent = _send_scout_alert(
                date=today,
                status='RECOVERED',
                time_str=ist_time_str,
                problem='Scout Bot is back online! Snapshots are being received again.',
                action='No action needed. Monitoring has resumed normally.',
                snapshot_count=snapshot_count,
                participant_count=participant_count,
                seconds_since=seconds_since,
                alert_type='recovery'
            )
            alert_type = 'recovery'

        _alert_state['last_status'] = status

        return jsonify({
            'status': status,
            'date': today,
            'time': ist_time_str,
            'snapshots_today': snapshot_count,
            'participants_today': participant_count,
            'seconds_since_last': seconds_since,
            'alert_sent': alert_sent,
            'alert_type': alert_type,
            'cooldown_remaining': max(0, int(cooldown - (now - _alert_state['last_sent'])))
        })

    except Exception as e:
        print(f"[MonitorAlert] Error: {e}")
        traceback.print_exc()
        return jsonify({
            'status': 'ERROR',
            'message': str(e),
            'alert_sent': False
        }), 500


def _send_scout_alert(date, status, time_str, problem, action, snapshot_count, participant_count, seconds_since, alert_type='problem'):
    """Send Scout Bot health alert email via SendGrid"""
    if not SENDGRID_API_KEY or not REPORT_EMAIL_TO:
        print(f"[ScoutAlert] SendGrid not configured, skipping {alert_type} alert")
        return False

    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail, Email, To, Content

        sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)

        if alert_type == 'recovery':
            emoji = '\u2705'
            color = '#059669'
            bg_color = '#ecfdf5'
            border_color = '#a7f3d0'
            subject = f"{emoji} Scout Bot Recovered - {date} {time_str}"
        else:
            emoji = '\U0001F6A8' if status == 'NO_DATA' else '\u26A0\uFE0F'
            color = '#dc2626' if status == 'NO_DATA' else '#d97706'
            bg_color = '#fef2f2' if status == 'NO_DATA' else '#fffbeb'
            border_color = '#fecaca' if status == 'NO_DATA' else '#fde68a'
            subject = f"{emoji} Scout Bot Alert: {status} - {date} {time_str}"

        mins_ago = int(seconds_since / 60) if seconds_since else 'N/A'
        action_emoji = '\U0001F527' if alert_type != 'recovery' else '\u2705'
        quick_fix_html = '<div style="margin-top: 16px;"><h4 style="color: #1e293b; margin: 0 0 8px; font-size: 13px;">Quick Fix Steps:</h4><ol style="color: #475569; font-size: 13px; line-height: 1.8; padding-left: 20px;"><li>SSH into Scout Bot VM: <code style="background: #e2e8f0; padding: 2px 6px; border-radius: 4px;">34.47.178.82</code></li><li>Check if Zoom is running and the bot is in the meeting</li><li>If not, restart the scheduled task or manually join the meeting</li><li>Open the Zoom App to restart snapshot monitoring</li></ol></div>' if alert_type != 'recovery' else ''

        html_content = f"""
        <div style="font-family: 'Inter', Arial, sans-serif; max-width: 600px; margin: 0 auto;">
          <div style="background: linear-gradient(135deg, #0f2847, #1a365d); padding: 24px 30px; border-radius: 12px 12px 0 0;">
            <h1 style="color: #fff; margin: 0; font-size: 20px;">
              {emoji} Scout Bot Monitor
            </h1>
            <p style="color: rgba(255,255,255,0.7); margin: 4px 0 0; font-size: 13px;">
              Verve Advisory - Attendance Tracker
            </p>
          </div>

          <div style="background: {bg_color}; border: 1px solid {border_color}; border-top: none; padding: 20px 30px;">
            <h2 style="color: {color}; margin: 0 0 8px; font-size: 18px;">
              Status: {status}
            </h2>
            <p style="color: #374151; margin: 0; font-size: 14px; line-height: 1.6;">
              {problem}
            </p>
          </div>

          <div style="background: #fff; border: 1px solid #e5e7eb; border-top: none; padding: 20px 30px;">
            <h3 style="color: #1e293b; margin: 0 0 12px; font-size: 14px;">Details</h3>
            <table style="width: 100%; font-size: 13px; border-collapse: collapse;">
              <tr><td style="padding: 6px 0; color: #64748b;">Date</td><td style="padding: 6px 0; font-weight: 600;">{date}</td></tr>
              <tr><td style="padding: 6px 0; color: #64748b;">Time of Check</td><td style="padding: 6px 0; font-weight: 600;">{time_str}</td></tr>
              <tr><td style="padding: 6px 0; color: #64748b;">Snapshots Today</td><td style="padding: 6px 0; font-weight: 600;">{snapshot_count}</td></tr>
              <tr><td style="padding: 6px 0; color: #64748b;">Participants Seen</td><td style="padding: 6px 0; font-weight: 600;">{participant_count}</td></tr>
              <tr><td style="padding: 6px 0; color: #64748b;">Last Snapshot</td><td style="padding: 6px 0; font-weight: 600;">{mins_ago} min ago</td></tr>
            </table>
          </div>

          <div style="background: #f8fafc; border: 1px solid #e5e7eb; border-top: none; padding: 20px 30px; border-radius: 0 0 12px 12px;">
            <h3 style="color: #1e293b; margin: 0 0 8px; font-size: 14px;">{action_emoji} Action Required</h3>
            <p style="color: #475569; margin: 0; font-size: 13px; line-height: 1.6;">{action}</p>

            {quick_fix_html}

            <div style="margin-top: 16px; padding-top: 16px; border-top: 1px solid #e2e8f0;">
              <a href="https://breakout-room-calibrator-1041741270489.us-central1.run.app/monitor/health"
                 style="display: inline-block; padding: 8px 20px; background: #1a365d; color: #fff; text-decoration: none; border-radius: 8px; font-size: 13px; font-weight: 600;">
                Check Health Dashboard
              </a>
              <a href="https://verve-attendance-tracker.vercel.app"
                 style="display: inline-block; padding: 8px 20px; background: #fff; color: #1a365d; text-decoration: none; border-radius: 8px; font-size: 13px; font-weight: 600; border: 1px solid #d1d5db; margin-left: 8px;">
                Open Attendance Tracker
              </a>
            </div>
          </div>

          <p style="color: #94a3b8; font-size: 11px; text-align: center; margin-top: 16px;">
            Automated alert from Verve Attendance Tracker. Checks run every 5 minutes during 9 AM - 8 PM IST.
          </p>
        </div>
        """

        recipients = [r.strip() for r in REPORT_EMAIL_TO.replace(';', ',').split(',') if r.strip()]

        mail = Mail(
            from_email=Email(REPORT_EMAIL_FROM),
            to_emails=[To(r) for r in recipients],
            subject=subject,
            html_content=Content("text/html", html_content)
        )

        response = sg.send(mail)
        print(f"[ScoutAlert] {alert_type} email sent to {recipients}, status: {response.status_code}")
        return response.status_code == 202

    except Exception as e:
        print(f"[ScoutAlert] Failed to send email: {e}")
        traceback.print_exc()
        return False


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
        # URL validation events don't have these headers
        return True, None

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

    # Validate webhook signature (security)
    valid, error = validate_webhook_signature(request)
    if not valid:
        return jsonify({'error': error}), 401

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

    print(f"\n{'='*60}")
    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] WEBHOOK EVENT: {event}")
    print(f"{'='*60}")

    # Log raw payload for debugging (first 500 chars)
    raw_str = json.dumps(data)
    if len(raw_str) > 500:
        print(f"[Webhook] Payload (truncated): {raw_str[:500]}...")
    else:
        print(f"[Webhook] Payload: {raw_str}")

    # Handle URL validation
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

    # CHECK FOR INCOMPLETE CALIBRATION (Auto-resume support)
    resume_from = 0
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

        # Send email alert if critical and alerts enabled
        alert_sent = False
        if health_status == 'CRITICAL' and send_alert and SENDGRID_API_KEY:
            try:
                alert_sent = send_calibration_alert(
                    target_date,
                    health_status,
                    message,
                    {
                        'total_mappings': total_mappings,
                        'sequence_mappings': sequence_mappings,
                        'sdk_mappings': sdk_mappings,
                        'current_room': current_room,
                        'total_rooms': total_rooms,
                        'calibration_started': calibration_started,
                        'calibration_completed': calibration_completed
                    }
                )
            except Exception as e:
                print(f"[CalibrationHealth] Alert send error: {e}")

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


def send_calibration_alert(date, status, message, details):
    """Send email alert for calibration issues via SendGrid"""
    if not SENDGRID_API_KEY or not REPORT_EMAIL_TO:
        print("[CalibrationAlert] SendGrid not configured, skipping alert")
        return False

    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail, Email, To, Content

        sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)

        subject = f"[ALERT] Zoom Calibration {status} - {date}"

        html_content = f"""
        <h2>Zoom Breakout Room Calibration Alert</h2>
        <p><strong>Status:</strong> <span style="color: {'red' if status == 'CRITICAL' else 'orange'}">{status}</span></p>
        <p><strong>Date:</strong> {date}</p>
        <p><strong>Message:</strong> {message}</p>

        <h3>Details</h3>
        <ul>
            <li>Calibration Started: {details.get('calibration_started', False)}</li>
            <li>Calibration Completed: {details.get('calibration_completed', False)}</li>
            <li>Rooms Progress: {details.get('current_room', 0)} / {details.get('total_rooms', 0)}</li>
            <li>Webhook UUID Mappings: {details.get('sequence_mappings', 0)}</li>
            <li>SDK-only Mappings: {details.get('sdk_mappings', 0)}</li>
        </ul>

        <h3>Action Required</h3>
        <p>Please run calibration by:</p>
        <ol>
            <li>Open Zoom meeting with Scout Bot</li>
            <li>Open the Zoom App calibration panel</li>
            <li>Click "Move Scout Bot" to auto-calibrate all rooms</li>
        </ol>

        <p style="color: gray; font-size: 12px;">
        This alert was sent by the Zoom Breakout Room Tracker system.
        </p>
        """

        recipients = [r.strip() for r in REPORT_EMAIL_TO.replace(';', ',').split(',') if r.strip()]

        mail = Mail(
            from_email=Email(REPORT_EMAIL_FROM),
            to_emails=[To(r) for r in recipients],
            subject=subject,
            html_content=Content("text/html", html_content)
        )

        response = sg.send(mail)
        print(f"[CalibrationAlert] Email sent to {recipients}, status: {response.status_code}")
        return response.status_code == 202

    except Exception as e:
        print(f"[CalibrationAlert] Failed to send email: {e}")
        traceback.print_exc()
        return False


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
            FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.participant_events`
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
          AND source IN ('webhook_calibration', 'pending_move_calibration', 'sequence_calibration')
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
      FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.participant_events`
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
    Real-time: Who's in which room RIGHT NOW.
    Returns latest snapshot data grouped by room.

    GET /attendance/live
    GET /attendance/live?date=2026-04-03
    """
    target_date = request.args.get('date', get_ist_date())
    try:
        target_date = validate_date_format(target_date)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    try:
        client = get_bq_client()
        query = f"""
        WITH latest_snapshot AS (
          SELECT MAX(snapshot_time) as max_time
          FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots`
          WHERE event_date = '{target_date}'
        ),
        -- All room names seen during the entire day
        all_rooms AS (
          SELECT DISTINCT room_name
          FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots`
          WHERE event_date = '{target_date}'
            AND room_name IS NOT NULL AND room_name != ''
        ),
        -- Who is in each room at the latest snapshot
        current_state AS (
          SELECT
            s.room_name,
            s.participant_name,
            s.participant_email,
            s.snapshot_time
          FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots` s
          CROSS JOIN latest_snapshot ls
          WHERE s.event_date = '{target_date}'
            AND s.snapshot_time = ls.max_time
            AND s.participant_name NOT LIKE '%Scout%'
        )
        SELECT
          ar.room_name,
          ARRAY_AGG(
            STRUCT(cs.participant_name, cs.participant_email)
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

        return jsonify({
            'success': True,
            'date': target_date,
            'snapshot_time': snapshot_time,
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
@app.route('/attendance/summary/<date>', methods=['GET'])
def attendance_summary(date=None):
    """
    Full attendance for a date - includes Main Room time from webhooks.
    Combines webhook join/leave data with SDK room snapshots.

    GET /attendance/summary/2026-04-03
    """
    if date is None:
        date = request.args.get('date', get_ist_date())
    try:
        date = validate_date_format(date)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    try:
        client = get_bq_client()
        query = f"""
        WITH
        -- Webhook events: main meeting join/leave
        webhook_events AS (
          SELECT
            LOWER(TRIM(participant_name)) as participant_key,
            participant_name,
            COALESCE(NULLIF(participant_email, ''), '') as participant_email,
            event_type,
            event_timestamp
          FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.participant_events`
          WHERE event_date = '{date}'
            AND participant_name IS NOT NULL AND participant_name != ''
            AND LOWER(participant_name) NOT LIKE '%scout%'
            AND event_type IN ('participant_joined', 'participant_left')
        ),
        -- Main meeting times from webhooks
        webhook_times AS (
          SELECT
            participant_key,
            MAX(participant_name) as participant_name,
            MAX(participant_email) as participant_email,
            MIN(CASE WHEN event_type = 'participant_joined' THEN event_timestamp END) as main_joined,
            MAX(CASE WHEN event_type = 'participant_left' THEN event_timestamp END) as main_left
          FROM webhook_events
          GROUP BY participant_key
        ),
        -- Clean snapshots (breakout rooms only)
        snapshot_clean AS (
          SELECT
            LOWER(TRIM(participant_name)) as participant_key,
            participant_name,
            COALESCE(NULLIF(participant_email, ''), '') as participant_email,
            room_name,
            snapshot_time
          FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots`
          WHERE event_date = '{date}'
            AND participant_name IS NOT NULL AND participant_name != ''
            AND room_name IS NOT NULL AND room_name != ''
            AND LOWER(participant_name) NOT LIKE '%scout%'
        ),
        -- Per-participant first/last seen in breakout rooms
        participant_breakout_times AS (
          SELECT
            participant_key,
            MAX(participant_name) as participant_name,
            MAX(participant_email) as participant_email,
            MIN(snapshot_time) as first_breakout,
            MAX(snapshot_time) as last_breakout
          FROM snapshot_clean
          GROUP BY participant_key
        ),
        -- Combine webhook and snapshot participants
        all_participants AS (
          SELECT
            COALESCE(w.participant_key, s.participant_key) as participant_key,
            COALESCE(w.participant_name, s.participant_name) as participant_name,
            COALESCE(NULLIF(w.participant_email, ''), s.participant_email, '') as participant_email,
            w.main_joined,
            w.main_left,
            s.first_breakout,
            s.last_breakout
          FROM webhook_times w
          FULL OUTER JOIN participant_breakout_times s ON w.participant_key = s.participant_key
        ),
        -- Detect room transitions
        snapshot_transitions AS (
          SELECT *,
            LAG(room_name) OVER (
              PARTITION BY participant_key ORDER BY snapshot_time
            ) as prev_room
          FROM snapshot_clean
        ),
        visit_groups AS (
          SELECT *,
            SUM(CASE WHEN prev_room IS NULL OR room_name != prev_room THEN 1 ELSE 0 END)
              OVER (PARTITION BY participant_key ORDER BY snapshot_time) as visit_id
          FROM snapshot_transitions
        ),
        -- Breakout room visits with duration
        breakout_visits AS (
          SELECT
            participant_key,
            room_name,
            MIN(snapshot_time) as room_join_time,
            MAX(snapshot_time) as room_leave_time,
            TIMESTAMP_DIFF(MAX(snapshot_time), MIN(snapshot_time), MINUTE) as room_duration_mins,
            visit_id
          FROM visit_groups
          GROUP BY participant_key, room_name, visit_id
        ),
        -- Re-merge consecutive same-room visits
        remerge AS (
          SELECT *,
            LAG(room_name) OVER (PARTITION BY participant_key ORDER BY room_join_time) as prev_room_name
          FROM breakout_visits
        ),
        remerge_groups AS (
          SELECT *,
            SUM(CASE WHEN prev_room_name IS NULL OR room_name != prev_room_name THEN 1 ELSE 0 END)
              OVER (PARTITION BY participant_key ORDER BY room_join_time) as merge_group
          FROM remerge
        ),
        breakout_visits_final AS (
          SELECT
            participant_key,
            room_name,
            MIN(room_join_time) as join_time,
            MAX(room_leave_time) as leave_time,
            TIMESTAMP_DIFF(MAX(room_leave_time), MIN(room_join_time), MINUTE) as duration_mins
          FROM remerge_groups
          GROUP BY participant_key, room_name, merge_group
        ),
        -- Calculate Main Room time (time in meeting but NOT in breakout rooms)
        main_room_time AS (
          SELECT
            ap.participant_key,
            ap.participant_name,
            ap.participant_email,
            ap.main_joined,
            COALESCE(ap.main_left, ap.last_breakout, ap.main_joined) as main_left,
            ap.first_breakout,
            ap.last_breakout,
            -- Main room time BEFORE first breakout
            CASE
              WHEN ap.main_joined IS NOT NULL AND ap.first_breakout IS NOT NULL
              THEN GREATEST(0, TIMESTAMP_DIFF(ap.first_breakout, ap.main_joined, MINUTE))
              WHEN ap.main_joined IS NOT NULL AND ap.first_breakout IS NULL AND ap.main_left IS NOT NULL
              THEN GREATEST(0, TIMESTAMP_DIFF(ap.main_left, ap.main_joined, MINUTE))
              ELSE 0
            END as main_room_before_mins,
            -- Main room time AFTER last breakout
            CASE
              WHEN ap.last_breakout IS NOT NULL AND ap.main_left IS NOT NULL
              THEN GREATEST(0, TIMESTAMP_DIFF(ap.main_left, ap.last_breakout, MINUTE))
              ELSE 0
            END as main_room_after_mins
          FROM all_participants ap
        ),
        -- Build main room visit records
        main_room_visits AS (
          SELECT
            participant_key,
            '0.Main Room' as room_name,
            main_joined as join_time,
            COALESCE(first_breakout, main_left) as leave_time,
            main_room_before_mins as duration_mins,
            0 as visit_order
          FROM main_room_time
          WHERE main_room_before_mins > 0 AND main_joined IS NOT NULL

          UNION ALL

          SELECT
            participant_key,
            '0.Main Room' as room_name,
            last_breakout as join_time,
            main_left as leave_time,
            main_room_after_mins as duration_mins,
            999 as visit_order
          FROM main_room_time
          WHERE main_room_after_mins > 0 AND last_breakout IS NOT NULL AND main_left IS NOT NULL
        ),
        -- Combine all room visits (main + breakout)
        all_room_visits AS (
          SELECT participant_key, room_name, join_time, leave_time, duration_mins
          FROM breakout_visits_final
          WHERE duration_mins > 0

          UNION ALL

          SELECT participant_key, room_name, join_time, leave_time, duration_mins
          FROM main_room_visits
          WHERE duration_mins > 0
        )
        SELECT
          mrt.participant_name as name,
          mrt.participant_email as email,
          FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(COALESCE(mrt.main_joined, mrt.first_breakout), INTERVAL 330 MINUTE)) as first_seen_ist,
          FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(COALESCE(mrt.main_left, mrt.last_breakout), INTERVAL 330 MINUTE)) as last_seen_ist,
          COALESCE(mrt.main_joined, mrt.first_breakout) as sort_time,
          COALESCE((SELECT SUM(duration_mins) FROM all_room_visits arv WHERE arv.participant_key = mrt.participant_key), 0) as total_duration_mins,
          ARRAY(
            SELECT AS STRUCT
              room_name,
              FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(join_time, INTERVAL 330 MINUTE)) as room_joined_ist,
              FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(leave_time, INTERVAL 330 MINUTE)) as room_left_ist,
              duration_mins as room_duration_mins
            FROM all_room_visits arv
            WHERE arv.participant_key = mrt.participant_key
            ORDER BY join_time
          ) as room_visits
        FROM main_room_time mrt
        WHERE mrt.participant_name IS NOT NULL
        ORDER BY mrt.participant_name
        """
        results = list(client.query(query).result())

        participants = []
        for row in results:
            visits = [dict(v) for v in row.get('room_visits', []) if v.get('room_name')]
            participants.append({
                'name': row.get('name', ''),
                'email': row.get('email', ''),
                'first_seen_ist': row.get('first_seen_ist', ''),
                'last_seen_ist': row.get('last_seen_ist', ''),
                'total_duration_mins': row.get('total_duration_mins', 0),
                'room_visits': visits
            })

        return jsonify({
            'success': True,
            'date': date,
            'generated_at': datetime.utcnow().isoformat(),
            'total_participants': len(participants),
            'participants': participants
        })

    except Exception as e:
        print(f"[Attendance] Summary error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


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
        date = request.args.get('date', get_ist_date())
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
        query = f"""
        WITH snapshots AS (
          SELECT
            room_name,
            participant_name,
            snapshot_time,
            TIMESTAMP_ADD(snapshot_time, INTERVAL 330 MINUTE) as snapshot_ist
          FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots`
          WHERE event_date = '{date}'
            AND participant_name NOT LIKE '%Scout%'
            AND room_name IS NOT NULL AND room_name != ''
        ),
        -- Bucket each snapshot into time slots
        time_bucketed AS (
          SELECT
            room_name,
            participant_name,
            TIMESTAMP_TRUNC(snapshot_ist, MINUTE) as snapshot_min,
            FORMAT_TIMESTAMP('%H:%M',
              TIMESTAMP_SECONDS(
                DIV(UNIX_SECONDS(TIMESTAMP_TRUNC(snapshot_ist, MINUTE)), {interval} * 60) * {interval} * 60
              )
            ) as time_slot
          FROM snapshots
        ),
        -- Count distinct participants per room per slot
        room_slot_counts AS (
          SELECT
            room_name,
            time_slot,
            COUNT(DISTINCT participant_name) as participant_count
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


@app.route('/teams', methods=['POST'])
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
def add_team_member(team_id):
    """Add a member to a team"""
    try:
        ensure_team_tables_once()
        data = request.json or {}
        participant_name = (data.get('participant_name') or '').strip()
        if not participant_name:
            return jsonify({'success': False, 'error': 'participant_name is required'}), 400

        participant_email = (data.get('participant_email') or '').strip()
        member_id = str(uuid_lib.uuid4())

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"
        table_ref = f"{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}"

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


@app.route('/teams/participants', methods=['GET'])
def list_known_participants():
    """Get distinct participants from recent snapshots (for adding to teams)"""
    try:
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        query = f"""
        SELECT DISTINCT participant_name, participant_email
        FROM `{dataset_ref}.room_snapshots`
        WHERE SAFE.PARSE_DATE('%Y-%m-%d', event_date) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
          AND LOWER(participant_name) NOT LIKE '%scout%'
          AND participant_name IS NOT NULL AND participant_name != ''
        ORDER BY participant_name
        """
        rows = list(client.query(query).result())
        participants = [{'participant_name': r.participant_name,
                         'participant_email': r.participant_email or ''}
                        for r in rows]
        return jsonify({'success': True, 'participants': participants})
    except Exception as e:
        print(f"[Teams] Participants list error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# TEAM ATTENDANCE - Break time, Isolation, Team-wise view
# ==============================================================================

@app.route('/teams/<team_id>/attendance/<date>', methods=['GET'])
def team_attendance(team_id, date):
    """Get team attendance for a specific date with break & isolation time"""
    try:
        ensure_team_tables_once()
        report_date = validate_date_format(date)
        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        # Big query: get team info, members, and their attendance from snapshots
        query = f"""
        WITH team_info AS (
            SELECT team_id, team_name, manager_name
            FROM `{dataset_ref}.{BQ_TEAMS_TABLE}`
            WHERE team_id = @team_id
        ),
        team_members AS (
            SELECT participant_name, participant_email
            FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}`
            WHERE team_id = @team_id
        ),
        clean_snapshots AS (
            SELECT
                s.snapshot_time,
                s.participant_name,
                s.participant_email,
                s.room_name,
                TIMESTAMP_ADD(s.snapshot_time, INTERVAL 330 MINUTE) as snapshot_ist
            FROM `{dataset_ref}.room_snapshots` s
            INNER JOIN team_members tm
                ON LOWER(TRIM(s.participant_name)) = LOWER(TRIM(tm.participant_name))
            WHERE s.event_date = @report_date
              AND s.room_name IS NOT NULL AND s.room_name != ''
              AND s.participant_name IS NOT NULL AND s.participant_name != ''
        ),
        -- Per-participant summary
        participant_summary AS (
            SELECT
                cs.participant_name,
                cs.participant_email,
                MIN(cs.snapshot_ist) as first_seen,
                MAX(cs.snapshot_ist) as last_seen,
                TIMESTAMP_DIFF(MAX(cs.snapshot_ist), MIN(cs.snapshot_ist), MINUTE) as total_active_mins,
                COUNT(DISTINCT cs.snapshot_time) as snapshot_count
            FROM clean_snapshots cs
            GROUP BY cs.participant_name, cs.participant_email
        ),
        -- Break detection: find gaps where participant was NOT seen
        participant_snapshots AS (
            SELECT
                cs.participant_name,
                cs.snapshot_time,
                LAG(cs.snapshot_time) OVER (PARTITION BY cs.participant_name ORDER BY cs.snapshot_time) as prev_snapshot
            FROM clean_snapshots cs
        ),
        break_gaps AS (
            SELECT
                participant_name,
                TIMESTAMP_DIFF(snapshot_time, prev_snapshot, SECOND) as gap_seconds
            FROM participant_snapshots
            WHERE prev_snapshot IS NOT NULL
              AND TIMESTAMP_DIFF(snapshot_time, prev_snapshot, SECOND) > 60
        ),
        break_summary AS (
            SELECT
                participant_name,
                SUM(CASE WHEN gap_seconds > 60 THEN gap_seconds - 30 ELSE 0 END) as total_break_seconds,
                COUNT(CASE WHEN gap_seconds > 60 THEN 1 END) as break_count
            FROM break_gaps
            GROUP BY participant_name
        ),
        -- Isolation: times when participant was alone in their room
        room_occupancy AS (
            SELECT
                snapshot_time,
                room_name,
                COUNT(DISTINCT participant_name) as occupant_count
            FROM `{dataset_ref}.room_snapshots`
            WHERE event_date = @report_date
              AND room_name IS NOT NULL AND room_name != ''
              AND participant_name IS NOT NULL AND participant_name != ''
              AND LOWER(participant_name) NOT LIKE '%scout%'
            GROUP BY snapshot_time, room_name
        ),
        isolation_snapshots AS (
            SELECT
                cs.participant_name,
                cs.snapshot_time,
                cs.room_name
            FROM clean_snapshots cs
            INNER JOIN room_occupancy ro
                ON cs.snapshot_time = ro.snapshot_time AND cs.room_name = ro.room_name
            WHERE ro.occupant_count = 1
        ),
        isolation_summary AS (
            SELECT
                participant_name,
                COUNT(*) * 30 as isolation_seconds
            FROM isolation_snapshots
            GROUP BY participant_name
        ),

        -- Main meeting time from webhooks (participant_joined / participant_left)
        webhook_times AS (
            SELECT
                participant_name,
                MIN(CASE WHEN event_type IN ('participant_joined', 'meeting.participant_joined')
                    THEN TIMESTAMP_ADD(CAST(event_timestamp AS TIMESTAMP), INTERVAL 330 MINUTE) END) as meeting_joined,
                MAX(CASE WHEN event_type IN ('participant_left', 'meeting.participant_left')
                    THEN TIMESTAMP_ADD(CAST(event_timestamp AS TIMESTAMP), INTERVAL 330 MINUTE) END) as meeting_left
            FROM `{dataset_ref}.{BQ_EVENTS_TABLE}`
            WHERE event_date = @report_date
              AND participant_name IS NOT NULL AND participant_name != ''
              AND LOWER(participant_name) NOT LIKE '%scout%'
            GROUP BY participant_name
        )

        SELECT
            ps.participant_name,
            ps.participant_email,
            FORMAT_TIMESTAMP('%H:%M', ps.first_seen) as first_seen_ist,
            FORMAT_TIMESTAMP('%H:%M', ps.last_seen) as last_seen_ist,
            ps.total_active_mins,
            ps.snapshot_count,
            COALESCE(bs.total_break_seconds, 0) as break_seconds,
            COALESCE(bs.break_count, 0) as break_count,
            COALESCE(iso.isolation_seconds, 0) as isolation_seconds,
            FORMAT_TIMESTAMP('%H:%M', wt.meeting_joined) as meeting_joined_ist,
            FORMAT_TIMESTAMP('%H:%M', wt.meeting_left) as meeting_left_ist,
            CASE WHEN wt.meeting_joined IS NOT NULL AND wt.meeting_left IS NOT NULL
                 THEN TIMESTAMP_DIFF(wt.meeting_left, wt.meeting_joined, MINUTE)
                 ELSE 0 END as meeting_duration_mins,
            CASE WHEN wt.meeting_joined IS NOT NULL AND wt.meeting_left IS NOT NULL
                 THEN GREATEST(TIMESTAMP_DIFF(wt.meeting_left, wt.meeting_joined, MINUTE) - ps.total_active_mins, 0)
                 ELSE 0 END as main_room_mins
        FROM participant_summary ps
        LEFT JOIN break_summary bs ON ps.participant_name = bs.participant_name
        LEFT JOIN isolation_summary iso ON ps.participant_name = iso.participant_name
        LEFT JOIN webhook_times wt ON LOWER(TRIM(ps.participant_name)) = LOWER(TRIM(wt.participant_name))
        ORDER BY ps.participant_name
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
                bigquery.ScalarQueryParameter("report_date", "STRING", report_date)
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())

        # Get team info
        team_q = f"""
        SELECT team_name, manager_name FROM `{dataset_ref}.{BQ_TEAMS_TABLE}` WHERE team_id = @team_id
        """
        team_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]
        )
        team_rows = list(client.query(team_q, job_config=team_config).result())
        team_name = team_rows[0].team_name if team_rows else 'Unknown'
        manager_name = team_rows[0].manager_name if team_rows else ''

        # Get all team member names for "absent" detection
        members_q = f"""
        SELECT participant_name, participant_email
        FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` WHERE team_id = @team_id
        """
        all_members = list(client.query(members_q, job_config=team_config).result())
        present_names = {r.participant_name.lower().strip() for r in rows}

        # Also get webhook-only participants (those in main meeting but never in breakout rooms)
        webhook_only_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
                bigquery.ScalarQueryParameter("report_date", "STRING", report_date)
            ]
        )
        webhook_only_q = f"""
        SELECT
            pe.participant_name,
            tm.participant_email,
            FORMAT_TIMESTAMP('%H:%M',
                MIN(CASE WHEN pe.event_type IN ('participant_joined', 'meeting.participant_joined')
                    THEN TIMESTAMP_ADD(CAST(pe.event_timestamp AS TIMESTAMP), INTERVAL 330 MINUTE) END)) as joined_ist,
            FORMAT_TIMESTAMP('%H:%M',
                MAX(CASE WHEN pe.event_type IN ('participant_left', 'meeting.participant_left')
                    THEN TIMESTAMP_ADD(CAST(pe.event_timestamp AS TIMESTAMP), INTERVAL 330 MINUTE) END)) as left_ist,
            TIMESTAMP_DIFF(
                MAX(CASE WHEN pe.event_type IN ('participant_left', 'meeting.participant_left')
                    THEN CAST(pe.event_timestamp AS TIMESTAMP) END),
                MIN(CASE WHEN pe.event_type IN ('participant_joined', 'meeting.participant_joined')
                    THEN CAST(pe.event_timestamp AS TIMESTAMP) END),
                MINUTE) as duration_mins
        FROM `{dataset_ref}.{BQ_EVENTS_TABLE}` pe
        INNER JOIN `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` tm
            ON LOWER(TRIM(pe.participant_name)) = LOWER(TRIM(tm.participant_name))
            AND tm.team_id = @team_id
        WHERE pe.event_date = @report_date
          AND pe.participant_name IS NOT NULL
          AND LOWER(pe.participant_name) NOT LIKE '%scout%'
        GROUP BY pe.participant_name, tm.participant_email
        """
        webhook_rows = {r.participant_name.lower().strip(): r
                        for r in client.query(webhook_only_q, job_config=webhook_only_config).result()}

        participants = []
        snapshot_names = set()
        for r in rows:
            break_mins = round(r.break_seconds / 60)
            iso_mins = round(r.isolation_seconds / 60)
            # Total time = breakout room time + main room time
            main_room = r.main_room_mins if r.main_room_mins else 0
            meeting_dur = r.meeting_duration_mins if r.meeting_duration_mins else 0
            # Use the larger of: snapshot active time, or meeting duration (webhook)
            total_mins = max(r.total_active_mins, meeting_dur) if meeting_dur > 0 else r.total_active_mins

            # Hour-based status: >=5hr=Present, 4-5hr=Half Day, <4hr=Absent
            if total_mins >= 300:
                status = 'present'
            elif total_mins >= 240:
                status = 'half_day'
            else:
                status = 'absent'

            participants.append({
                'name': r.participant_name,
                'email': r.participant_email or '',
                'first_seen_ist': r.meeting_joined_ist or r.first_seen_ist,
                'last_seen_ist': r.meeting_left_ist or r.last_seen_ist,
                'total_duration_mins': total_mins,
                'breakout_mins': r.total_active_mins,
                'main_room_mins': main_room,
                'break_minutes': break_mins,
                'isolation_minutes': iso_mins,
                'status': status
            })
            snapshot_names.add(r.participant_name.lower().strip())

        # Add webhook-only participants (in main meeting but never went to breakout)
        for m in all_members:
            name_lower = m.participant_name.lower().strip()
            if name_lower not in snapshot_names and name_lower in webhook_rows:
                wr = webhook_rows[name_lower]
                dur = wr.duration_mins or 0
                if dur >= 300:
                    status = 'present'
                elif dur >= 240:
                    status = 'half_day'
                else:
                    status = 'absent'
                participants.append({
                    'name': m.participant_name,
                    'email': m.participant_email or '',
                    'first_seen_ist': wr.joined_ist,
                    'last_seen_ist': wr.left_ist,
                    'total_duration_mins': dur,
                    'breakout_mins': 0,
                    'main_room_mins': dur,
                    'break_minutes': 0,
                    'isolation_minutes': 0,
                    'status': status
                })
                snapshot_names.add(name_lower)

        # Add fully absent members (not in snapshots and not in webhooks)
        for m in all_members:
            if m.participant_name.lower().strip() not in snapshot_names:
                participants.append({
                    'name': m.participant_name,
                    'email': m.participant_email or '',
                    'first_seen_ist': None,
                    'last_seen_ist': None,
                    'total_duration_mins': 0,
                    'breakout_mins': 0,
                    'main_room_mins': 0,
                    'break_minutes': 0,
                    'isolation_minutes': 0,
                    'status': 'absent'
                })

        total_members = len(all_members)
        present_count = len([p for p in participants if p['status'] == 'present'])
        half_day_count = len([p for p in participants if p['status'] == 'half_day'])

        return jsonify({
            'success': True,
            'date': report_date,
            'team_id': team_id,
            'team_name': team_name,
            'manager_name': manager_name,
            'total_members': total_members,
            'present_count': present_count,
            'half_day_count': half_day_count,
            'absent_count': total_members - present_count - half_day_count,
            'participants': participants
        })
    except Exception as e:
        print(f"[Teams] Attendance error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/teams/<team_id>/attendance/range', methods=['GET'])
def team_attendance_range(team_id):
    """Get team attendance for a date range. Query params: start, end"""
    try:
        ensure_team_tables_once()
        start_date = validate_date_format(request.args.get('start'))
        end_date = validate_date_format(request.args.get('end'))

        client = get_bq_client()
        dataset_ref = f"{GCP_PROJECT_ID}.{BQ_DATASET}"

        query = f"""
        WITH team_members AS (
            SELECT participant_name, participant_email
            FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}`
            WHERE team_id = @team_id
        ),
        daily_stats AS (
            SELECT
                s.event_date,
                s.participant_name,
                MIN(TIMESTAMP_ADD(s.snapshot_time, INTERVAL 330 MINUTE)) as first_seen,
                MAX(TIMESTAMP_ADD(s.snapshot_time, INTERVAL 330 MINUTE)) as last_seen,
                TIMESTAMP_DIFF(
                    MAX(TIMESTAMP_ADD(s.snapshot_time, INTERVAL 330 MINUTE)),
                    MIN(TIMESTAMP_ADD(s.snapshot_time, INTERVAL 330 MINUTE)),
                    MINUTE
                ) as active_mins,
                COUNT(DISTINCT s.snapshot_time) as snapshot_count
            FROM `{dataset_ref}.room_snapshots` s
            INNER JOIN team_members tm
                ON LOWER(TRIM(s.participant_name)) = LOWER(TRIM(tm.participant_name))
            WHERE s.event_date >= @start_date AND s.event_date <= @end_date
              AND s.room_name IS NOT NULL AND s.room_name != ''
              AND s.participant_name IS NOT NULL
              AND LOWER(s.participant_name) NOT LIKE '%scout%'
            GROUP BY s.event_date, s.participant_name
        ),
        ordered_snaps AS (
            SELECT
                s.event_date,
                s.participant_name,
                s.snapshot_time,
                LAG(s.snapshot_time) OVER (
                    PARTITION BY s.event_date, s.participant_name ORDER BY s.snapshot_time
                ) as prev_snapshot
            FROM `{dataset_ref}.room_snapshots` s
            INNER JOIN team_members tm
                ON LOWER(TRIM(s.participant_name)) = LOWER(TRIM(tm.participant_name))
            WHERE s.event_date >= @start_date AND s.event_date <= @end_date
              AND s.room_name IS NOT NULL AND s.room_name != ''
              AND s.participant_name IS NOT NULL
              AND LOWER(s.participant_name) NOT LIKE '%scout%'
        ),
        daily_breaks AS (
            SELECT
                event_date,
                participant_name,
                SUM(CASE WHEN TIMESTAMP_DIFF(snapshot_time, prev_snapshot, SECOND) > 60
                    THEN TIMESTAMP_DIFF(snapshot_time, prev_snapshot, SECOND) - 30 ELSE 0 END) as break_seconds,
                COUNT(CASE WHEN TIMESTAMP_DIFF(snapshot_time, prev_snapshot, SECOND) > 60 THEN 1 END) as break_count
            FROM ordered_snaps
            WHERE prev_snapshot IS NOT NULL
            GROUP BY event_date, participant_name
        ),
        room_occupancy AS (
            SELECT snapshot_time, room_name,
                   COUNT(DISTINCT participant_name) as occupant_count
            FROM `{dataset_ref}.room_snapshots`
            WHERE event_date >= @start_date AND event_date <= @end_date
              AND room_name IS NOT NULL AND room_name != ''
              AND participant_name IS NOT NULL
              AND LOWER(participant_name) NOT LIKE '%scout%'
            GROUP BY snapshot_time, room_name
        ),
        daily_isolation AS (
            SELECT
                s.event_date,
                s.participant_name,
                COUNT(*) * 30 as isolation_seconds
            FROM `{dataset_ref}.room_snapshots` s
            INNER JOIN team_members tm
                ON LOWER(TRIM(s.participant_name)) = LOWER(TRIM(tm.participant_name))
            INNER JOIN room_occupancy ro
                ON s.snapshot_time = ro.snapshot_time AND s.room_name = ro.room_name
            WHERE s.event_date >= @start_date AND s.event_date <= @end_date
              AND ro.occupant_count = 1
              AND s.room_name IS NOT NULL AND s.room_name != ''
            GROUP BY s.event_date, s.participant_name
        )
        SELECT
            ds.event_date,
            ds.participant_name,
            tm.participant_email,
            FORMAT_TIMESTAMP('%H:%M', ds.first_seen) as first_seen_ist,
            FORMAT_TIMESTAMP('%H:%M', ds.last_seen) as last_seen_ist,
            ds.active_mins,
            COALESCE(ROUND(db.break_seconds / 60), 0) as break_mins,
            COALESCE(db.break_count, 0) as break_count,
            COALESCE(ROUND(di.isolation_seconds / 60), 0) as isolation_mins
        FROM daily_stats ds
        LEFT JOIN team_members tm ON LOWER(TRIM(ds.participant_name)) = LOWER(TRIM(tm.participant_name))
        LEFT JOIN daily_breaks db ON ds.event_date = db.event_date AND ds.participant_name = db.participant_name
        LEFT JOIN daily_isolation di ON ds.event_date = di.event_date AND ds.participant_name = di.participant_name
        ORDER BY ds.event_date, ds.participant_name
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
                bigquery.ScalarQueryParameter("start_date", "STRING", start_date),
                bigquery.ScalarQueryParameter("end_date", "STRING", end_date)
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())

        # Team info
        team_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("team_id", "STRING", team_id)]
        )
        team_rows = list(client.query(
            f"SELECT team_name, manager_name FROM `{dataset_ref}.{BQ_TEAMS_TABLE}` WHERE team_id = @team_id",
            job_config=team_config
        ).result())
        team_name = team_rows[0].team_name if team_rows else 'Unknown'

        # All team members for absent detection
        all_members = list(client.query(
            f"SELECT participant_name, participant_email FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` WHERE team_id = @team_id",
            job_config=team_config
        ).result())
        all_member_names = [m.participant_name for m in all_members]

        # Build daily_data with hour-based status
        daily_data = []
        for r in rows:
            active = r.active_mins
            if active >= 300:
                status = 'Present'
            elif active >= 240:
                status = 'Half Day'
            else:
                status = 'Absent'
            daily_data.append({
                'date': str(r.event_date),
                'name': r.participant_name,
                'email': r.participant_email or '',
                'first_seen_ist': r.first_seen_ist,
                'last_seen_ist': r.last_seen_ist,
                'active_minutes': active,
                'break_minutes': int(r.break_mins),
                'isolation_minutes': int(r.isolation_mins),
                'status': status
            })

        # Per-member summary across date range
        member_summary = {}
        for r in daily_data:
            name = r['name']
            if name not in member_summary:
                member_summary[name] = {
                    'name': name, 'email': r['email'],
                    'days_present': 0, 'total_active_mins': 0,
                    'total_break_mins': 0, 'total_isolation_mins': 0
                }
            if r['status'] in ('Present', 'Half Day'):
                member_summary[name]['days_present'] += 1
            member_summary[name]['total_active_mins'] += r['active_minutes']
            member_summary[name]['total_break_mins'] += r['break_minutes']
            member_summary[name]['total_isolation_mins'] += r['isolation_minutes']

        # CSV export
        if request.args.get('format') == 'csv':
            import csv
            import io
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(['Date', 'Name', 'Email', 'Status', 'First_Seen_IST', 'Last_Seen_IST',
                             'Active_Minutes', 'Break_Minutes', 'Isolation_Minutes'])
            for r in daily_data:
                writer.writerow([r['date'], r['name'], r['email'], r['status'],
                                 r['first_seen_ist'], r['last_seen_ist'], r['active_minutes'],
                                 r['break_minutes'], r['isolation_minutes']])
            csv_content = output.getvalue()
            filename = f"team_{team_name.replace(' ', '_')}_{start_date}_to_{end_date}.csv"
            return Response(csv_content, mimetype='text/csv',
                            headers={'Content-Disposition': f'attachment; filename={filename}'})

        return jsonify({
            'success': True,
            'team_id': team_id,
            'team_name': team_name,
            'start_date': start_date,
            'end_date': end_date,
            'total_members': len(all_member_names),
            'daily_data': daily_data,
            'member_summary': list(member_summary.values())
        })
    except Exception as e:
        print(f"[Teams] Range attendance error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


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
                    bigquery.ScalarQueryParameter("report_date", "STRING", report_date)
                ]
            )
            stats_query = f"""
            WITH team_members AS (
                SELECT participant_name FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}` WHERE team_id = @team_id
            ),
            snaps AS (
                SELECT
                    s.participant_name,
                    s.snapshot_time,
                    TIMESTAMP_ADD(s.snapshot_time, INTERVAL 330 MINUTE) as snapshot_ist,
                    LAG(s.snapshot_time) OVER (PARTITION BY s.participant_name ORDER BY s.snapshot_time) as prev_snapshot
                FROM `{dataset_ref}.room_snapshots` s
                INNER JOIN team_members tm ON LOWER(TRIM(s.participant_name)) = LOWER(TRIM(tm.participant_name))
                WHERE s.event_date = @report_date
                  AND s.room_name IS NOT NULL AND s.room_name != ''
                  AND s.participant_name IS NOT NULL
                  AND LOWER(s.participant_name) NOT LIKE '%scout%'
            ),
            per_person AS (
                SELECT
                    participant_name,
                    MIN(snapshot_ist) as first_seen,
                    MAX(snapshot_ist) as last_seen,
                    TIMESTAMP_DIFF(MAX(snapshot_ist), MIN(snapshot_ist), MINUTE) as active_mins,
                    SUM(CASE WHEN prev_snapshot IS NOT NULL AND TIMESTAMP_DIFF(snapshot_time, prev_snapshot, SECOND) > 60
                        THEN TIMESTAMP_DIFF(snapshot_time, prev_snapshot, SECOND) - 30 ELSE 0 END) as break_secs
                FROM snaps
                GROUP BY participant_name
            )
            SELECT
                COUNT(*) as present,
                ROUND(AVG(active_mins), 0) as avg_active,
                ROUND(AVG(break_secs / 60), 0) as avg_break,
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
    """Generate monthly CSV report for a team. Query params: year, month"""
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
        _, last_day = monthrange(year, month)
        start_date = f"{year}-{month:02d}-01"
        end_date = f"{year}-{month:02d}-{last_day:02d}"

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

        # Monthly query: per member, per date stats
        query = f"""
        WITH team_members AS (
            SELECT participant_name, participant_email
            FROM `{dataset_ref}.{BQ_TEAM_MEMBERS_TABLE}`
            WHERE team_id = @team_id
        ),
        daily_stats AS (
            SELECT
                s.event_date,
                s.participant_name,
                MIN(TIMESTAMP_ADD(s.snapshot_time, INTERVAL 330 MINUTE)) as first_seen,
                MAX(TIMESTAMP_ADD(s.snapshot_time, INTERVAL 330 MINUTE)) as last_seen,
                TIMESTAMP_DIFF(
                    MAX(TIMESTAMP_ADD(s.snapshot_time, INTERVAL 330 MINUTE)),
                    MIN(TIMESTAMP_ADD(s.snapshot_time, INTERVAL 330 MINUTE)),
                    MINUTE
                ) as active_mins,
                COUNT(DISTINCT s.snapshot_time) as snapshot_count
            FROM `{dataset_ref}.room_snapshots` s
            INNER JOIN team_members tm
                ON LOWER(TRIM(s.participant_name)) = LOWER(TRIM(tm.participant_name))
            WHERE s.event_date >= @start_date AND s.event_date <= @end_date
              AND s.room_name IS NOT NULL AND s.room_name != ''
              AND s.participant_name IS NOT NULL
              AND LOWER(s.participant_name) NOT LIKE '%scout%'
            GROUP BY s.event_date, s.participant_name
        ),
        -- Break detection per day
        ordered_snaps AS (
            SELECT
                s.event_date,
                s.participant_name,
                s.snapshot_time,
                LAG(s.snapshot_time) OVER (
                    PARTITION BY s.event_date, s.participant_name ORDER BY s.snapshot_time
                ) as prev_snapshot
            FROM `{dataset_ref}.room_snapshots` s
            INNER JOIN team_members tm
                ON LOWER(TRIM(s.participant_name)) = LOWER(TRIM(tm.participant_name))
            WHERE s.event_date >= @start_date AND s.event_date <= @end_date
              AND s.room_name IS NOT NULL AND s.room_name != ''
              AND s.participant_name IS NOT NULL
              AND LOWER(s.participant_name) NOT LIKE '%scout%'
        ),
        daily_breaks AS (
            SELECT
                event_date,
                participant_name,
                SUM(CASE WHEN TIMESTAMP_DIFF(snapshot_time, prev_snapshot, SECOND) > 60
                    THEN TIMESTAMP_DIFF(snapshot_time, prev_snapshot, SECOND) - 30 ELSE 0 END) as break_seconds
            FROM ordered_snaps
            WHERE prev_snapshot IS NOT NULL
            GROUP BY event_date, participant_name
        ),
        -- Isolation per day
        room_occupancy AS (
            SELECT snapshot_time, room_name,
                   COUNT(DISTINCT participant_name) as occupant_count
            FROM `{dataset_ref}.room_snapshots`
            WHERE event_date >= @start_date AND event_date <= @end_date
              AND room_name IS NOT NULL AND room_name != ''
              AND participant_name IS NOT NULL
              AND LOWER(participant_name) NOT LIKE '%scout%'
            GROUP BY snapshot_time, room_name
        ),
        daily_isolation AS (
            SELECT
                s.event_date,
                s.participant_name,
                COUNT(*) * 30 as isolation_seconds
            FROM `{dataset_ref}.room_snapshots` s
            INNER JOIN team_members tm
                ON LOWER(TRIM(s.participant_name)) = LOWER(TRIM(tm.participant_name))
            INNER JOIN room_occupancy ro
                ON s.snapshot_time = ro.snapshot_time AND s.room_name = ro.room_name
            WHERE s.event_date >= @start_date AND s.event_date <= @end_date
              AND ro.occupant_count = 1
              AND s.room_name IS NOT NULL AND s.room_name != ''
            GROUP BY s.event_date, s.participant_name
        )

        SELECT
            ds.event_date,
            ds.participant_name,
            tm.participant_email,
            FORMAT_TIMESTAMP('%H:%M', ds.first_seen) as first_seen_ist,
            FORMAT_TIMESTAMP('%H:%M', ds.last_seen) as last_seen_ist,
            ds.active_mins,
            COALESCE(ROUND(db.break_seconds / 60), 0) as break_mins,
            COALESCE(ROUND(di.isolation_seconds / 60), 0) as isolation_mins
        FROM daily_stats ds
        LEFT JOIN team_members tm ON LOWER(TRIM(ds.participant_name)) = LOWER(TRIM(tm.participant_name))
        LEFT JOIN daily_breaks db ON ds.event_date = db.event_date AND ds.participant_name = db.participant_name
        LEFT JOIN daily_isolation di ON ds.event_date = di.event_date AND ds.participant_name = di.participant_name
        ORDER BY ds.participant_name, ds.event_date
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("team_id", "STRING", team_id),
                bigquery.ScalarQueryParameter("start_date", "STRING", start_date),
                bigquery.ScalarQueryParameter("end_date", "STRING", end_date)
            ]
        )
        rows = list(client.query(query, job_config=job_config).result())

        # Check if download=csv requested
        if request.args.get('format') == 'csv':
            import csv
            import io
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(['Date', 'Name', 'Email', 'Status', 'First_Seen_IST', 'Last_Seen_IST',
                             'Active_Minutes', 'Break_Minutes', 'Isolation_Minutes'])
            for r in rows:
                active = r.active_mins or 0
                status = 'Present' if active >= 300 else 'Half Day' if active >= 240 else 'Absent'
                writer.writerow([r.event_date, r.participant_name, r.participant_email or '',
                                 status, r.first_seen_ist, r.last_seen_ist, active,
                                 r.break_mins, r.isolation_mins])

            csv_content = output.getvalue()
            filename = f"team_{team_name.replace(' ', '_')}_{year}_{month:02d}.csv"
            return Response(
                csv_content,
                mimetype='text/csv',
                headers={'Content-Disposition': f'attachment; filename={filename}'}
            )

        # JSON response for dashboard display
        data = []
        for r in rows:
            active = r.active_mins or 0
            status = 'Present' if active >= 300 else 'Half Day' if active >= 240 else 'Absent'
            data.append({
                'date': str(r.event_date),
                'name': r.participant_name,
                'email': r.participant_email or '',
                'status': status,
                'first_seen_ist': r.first_seen_ist,
                'last_seen_ist': r.last_seen_ist,
                'active_minutes': active,
                'break_minutes': int(r.break_mins),
                'isolation_minutes': int(r.isolation_mins)
            })

        # Summary per member across the month
        member_summary = {}
        for r in data:
            name = r['name']
            if name not in member_summary:
                member_summary[name] = {
                    'name': name, 'email': r['email'],
                    'days_present': 0, 'total_active_mins': 0,
                    'total_break_mins': 0, 'total_isolation_mins': 0
                }
            if r['status'] in ('Present', 'Half Day'):
                member_summary[name]['days_present'] += 1
            member_summary[name]['total_active_mins'] += r['active_minutes']
            member_summary[name]['total_break_mins'] += r['break_minutes']
            member_summary[name]['total_isolation_mins'] += r['isolation_minutes']

        # Employee-wise CSV: grouped by employee with summary + daily rows
        if request.args.get('format') == 'employee_csv':
            import csv
            import io
            output = io.StringIO()
            writer = csv.writer(output)

            # Group daily data by employee
            emp_data = {}
            for r in data:
                name = r['name']
                if name not in emp_data:
                    emp_data[name] = []
                emp_data[name].append(r)

            for name in sorted(emp_data.keys()):
                emp_rows = emp_data[name]
                summary = member_summary.get(name, {})
                email = emp_rows[0].get('email', '') if emp_rows else ''

                # Employee header
                writer.writerow([])
                writer.writerow([f'EMPLOYEE: {name}'])
                writer.writerow([f'Email: {email}'])
                writer.writerow([f'Team: {team_name}'])
                writer.writerow([f'Period: {start_date} to {end_date}'])
                writer.writerow([f'Days Present: {summary.get("days_present", 0)}',
                                 f'Total Active: {summary.get("total_active_mins", 0)} min',
                                 f'Total Break: {summary.get("total_break_mins", 0)} min',
                                 f'Total Isolation: {summary.get("total_isolation_mins", 0)} min'])
                writer.writerow([])
                writer.writerow(['Date', 'Status', 'First_Seen_IST', 'Last_Seen_IST',
                                 'Active_Minutes', 'Break_Minutes', 'Isolation_Minutes'])
                for r in sorted(emp_rows, key=lambda x: x['date']):
                    writer.writerow([r['date'], r.get('status', ''), r['first_seen_ist'], r['last_seen_ist'],
                                     r['active_minutes'], r['break_minutes'], r['isolation_minutes']])
                writer.writerow([])

            csv_content = output.getvalue()
            filename = f"team_{team_name.replace(' ', '_')}_employee_report_{year}_{month:02d}.csv"
            return Response(csv_content, mimetype='text/csv',
                            headers={'Content-Disposition': f'attachment; filename={filename}'})

        return jsonify({
            'success': True,
            'team_id': team_id,
            'team_name': team_name,
            'year': year,
            'month': month,
            'start_date': start_date,
            'end_date': end_date,
            'daily_data': data,
            'member_summary': list(member_summary.values())
        })
    except Exception as e:
        print(f"[Teams] Monthly report error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


# ==============================================================================
# AUTH ENDPOINTS (BigQuery-based)
# ==============================================================================

@app.route('/auth/login', methods=['POST'])
def auth_login():
    """Login endpoint - validates username/password against BigQuery users table"""
    try:
        data = request.get_json() or {}
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()

        if not username or not password:
            return jsonify({'success': False, 'error': 'Username and password required'}), 400

        client = bigquery.Client(project=GCP_PROJECT_ID)
        query = f"""
            SELECT user_id, username, name, role, email
            FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.app_users`
            WHERE username = @username AND password = @password
            LIMIT 1
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("username", "STRING", username),
                bigquery.ScalarQueryParameter("password", "STRING", password),
            ]
        )
        results = list(client.query(query, job_config=job_config).result())

        if not results:
            return jsonify({'success': False, 'error': 'Invalid username or password'}), 401

        user = results[0]
        return jsonify({
            'success': True,
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
# RUN SERVER
# ==============================================================================

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
