/**
 * Storage utilities - Uses Cloud Run API for authentication and data
 * Replaces Supabase with direct BigQuery API calls
 */

const API_BASE = 'https://breakout-room-calibrator-1073587167150.us-central1.run.app';

const STORAGE_KEY = 'verve_attendance_data';

// ─── AUTH ───────────────────────────────────────────────

const DEFAULT_USERS = [
  { username: 'admin', password: 'verve2026', name: 'Admin', role: 'admin', email: '' },
  { username: 'hr1', password: 'verve2026', name: 'HR User 1', role: 'hr', email: '' },
  { username: 'hr2', password: 'verve2026', name: 'HR User 2', role: 'hr', email: '' },
  { username: 'manager1', password: 'verve2026', name: 'Manager 1', role: 'manager', email: '' },
  { username: 'manager2', password: 'verve2026', name: 'Manager 2', role: 'manager', email: '' },
];

export async function validateLogin(username, password) {
  try {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });
    const data = await res.json();
    if (data.success && data.user) {
      return data.user;
    }
    return null;
  } catch (err) {
    console.error('Login API error, using fallback:', err);
    // Fallback: local
    return DEFAULT_USERS.find(u => u.username === username && u.password === password) || null;
  }
}

export function getSession() {
  try {
    const s = sessionStorage.getItem('verve_session');
    return s ? JSON.parse(s) : null;
  } catch { return null; }
}

export function setSession(user) {
  sessionStorage.setItem('verve_session', JSON.stringify(user));
}

export function clearSession() {
  sessionStorage.removeItem('verve_session');
}

// ─── ATTENDANCE DATA ────────────────────────────────────

export async function getAllData() {
  try {
    const res = await fetch(`${API_BASE}/data/attendance`);
    const data = await res.json();
    if (data.success && data.dates) {
      const result = {};
      data.dates.forEach(row => { result[row.report_date] = row.employees; });
      return result;
    }
    return {};
  } catch (err) {
    console.error('Attendance API error, using fallback:', err);
    // Fallback: localStorage
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch { return {}; }
  }
}

export async function getDayData(dateStr) {
  try {
    const res = await fetch(`${API_BASE}/data/attendance/${dateStr}`);
    const data = await res.json();
    if (data.success && data.employees) {
      return data.employees;
    }
    return null;
  } catch (err) {
    console.error('Day data API error:', err);
    const all = await getAllData();
    return all[dateStr] || null;
  }
}

export async function saveDayData(dateStr, employees, uploadedBy) {
  try {
    const res = await fetch(`${API_BASE}/data/attendance`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        report_date: dateStr,
        employees: employees,
        uploaded_by: uploadedBy || 'unknown'
      })
    });
    const data = await res.json();
    if (!data.success) throw new Error(data.error || 'Save failed');
    return;
  } catch (err) {
    console.error('Save API error, using fallback:', err);
    // Fallback: localStorage
    const all = await getAllData();
    all[dateStr] = employees;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
  }
}

export async function deleteDayData(dateStr) {
  try {
    const res = await fetch(`${API_BASE}/data/attendance/${dateStr}`, {
      method: 'DELETE'
    });
    const data = await res.json();
    if (!data.success) console.error('Delete API error:', data.error);
    return;
  } catch (err) {
    console.error('Delete API error, using fallback:', err);
    const all = await getAllData();
    delete all[dateStr];
    localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
  }
}

export async function getUploadedDates() {
  try {
    const res = await fetch(`${API_BASE}/data/attendance/dates`);
    const data = await res.json();
    if (data.success && data.dates) {
      return data.dates;
    }
    return [];
  } catch (err) {
    console.error('Dates API error, using fallback:', err);
    const all = await getAllData();
    return Object.keys(all).sort();
  }
}
