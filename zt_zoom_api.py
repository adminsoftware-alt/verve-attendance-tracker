"""Zoom REST API client (Server-to-Server OAuth): participants,
QoS/camera data, meeting reports."""
import json
import time
import traceback
import requests
from zt_config import ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET

__all__ = [
    'ZoomAPI',
    'zoom_api',
]

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
                    method_started = time.time()
                    max_wall_secs = 120  # Bail out if a single method drags on

                    while page_count < max_pages:
                        # Wall-clock guard: a slow Zoom API shouldn't pin
                        # Cloud Run indefinitely. Bail with partial data.
                        if time.time() - method_started > max_wall_secs:
                            print(f"[ZoomAPI] {method_name} wall-clock cap hit "
                                  f"({max_wall_secs}s) after {page_count} pages — bailing")
                            break

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

    def get_meeting_participants_qos(self, meeting_id, max_pages=200, max_wall_secs=300):
        """
        Get QoS data for meeting participants using Dashboard Metrics API.
        This includes video_output data which indicates camera status.

        IMPORTANT: Requires Business/Education/Enterprise plan and
        dashboard_meetings:read:admin scope.

        Args:
            meeting_id: The meeting ID
            max_pages: Maximum pages to fetch (default 200 = 2000 participants)
                       Use smaller value for quick searches
            max_wall_secs: Hard wall-clock cap on the whole pagination loop.
                           Without this a slow Dashboard API could keep
                           Cloud Run busy past its request timeout (default 1
                           hour for Cloud Run, but we want to fail fast).
                           Returns partial results when exceeded.

        Returns list of participants with video_output stats.
        When camera is ON: video_output has bitrate, resolution, etc.
        When camera is OFF: video_output is empty/null
        """
        all_participants = []
        loop_started = time.time()

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
                # Wall-clock guard: bail out with whatever we have rather
                # than blocking the Cloud Run instance indefinitely.
                if time.time() - loop_started > max_wall_secs:
                    print(f"[ZoomAPI] QoS pagination wall-clock cap hit "
                          f"({max_wall_secs}s) after {page_count} pages, "
                          f"{len(all_participants)} participants — returning partial")
                    break
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


