/**
 * Storage utilities - Uses Cloud Run API for authentication and data
 * Replaces Supabase with direct BigQuery API calls
 */

const API_BASE = 'https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app';

const STORAGE_KEY = 'verve_attendance_data';

// ─── AUTH ───────────────────────────────────────────────
// (DEFAULT_USERS local fallback removed: it shipped plaintext credentials in
// the JS bundle and bypassed the server. Login is server-side only now.)

const TOKEN_KEY = 'verve_token';

export function getAuthToken() {
  try { return sessionStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}

export function authHeaders() {
  const t = getAuthToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export async function validateLogin(username, password) {
  try {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });
    const data = await res.json();
    if (data.success && data.user) {
      if (data.token) {
        try { sessionStorage.setItem(TOKEN_KEY, data.token); } catch { /* ignore */ }
      }
      return data.user;
    }
    return null;
  } catch (err) {
    console.error('Login API error:', err);
    return null;
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
  try { sessionStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ }
}

// ─── ATTENDANCE DATA ────────────────────────────────────

// DEPRECATED: Don't load all historical data at startup - use getUploadedDates() + getDayData() instead
// This function now returns empty to prevent performance degradation as data grows
export async function getAllData() {
  console.warn('getAllData() is deprecated - use getUploadedDates() + getDayData() for lazy loading');
  // Return empty - data should be loaded on-demand per date
  return {};
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
    // Fallback: read localStorage directly (getAllData() is deprecated and returns {})
    let all = {};
    try { all = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); } catch { all = {}; }
    return all[dateStr] || null;
  }
}

export async function saveDayData(dateStr, employees, uploadedBy) {
  try {
    const res = await fetch(`${API_BASE}/data/attendance`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
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
    // Fallback: read/merge/write localStorage directly (getAllData() is deprecated and returns {})
    let all = {};
    try { all = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); } catch { all = {}; }
    all[dateStr] = employees;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
  }
}

export async function deleteDayData(dateStr) {
  try {
    const res = await fetch(`${API_BASE}/data/attendance/${dateStr}`, {
      method: 'DELETE',
      headers: authHeaders()
    });
    const data = await res.json();
    if (!data.success) console.error('Delete API error:', data.error);
    return;
  } catch (err) {
    console.error('Delete API error, using fallback:', err);
    try {
      const all = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
      delete all[dateStr];
      localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
    } catch (e) {
      console.error('localStorage fallback failed:', e);
    }
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
    try {
      const all = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
      return Object.keys(all).sort();
    } catch (e) {
      console.error('localStorage fallback failed:', e);
      return [];
    }
  }
}
