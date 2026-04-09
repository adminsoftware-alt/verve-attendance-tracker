import { useState, useEffect, useCallback } from 'react';
import { fetchTeamHolidays, addTeamHoliday, deleteTeamHoliday } from '../utils/zoomApi';

/**
 * Modal for managing holidays on a team. Lets an admin see / add / remove
 * holiday dates for the selected team and month. Holidays are per-team so
 * different teams can have different holiday calendars.
 */
export default function HolidayManager({ teamId, teamName, year, month, onClose, onChange }) {
  const [holidays, setHolidays] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  const [newDate, setNewDate] = useState(`${year}-${String(month).padStart(2, '0')}-01`);
  const [newDesc, setNewDesc] = useState('');
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const res = await fetchTeamHolidays(teamId, year, month);
      setHolidays(res.holidays || []);
    } catch (e) {
      setErr(e.message);
    }
    setLoading(false);
  }, [teamId, year, month]);

  useEffect(() => { load(); }, [load]);

  const handleAdd = async (e) => {
    e.preventDefault();
    if (!newDate) { setErr('Pick a date'); return; }
    setSaving(true);
    setErr(null);
    try {
      await addTeamHoliday(teamId, newDate, newDesc);
      setNewDesc('');
      await load();
      onChange && onChange();
    } catch (ex) {
      setErr(ex.message);
    }
    setSaving(false);
  };

  const handleDelete = async (holidayId) => {
    try {
      await deleteTeamHoliday(teamId, holidayId);
      await load();
      onChange && onChange();
    } catch (e) {
      setErr(e.message);
    }
  };

  // Constrain the "add date" input to the selected month for clarity
  const monthStr = String(month).padStart(2, '0');
  const daysInMonth = new Date(year, month, 0).getDate();
  const minDate = `${year}-${monthStr}-01`;
  const maxDate = `${year}-${monthStr}-${String(daysInMonth).padStart(2, '0')}`;

  return (
    <div style={s.overlay} onClick={onClose}>
      <div style={s.modal} onClick={e => e.stopPropagation()}>
        <div style={s.header}>
          <div>
            <h3 style={{ margin: 0, fontSize: 16 }}>Manage Holidays</h3>
            <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>
              {teamName} — {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][month - 1]} {year}
            </div>
          </div>
          <button onClick={onClose} style={s.close}>×</button>
        </div>

        <div style={s.body}>
          {/* Add form */}
          <form onSubmit={handleAdd} style={s.addForm}>
            <div style={s.formGroup}>
              <label style={s.label}>Date</label>
              <input
                type="date"
                value={newDate}
                min={minDate}
                max={maxDate}
                onChange={e => setNewDate(e.target.value)}
                style={s.input}
                required
              />
            </div>
            <div style={{ ...s.formGroup, flex: 1 }}>
              <label style={s.label}>Description</label>
              <input
                type="text"
                value={newDesc}
                onChange={e => setNewDesc(e.target.value)}
                placeholder="e.g. Diwali, Team off-site"
                style={s.input}
              />
            </div>
            <button type="submit" disabled={saving} style={s.addBtn}>
              {saving ? 'Adding...' : '+ Add'}
            </button>
          </form>

          {err && <div style={s.error}>{err}</div>}

          {/* List */}
          <div style={s.sectionTitle}>Holidays this month</div>
          {loading && <div style={s.empty}>Loading...</div>}
          {!loading && holidays.length === 0 && (
            <div style={s.empty}>No holidays configured for this month.</div>
          )}
          {!loading && holidays.length > 0 && (
            <div style={s.tableWrap}>
              <table style={s.table}>
                <thead>
                  <tr>
                    <th style={s.th}>Date</th>
                    <th style={s.th}>Day</th>
                    <th style={s.th}>Description</th>
                    <th style={s.th}></th>
                  </tr>
                </thead>
                <tbody>
                  {holidays.map(h => {
                    const [y, m, d] = h.date.split('-').map(Number);
                    const dow = new Date(y, m - 1, d).getDay();
                    const dayName = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'][dow];
                    return (
                      <tr key={h.holiday_id}>
                        <td style={s.td}><strong>{h.date}</strong></td>
                        <td style={s.td}>{dayName}</td>
                        <td style={s.td}>{h.description || <span style={{ color: '#94a3b8' }}>—</span>}</td>
                        <td style={s.td}>
                          <button
                            onClick={() => handleDelete(h.holiday_id)}
                            style={s.deleteBtn}
                            title="Remove holiday"
                          >
                            Remove
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          <div style={s.hint}>
            Holidays are per team. Adding a holiday here marks that date as
            non-working for <strong>{teamName}</strong> only — it won&apos;t
            count as an absence in reports for this team.
          </div>
        </div>
      </div>
    </div>
  );
}

const s = {
  overlay: {
    position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
    background: 'rgba(15,23,42,0.45)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    zIndex: 1000,
  },
  modal: {
    background: '#fff', borderRadius: 14,
    width: 'min(620px, 94vw)', maxHeight: '90vh',
    boxShadow: '0 20px 40px rgba(0,0,0,0.2)',
    display: 'flex', flexDirection: 'column',
  },
  header: {
    padding: '16px 20px', borderBottom: '1px solid #e5e7eb',
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
  },
  close: { background: 'none', border: 'none', fontSize: 26, cursor: 'pointer', color: '#94a3b8', padding: 0, lineHeight: 1 },
  body: { padding: '18px 20px', overflow: 'auto', flex: 1 },

  addForm: {
    display: 'flex', gap: 10, alignItems: 'flex-end', marginBottom: 14,
    padding: 12, background: '#f8fafc',
    border: '1px solid #e5e7eb', borderRadius: 10,
  },
  formGroup: { display: 'flex', flexDirection: 'column', gap: 4 },
  label: { fontSize: 10, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.03em', fontWeight: 600 },
  input: { padding: '7px 10px', border: '1px solid #d1d5db', borderRadius: 6, fontSize: 12, background: '#fff' },

  addBtn: {
    padding: '7px 16px', background: '#f97316', color: '#fff',
    border: 'none', borderRadius: 6, fontSize: 12, fontWeight: 700,
    cursor: 'pointer', whiteSpace: 'nowrap',
  },
  deleteBtn: {
    padding: '4px 10px', background: '#fef2f2', color: '#b91c1c',
    border: '1px solid #fecaca', borderRadius: 6, fontSize: 11,
    fontWeight: 600, cursor: 'pointer',
  },

  sectionTitle: {
    fontSize: 11, fontWeight: 700, color: '#64748b',
    textTransform: 'uppercase', letterSpacing: '0.04em',
    marginBottom: 8, marginTop: 4,
  },
  tableWrap: { border: '1px solid #e5e7eb', borderRadius: 8, overflow: 'hidden', marginBottom: 14 },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: {
    padding: '8px 12px', textAlign: 'left', fontSize: 10, fontWeight: 600,
    color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.04em',
    borderBottom: '1px solid #e5e7eb', background: '#f8fafc',
  },
  td: { padding: '8px 12px', fontSize: 12, color: '#1e293b', borderBottom: '1px solid #f1f5f9' },

  empty: { textAlign: 'center', padding: '24px 12px', color: '#94a3b8', fontSize: 12, background: '#f8fafc', border: '1px dashed #e5e7eb', borderRadius: 8, marginBottom: 14 },
  error: { padding: '8px 12px', background: '#fef2f2', color: '#dc2626', border: '1px solid #fecaca', borderRadius: 6, fontSize: 12, marginBottom: 12 },
  hint: { fontSize: 11, color: '#64748b', padding: '10px 12px', background: '#fff7ed', border: '1px solid #fed7aa', borderRadius: 6 },
};
