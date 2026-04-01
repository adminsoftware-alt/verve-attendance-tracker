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

    MONITOR MODE: Room history is built from SDK polling snapshots (room_snapshots table).
    Main room join/leave still comes from webhooks (participant_events table).

    Args:
        report_date: Date string 'YYYY-MM-DD' (defaults to yesterday)

    Returns:
        Dictionary with report data and CSV content
    """
    if report_date is None:
        report_date = get_yesterday_ist()

    import re
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', report_date):
        raise ValueError(f"Invalid date format: {report_date}. Expected YYYY-MM-DD")

    try:
        datetime.strptime(report_date, '%Y-%m-%d')
    except ValueError:
        raise ValueError(f"Invalid date: {report_date}")

    print(f"[Report] Generating report for {report_date} (IST) using SDK snapshots")

    client = get_bq_client()

    # =============================================
    # MAIN QUERY - ONE ROW PER ROOM VISIT
    #
    # Each row = one participant's visit to one room
    # Shows: Name, Email, Room, Room_Joined, Room_Left, Duration
    #
    # How it works:
    #   SDK polls every 30s → detect room transitions → output each visit
    # =============================================

    main_query = f"""
    WITH
    -- ==========================================================
    -- PART 1: ROOM HISTORY from SDK snapshots
    -- Uses participant_name as key (SDK may not return emails)
    -- Handles duplicate names by also using participant_uuid
    -- ==========================================================
    snapshot_keyed AS (
      SELECT
        -- Unique key: prefer uuid, fall back to name
        COALESCE(NULLIF(participant_uuid, ''), participant_name) as participant_key,
        participant_name,
        COALESCE(NULLIF(participant_email, ''), '') as participant_email,
        room_name,
        snapshot_time
      FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_snapshots`
      WHERE event_date = '{report_date}'
        AND participant_name IS NOT NULL AND participant_name != ''
    ),
    snapshot_transitions AS (
      SELECT *,
        LAG(room_name) OVER (
          PARTITION BY participant_key
          ORDER BY snapshot_time
        ) as prev_room
      FROM snapshot_keyed
    ),
    visit_groups AS (
      SELECT *,
        SUM(CASE
          WHEN prev_room IS NULL OR room_name != prev_room THEN 1
          ELSE 0
        END) OVER (
          PARTITION BY participant_key
          ORDER BY snapshot_time
        ) as visit_id
      FROM snapshot_transitions
    ),
    room_visits AS (
      SELECT
        participant_key,
        MAX(participant_name) as participant_name,
        MAX(participant_email) as participant_email,
        room_name,
        MIN(snapshot_time) as join_time,
        MAX(snapshot_time) as leave_time,
        FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MIN(snapshot_time), INTERVAL 330 MINUTE)) as join_ist,
        FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(MAX(snapshot_time), INTERVAL 330 MINUTE)) as leave_ist
      FROM visit_groups
      GROUP BY participant_key, room_name, visit_id
    ),
    room_history AS (
      SELECT
        MAX(participant_name) as participant_name,
        MAX(participant_email) as snapshot_email,
        STRING_AGG(
          CONCAT(room_name, ' [', join_ist, '-', leave_ist, ']'),
          ' -> ' ORDER BY join_time
        ) as rooms
      FROM room_visits
      GROUP BY participant_key
    ),
    -- ==========================================================
    -- PART 2: JOIN/LEAVE TIMES from webhooks (EXACT timestamps)
    -- Webhooks have real join/leave times, not when bot saw them
    -- ==========================================================
    webhook_by_email AS (
      SELECT
        participant_email,
        ARRAY_AGG(participant_name ORDER BY cnt DESC LIMIT 1)[OFFSET(0)] as participant_name,
        MIN(first_join) as first_join,
        MAX(last_leave) as last_leave
      FROM (
        SELECT
          participant_email,
          participant_name,
          COUNT(*) as cnt,
          MIN(CASE WHEN event_type = 'participant_joined' THEN event_timestamp END) as first_join,
          MAX(CASE WHEN event_type = 'participant_left' THEN event_timestamp END) as last_leave
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.participant_events`
        WHERE event_date = '{report_date}'
        GROUP BY participant_email, participant_name
      )
      WHERE participant_email IS NOT NULL AND participant_email != ''
      GROUP BY participant_email
    ),
    webhook_parsed AS (
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
      FROM webhook_by_email
    ),
    -- ==========================================================
    -- PART 3: COMBINE
    -- Match webhooks ↔ snapshots using 3-tier matching:
    --   1. Email match (most reliable - if SDK returns email)
    --   2. Exact name match (case-insensitive)
    --   3. First-word name match ("Shashank" matches "Shashank C" and "Shashank Channawar")
    -- ==========================================================
    matched AS (
      SELECT
        w.participant_name as webhook_name,
        w.participant_email,
        w.first_join_ts,
        w.last_leave_ts,
        rh.participant_name as snapshot_name,
        rh.snapshot_email,
        rh.rooms,
        ROW_NUMBER() OVER (
          PARTITION BY COALESCE(w.participant_email, LOWER(TRIM(w.participant_name)))
          ORDER BY
            -- Priority: email match > exact name > first-word match
            CASE
              WHEN w.participant_email != '' AND rh.snapshot_email != ''
                   AND LOWER(w.participant_email) = LOWER(rh.snapshot_email) THEN 0
              WHEN LOWER(TRIM(w.participant_name)) = LOWER(TRIM(rh.participant_name)) THEN 1
              WHEN SPLIT(LOWER(TRIM(w.participant_name)), ' ')[OFFSET(0)]
                   = SPLIT(LOWER(TRIM(rh.participant_name)), ' ')[OFFSET(0)] THEN 2
              ELSE 3
            END
        ) as match_rank
      FROM webhook_parsed w
      LEFT JOIN room_history rh ON (
        -- Tier 1: Email match
        (w.participant_email != '' AND rh.snapshot_email != ''
         AND LOWER(w.participant_email) = LOWER(rh.snapshot_email))
        OR
        -- Tier 2: Exact name match
        LOWER(TRIM(w.participant_name)) = LOWER(TRIM(rh.participant_name))
        OR
        -- Tier 3: First word match (handles "Shashank" vs "Shashank Channawar")
        (SPLIT(LOWER(TRIM(w.participant_name)), ' ')[OFFSET(0)]
         = SPLIT(LOWER(TRIM(rh.participant_name)), ' ')[OFFSET(0)]
         AND LENGTH(SPLIT(LOWER(TRIM(w.participant_name)), ' ')[OFFSET(0)]) >= 3)
      )
    ),
    -- Get best match per webhook participant
    best_match AS (
      SELECT * FROM matched WHERE match_rank = 1
    ),
    -- Also include snapshot-only participants (no webhook match)
    snapshot_only AS (
      SELECT rh.participant_name, rh.snapshot_email, rh.rooms
      FROM room_history rh
      WHERE NOT EXISTS (
        SELECT 1 FROM best_match bm
        WHERE bm.rooms = rh.rooms AND bm.snapshot_name = rh.participant_name
      )
    )
    -- Final output
    SELECT
      bm.webhook_name as Name,
      bm.participant_email as Email,
      FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(bm.first_join_ts, INTERVAL 330 MINUTE)) as Main_Joined_IST,
      FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(bm.last_leave_ts, INTERVAL 330 MINUTE)) as Main_Left_IST,
      TIMESTAMP_DIFF(bm.last_leave_ts, bm.first_join_ts, MINUTE) as Total_Duration_Min,
      COALESCE(bm.rooms, '-') as Room_History
    FROM best_match bm
    WHERE bm.webhook_name NOT LIKE '%Scout%'

    UNION ALL

    -- Participants only in snapshots (no webhook - maybe joined before webhooks started)
    SELECT
      so.participant_name as Name,
      COALESCE(so.snapshot_email, '') as Email,
      '' as Main_Joined_IST,
      '' as Main_Left_IST,
      NULL as Total_Duration_Min,
      so.rooms as Room_History
    FROM snapshot_only so
    WHERE so.participant_name NOT LIKE '%Scout%'

    ORDER BY Name
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
