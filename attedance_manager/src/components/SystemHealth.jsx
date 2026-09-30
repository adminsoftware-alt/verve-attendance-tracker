import { useState, useEffect, useCallback } from 'react';
import { createRoomOverride } from '../utils/zoomApi';

// System Health — the backend's self-contained /health/dashboard page
// (tiles + pipeline checks, auto-refreshes every 60s) embedded as an iframe,
// plus a "Name a room" box so an admin can fix an unnamed room right here.
//
// Why the box lives in THIS app and not on the embedded page: naming a room
// is a room_override (top-priority input to the hours builder) and needs
// the admin login token, which only this app holds.
const API = 'https://breakout-room-calibrator-4e5na4tdha-uc.a.run.app';
const HEALTH_URL = `${API}/health/dashboard`;

export default function SystemHealth() {
  const [rooms, setRooms] = useState([]);
  const [names, setNames] = useState([]);
  const [bizDate, setBizDate] = useState('');
  const [draft, setDraft] = useState({});      // room_uuid -> typed name
  const [saving, setSaving] = useState(null);  // room_uuid being saved
  const [msg, setMsg] = useState(null);
  const [loadError, setLoadError] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API}/health/summary`);
      const d = await res.json();
      setRooms(Array.isArray(d.unnamed_rooms) ? d.unnamed_rooms : []);
      setNames(Array.isArray(d.room_names_today) ? d.room_names_today : []);
      setBizDate(d.business_date_ist || '');
      setLoadError(null);
    } catch (e) {
      setLoadError(`Could not load unnamed rooms: ${e.message}`);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [load]);

  const save = async (room) => {
    const name = (draft[room.room_uuid] || '').trim();
    if (!name) return;
    setSaving(room.room_uuid);
    setMsg(null);
    try {
      const res = await createRoomOverride({
        room_uuid: room.room_uuid,
        room_name: name,
        mapping_date: bizDate,          // an override is always for one day
        note: 'Named from System Health',
      });
      setMsg({ ok: true, text: `Saved "${name}". Rebuilt ${(res.rebuilt_dates || []).length} day(s) — the hours have already moved.` });
      // The summary is cached 60s server-side: drop the row now so the list
      // matches what just happened.
      setRooms((prev) => prev.filter((r) => r.room_uuid !== room.room_uuid));
    } catch (e) {
      setMsg({ ok: false, text: `Save failed: ${e.message}` });
    } finally {
      setSaving(null);
    }
  };

  return (
    <div style={s.wrap}>
      <div style={s.header}>
        <div>
          <h1 style={s.title}>System Health</h1>
          <p style={s.sub}>
            Webhook feed, duplicates, unknown-room time, disputed rooms, last build.
            Green = fine. Amber/red = read the tile and check that area first. All times IST.
          </p>
        </div>
        <a href={HEALTH_URL} target="_blank" rel="noreferrer" style={s.link}>
          Open full page {'↗'}
        </a>
      </div>

      <div style={s.box}>
        <div style={s.boxTitle}>Name a room {bizDate ? `— ${bizDate}` : ''}</div>
        <p style={s.sub}>
          Rooms still without a name today. 3+ person rooms get named automatically on the next
          Room Mapper run; name the small ones here. Type the room name (e.g. BREAK TIME) and Save —
          the whole day is rebuilt with the correct name.
        </p>
        {msg && (
          <div style={{ ...s.msg, color: msg.ok ? '#166534' : '#991b1b', background: msg.ok ? '#f0fdf4' : '#fef2f2' }}>
            {msg.text}
          </div>
        )}
        {loadError ? (
          <div style={{ ...s.msg, color: '#991b1b', background: '#fef2f2' }}>{loadError}</div>
        ) : rooms.length === 0 ? (
          <div style={{ ...s.sub, color: '#166534' }}>All rooms are named today.</div>
        ) : (
          <table style={s.table}>
            <thead>
              <tr>
                <th style={s.th}>People</th>
                <th style={s.th}>Minutes</th>
                <th style={s.th}>Last seen</th>
                <th style={s.th}>Who</th>
                <th style={s.th}>Room name</th>
                <th style={s.th}></th>
              </tr>
            </thead>
            <tbody>
              {rooms.map((r) => (
                <tr key={r.room_uuid}>
                  <td style={s.td}>{r.people}</td>
                  <td style={s.td}>{r.minutes}</td>
                  <td style={s.td}>
                    {r.last_seen} <span style={{ color: r.live ? '#166534' : '#64748b' }}>{r.live ? 'live' : 'left'}</span>
                  </td>
                  <td style={s.td} title={r.room_uuid}>{r.who}</td>
                  <td style={s.td}>
                    <input
                      list="room-name-suggestions"
                      value={draft[r.room_uuid] || ''}
                      onChange={(e) => setDraft({ ...draft, [r.room_uuid]: e.target.value })}
                      placeholder="e.g. BREAK TIME"
                      style={s.input}
                    />
                  </td>
                  <td style={s.td}>
                    <button
                      onClick={() => save(r)}
                      disabled={saving === r.room_uuid || !(draft[r.room_uuid] || '').trim()}
                      style={s.btn}
                    >
                      {saving === r.room_uuid ? 'Saving…' : 'Save'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <datalist id="room-name-suggestions">
          {names.map((n) => <option key={n} value={n} />)}
        </datalist>
      </div>

      <iframe title="System Health" src={HEALTH_URL} style={s.frame} />
    </div>
  );
}

const s = {
  wrap: { display: 'flex', flexDirection: 'column', height: '100%' },
  header: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start',
    gap: 12, marginBottom: 12, flexWrap: 'wrap',
  },
  title: { margin: 0, fontSize: 22, color: '#0f172a' },
  sub: { margin: '4px 0 0', fontSize: 13, color: '#64748b', maxWidth: 760 },
  link: {
    fontSize: 13, color: '#2563eb', textDecoration: 'none',
    padding: '6px 10px', border: '1px solid #bfdbfe', borderRadius: 6,
    whiteSpace: 'nowrap',
  },
  box: {
    border: '1px solid #e2e8f0', borderRadius: 8, padding: '12px 14px',
    marginBottom: 12, background: '#fff',
  },
  boxTitle: { fontSize: 15, fontWeight: 700, color: '#0f172a' },
  msg: { fontSize: 13, padding: '6px 10px', borderRadius: 6, margin: '8px 0' },
  table: { width: '100%', borderCollapse: 'collapse', marginTop: 8, fontSize: 13 },
  th: { textAlign: 'left', color: '#64748b', fontWeight: 600, padding: '6px 8px', borderBottom: '1px solid #e2e8f0' },
  td: { padding: '6px 8px', borderBottom: '1px solid #f1f5f9', verticalAlign: 'top' },
  input: { fontSize: 13, padding: '4px 8px', border: '1px solid #cbd5e1', borderRadius: 6, minWidth: 180 },
  btn: {
    fontSize: 13, padding: '5px 12px', border: 'none', borderRadius: 6,
    background: '#2563eb', color: '#fff', cursor: 'pointer',
  },
  frame: {
    flex: 1, width: '100%', minHeight: 480,
    border: '1px solid #e2e8f0', borderRadius: 8, background: '#fff',
  },
};
