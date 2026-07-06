# Graph Report - C:\Users\shash\Downloads\zoom_tracker_project\verve-attendance-tracker  (2026-07-06)

## Corpus Check
- 51 files · ~226,822 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 806 nodes · 1385 edges · 59 communities detected
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 17 edges (avg confidence: 0.8)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]

## God Nodes (most connected - your core abstractions)
1. `get_bq_client()` - 110 edges
2. `get_ist_date()` - 52 edges
3. `ensure_team_tables_once()` - 47 edges
4. `validate_date_format()` - 27 edges
5. `get_ist_now()` - 25 edges
6. `apiFetch()` - 25 edges
7. `normalize_participant_name()` - 20 edges
8. `handle_breakout_room_join()` - 15 edges
9. `apiPost()` - 15 edges
10. `MeetingState` - 14 edges

## Surprising Connections (you probably didn't know these)
- `email_alert_test()` --calls--> `send_report_email()`  [INFERRED]
  C:\Users\shash\Downloads\zoom_tracker_project\verve-attendance-tracker\app.py → C:\Users\shash\Downloads\zoom_tracker_project\verve-attendance-tracker\report_generator.py
- `generate_daily_report()` --calls--> `_ensure_presence_intervals_table()`  [INFERRED]
  C:\Users\shash\Downloads\zoom_tracker_project\verve-attendance-tracker\report_generator.py → C:\Users\shash\Downloads\zoom_tracker_project\verve-attendance-tracker\app.py
- `generate_daily_report()` --calls--> `build_presence_intervals()`  [INFERRED]
  C:\Users\shash\Downloads\zoom_tracker_project\verve-attendance-tracker\report_generator.py → C:\Users\shash\Downloads\zoom_tracker_project\verve-attendance-tracker\app.py
- `generate_report()` --calls--> `generate_daily_report()`  [INFERRED]
  C:\Users\shash\Downloads\zoom_tracker_project\verve-attendance-tracker\app.py → C:\Users\shash\Downloads\zoom_tracker_project\verve-attendance-tracker\report_generator.py
- `generate_report()` --calls--> `get_yesterday_ist()`  [INFERRED]
  C:\Users\shash\Downloads\zoom_tracker_project\verve-attendance-tracker\app.py → C:\Users\shash\Downloads\zoom_tracker_project\verve-attendance-tracker\report_generator.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (111): calibration_correct(), calibration_health(), calibration_live_rooms(), calibration_mapping(), calibration_reload(), calibration_single_room_complete(), calibration_start(), calibration_status() (+103 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (58): EmployeeManager(), fmtMins(), addAttendanceOverride(), addBulkEmployeeLeave(), addEmployeeLeave(), addTeamHoliday(), addTeamMember(), adminAddSnapshots() (+50 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (57): admin_search_events(), admin_search_snapshots(), attendance_heatmap(), attendance_live(), attendance_summary(), attendance_summary_v2(), _auto_build_dates_in_range(), build_presence_intervals() (+49 more)

### Community 3 - "Community 3"
Cohesion: 0.04
Nodes (49): auth_list_users(), auth_login(), calibration_pending(), calibration_verify(), check_room_mapped(), data_delete_attendance(), data_get_all_attendance(), data_get_attendance_dates() (+41 more)

### Community 4 - "Community 4"
Cohesion: 0.04
Nodes (46): add_attendance_override(), add_employee_leave(), add_team_holiday(), add_team_member(), create_team(), delete_attendance_override(), delete_employee(), delete_employee_leave() (+38 more)

### Community 5 - "Community 5"
Cohesion: 0.05
Nodes (43): admin_add_snapshot(), admin_delete_events(), admin_delete_snapshots(), admin_edit_events(), admin_edit_snapshots(), admin_update_role(), calibration_mapping_summary(), calibration_recalibrate_room() (+35 more)

### Community 6 - "Community 6"
Cohesion: 0.1
Nodes (40): _apply_leave(), _apply_override(), classify_intent_with_gemini(), dispatch(), _find_employee(), _find_team(), _fmt_mins(), h_add_leave() (+32 more)

### Community 7 - "Community 7"
Cohesion: 0.09
Nodes (26): collect_qos_manual(), find_camera_data(), format_camera_intervals(), insert_qos_data(), qos_scheduled_collection(), qos_update_camera(), Find camera data for a participant using fuzzy matching.      Handles cases wh, Format camera ON timestamps into IST time intervals.      Input: List of UTC t (+18 more)

### Community 8 - "Community 8"
Cohesion: 0.1
Nodes (11): checkRoomWebhookReceived(), waitForWebhookConfirmation(), findScoutBot(), getParticipantName(), isBotNameMatch(), moveWithRetry(), runCalibration(), sleep() (+3 more)

### Community 9 - "Community 9"
Cohesion: 0.09
Nodes (14): App(), DayView(), IsolationRow(), cleanStr(), excelTimeToStr(), findCol(), formatDuration(), parseDurationStr() (+6 more)

### Community 10 - "Community 10"
Cohesion: 0.12
Nodes (24): generate_report(), preview_report(), Manually trigger report generation - defaults to YESTERDAY's data, Preview report data for a date, format_minutes_to_hhmm(), generate_csv(), generate_daily_report(), generate_report_handler() (+16 more)

### Community 11 - "Community 11"
Cohesion: 0.09
Nodes (22): bulk_add_team_members(), bulk_import_teams_and_members(), create_employee(), list_classified_monthly(), list_known_participants(), list_unrecognized_monthly(), list_unrecognized_participants(), normalize_participant_name() (+14 more)

### Community 12 - "Community 12"
Cohesion: 0.2
Nodes (5): ClassifiedPanel(), EmployeeDetailDrawer(), fmtHours(), fmtMins(), UnrecognizedPanel()

### Community 13 - "Community 13"
Cohesion: 0.15
Nodes (4): CalibrationPanel(), loadPendingSnapshots(), MonitorPanel(), useZoomSdk()

### Community 14 - "Community 14"
Cohesion: 0.17
Nodes (2): hl(), RoomCard()

### Community 15 - "Community 15"
Cohesion: 0.22
Nodes (7): BreakPivot(), dayOfWeek(), dowOf(), fmtHoursDecimal(), HoursPivot(), IsolationPivot(), isWeekend()

### Community 16 - "Community 16"
Cohesion: 0.17
Nodes (12): add_team_leave(), delete_team_leave(), ensure_leave_table(), get_team_leave(), Create leave records table if it doesn't exist, Get leave records for a team     Query params:         start_date: YYYY-MM-DD, Add a leave record for a team member     Body: {member_name, leave_date, leave_, Delete a leave record (+4 more)

### Community 17 - "Community 17"
Cohesion: 0.24
Nodes (10): apply_daily_attendance_overrides(), assign_unrecognized_attendance(), ensure_employee_registry_entry(), mark_source_participant_handled(), Ensure an employee exists in the registry and optionally in team_members., Copy monthly daily attendance rows to attendance_overrides for one employee., Insert a placeholder registry row so handled unrecognized names stop reappearing, Assign an unrecognized participant's daily attendance to one employee. (+2 more)

### Community 18 - "Community 18"
Cohesion: 0.2
Nodes (10): delete_team_tag(), ensure_team_tags_table(), get_team_tags(), list_teams_by_tag(), Create team tags table if it doesn't exist, Get all tags for a team, Set tags for a team (upsert behavior)     Body: {tags: {department: 'Engineerin, Delete a specific tag from a team (+2 more)

### Community 19 - "Community 19"
Cohesion: 0.18
Nodes (10): calibration_abort(), calibration_complete(), complete_calibration_state(), ensure_initialized(), init_meeting_state(), Mark calibration as complete in BigQuery, Mark calibration as complete, Abort calibration and DELETE all mappings saved during this session.     Called (+2 more)

### Community 20 - "Community 20"
Cohesion: 0.2
Nodes (10): get_attendance_for_sheet(), get_or_create_date_sheet(), get_sheets_service(), google_sheet_status(), Get Google Sheets API service using default credentials (Cloud Run service accou, Get attendance data for Google Sheets.     Includes ALL events from the date (n, Get or create a sheet tab for a specific date.     Sheet name format: DD-MM-YY, Update Google Sheet with attendance data.     Creates a separate sheet tab for (+2 more)

### Community 21 - "Community 21"
Cohesion: 0.24
Nodes (3): deleteDayData(), getAllData(), getUploadedDates()

### Community 22 - "Community 22"
Cohesion: 0.38
Nodes (9): dateFromStr(), downloadTeamPivotExcel(), dowShort(), expandRange(), isWeekend(), ordinal(), pad2(), setCell() (+1 more)

### Community 23 - "Community 23"
Cohesion: 0.33
Nodes (2): fmtMins(), TeamDashboard()

### Community 24 - "Community 24"
Cohesion: 0.33
Nodes (2): fmtMins(), TeamView()

### Community 25 - "Community 25"
Cohesion: 0.33
Nodes (0): 

### Community 26 - "Community 26"
Cohesion: 0.6
Nodes (5): downloadEmployeeYearExcel(), getAttendanceStyle(), getBreakStyle(), getHoursStyle(), getIsolationStyle()

### Community 27 - "Community 27"
Cohesion: 0.53
Nodes (4): downloadCsv(), exportDayViewCsv(), exportEmployeeCsv(), exportRowsCsv()

### Community 28 - "Community 28"
Cohesion: 0.4
Nodes (5): monitor_snapshot(), Convert an ISO timestamp to its 30-second bucket (matches the     DIV(UNIX_SECO, Receive a room snapshot from SDK polling.     Called every 30s by React app run, _snapshot_bucket(), _snapshot_cleanup()

### Community 29 - "Community 29"
Cohesion: 0.5
Nodes (2): ChatMessage(), renderRich()

### Community 30 - "Community 30"
Cohesion: 0.4
Nodes (0): 

### Community 31 - "Community 31"
Cohesion: 0.5
Nodes (0): 

### Community 32 - "Community 32"
Cohesion: 0.67
Nodes (2): EmployeeSummary(), getCellStyle()

### Community 33 - "Community 33"
Cohesion: 0.5
Nodes (0): 

### Community 34 - "Community 34"
Cohesion: 0.5
Nodes (0): 

### Community 35 - "Community 35"
Cohesion: 0.67
Nodes (0): 

### Community 36 - "Community 36"
Cohesion: 0.67
Nodes (0): 

### Community 37 - "Community 37"
Cohesion: 1.0
Nodes (2): getRoleLabel(), Sidebar()

### Community 38 - "Community 38"
Cohesion: 0.67
Nodes (0): 

### Community 39 - "Community 39"
Cohesion: 0.67
Nodes (0): 

### Community 40 - "Community 40"
Cohesion: 0.67
Nodes (0): 

### Community 41 - "Community 41"
Cohesion: 1.0
Nodes (2): getDefaultMessage(), StatusMessage()

### Community 42 - "Community 42"
Cohesion: 1.0
Nodes (2): Get historical trends for a team - monthly aggregated data     Query params:, team_historical_trends()

### Community 43 - "Community 43"
Cohesion: 1.0
Nodes (2): employee_attendance_detail(), Get detailed attendance for one employee for a month.     date format: YYYY-MM

### Community 44 - "Community 44"
Cohesion: 1.0
Nodes (2): add_bulk_leave(), Add leave for multiple employees. Body: {date: 'YYYY-MM-DD', employee_ids: [...]

### Community 45 - "Community 45"
Cohesion: 1.0
Nodes (2): employee_yearly_report(), Generate yearly summary for a single employee.     Query params: year (defaults

### Community 46 - "Community 46"
Cohesion: 1.0
Nodes (2): compare_teams(), Compare multiple teams side-by-side. Query params: ids (comma-sep), date

### Community 47 - "Community 47"
Cohesion: 1.0
Nodes (2): get_team(), Get team details with all members

### Community 48 - "Community 48"
Cohesion: 1.0
Nodes (0): 

### Community 49 - "Community 49"
Cohesion: 1.0
Nodes (0): 

### Community 50 - "Community 50"
Cohesion: 1.0
Nodes (0): 

### Community 51 - "Community 51"
Cohesion: 1.0
Nodes (0): 

### Community 52 - "Community 52"
Cohesion: 1.0
Nodes (1): Create all teams + members from the PDF employee list

### Community 53 - "Community 53"
Cohesion: 1.0
Nodes (0): 

### Community 54 - "Community 54"
Cohesion: 1.0
Nodes (0): 

### Community 55 - "Community 55"
Cohesion: 1.0
Nodes (0): 

### Community 56 - "Community 56"
Cohesion: 1.0
Nodes (0): 

### Community 57 - "Community 57"
Cohesion: 1.0
Nodes (0): 

### Community 58 - "Community 58"
Cohesion: 1.0
Nodes (0): 

## Knowledge Gaps
- **240 isolated node(s):** `ZOOM BREAKOUT ROOM TRACKER - GCP CLOUD RUN + BIGQUERY =========================`, `Get current datetime in IST`, `Get current date in IST (YYYY-MM-DD)`, `Convert UTC datetime to IST datetime`, `Get IST date string from UTC datetime` (+235 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 42`** (2 nodes): `Get historical trends for a team - monthly aggregated data     Query params:`, `team_historical_trends()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 43`** (2 nodes): `employee_attendance_detail()`, `Get detailed attendance for one employee for a month.     date format: YYYY-MM`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 44`** (2 nodes): `add_bulk_leave()`, `Add leave for multiple employees. Body: {date: 'YYYY-MM-DD', employee_ids: [...]`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 45`** (2 nodes): `employee_yearly_report()`, `Generate yearly summary for a single employee.     Query params: year (defaults`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 46`** (2 nodes): `compare_teams()`, `Compare multiple teams side-by-side. Query params: ids (comma-sep), date`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 47`** (2 nodes): `get_team()`, `Get team details with all members`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 48`** (2 nodes): `AttendanceEditModal()`, `AttendanceEditModal.jsx`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 49`** (2 nodes): `RoomAnalytics.jsx`, `RoomAnalytics()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 50`** (2 nodes): `Teams.jsx`, `Teams()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 51`** (2 nodes): `supabase.js`, `isSupabaseConfigured()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 52`** (2 nodes): `create_teams.py`, `Create all teams + members from the PDF employee list`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 53`** (1 nodes): `vite.config.js`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 54`** (1 nodes): `main.jsx`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 55`** (1 nodes): `index.js`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 56`** (1 nodes): `setupProxy.js`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 57`** (1 nodes): `scout_bot_watchdog.ps1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 58`** (1 nodes): `setup_scout_bot.ps1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ZoomAPI` connect `Community 7` to `Community 3`?**
  _High betweenness centrality (0.049) - this node is a cross-community bridge._
- **Why does `sleep()` connect `Community 8` to `Community 7`?**
  _High betweenness centrality (0.041) - this node is a cross-community bridge._
- **Why does `get_bq_client()` connect `Community 5` to `Community 0`, `Community 2`, `Community 3`, `Community 4`, `Community 7`, `Community 42`, `Community 11`, `Community 44`, `Community 43`, `Community 46`, `Community 47`, `Community 16`, `Community 17`, `Community 18`, `Community 19`, `Community 45`, `Community 20`, `Community 28`?**
  _High betweenness centrality (0.038) - this node is a cross-community bridge._
- **What connects `ZOOM BREAKOUT ROOM TRACKER - GCP CLOUD RUN + BIGQUERY =========================`, `Get current datetime in IST`, `Get current date in IST (YYYY-MM-DD)` to the rest of the system?**
  _240 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Community 0` be split into smaller, more focused modules?**
  _Cohesion score 0.03 - nodes in this community are weakly interconnected._
- **Should `Community 1` be split into smaller, more focused modules?**
  _Cohesion score 0.05 - nodes in this community are weakly interconnected._
- **Should `Community 2` be split into smaller, more focused modules?**
  _Cohesion score 0.05 - nodes in this community are weakly interconnected._