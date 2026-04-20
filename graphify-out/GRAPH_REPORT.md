# Graph Report - .  (2026-04-20)

## Corpus Check
- 57 files · ~107,420 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 775 nodes · 1238 edges · 49 communities detected
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 26 edges (avg confidence: 0.8)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Flask Core & Calibration|Flask Core & Calibration]]
- [[_COMMUNITY_Team & Attendance APIs|Team & Attendance APIs]]
- [[_COMMUNITY_Zoom API Client|Zoom API Client]]
- [[_COMMUNITY_Chatbot Module|Chatbot Module]]
- [[_COMMUNITY_Shared Components|Shared Components]]
- [[_COMMUNITY_QoS & Camera Tracking|QoS & Camera Tracking]]
- [[_COMMUNITY_Calibration Services|Calibration Services]]
- [[_COMMUNITY_Day View & Parsing|Day View & Parsing]]
- [[_COMMUNITY_Report Generator|Report Generator]]
- [[_COMMUNITY_SDK Integration|SDK Integration]]
- [[_COMMUNITY_Employee List UI|Employee List UI]]
- [[_COMMUNITY_Live Dashboard|Live Dashboard]]
- [[_COMMUNITY_Monthly Pivot Tables|Monthly Pivot Tables]]
- [[_COMMUNITY_Attendance Override|Attendance Override]]
- [[_COMMUNITY_Storage Utils|Storage Utils]]
- [[_COMMUNITY_Calibration Panels|Calibration Panels]]
- [[_COMMUNITY_Team Pivot Excel|Team Pivot Excel]]
- [[_COMMUNITY_Team Tags|Team Tags]]
- [[_COMMUNITY_Team Dashboard|Team Dashboard]]
- [[_COMMUNITY_Team View|Team View]]
- [[_COMMUNITY_Report Builder|Report Builder]]
- [[_COMMUNITY_Employee Year Excel|Employee Year Excel]]
- [[_COMMUNITY_CSV Export Utils|CSV Export Utils]]
- [[_COMMUNITY_Chatbot UI|Chatbot UI]]
- [[_COMMUNITY_Loading Spinners|Loading Spinners]]
- [[_COMMUNITY_GCP Deployment|GCP Deployment]]
- [[_COMMUNITY_Data Editor|Data Editor]]
- [[_COMMUNITY_Employee Summary|Employee Summary]]
- [[_COMMUNITY_Room Table|Room Table]]
- [[_COMMUNITY_Team Compare|Team Compare]]
- [[_COMMUNITY_Holiday Manager|Holiday Manager]]
- [[_COMMUNITY_Login Component|Login Component]]
- [[_COMMUNITY_Sidebar|Sidebar]]
- [[_COMMUNITY_Isolation Analysis|Isolation Analysis]]
- [[_COMMUNITY_Progress Indicator|Progress Indicator]]
- [[_COMMUNITY_Room List|Room List]]
- [[_COMMUNITY_Status Message|Status Message]]
- [[_COMMUNITY_Attendance Edit Modal|Attendance Edit Modal]]
- [[_COMMUNITY_Room Analytics|Room Analytics]]
- [[_COMMUNITY_Teams List|Teams List]]
- [[_COMMUNITY_Supabase Client|Supabase Client]]
- [[_COMMUNITY_Team Import Script|Team Import Script]]
- [[_COMMUNITY_Breakout App Entry|Breakout App Entry]]
- [[_COMMUNITY_Vite Config|Vite Config]]
- [[_COMMUNITY_React Entry Point|React Entry Point]]
- [[_COMMUNITY_Calibrator Index|Calibrator Index]]
- [[_COMMUNITY_Proxy Setup|Proxy Setup]]
- [[_COMMUNITY_Scout Bot VM Setup|Scout Bot VM Setup]]
- [[_COMMUNITY_Employee Summary 2|Employee Summary 2]]

## God Nodes (most connected - your core abstractions)
1. `get_bq_client()` - 97 edges
2. `ensure_team_tables_once()` - 47 edges
3. `get_ist_date()` - 37 edges
4. `apiFetch()` - 25 edges
5. `validate_date_format()` - 20 edges
6. `normalize_participant_name()` - 17 edges
7. `handle_breakout_room_join()` - 15 edges
8. `apiPost()` - 15 edges
9. `MeetingState` - 14 edges
10. `get_ist_now()` - 13 edges

## Surprising Connections (you probably didn't know these)
- `generate_report()` --calls--> `generate_daily_report()`  [INFERRED]
  app.py → report_generator.py
- `generate_report()` --calls--> `get_yesterday_ist()`  [INFERRED]
  app.py → report_generator.py
- `generate_report()` --calls--> `send_report_email()`  [INFERRED]
  app.py → report_generator.py
- `preview_report()` --calls--> `generate_daily_report()`  [INFERRED]
  app.py → report_generator.py
- `App()` --calls--> `useAllData()`  [INFERRED]
  breakout-calibrator\src\App.js → attedance_manager\src\hooks\useData.js

## Hyperedges (group relationships)
- **attendance_data_flow** — flask_app, bigquery_tables, report_generator, react_app, dayview_component [INFERRED 1.00]
- **chatbot_action_chain** — chatbot_component, zoom_api_util, flask_app, chatbot_module, bigquery_tables [INFERRED 1.00]
- **role_based_access** — react_app, login_component, data_editor_component, flask_app [INFERRED 1.00]
- **ist_timezone_consistency** — flask_app, report_generator, bigquery_tables, ist_timezone_helper [INFERRED 1.00]
- **employee_management_subsystem** — employee_manager_component, employees_component, employee_summary_component, flask_app, bigquery_tables [INFERRED 0.80]
- **reporting_subsystem** — report_generator, report_builder_component, monthly_pivot_component, fixed_room_sequence [INFERRED 0.90]
- **real_time_monitoring** — live_dashboard_component, flask_app, meeting_state_class, bigquery_tables [INFERRED 0.90]
- **leave_management** — holiday_manager_component, attendance_edit_modal, flask_app, bigquery_tables [INFERRED 0.80]
- **Team Management Feature** — teams_component, teamview_component, teamcompare_component, teamdashboard_component, zoomapi_util [INFERRED 0.85]
- **Room Analytics Feature** — roomanalytics_component, rooms_component, roomtable_component, isolation_util [INFERRED 0.85]
- **Attendance Data Pipeline** — usedata_hook, storage_util, zoomapi_util, parser_util, cloud_run_api [INFERRED 0.90]
- **Excel Export Feature** — teampivotexcel_util, employeeyearexcel_util, teamview_component [INFERRED 0.80]
- **Breakout Calibrator Application** — breakout_app, breakout_index, setupproxy [EXTRACTED 1.00]
- **Calibration Flow** — CalibrationPanel, zoomService, apiService, useZoomSdk, ScoutBot, FlaskBackend [INFERRED]
- **SDK Monitoring Flow** — MonitorPanel, useZoomSdk, MonitorAPI, FlaskBackend, RoomSnapshots [INFERRED]
- **GCP Deployment Stack** — BreakoutCalibrator, AttendanceFrontend, FlaskBackend, CloudRun, BigQuery [INFERRED]

## Communities

### Community 0 - "Flask Core & Calibration"
Cohesion: 0.02
Nodes (143): auth_list_users(), auth_login(), calibration_abort(), calibration_complete(), calibration_correct(), calibration_health(), calibration_live_rooms(), calibration_mapping() (+135 more)

### Community 1 - "Team & Attendance APIs"
Cohesion: 0.02
Nodes (145): add_attendance_override(), add_bulk_leave(), add_employee_leave(), add_team_holiday(), add_team_leave(), add_team_member(), admin_add_snapshot(), admin_delete_events() (+137 more)

### Community 2 - "Zoom API Client"
Cohesion: 0.05
Nodes (58): EmployeeManager(), fmtMins(), addAttendanceOverride(), addBulkEmployeeLeave(), addEmployeeLeave(), addTeamHoliday(), addTeamMember(), adminAddSnapshots() (+50 more)

### Community 3 - "Chatbot Module"
Cohesion: 0.1
Nodes (40): _apply_leave(), _apply_override(), classify_intent_with_gemini(), dispatch(), _find_employee(), _find_team(), _fmt_mins(), h_add_leave() (+32 more)

### Community 4 - "Shared Components"
Cohesion: 0.06
Nodes (41): attendance_edit_modal, bigquery_tables, chatbot_component, chatbot_module, Cloud Run Backend API, data_editor_component, dayview_component, employee_manager_component (+33 more)

### Community 5 - "QoS & Camera Tracking"
Cohesion: 0.09
Nodes (26): collect_qos_manual(), find_camera_data(), format_camera_intervals(), insert_qos_data(), qos_scheduled_collection(), qos_update_camera(), Find camera data for a participant using fuzzy matching.      Handles cases wh, Format camera ON timestamps into IST time intervals.      Input: List of UTC t (+18 more)

### Community 6 - "Calibration Services"
Cohesion: 0.1
Nodes (11): checkRoomWebhookReceived(), waitForWebhookConfirmation(), findScoutBot(), getParticipantName(), isBotNameMatch(), moveWithRetry(), runCalibration(), sleep() (+3 more)

### Community 7 - "Day View & Parsing"
Cohesion: 0.09
Nodes (14): App(), DayView(), IsolationRow(), cleanStr(), excelTimeToStr(), findCol(), formatDuration(), parseDurationStr() (+6 more)

### Community 8 - "Report Generator"
Cohesion: 0.12
Nodes (24): generate_report(), preview_report(), Manually trigger report generation - defaults to YESTERDAY's data, Preview report data for a date, format_minutes_to_hhmm(), generate_csv(), generate_daily_report(), generate_report_handler() (+16 more)

### Community 9 - "SDK Integration"
Cohesion: 0.11
Nodes (20): BigQuery, Calibration API, CalibrationPanel, UI_STATES, Flask Backend (app.py), Monitor API, MonitorPanel, Position-Based Matching (+12 more)

### Community 10 - "Employee List UI"
Cohesion: 0.19
Nodes (5): ClassifiedPanel(), EmployeeDetailDrawer(), fmtHours(), fmtMins(), UnrecognizedPanel()

### Community 11 - "Live Dashboard"
Cohesion: 0.17
Nodes (2): hl(), RoomCard()

### Community 12 - "Monthly Pivot Tables"
Cohesion: 0.22
Nodes (3): dayOfWeek(), dowOf(), isWeekend()

### Community 13 - "Attendance Override"
Cohesion: 0.24
Nodes (10): apply_daily_attendance_overrides(), assign_unrecognized_attendance(), ensure_employee_registry_entry(), mark_source_participant_handled(), Ensure an employee exists in the registry and optionally in team_members., Copy monthly daily attendance rows to attendance_overrides for one employee., Insert a placeholder registry row so handled unrecognized names stop reappearing, Assign an unrecognized participant's daily attendance to one employee. (+2 more)

### Community 14 - "Storage Utils"
Cohesion: 0.29
Nodes (5): deleteDayData(), getAllData(), getDayData(), getUploadedDates(), saveDayData()

### Community 15 - "Calibration Panels"
Cohesion: 0.2
Nodes (3): CalibrationPanel(), MonitorPanel(), useZoomSdk()

### Community 16 - "Team Pivot Excel"
Cohesion: 0.42
Nodes (8): dateFromStr(), downloadTeamPivotExcel(), dowShort(), expandRange(), isWeekend(), pad2(), setCell(), writeCell()

### Community 17 - "Team Tags"
Cohesion: 0.25
Nodes (8): delete_team_tag(), ensure_team_tags_table(), get_team_tags(), Create team tags table if it doesn't exist, Get all tags for a team, Set tags for a team (upsert behavior)     Body: {tags: {department: 'Engineerin, Delete a specific tag from a team, set_team_tags()

### Community 18 - "Team Dashboard"
Cohesion: 0.33
Nodes (2): fmtMins(), TeamDashboard()

### Community 19 - "Team View"
Cohesion: 0.33
Nodes (2): fmtMins(), TeamView()

### Community 20 - "Report Builder"
Cohesion: 0.33
Nodes (0): 

### Community 21 - "Employee Year Excel"
Cohesion: 0.6
Nodes (5): downloadEmployeeYearExcel(), getAttendanceStyle(), getBreakStyle(), getHoursStyle(), getIsolationStyle()

### Community 22 - "CSV Export Utils"
Cohesion: 0.53
Nodes (4): downloadCsv(), exportDayViewCsv(), exportEmployeeCsv(), exportRowsCsv()

### Community 23 - "Chatbot UI"
Cohesion: 0.5
Nodes (2): ChatMessage(), renderRich()

### Community 24 - "Loading Spinners"
Cohesion: 0.4
Nodes (0): 

### Community 25 - "GCP Deployment"
Cohesion: 0.4
Nodes (5): Attendance Frontend, Breakout Calibrator App, Cloud Run, Teams API, create_teams.py

### Community 26 - "Data Editor"
Cohesion: 0.5
Nodes (0): 

### Community 27 - "Employee Summary"
Cohesion: 0.67
Nodes (2): EmployeeSummary(), getCellStyle()

### Community 28 - "Room Table"
Cohesion: 0.5
Nodes (0): 

### Community 29 - "Team Compare"
Cohesion: 0.5
Nodes (0): 

### Community 30 - "Holiday Manager"
Cohesion: 0.67
Nodes (0): 

### Community 31 - "Login Component"
Cohesion: 0.67
Nodes (0): 

### Community 32 - "Sidebar"
Cohesion: 1.0
Nodes (2): getRoleLabel(), Sidebar()

### Community 33 - "Isolation Analysis"
Cohesion: 0.67
Nodes (0): 

### Community 34 - "Progress Indicator"
Cohesion: 0.67
Nodes (0): 

### Community 35 - "Room List"
Cohesion: 0.67
Nodes (0): 

### Community 36 - "Status Message"
Cohesion: 1.0
Nodes (2): getDefaultMessage(), StatusMessage()

### Community 37 - "Attendance Edit Modal"
Cohesion: 1.0
Nodes (0): 

### Community 38 - "Room Analytics"
Cohesion: 1.0
Nodes (0): 

### Community 39 - "Teams List"
Cohesion: 1.0
Nodes (0): 

### Community 40 - "Supabase Client"
Cohesion: 1.0
Nodes (0): 

### Community 41 - "Team Import Script"
Cohesion: 1.0
Nodes (1): Create all teams + members from the PDF employee list

### Community 42 - "Breakout App Entry"
Cohesion: 1.0
Nodes (2): Breakout Calibrator App, Breakout Calibrator Index

### Community 43 - "Vite Config"
Cohesion: 1.0
Nodes (0): 

### Community 44 - "React Entry Point"
Cohesion: 1.0
Nodes (0): 

### Community 45 - "Calibrator Index"
Cohesion: 1.0
Nodes (0): 

### Community 46 - "Proxy Setup"
Cohesion: 1.0
Nodes (0): 

### Community 47 - "Scout Bot VM Setup"
Cohesion: 1.0
Nodes (0): 

### Community 48 - "Employee Summary 2"
Cohesion: 1.0
Nodes (1): employee_summary_component

## Knowledge Gaps
- **203 isolated node(s):** `ZOOM BREAKOUT ROOM TRACKER - GCP CLOUD RUN + BIGQUERY =========================`, `Get current datetime in IST`, `Get current date in IST (YYYY-MM-DD)`, `Convert UTC datetime to IST datetime`, `Get IST date string from UTC datetime` (+198 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Attendance Edit Modal`** (2 nodes): `AttendanceEditModal.jsx`, `AttendanceEditModal()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Room Analytics`** (2 nodes): `RoomAnalytics.jsx`, `RoomAnalytics()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Teams List`** (2 nodes): `Teams.jsx`, `Teams()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Supabase Client`** (2 nodes): `supabase.js`, `isSupabaseConfigured()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Team Import Script`** (2 nodes): `Create all teams + members from the PDF employee list`, `create_teams.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Breakout App Entry`** (2 nodes): `Breakout Calibrator App`, `Breakout Calibrator Index`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Vite Config`** (1 nodes): `vite.config.js`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `React Entry Point`** (1 nodes): `main.jsx`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Calibrator Index`** (1 nodes): `index.js`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Proxy Setup`** (1 nodes): `setupProxy.js`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Scout Bot VM Setup`** (1 nodes): `setup_scout_bot.ps1`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Employee Summary 2`** (1 nodes): `employee_summary_component`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ZoomAPI` connect `QoS & Camera Tracking` to `Flask Core & Calibration`?**
  _High betweenness centrality (0.043) - this node is a cross-community bridge._
- **Why does `generate_report()` connect `Report Generator` to `Flask Core & Calibration`?**
  _High betweenness centrality (0.042) - this node is a cross-community bridge._
- **Why does `sleep()` connect `Calibration Services` to `QoS & Camera Tracking`?**
  _High betweenness centrality (0.036) - this node is a cross-community bridge._
- **What connects `ZOOM BREAKOUT ROOM TRACKER - GCP CLOUD RUN + BIGQUERY =========================`, `Get current datetime in IST`, `Get current date in IST (YYYY-MM-DD)` to the rest of the system?**
  _203 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Flask Core & Calibration` be split into smaller, more focused modules?**
  _Cohesion score 0.02 - nodes in this community are weakly interconnected._
- **Should `Team & Attendance APIs` be split into smaller, more focused modules?**
  _Cohesion score 0.02 - nodes in this community are weakly interconnected._
- **Should `Zoom API Client` be split into smaller, more focused modules?**
  _Cohesion score 0.05 - nodes in this community are weakly interconnected._