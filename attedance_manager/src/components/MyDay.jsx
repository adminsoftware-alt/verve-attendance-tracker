import { useState, useEffect, useCallback, useMemo } from 'react';
import { fetchSummary, createRoomOverride, fetchRoomOverrides, rebuildDayViaProcedure } from '../utils/zoomApi';

/**
 * MY DAY — your own attendance for any date, and the place to correct a room.
 *
 * Why this screen exists: Zoom never sends a room name in its breakout
 * webhooks. The server guesses one when the webhook lands and freezes that
 * guess. When it guesses wrong the cost is real — on 2026-08-11 a work room
 * was stamped with the break room's name and 187 minutes of working time were
 * filed as break, for everyone who was in that room.
 *
 * Nobody can look at a list of room IDs and know which is which. But you know
 * where YOU were. That is the ground truth this screen collects: you find the
 * row you remember, say what the room really was, and the correction is stored
 * against the room UUID so it applies to everyone who was in it.
 */

function istDate() {
  // Business date: the attendance day runs 05:00->05:00 IST, so before
  // 05:00 IST "today" is still yesterday's business day.
  const now = new Date();
  return new Date(now.getTime() + 30 * 60000).toISOString().slice(0, 10);
}

function fmtMins(m) {
  if (!m) return '0m';
  const h = Math.floor(m / 60);
  const min = Math.round(m % 60);
  if (h > 0 && min > 0) return `${h}h ${min}m`;
  if (h > 0) return `${h}h`;
  return `${min}m`;
}

function catStyle(cat) {
  switch (cat) {
    case 'break':    return { background: '#fff7ed', color: '#9a3412', border: '1px solid #fed7aa' };
    case 'main':     return { background: '#f1f5f9', color: '#475569', border: '1px solid #e2e8f0' };
    default:         return { background: '#eff6ff', color: '#1d4ed8', border: '1px solid #bfdbfe' };
  }
}

export default function MyDay({ user }) {
  const [date, setDate] = useState(istDate);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Correction state
  const [editing, setEditing] = useState(null);   // the room_visit being corrected
  const [formName, setFormName] = useState('');
  const [formCategory, setFormCategory] = useState('break');
  const [formNote, setFormNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [savedMsg, setSavedMsg] = useState('');
  const [overrides, setOverrides] = useState([]);
  const [rebuilding, setRebuilding] = useState(false);

  const canOverride = user?.role === 'admin' || user?.role === 'superadmin';

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchSummary(date);
      setSummary(data);
    } catch (e) {
      setError(e.message);
      setSummary(null);
    }
    setLoading(false);
  }, [date]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!canOverride) return;
    fetchRoomOverrides(true)
      .then(d => setOverrides(d.overrides || []))
      .catch(() => { /* the list is a convenience; failing to load it is not fatal */ });
  }, [canOverride, savedMsg]);

  const people = useMemo(() => {
    const list = summary?.participants || [];
    return [...list].sort((a, b) => (a.name || '').localeCompare(b.name || ''));
  }, [summary]);

  // My Day shows YOU and nobody else. It is the one screen where the person
  // reading it is the ground truth — you know which room you were actually in,
  // and a correction is stored against the room UUID, so fixing it from your
  // own row fixes it for everyone who was in that room. There is no reason to
  // browse other people's days from here.
  //
  // Match on email first (stable), then on display name — the same order the
  // backend uses everywhere else. Zoom display names drift; emails do not.
  const me = useMemo(() => {
    if (!people.length) return null;
    const email = (user?.email || '').toLowerCase().trim();
    const name = (user?.name || '').toLowerCase().trim();
    return (
      people.find(p => email && (p.email || '').toLowerCase().trim() === email) ||
      people.find(p => name && (p.name || '').toLowerCase().trim() === name) ||
      null
    );
  }, [people, user]);

  const visits = me?.room_visits || [];

  const totals = useMemo(() => {
    let total = 0, breakMins = 0;
    visits.forEach(v => {
      const m = Number(v.room_duration_mins) || 0;
      total += m;
      if (v.room_category === 'break') breakMins += m;
    });
    return { total, breakMins, working: total - breakMins };
  }, [visits]);

  // Days built before v13 have no room_uuid on their intervals, so there is
  // nothing to attach a correction to. That is a stale-data problem, not a
  // permissions one — rebuilding the day through the stored procedure fills
  // the column in and the buttons appear. Main Room stays never have a room
  // ID, so they are excluded from the test.
  const needsRebuild = useMemo(() => {
    const breakoutVisits = visits.filter(v => v.room_category !== 'main');
    return breakoutVisits.length > 0 && breakoutVisits.every(v => !v.room_uuid);
  }, [visits]);

  const rebuildDay = async () => {
    setRebuilding(true);
    setError(null);
    try {
      await rebuildDayViaProcedure(date);
      await load();
      setSavedMsg(`Rebuilt ${date}. You can now correct rooms on this day.`);
    } catch (e) {
      setError(`Rebuild failed: ${e.message}`);
    }
    setRebuilding(false);
  };

  const openEditor = (v) => {
    setEditing(v);
    setFormName(v.room_name || '');
    setFormCategory(v.room_category === 'break' ? 'break' : 'break');
    setFormScope('date');
    setFormNote('');
    setSavedMsg('');
  };

  const submitOverride = async () => {
    if (!editing?.room_uuid) return;
    setSaving(true);
    setError(null);
    try {
      const res = await createRoomOverride({
        room_uuid: editing.room_uuid,
        room_name: formName.trim() || undefined,
        room_category: formCategory || undefined,
        mapping_date: date,   // an override is always for one day
        note: formNote.trim() || undefined,
      });
      setSavedMsg(
        `Saved. Rebuilt ${(res.rebuilt_dates || []).length} day(s)` +
        ((res.rebuild_errors || []).length ? ` — ${res.rebuild_errors.length} rebuild error(s)` : '')
      );
      setEditing(null);
      await load();          // the numbers have already changed server-side
    } catch (e) {
      setError(e.message);
    }
    setSaving(false);
  };

  const shiftDate = (days) => {
    // Local parts, not toISOString() — that converts to UTC and in IST would
    // step back an extra day.
    const d = new Date(`${date}T00:00:00`);
    d.setDate(d.getDate() + days);
    const p2 = (n) => String(n).padStart(2, '0');
    setDate(`${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`);
  };

  return (
    <div>
      <div style={s.header}>
        <div>
          <h2 style={s.title}>My Day</h2>
          <div style={s.subtitle}>Your rooms for a single day — and where to fix one that is labelled wrong</div>
        </div>
        <div style={s.controls}>
          <button onClick={() => shiftDate(-1)} style={s.navBtn} aria-label="Previous day">←</button>
          <input type="date" value={date} onChange={e => setDate(e.target.value)} style={s.dateInput} />
          <button onClick={() => shiftDate(1)} style={s.navBtn} aria-label="Next day">→</button>
          <button onClick={() => setDate(istDate())} style={s.todayBtn}>Today</button>
          <button onClick={load} disabled={loading} style={s.refreshBtn}>
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        </div>
      </div>

      {error && <div style={s.error}>{error}</div>}
      {savedMsg && <div style={s.ok}>{savedMsg}</div>}

      {/* Whose day? Yours. Not a chooser — a statement of who is signed in. */}
      <div style={s.whoBar}>
        <label style={s.whoLabel}>Showing</label>
        <span style={s.whoName}>
          {user?.name || 'You'}
          {user?.email && <span style={s.whoEmail}> ({user.email})</span>}
        </span>
      </div>

      {!loading && !me && people.length > 0 && (
        <div style={s.notFound}>
          No rooms recorded for <strong>{user?.email || user?.name}</strong> on this date.
          If you were on Zoom that day, the email you sign in with may differ from the
          one Zoom has for you — an admin can align them under Employees.
        </div>
      )}

      {loading && <div style={s.empty}>Loading…</div>}

      {!loading && me && canOverride && needsRebuild && (
        <div style={s.rebuildBar}>
          <span>
            This day was built before room IDs were recorded, so its rooms cannot be
            corrected yet. Rebuilding reads the same webhook events again and fills them in —
            it does not change anyone's hours by itself.
          </span>
          <button onClick={rebuildDay} disabled={rebuilding} style={s.rebuildBtn}>
            {rebuilding ? 'Rebuilding…' : 'Rebuild this day'}
          </button>
        </div>
      )}

      {!loading && me && (
        <>
          <div style={s.statsRow}>
            <Stat label="Total Zoom Hours" value={fmtMins(totals.total)} color="#0f172a" />
            <Stat label="Working Hours" value={fmtMins(totals.working)} color="#15803d" />
            <Stat label="Break Time" value={fmtMins(totals.breakMins)} color="#c2410c" />
            <Stat label="Rooms" value={visits.length} color="#2563eb" />
          </div>

          <div style={s.tableWrap}>
            <table style={s.table}>
              <thead>
                <tr>
                  <th style={s.th}>#</th>
                  <th style={s.th}>Room</th>
                  <th style={s.th}>Type</th>
                  <th style={s.th}>In</th>
                  <th style={s.th}>Out</th>
                  <th style={s.th}>Duration</th>
                  {canOverride && <th style={s.th}>Wrong room?</th>}
                </tr>
              </thead>
              <tbody>
                {visits.map((v, i) => (
                  <tr key={i} style={i % 2 === 0 ? s.trEven : {}}>
                    <td style={{ ...s.td, color: '#94a3b8' }}>{i + 1}</td>
                    <td style={{ ...s.td, fontWeight: 600 }}>
                      {v.room_name || '—'}
                      {(v.room_name || '').startsWith('Room-') && (
                        <span style={s.warnTag} title="This room was never named. Break time here is counted as working time.">
                          unnamed
                        </span>
                      )}
                    </td>
                    <td style={s.td}>
                      <span style={{ ...s.badge, ...catStyle(v.room_category) }}>
                        {v.room_category || '—'}
                      </span>
                    </td>
                    <td style={{ ...s.td, color: '#15803d' }}>{v.room_joined_ist || '—'}</td>
                    <td style={{ ...s.td, color: '#b91c1c' }}>{v.room_left_ist || '—'}</td>
                    <td style={{ ...s.td, fontWeight: 600 }}>{fmtMins(v.room_duration_mins)}</td>
                    {canOverride && (
                      <td style={s.td}>
                        {v.room_uuid ? (
                          <button onClick={() => openEditor(v)} style={s.fixBtn}>Correct this room</button>
                        ) : (
                          <span
                            style={s.hint}
                            title={v.room_category === 'main'
                              ? 'Main Room stays have no room ID to correct'
                              : 'No room ID on this row — rebuild the day to correct it'}
                          >—</span>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
                {visits.length === 0 && (
                  <tr><td colSpan={canOverride ? 7 : 6} style={s.empty}>No rooms recorded for this date.</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}

      {!loading && !me && people.length === 0 && (
        <div style={s.empty}>No attendance data for {date}.</div>
      )}

      {/* ── correction dialog ── */}
      {editing && (
        <div style={s.modalBg} onClick={() => setEditing(null)}>
          <div style={s.modal} onClick={e => e.stopPropagation()}>
            <h3 style={s.modalTitle}>Correct this room</h3>
            <p style={s.modalIntro}>
              Recorded as <strong>{editing.room_name}</strong>, {editing.room_joined_ist}–{editing.room_left_ist}.
              The correction is stored against the room, so it applies to
              everyone who was in it — not just you.
            </p>
            <div style={s.uuidBox}>{editing.room_uuid}</div>

            <label style={s.fieldLabel}>What was it really?</label>
            <div style={s.catRow}>
              {['break', 'breakout', 'main'].map(c => (
                <button
                  key={c}
                  onClick={() => setFormCategory(c)}
                  style={{ ...s.catBtn, ...(formCategory === c ? s.catBtnOn : {}) }}
                >
                  {c === 'break' ? 'Break room' : c === 'main' ? 'Main room' : 'Work room'}
                </button>
              ))}
            </div>
            <div style={s.helpText}>
              Only <strong>Break room</strong> changes anyone's hours — break time is
              subtracted from working hours. Work and Main are treated the same
              way in every report.
            </div>

            <label style={s.fieldLabel}>Room name (optional)</label>
            <input
              value={formName}
              onChange={e => setFormName(e.target.value)}
              placeholder="e.g. 8.0:BREAK TIME - Tea/Lunch/ Dinner"
              style={s.input}
            />

            <div style={s.helpText}>
              This correction applies to <strong>{date}</strong> only. Room IDs stay the
              same for days at a time and then rotate, so if the same room is wrong on
              another day, open that day and correct it there too.
            </div>

            <label style={s.fieldLabel}>Note (optional)</label>
            <input
              value={formNote}
              onChange={e => setFormNote(e.target.value)}
              placeholder="why you are changing it"
              style={s.input}
            />

            <div style={s.modalActions}>
              <button onClick={() => setEditing(null)} style={s.cancelBtn}>Cancel</button>
              <button onClick={submitOverride} disabled={saving} style={s.saveBtn}>
                {saving ? 'Saving and rebuilding…' : 'Save and rebuild'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── corrections already in force ── */}
      {canOverride && overrides.length > 0 && (
        <div style={{ marginTop: 28 }}>
          <h3 style={s.sectionTitle}>Corrections in force</h3>
          <div style={s.tableWrap}>
            <table style={s.table}>
              <thead>
                <tr>
                  <th style={s.th}>Room</th>
                  <th style={s.th}>Type</th>
                  <th style={s.th}>Applies to</th>
                  <th style={s.th}>By</th>
                  <th style={s.th}>Note</th>
                </tr>
              </thead>
              <tbody>
                {overrides.slice(0, 25).map(o => (
                  <tr key={o.override_id}>
                    <td style={s.td}>
                      <div style={{ fontWeight: 600 }}>{o.room_name || '(category only)'}</div>
                      <div style={s.uuidSmall}>{o.room_uuid}</div>
                    </td>
                    <td style={s.td}>
                      {o.room_category
                        ? <span style={{ ...s.badge, ...catStyle(o.room_category) }}>{o.room_category}</span>
                        : '—'}
                    </td>
                    <td style={s.td}>{o.mapping_date || '—'}</td>
                    <td style={s.td}>{o.set_by || '—'}</td>
                    <td style={{ ...s.td, color: '#64748b' }}>{o.note || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, color }) {
  return (
    <div style={s.statCard}>
      <div style={s.statLabel}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 800, color, lineHeight: 1.1 }}>{value}</div>
    </div>
  );
}

const s = {
  header: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 16, flexWrap: 'wrap', gap: 12 },
  title: { fontSize: 22, fontWeight: 800, color: '#0f172a', margin: 0 },
  subtitle: { fontSize: 12, color: '#64748b', marginTop: 3 },
  controls: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' },
  navBtn: { padding: '7px 12px', border: '1px solid #d1d5db', borderRadius: 8, background: '#fff', cursor: 'pointer', fontSize: 14, color: '#334155' },
  todayBtn: { padding: '7px 12px', border: '1px solid #d1d5db', borderRadius: 8, background: '#fff', cursor: 'pointer', fontSize: 12, color: '#334155' },
  dateInput: { padding: '8px 12px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13 },
  refreshBtn: { padding: '8px 16px', background: '#f1f5f9', color: '#475569', border: '1px solid #e5e7eb', borderRadius: 8, fontSize: 12, cursor: 'pointer' },

  whoBar: { display: 'flex', gap: 10, alignItems: 'center', marginBottom: 16, flexWrap: 'wrap' },
  whoLabel: { fontSize: 12, color: '#64748b', fontWeight: 600 },
  whoName: { fontSize: 14, fontWeight: 700, color: '#0f172a' },
  whoEmail: { fontSize: 13, fontWeight: 500, color: '#64748b' },
  rebuildBar: { display: 'flex', gap: 12, alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', padding: '12px 14px', background: '#eff6ff', color: '#1e40af', border: '1px solid #bfdbfe', borderRadius: 10, fontSize: 13, marginBottom: 16, lineHeight: 1.5 },
  rebuildBtn: { padding: '8px 14px', border: 'none', borderRadius: 8, background: '#2563eb', color: '#fff', fontSize: 13, fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap' },
  notFound: { padding: '12px 14px', background: '#fffbeb', color: '#92400e', border: '1px solid #fde68a', borderRadius: 10, fontSize: 13, marginBottom: 16, lineHeight: 1.5 },
  hint: { fontSize: 12, color: '#94a3b8' },

  error: { padding: '10px 14px', background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', borderRadius: 10, fontSize: 13, marginBottom: 14 },
  ok: { padding: '10px 14px', background: '#f0fdf4', color: '#15803d', border: '1px solid #bbf7d0', borderRadius: 10, fontSize: 13, marginBottom: 14 },
  empty: { textAlign: 'center', padding: '48px 20px', color: '#94a3b8', fontSize: 14 },

  statsRow: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10, marginBottom: 18 },
  statCard: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, padding: '14px 16px' },
  statLabel: { fontSize: 10, color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 4, fontWeight: 600 },

  sectionTitle: { fontSize: 14, fontWeight: 700, color: '#1e293b', margin: '0 0 10px' },
  tableWrap: { background: '#fff', border: '1px solid #e5e7eb', borderRadius: 12, overflow: 'auto' },
  table: { width: '100%', borderCollapse: 'collapse', minWidth: 720 },
  th: { padding: '10px 14px', textAlign: 'left', fontSize: 11, fontWeight: 600, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.05em', borderBottom: '1px solid #e5e7eb', background: '#f8fafc', whiteSpace: 'nowrap' },
  td: { padding: '10px 14px', fontSize: 13, color: '#1e293b', borderBottom: '1px solid #f1f5f9' },
  trEven: { background: '#fafbfc' },
  badge: { padding: '3px 10px', borderRadius: 12, fontSize: 11, fontWeight: 600, display: 'inline-block', textTransform: 'capitalize' },
  warnTag: { marginLeft: 8, padding: '2px 7px', borderRadius: 10, fontSize: 10, fontWeight: 700, background: '#fef3c7', color: '#92400e' },
  fixBtn: { padding: '5px 12px', background: '#eff6ff', color: '#2563eb', border: '1px solid #bfdbfe', borderRadius: 6, fontSize: 11, fontWeight: 600, cursor: 'pointer', whiteSpace: 'nowrap' },

  modalBg: { position: 'fixed', inset: 0, background: 'rgba(15,23,42,0.45)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 200, padding: 20 },
  modal: { background: '#fff', borderRadius: 14, padding: '22px 24px', width: 'min(540px, 100%)', maxHeight: '90vh', overflow: 'auto', boxShadow: '0 20px 50px rgba(0,0,0,0.25)' },
  modalTitle: { margin: '0 0 10px', fontSize: 18, fontWeight: 800, color: '#0f172a' },
  modalIntro: { margin: '0 0 12px', fontSize: 13, color: '#475569', lineHeight: 1.55 },
  uuidBox: { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 11, background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 8, padding: '8px 10px', color: '#475569', wordBreak: 'break-all', marginBottom: 16 },
  uuidSmall: { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 10, color: '#94a3b8', wordBreak: 'break-all' },
  fieldLabel: { display: 'block', fontSize: 11, fontWeight: 700, color: '#475569', textTransform: 'uppercase', letterSpacing: '0.05em', margin: '14px 0 6px' },
  catRow: { display: 'flex', gap: 8, flexWrap: 'wrap' },
  catBtn: { padding: '8px 14px', border: '1px solid #d1d5db', borderRadius: 8, background: '#fff', color: '#475569', fontSize: 12.5, cursor: 'pointer' },
  catBtnOn: { background: '#0f172a', color: '#fff', borderColor: '#0f172a', fontWeight: 600 },
  helpText: { fontSize: 11.5, color: '#64748b', marginTop: 7, lineHeight: 1.5 },
  input: { width: '100%', padding: '9px 12px', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13, boxSizing: 'border-box' },
  modalActions: { display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 22 },
  cancelBtn: { padding: '9px 18px', background: '#fff', color: '#475569', border: '1px solid #d1d5db', borderRadius: 8, fontSize: 13, cursor: 'pointer' },
  saveBtn: { padding: '9px 18px', background: '#2563eb', color: '#fff', border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: 'pointer' },
};
