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

from flask import Flask, request, jsonify, send_from_directory
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

# ==============================================================================
# CONFIGURATION
# ==============================================================================

REACT_BUILD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'breakout-calibrator', 'build')
STATIC_PATH = os.path.join(REACT_BUILD_PATH, 'static')
app = Flask(__name__, static_folder=STATIC_PATH, static_url_path='/app/static')
import re
CORS(app, resources={r"/*": {"origins": re.compile(r"https://.*\.(zoom\.us|zoom\.com)$"), "methods": ["GET", "POST", "OPTIONS"], "allow_headers": ["Content-Type", "Authorization"]}})


# Headers for Zoom Apps - allow embedding
@app.after_request
def add_zoom_headers(response):
    # Do NOT set X-Frame-Options - allow Zoom to embed
    # CORS headers - restrict to Zoom domains (flask-cors handles per-request origin matching)
    origin = request.headers.get('Origin', '')
    if origin and ('.zoom.us' in origin or '.zoom.com' in origin):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'

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

# GCP Configuration
GCP_PROJECT_ID = os.environ.get('GCP_PROJECT_ID', '')
BQ_DATASET = os.environ.get('BQ_DATASET', 'breakout_room_calibrator')

# BigQuery Tables
BQ_EVENTS_TABLE = 'participant_events'
BQ_MAPPINGS_TABLE = 'room_mappings'
BQ_CAMERA_TABLE = 'camera_events'
BQ_QOS_TABLE = 'qos_data'

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
                            'event_date': get_ist_date()
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
            WHERE mapping_date < '{cutoff_date}'
            """
            client.query(query).result()
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
    """Insert room mappings into BigQuery with validation"""
    try:
        # Clean each mapping
        cleaned_mappings = []
        required = ['mapping_id', 'meeting_id', 'room_uuid', 'room_name', 'mapping_date', 'mapped_at']

        for mapping in mappings:
            cleaned = validate_and_clean_event(mapping, required)
            if cleaned:
                # Ensure room_index is int
                if 'room_index' in cleaned:
                    try:
                        cleaned['room_index'] = int(cleaned['room_index']) if cleaned['room_index'] else 0
                    except (ValueError, TypeError):
                        cleaned['room_index'] = 0
                cleaned_mappings.append(cleaned)
            else:
                print(f"[BigQuery] Skipping invalid mapping: {mapping}")

        if not cleaned_mappings:
            print(f"[BigQuery] No valid mappings to insert")
            return False

        client = get_bq_client()
        table_id = f"{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_MAPPINGS_TABLE}"

        errors = client.insert_rows_json(table_id, cleaned_mappings)
        if errors:
            print(f"[BigQuery] Mapping insert error: {errors}")
            return False

        print(f"[BigQuery] Inserted {len(cleaned_mappings)} mappings successfully")
        return True
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
                            print(f"[ZoomAPI] {method_name}: Unauthorized (401) - refreshing token")
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
                    print(f"[ZoomAPI] QoS API: Unauthorized - refreshing token")
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

    # Parse timestamp - Zoom sends event_ts in milliseconds
    event_ts = data.get('event_ts', 0)
    if event_ts and event_ts > 0:
        try:
            # Handle both milliseconds and seconds
            if event_ts > 1e12:  # Milliseconds
                event_dt = datetime.fromtimestamp(event_ts / 1000)
            else:  # Seconds
                event_dt = datetime.fromtimestamp(event_ts)
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
        print(f"  -> Calibration participant detected: {p['participant_name']} (mode: {cal_mode}, expected: {cal_name})")
        print(f"  -> Calibration in progress: {meeting_state.calibration_in_progress}")
        print(f"  -> Pending room moves: {len(meeting_state.pending_room_moves)}")

        # Scout Bot is moving during calibration
        # Find the oldest unmatched OR matched-but-not-verified pending room move
        # (matched-but-not-verified means we're retrying after a mismatch)
        room_name = None
        matched_move = None

        # First, look for completely unmatched entries
        for move in meeting_state.pending_room_moves:
            if not move.get('matched'):
                room_name = move['room_name']
                matched_move = move
                break

        # If all entries are matched, look for matched-but-not-verified (retry scenario)
        # This handles the case where bot went to wrong room, webhook arrived, but SDK detected mismatch
        # IMPORTANT: Iterate in REVERSE to find the MOST RECENT unverified room (the one being retried)
        if not matched_move:
            for move in reversed(meeting_state.pending_room_moves):
                if move.get('matched') and not move.get('verified'):
                    room_name = move['room_name']
                    matched_move = move
                    print(f"  -> RETRY: Updating webhook_uuid for unverified room: {room_name}")
                    break

        # Fallback to scout_bot_current_room if no pending moves
        if not room_name and hasattr(meeting_state, 'scout_bot_current_room'):
            room_name = meeting_state.scout_bot_current_room

        if room_name and room_uuid:
            # Mark the move as matched (but NOT verified yet - frontend must confirm)
            if matched_move:
                matched_move['matched'] = True
                matched_move['webhook_uuid'] = room_uuid
                matched_move['verified'] = False  # Will be set True by frontend after SDK verification
                print(f"  -> MATCHED pending move: {room_name} (awaiting frontend verification)")

            # Store webhook UUID -> room name mapping in memory (temporary)
            meeting_state.add_webhook_room_mapping(room_uuid, room_name)
            print(f"  -> CALIBRATION: Learned webhook UUID {room_uuid[:20]}... = {room_name}")
            print(f"  -> NOTE: Mapping will be saved to BigQuery after frontend verification")

            # DON'T save to BigQuery yet - wait for frontend to verify via /calibration/verify
            # This prevents saving wrong mappings when bot ends up in wrong room
        else:
            print(f"  -> WARNING: Could not match webhook UUID - room_name={room_name}, room_uuid={room_uuid[:20] if room_uuid else 'None'}")

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
                        'event_date': get_ist_date()
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
    """Start calibration session"""
    data = request.json or {}
    meeting_id = data.get('meeting_id')
    meeting_uuid = data.get('meeting_uuid')

    # Calibration participant info (for "Move Myself" mode)
    calibration_mode = data.get('calibration_mode', 'scout_bot')
    calibration_participant_name = data.get('calibration_participant_name', '')
    calibration_participant_uuid = data.get('calibration_participant_uuid', '')

    if not meeting_id:
        return jsonify({'error': 'meeting_id required'}), 400

    # Reset state for new calibration
    meeting_state.set_meeting(meeting_id, meeting_uuid)
    meeting_state.calibration_complete = False
    meeting_state.calibration_in_progress = True
    meeting_state.pending_room_moves = []

    # Store calibration participant info
    meeting_state.calibration_mode = calibration_mode
    meeting_state.calibration_participant_name = calibration_participant_name
    meeting_state.calibration_participant_uuid = calibration_participant_uuid

    print(f"\n{'='*50}")
    print(f"[Calibration] STARTED for meeting {meeting_id}")
    print(f"[Calibration] Mode: {calibration_mode}")
    if calibration_mode == 'self':
        print(f"[Calibration] Participant: {calibration_participant_name}")
    else:
        print(f"[Calibration] Using Scout Bot: {SCOUT_BOT_NAME}")
    print(f"[Calibration] Webhook UUID capture ENABLED")
    print(f"{'='*50}\n")

    return jsonify({
        'success': True,
        'message': 'Calibration started',
        'meeting_id': meeting_id,
        'calibration_mode': calibration_mode,
        'calibration_participant': calibration_participant_name or SCOUT_BOT_NAME
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
    Get pending room moves and their match status.
    Used by React app to poll and wait for webhook confirmation.
    """
    room_name = request.args.get('room_name')

    pending_moves = []
    for move in meeting_state.pending_room_moves:
        move_info = {
            'room_name': move.get('room_name'),
            'sdk_uuid': move.get('sdk_uuid'),
            'matched': move.get('matched', False),
            'webhook_uuid': move.get('webhook_uuid') if move.get('matched') else None
        }
        pending_moves.append(move_info)

    # If room_name is specified, check if that specific room is matched
    if room_name:
        room_matched = any(
            m.get('room_name') == room_name and m.get('matched')
            for m in meeting_state.pending_room_moves
        )
        return jsonify({
            'room_name': room_name,
            'matched': room_matched,
            'total_pending': len([m for m in meeting_state.pending_room_moves if not m['matched']]),
            'total_matched': len([m for m in meeting_state.pending_room_moves if m['matched']])
        })

    return jsonify({
        'pending_moves': pending_moves,
        'total_pending': len([m for m in meeting_state.pending_room_moves if not m['matched']]),
        'total_matched': len([m for m in meeting_state.pending_room_moves if m['matched']])
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

    # Count webhook UUID matches
    webhook_matches = len([m for m in meeting_state.pending_room_moves if m.get('matched')])
    unmatched = len([m for m in meeting_state.pending_room_moves if not m.get('matched')])

    print(f"\n{'='*50}")
    print(f"[Calibration] COMPLETE - {mapped_rooms}/{total_rooms} SDK room mappings")
    print(f"[Calibration] Webhook UUID matches: {webhook_matches} matched, {unmatched} unmatched")
    print(f"[Calibration] Total mappings in memory: {len(meeting_state.uuid_to_name)}")
    print(f"[Calibration] Scout Bot can now leave the meeting")
    print(f"{'='*50}\n")

    return jsonify({
        'success': True,
        'message': 'Calibration complete - Scout Bot can leave now',
        'sdk_mappings': mapped_rooms,
        'webhook_uuid_matches': webhook_matches,
        'unmatched_rooms': unmatched
    })


@app.route('/calibration/verify', methods=['POST'])
def calibration_verify():
    """
    Frontend calls this AFTER SDK verification confirms bot is in correct room.
    This saves the webhook UUID mapping to BigQuery.
    """
    data = request.json or {}
    room_name = data.get('room_name')
    meeting_id = data.get('meeting_id')

    if not room_name:
        return jsonify({'error': 'room_name required'}), 400

    # Find the matched pending move for this room
    matched_move = None
    for move in meeting_state.pending_room_moves:
        if move.get('room_name') == room_name and move.get('matched') and not move.get('verified'):
            matched_move = move
            break

    if not matched_move:
        print(f"[Calibration] Verify called but no unverified match found for: {room_name}")
        return jsonify({
            'success': False,
            'error': f'No unverified match found for room: {room_name}'
        }), 404

    webhook_uuid = matched_move.get('webhook_uuid')
    if not webhook_uuid:
        print(f"[Calibration] Verify called but no webhook UUID for: {room_name}")
        return jsonify({
            'success': False,
            'error': f'No webhook UUID captured for room: {room_name}'
        }), 404

    # Mark as verified
    matched_move['verified'] = True
    print(f"[Calibration] VERIFIED: {room_name} = {webhook_uuid[:20]}...")

    # NOW save to BigQuery (only after frontend verification)
    try:
        today = get_ist_date()
        mapping_row = {
            'mapping_id': str(uuid_lib.uuid4()),
            'meeting_id': str(meeting_id or meeting_state.meeting_id),
            'meeting_uuid': meeting_state.meeting_uuid or '',
            'room_uuid': webhook_uuid,
            'room_name': room_name,
            'room_index': len([m for m in meeting_state.pending_room_moves if m.get('verified')]) - 1,
            'mapping_date': today,
            'mapped_at': datetime.utcnow().isoformat(),
            'source': 'webhook_calibration'  # Webhook UUID verified by SDK
        }
        success = insert_room_mappings([mapping_row])
        if success:
            print(f"[Calibration] SAVED verified mapping to BigQuery: {room_name}")
        else:
            print(f"[Calibration] WARNING: BigQuery insert returned false for {room_name}")

        # Prune old verified entries to prevent list from growing indefinitely
        # Keep only entries that are either unverified or verified within last 5 minutes
        now = datetime.utcnow()
        meeting_state.pending_room_moves = [
            m for m in meeting_state.pending_room_moves
            if not m.get('verified') or (now - m.get('timestamp', now)).total_seconds() < 300
        ]
        print(f"[Calibration] Pending moves after prune: {len(meeting_state.pending_room_moves)}")

    except Exception as e:
        print(f"[Calibration] ERROR saving verified mapping: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({
        'success': True,
        'room_name': room_name,
        'webhook_uuid': webhook_uuid,
        'verified': True
    })


@app.route('/calibration/status', methods=['GET'])
def calibration_status():
    """Get current calibration status"""
    return jsonify({
        'meeting_id': meeting_state.meeting_id,
        'calibration_complete': meeting_state.calibration_complete,
        'calibrated_at': meeting_state.calibrated_at,
        'rooms_mapped': len(meeting_state.uuid_to_name),
        'room_names': list(meeting_state.name_to_uuid.keys())[:20]
    })


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
        WHERE mapping_date IN ('{today}', '{yesterday}')
        ORDER BY mapped_at DESC
        LIMIT 100
        """
        results = list(client.query(query).result())

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

    try:
        client = get_bq_client()
        query = f"""
        DELETE FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_QOS_TABLE}`
        WHERE event_date = '{target_date}'
        """
        job = client.query(query)
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

    try:
        client = get_bq_client()

        # Get meeting UUID and ID if not provided
        meeting_id = data.get('meeting_id')
        if not meeting_uuid:
            query = f"""
            SELECT DISTINCT meeting_uuid, meeting_id
            FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_EVENTS_TABLE}`
            WHERE event_date = '{target_date}'
              AND meeting_uuid IS NOT NULL
            LIMIT 1
            """
            results = list(client.query(query).result())
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
        # Default to yesterday
        target_date = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"[ScheduledQoS] Starting collection for date: {target_date}")

    try:
        client = get_bq_client()

        # Find meeting UUID(s) and ID(s) from participant_events for that date
        query = f"""
        SELECT DISTINCT meeting_uuid, meeting_id
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{BQ_EVENTS_TABLE}`
        WHERE event_date = '{target_date}'
          AND meeting_uuid IS NOT NULL
          AND meeting_uuid != ''
        LIMIT 5
        """
        results = list(client.query(query).result())

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
        WHERE event_date = '{target_date}'
        """
        check_result = list(client.query(check_query).result())[0]
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
            WHERE event_date < '{cleanup_date}'
            """
            cleanup_job = client.query(cleanup_query)
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
      WHERE event_date = '{today}'
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
        results = list(client.query(query).result())

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
                    query = f"SELECT COUNT(*) as count FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.{table_var}` WHERE event_date = '{today}'"
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
