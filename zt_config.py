"""Environment configuration, BigQuery table names, and the shared
BigQuery client singleton."""
import os
from google.cloud import bigquery

__all__ = [
    'ZOOM_WEBHOOK_SECRET',
    'ZOOM_ACCOUNT_ID',
    'ZOOM_CLIENT_ID',
    'ZOOM_CLIENT_SECRET',
    'SCOUT_BOT_NAME',
    'SCOUT_BOT_EMAIL',
    'GCP_PROJECT_ID',
    'BQ_DATASET',
    'BQ_EVENTS_TABLE',
    'BQ_MAPPINGS_TABLE',
    'BQ_CAMERA_TABLE',
    'BQ_QOS_TABLE',
    'BQ_CALIBRATION_STATE_TABLE',
    'BQ_TEAMS_TABLE',
    'BQ_TEAM_MEMBERS_TABLE',
    'BQ_TEAM_HOLIDAYS_TABLE',
    'BQ_EMPLOYEE_LEAVE_TABLE',
    'BQ_ATTENDANCE_OVERRIDES_TABLE',
    'bq_client',
    'get_bq_client',
]

ZOOM_WEBHOOK_SECRET = os.environ.get('ZOOM_WEBHOOK_SECRET', '').strip()


ZOOM_ACCOUNT_ID = os.environ.get('ZOOM_ACCOUNT_ID', '')


ZOOM_CLIENT_ID = os.environ.get('ZOOM_CLIENT_ID', '')


ZOOM_CLIENT_SECRET = os.environ.get('ZOOM_CLIENT_SECRET', '')


SCOUT_BOT_NAME = os.environ.get('SCOUT_BOT_NAME', 'Scout Bot')


SCOUT_BOT_EMAIL = os.environ.get('SCOUT_BOT_EMAIL', '')


GCP_PROJECT_ID = os.environ.get('GCP_PROJECT_ID', '')


BQ_DATASET = os.environ.get('BQ_DATASET', 'breakout_room_calibrator')


# Partitioned (by event_date) + clustered successor of the original
# unpartitioned `participant_events`. Switched via code pointer on
# 2026-07-17 because ALTER TABLE RENAME is blocked while the webhook
# streaming buffer is warm. The old table is frozen and dropped after
# row-count verification.
BQ_EVENTS_TABLE = 'participant_events_p'


BQ_MAPPINGS_TABLE = 'room_mappings'


BQ_CAMERA_TABLE = 'camera_events'


BQ_QOS_TABLE = 'qos_data'


BQ_CALIBRATION_STATE_TABLE = 'calibration_state'


BQ_TEAMS_TABLE = 'teams'


BQ_TEAM_MEMBERS_TABLE = 'team_members'


BQ_TEAM_HOLIDAYS_TABLE = 'team_holidays'


BQ_EMPLOYEE_LEAVE_TABLE = 'employee_leave'


BQ_ATTENDANCE_OVERRIDES_TABLE = 'attendance_overrides'


bq_client = None


def get_bq_client():
    global bq_client
    if bq_client is None:
        bq_client = bigquery.Client(project=GCP_PROJECT_ID)
    return bq_client


