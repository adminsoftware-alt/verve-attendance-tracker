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
