import * as XLSX from 'xlsx';

// Convert Excel time serial (0.4375 = 10:30) to "HH:MM"
function excelTimeToStr(val) {
  if (!val && val !== 0) return '';
  if (typeof val === 'string') {
    const m = val.match(/^(\d{1,2}):(\d{2})/);
    if (m) return m[1].padStart(2, '0') + ':' + m[2];
    return '';
  }
  if (typeof val === 'number') {
    const totalMin = Math.round(val * 24 * 60);
    const h = Math.floor(totalMin / 60) % 24;
    const min = totalMin % 60;
    return String(h).padStart(2, '0') + ':' + String(min).padStart(2, '0');
  }
  return '';
}

function cleanStr(v) {
  if (v === null || v === undefined) return '';
  return String(v).trim();
}

// Detect format from headers and parse accordingly
export function parseFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = (e) => {
      try {
        const data = new Uint8Array(e.target.result);
        const wb = XLSX.read(data, { type: 'array' });
        const ws = wb.Sheets[wb.SheetNames[0]];
        const rows = XLSX.utils.sheet_to_json(ws, { defval: '' });

        if (!rows.length) {
          resolve([]);
          return;
        }

        const headers = Object.keys(rows[0]).map(h => h.toLowerCase().trim());

        // New format: per-room-visit rows with Room_Joined_IST, Room_Left_IST, Duration_Minutes
        if (headers.some(h => h.includes('room_joined')) || headers.some(h => h.includes('duration_minutes'))) {
          resolve(parseNewFormat(rows));
        }
        // Old format: Room_History column with inline room data
        else if (headers.some(h => h.includes('room_history'))) {
          resolve(parseOldFormat(rows));
        }
        // Fallback: try new format
        else {
          resolve(parseNewFormat(rows));
        }
      } catch (err) {
        reject(err);
      }
    };
    reader.onerror = () => reject(new Error('Failed to read file'));
    reader.readAsArrayBuffer(file);
  });
}

// Find column by partial match
function findCol(row, ...partials) {
  const keys = Object.keys(row);
  for (const p of partials) {
    const found = keys.find(k => k.toLowerCase().includes(p.toLowerCase()));
    if (found) return found;
  }
  return null;
}

function parseNewFormat(rows) {
  const sample = rows[0];
  const nameCol = findCol(sample, 'name', 'Name');
  const emailCol = findCol(sample, 'email', 'Email');
  const mainJoinCol = findCol(sample, 'main_joined', 'Main_Joined');
  const mainLeftCol = findCol(sample, 'main_left', 'Main_Left');
  const roomCol = findCol(sample, 'room', 'Room');
  const roomJoinCol = findCol(sample, 'room_joined', 'Room_Joined');
  const roomLeftCol = findCol(sample, 'room_left', 'Room_Left');
  const durMinCol = findCol(sample, 'duration_min', 'Duration_Min');

  // Group by employee (name + email)
  const empMap = {};

  for (const row of rows) {
    const rawName = cleanStr(row[nameCol]);
    if (!rawName) continue;

    const email = cleanStr(row[emailCol]).replace(/^none$/i, '');
    const mainJoin = excelTimeToStr(row[mainJoinCol]);
    const mainLeft = excelTimeToStr(row[mainLeftCol]);
    const roomName = cleanStr(row[roomCol]);
    const roomJoin = excelTimeToStr(row[roomJoinCol]);
    const roomLeft = excelTimeToStr(row[roomLeftCol]);
    const durMin = parseFloat(row[durMinCol]) || 0;

    // Strip session suffixes like -2, -3, -DND, -recording
    const baseName = rawName.replace(/-(DND|\d+|recording)$/i, '').trim();
    const key = (email || baseName).toLowerCase();

    if (!empMap[key]) {
      empMap[key] = { name: baseName, email, mainJoin: '', mainLeft: '', rooms: [], rawNames: new Set() };
    }

    const emp = empMap[key];
    // Prefer capitalized version (e.g. "Dev" over "dev")
    if (!/(-(DND|\d+|recording))$/i.test(rawName) && baseName && baseName[0] !== baseName[0].toLowerCase()) emp.name = baseName;
    emp.rawNames.add(rawName);

    // Track earliest join / latest left
    if (mainJoin && (!emp.mainJoin || mainJoin < emp.mainJoin)) emp.mainJoin = mainJoin;
    if (mainLeft && (!emp.mainLeft || mainLeft > emp.mainLeft)) emp.mainLeft = mainLeft;

    if (roomName && roomName !== '-') {
      emp.rooms.push({
        name: roomName,
        start: roomJoin,
        end: roomLeft,
        duration: durMin,
        isNamed: roomName.includes(':') && !roomName.startsWith('Room-'),
        session: rawName
      });
    }
  }

  return Object.values(empMap).map(emp => {
    const totalMin = emp.rooms.reduce((s, r) => s + r.duration, 0);
    return {
      name: emp.name,
      email: emp.email,
      joined: emp.mainJoin,
      left: emp.mainLeft,
      totalMinutes: totalMin,
      duration: formatDuration(totalMin),
      sessions: emp.rawNames.size,
      rooms: emp.rooms.sort((a, b) => dayMinutes(a.start) - dayMinutes(b.start))
    };
  }).sort((a, b) => a.name.localeCompare(b.name));
}

function parseOldFormat(rows) {
  const sample = rows[0];
  const nameCol = findCol(sample, 'name');
  const emailCol = findCol(sample, 'email');
  const joinCol = findCol(sample, 'joined', 'main_joined');
  const leftCol = findCol(sample, 'left', 'main_left');
  const durCol = findCol(sample, 'duration', 'total_duration');
  const rhCol = findCol(sample, 'room_history');

  const empMap = {};

  for (const row of rows) {
    const rawName = cleanStr(row[nameCol]);
    if (!rawName) continue;

    const email = cleanStr(row[emailCol]).replace(/^none$/i, '');
    const joined = excelTimeToStr(row[joinCol]);
    const left = excelTimeToStr(row[leftCol]);
    const durStr = cleanStr(row[durCol]);
    const rh = cleanStr(row[rhCol]);

    const baseName = rawName.replace(/-(DND|\d+|recording)$/i, '').trim();
    const key = (email || baseName).toLowerCase();

    if (!empMap[key]) {
      empMap[key] = { name: baseName, email, joined: '', left: '', rooms: [], totalMinutes: 0, sessionCount: 0 };
    }

    const emp = empMap[key];
    // Prefer capitalized version (e.g. "Dev" over "dev")
    if (!/(-(DND|\d+|recording))$/i.test(rawName) && baseName && baseName[0] !== baseName[0].toLowerCase()) emp.name = baseName;

    if (joined && (!emp.joined || dayMinutes(joined) < dayMinutes(emp.joined))) emp.joined = joined;
    if (left && (!emp.left || dayMinutes(left) > dayMinutes(emp.left))) emp.left = left;
    emp.totalMinutes += parseDurationStr(durStr);
    emp.sessionCount++;

    // Parse room history: "RoomName [HH:MM-HH:MM] -> ..."
    if (rh && rh !== '-') {
      rh.split('->').forEach(seg => {
        const m = seg.trim().match(/^(.+?)\s*\[(\d{2}:\d{2})-(\d{2}:\d{2})\]$/);
        if (m) {
          const rName = m[1].trim();
          const start = m[2];
          const end = m[3];
          const startMin = timeToMin(start);
          let endMin = timeToMin(end);
          if (endMin < startMin) endMin += 1440; // interval crosses midnight
          const durMins = endMin - startMin;
          emp.rooms.push({
            name: rName,
            start,
            end,
            duration: Math.max(durMins, 0),
            isNamed: rName.includes(':') && !rName.startsWith('Room-'),
            session: rawName
          });
        }
      });
    }
  }

  return Object.values(empMap).map(emp => ({
    name: emp.name,
    email: emp.email,
    joined: emp.joined,
    left: emp.left,
    totalMinutes: emp.totalMinutes,
    duration: formatDuration(emp.totalMinutes),
    sessions: emp.sessionCount,
    rooms: emp.rooms.sort((a, b) => dayMinutes(a.start) - dayMinutes(b.start))
  })).sort((a, b) => a.name.localeCompare(b.name));
}

// Merge duplicate employees within a single day (same name, case-insensitive)
// Handles data that was stored before dedup fix, or multiple sessions
export function mergeDayEmployees(employees) {
  if (!employees || !employees.length) return employees;

  const map = {};
  for (const emp of employees) {
    const key = emp.name.toLowerCase();
    if (!map[key]) {
      map[key] = { ...emp, rooms: [...(emp.rooms || [])] };
      continue;
    }
    const m = map[key];
    // Prefer capitalized name
    if (emp.name && emp.name[0] !== emp.name[0].toLowerCase()) m.name = emp.name;
    if (emp.email && !m.email) m.email = emp.email;
    // Earliest joined, latest left — by business-day order (00:33 is LATER
    // than 23:04 within a 05:00->05:00 day), never by string compare.
    if (emp.joined && (!m.joined || dayMinutes(emp.joined) < dayMinutes(m.joined))) m.joined = emp.joined;
    if (emp.left && (!m.left || dayMinutes(emp.left) > dayMinutes(m.left))) m.left = emp.left;
    // Combine rooms and minutes
    m.rooms = [...m.rooms, ...(emp.rooms || [])];
    m.totalMinutes = (m.totalMinutes || 0) + (emp.totalMinutes || 0);
    m.sessions = (m.sessions || 1) + (emp.sessions || 1);
  }

  return Object.values(map).map(emp => {
    // Sort rooms chronologically within the business day
    emp.rooms.sort((a, b) => dayMinutes(a.start) - dayMinutes(b.start));
    // Fill joined/left from rooms ONLY when missing. The backend computes
    // first/last seen from real timestamps (correct across midnight) —
    // never overwrite a value that is already present.
    const roomStarts = emp.rooms.map(r => r.start).filter(Boolean);
    const roomEnds = emp.rooms.map(r => r.end).filter(Boolean);
    if (!emp.joined && roomStarts.length) {
      emp.joined = roomStarts.sort((a, b) => dayMinutes(a) - dayMinutes(b))[0];
    }
    if (!emp.left && roomEnds.length) {
      emp.left = roomEnds.sort((a, b) => dayMinutes(a) - dayMinutes(b)).pop();
    }
    // Recalculate duration
    emp.duration = formatDuration(emp.totalMinutes);
    return emp;
  }).sort((a, b) => a.name.localeCompare(b.name));
}

// Today's BUSINESS date. The attendance day runs 05:00->05:00 IST, so
// before 05:00 IST "today" is still yesterday's business day.
// IST offset (+330 min) minus the 5h day-start (-300 min) = +30 min.
export function todayIST() {
  const now = new Date();
  const ist = new Date(now.getTime() + (30 * 60000));
  return ist.toISOString().slice(0, 10);
}

export function timeToMin(t) {
  if (!t) return 0;
  const [h, m] = t.split(':').map(Number);
  return h * 60 + (m || 0);
}

// Minutes for ORDERING within a business day (05:00 -> 05:00 IST).
// Times before 05:00 belong to the tail end of the previous evening's
// shift, so "00:33" must sort AFTER "23:04" — add 24h to them.
// Use this (never raw string compare or timeToMin) whenever comparing
// or sorting "HH:MM" strings within one attendance day.
export function dayMinutes(t) {
  if (!t) return 0;
  const m = timeToMin(t);
  return m < 300 ? m + 1440 : m;
}

export function minToTime(m) {
  if (!m && m !== 0) return '--:--';
  const h = Math.floor(m / 60) % 24;
  const min = Math.round(m % 60);
  return String(h).padStart(2, '0') + ':' + String(min).padStart(2, '0');
}

export function formatDuration(mins) {
  if (!mins) return '0min';
  const h = Math.floor(mins / 60);
  const m = Math.round(mins % 60);
  if (h > 0 && m > 0) return h + 'hr ' + m + 'min';
  if (h > 0) return h + 'hr';
  return m + 'min';
}

function parseDurationStr(s) {
  if (!s) return 0;
  const h = s.match(/(\d+)h/);
  const m = s.match(/(\d+)m/);
  return (h ? +h[1] * 60 : 0) + (m ? +m[1] : 0);
}
