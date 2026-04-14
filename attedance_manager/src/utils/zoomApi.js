/**
 * Zoom Tracker API - Fetches live attendance data from Cloud Run BigQuery endpoints.
 * Replaces CSV upload workflow with direct API calls.
 */

const ZOOM_API_BASE = 'https://breakout-room-calibrator-1073587167150.us-central1.run.app';

// ─── FETCH HELPERS ─────────────────────────────────────

async function apiFetch(path) {
  const res = await fetch(`${ZOOM_API_BASE}${path}`);
  if (!res.ok) throw new Error(`API error ${res.status}: ${res.statusText}`);
  const data = await res.json();
  if (data.success === false) throw new Error(data.error || 'API returned failure');
  return data;
}

// ─── LIVE DATA (who's where right now) ─────────────────

function istDate() {
  const now = new Date();
  return new Date(now.getTime() + 330 * 60000).toISOString().slice(0, 10);
}

export async function fetchLiveRooms(date) {
  const d = date || istDate();
  return apiFetch(`/attendance/live?date=${d}`);
}

// ─── HEATMAP (room occupancy over time) ────────────────

export async function fetchHeatmap(date, interval = 15) {
  const d = date || istDate();
  return apiFetch(`/attendance/heatmap/${d}?interval=${interval}`);
}

// ─── ATTENDANCE SUMMARY (full day data) ────────────────

export async function fetchSummary(date) {
  const d = date || istDate();
  return apiFetch(`/attendance/summary/${d}`);
}

// ─── TRANSFORM: API response → app employee format ─────
// Converts /attendance/summary response into the same shape
// that parser.js produces, so all existing views work seamlessly.

export function transformSummaryToEmployees(summaryData) {
  if (!summaryData?.participants) return [];

  return summaryData.participants.map(p => {
    const rooms = (p.room_visits || [])
      .filter(v => v.room_name)
      .map(v => ({
        name: v.room_name,
        start: v.room_joined_ist || '',
        end: v.room_left_ist || '',
        duration: v.room_duration_mins || 0,
        isNamed: v.room_name.includes(':') && !v.room_name.startsWith('Room-'),
        session: p.name,
      }));

    const totalMin = p.total_duration_mins || 0;
    const h = Math.floor(totalMin / 60);
    const m = Math.round(totalMin % 60);

    return {
      name: p.name || '',
      email: p.email || '',
      joined: p.first_seen_ist || '',
      left: p.last_seen_ist || '',
      totalMinutes: totalMin,
      duration: h + 'h ' + m + 'm',
      sessions: 1,
      rooms: rooms.sort((a, b) => (a.start || '').localeCompare(b.start || '')),
    };
  }).sort((a, b) => a.name.localeCompare(b.name));
}

// ─── TRANSFORM: /attendance/live → employee format ─────
// Fallback when /attendance/summary is unavailable (ongoing meeting)

export function transformLiveToEmployees(liveData) {
  if (!liveData?.rooms) return [];

  // Flatten all participants across rooms
  const empMap = {};
  for (const room of liveData.rooms) {
    for (const p of room.participants) {
      const key = p.participant_name.toLowerCase();
      if (!empMap[key]) {
        empMap[key] = {
          name: p.participant_name,
          email: p.participant_email || '',
          joined: '',
          left: '',
          totalMinutes: 0,
          duration: '0h 0m',
          sessions: 1,
          rooms: [],
        };
      }
      if (p.participant_email && !empMap[key].email) empMap[key].email = p.participant_email;
      empMap[key].rooms.push({
        name: room.room_name,
        start: '',
        end: '',
        duration: 0,
        isNamed: room.room_name.includes(':') && !room.room_name.startsWith('Room-'),
        session: p.participant_name,
      });
    }
  }

  return Object.values(empMap).sort((a, b) => a.name.localeCompare(b.name));
}

// ─── HEALTH CHECK ──────────────────────────────────────

export async function fetchMonitorHealth() {
  return apiFetch('/monitor/health');
}

// ─── TEAMS API ──────────────────────────────────────────

async function apiPost(path, body) {
  const res = await fetch(`${ZOOM_API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!res.ok) throw new Error(`API error ${res.status}: ${res.statusText}`);
  const data = await res.json();
  if (data.success === false) throw new Error(data.error || 'API returned failure');
  return data;
}

async function apiPut(path, body) {
  const res = await fetch(`${ZOOM_API_BASE}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  if (!res.ok) throw new Error(`API error ${res.status}: ${res.statusText}`);
  const data = await res.json();
  if (data.success === false) throw new Error(data.error || 'API returned failure');
  return data;
}

async function apiDelete(path) {
  const res = await fetch(`${ZOOM_API_BASE}${path}`, { method: 'DELETE' });
  if (!res.ok) throw new Error(`API error ${res.status}: ${res.statusText}`);
  const data = await res.json();
  if (data.success === false) throw new Error(data.error || 'API returned failure');
  return data;
}

export async function fetchTeams() {
  return apiFetch('/teams');
}

export async function fetchTeamDetail(teamId) {
  return apiFetch(`/teams/${teamId}`);
}

export async function createTeam(name, managerName, managerEmail) {
  return apiPost('/teams', { team_name: name, manager_name: managerName, manager_email: managerEmail });
}

export async function updateTeam(teamId, name, managerName, managerEmail) {
  return apiPut(`/teams/${teamId}`, { team_name: name, manager_name: managerName, manager_email: managerEmail });
}

export async function deleteTeam(teamId) {
  return apiDelete(`/teams/${teamId}`);
}

export async function addTeamMember(teamId, name, email) {
  return apiPost(`/teams/${teamId}/members`, { participant_name: name, participant_email: email });
}

export async function bulkAddTeamMembers(teamId, members) {
  return apiPost(`/teams/${teamId}/members/bulk`, { members });
}

export async function bulkImportTeams(members) {
  return apiPost('/teams/bulk-import', { members });
}

export async function removeTeamMember(teamId, memberId) {
  return apiDelete(`/teams/${teamId}/members/${memberId}`);
}

export async function fetchParticipants() {
  return apiFetch('/teams/participants');
}

export async function fetchTeamAttendance(teamId, date) {
  return apiFetch(`/teams/${teamId}/attendance/${date}`);
}

export async function fetchTeamAttendanceRange(teamId, startDate, endDate) {
  return apiFetch(`/teams/${teamId}/attendance/range?start=${startDate}&end=${endDate}`);
}

export async function fetchTeamMonthlyReport(teamId, year, month) {
  return apiFetch(`/teams/${teamId}/report/monthly?year=${year}&month=${month}`);
}

export async function fetchTeamComparison(teamIds, date) {
  return apiFetch(`/teams/compare?ids=${teamIds.join(',')}&date=${date}`);
}

// ─── TEAM HOLIDAYS ─────────────────────────────────────
export async function fetchTeamHolidays(teamId, year, month) {
  const q = new URLSearchParams();
  if (year) q.set('year', year);
  if (month) q.set('month', month);
  const qs = q.toString();
  return apiFetch(`/teams/${teamId}/holidays${qs ? '?' + qs : ''}`);
}

export async function fetchTeamsHolidaysSummary(year, month) {
  const q = new URLSearchParams();
  if (year) q.set('year', year);
  if (month) q.set('month', month);
  const qs = q.toString();
  return apiFetch(`/teams/holidays-summary${qs ? '?' + qs : ''}`);
}

export async function addTeamHoliday(teamId, date, description) {
  return apiPost(`/teams/${teamId}/holidays`, { date, description: description || '' });
}

export async function deleteTeamHoliday(teamId, holidayId) {
  return apiDelete(`/teams/${teamId}/holidays/${holidayId}`);
}

export async function updateTeamHoliday(teamId, holidayId, date, description) {
  return apiPut(`/teams/${teamId}/holidays/${holidayId}`, { date, description });
}

// ─── EMPLOYEE LEAVE ───────────────────────────────────
export async function fetchAllEmployeeLeave(year, month) {
  const q = new URLSearchParams();
  if (year) q.set('year', year);
  if (month) q.set('month', month);
  const qs = q.toString();
  return apiFetch(`/employees/leave${qs ? '?' + qs : ''}`);
}

export async function fetchEmployeeLeave(employeeId, year, month) {
  const q = new URLSearchParams();
  if (year) q.set('year', year);
  if (month) q.set('month', month);
  const qs = q.toString();
  return apiFetch(`/employees/${employeeId}/leave${qs ? '?' + qs : ''}`);
}

export async function addEmployeeLeave(employeeId, date, leaveType, description) {
  return apiPost(`/employees/${employeeId}/leave`, { date, leave_type: leaveType, description: description || '' });
}

export async function deleteEmployeeLeave(employeeId, leaveId) {
  return apiDelete(`/employees/${employeeId}/leave/${leaveId}`);
}

export async function updateEmployeeLeave(employeeId, leaveId, date, leaveType, description) {
  return apiPut(`/employees/${employeeId}/leave/${leaveId}`, { date, leave_type: leaveType, description });
}

export async function addBulkEmployeeLeave(date, employeeIds, leaveType, description) {
  return apiPost('/employees/leave/bulk', { date, employee_ids: employeeIds, leave_type: leaveType, description: description || '' });
}

// ─── ATTENDANCE OVERRIDES ─────────────────────────────
export async function fetchAttendanceOverrides(date, employeeName) {
  const q = new URLSearchParams();
  if (date) q.set('date', date);
  if (employeeName) q.set('employee_name', employeeName);
  const qs = q.toString();
  return apiFetch(`/attendance/overrides${qs ? '?' + qs : ''}`);
}

export async function addAttendanceOverride(data) {
  return apiPost('/attendance/override', data);
}

export async function deleteAttendanceOverride(overrideId) {
  return apiDelete(`/attendance/override/${overrideId}`);
}

export function getTeamRangeCsvUrl(teamId, startDate, endDate) {
  return `${ZOOM_API_BASE}/teams/${teamId}/attendance/range?start=${startDate}&end=${endDate}&format=csv`;
}

export function getTeamMonthlyCsvUrl(teamId, year, month) {
  return `${ZOOM_API_BASE}/teams/${teamId}/report/monthly?year=${year}&month=${month}&format=csv`;
}

export function getTeamMonthlyEmployeeCsvUrl(teamId, year, month) {
  return `${ZOOM_API_BASE}/teams/${teamId}/report/monthly?year=${year}&month=${month}&format=employee_csv`;
}

export function getTeamSummaryCsvUrl(teamId, year, month) {
  return `${ZOOM_API_BASE}/teams/${teamId}/report/monthly?year=${year}&month=${month}&format=team_summary_csv`;
}

// ─── EMPLOYEE REGISTRY ─────────────────────────────────

export async function fetchEmployees(params = {}) {
  const q = new URLSearchParams(params).toString();
  return apiFetch(`/employees${q ? '?' + q : ''}`);
}

export async function createEmployee(data) {
  return apiPost('/employees', data);
}

export async function updateEmployee(id, data) {
  return apiPut(`/employees/${id}`, data);
}

export async function deleteEmployee(id) {
  return apiDelete(`/employees/${id}`);
}

export async function syncEmployeesFromTeams() {
  return apiPost('/employees/sync-from-teams', {});
}

export async function fetchUnrecognized(date) {
  return apiFetch(`/employees/unrecognized/${date}`);
}

export async function fetchUnrecognizedMonthly(year, month) {
  return apiFetch(`/employees/unrecognized-monthly?year=${year}&month=${month}`);
}

// ─── CHATBOT ──────────────────────────────────────────────
// Sends a prompt (or a confirm_token, for two-step edits) to the LLM-backed
// /chat endpoint. Unlike apiPost we DON'T throw on success:false here — the
// chatbot UI displays the message field directly so users can see real
// error text ("Chatbot module failed to load: ...") instead of a generic
// "API error" toast.
export async function sendChatPrompt({ prompt, user, role, confirmToken } = {}) {
  const res = await fetch(`${ZOOM_API_BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prompt: prompt || '',
      user: user || '',
      role: role || '',
      confirm_token: confirmToken || null,
    }),
  });
  // Try to parse JSON regardless of status code; surface raw text on parse fail.
  let data;
  try {
    data = await res.json();
  } catch (e) {
    const text = await res.text().catch(() => '');
    return { success: false, message: `Bad response (HTTP ${res.status}): ${text || res.statusText}` };
  }
  return data;
}

// Split a shared session ("A & B & C") to N employees. Accepts either the
// new form (array of employee objects) or legacy (employee1, employee2).
export async function splitSharedAttendance(sharedName, employeesOrFirst, secondOrDaily, dailyOrApply, applyAttendance = true) {
  // Legacy signature: (sharedName, employee1, employee2, daily, applyAttendance?)
  const isLegacy = !Array.isArray(employeesOrFirst);
  const body = isLegacy
    ? {
        shared_name: sharedName,
        employees: [employeesOrFirst, secondOrDaily].filter(Boolean),
        daily: dailyOrApply,
        apply_attendance: applyAttendance,
      }
    : {
        shared_name: sharedName,
        employees: employeesOrFirst,
        daily: secondOrDaily,
        apply_attendance: dailyOrApply !== undefined ? dailyOrApply : true,
      };
  return apiPost('/employees/split-shared-attendance', body);
}

export async function assignUnrecognizedAttendance(sourceName, employee, daily, markSource = true) {
  return apiPost('/employees/assign-attendance', {
    source_name: sourceName,
    employee,
    daily,
    mark_source: markSource,
  });
}

export async function fetchClassifiedMonthly(year, month, categories) {
  const cats = Array.isArray(categories) ? categories.join(',') : (categories || '');
  const catParam = cats ? `&categories=${encodeURIComponent(cats)}` : '';
  return apiFetch(`/employees/classified-monthly?year=${year}&month=${month}${catParam}`);
}

export async function fetchEmployeeDetail(employeeId, yearMonth) {
  return apiFetch(`/employees/${employeeId}/attendance/${yearMonth}`);
}

export function getEmployeeCsvUrl(employeeId, yearMonth) {
  return `${ZOOM_API_BASE}/employees/${employeeId}/attendance/${yearMonth}?format=csv`;
}

export async function fetchEmployeeYearlySummary(employeeId, year) {
  return apiFetch(`/employees/${employeeId}/report/yearly?year=${year}`);
}

// ─── SUPERADMIN DATA EDITOR ───────────────────────────────

export async function adminUpdateRole(userId, role) {
  return apiPost('/admin/update-role', { user_id: userId, role });
}

export async function adminSearchSnapshots(date, search) {
  const q = new URLSearchParams({ date });
  if (search) q.set('search', search);
  return apiFetch(`/admin/snapshots?${q}`);
}

export async function adminEditSnapshots(snapshotIds, fields) {
  return apiPut('/admin/snapshots/edit', { snapshot_ids: snapshotIds, ...fields });
}

export async function adminDeleteSnapshots(snapshotIds) {
  const res = await fetch(`${ZOOM_API_BASE}/admin/snapshots/delete`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ snapshot_ids: snapshotIds })
  });
  if (!res.ok) throw new Error(`API error ${res.status}: ${res.statusText}`);
  return res.json();
}

export async function adminAddSnapshots(rows) {
  return apiPost('/admin/snapshots/add', { rows });
}

export async function adminSearchEvents(date, search) {
  const q = new URLSearchParams({ date });
  if (search) q.set('search', search);
  return apiFetch(`/admin/events?${q}`);
}

export async function adminEditEvents(eventIds, fields) {
  return apiPut('/admin/events/edit', { event_ids: eventIds, ...fields });
}

export async function adminDeleteEvents(eventIds) {
  const res = await fetch(`${ZOOM_API_BASE}/admin/events/delete`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event_ids: eventIds })
  });
  if (!res.ok) throw new Error(`API error ${res.status}: ${res.statusText}`);
  return res.json();
}
