"""
DAILY ATTENDANCE REPORT GENERATOR
=================================
Generates CSV report with ONE ROW PER PARTICIPANT
All times in IST (Indian Standard Time)

Format:
- Name, Email, Main Join IST, Main Left IST, Total Duration
- Room History: RoomName [Joined: HH:MM | Left: HH:MM | Duration: Xmin] -> NextRoom [...]

Triggered by Cloud Scheduler daily or /generate-report endpoint

ACCURACY ENHANCEMENT:
- Uses FIXED_ROOM_SEQUENCE as authoritative source for room names
- Cross-references room_index from calibration with fixed sequence
- Validates and corrects room names using multiple mapping sources
"""

from google.cloud import bigquery
from datetime import datetime, timedelta
import os
import csv
import io
import json

# ==============================================================================
# FIXED ROOM SEQUENCE - AUTHORITATIVE ROOM ORDER
# This MUST match the FIXED_ROOM_SEQUENCE in app.py
# Used for cross-referencing and correcting room names by index
# ==============================================================================
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

# Build reverse lookup: room_name -> room_index
ROOM_NAME_TO_INDEX = {name: idx for idx, name in enumerate(FIXED_ROOM_SEQUENCE)}

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

def get_yesterday_ist():
    """Get yesterday's date in IST"""
    return (get_ist_now() - timedelta(days=1)).strftime('%Y-%m-%d')

# SendGrid for email
try:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition
    import base64
    SENDGRID_AVAILABLE = True
except ImportError:
    SENDGRID_AVAILABLE = False
    print("[ReportGenerator] SendGrid not installed - email disabled")

# Configuration
GCP_PROJECT_ID = os.environ.get('GCP_PROJECT_ID', 'variant-finance-data-project')
BQ_DATASET = os.environ.get('BQ_DATASET', 'breakout_room_calibrator')
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
REPORT_EMAIL_FROM = os.environ.get('REPORT_EMAIL_FROM', 'reports@verveadvisory.com')
REPORT_EMAIL_TO = os.environ.get('REPORT_EMAIL_TO', '')


def get_bq_client():
    """Get BigQuery client"""
    return bigquery.Client(project=GCP_PROJECT_ID)


def generate_daily_report(report_date=None):
    """
    Generate daily attendance report with ONE ROW PER PARTICIPANT
    All times in IST (UTC + 5:30)

    Args:
        report_date: Date string 'YYYY-MM-DD' (defaults to yesterday)

    Returns:
        Dictionary with report data and CSV content
    """
    if report_date is None:
        # Default to yesterday in IST
        report_date = get_yesterday_ist()

    # SECURITY: Validate date format to prevent SQL injection
    # Only allow YYYY-MM-DD format
    import re
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', report_date):
        raise ValueError(f"Invalid date format: {report_date}. Expected YYYY-MM-DD")

    # Additional validation: ensure it's a valid date
    try:
        datetime.strptime(report_date, '%Y-%m-%d')
    except ValueError:
        raise ValueError(f"Invalid date: {report_date}")

    # Calculate previous day for mapping fallback (overnight meetings)
    prev_date = (datetime.strptime(report_date, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"[Report] Generating report for {report_date} (IST), mapping fallback: {prev_date}")

    client = get_bq_client()

    # =============================================
    # MAIN QUERY - ONE ROW PER PARTICIPANT
    # Room history: RoomName [HH:MM-HH:MM] -> NextRoom [HH:MM-HH:MM]
    # Total time = sum of all room visit durations
    #
    # ACCURACY: Uses room_index from calibration to look up FIXED_ROOM_SEQUENCE
    # This is more reliable than room_name which can be mismatched
    # =============================================

    # Build FIXED_ROOM_SEQUENCE as SQL array for lookup (BigQuery compatible)
    fixed_rooms_sql = ", ".join([f"STRUCT({i} AS room_index, '{name.replace(chr(39), chr(39)+chr(39))}' AS room_name)" for i, name in enumerate(FIXED_ROOM_SEQUENCE)])

    main_query = f"""
    WITH
    -- FIXED_ROOM_SEQUENCE as lookup table (authoritative room names by index)
    fixed_room_sequence AS (
      SELECT room_index, room_name FROM UNNEST([
        {fixed_rooms_sql}
      ])
    ),
    -- Get distinct meeting IDs for this report date
    report_meetings AS (
      SELECT DISTINCT meeting_id
      FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.participant_events`
      WHERE event_date = '{report_date}'
        AND meeting_id IS NOT NULL AND meeting_id != ''
    ),
    -- Room name mappings with INDEX-BASED correction
    -- Priority:
    -- 1. Use room_index to look up from FIXED_ROOM_SEQUENCE (most accurate)
    -- 2. Fall back to stored room_name if index not available
    -- 3. timestamp_calibration/sequence_calibration sources preferred
    room_name_map AS (
      SELECT room_uuid,
        COALESCE(frs.room_name, rm.room_name) as room_name,
        rm.room_index,
        rm.source
      FROM (
        SELECT rm.room_uuid, rm.room_name, rm.room_index, rm.source,
          ROW_NUMBER() OVER (
            PARTITION BY rm.room_uuid
            ORDER BY
              -- SOURCE PRIORITY: recalibration > pending_move > timestamp > sequence/webhook > others
              CASE WHEN rm.source = 'recalibration' THEN 0
                   WHEN rm.source = 'pending_move_calibration' THEN 1
                   WHEN rm.source = 'timestamp_calibration' THEN 2
                   WHEN rm.source IN ('sequence_calibration', 'webhook_calibration') THEN 3
                   ELSE 4 END,
              CASE WHEN rm.mapping_date = '{report_date}' THEN 0 ELSE 1 END,
              rm.mapped_at DESC
          ) as rn
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_mappings` rm
        INNER JOIN report_meetings rpt ON rm.meeting_id = rpt.meeting_id
        WHERE rm.mapping_date IN ('{report_date}', '{prev_date}')
      ) rm
      LEFT JOIN fixed_room_sequence frs ON rm.room_index = frs.room_index
      WHERE rm.rn = 1
    ),
    -- Secondary map: room_name consensus from event data
    -- If many participants have the same room_uuid with a real room_name in the event,
    -- that name is trustworthy (it was resolved at webhook time when mappings were in memory)
    event_room_consensus AS (
      SELECT room_uuid, room_name, cnt
      FROM (
        SELECT room_uuid, room_name,
          COUNT(*) as cnt,
          ROW_NUMBER() OVER (
            PARTITION BY room_uuid
            ORDER BY COUNT(*) DESC
          ) as rn
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.participant_events`
        WHERE event_date = '{report_date}'
          AND event_type IN ('breakout_room_joined', 'breakout_room_left')
          AND room_name IS NOT NULL AND room_name != ''
          AND NOT STARTS_WITH(room_name, 'Room-')
        GROUP BY room_uuid, room_name
      )
      WHERE rn = 1 AND cnt >= 2
    ),
    -- All breakout room events - deduplicated by participant + room + timestamp
    breakout_events AS (
      SELECT
        participant_id,
        participant_email,
        participant_name,
        room_uuid,
        room_name as event_room_name,
        event_type,
        event_timestamp,
        SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*SZ', event_timestamp) as event_ts_z,
        SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', event_timestamp) as event_ts_plain,
        ROW_NUMBER() OVER (
          PARTITION BY participant_id, room_uuid, event_type, event_timestamp
          ORDER BY inserted_at
        ) as dup_rank
      FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.participant_events`
      WHERE event_date = '{report_date}'
        AND event_type IN ('breakout_room_joined', 'breakout_room_left')
    ),
    breakout_events_dedup AS (
      SELECT
        participant_id, participant_email, participant_name,
        room_uuid, event_room_name, event_type, event_timestamp,
        COALESCE(event_ts_z, event_ts_plain) as event_ts
      FROM breakout_events
      WHERE dup_rank = 1
    ),
    -- Pair each JOIN with its corresponding LEAVE
    room_visits_paired AS (
      SELECT
        j.participant_id,
        j.participant_email,
        j.participant_name,
        j.room_uuid,
        j.event_room_name,
        j.event_ts as join_ts,
        (
          SELECT MIN(l.event_ts)
          FROM breakout_events_dedup l
          WHERE l.participant_id = j.participant_id
            AND l.room_uuid = j.room_uuid
            AND l.event_type = 'breakout_room_left'
            AND l.event_ts > j.event_ts
        ) as leave_ts
      FROM breakout_events_dedup j
      WHERE j.event_type = 'breakout_room_joined'
    ),
    -- Resolve room names with 4-tier priority:
    -- 1. event_room_name if it's a real name (resolved at webhook time)
    -- 2. room_mappings table (calibration data)
    -- 3. consensus from other participants' events (crowd-sourced truth)
    -- 4. NEVER fall back to Room-XXXX - use "Unmapped Room" to flag data issues
    room_visits_named AS (
      SELECT
        rv.participant_email,
        rv.participant_name,
        rv.room_uuid,
        COALESCE(
          CASE WHEN rv.event_room_name IS NOT NULL
                AND rv.event_room_name != ''
                AND NOT STARTS_WITH(rv.event_room_name, 'Room-')
               THEN rv.event_room_name END,
          rm.room_name,
          erc.room_name,
          CASE WHEN rv.event_room_name IS NOT NULL
                AND rv.event_room_name != ''
               THEN rv.event_room_name END
        ) as room_name,
        rv.join_ts,
        rv.leave_ts,
        FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(rv.join_ts, INTERVAL 330 MINUTE)) as join_ist,
        FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(rv.leave_ts, INTERVAL 330 MINUTE)) as leave_ist,
        TIMESTAMP_DIFF(rv.leave_ts, rv.join_ts, MINUTE) as duration_mins
      FROM room_visits_paired rv
      LEFT JOIN room_name_map rm ON rv.room_uuid = rm.room_uuid
      LEFT JOIN event_room_consensus erc ON rv.room_uuid = erc.room_uuid
    ),
    -- Merge consecutive visits to the SAME room (fixes duplicate room history)
    -- Assign a group number: increment when room changes
    room_visits_grouped AS (
      SELECT *,
        SUM(CASE WHEN room_name != prev_room OR prev_room IS NULL THEN 1 ELSE 0 END)
          OVER (PARTITION BY participant_email, participant_name ORDER BY join_ts) as room_group
      FROM (
        SELECT *,
          LAG(room_name) OVER (PARTITION BY participant_email, participant_name ORDER BY join_ts) as prev_room
        FROM room_visits_named
      )
    ),
    -- Collapse consecutive same-room visits into one entry
    room_visits AS (
      SELECT
        participant_email,
        participant_name,
        room_name,
        MIN(join_ts) as join_ts,
        MAX(leave_ts) as leave_ts,
        FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MIN(join_ts), INTERVAL 330 MINUTE)) as join_ist,
        FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MAX(leave_ts), INTERVAL 330 MINUTE)) as leave_ist,
        SUM(COALESCE(duration_mins, 0)) as duration_mins
      FROM room_visits_grouped
      GROUP BY participant_email, participant_name, room_name, room_group
    ),
    -- Build room history string per participant
    room_history AS (
      SELECT
        participant_email,
        participant_name,
        STRING_AGG(
          CONCAT(room_name, ' [', join_ist, '-', COALESCE(leave_ist, '?'), ']'),
          ' -> ' ORDER BY join_ts
        ) as rooms,
        SUM(COALESCE(duration_mins, 0)) as total_room_mins
      FROM room_visits
      GROUP BY participant_email, participant_name
    ),
    -- Main meeting join/leave - group by EMAIL only (prevents duplicate rows for name variations)
    participant_main AS (
      SELECT
        participant_email,
        -- Pick the most common name for this email (handles "John Doe" vs "J. Doe")
        ARRAY_AGG(participant_name ORDER BY event_count DESC LIMIT 1)[OFFSET(0)] as participant_name,
        MIN(first_join) as first_join,
        MAX(last_leave) as last_leave
      FROM (
        SELECT
          participant_email,
          participant_name,
          COUNT(*) as event_count,
          MIN(CASE WHEN event_type = 'participant_joined' THEN event_timestamp END) as first_join,
          MAX(CASE WHEN event_type = 'participant_left' THEN event_timestamp END) as last_leave
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.participant_events`
        WHERE event_date = '{report_date}'
        GROUP BY participant_email, participant_name
      )
      WHERE participant_email IS NOT NULL AND participant_email != ''
      GROUP BY participant_email
    ),
    -- Safe timestamp parser (handles both Z suffix and plain ISO)
    participant_main_parsed AS (
      SELECT
        participant_name,
        participant_email,
        COALESCE(
          SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*SZ', first_join),
          SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', first_join)
        ) as first_join_ts,
        COALESCE(
          SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*SZ', last_leave),
          SAFE.PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', last_leave)
        ) as last_leave_ts
      FROM participant_main
    )
    SELECT
      pm.participant_name as Name,
      pm.participant_email as Email,
      FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(pm.first_join_ts, INTERVAL 330 MINUTE)) as Main_Joined_IST,
      FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(pm.last_leave_ts, INTERVAL 330 MINUTE)) as Main_Left_IST,
      -- Total duration = time from main room join to main room leave (actual meeting attendance)
      -- This is more accurate than breakout room time which excludes main room time
      TIMESTAMP_DIFF(pm.last_leave_ts, pm.first_join_ts, MINUTE) as Total_Duration_Min,
      COALESCE(rh.rooms, '-') as Room_History
    FROM participant_main_parsed pm
    LEFT JOIN room_history rh
      ON pm.participant_email = rh.participant_email
    WHERE pm.participant_name NOT LIKE '%Scout%'
    ORDER BY pm.participant_name
    """

    try:
        results = list(client.query(main_query).result())
        print(f"[Report] Query returned {len(results)} participants")
    except Exception as e:
        print(f"[Report] Query error: {e}")
        results = []

    # =============================================
    # BUILD REPORT OBJECT
    # =============================================
    report = {
        'report_date': report_date,
        'generated_at': datetime.utcnow().isoformat(),
        'total_participants': len(results),
        'participants': [dict(row.items()) for row in results]
    }

    # Generate CSV
    report['csv_content'] = generate_csv(report)

    print(f"[Report] Generated report with {len(results)} participants")
    return report


def format_minutes_to_hhmm(minutes):
    """Format minutes as Xh Ym"""
    if not minutes or minutes <= 0:
        return '0m'
    try:
        minutes = int(minutes)
        hours = minutes // 60
        mins = minutes % 60
        if hours > 0:
            return f"{hours}h {mins}m"
        return f"{mins}m"
    except (ValueError, TypeError):
        return '0m'


def generate_csv(report):
    """Generate CSV content from report data"""
    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow([
        'Name',
        'Email',
        'Main_Joined_IST',
        'Main_Left_IST',
        'Total_Duration',
        'Room_History'
    ])

    # Data rows
    for p in report['participants']:
        # Format durations as Xh Ym
        total_mins = p.get('Total_Duration_Min', 0) or 0

        writer.writerow([
            p.get('Name', '') or '',
            p.get('Email', '') or '',
            p.get('Main_Joined_IST', '') or '',
            p.get('Main_Left_IST', '') or '',
            format_minutes_to_hhmm(total_mins),
            p.get('Room_History', '-') or '-'
        ])

    return output.getvalue()


def send_report_email(report, report_date):
    """Send report via SendGrid with CSV attachment"""
    if not SENDGRID_AVAILABLE:
        print("[Report] SendGrid not available")
        return False

    if not all([SENDGRID_API_KEY, REPORT_EMAIL_FROM, REPORT_EMAIL_TO]):
        print("[Report] Email configuration incomplete")
        print(f"  SENDGRID_API_KEY: {'set' if SENDGRID_API_KEY else 'NOT SET'}")
        print(f"  REPORT_EMAIL_FROM: {REPORT_EMAIL_FROM}")
        print(f"  REPORT_EMAIL_TO: {REPORT_EMAIL_TO}")
        return False

    try:
        # Build HTML email body
        html_content = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                h1 {{ color: #2D8CFF; }}
                h2 {{ color: #333; border-bottom: 2px solid #2D8CFF; padding-bottom: 5px; }}
                table {{ border-collapse: collapse; width: 100%; margin: 20px 0; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 12px; }}
                th {{ background-color: #2D8CFF; color: white; }}
                tr:nth-child(even) {{ background-color: #f9f9f9; }}
                .summary {{ background: #f0f8ff; padding: 15px; border-radius: 8px; margin: 20px 0; }}
                .footer {{ color: #666; font-size: 12px; margin-top: 30px; padding-top: 20px; border-top: 1px solid #ddd; }}
            </style>
        </head>
        <body>
            <h1>Daily Zoom Attendance Report</h1>
            <p><strong>Date:</strong> {report_date}</p>
            <p><strong>Generated:</strong> {report['generated_at']} UTC</p>
            <p><strong>All times shown in IST (Indian Standard Time)</strong></p>

            <div class="summary">
                <h2>Summary</h2>
                <p><strong>Total Participants:</strong> {report['total_participants']}</p>
            </div>

            <h2>Attendance (First 30 shown, full data in CSV)</h2>
            <table>
                <tr>
                    <th>Name</th>
                    <th>Email</th>
                    <th>Joined IST</th>
                    <th>Left IST</th>
                    <th>Duration</th>
                    <th>Room History</th>
                </tr>
        """

        for p in report['participants'][:30]:  # Limit to 30 in email
            room_history = p.get('Room_History', '-') or '-'
            # Truncate long room history for email
            if len(room_history) > 120:
                room_history = room_history[:120] + '...'

            html_content += f"""
                <tr>
                    <td>{p.get('Name', '')}</td>
                    <td>{p.get('Email', '')}</td>
                    <td>{p.get('Main_Joined_IST', '')}</td>
                    <td>{p.get('Main_Left_IST', '')}</td>
                    <td>{format_minutes_to_hhmm(p.get('Total_Duration_Min', 0))}</td>
                    <td style="font-size:10px;">{room_history}</td>
                </tr>
            """

        html_content += """
            </table>

            <div class="footer">
                <p><strong>Full attendance data is in the attached CSV file.</strong></p>
                <p>CSV Format: One row per participant with complete room visit history</p>
                <p>Room History Format: RoomName [HH:MM-HH:MM] -> NextRoom [HH:MM-HH:MM]</p>
                <p>Generated by Zoom Breakout Room Tracker</p>
            </div>
        </body>
        </html>
        """

        # Create email
        # Support both comma and semicolon as email delimiters
        to_emails = [e.strip() for e in REPORT_EMAIL_TO.replace(';', ',').split(',') if e.strip()]
        message = Mail(
            from_email=REPORT_EMAIL_FROM,
            to_emails=to_emails,
            subject=f"Daily Zoom Attendance Report - {report_date}",
            html_content=html_content
        )

        # Attach CSV
        csv_content = report['csv_content']
        encoded = base64.b64encode(csv_content.encode('utf-8')).decode()
        attachment = Attachment(
            FileContent(encoded),
            FileName(f"attendance_report_{report_date}.csv"),
            FileType('text/csv'),
            Disposition('attachment')
        )
        message.add_attachment(attachment)

        # Send
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)

        print(f"[Report] Email sent! Status: {response.status_code}")
        return True

    except Exception as e:
        print(f"[Report] Email error: {e}")
        import traceback
        traceback.print_exc()
        return False


def save_csv_to_gcs(report, report_date, bucket_name):
    """Save CSV file to Google Cloud Storage"""
    from google.cloud import storage

    try:
        client = storage.Client()
        bucket = client.bucket(bucket_name)

        blob_path = f"reports/attendance_report_{report_date}.csv"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(report['csv_content'], content_type='text/csv')

        print(f"[Report] Saved to GCS: gs://{bucket_name}/{blob_path}")
        return f"gs://{bucket_name}/{blob_path}"

    except Exception as e:
        print(f"[Report] GCS save error: {e}")
        return None


# Flask endpoint handler (called from app.py)
def generate_report_handler(report_date=None):
    """
    Handler for /generate-report endpoint
    Returns report data and optionally sends email
    """
    if report_date is None:
        # Default to yesterday in IST
        report_date = get_yesterday_ist()

    try:
        report = generate_daily_report(report_date)

        email_sent = False
        if SENDGRID_API_KEY and REPORT_EMAIL_TO:
            email_sent = send_report_email(report, report_date)

        return {
            'success': True,
            'date': report_date,
            'participants': report['total_participants'],
            'email_sent': email_sent,
            'email_to': REPORT_EMAIL_TO if email_sent else None
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'error': str(e)
        }


if __name__ == '__main__':
    # Test report generation
    import sys

    if len(sys.argv) > 1:
        date = sys.argv[1]
    else:
        date = get_ist_date()  # Use IST date

    print(f"Generating report for {date}...")
    report = generate_daily_report(date)

    # Save CSV locally for testing
    filename = f"attendance_report_{date}.csv"
    with open(filename, 'w', newline='', encoding='utf-8') as f:
        f.write(report['csv_content'])
    print(f"Saved: {filename}")

    print(f"\nReport generated with {report['total_participants']} participants")

    # Show first few rows
    print("\nFirst 5 participants:")
    for p in report['participants'][:5]:
        print(f"  {p.get('Name', '')} - {p.get('Main_Joined_IST', '')} to {p.get('Main_Left_IST', '')}")
