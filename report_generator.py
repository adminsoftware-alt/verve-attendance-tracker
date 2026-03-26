"""
DAILY ATTENDANCE REPORT GENERATOR
=================================
Generates CSV report with ONE ROW PER PARTICIPANT
All times in IST (Indian Standard Time)

Format:
- Name, Email, Main Join IST, Main Left IST, Total Duration
- Room History: RoomName [Joined: HH:MM | Left: HH:MM | Duration: Xmin] -> NextRoom [...]

Triggered by Cloud Scheduler daily or /generate-report endpoint
"""

from google.cloud import bigquery
from datetime import datetime, timedelta
import os
import csv
import io
import json

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

    # Calculate previous day for mapping fallback (overnight meetings)
    prev_date = (datetime.strptime(report_date, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"[Report] Generating report for {report_date} (IST), mapping fallback: {prev_date}")

    client = get_bq_client()

    # =============================================
    # MAIN QUERY - ONE ROW PER PARTICIPANT
    # Room history: RoomName [HH:MM-HH:MM] -> NextRoom [HH:MM-HH:MM]
    # Total time = sum of all room visit durations
    # =============================================
    main_query = f"""
    WITH
    -- Get room name mappings - ONE name per UUID
    -- Priority: same-day mapping > previous-day mapping (for overnight meetings 9AM-9AM)
    -- Within same day: webhook_calibration > zoom_sdk_app
    -- Get distinct meeting IDs for this report date to scope mappings
    report_meetings AS (
      SELECT DISTINCT meeting_id
      FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.participant_events`
      WHERE event_date = '{report_date}'
        AND meeting_id IS NOT NULL AND meeting_id != ''
    ),
    room_name_map AS (
      SELECT room_uuid, room_name
      FROM (
        SELECT rm.room_uuid, rm.room_name,
          ROW_NUMBER() OVER (
            PARTITION BY rm.room_uuid
            ORDER BY
              CASE WHEN rm.mapping_date = '{report_date}' THEN 0 ELSE 1 END,
              CASE WHEN rm.source = 'webhook_calibration' THEN 0 ELSE 1 END,
              rm.mapped_at DESC
          ) as rn
        FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.room_mappings` rm
        INNER JOIN report_meetings rpt ON rm.meeting_id = rpt.meeting_id
        WHERE rm.mapping_date IN ('{report_date}', '{prev_date}')
          AND rm.source = 'webhook_calibration'  -- Only use webhook UUIDs (SDK UUIDs are different format)
      )
      WHERE rn = 1
    ),
    -- All breakout room events for the day - deduplicated by participant + timestamp
    breakout_events AS (
      SELECT
        participant_id,
        participant_email,
        participant_name,
        room_uuid,
        room_name as event_room_name,  -- Room name stored directly in event
        event_type,
        event_timestamp,
        PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', event_timestamp) as event_ts,
        ROW_NUMBER() OVER (
          PARTITION BY participant_id, event_type, event_timestamp
          ORDER BY inserted_at
        ) as dup_rank
      FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.participant_events`
      WHERE event_date = '{report_date}'
        AND event_type IN ('breakout_room_joined', 'breakout_room_left')
    ),
    -- Deduplicated events - ONE event per participant per timestamp
    breakout_events_dedup AS (
      SELECT * FROM breakout_events WHERE dup_rank = 1
    ),
    -- Pair each JOIN with its corresponding LEAVE (same room, same participant, leave after join)
    room_visits_paired AS (
      SELECT
        j.participant_id,
        j.participant_email,
        j.participant_name,
        j.room_uuid,
        j.event_room_name,  -- Carry forward room name from event
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
    -- Add room names and calculate durations
    -- Priority: 1) event_room_name (if actually resolved, not "Room-XXXX" placeholder)
    --           2) room_mappings table
    --           3) Fallback to Room-{uuid}
    room_visits AS (
      SELECT
        rv.participant_email,
        rv.participant_name,
        COALESCE(
          -- First: use room_name from event if it's a real name (not Room-XXXX placeholder)
          CASE WHEN rv.event_room_name IS NOT NULL
                AND rv.event_room_name != ''
                AND NOT STARTS_WITH(rv.event_room_name, 'Room-')
               THEN rv.event_room_name END,
          rm.room_name,                     -- Second: lookup from room_mappings table
          rv.event_room_name,               -- Third: use event room_name even if Room-XXXX
          CONCAT('Room-', SUBSTR(rv.room_uuid, 1, 8))  -- Last fallback
        ) as room_name,
        rv.join_ts,
        rv.leave_ts,
        -- IST times (UTC + 5:30)
        FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(rv.join_ts, INTERVAL 330 MINUTE)) as join_ist,
        FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(rv.leave_ts, INTERVAL 330 MINUTE)) as leave_ist,
        TIMESTAMP_DIFF(rv.leave_ts, rv.join_ts, MINUTE) as duration_mins
      FROM room_visits_paired rv
      LEFT JOIN room_name_map rm ON rv.room_uuid = rm.room_uuid
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
    -- Main meeting join/leave (first join, last leave across ALL sessions)
    participant_main AS (
      SELECT
        participant_email,
        participant_name,
        MIN(CASE WHEN event_type = 'participant_joined' THEN event_timestamp END) as first_join,
        MAX(CASE WHEN event_type = 'participant_left' THEN event_timestamp END) as last_leave
      FROM `{GCP_PROJECT_ID}.{BQ_DATASET}.participant_events`
      WHERE event_date = '{report_date}'
      GROUP BY participant_email, participant_name
    )
    SELECT
      pm.participant_name as Name,
      pm.participant_email as Email,
      -- Main room times in IST (first join to last leave)
      FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', pm.first_join), INTERVAL 330 MINUTE)) as Main_Joined_IST,
      FORMAT_TIMESTAMP('%H:%M', TIMESTAMP_ADD(PARSE_TIMESTAMP('%Y-%m-%dT%H:%M:%E*S', pm.last_leave), INTERVAL 330 MINUTE)) as Main_Left_IST,
      -- Total duration from room visits (more accurate than join-leave diff)
      COALESCE(rh.total_room_mins, 0) as Total_Duration_Min,
      -- Room history: RoomName [HH:MM-HH:MM] -> NextRoom [HH:MM-HH:MM]
      COALESCE(rh.rooms, '-') as Room_History
    FROM participant_main pm
    LEFT JOIN room_history rh
      ON pm.participant_email = rh.participant_email
      AND pm.participant_name = rh.participant_name
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
                    <td>{p.get('Total_Duration_Min', '')} min</td>
                    <td style="font-size:10px;">{room_history}</td>
                </tr>
            """

        html_content += """
            </table>

            <div class="footer">
                <p><strong>Full attendance data is in the attached CSV file.</strong></p>
                <p>CSV Format: One row per participant with complete room visit history</p>
                <p>Room History Format: RoomName [Joined: HH:MM | Left: HH:MM | Duration: Xmin] -> NextRoom [...]</p>
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
